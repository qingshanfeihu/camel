# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农场主独立流程：质量门控后完整分类（规则+LLM）与农民/质检员交接。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import sys
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field as dc_field
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

# ── Classification constants ─────────────────────────────────────────
_CLASSIFY_BATCH_SIZE = 20       # chunks per LLM classification call
_CLASSIFY_TEXT_LIMIT = 800      # chars per chunk snippet in classification prompt
_CLASSIFY_TOKEN_BUDGET = 8000   # token budget per sub-batch
_CLASSIFY_CHARS_PER_TOKEN = 3.5
_CLASSIFY_MAX_WORKERS = 8

_VALID_CLASSIFICATION_ACTIONS = frozenset({
    "discard",
    "merge_into_existing",
    "tree_create_leaf",
    "tree_create_branch",
    "tree_create_trunk",
    "tree_create_root",
    "needs_tree_session",
})

_ACTION_RANK: Dict[str, int] = {
    "tree_create_leaf": 1,
    "tree_create_branch": 2,
    "tree_create_trunk": 3,
    "tree_create_root": 4,
}

_TREE_CREATE_TO_LEVEL: Dict[str, str] = {
    "tree_create_leaf": "leaf",
    "tree_create_branch": "branch",
    "tree_create_trunk": "trunk",
    "tree_create_root": "root",
}

_CMD_LIKE_KEYWORDS = re.compile(
    r"(?i)\b(command|cmd|cli|show\s|no\s|display\s|设置|配置命令|"
    r"命令参数|命令格式|command\s+reference)\b"
)

_SECTION_NUMBER_RE = re.compile(r"^\s*[\d.]+\s*")
_MIN_USEFUL_CONTENT_LEN = 10  # chunks with < 10 useful chars (after stripping title/number) are thin headers
_GENERIC_NON_ACTIONABLE_TITLES = frozenset({
    "配置示例",
    "配置目标",
    "概述",
    "结果验证",
    "Function List",
    "Source List",
})
_OVERVIEW_KW = ("功能原理", "工作机制", "概述", "简介", "总体介绍", "体系结构", "架构")
_EXPLICIT_COMMAND_LINE_RE = re.compile(
    r"(?im)^\s*(show|no|clear|display|enable|config|write|hostname|webui|"
    r"ping|traceroute|ip|interface|nat|slb|gslb|ipv6)\b"
)
_FUNCTION_SIGNATURE_RE = re.compile(
    r"\b[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\([^\n;]*\)\s*;"
)
_GENERIC_PROCEDURAL_TITLES = frozenset({
    "操作步骤",
    "配置步骤",
    "步骤",
    "Operation Steps",
    "Configuration Steps",
    "Steps",
})
_NARRATIVE_BRANCH_KW = (
    "功能原理",
    "基本原理",
    "处理逻辑",
    "工作机制",
    "架构视图",
    "系统架构",
    "总体介绍",
    "简介",
)
_CLI_PROMPT_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]\s*|<[^>]+>\s*|[A-Za-z0-9_.-]+(?:\([^)]*\))?[#>]\s*)+"
)
_CLI_COMMAND_START_RE = re.compile(
    r"(?i)^(show|no|clear|display|enable|config|write|hostname|webui|"
    r"ping|traceroute|ip|interface|nat|slb|gslb|ipv6|acl|rule|firewall|"
    r"packet-filter|bond|quit|add)\b"
)


def _useful_content_length(text: str, section_title: str) -> int:
    """Return the length of actual content after stripping first-line title and section numbers."""
    lines = text.strip().split("\n")
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    body = _SECTION_NUMBER_RE.sub("", body).strip()
    if section_title:
        body = body.replace(section_title, "").strip()
    return len(body)


@dataclass
class _ChunkTreeContext:
    """Lightweight tree context for a chunk (no GraphRAG dependency)."""

    chunk_key: str  # "source_file::block_id"
    exists_in_tree: bool = False
    tree_level: str = ""  # root|trunk|branch|leaf|""
    matched_node_id: str = ""
    parent_candidate_id: str = ""
    similar_commands: List[str] = dc_field(default_factory=list)
    skeleton_module_id: str = ""
    skeleton_artifact_exists: bool = False
    hierarchy_prefix: str = ""


@dataclass
class _ExistingTreeIndex:
    trunks: Set[str] = dc_field(default_factory=set)
    branches: Set[str] = dc_field(default_factory=set)
    commands: Set[str] = dc_field(default_factory=set)


@dataclass
class _CreatedTreeIndex:
    trunks: Set[str] = dc_field(default_factory=set)
    branches: Set[str] = dc_field(default_factory=set)


def _normalize_target_node_id(raw: str) -> str:
    cleaned = str(raw or "").strip().strip("`\"'")
    if not cleaned:
        return ""
    cleaned = cleaned.replace("＞", ">").replace("›", ">")
    parts = [
        re.sub(r"\s+", " ", part).strip()
        for part in cleaned.split(">")
        if part.strip()
    ]
    return " > ".join(parts)


def _normalize_tree_fields(
    action: str,
    tree_level: str,
    target_node_id: str,
) -> Tuple[str, str, str]:
    target = _normalize_target_node_id(target_node_id)
    if action in _TREE_CREATE_TO_LEVEL:
        return action, _TREE_CREATE_TO_LEVEL[action], target
    if action in ("discard", "needs_tree_session"):
        return action, "", ""
    if action == "merge_into_existing":
        if tree_level not in ("leaf", "branch", "trunk", "root"):
            tree_level = ""
        return action, tree_level, target
    return "needs_tree_session", "", ""


def _normalize_branch_parent_target(target_node_id: str, entity_title: str) -> str:
    target = _normalize_target_node_id(target_node_id)
    entity = _normalize_target_node_id(entity_title)
    if not target or not entity:
        return target
    parts = [part.strip() for part in target.split(" > ") if part.strip()]
    if parts and parts[-1] == entity:
        return " > ".join(parts[:-1])
    return target


def _branch_parent_from_function_hierarchy(function_hierarchy: str) -> str:
    normalized = _normalize_target_node_id(function_hierarchy)
    if " > " not in normalized:
        return ""
    return " > ".join(normalized.split(" > ")[:-1])


def _infer_tree_level_from_meta_signals(meta: Optional[Dict[str, object]]) -> str:
    if not meta:
        return "unknown"

    llm_level = str(meta.get("tree_level") or "")
    if llm_level in ("leaf", "new_leaf", "branch", "trunk", "root"):
        return llm_level

    fh = str(meta.get("function_hierarchy") or "").strip()
    if fh:
        parts = [p.strip() for p in fh.replace(">", "/").split("/") if p.strip()]
        if len(parts) == 1:
            return "trunk"
        if len(parts) > 1:
            sp = str(meta.get("section_path") or "").strip()
            sp_parts = [p.strip() for p in sp.split(">") if p.strip()] if sp else []
            if len(sp_parts) <= 1 and any(kw in sp for kw in _OVERVIEW_KW):
                return "trunk"
            return "branch"

    return "unknown"


def _looks_like_internal_function_signature(text: str, title: str) -> bool:
    title_norm = str(title or "").strip().lower()
    if title_norm in {"function list", "source list"}:
        return True
    preview = "\n".join(str(text or "").splitlines()[:4])
    return bool(_FUNCTION_SIGNATURE_RE.search(preview))


