# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Mechanical parent/child reference JSON merge (no LLM). See PLAN_CLOSED_LOOP_QA_AND_PARENT_DOC_MERGE.md."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import yaml

from INAGENT.data_tools.quality_feedback_loop import (
    _is_safe_reference_basename,
    _reference_dir_must_be_under_inagent,
)

logger = logging.getLogger(__name__)

CONFIG_SCHEMA_VERSION = "1.0"


@dataclass
class ParentChildMergeReport:
    dry_run: bool = True
    jobs: int = 0
    chunks_moved: int = 0
    child_files_emptied: int = 0
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (context, reason)
    block_id_collisions: List[str] = field(default_factory=list)


def _load_config(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("config must be a mapping")
    return data


def _existing_block_ids(chunks: List[Any]) -> Set[str]:
    out: Set[str] = set()
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        meta = ch.get("metadata") or {}
        if isinstance(meta, dict):
            bid = str(meta.get("block_id") or "")
            if bid:
                out.add(bid)
    return out


def merge_reference_parent_child(
    reference_dir: Path,
    config_path: Path,
    *,
    dry_run: bool = True,
    strict_inagent: bool = True,
) -> ParentChildMergeReport:
    """Append chunks from child reference JSON files into parent files.

    - Preserves each chunk's ``metadata.block_id`` (stable gate keys).
    - Sets ``metadata.source_file`` to the **parent** basename so ``merge_knowledge_base`` is consistent.
    - After merge: each child file is written as ``[]`` if ``child_after_merge`` is ``empty`` (default).
    """
    report = ParentChildMergeReport(dry_run=dry_run)
    if strict_inagent:
        reference_dir = _reference_dir_must_be_under_inagent(reference_dir)
    ref = reference_dir.resolve()

    cfg = _load_config(config_path)
    ver = str(cfg.get("schema_version") or CONFIG_SCHEMA_VERSION)
    if ver != CONFIG_SCHEMA_VERSION:
        report.skipped.append(("config", f"unsupported schema_version {ver!r}"))
        return report

    jobs = cfg.get("merge_jobs") or []
    if not isinstance(jobs, list):
        report.skipped.append(("config", "merge_jobs must be a list"))
        return report

    for ji, job in enumerate(jobs):
        if not isinstance(job, dict):
            report.skipped.append((f"job[{ji}]", "not an object"))
            continue
        parent_name = str(job.get("parent") or "").strip()
        children = job.get("children") or []
        child_after = str(job.get("child_after_merge") or "empty").strip()
        if child_after not in ("empty", "noop"):
            report.skipped.append((f"job[{ji}]", f"unknown child_after_merge: {child_after!r}"))
            continue
        if not _is_safe_reference_basename(parent_name):
            report.skipped.append((f"job[{ji}]", f"invalid parent: {parent_name!r}"))
            continue
        if not isinstance(children, list) or not children:
            report.skipped.append((f"job[{ji}]", "children must be a non-empty list"))
            continue

        parent_path = (ref / parent_name).resolve()
        try:
            parent_path.relative_to(ref)
        except ValueError:
            report.skipped.append((f"job[{ji}]", "parent path escape"))
            continue

        report.jobs += 1

        if parent_path.is_file():
            try:
                parent_chunks = json.loads(parent_path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                report.skipped.append((parent_name, f"parent read error: {e}"))
                continue
        elif dry_run:
            parent_chunks = []
        else:
            parent_path.parent.mkdir(parents=True, exist_ok=True)
            parent_chunks = []

        if not isinstance(parent_chunks, list):
            report.skipped.append((parent_name, "parent is not a JSON list"))
            continue

        occupied = _existing_block_ids(parent_chunks)

        for ch_name in children:
            cn = str(ch_name).strip()
            if not _is_safe_reference_basename(cn):
                report.skipped.append((cn, "invalid child basename"))
                continue
            cp = (ref / cn).resolve()
            try:
                cp.relative_to(ref)
            except ValueError:
                report.skipped.append((cn, "child path escape"))
                continue
            if not cp.is_file():
                report.skipped.append((cn, "child file missing"))
                continue
            try:
                child_chunks = json.loads(cp.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                report.skipped.append((cn, f"child read error: {e}"))
                continue
            if not isinstance(child_chunks, list):
                report.skipped.append((cn, "child is not a JSON list"))
                continue

            for ch in child_chunks:
                if not isinstance(ch, dict):
                    continue
                meta = ch.get("metadata")
                if not isinstance(meta, dict):
                    meta = {}
                    ch["metadata"] = meta
                bid = str(meta.get("block_id") or "")
                if not bid:
                    report.skipped.append((cn, "chunk missing block_id, skipped"))
                    continue
                if bid in occupied:
                    report.block_id_collisions.append(f"{parent_name}::{bid}")
                    report.skipped.append((f"{cn}::{bid}", "block_id already in parent"))
                    continue
                meta["source_file"] = parent_name
                parent_chunks.append(ch)
                occupied.add(bid)
                report.chunks_moved += 1

            if child_after == "empty":
                if dry_run:
                    report.child_files_emptied += 1
                else:
                    cp.write_text("[]\n", encoding="utf-8")
                    report.child_files_emptied += 1

        if not dry_run:
            parent_path.write_text(
                json.dumps(parent_chunks, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    return report
