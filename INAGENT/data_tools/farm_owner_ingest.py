# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农场主独立流程：质量门控后的通过项增强与销售员交接。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
import sys
from datetime import datetime, timezone
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Dict, List, Optional

from INAGENT.data_tools.quality_feedback_loop import (
    FEEDBACK_INBOX_SCHEMA_VERSION,
    QUALITY_RULE_PROPOSALS_FILE,
    build_rule_proposals_from_inbox,
    feedback_gate_key,
)
from INAGENT.rag.knowledge_schema import FillRequest, SchemaGapEntry
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("farm_owner_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
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


def _build_passthrough_decisions(
    gate: Dict[str, Dict[str, object]],
    passed_chunks: List[Dict[str, object]],
) -> tuple[List[Dict[str, object]], Dict[str, int]]:
    rows: List[Dict[str, object]] = []
    llm_patches = _build_quality_passed_llm_patches(passed_chunks)

    llm_enhanced = 0
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
        "classified": 0,
        "discarded": 0,
    }


def _infer_source_file_for_request(
    req: FillRequest,
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
    deferred_rows: List[Dict[str, object]],
    gate: Dict[str, Dict[str, object]],
) -> Path:
    out_file = reference_dir / _OWNER_DECISIONS_FILE
    lines: List[str] = []

    lines.extend(json.dumps(row, ensure_ascii=False, default=str) for row in passthrough_rows)
    lines.extend(json.dumps(row, ensure_ascii=False, default=str) for row in deferred_rows)

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

    out_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_file


def _build_quality_deferred_decisions(
    entries: List[SchemaGapEntry],
    gate: Dict[str, Dict[str, object]],
    chunks: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    if not entries:
        return []

    block_id_to_source = _build_block_id_to_source(chunks)
    rows: List[Dict[str, object]] = []
    for entry in entries:
        req = FillRequest(
            entity_title=str(entry.entity_title or ""),
            action="needs_tree_session",
            target_block_id=str(entry.chunk_block_id or ""),
            source_evidence="quality_rule_candidate_for_inspector",
            resolved_value={
                "delegate_to": "quality_inspector",
                "gap_type": str(entry.gap_type or ""),
                "field_name": str(entry.field_name or ""),
            },
            chunk_meta_patch={"source_file": str(entry.source_file or "")},
        )
        source_file = _infer_source_file_for_request(req, gate, block_id_to_source)
        rows.append(
            {
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
        )
    return rows


def _build_quality_feedback_records(entries: List[SchemaGapEntry]) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for idx, entry in enumerate(entries):
        source_file = str(entry.source_file or "").strip()
        block_id = str(entry.chunk_block_id or "").strip()
        if not source_file or not block_id:
            continue
        gap_type = str(entry.gap_type or "unknown").strip() or "unknown"
        suggested_rule: Dict[str, object] = {
            "rule_source": "farm_owner_ingest",
            "reason_code": f"farm_owner_gap_{gap_type}",
            "match": {
                "source_file_glob": source_file,
                "block_id": block_id,
            },
            "action": {
                "verdict": "review",
            },
        }
        if entry.field_name:
            suggested_rule["match"]["field_name"] = str(entry.field_name)
        if entry.entity_title:
            suggested_rule["match"]["entity_title"] = str(entry.entity_title)
        records.append(
            {
                "schema_version": FEEDBACK_INBOX_SCHEMA_VERSION,
                "feedback_id": f"farm-owner-gap-{idx}",
                "source_file": source_file,
                "block_id": block_id,
                "gate_key": feedback_gate_key(source_file, block_id),
                "verdict": "review",
                "reason_code": f"farm_owner_gap_{gap_type}",
                "source_run_id": "farm_owner_ingest",
                "notes": f"generated_at={now}",
                "suggested_rule": suggested_rule,
            }
        )
    return records


def _write_quality_rule_proposals(
    reference_dir: Path,
    entries: List[SchemaGapEntry],
) -> Optional[Path]:
    records = _build_quality_feedback_records(entries)
    if not records:
        return None
    proposals_path = reference_dir / QUALITY_RULE_PROPOSALS_FILE
    doc = build_rule_proposals_from_inbox(
        records,
        inbox_path="farm_owner_ingest_generated",
    )
    doc["from_gap_entries"] = [asdict(entry) for entry in entries]
    proposals_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return proposals_path


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
    gate = _load_quality_gate(gate_file)
    validated_chunks = _filter_chunks_by_quality_gate(
        _load_quality_filtered_reference(reference_dir),
        gate,
    )
    passthrough_rows, passthrough_stats = _build_passthrough_decisions(
        gate,
        validated_chunks,
    )

    entries: List[SchemaGapEntry] = []
    if gaps_file.exists() and gaps_file.stat().st_size > 0:
        entries = _load_gap_entries(gaps_file)
    entries = _filter_entries_by_quality_gate(entries, gate)
    deferred_rows = _build_quality_deferred_decisions(entries, gate, validated_chunks)
    quality_rule_proposals_file = _write_quality_rule_proposals(reference_dir, entries)

    if not entries:
        owner_decisions_file = _write_owner_decisions(
            reference_dir,
            passthrough_rows,
            deferred_rows,
            gate,
        )
        result = {
            "processed": bool(passthrough_rows),
            "reason": "no valid gap entries after quality gate",
            "decision_schema_version": _OWNER_DECISION_SCHEMA_VERSION,
            "owner_decisions_file": str(owner_decisions_file),
            "quality_passed_blocks": len(passthrough_rows),
            "llm_enhanced_blocks": passthrough_stats.get("llm_enhanced", 0),
            "classified_blocks": passthrough_stats.get("classified", 0),
            "discarded_blocks": passthrough_stats.get("discarded", 0),
            "quality_rule_proposals_file": (
                str(quality_rule_proposals_file) if quality_rule_proposals_file else None
            ),
        }
        logger.info("[farm-owner] done: %s", json.dumps(result, ensure_ascii=False))
        return result

    owner_decisions_file = _write_owner_decisions(
        reference_dir,
        passthrough_rows,
        deferred_rows,
        gate,
    )

    result = {
        "processed": bool(passthrough_rows or deferred_rows),
        "decision_schema_version": _OWNER_DECISION_SCHEMA_VERSION,
        "gap_entries": len(entries),
        "delegated_gap_entries": len(deferred_rows),
        "quality_passed_blocks": len(passthrough_rows),
        "llm_enhanced_blocks": passthrough_stats.get("llm_enhanced", 0),
        "classified_blocks": passthrough_stats.get("classified", 0),
        "discarded_blocks": passthrough_stats.get("discarded", 0),
        "entities_added": 0,
        "discarded_count": 0,
        "fill_requests": 0,
        "deferred": len(deferred_rows),
        "errors": [],
        "snapshot_dir": None,
        "processed_file": None,
        "quality_gate_file": str(gate_file),
        "owner_decisions_file": str(owner_decisions_file),
        "quality_rule_proposals_file": (
            str(quality_rule_proposals_file) if quality_rule_proposals_file else None
        ),
        "delegated_to": "quality_inspector",
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