def _looks_like_non_actionable_heading(text: str, title: str) -> bool:
    title_norm = str(title or "").strip()
    useful_len = _useful_content_length(str(text or ""), title_norm)
    if title_norm in _GENERIC_NON_ACTIONABLE_TITLES and not _EXPLICIT_COMMAND_LINE_RE.search(str(text or "")):
        return True
    if useful_len < 30 and any(token in str(text or "") for token in ("执行如下命令", "如下图", "拓扑图")):
        return True
    return False


def _append_source_evidence(row: Dict[str, object], suffix: str) -> None:
    evidence = str(row.get("source_evidence") or "")
    row["source_evidence"] = f"{evidence}+{suffix}" if evidence else suffix


def _rewrite_row_as_discard(row: Dict[str, object], reason: str) -> None:
    row["action"] = "discard"
    row["tree_level"] = ""
    row["target_node_id"] = ""
    chunk_meta_patch = row.get("chunk_meta_patch") or {}
    if isinstance(chunk_meta_patch, dict):
        chunk_meta_patch["owner_excluded"] = True
    _append_source_evidence(row, reason)


def _rewrite_row_as_needs_tree_session(row: Dict[str, object], reason: str) -> None:
    row["action"] = "needs_tree_session"
    row["tree_level"] = ""
    row["target_node_id"] = ""
    _append_source_evidence(row, reason)


def _compose_branch_path(parent: str, entity_title: str) -> str:
    parent_norm = _normalize_target_node_id(parent)
    entity_norm = _normalize_target_node_id(entity_title)
    if not parent_norm or not entity_norm:
        return ""
    return _normalize_target_node_id(f"{parent_norm} > {entity_norm}")


def _select_llm_target_id(
    action: str,
    raw_target_id: str,
    ctx: _ChunkTreeContext,
    patch: Dict[str, object],
) -> str:
    target = _normalize_target_node_id(raw_target_id)
    if target:
        return target

    if not action.startswith("tree_create"):
        return ""

    if action == "tree_create_leaf":
        for candidate in (
            ctx.parent_candidate_id,
            ctx.hierarchy_prefix,
            patch.get("function_hierarchy") if isinstance(patch, dict) else "",
        ):
            normalized = _normalize_target_node_id(str(candidate or ""))
            if normalized:
                return normalized
        return ""

    if action == "tree_create_branch":
        parent_hint = _branch_parent_from_function_hierarchy(
            str(patch.get("function_hierarchy") or "") if isinstance(patch, dict) else "",
        )
        if parent_hint:
            return parent_hint
        pm = _normalize_target_node_id(
            str(patch.get("product_module") or "") if isinstance(patch, dict) else "",
        )
        if pm and pm != "unknown":
            return pm

    return ""


def _get_cli_graph_store_safe() -> object:
    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        return get_cli_graph_store()
    except Exception:
        return None


def _build_existing_tree_index(cli_graph: object = None) -> _ExistingTreeIndex:
    index = _ExistingTreeIndex()
    cli_graph = cli_graph or _get_cli_graph_store_safe()
    if cli_graph is None:
        return index

    try:
        cli_graph._ensure_loaded()  # type: ignore[attr-defined]
    except Exception:
        return index

    module_nodes = getattr(cli_graph, "_module_nodes", {}) or {}
    nodes_by_id = getattr(cli_graph, "_nodes_by_id", {}) or {}
    cmd_nid_to_label = getattr(cli_graph, "_cmd_nid_to_label", {}) or {}

    for module_id, node in module_nodes.items():
        label = _normalize_target_node_id(str(node.get("label") or module_id))
        if label:
            index.trunks.add(label)

    for module_id, node in module_nodes.items():
        module_label = _normalize_target_node_id(str(node.get("label") or module_id))
        if not module_label:
            continue
        try:
            branch_ids = cli_graph.get_branch_ids(module_id)  # type: ignore[attr-defined]
        except Exception:
            branch_ids = []
        for branch_id in branch_ids:
            branch_node = nodes_by_id.get(branch_id, {})
            branch_label = _normalize_target_node_id(str(branch_node.get("label") or branch_id))
            if branch_label:
                index.branches.add(f"{module_label} > {branch_label}")

    for nid in cmd_nid_to_label:
        label = _normalize_target_node_id(str(nodes_by_id.get(nid, {}).get("label") or nid))
        if label:
            index.commands.add(label)

    return index


def _target_kind(
    target_node_id: str,
    existing: _ExistingTreeIndex,
    created: Optional[_CreatedTreeIndex] = None,
) -> str:
    target = _normalize_target_node_id(target_node_id)
    created = created or _CreatedTreeIndex()
    if not target:
        return "unknown"
    if target in existing.commands:
        return "command"
    if target in existing.branches or target in created.branches:
        return "branch"
    if target in existing.trunks or target in created.trunks:
        return "trunk"
    return "unknown"


def _build_created_tree_index(
    rows: List[Dict[str, object]],
    existing: _ExistingTreeIndex,
) -> _CreatedTreeIndex:
    created = _CreatedTreeIndex()
    for row in rows:
        if str(row.get("action") or "") == "tree_create_trunk":
            entity = _normalize_target_node_id(str(row.get("entity_title") or ""))
            if entity:
                created.trunks.add(entity)

    changed = True
    while changed:
        changed = False
        for row in rows:
            if str(row.get("action") or "") != "tree_create_branch":
                continue
            parent = _normalize_branch_parent_target(
                str(row.get("target_node_id") or ""),
                str(row.get("entity_title") or ""),
            )
            entity = _normalize_target_node_id(str(row.get("entity_title") or ""))
            if not parent or not entity:
                continue
            if _target_kind(parent, existing, created) not in {"trunk", "branch"}:
                continue
            path = _compose_branch_path(parent, entity)
            if path and path not in created.branches:
                created.branches.add(path)
                changed = True
    return created


def _best_branch_parent(
    row: Dict[str, object],
    ctx: _ChunkTreeContext,
    patch: Dict[str, object],
    existing: _ExistingTreeIndex,
    created: _CreatedTreeIndex,
    cli_graph: object = None,
) -> str:
    cli_graph = cli_graph or _get_cli_graph_store_safe()
    target = _normalize_branch_parent_target(
        str(row.get("target_node_id") or ""),
        str(row.get("entity_title") or ""),
    )
    candidates = [
        target,
        _branch_parent_from_function_hierarchy(str(patch.get("function_hierarchy") or "")),
        _normalize_target_node_id(str(ctx.hierarchy_prefix or "")),
        _normalize_target_node_id(str(patch.get("product_module") or "")),
    ]
    cmd_prefix = str(patch.get("command_prefix") or "")
    if cli_graph is not None and cmd_prefix:
        try:
            hp = _normalize_target_node_id(cli_graph.get_hierarchy_prefix(cmd_prefix))  # type: ignore[attr-defined]
        except Exception:
            hp = ""
        if hp:
            candidates.insert(1, hp)

    for candidate in candidates:
        if _target_kind(candidate, existing, created) in {"trunk", "branch"}:
            return candidate
    return ""


