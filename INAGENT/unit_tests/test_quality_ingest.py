# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for IngestValidator and run_quality_pipeline (IV-01 to IV-05)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.data_tools.ingest_validator import IngestValidator
from INAGENT.data_tools.quality_ingest import run_quality_pipeline
from INAGENT.utils.quality_test_output import prepare_run_dir


def _accept_chunk(page: str, **meta_extra: object) -> dict:
    meta = {
        "section_title": "CmdRef",
        "tree_position": {"tree_level": "leaf", "linked_nodes": []},
        "document_category": "cli/reference",
        **meta_extra,
    }
    return {"page_content": page, "metadata": meta}


# IV-01: MinLength / short content
def test_iv01_rejected_short() -> None:
    v = IngestValidator(cli_graph_store=None)
    chunk = _accept_chunk("short")
    verdict, reason = v.validate_chunk(chunk)
    assert verdict == "reject"
    assert reason == "short_content"
    assert v.report.rejected_short >= 1


# IV-02: non-product section_title (non_product_section_title_regex)
def test_iv02_non_product_section_title() -> None:
    v = IngestValidator(cli_graph_store=None)
    chunk = _accept_chunk("y" * 30, section_title="版权声明")
    verdict, reason = v.validate_chunk(chunk)
    assert verdict == "reject"
    assert reason == "non_product_content"
    assert v.report.rejected_excluded >= 1


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


# IV-04: SimHash near-duplicate
def test_iv04_duplicate_simhash() -> None:
    v = IngestValidator(cli_graph_store=None)
    body = "duplicate body for simhash " * 5
    c1 = _accept_chunk(body)
    c2 = _accept_chunk(body)
    assert v.validate_chunk(c1)[0] == "accept"
    verdict, reason = v.validate_chunk(c2)
    assert verdict == "duplicate"
    assert reason == "simhash_near_duplicate"
    assert v.report.duplicates >= 1


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
