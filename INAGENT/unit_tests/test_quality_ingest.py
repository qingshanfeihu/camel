# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for IngestValidator and run_quality_pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.data_tools.ingest_validator import IngestValidator
from INAGENT.data_tools.quality_ingest import (
    _apply_condition_rules,
    _match_chunk_against_rule,
    run_quality_pipeline,
)
from INAGENT.utils.quality_test_output import prepare_run_dir


def _accept_chunk(page: str, **meta_extra: object) -> dict:
    meta = {
        "section_title": "CmdRef",
        "tree_position": {"tree_level": "leaf", "linked_nodes": []},
        "document_category": "cli/reference",
        "source_file": "fixture.json",
        "block_id": "block-1",
        **meta_extra,
    }
    return {"page_content": page, "metadata": meta}


# IV-01: 短文本不再由 ingest_validator 硬拦截
def test_iv01_short_content_not_rejected_by_default() -> None:
    v = IngestValidator(cli_graph_store=None)
    chunk = _accept_chunk("short")
    verdict, reason = v.validate_chunk(chunk)
    assert verdict == "accept"
    assert reason == ""
    assert v.report.rejected_short == 0


# IV-02: non-product-like section_title is no longer rejected by keyword rule
def test_iv02_no_keyword_reject_on_section_title() -> None:
    v = IngestValidator(cli_graph_store=None)
    chunk = _accept_chunk("y" * 30, section_title="版权声明")
    verdict, reason = v.validate_chunk(chunk)
    assert verdict == "accept"
    assert reason == ""


# IV-03: no tree_position and no migratable document_category -> quarantine
def test_iv03_quarantine_no_tree_position() -> None:
    v = IngestValidator(cli_graph_store=None)
    chunk = {
        "page_content": "z" * 30,
        "metadata": {"section_title": "ValidTitle"},
    }
    verdict, reason = v.validate_chunk(chunk)
    assert verdict == "quarantine"
    assert reason == "no_tree_position"
    assert v.report.quarantined >= 1


# IV-04: 近重复不再由 ingest_validator 硬拦截
def test_iv04_duplicate_simhash_not_filtered_by_default() -> None:
    v = IngestValidator(cli_graph_store=None)
    body = "duplicate body for simhash " * 5
    c1 = _accept_chunk(body)
    c2 = _accept_chunk(body)
    assert v.validate_chunk(c1)[0] == "accept"
    verdict, reason = v.validate_chunk(c2)
    assert verdict == "accept"
    assert reason == ""
    assert v.report.duplicates == 0


# IV-05: run_quality_pipeline writes _quality_report.json (ingest path)
def test_iv05_run_quality_pipeline_writes_report(tmp_path: Path) -> None:
    kb = [_accept_chunk("pipeline content " * 5)]
    (tmp_path / "knowledge_base.json").write_text(
        json.dumps(kb, ensure_ascii=False),
        encoding="utf-8",
    )
    with patch(
        "INAGENT.data_tools.quality_ingest.get_cli_graph_store",
        return_value=None,
    ):
        result = run_quality_pipeline(reference_dir=tmp_path)
    assert (tmp_path / "_quality_reference_for_owner.json").exists()
    assert (tmp_path / "_quality_report.json").exists()
    assert (tmp_path / "_ingest_report.json").exists()
    assert result.get("ingest_report") is not None
    ing = result["ingest_report"]
    assert isinstance(ing, dict)
    assert ing.get("accepted", 0) >= 1


def test_run_quality_pipeline_archive_to(tmp_path: Path) -> None:
    kb = [_accept_chunk("archive test body " * 5)]
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "knowledge_base.json").write_text(
        json.dumps(kb, ensure_ascii=False),
        encoding="utf-8",
    )
    arc = tmp_path / "archive"
    with patch(
        "INAGENT.data_tools.quality_ingest.get_cli_graph_store",
        return_value=None,
    ):
        run_quality_pipeline(reference_dir=ref, archive_to=arc)
    assert (arc / "_quality_report.json").exists()
    assert (arc / "_ingest_report.json").exists()
    assert (arc / "summary.json").exists()


