# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Quality ingest: validate procurement output and emit filtered reference for farm-owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
from fnmatch import fnmatch as _fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional

from INAGENT.agents.knowledge_quality_inspector_agent import (
    KnowledgeQualityInspectorAgent,
)
from INAGENT.data_tools.ingest_validator import IngestValidator
from INAGENT.rag.cli_graph_store import get_cli_graph_store
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("quality_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
_QUALITY_GATE_FILE = "_quality_gate_for_owner.jsonl"
_OWNER_INPUT_GAPS_FILE = "schema_gaps.jsonl"
_QUALITY_REFERENCE_FOR_OWNER_FILE = "_quality_reference_for_owner.json"
_OWNER_RULE_OVERRIDES_FILE = "_owner_quality_overrides.jsonl"
_OWNER_CONDITION_RULES_FILE = "_owner_quality_rules.json"
_QUALITY_GATE_SCHEMA_VERSION = "1.0"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _load_owner_rule_overrides(reference_dir: Path) -> Dict[str, Dict[str, object]]:
    """Load optional owner-side quality overrides for future rule evolution.

    Format: JSONL keyed by source_file + block_id.
    Supported fields:
      - is_product_knowledge: bool
      - quality_reason: str
      - risk_level: str
      - rule_tag: str
      - rule_version: str
    """
    path = reference_dir / _OWNER_RULE_OVERRIDES_FILE
    if not path.exists() or path.stat().st_size == 0:
        return {}

    overrides: Dict[str, Dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
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

        patch: Dict[str, object] = {}
        if "is_product_knowledge" in item:
            patch["is_product_knowledge"] = bool(item.get("is_product_knowledge"))
        if item.get("quality_reason"):
            patch["quality_reason"] = str(item.get("quality_reason"))
        if item.get("risk_level"):
            patch["risk_level"] = str(item.get("risk_level"))
        if item.get("rule_tag"):
            patch["rule_tag"] = str(item.get("rule_tag"))
        if item.get("rule_version"):
            patch["rule_version"] = str(item.get("rule_version"))
        if patch:
            overrides[key] = patch

    return overrides


def _load_owner_condition_rules(reference_dir: Path) -> List[Dict[str, Any]]:
    """Load condition-based owner quality rules from JSON.

    File location: reference_dir/_owner_quality_rules.json
    Schema::

        {
          "schema_version": "1.0",
          "rules": [
            {
              "rule_id": "block_copyright",
              "rule_version": "1.0",
              "enabled": true,
              "description": "optional human note",
              "priority": 100,
              "match_conditions": {
                "section_title_regex": "版权|copyright",
                "source_file_glob": "app_*",
                "content_contains": null,
                "min_content_length": null,
                "max_content_length": null
              },
              "action": "block",
              "override": {
                "quality_reason": "owner_rule:copyright_section",
                "risk_level": "high"
              }
            }
          ]
        }

    Rules are applied in descending ``priority`` order; first match wins.
    Missing file or empty file → returns empty list (no-op).
    """
    path = reference_dir / _OWNER_CONDITION_RULES_FILE
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[quality] failed to load condition rules: %s", exc)
        return []
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        return []
    rules = [r for r in rules if isinstance(r, dict)]
    rules.sort(key=lambda r: -int(r.get("priority") or 0))
    return rules


def _match_chunk_against_rule(chunk: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """Return True if *chunk* satisfies every condition in *rule*."""
    conds = rule.get("match_conditions")
    if not conds or not isinstance(conds, dict):
        return True  # No conditions → unconditional match

    meta = chunk.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    title_regex = conds.get("section_title_regex")
    if title_regex:
        title = str(meta.get("section_title") or "")
        if not re.search(title_regex, title, re.IGNORECASE):
            return False

    src_glob = conds.get("source_file_glob")
    if src_glob:
        src_full = str(meta.get("source_file") or "")
        src_name = Path(src_full).name if src_full else ""
        if not (_fnmatch(src_full, src_glob) or _fnmatch(src_name, src_glob)):
            return False

    content_contains = conds.get("content_contains")
    min_len = conds.get("min_content_length")
    max_len = conds.get("max_content_length")
    if content_contains is not None or min_len is not None or max_len is not None:
        content = str(chunk.get("page_content") or chunk.get("text") or "")
        if content_contains is not None and str(content_contains) not in content:
            return False
        if min_len is not None and len(content) < int(min_len):
            return False
        if max_len is not None and len(content) > int(max_len):
            return False

    return True


def _apply_condition_rules(
    chunk: Dict[str, Any],
    rules: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return a decision patch from the first matching enabled rule, or None."""
    for rule in rules:
        if not rule.get("enabled", False):
            continue
        if _match_chunk_against_rule(chunk, rule):
            action = str(rule.get("action") or "block").strip().lower()
            is_product = action == "pass"
            override = rule.get("override") or {}
            rule_id = str(rule.get("rule_id") or "")
            return {
                "is_product_knowledge": is_product,
                "quality_reason": str(
                    override.get("quality_reason")
                    or f"owner_condition_rule:{rule_id}"
                ),
                "risk_level": str(
                    override.get("risk_level") or ("low" if is_product else "high")
                ),
                "rule_tag": rule_id,
                "rule_version": str(rule.get("rule_version") or ""),
            }
    return None


def _build_chunk_decisions(
    raw_chunks: list,
    validator: IngestValidator,
    owner_overrides: Dict[str, Dict[str, object]],
    *,
    condition_rules: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Dict[int, Dict[str, object]], Dict[str, object]]:
    if condition_rules is None:
        condition_rules = []
    decisions: Dict[int, Dict[str, object]] = {}
    override_total = 0
    override_to_pass = 0
    override_to_block = 0
    condition_rule_total = 0
    condition_rule_to_pass = 0
    condition_rule_to_block = 0

    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue

        verdict, reason = validator.validate_chunk(chunk)
        is_product = verdict == "accept"
        quality_reason = "passed" if is_product else (reason or "filtered_by_ingest_validator")
        risk_level = "low" if is_product else "high"

        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        source_file = str(meta.get("source_file") or "unknown")
        block_id = str(meta.get("block_id") or "")
        key = f"{source_file}::{block_id}"

        decision: Dict[str, object] = {
            "is_product_knowledge": is_product,
            "quality_reason": quality_reason,
            "risk_level": risk_level,
            "decision_source": "ingest_validator",
            "rule_tag": "",
            "rule_version": "",
        }

        # Apply condition-based owner rules (pattern/metadata matching; no block_id needed)
        cond_patch = _apply_condition_rules(chunk, condition_rules)
        if cond_patch:
            condition_rule_total += 1
            old_is_product = bool(decision["is_product_knowledge"])
            decision["is_product_knowledge"] = bool(cond_patch["is_product_knowledge"])
            decision["quality_reason"] = str(cond_patch["quality_reason"])
            decision["risk_level"] = str(cond_patch["risk_level"])
            decision["decision_source"] = "owner_condition_rule"
            decision["rule_tag"] = str(cond_patch["rule_tag"])
            decision["rule_version"] = str(cond_patch["rule_version"])
            new_is_product = bool(decision["is_product_knowledge"])
            if old_is_product != new_is_product:
                if new_is_product:
                    condition_rule_to_pass += 1
                else:
                    condition_rule_to_block += 1

        patch = owner_overrides.get(key)
        if patch:
            override_total += 1
            old_is_product = bool(decision["is_product_knowledge"])
            if "is_product_knowledge" in patch:
                decision["is_product_knowledge"] = bool(patch["is_product_knowledge"])
            if patch.get("quality_reason"):
                decision["quality_reason"] = str(patch["quality_reason"])
            else:
                decision["quality_reason"] = (
                    "owner_rule_override_pass"
                    if bool(decision["is_product_knowledge"])
                    else "owner_rule_override_block"
                )
            if patch.get("risk_level"):
                decision["risk_level"] = str(patch["risk_level"])
            else:
                decision["risk_level"] = (
                    "low" if bool(decision["is_product_knowledge"]) else "high"
                )
            decision["decision_source"] = "owner_rule_hook"
            decision["rule_tag"] = str(patch.get("rule_tag") or "")
            decision["rule_version"] = str(patch.get("rule_version") or "")

            new_is_product = bool(decision["is_product_knowledge"])
            if old_is_product != new_is_product:
                if new_is_product:
                    override_to_pass += 1
                else:
                    override_to_block += 1

        decisions[id(chunk)] = decision

    return decisions, {
        "overrides_file": str(_OWNER_RULE_OVERRIDES_FILE),
        "overrides_total": override_total,
        "overrides_to_pass": override_to_pass,
        "overrides_to_block": override_to_block,
        "condition_rules_total": condition_rule_total,
        "condition_rules_to_pass": condition_rule_to_pass,
        "condition_rules_to_block": condition_rule_to_block,
    }


def _write_quality_gate_and_owner_gaps(
    reference_dir: Path,
    raw_chunks: list,
    chunk_decisions: Dict[int, Dict[str, object]],
) -> Dict[str, object]:
    gate_path = reference_dir / _QUALITY_GATE_FILE
    owner_gaps_path = reference_dir / _OWNER_INPUT_GAPS_FILE
    gate_lines = []
    owner_gap_lines = []

    for index, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        decision = chunk_decisions.get(id(chunk), {})
        is_product = bool(decision.get("is_product_knowledge", False))
        source_file = str(meta.get("source_file") or "unknown")
        block_id = str(meta.get("block_id") or "")
        section_title = str(meta.get("section_title") or "")
        doc_category = str(meta.get("document_category") or "")
        quality_reason = str(
            decision.get(
                "quality_reason",
                "passed" if is_product else "filtered_by_ingest_validator",
            )
        )
        risk_level = str(decision.get("risk_level", "low" if is_product else "high"))
        decision_source = str(decision.get("decision_source", "ingest_validator"))
        rule_tag = str(decision.get("rule_tag") or "")
        rule_version = str(decision.get("rule_version") or "")

        gate_item = {
            "schema_version": _QUALITY_GATE_SCHEMA_VERSION,
            "source_file": source_file,
            "block_id": block_id,
            "chunk_index": index,
            "is_product_knowledge": is_product,
            "quality_reason": quality_reason,
            "risk_level": risk_level,
            "decision_source": decision_source,
            "hook_rule_tag": rule_tag,
            "hook_rule_version": rule_version,
            "section_title": section_title,
            "document_category": doc_category,
        }
        gate_lines.append(json.dumps(gate_item, ensure_ascii=False))

        if not is_product:
            content = str(chunk.get("page_content") or chunk.get("text") or "")
            owner_gap = {
                "schema_version": _QUALITY_GATE_SCHEMA_VERSION,
                "gap_type": "non_knowledge",
                "entity_title": section_title or f"{source_file}#{block_id or index}",
                "entity_description": "filtered by quality gate",
                "evidence": quality_reason,
                "source_file": source_file,
                "chunk_content": content[:4000],
                "chunk_block_id": block_id,
            }
            owner_gap_lines.append(json.dumps(owner_gap, ensure_ascii=False))

    gate_path.write_text("\n".join(gate_lines) + ("\n" if gate_lines else ""), encoding="utf-8")
    owner_gaps_path.write_text(
        "\n".join(owner_gap_lines) + ("\n" if owner_gap_lines else ""),
        encoding="utf-8",
    )
    return {
        "quality_gate_schema_version": _QUALITY_GATE_SCHEMA_VERSION,
        "quality_gate_path": str(gate_path),
        "quality_gate_total": len(gate_lines),
        "quality_gate_passed": len(gate_lines) - len(owner_gap_lines),
        "quality_gate_blocked": len(owner_gap_lines),
        "owner_input_gaps_path": str(owner_gaps_path),
    }


def run_quality_pipeline(
    reference_dir: Path | None = None,
    decisions_path: Optional[Path] = None,
    accepted_path: Optional[Path] = None,
    summary_path: Optional[Path] = None,
    archive_to: Optional[Path] = None,
) -> Dict[str, object]:
    load_inagent_env()
    reference_dir = reference_dir or _REFERENCE_DIR
    kb_input_path = reference_dir / "knowledge_base.json"
    ref_out_path = reference_dir / _QUALITY_REFERENCE_FOR_OWNER_FILE
    result: Dict[str, object] = {}

    logger.info("[quality] start: reference_dir=%s", reference_dir)

    if kb_input_path.exists():
        raw_chunks = json.loads(kb_input_path.read_text(encoding="utf-8"))
        cli_graph = get_cli_graph_store()
        validator = IngestValidator(cli_graph_store=cli_graph)

        owner_overrides = _load_owner_rule_overrides(reference_dir)
        condition_rules = _load_owner_condition_rules(reference_dir)
        chunk_decisions, hook_summary = _build_chunk_decisions(
            raw_chunks,
            validator,
            owner_overrides,
            condition_rules=condition_rules,
        )
        validated = [
            chunk
            for chunk in raw_chunks
            if isinstance(chunk, dict)
            and bool(chunk_decisions.get(id(chunk), {}).get("is_product_knowledge", False))
        ]

        ref_out_path.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validator.save_report(reference_dir)
        result["ingest_report"] = validator.report.to_dict()
        result["owner_rule_hook"] = hook_summary
        result["reference_for_owner_path"] = str(ref_out_path)
        result.update(
            _write_quality_gate_and_owner_gaps(
                reference_dir,
                raw_chunks,
                chunk_decisions,
            )
        )
        logger.info(
            "[quality] knowledge_base validated: accepted=%d rejected=%d",
            len(validated),
            int(result.get("quality_gate_blocked", 0)),
        )
        logger.info(
            "[quality] gate written: total=%d passed=%d blocked=%d",
            result.get("quality_gate_total", 0),
            result.get("quality_gate_passed", 0),
            result.get("quality_gate_blocked", 0),
        )
        logger.info(
            "[quality] owner rule hook: total=%d pass=%d block=%d",
            hook_summary.get("overrides_total", 0),
            hook_summary.get("overrides_to_pass", 0),
            hook_summary.get("overrides_to_block", 0),
        )
        logger.info(
            "[quality] condition rules: total=%d pass=%d block=%d",
            hook_summary.get("condition_rules_total", 0),
            hook_summary.get("condition_rules_to_pass", 0),
            hook_summary.get("condition_rules_to_block", 0),
        )
        logger.info(
            "[quality] mode: algorithm + owner condition rules + owner overrides (no llm fallback)",
        )
    else:
        result["ingest_report"] = None

    has_exports = (
        decisions_path
        and accepted_path
        and decisions_path.exists()
        and accepted_path.exists()
    )
    if has_exports:
        export_check = (
            KnowledgeQualityInspectorAgent.validate_procurement_export(
                decisions_path,
                accepted_path,
            )
        )
        result["procurement_export"] = {
            "ok": export_check.ok,
            "expected_count": export_check.expected_count,
            "on_disk_count": export_check.on_disk_count,
            "detail": export_check.mismatch_detail,
        }
        if summary_path and summary_path.exists():
            summary_check = (
                KnowledgeQualityInspectorAgent.validate_summary_accept_count(
                    decisions_path,
                    summary_path,
                )
            )
            result["summary_accept_count"] = {
                "ok": summary_check.ok,
                "summary_accept": summary_check.summary_accept,
                "decisions_accept": summary_check.decisions_accept,
                "detail": summary_check.detail,
            }
    else:
        result["procurement_export"] = {
            "ok": None,
            "skipped": True,
            "reason": "decisions/accepted inputs missing",
            "decisions_path": str(decisions_path) if decisions_path else None,
            "accepted_path": str(accepted_path) if accepted_path else None,
        }
        logger.info(
            "[quality] procurement export check skipped: decisions=%s accepted=%s",
            decisions_path,
            accepted_path,
        )

    report_path = reference_dir / "_quality_report.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[quality] report written -> %s", report_path)

    if archive_to is not None:
        archive_to.mkdir(parents=True, exist_ok=True)
        ref_snapshot = archive_to / "reference_snapshot"
        ref_snapshot.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            shutil.copy2(report_path, archive_to / "_quality_report.json")
        ir = reference_dir / "_ingest_report.json"
        if ir.exists():
            shutil.copy2(ir, archive_to / "_ingest_report.json")
        if ref_out_path.exists():
            shutil.copy2(ref_out_path, archive_to / _QUALITY_REFERENCE_FOR_OWNER_FILE)
        for ref_json in reference_dir.glob("*.json"):
            if ref_json.name.startswith("_"):
                continue
            shutil.copy2(ref_json, ref_snapshot / ref_json.name)
        gate = reference_dir / _QUALITY_GATE_FILE
        if gate.exists():
            shutil.copy2(gate, archive_to / _QUALITY_GATE_FILE)
        owner_gaps = reference_dir / _OWNER_INPUT_GAPS_FILE
        if owner_gaps.exists():
            shutil.copy2(owner_gaps, archive_to / _OWNER_INPUT_GAPS_FILE)
        summ = {
            "reference_dir": str(reference_dir),
            "archive_to": str(archive_to),
            "ingest_report": result.get("ingest_report"),
            "procurement_export": result.get("procurement_export"),
            "reference_for_owner_path": result.get("reference_for_owner_path"),
            "quality_gate_path": result.get("quality_gate_path"),
            "owner_input_gaps_path": result.get("owner_input_gaps_path"),
        }
        (archive_to / "summary.json").write_text(
            json.dumps(summ, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[quality] archived -> %s", archive_to)

    logger.info("[quality] done")

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=None)
    parser.add_argument("--accepted", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument(
        "--archive-to",
        type=Path,
        default=None,
        help="Archive dir for copied quality and ingest JSON reports",
    )
    parser.add_argument(
        "--disable-llm-fallback",
        action="store_true",
        help="Deprecated: LLM fallback has been removed from quality stage",
    )
    args = parser.parse_args()

    _setup_logging()
    run_quality_pipeline(
        decisions_path=args.decisions,
        accepted_path=args.accepted,
        summary_path=args.summary,
        archive_to=args.archive_to,
    )


if __name__ == "__main__":
    asyncio.run(main())