def _best_leaf_parent(
    row: Dict[str, object],
    ctx: _ChunkTreeContext,
    patch: Dict[str, object],
    existing: _ExistingTreeIndex,
    created: _CreatedTreeIndex,
    cli_graph: object = None,
) -> str:
    cli_graph = cli_graph or _get_cli_graph_store_safe()
    candidates = [
        _normalize_target_node_id(str(row.get("target_node_id") or "")),
        _normalize_target_node_id(str(ctx.parent_candidate_id or "")),
        _normalize_target_node_id(str(ctx.hierarchy_prefix or "")),
        _normalize_target_node_id(str(patch.get("function_hierarchy") or "")),
    ]
    cmd_prefix = str(patch.get("command_prefix") or "")
    if cli_graph is not None and cmd_prefix:
        try:
            hp = _normalize_target_node_id(cli_graph.get_hierarchy_prefix(cmd_prefix))  # type: ignore[attr-defined]
        except Exception:
            hp = ""
        if hp:
            candidates.insert(1, hp)

    for candidate in candidates:
        if _target_kind(candidate, existing, created) in {"command", "branch"}:
            return candidate
    return ""


def _validate_hierarchy_decisions(
    rules_decided: List[Dict[str, object]],
    llm_decided: List[Dict[str, object]],
    tree_contexts: Dict[str, _ChunkTreeContext],
    llm_patches: Dict[str, Dict[str, object]],
    candidate_chunks: List[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    chunk_by_key = {
        _chunk_key(chunk): chunk
        for chunk in candidate_chunks
        if _chunk_key(chunk)
    }
    all_rows = list(rules_decided) + list(llm_decided)
    cli_graph = _get_cli_graph_store_safe()
    existing = _build_existing_tree_index(cli_graph)

    for row in all_rows:
        action, tree_level, target = _normalize_tree_fields(
            str(row.get("action") or ""),
            str(row.get("tree_level") or ""),
            str(row.get("target_node_id") or ""),
        )
        row["action"] = action
        row["tree_level"] = tree_level
        row["target_node_id"] = target
        if action == "tree_create_branch":
            row["target_node_id"] = _normalize_branch_parent_target(
                str(row.get("target_node_id") or ""),
                str(row.get("entity_title") or ""),
            )

    created = _build_created_tree_index(all_rows, existing)
    normalized = 0
    rerouted = 0

    for row in all_rows:
        action = str(row.get("action") or "")
        if action not in _VALID_CLASSIFICATION_ACTIONS:
            _rewrite_row_as_needs_tree_session(row, "hierarchy_invalid_action")
            rerouted += 1
            continue
        if action in {"discard", "needs_tree_session", "merge_into_existing"}:
            action, tree_level, target = _normalize_tree_fields(
                action,
                str(row.get("tree_level") or ""),
                str(row.get("target_node_id") or ""),
            )
            row["action"] = action
            row["tree_level"] = tree_level
            row["target_node_id"] = target
            continue

        key = f"{str(row.get('source_file') or '')}::{str(row.get('target_block_id') or '')}"
        chunk = chunk_by_key.get(key, {})
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        patch = llm_patches.get(key) or {}
        chunk_meta = dict(meta)
        if isinstance(patch, dict):
            chunk_meta.update(patch)
        ctx = tree_contexts.get(key, _ChunkTreeContext(chunk_key=key))
        title = str(meta.get("section_title") or row.get("entity_title") or "")
        text = str(chunk.get("page_content") or "")

        if _looks_like_internal_function_signature(text, title):
            _rewrite_row_as_discard(row, "hierarchy_internal_function_signature")
            rerouted += 1
            continue

        if action == "tree_create_branch":
            best_parent = _best_branch_parent(row, ctx, patch, existing, created, cli_graph)
            if best_parent:
                if best_parent != str(row.get("target_node_id") or ""):
                    normalized += 1
                row["target_node_id"] = best_parent
                row["tree_level"] = "branch"
                created = _build_created_tree_index(all_rows, existing)
                continue
            _rewrite_row_as_needs_tree_session(row, "hierarchy_invalid_branch_parent")
            rerouted += 1
            continue

        if action != "tree_create_leaf":
            continue

        best_leaf_parent = _best_leaf_parent(row, ctx, patch, existing, created, cli_graph)
        if best_leaf_parent:
            if best_leaf_parent != str(row.get("target_node_id") or ""):
                normalized += 1
            row["target_node_id"] = best_leaf_parent
            row["tree_level"] = "leaf"
            continue

        if _looks_like_non_actionable_heading(text, title):
            _rewrite_row_as_discard(row, "hierarchy_non_actionable_heading")
            rerouted += 1
            continue

        inferred_level = _infer_tree_level_from_meta_signals(chunk_meta)
        if inferred_level in {"branch", "trunk"}:
            best_parent = _best_branch_parent(row, ctx, patch, existing, created, cli_graph)
            if best_parent:
                row["action"] = "tree_create_branch"
                row["tree_level"] = "branch"
                row["target_node_id"] = best_parent
                _append_source_evidence(row, "normalized_leaf_to_branch")
                normalized += 1
                created = _build_created_tree_index(all_rows, existing)
                continue

        _rewrite_row_as_needs_tree_session(row, "hierarchy_invalid_leaf_parent")
        rerouted += 1

    logger.info(
        "[farm-owner] hierarchy validation: normalized %d rows, rerouted %d rows",
        normalized,
        rerouted,
    )
    return rules_decided, llm_decided


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


# ── Stage 1: Rules-based pre-filter ──────────────────────────────────


def _chunk_key(chunk: Dict[str, object]) -> str:
    meta = chunk.get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    sf = str(meta.get("source_file") or "")
    bid = str(meta.get("block_id") or "")
    return f"{sf}::{bid}" if sf and bid else ""


def _stage1_rules_prefilter(
    chunks: List[Dict[str, object]],
) -> Tuple[
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
]:
    """Separate chunks into: candidates, garbage, non_knowledge, wrong_product."""
    from INAGENT.utils.chunk_text_quality import is_garbage_page_content
    from INAGENT.utils.non_product_section_patterns import (
        NON_KNOWLEDGE_CONTENT_KEYWORDS,
        non_product_section_title_regex,
    )

    title_re = non_product_section_title_regex()
    body_kws = NON_KNOWLEDGE_CONTENT_KEYWORDS

    candidates: List[Dict[str, object]] = []
    garbage: List[Dict[str, object]] = []
    non_knowledge: List[Dict[str, object]] = []

    for chunk in chunks:
        text = str(chunk.get("page_content") or chunk.get("text") or "").strip()
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        section_title = str(meta.get("section_title") or "").strip()

        if is_garbage_page_content(text):
            garbage.append(chunk)
            continue

        if section_title and title_re.search(section_title):
            non_knowledge.append(chunk)
            continue

        body_prefix = text[:500].lower()
        is_non_kw = any(kw.lower() in body_prefix for kw in body_kws)
        if is_non_kw:
            non_knowledge.append(chunk)
            continue

        # ── Thin content filter: pure section headers with no real body ──
        if _useful_content_length(text, section_title) < _MIN_USEFUL_CONTENT_LEN:
            garbage.append(chunk)
            continue

        candidates.append(chunk)

    # ── Product filter: reject competitor product knowledge ──
    wrong_product: List[Dict[str, object]] = []
    try:
        from INAGENT.agents._farm_owner_product_filter import analyze_product_match
        feedback = analyze_product_match(candidates)
        if feedback:
            rejected_keys = {
                f"{r['source_file']}::{r['block_id']}" for r in feedback
            }
            filtered_candidates = []
            for chunk in candidates:
                key = _chunk_key(chunk)
                if key in rejected_keys:
                    wrong_product.append(chunk)
                else:
                    filtered_candidates.append(chunk)
            candidates = filtered_candidates
            logger.info(
                "[farm-owner] product filter: rejected %d competitor chunks",
                len(wrong_product),
            )
    except Exception as exc:
        logger.warning("[farm-owner] product filter unavailable: %s", exc)

    logger.info(
        "[farm-owner] stage1: %d candidates, %d garbage, %d non_knowledge, %d wrong_product",
        len(candidates), len(garbage), len(non_knowledge), len(wrong_product),
    )
    return candidates, garbage, non_knowledge, wrong_product


# ── Stage 2: Tree context lookup (CLIGraphStore + SkeletonIndex) ─────


def _stage2_tree_context_lookup(
    chunks: List[Dict[str, object]],
) -> Dict[str, _ChunkTreeContext]:
    """Look up tree context for each candidate chunk using read-only stores."""
    cli_graph = None
    skeleton = None

    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        cli_graph = get_cli_graph_store()
    except Exception as exc:
        logger.warning("[farm-owner] CLIGraphStore not available: %s", exc)

    try:
        from INAGENT.rag.skeleton_index import get_skeleton_index
        skeleton = get_skeleton_index()
    except Exception as exc:
        logger.warning("[farm-owner] SkeletonIndex not available: %s", exc)

    contexts: Dict[str, _ChunkTreeContext] = {}

    for chunk in chunks:
        key = _chunk_key(chunk)
        if not key:
            continue

        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}

        ctx = _ChunkTreeContext(chunk_key=key)

        # Derive entity title candidates for tree lookup
        entity_hints: List[str] = []
        cmd_prefix = str(meta.get("command_prefix") or "").strip()
        if cmd_prefix:
            entity_hints.append(cmd_prefix)
        section_title = str(meta.get("section_title") or "").strip()
        if section_title and section_title not in entity_hints:
            entity_hints.append(section_title)
        tree_node_id = str(meta.get("tree_node_id") or "").strip()
        if tree_node_id and tree_node_id not in entity_hints:
            entity_hints.append(tree_node_id)

        # CLI graph existence check
        if cli_graph and entity_hints:
            for hint in entity_hints:
                try:
                    exists, similar = cli_graph.command_exists(hint)
                except Exception:
                    exists, similar = False, []
                if exists:
                    ctx.exists_in_tree = True
                    ctx.matched_node_id = similar[0] if similar else hint
                    ctx.similar_commands = similar
                    ctx.tree_level = "leaf"
                    break
                if similar:
                    ctx.similar_commands = similar

            # Prefix-truncation parent search if not found
            if not ctx.exists_in_tree and entity_hints:
                best_hint = entity_hints[0]
                tokens = best_hint.split()
                for length in range(len(tokens) - 1, 1, -1):
                    prefix = " ".join(tokens[:length])
                    try:
                        exists, similar = cli_graph.command_exists(prefix)
                    except Exception:
                        exists, similar = False, []
                    if exists:
                        ctx.parent_candidate_id = similar[0] if similar else prefix
                        break

            # Hierarchy prefix
            if ctx.exists_in_tree or ctx.parent_candidate_id:
                h_hint = ctx.matched_node_id or ctx.parent_candidate_id
                try:
                    ctx.hierarchy_prefix = cli_graph.get_hierarchy_prefix(h_hint)
                except Exception:
                    pass

        # Skeleton module lookup
        if skeleton and entity_hints:
            for hint in entity_hints:
                try:
                    mod_id = skeleton.resolve_module(hint)
                except Exception:
                    mod_id = None
                if mod_id:
                    ctx.skeleton_module_id = mod_id
                    try:
                        ctx.skeleton_artifact_exists = skeleton.artifact_count(mod_id) > 0
                    except Exception:
                        pass
                    break

        # Infer tree_level from existing metadata
        tp = meta.get("tree_position")
        if isinstance(tp, dict) and tp.get("tree_level"):
            meta_level = str(tp["tree_level"]).strip().lower()
            if meta_level in ("root", "trunk", "branch", "leaf"):
                if not ctx.tree_level:
                    ctx.tree_level = meta_level

        contexts[key] = ctx

    found = sum(1 for c in contexts.values() if c.exists_in_tree)
    parent = sum(1 for c in contexts.values() if c.parent_candidate_id and not c.exists_in_tree)
    logger.info(
        "[farm-owner] stage2: %d contexts built, %d exact tree match, %d parent match",
        len(contexts), found, parent,
    )
    return contexts


# ── Stage 3: Rules-based classification ──────────────────────────────


def _stage3_rules_classification(
    chunks: List[Dict[str, object]],
    tree_contexts: Dict[str, _ChunkTreeContext],
    llm_patches: Dict[str, Dict[str, object]],
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """Classify chunks where tree context is sufficient. Returns (decided_rows, undecided_chunks)."""
    decided: List[Dict[str, object]] = []
    undecided: List[Dict[str, object]] = []

    for chunk in chunks:
        key = _chunk_key(chunk)
        if not key:
            undecided.append(chunk)
            continue

        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        ctx = tree_contexts.get(key, _ChunkTreeContext(chunk_key=key))
        patch = llm_patches.get(key) or {}
        text = str(chunk.get("page_content") or "").strip()

        source_file = str(meta.get("source_file") or "")
        block_id = str(meta.get("block_id") or "")
        section_title = str(meta.get("section_title") or "")

        # Build chunk_meta_patch from LLM enrichment
        chunk_meta_patch: Dict[str, object] = {
            "source_file": source_file,
            "document_category": str(meta.get("document_category") or ""),
        }
        if patch:
            chunk_meta_patch.update(patch)

        # Rule 1: Exact tree match → merge_into_existing
        if ctx.exists_in_tree:
            evidence = "tree_exact_match"
            if ctx.skeleton_artifact_exists:
                evidence = "tree_exact_match_with_artifact"
            decided.append({
                "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
                "source_file": source_file,
                "entity_title": section_title or ctx.matched_node_id,
                "action": "merge_into_existing",
                "target_node_id": ctx.matched_node_id,
                "target_block_id": block_id,
                "tree_level": ctx.tree_level or "leaf",
                "fill_fields": {},
                "enrich_fields": {},
                "chunk_meta_patch": chunk_meta_patch,
                "source_evidence": evidence,
                "resolved_value": {},
            })
            continue

        # Rule 2: Parent found via prefix truncation → tree_create_*
        if ctx.parent_candidate_id:
            is_cmd = bool(_CMD_LIKE_KEYWORDS.search(text[:300]) or
                          _CMD_LIKE_KEYWORDS.search(section_title))
            action = "tree_create_leaf" if is_cmd else "tree_create_branch"
            decided.append({
                "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
                "source_file": source_file,
                "entity_title": section_title or f"{source_file}#{block_id}",
                "action": action,
                "target_node_id": ctx.parent_candidate_id,
                "target_block_id": block_id,
                "tree_level": "leaf" if is_cmd else "branch",
                "fill_fields": {},
                "enrich_fields": {"parent_candidate": ctx.parent_candidate_id},
                "chunk_meta_patch": chunk_meta_patch,
                "source_evidence": f"parent_prefix_match:{ctx.parent_candidate_id}",
                "resolved_value": {},
            })
            continue

        # Rule 3: LLM-enriched command_prefix matches CLI graph
        enriched_cp = str(patch.get("command_prefix") or "").strip()
        if enriched_cp and key in tree_contexts:
            # Re-check with enriched command_prefix (may not have been used in stage2)
            try:
                from INAGENT.rag.cli_graph_store import get_cli_graph_store
                cg = get_cli_graph_store()
                exists, similar = cg.command_exists(enriched_cp)
            except Exception:
                exists, similar = False, []
            if exists:
                decided.append({
                    "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
                    "source_file": source_file,
                    "entity_title": section_title or enriched_cp,
                    "action": "merge_into_existing",
                    "target_node_id": similar[0] if similar else enriched_cp,
                    "target_block_id": block_id,
                    "tree_level": "leaf",
                    "fill_fields": {},
                    "enrich_fields": {},
                    "chunk_meta_patch": chunk_meta_patch,
                    "source_evidence": "llm_enriched_command_prefix_match",
                    "resolved_value": {},
                })
                continue

        # No rule matched → undecided
        undecided.append(chunk)

    logger.info(
        "[farm-owner] stage3: %d rules-decided, %d undecided -> LLM",
        len(decided), len(undecided),
    )
    return decided, undecided


# ── Stage 4: LLM batch classification ────────────────────────────────

_CLASSIFICATION_SYSTEM_PROMPT = """\
你是一个网络产品CLI知识库的知识分类器。知识树有4层：
- root（系统架构/产品总览）
- trunk（功能模块，如SLB、GSLB）
- branch（功能子组，如SLB > Cookie持久化）
- leaf（具体CLI命令）

对每个文本块，判断其归属：
1. "discard" — 无用知识：
   - 样板文字、目录页、版权页、纯章节标题/编号
   - 物理环境规格（机房温湿度、通风散热、电源要求、防静电、空气质量、机柜安装）
   - 硬件部署准备（物理布线、接地、散热栅、设备开箱）
   - 营销口号、厂商水印、重复前言
   - 内容不含任何CLI命令、配置参数或功能行为描述的纯概述/导言
2. "merge_into_existing" — 内容包含实质性CLI配置/命令/参数描述，属于已有树节点，可直接补充信息。target_node_id填已有节点名
3. "tree_create_leaf" — 包含新CLI命令或配置项的实质性内容，树中不存在。target_node_id填建议的**父节点**名
4. "tree_create_branch" — 包含完整功能子组描述的实质性内容。target_node_id填建议的**父模块/父节点**名（如 "SLB"、"基础网络 > VLAN"）
5. "tree_create_trunk" — 包含完整功能模块描述的实质性内容，树中不存在。target_node_id填建议的**父root节点**或留空
6. "tree_create_root" — 新系统架构文档（极少见）
7. "needs_tree_session" — 无法判断，需人工审阅

注意：
- 只有包含实质性技术内容（CLI命令/配置步骤/功能行为描述/参数说明）的chunk才应归入2-6类。
- 仅有标题+章节号、概述性导语、或物理环境相关说明的chunk应归为discard。
- 下列内容默认不要判为 tree_create_leaf：Function List/Source List、C函数签名/内部实现、物理接线/拓扑步骤、CLI快捷键/语法说明、只有“配置示例/配置目标”标题但没有具体命令正文的chunk。
- tree_create_leaf 只用于原子CLI命令、明确命令参数、或绑定到具体命令的输出/行为说明。
- target_node_id对于tree_create_*类action必须填写建议的父节点路径（如"SLB"、"基础网络 > VXLAN"），不可为空。
- tree_create_leaf 的 target_node_id 不能是 bare trunk（如"SLB"、"基础网络"）；如果只能判断到模块层，请改判为 tree_create_branch 或 needs_tree_session。
- 不要凭空发明 A > B 形式的父路径；只有当 B 明显是已有或应新建的功能分支时才使用该路径。
- tree_level要与action匹配：tree_create_leaf→"leaf"，tree_create_branch→"branch"，tree_create_trunk→"trunk"。

返回JSON对象，包含 "results" 数组，每个元素必须包含 chunk_index 字段与输入对应。
结果数组长度必须与输入chunk数量严格一致，不可省略任何chunk。
{"results": [{"chunk_index": 0, "action": "...", "tree_level": "root|trunk|branch|leaf", "target_node_id": "", "reason": "...", "confidence": 0.0}]}

只返回JSON，不要markdown、不要解释。"""


def _stage4_llm_batch_classification(
    undecided_chunks: List[Dict[str, object]],
    tree_contexts: Dict[str, _ChunkTreeContext],
    llm_patches: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    """Classify remaining undecided chunks via LLM batch calls."""
    if not undecided_chunks:
        return []

    try:
        from openai import OpenAI
    except ImportError:
        OpenAI = None  # type: ignore[misc]

    try:
        import json_repair as _json_repair
    except ImportError:
        _json_repair = None

    if OpenAI is None:
        logger.warning("[farm-owner] openai not available; marking all undecided as needs_tree_session")
        return _fallback_all_undecided(undecided_chunks, llm_patches)

    try:
        from INAGENT.data_tools.auto_convert import (
            _get_llm_gateway_runtime,
            _call_llm_with_retry_and_fallback,
        )
        runtime = _get_llm_gateway_runtime()
    except Exception as exc:
        logger.warning("[farm-owner] LLM gateway not available: %s; fallback", exc)
        return _fallback_all_undecided(undecided_chunks, llm_patches)

    api_key = str(runtime.get("api_key") or "")
    base_url = str(runtime.get("base_url") or "")
    model = str(runtime.get("model") or "")
    if not api_key or not base_url:
        logger.warning("[farm-owner] LLM credentials missing; fallback")
        return _fallback_all_undecided(undecided_chunks, llm_patches)

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=runtime.get("timeout", 120))

    # Build sub-batches (token-aware)
    sub_batches: List[List[Dict[str, object]]] = []
    sub_batch: List[Dict[str, object]] = []
    token_count = 0
    for chunk in undecided_chunks:
        text = str(chunk.get("page_content") or "")[:_CLASSIFY_TEXT_LIMIT]
        item_tokens = math.ceil(len(text) / _CLASSIFY_CHARS_PER_TOKEN)
        if sub_batch and (
            token_count + item_tokens > _CLASSIFY_TOKEN_BUDGET
            or len(sub_batch) >= _CLASSIFY_BATCH_SIZE
        ):
            sub_batches.append(sub_batch)
            sub_batch = []
            token_count = 0
        sub_batch.append(chunk)
        token_count += item_tokens
    if sub_batch:
        sub_batches.append(sub_batch)

    all_decided: List[Dict[str, object]] = []
    lock = __import__("threading").Lock()

    def _process_sub_batch(batch: List[Dict[str, object]]) -> List[Dict[str, object]]:
        chunk_lines = []
        batch_keys = []
        for idx, chunk in enumerate(batch):
            meta = chunk.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            key = _chunk_key(chunk)
            batch_keys.append(key)
            text = str(chunk.get("page_content") or "")[:_CLASSIFY_TEXT_LIMIT]
            sec = str(meta.get("section_title") or "")
            src = str(meta.get("source_file") or "")
            ctx = tree_contexts.get(key, _ChunkTreeContext(chunk_key=key))
            patch = llm_patches.get(key, {})

            header = f"[CHUNK {idx}]"
            if sec:
                header += f" section={sec}"
            if src:
                header += f" source={src}"
            if ctx.similar_commands:
                sc_str = ",".join(ctx.similar_commands[:3])
                header += f" similar_nodes={sc_str}"
            if ctx.parent_candidate_id:
                header += f" parent_candidate={ctx.parent_candidate_id}"
            if ctx.hierarchy_prefix:
                header += f" hierarchy_prefix={ctx.hierarchy_prefix}"
            if patch.get("product_module"):
                header += f" module={patch['product_module']}"
            if patch.get("function_hierarchy"):
                header += f" function_hierarchy={patch['function_hierarchy']}"
            if patch.get("command_prefix"):
                header += f" cmd_prefix={patch['command_prefix']}"
            if patch.get("chunk_type"):
                header += f" chunk_type={patch['chunk_type']}"
            if patch.get("step_type"):
                header += f" step_type={patch['step_type']}"
            if meta.get("section_path"):
                header += f" section_path={meta['section_path']}"
            chunk_lines.append(f"{header}\n{text}")

        user_content = f"共 {len(batch)} 个chunk，请逐一返回分类结果。\n\n" + "\n---\n".join(chunk_lines)

        try:
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[
                    {"role": "system", "content": _CLASSIFICATION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                allow_fallback=False,
                fallback_config=None,
                max_retries=3,
            )
            content = response.choices[0].message.content or "[]"
            if "```" in content:
                match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
                if match:
                    content = match.group(1).strip()
            if _json_repair:
                parsed = _json_repair.loads(content)
            else:
                parsed = json.loads(content)

            if isinstance(parsed, dict):
                for arr_key in ("results", "chunks", "data", "items"):
                    if arr_key in parsed and isinstance(parsed[arr_key], list):
                        parsed = parsed[arr_key]
                        break
                else:
                    parsed = [parsed]

            if not isinstance(parsed, list):
                raise ValueError(f"Expected list, got {type(parsed)}")

        except Exception as exc:
            logger.warning("[farm-owner] LLM classification batch failed: %s", exc)
            return _fallback_batch(batch, llm_patches)

        # Build index map: chunk_index -> parsed item (prefer explicit index)
        parsed_by_idx: Dict[int, Dict[str, object]] = {}
        for pi, item in enumerate(parsed):
            if isinstance(item, dict):
                ci = item.get("chunk_index")
                if isinstance(ci, int) and 0 <= ci < len(batch):
                    parsed_by_idx[ci] = item
                elif pi < len(batch):
                    parsed_by_idx.setdefault(pi, item)

        rows: List[Dict[str, object]] = []
        for idx, chunk in enumerate(batch):
            key = batch_keys[idx]
            meta = chunk.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            source_file = str(meta.get("source_file") or "")
            block_id = str(meta.get("block_id") or "")
            section_title = str(meta.get("section_title") or "")
            patch = llm_patches.get(key, {})

            chunk_meta_patch: Dict[str, object] = {
                "source_file": source_file,
                "document_category": str(meta.get("document_category") or ""),
            }
            if patch:
                chunk_meta_patch.update(patch)

            llm_item = parsed_by_idx.get(idx)
            if llm_item is not None:
                action = str(llm_item.get("action") or "needs_tree_session")
                if action not in _VALID_CLASSIFICATION_ACTIONS:
                    action = "needs_tree_session"
                tree_level = str(llm_item.get("tree_level") or "")
                reason = str(llm_item.get("reason") or "")
                target_id = _select_llm_target_id(
                    action,
                    str(llm_item.get("target_node_id") or ""),
                    tree_contexts.get(key, _ChunkTreeContext(chunk_key=key)),
                    patch if isinstance(patch, dict) else {},
                )
            else:
                action = "needs_tree_session"
                tree_level = ""
                target_id = ""
                reason = "llm_response_missing"

            action, tree_level, target_id = _normalize_tree_fields(action, tree_level, target_id)

            rows.append({
                "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
                "source_file": source_file,
                "entity_title": section_title or f"{source_file}#{block_id}",
                "action": action,
                "target_node_id": target_id,
                "target_block_id": block_id,
                "tree_level": tree_level,
                "fill_fields": {},
                "enrich_fields": {},
                "chunk_meta_patch": chunk_meta_patch,
                "source_evidence": f"llm_classification:{reason}",
                "resolved_value": {},
            })
        return rows

    total_sub = len(sub_batches)
    workers = min(_CLASSIFY_MAX_WORKERS, total_sub) if total_sub else 1
    logger.info(
        "[farm-owner] stage4: %d undecided chunks, %d sub-batches, workers=%d",
        len(undecided_chunks), total_sub, workers,
    )

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_process_sub_batch, sb): i
            for i, sb in enumerate(sub_batches)
        }
        for future in as_completed(futures):
            completed += 1
            idx = futures[future]
            try:
                rows = future.result()
                with lock:
                    all_decided.extend(rows)
                logger.info(
                    "[farm-owner] stage4 sub-batch %d/%d done (%d/%d)",
                    idx + 1, total_sub, completed, total_sub,
                )
            except Exception as exc:
                logger.warning("[farm-owner] stage4 sub-batch %d failed: %s", idx + 1, exc)

    return all_decided


def _fallback_all_undecided(
    chunks: List[Dict[str, object]],
    llm_patches: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    """Fallback: mark all undecided as needs_tree_session."""
    return _fallback_batch(chunks, llm_patches)


def _fallback_batch(
    chunks: List[Dict[str, object]],
    llm_patches: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        key = _chunk_key(chunk)
        source_file = str(meta.get("source_file") or "")
        block_id = str(meta.get("block_id") or "")
        section_title = str(meta.get("section_title") or "")
        patch = llm_patches.get(key, {})
        chunk_meta_patch: Dict[str, object] = {
            "source_file": source_file,
            "document_category": str(meta.get("document_category") or ""),
        }
        if patch:
            chunk_meta_patch.update(patch)
        rows.append({
            "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
            "source_file": source_file,
            "entity_title": section_title or f"{source_file}#{block_id}",
            "action": "needs_tree_session",
            "target_node_id": "",
            "target_block_id": block_id,
            "tree_level": "",
            "fill_fields": {},
            "enrich_fields": {},
            "chunk_meta_patch": chunk_meta_patch,
            "source_evidence": "llm_unavailable_fallback",
            "resolved_value": {},
        })
    return rows


# ── Reconciliation: resolve conflicting tree levels ───────────────────


def _reconcile_decisions(
    rules_decided: List[Dict[str, object]],
    llm_decided: List[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """Reconcile conflicting tree-level decisions for the same entity.

    Two passes:
    1. Same-key pass: group by (entity_title, target_node_id) — when multiple
       chunks target the exact same parent with different tree_create_* actions,
       promote all to the highest level.
    2. Cross-parent pass: group by entity_title alone — the same entity may
       appear under different parents at different levels (e.g. "系统健康检查"
       as branch under "基础网络" AND as leaf under "基础网络 > 系统健康检查").
       Promote all to the highest level across all parents so the entity has
       a single consistent tree level.

    Mutates rows in place. merge_into_existing rows are left unchanged.
    """
    all_rows = list(rules_decided) + list(llm_decided)
    tree_create_rows = [
        r for r in all_rows
        if str(r.get("action") or "") in _ACTION_RANK
    ]

    # ── Pass 1: same (entity_title, target_node_id) ──
    groups: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
    for row in tree_create_rows:
        key = (str(row.get("entity_title") or ""), str(row.get("target_node_id") or ""))
        groups.setdefault(key, []).append(row)

    pass1_promoted = _promote_group_rows(groups)

    # ── Pass 2: same entity_title across all parents ──
    entity_groups: Dict[str, List[Dict[str, object]]] = {}
    for row in tree_create_rows:
        et = str(row.get("entity_title") or "")
        entity_groups.setdefault(et, []).append(row)

    pass2_promoted = _promote_group_rows(entity_groups)

    logger.info(
        "[farm-owner] reconcile: pass1 (same-key) promoted %d rows, "
        "pass2 (cross-parent) promoted %d rows",
        pass1_promoted, pass2_promoted,
    )
    return rules_decided, llm_decided


def _promote_group_rows(
    groups: Dict[object, List[Dict[str, object]]],
) -> int:
    """Promote all rows in each group to the highest tree_create level.

    Returns count of promoted rows.
    """
    promoted_count = 0
    for group_rows in groups.values():
        ranks = [_ACTION_RANK[str(r["action"])] for r in group_rows]
        max_rank = max(ranks)
        if max_rank == min(ranks):
            continue

        max_action = ""
        for act, rank in _ACTION_RANK.items():
            if rank == max_rank:
                max_action = act
                break
        new_level = _TREE_CREATE_TO_LEVEL[max_action]

        for row in group_rows:
            old_action = str(row["action"])
            old_rank = _ACTION_RANK[old_action]
            if old_rank < max_rank:
                old_level = _TREE_CREATE_TO_LEVEL[old_action]
                row["action"] = max_action
                row["tree_level"] = new_level
                evidence = str(row.get("source_evidence") or "")
                row["source_evidence"] = (
                    f"{evidence}+promoted_from_{old_level}_to_{new_level}"
                )
                promoted_count += 1
    return promoted_count


# ── Stage 5: Decision assembly ────────────────────────────────────────


def _build_discard_row(
    chunk: Dict[str, object],
    reason: str,
    llm_patches: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    meta = chunk.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    key = _chunk_key(chunk)
    source_file = str(meta.get("source_file") or "")
    block_id = str(meta.get("block_id") or "")
    section_title = str(meta.get("section_title") or "")
    patch = llm_patches.get(key, {})

    chunk_meta_patch: Dict[str, object] = {
        "source_file": source_file,
        "document_category": str(meta.get("document_category") or ""),
        "owner_excluded": True,
    }
    if patch:
        chunk_meta_patch.update(patch)

    return {
        "schema_version": _OWNER_DECISION_SCHEMA_VERSION,
        "source_file": source_file,
        "entity_title": section_title or f"{source_file}#{block_id}",
        "action": "discard",
        "target_node_id": "",
        "target_block_id": block_id,
        "tree_level": "",
        "fill_fields": {},
        "enrich_fields": {},
        "chunk_meta_patch": chunk_meta_patch,
        "source_evidence": reason,
        "resolved_value": {},
    }


def _build_discard_quality_feedback(
    discarded_chunks: List[Dict[str, object]],
    reason_code: str,
) -> List[Dict[str, object]]:
    """Build quality feedback records for inspector to learn from discards."""
    records: List[Dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for idx, chunk in enumerate(discarded_chunks):
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        source_file = str(meta.get("source_file") or "").strip()
        block_id = str(meta.get("block_id") or "").strip()
        section_title = str(meta.get("section_title") or "").strip()
        if not source_file or not block_id:
            continue
        suggested_rule: Dict[str, object] = {
            "rule_source": "farm_owner_classification",
            "reason_code": reason_code,
            "match": {
                "source_file_glob": source_file,
                "block_id": block_id,
            },
            "action": {"verdict": "reject"},
        }
        if section_title:
            suggested_rule["match"]["section_title"] = section_title
        records.append({
            "schema_version": FEEDBACK_INBOX_SCHEMA_VERSION,
            "feedback_id": f"farm-owner-discard-{reason_code}-{idx}",
            "source_file": source_file,
            "block_id": block_id,
            "gate_key": feedback_gate_key(source_file, block_id),
            "verdict": "reject",
            "reason_code": reason_code,
            "source_run_id": "farm_owner_classification",
            "notes": f"generated_at={now}",
            "suggested_rule": suggested_rule,
        })
    return records


def _stage5_assemble_decisions(
    garbage_chunks: List[Dict[str, object]],
    non_knowledge_chunks: List[Dict[str, object]],
    wrong_product_chunks: List[Dict[str, object]],
    rules_decided: List[Dict[str, object]],
    llm_decided: List[Dict[str, object]],
    llm_patches: Dict[str, Dict[str, object]],
) -> Tuple[List[Dict[str, object]], Dict[str, int], List[Dict[str, object]]]:
    """Assemble final decision rows, stats, and quality rule feedback."""
    all_rows: List[Dict[str, object]] = []

    # Garbage → discard
    garbage_rows = [
        _build_discard_row(c, "garbage_detected", llm_patches) for c in garbage_chunks
    ]
    all_rows.extend(garbage_rows)

    # Non-knowledge → discard
    non_kw_rows = [
        _build_discard_row(c, "non_product_knowledge", llm_patches)
        for c in non_knowledge_chunks
    ]
    all_rows.extend(non_kw_rows)

    # Wrong product → discard
    wrong_product_rows = [
        _build_discard_row(c, "wrong_product", llm_patches)
        for c in wrong_product_chunks
    ]
    all_rows.extend(wrong_product_rows)

    # Rules-decided
    all_rows.extend(rules_decided)

    # LLM-decided
    llm_discarded = [r for r in llm_decided if r.get("action") == "discard"]
    all_rows.extend(llm_decided)

    # Compute stats
    action_counts: Dict[str, int] = {}
    for row in all_rows:
        act = str(row.get("action") or "unknown")
        action_counts[act] = action_counts.get(act, 0) + 1

    direct_fill = action_counts.get("merge_into_existing", 0)
    structural = sum(
        action_counts.get(a, 0)
        for a in ("tree_create_leaf", "tree_create_branch",
                   "tree_create_trunk", "tree_create_root")
    )
    stats = {
        "llm_enhanced": sum(1 for key, p in llm_patches.items() if p),
        "classified": len(all_rows),
        "discarded": len(garbage_rows) + len(non_kw_rows) + len(wrong_product_rows) + len(llm_discarded),
        "direct_fill": direct_fill,
        "structural": structural,
        "rules_decided": len(garbage_rows) + len(non_kw_rows) + len(wrong_product_rows) + len(rules_decided),
        "llm_decided": len(llm_decided),
        "needs_tree_session": action_counts.get("needs_tree_session", 0),
        "wrong_product": len(wrong_product_rows),
        "action_breakdown": action_counts,
    }

    # Quality feedback proposals for discarded chunks
    feedback_records: List[Dict[str, object]] = []
    if garbage_chunks:
        feedback_records.extend(
            _build_discard_quality_feedback(garbage_chunks, "garbage_content")
        )
    if non_knowledge_chunks:
        feedback_records.extend(
            _build_discard_quality_feedback(non_knowledge_chunks, "non_product_section")
        )
    if wrong_product_chunks:
        feedback_records.extend(
            _build_discard_quality_feedback(wrong_product_chunks, "wrong_product")
        )
    # LLM-discarded chunks (extract originals from llm_decided)
    llm_discard_chunks = []
    for row in llm_discarded:
        sf = str(row.get("source_file") or "")
        bid = str(row.get("target_block_id") or "")
        sec = str(row.get("entity_title") or "")
        llm_discard_chunks.append({
            "metadata": {
                "source_file": sf,
                "block_id": bid,
                "section_title": sec,
            }
        })
    if llm_discard_chunks:
        feedback_records.extend(
            _build_discard_quality_feedback(llm_discard_chunks, "llm_classified_useless")
        )

    logger.info(
        "[farm-owner] stage5: %d total rows, %d discard (garbage=%d, non_kw=%d, "
        "wrong_product=%d, llm=%d), %d direct_fill, %d structural, "
        "%d needs_tree_session, %d feedback records",
        len(all_rows), stats["discarded"],
        len(garbage_rows), len(non_kw_rows), len(wrong_product_rows),
        len(llm_discarded), direct_fill,
        structural, stats["needs_tree_session"], len(feedback_records),
    )
    return all_rows, stats, feedback_records


# ── Top-level classification orchestrator ─────────────────────────────


def _classify_quality_passed_chunks(
    gate: Dict[str, Dict[str, object]],
    passed_chunks: List[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], Dict[str, int], List[Dict[str, object]]]:
    """Classify all quality-passed chunks into actionable decisions.

    Returns:
        (decision_rows, stats_dict, quality_feedback_records)
    """
    # Stage 1: Rules-based pre-filter (includes product filter)
    candidates, garbage, non_knowledge, wrong_product = _stage1_rules_prefilter(passed_chunks)

    # Stage 2 + LLM enrichment in parallel (no data dependency between them)
    llm_patches: Dict[str, Dict[str, object]] = {}
    tree_contexts: Dict[str, _ChunkTreeContext] = {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_llm = executor.submit(_build_quality_passed_llm_patches, candidates)
        future_tree = executor.submit(_stage2_tree_context_lookup, candidates)
        try:
            llm_patches = future_llm.result()
        except Exception as exc:
            logger.warning("[farm-owner] LLM enrichment failed: %s", exc)
        try:
            tree_contexts = future_tree.result()
        except Exception as exc:
            logger.warning("[farm-owner] tree context lookup failed: %s", exc)

    # Stage 3: Rules-based classification
    rules_decided, undecided = _stage3_rules_classification(
        candidates, tree_contexts, llm_patches,
    )

    # Stage 4: LLM batch classification for undecided
    llm_decided = _stage4_llm_batch_classification(
        undecided, tree_contexts, llm_patches,
    )

    # Reconcile conflicting tree-level decisions across chunks
    rules_decided, llm_decided = _reconcile_decisions(rules_decided, llm_decided)

    # Validate hierarchy structure after reconciliation
    rules_decided, llm_decided = _validate_hierarchy_decisions(
        rules_decided,
        llm_decided,
        tree_contexts,
        llm_patches,
        candidates,
    )

    # Stage 5: Assemble
    return _stage5_assemble_decisions(
        garbage, non_knowledge, wrong_product, rules_decided, llm_decided, llm_patches,
    )


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

    # ── Full classification pipeline (replaces blind passthrough) ──
    classified_rows, classification_stats, discard_feedback = (
        _classify_quality_passed_chunks(gate, validated_chunks)
    )

    # ── Gap entries (schema_gaps.jsonl) ──
    entries: List[SchemaGapEntry] = []
    if gaps_file.exists() and gaps_file.stat().st_size > 0:
        entries = _load_gap_entries(gaps_file)
    entries = _filter_entries_by_quality_gate(entries, gate)
    deferred_rows = _build_quality_deferred_decisions(entries, gate, validated_chunks)

    # ── Quality rule proposals: from gap entries + discard feedback ──
    gap_proposals_file = _write_quality_rule_proposals(reference_dir, entries)

    # Write discard feedback as additional rule proposals
    discard_proposals_file: Optional[Path] = None
    if discard_feedback:
        discard_proposals_file = reference_dir / QUALITY_RULE_PROPOSALS_FILE
        doc = build_rule_proposals_from_inbox(
            discard_feedback,
            inbox_path="farm_owner_classification_discards",
        )
        # Merge with existing gap proposals if present
        if gap_proposals_file and gap_proposals_file.exists():
            try:
                existing = json.loads(
                    gap_proposals_file.read_text(encoding="utf-8")
                )
                gap_rules = existing.get("rules", [])
                doc["rules"] = doc.get("rules", []) + gap_rules
            except Exception:
                pass
        discard_proposals_file.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    proposals_file = discard_proposals_file or gap_proposals_file

    # ── Write decisions ──
    owner_decisions_file = _write_owner_decisions(
        reference_dir,
        classified_rows,
        deferred_rows,
        gate,
    )

    result: Dict[str, object] = {
        "processed": bool(classified_rows or deferred_rows),
        "decision_schema_version": _OWNER_DECISION_SCHEMA_VERSION,
        "owner_decisions_file": str(owner_decisions_file),
        "quality_passed_blocks": len(validated_chunks),
        "classified_blocks": classification_stats.get("classified", 0),
        "llm_enhanced_blocks": classification_stats.get("llm_enhanced", 0),
        "discarded_blocks": classification_stats.get("discarded", 0),
        "direct_fill_blocks": classification_stats.get("direct_fill", 0),
        "structural_blocks": classification_stats.get("structural", 0),
        "wrong_product_blocks": classification_stats.get("wrong_product", 0),
        "rules_decided_blocks": classification_stats.get("rules_decided", 0),
        "llm_decided_blocks": classification_stats.get("llm_decided", 0),
        "needs_tree_session_blocks": classification_stats.get("needs_tree_session", 0),
        "action_breakdown": classification_stats.get("action_breakdown", {}),
        "gap_entries": len(entries),
        "delegated_gap_entries": len(deferred_rows),
        "quality_rule_proposals_file": (
            str(proposals_file) if proposals_file else None
        ),
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
