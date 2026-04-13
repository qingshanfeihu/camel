# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Closed-loop quality feedback: inbox validation, rule proposals, purge manifest (see PLAN_CLOSED_LOOP_QA_AND_PARENT_DOC_MERGE.md)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_INAGENT_ROOT = Path(__file__).resolve().parent.parent

# Artifact filenames (under knowledge_base/reference unless noted)
QUALITY_FEEDBACK_INBOX_FILE = "_quality_feedback_inbox.jsonl"
QUALITY_RULE_PROPOSALS_FILE = "_quality_rule_proposals.json"
QUALITY_PURGE_MANIFEST_FILE = "_quality_purge_manifest.jsonl"

FEEDBACK_INBOX_SCHEMA_VERSION = "1.0"
RULE_PROPOSALS_SCHEMA_VERSION = "1.0"
PURGE_MANIFEST_SCHEMA_VERSION = "1.0"

_SAFE_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.json$")


def feedback_gate_key(source_file: str, block_id: str) -> str:
    """Align with quality gate / farm_owner_ingest keying."""
    return f"{source_file}::{block_id}"


def _is_safe_reference_basename(name: str) -> bool:
    if not name or name.startswith("_"):
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_SAFE_REF_NAME.match(name))


def _reference_dir_must_be_under_inagent(reference_dir: Path) -> Path:
    ref = reference_dir.resolve()
    root = _INAGENT_ROOT.resolve()
    try:
        ref.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"reference_dir must be under INAGENT root {root}, got {ref}"
        ) from exc
    return ref


