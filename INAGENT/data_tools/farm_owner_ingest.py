# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农场主独立流程：消费 schema gaps，执行树结构裁决与图更新。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
from INAGENT.rag.graphrag_integration import GraphRAGRetriever
from INAGENT.rag.knowledge_schema import FarmOwnerReport, FillRequest, SchemaGapEntry
from INAGENT.utils.env_utils import load_inagent_env
from INAGENT.web.deps import get_llm_model

logger = logging.getLogger("farm_owner_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
_GRAPH_DIR = _INAGENT_ROOT / "graphrag_index"
_QUALITY_GATE_FILE = "_quality_gate_for_owner.jsonl"
_QUALITY_REFERENCE_FOR_OWNER_FILE = "_quality_reference_for_owner.json"
_OWNER_DECISIONS_FILE = "_owner_decisions_for_farmer.jsonl"
_OWNER_DECISION_SCHEMA_VERSION = "1.0"
_PASSTHROUGH_LLM_META_FIELDS = (
    "intent",
    "config_mode",
    "required_keywords",
    "product_module",
    "protocol_type",
    "command_prefix",
    "description",
    "section_title",
    "parent_section",
    "scenario_id",
    "step_type",
    "function_hierarchy",
    "command_structure",
    "chunk_type",
    "override_commands",
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _load_gap_entries(gaps_file: Path) -> List[SchemaGapEntry]:
    entries: List[SchemaGapEntry] = []
    field_names = {field.name for field in dc_fields(SchemaGapEntry)}
    for line in gaps_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            filtered = {key: value for key, value in raw.items() if key in field_names}
            entries.append(SchemaGapEntry(**filtered))
        except Exception as exc:
            logger.warning("[farm-owner] 跳过非法 gap 条目: %s", exc)
    return entries


def _load_quality_gate(gate_file: Path) -> Dict[str, Dict[str, object]]:
    gate: Dict[str, Dict[str, object]] = {}
    for line in gate_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        source_file = str(item.get("source_file") or "")
        block_id = str(item.get("block_id") or "")
        if not source_file:
            continue
        key = f"{source_file}::{block_id}"
        gate[key] = {
            "source_file": source_file,
            "block_id": block_id,
            "chunk_index": item.get("chunk_index"),
            "is_product_knowledge": bool(item.get("is_product_knowledge", False)),
            "section_title": item.get("section_title"),
            "document_category": item.get("document_category"),
        }
    return gate


def _load_quality_filtered_reference(reference_dir: Path) -> List[Dict[str, object]]:
    ref_path = reference_dir / _QUALITY_REFERENCE_FOR_OWNER_FILE
    if not ref_path.exists() or ref_path.stat().st_size == 0:
        return []
    try:
        payload = json.loads(ref_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[farm-owner] 读取 quality filtered reference 失败: %s", exc)
        return []
    if not isinstance(payload, list):
        return []
    return [chunk for chunk in payload if isinstance(chunk, dict)]


def _filter_entries_by_quality_gate(
    entries: List[SchemaGapEntry],
    gate: Dict[str, Dict[str, object]],
) -> List[SchemaGapEntry]:
    filtered: List[SchemaGapEntry] = []
    for entry in entries:
        source_file = str(entry.source_file or "")
        block_id = str(entry.chunk_block_id or "")
        if source_file:
            key = f"{source_file}::{block_id}"
            gate_item = gate.get(key)
            # 强门控：若有 gate 记录且被拒绝，则不允许进入农场主结构裁决。
            if gate_item and not bool(gate_item.get("is_product_knowledge", False)):
                continue
        filtered.append(entry)
    return filtered


def _filter_chunks_by_quality_gate(
    chunks: List[Dict[str, object]],
    gate: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    passed_keys = {
        key for key, item in gate.items() if bool(item.get("is_product_knowledge", False))
    }
    if not passed_keys:
        return []

    filtered: List[Dict[str, object]] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        source_file = str(meta.get("source_file") or "")
        block_id = str(meta.get("block_id") or "")
        if not source_file or not block_id:
            continue
        if f"{source_file}::{block_id}" in passed_keys:
            filtered.append(chunk)
    return filtered


def _build_block_id_to_source(
    chunks: List[Dict[str, object]],
) -> Dict[str, Optional[str]]:
    block_id_to_source: Dict[str, Optional[str]] = {}
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        block_id = str(meta.get("block_id") or "")
        source_file = str(meta.get("source_file") or "")
        if not block_id or not source_file:
            continue
        if block_id not in block_id_to_source:
            block_id_to_source[block_id] = source_file
        elif block_id_to_source[block_id] != source_file:
            block_id_to_source[block_id] = None
    return block_id_to_source


def _build_quality_passed_llm_patches(
    chunks: List[Dict[str, object]],
) -> Dict[str, Dict[str, object]]:
    if not chunks:
        return {}

    try:
        from INAGENT.data_tools.auto_convert import (
            _apply_llm_metadata_extraction_batch,
            _load_project_config,
        )
    except Exception as exc:
        logger.warning("[farm-owner] 无法加载 LLM metadata 增强器: %s", exc)
        return {}

    llm_config = (
        _load_project_config()
        .get("llm-aided-config", {})
        .get("metadata_extraction", {})
    )
    if not llm_config.get("enable", False):
        logger.info("[farm-owner] metadata LLM disabled in config; skip enhancement")
        return {}

    work_items = []
    indexed_items = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        source_file = str(meta.get("source_file") or "")
        block_id = str(meta.get("block_id") or "")
        text = str(chunk.get("page_content") or chunk.get("text") or "").strip()
        if not source_file or not block_id or not text:
            continue
        meta_copy = dict(meta)
        meta_copy.pop("clean_text", None)
        work_items.append((text, meta_copy))
        indexed_items.append((source_file, block_id, meta, meta_copy))

    if not work_items:
        return {}

    _apply_llm_metadata_extraction_batch(work_items, llm_config)

    patches: Dict[str, Dict[str, object]] = {}
    for source_file, block_id, original_meta, enhanced_meta in indexed_items:
        patch: Dict[str, object] = {}
        for field in _PASSTHROUGH_LLM_META_FIELDS:
            new_value = enhanced_meta.get(field)
            if new_value in (None, "", [], {}):
                continue
            if new_value != original_meta.get(field):
                patch[field] = new_value
        if patch:
            patches[f"{source_file}::{block_id}"] = patch

    logger.info(
        "[farm-owner] metadata LLM enhanced %d/%d quality-passed chunks",
        len(patches),
        len(indexed_items),
    )
    return patches


def _build_quality_passed_classification(
    owner: KnowledgeFarmOwnerAgent,
    chunks: List[Dict[str, object]],
) -> Dict[str, FillRequest]:
    if not chunks:
        return {}

    report = FarmOwnerReport()
    requests = owner.classify_uncovered_chunks(chunks, report)
    block_id_to_source = _build_block_id_to_source(chunks)
    by_key: Dict[str, FillRequest] = {}
    for request in requests:
        block_id = str(request.target_block_id or "")
        if not block_id:
            continue
        source_file = ""
        if isinstance(request.chunk_meta_patch, dict):
            source_file = str(request.chunk_meta_patch.get("source_file") or "")
        if not source_file:
            source_file = str(block_id_to_source.get(block_id) or "")
        if not source_file:
            continue
        by_key[f"{source_file}::{block_id}"] = request
    return by_key


def _build_passthrough_decisions(
    gate: Dict[str, Dict[str, object]],
    passed_chunks: List[Dict[str, object]],
    owner: KnowledgeFarmOwnerAgent,
) -> tuple[List[Dict[str, object]], Dict[str, int]]:
    rows: List[Dict[str, object]] = []
    llm_patches = _build_quality_passed_llm_patches(passed_chunks)
    classification = _build_quality_passed_classification(owner, passed_chunks)

    llm_enhanced = 0
    classified = 0
    discarded = 0
    for chunk in passed_chunks:
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        source_file = str(meta.get("source_file") or "")
        block_id = str(meta.get("block_id") or "")
        if not source_file or not block_id:
            continue
        key = f"{source_file}::{block_id}"
        gate_item = gate.get(key, {})
        if gate_item and not bool(gate_item.get("is_product_knowledge", False)):
            continue

        chunk_meta_patch: Dict[str, object] = {
            "source_file": source_file,
            "document_category": str(meta.get("document_category") or ""),
        }
        llm_patch = llm_patches.get(key) or {}
        if llm_patch:
            chunk_meta_patch.update(llm_patch)
            llm_enhanced += 1

        action = "merge_into_existing"
        class_req = classification.get(key)
        if class_req is not None:
            action = str(class_req.action or action)
            if isinstance(class_req.chunk_meta_patch, dict):
                chunk_meta_patch.update(class_req.chunk_meta_patch)
            classified += 1
            if action == "discard":
                discarded += 1

        rows.append(
            {
                "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
                "source_file": source_file,
                "entity_title": str(meta.get("section_title") or f"{source_file}#{block_id}"),
                "action": action,
                "target_node_id": str(meta.get("tree_node_id") or ""),
                "target_block_id": block_id,
                "tree_level": str(chunk_meta_patch.get("tree_level") or ""),
                "fill_fields": {},
                "enrich_fields": {},
                "chunk_meta_patch": chunk_meta_patch,
                "source_evidence": "quality_gate_pass_with_farm_owner_enhancement",
                "resolved_value": {},
            }
        )
    return rows, {
        "llm_enhanced": llm_enhanced,
        "classified": classified,
        "discarded": discarded,
    }


def _infer_source_file_for_request(
    req,
    gate: Dict[str, Dict[str, object]],
    block_id_to_source: Dict[str, Optional[str]],
) -> str:
    source_file = ""
    if isinstance(req.chunk_meta_patch, dict):
        source_file = str(req.chunk_meta_patch.get("source_file") or "")
    if source_file:
        return source_file

    block_id = str(req.target_block_id or "")
    if block_id and block_id in block_id_to_source and block_id_to_source[block_id]:
        return str(block_id_to_source[block_id])

    if req.target_node_id:
        for item in gate.values():
            if str(item.get("tree_node_id") or "") == str(req.target_node_id):
                return str(item.get("source_file") or "")
    return ""


def _write_owner_decisions(
    reference_dir: Path,
    passthrough_rows: List[Dict[str, object]],
    report,
    gate: Dict[str, Dict[str, object]],
) -> Path:
    out_file = reference_dir / _OWNER_DECISIONS_FILE
    lines: List[str] = []

    lines.extend(json.dumps(row, ensure_ascii=False, default=str) for row in passthrough_rows)

    block_id_to_source: Dict[str, Optional[str]] = {}
    for item in gate.values():
        block_id = str(item.get("block_id") or "")
        source_file = str(item.get("source_file") or "")
        if not block_id or not source_file:
            continue
        if block_id not in block_id_to_source:
            block_id_to_source[block_id] = source_file
        elif block_id_to_source[block_id] != source_file:
            block_id_to_source[block_id] = None

    for req in list(report.fill_requests) + list(report.deferred):
        source_file = _infer_source_file_for_request(req, gate, block_id_to_source)
        payload = {
            "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
            "source_file": source_file,
            "entity_title": req.entity_title,
            "action": req.action,
            "target_node_id": req.target_node_id,
            "target_block_id": req.target_block_id,
            "tree_level": req.tree_level,
            "fill_fields": req.fill_fields,
            "enrich_fields": req.enrich_fields,
            "chunk_meta_patch": req.chunk_meta_patch,
            "source_evidence": req.source_evidence,
            "resolved_value": req.resolved_value,
        }
        lines.append(json.dumps(payload, ensure_ascii=False, default=str))
    out_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_file


def run_farm_owner_pipeline(reference_dir: Path | None = None) -> Dict[str, object]:
    load_inagent_env()
    reference_dir = reference_dir or _REFERENCE_DIR
    logger.info("[farm-owner] start: reference_dir=%s", reference_dir)
    gate_file = reference_dir / _QUALITY_GATE_FILE
    ref_file = reference_dir / _QUALITY_REFERENCE_FOR_OWNER_FILE
    gaps_file = reference_dir / "schema_gaps.jsonl"
    if not gate_file.exists() or gate_file.stat().st_size == 0:
        result = {
            "processed": False,
            "reason": "missing quality gate file",
            "required_file": str(gate_file),
        }
        logger.info("[farm-owner] blocked: %s", json.dumps(result, ensure_ascii=False))
        return result
    if not ref_file.exists() or ref_file.stat().st_size == 0:
        result = {
            "processed": False,
            "reason": "missing quality filtered reference",
            "required_file": str(ref_file),
        }
        logger.info("[farm-owner] blocked: %s", json.dumps(result, ensure_ascii=False))
        return result
    graphrag = GraphRAGRetriever(workspace_dir=_GRAPH_DIR)
    if not graphrag.is_available():
        return {"processed": False, "reason": "GraphRAG unavailable"}

    gate = _load_quality_gate(gate_file)
    validated_chunks = _filter_chunks_by_quality_gate(
        _load_quality_filtered_reference(reference_dir),
        gate,
    )

    owner_model = None
    try:
        owner_model = get_llm_model()
    except Exception as exc:
        logger.warning("[farm-owner] 获取 LLM model 失败，回退为非 LLM 模式: %s", exc)

    owner = KnowledgeFarmOwnerAgent(graphrag, model=owner_model)
    passthrough_rows, passthrough_stats = _build_passthrough_decisions(
        gate,
        validated_chunks,
        owner,
    )

    entries: List[SchemaGapEntry] = []
    if gaps_file.exists() and gaps_file.stat().st_size > 0:
        entries = _load_gap_entries(gaps_file)
    entries = _filter_entries_by_quality_gate(entries, gate)

    if not entries:
        report = type("_DummyReport", (), {"fill_requests": [], "deferred": []})()
        owner_decisions_file = _write_owner_decisions(reference_dir, passthrough_rows, report, gate)
        result = {
            "processed": bool(passthrough_rows),
            "reason": "no valid gap entries after quality gate",
            "decision_schema_version": _OWNER_DECISION_SCHEMA_VERSION,
            "owner_decisions_file": str(owner_decisions_file),
            "quality_passed_blocks": len(passthrough_rows),
            "llm_enhanced_blocks": passthrough_stats.get("llm_enhanced", 0),
            "classified_blocks": passthrough_stats.get("classified", 0),
            "discarded_blocks": passthrough_stats.get("discarded", 0),
        }
        logger.info("[farm-owner] done: %s", json.dumps(result, ensure_ascii=False))
        return result

    report = owner.process_gap_entries(entries)
    owner_decisions_file = _write_owner_decisions(reference_dir, passthrough_rows, report, gate)
    processed_file = None
    if gaps_file.exists() and gaps_file.stat().st_size > 0:
        processed_file = gaps_file.with_suffix(".jsonl.processed")
        try:
            os.replace(gaps_file, processed_file)
        except OSError as exc:
            logger.warning("[farm-owner] 标记 processed 失败: %s", exc)

    result = {
        "processed": True,
        "decision_schema_version": _OWNER_DECISION_SCHEMA_VERSION,
        "gap_entries": len(entries),
        "quality_passed_blocks": len(passthrough_rows),
        "llm_enhanced_blocks": passthrough_stats.get("llm_enhanced", 0),
        "classified_blocks": passthrough_stats.get("classified", 0),
        "discarded_blocks": passthrough_stats.get("discarded", 0),
        "entities_added": report.entities_added,
        "discarded_count": report.discarded_count,
        "fill_requests": len(report.fill_requests),
        "deferred": len(report.deferred),
        "errors": list(report.errors),
        "snapshot_dir": str(report.snapshot_dir) if report.snapshot_dir else None,
        "processed_file": str(processed_file) if processed_file else None,
        "quality_gate_file": str(gate_file),
        "owner_decisions_file": str(owner_decisions_file),
    }
    logger.info("[farm-owner] done: %s", json.dumps(result, ensure_ascii=False))
    return result


async def main() -> None:
    _setup_logging()
    run_farm_owner_pipeline()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(main())