def test_run_quality_pipeline_owner_rule_override_blocks_chunk(tmp_path: Path) -> None:
    kb = [
        _accept_chunk(
            "override target body " * 5,
            source_file="owner.json",
            block_id="b-1",
        )
    ]
    (tmp_path / "knowledge_base.json").write_text(
        json.dumps(kb, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "_owner_quality_overrides.jsonl").write_text(
        json.dumps(
            {
                "source_file": "owner.json",
                "block_id": "b-1",
                "is_product_knowledge": False,
                "quality_reason": "owner_rule_block",
                "rule_tag": "owner.product.rule",
                "rule_version": "2026-04-13",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with patch(
        "INAGENT.data_tools.quality_ingest.get_cli_graph_store",
        return_value=None,
    ):
        result = run_quality_pipeline(reference_dir=tmp_path)

    source_after = json.loads((tmp_path / "knowledge_base.json").read_text(encoding="utf-8"))
    assert len(source_after) == 1
    out_ref = json.loads(
        (tmp_path / "_quality_reference_for_owner.json").read_text(encoding="utf-8")
    )
    assert out_ref == []
    assert result.get("quality_gate_blocked") == 1
    assert result.get("owner_rule_hook", {}).get("overrides_total") == 1
    gate_lines = (tmp_path / "_quality_gate_for_owner.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert gate_lines
    gate_item = json.loads(gate_lines[0])
    assert gate_item.get("decision_source") == "owner_rule_hook"
    assert gate_item.get("quality_reason") == "owner_rule_block"


def test_prepare_run_dir_copies_reference(tmp_path: Path) -> None:
    ref = tmp_path / "src_ref"
    ref.mkdir()
    (ref / "chunk.json").write_text("[]", encoding="utf-8")
    out_base = tmp_path / "质检员输出"
    root = prepare_run_dir(
        "fixture_run",
        reference_dir=ref,
        out_base=out_base,
        copy_reference=True,
    )
    assert (root / "ingest" / "chunk.json").exists()
    assert (root / "procurement_contract").is_dir()


# ──────────────────────────────────────────────
# Condition-rule unit tests
# ──────────────────────────────────────────────

_BLOCK_RULE = {
    "rule_id": "test_block_copyright",
    "rule_version": "1.0",
    "enabled": True,
    "priority": 100,
    "match_conditions": {"section_title_regex": "版权|copyright"},
    "action": "block",
    "override": {"quality_reason": "owner_rule:copyright", "risk_level": "high"},
}

_PASS_RULE = {
    "rule_id": "test_pass_tech",
    "rule_version": "1.0",
    "enabled": True,
    "priority": 80,
    "match_conditions": {"section_title_regex": "^配置示例"},
    "action": "pass",
    "override": {"quality_reason": "owner_rule:pass_tech", "risk_level": "low"},
}

_DISABLED_RULE = {
    "rule_id": "test_disabled",
    "rule_version": "1.0",
    "enabled": False,
    "priority": 200,
    "match_conditions": {"section_title_regex": ".*"},
    "action": "block",
}


def test_match_chunk_against_rule_title_regex_match() -> None:
    chunk = _accept_chunk("x" * 30, section_title="版权声明")
    assert _match_chunk_against_rule(chunk, _BLOCK_RULE) is True


def test_match_chunk_against_rule_title_regex_no_match() -> None:
    chunk = _accept_chunk("x" * 30, section_title="配置示例")
    assert _match_chunk_against_rule(chunk, _BLOCK_RULE) is False


def test_match_chunk_against_rule_source_file_glob() -> None:
    rule = {
        "rule_id": "glob_test",
        "enabled": True,
        "match_conditions": {"source_file_glob": "app_*"},
        "action": "block",
    }
    chunk_match = _accept_chunk("x" * 30, source_file="app_1-40.json")
    chunk_no_match = _accept_chunk("x" * 30, source_file="cli_1-82.json")
    assert _match_chunk_against_rule(chunk_match, rule) is True
    assert _match_chunk_against_rule(chunk_no_match, rule) is False


def test_apply_condition_rules_block_action() -> None:
    chunk = _accept_chunk("x" * 30, section_title="版权声明")
    patch = _apply_condition_rules(chunk, [_BLOCK_RULE, _PASS_RULE])
    assert patch is not None
    assert patch["is_product_knowledge"] is False
    assert patch["rule_tag"] == "test_block_copyright"
    assert patch["risk_level"] == "high"


def test_apply_condition_rules_pass_action() -> None:
    chunk = _accept_chunk("x" * 30, section_title="配置示例 OSPF")
    patch = _apply_condition_rules(chunk, [_PASS_RULE])
    assert patch is not None
    assert patch["is_product_knowledge"] is True
    assert patch["rule_tag"] == "test_pass_tech"


def test_apply_condition_rules_disabled_rule_skipped() -> None:
    chunk = _accept_chunk("x" * 30, section_title="任意标题")
    patch = _apply_condition_rules(chunk, [_DISABLED_RULE])
    assert patch is None


def test_apply_condition_rules_no_match_returns_none() -> None:
    chunk = _accept_chunk("x" * 30, section_title="普通标题")
    patch = _apply_condition_rules(chunk, [_BLOCK_RULE])
    assert patch is None


def test_run_quality_pipeline_condition_rule_blocks_chunk(tmp_path: Path) -> None:
    kb = [_accept_chunk("content body " * 5, section_title="版权声明")]
    (tmp_path / "knowledge_base.json").write_text(
        json.dumps(kb, ensure_ascii=False), encoding="utf-8"
    )
    rules_data = {
        "schema_version": "1.0",
        "rules": [
            {
                "rule_id": "test_block_copyright",
                "rule_version": "1.0",
                "enabled": True,
                "priority": 100,
                "match_conditions": {"section_title_regex": "版权"},
                "action": "block",
                "override": {
                    "quality_reason": "owner_rule:copyright",
                    "risk_level": "high",
                },
            }
        ],
    }
    (tmp_path / "_owner_quality_rules.json").write_text(
        json.dumps(rules_data, ensure_ascii=False), encoding="utf-8"
    )

    with patch(
        "INAGENT.data_tools.quality_ingest.get_cli_graph_store",
        return_value=None,
    ):
        result = run_quality_pipeline(reference_dir=tmp_path)

    source_after = json.loads((tmp_path / "knowledge_base.json").read_text(encoding="utf-8"))
    assert len(source_after) == 1
    out_ref = json.loads(
        (tmp_path / "_quality_reference_for_owner.json").read_text(encoding="utf-8")
    )
    assert out_ref == []
    assert result.get("quality_gate_blocked") == 1
    assert result.get("owner_rule_hook", {}).get("condition_rules_total") == 1
    assert result.get("owner_rule_hook", {}).get("condition_rules_to_block") == 1
    gate_lines = (
        (tmp_path / "_quality_gate_for_owner.jsonl").read_text(encoding="utf-8").splitlines()
    )
    gate_item = json.loads(gate_lines[0])
    assert gate_item.get("decision_source") == "owner_condition_rule"
    assert gate_item.get("hook_rule_tag") == "test_block_copyright"