def validate_feedback_inbox_record(obj: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate one inbox JSON object. Returns (record, error_message)."""
    if not isinstance(obj, dict):
        return None, "record must be a JSON object"
    ver = str(obj.get("schema_version") or FEEDBACK_INBOX_SCHEMA_VERSION)
    if ver != FEEDBACK_INBOX_SCHEMA_VERSION:
        return None, f"unsupported schema_version {ver!r}, expected {FEEDBACK_INBOX_SCHEMA_VERSION!r}"

    fid = str(obj.get("feedback_id") or "").strip()
    if not fid:
        return None, "missing feedback_id"

    source_file = str(obj.get("source_file") or "").strip()
    if not source_file:
        return None, "missing source_file"
    if not _is_safe_reference_basename(source_file):
        return None, f"unsafe or reserved source_file: {source_file!r}"

    block_id = str(obj.get("block_id") or "").strip()
    if not block_id:
        return None, "missing block_id"

    verdict = str(obj.get("verdict") or "").strip()
    if verdict not in ("reject", "flag", "review"):
        return None, f"verdict must be reject|flag|review, got {verdict!r}"

    reason_code = str(obj.get("reason_code") or "").strip()
    if not reason_code:
        return None, "missing reason_code"

    record: Dict[str, Any] = {
        "schema_version": FEEDBACK_INBOX_SCHEMA_VERSION,
        "feedback_id": fid,
        "source_file": source_file,
        "block_id": block_id,
        "verdict": verdict,
        "reason_code": reason_code,
        "gate_key": feedback_gate_key(source_file, block_id),
    }
    if obj.get("suggested_rule") is not None:
        if not isinstance(obj["suggested_rule"], dict):
            return None, "suggested_rule must be an object when present"
        record["suggested_rule"] = obj["suggested_rule"]
    if obj.get("source_run_id") is not None:
        record["source_run_id"] = str(obj["source_run_id"])
    if obj.get("notes") is not None:
        record["notes"] = str(obj["notes"])
    return record, None


def load_and_validate_inbox(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load JSONL inbox; return (valid_records, error_lines)."""
    if not path.exists():
        return [], [f"file not found: {path}"]

    valid: List[Dict[str, Any]] = []
    errors: List[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"line {i}: JSON decode error: {e}")
            continue
        rec, err = validate_feedback_inbox_record(obj)
        if err:
            errors.append(f"line {i}: {err}")
        else:
            assert rec is not None
            valid.append(rec)
    return valid, errors


def _canonical_rule_key(rule: Dict[str, Any]) -> str:
    return json.dumps(rule, ensure_ascii=False, sort_keys=True, default=str)


def build_rule_proposals_from_inbox(
    records: List[Dict[str, Any]],
    *,
    inbox_path: str = "",
) -> Dict[str, Any]:
    """Aggregate suggested_rule fragments into a reviewable proposals document (does not write files)."""
    buckets: Dict[str, List[str]] = {}
    for rec in records:
        sr = rec.get("suggested_rule")
        if not isinstance(sr, dict) or not sr:
            continue
        key = _canonical_rule_key(sr)
        buckets.setdefault(key, []).append(rec["feedback_id"])

    proposals: List[Dict[str, Any]] = []
    for key, fids in sorted(buckets.items(), key=lambda x: -len(x[1])):
        rule = json.loads(key)
        proposals.append(
            {
                "aggregated_from_feedback_ids": sorted(fids),
                "support_count": len(fids),
                "suggested_rule": rule,
                "conflict_note": None,
            }
        )

    conflicts: List[Dict[str, Any]] = []
    by_gate: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        gk = rec["gate_key"]
        by_gate.setdefault(gk, []).append(rec)
    for gk, group in by_gate.items():
        keys = {_canonical_rule_key(r["suggested_rule"]) for r in group if r.get("suggested_rule")}
        if len(keys) > 1:
            conflicts.append(
                {
                    "gate_key": gk,
                    "feedback_ids": [r["feedback_id"] for r in group],
                    "distinct_suggested_rules": len(keys),
                }
            )

    return {
        "schema_version": RULE_PROPOSALS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from_inbox": inbox_path,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "conflicts": conflicts,
    }


def validate_purge_manifest_record(obj: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(obj, dict):
        return None, "record must be a JSON object"
    ver = str(obj.get("schema_version") or PURGE_MANIFEST_SCHEMA_VERSION)
    if ver != PURGE_MANIFEST_SCHEMA_VERSION:
        return None, f"unsupported schema_version {ver!r}"

    mid = str(obj.get("manifest_id") or "").strip()
    if not mid:
        return None, "missing manifest_id"

    source_file = str(obj.get("source_file") or "").strip()
    if not source_file or not _is_safe_reference_basename(source_file):
        return None, f"invalid source_file: {source_file!r}"

    block_id = str(obj.get("block_id") or "").strip()
    if not block_id:
        return None, "missing block_id"

    action = str(obj.get("action") or "").strip()
    if action not in ("remove_chunk", "patch_metadata"):
        return None, f"action must be remove_chunk|patch_metadata, got {action!r}"

    approved_by = str(obj.get("approved_by") or "").strip()
    if not approved_by:
        return None, "missing approved_by (human gate)"

    rec: Dict[str, Any] = {
        "schema_version": PURGE_MANIFEST_SCHEMA_VERSION,
        "manifest_id": mid,
        "source_file": source_file,
        "block_id": block_id,
        "action": action,
        "approved_by": approved_by,
        "gate_key": feedback_gate_key(source_file, block_id),
    }
    if obj.get("approved_at"):
        rec["approved_at"] = str(obj["approved_at"])
    if action == "patch_metadata":
        patch = obj.get("metadata_patch")
        if not isinstance(patch, dict) or not patch:
            return None, "patch_metadata action requires non-empty metadata_patch object"
        rec["metadata_patch"] = patch
    return rec, None


def load_and_validate_purge_manifest(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not path.exists():
        return [], [f"file not found: {path}"]
    valid: List[Dict[str, Any]] = []
    errors: List[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"line {i}: JSON decode error: {e}")
            continue
        rec, err = validate_purge_manifest_record(obj)
        if err:
            errors.append(f"line {i}: {err}")
        else:
            assert rec is not None
            valid.append(rec)
    return valid, errors


@dataclass
class PurgeApplyReport:
    applied: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    dry_run: bool = True


def _find_block_index(chunks: List[Any], block_id: str) -> Optional[int]:
    for i, ch in enumerate(chunks):
        if not isinstance(ch, dict):
            continue
        meta = ch.get("metadata") or {}
        if isinstance(meta, dict) and str(meta.get("block_id") or "") == block_id:
            return i
    return None


def apply_purge_manifest(
    reference_dir: Path,
    records: List[Dict[str, Any]],
    *,
    dry_run: bool = True,
    strict_inagent: bool = True,
) -> PurgeApplyReport:
    """Apply validated purge records to JSON lists under *reference_dir*.

    Security: *reference_dir* must resolve under INAGENT root when strict_inagent is True.
    Only basenames in records are used; files must not start with ``_``.
    """
    report = PurgeApplyReport(dry_run=dry_run)
    if strict_inagent:
        reference_dir = _reference_dir_must_be_under_inagent(reference_dir)

    ref = reference_dir.resolve()

    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        by_file.setdefault(rec["source_file"], []).append(rec)

    for source_file, file_recs in by_file.items():
        if not _is_safe_reference_basename(source_file):
            for r in file_recs:
                report.skipped.append((r["gate_key"], "unsafe source_file"))
            continue
        fp = (ref / source_file).resolve()
        try:
            fp.relative_to(ref)
        except ValueError:
            for r in file_recs:
                report.skipped.append((r["gate_key"], "path escape"))
            continue
        if not fp.is_file():
            for r in file_recs:
                report.skipped.append((r["gate_key"], "file missing"))
            continue

        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            for r in file_recs:
                report.skipped.append((r["gate_key"], f"read error: {e}"))
            continue
        if not isinstance(raw, list):
            for r in file_recs:
                report.skipped.append((r["gate_key"], "reference file is not a list"))
            continue

        working: List[Any] = list(raw)
        file_dirty = False

        for r in file_recs:
            bid = r["block_id"]
            action = r["action"]
            idx = _find_block_index(working, bid)
            if idx is None:
                report.skipped.append((r["gate_key"], "block_id not found"))
                continue

            if action == "remove_chunk":
                if dry_run:
                    report.applied.append({**r, "dry_run": True})
                else:
                    del working[idx]
                    file_dirty = True
                    report.applied.append({**r, "dry_run": False})
            else:
                chunk = working[idx]
                if not isinstance(chunk, dict):
                    report.skipped.append((r["gate_key"], "chunk is not an object"))
                    continue
                meta = chunk.get("metadata")
                if not isinstance(meta, dict):
                    meta = {}
                    chunk["metadata"] = meta
                patch = r.get("metadata_patch") or {}
                if dry_run:
                    report.applied.append({**r, "dry_run": True, "would_patch": dict(patch)})
                else:
                    meta.update(patch)
                    file_dirty = True
                    report.applied.append({**r, "dry_run": False})

        if not dry_run and file_dirty:
            fp.write_text(json.dumps(working, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report
