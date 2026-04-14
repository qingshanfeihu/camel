# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for farm_owner_ingest stage wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline


def test_run_farm_owner_pipeline_enhances_quality_passed_chunks(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    kb = [
        {
            "page_content": "slb virtual server 的配置说明",
            "metadata": {
                "source_file": "cli.json",
                "block_id": "blk-1",
                "section_title": "CLI 概述",
                "document_category": "cli/reference",
            },
        }
    ]
    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps(kb, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ref / "_quality_gate_for_owner.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_file": "cli.json",
                "block_id": "blk-1",
                "chunk_index": 0,
                "is_product_knowledge": True,
                "section_title": "CLI 概述",
                "document_category": "cli/reference",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={
            "cli.json::blk-1": {
                "description": "由农场主阶段补充的 LLM 描述",
                "command_prefix": "slb",
            }
        },
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["quality_passed_blocks"] == 1
    assert result["llm_enhanced_blocks"] == 1
    assert result["classified_blocks"] == 0

    owner_decisions = (ref / "_owner_decisions_for_farmer.jsonl").read_text(
        encoding="utf-8",
    ).strip().splitlines()
    assert len(owner_decisions) == 1

    row = json.loads(owner_decisions[0])
    assert row["action"] == "merge_into_existing"
    assert row["chunk_meta_patch"]["description"] == "由农场主阶段补充的 LLM 描述"
    assert row["chunk_meta_patch"]["command_prefix"] == "slb"
    assert row["source_evidence"] == "quality_gate_pass_with_farm_owner_enhancement"


def test_run_farm_owner_pipeline_requires_quality_reference_file(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    (ref / "_quality_gate_for_owner.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_file": "cli.json",
                "block_id": "blk-1",
                "chunk_index": 0,
                "is_product_knowledge": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is False
    assert result["reason"] == "missing quality filtered reference"
    assert result["required_file"].endswith("_quality_reference_for_owner.json")


def test_run_farm_owner_pipeline_requires_quality_gate_file(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "slb virtual server 的配置说明",
                    "metadata": {
                        "source_file": "cli.json",
                        "block_id": "blk-1",
                        "section_title": "CLI 概述",
                        "document_category": "cli/reference",
                    },
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is False
    assert result["reason"] == "missing quality gate file"
    assert result["required_file"].endswith("_quality_gate_for_owner.jsonl")


def test_run_farm_owner_pipeline_drops_quality_blocked_chunks(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "应该通过的块",
                    "metadata": {
                        "source_file": "cli.json",
                        "block_id": "blk-pass",
                        "section_title": "通过块",
                        "document_category": "cli/reference",
                    },
                },
                {
                    "page_content": "应该被门控拦截的块",
                    "metadata": {
                        "source_file": "cli.json",
                        "block_id": "blk-block",
                        "section_title": "拦截块",
                        "document_category": "cli/reference",
                    },
                },
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (ref / "_quality_gate_for_owner.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "source_file": "cli.json",
                        "block_id": "blk-pass",
                        "chunk_index": 0,
                        "is_product_knowledge": True,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "source_file": "cli.json",
                        "block_id": "blk-block",
                        "chunk_index": 1,
                        "is_product_knowledge": False,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["quality_passed_blocks"] == 1

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["target_block_id"] == "blk-pass"


def test_run_farm_owner_pipeline_writes_gap_decisions_for_farmer(tmp_path: Path) -> None:
    ref = tmp_path / "reference"
    ref.mkdir()

    (ref / "_quality_reference_for_owner.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "slb virtual server 的配置说明",
                    "metadata": {
                        "source_file": "cli.json",
                        "block_id": "blk-1",
                        "section_title": "CLI 概述",
                        "document_category": "cli/reference",
                    },
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (ref / "_quality_gate_for_owner.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_file": "cli.json",
                "block_id": "blk-1",
                "chunk_index": 0,
                "is_product_knowledge": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    (ref / "schema_gaps.jsonl").write_text(
        json.dumps(
            {
                "gap_type": "non_knowledge",
                "entity_title": "版权声明",
                "entity_description": "文档尾部版权段落",
                "source_file": "cli.json",
                "chunk_block_id": "blk-1",
                "evidence": "copyright section",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with patch(
        "INAGENT.data_tools.farm_owner_ingest._build_quality_passed_llm_patches",
        return_value={},
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is True
    assert result["gap_entries"] == 1
    assert result["fill_requests"] == 0
    assert result["deferred"] == 1
    assert result["delegated_gap_entries"] == 1
    assert result["delegated_to"] == "quality_inspector"

    rows = [
        json.loads(line)
        for line in (ref / "_owner_decisions_for_farmer.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
        if line.strip()
    ]
    assert len(rows) == 2

    deferred_rows = [r for r in rows if r["action"] == "needs_tree_session"]
    assert len(deferred_rows) == 1
    assert deferred_rows[0]["target_block_id"] == "blk-1"
    assert deferred_rows[0]["source_file"] == "cli.json"
    assert deferred_rows[0]["source_evidence"] == "quality_rule_candidate_for_inspector"

    proposals_file = ref / "_quality_rule_proposals.json"
    assert proposals_file.exists()
    proposals_doc = json.loads(proposals_file.read_text(encoding="utf-8"))
    assert proposals_doc["schema_version"] == "1.0"
    assert proposals_doc["proposal_count"] == 1
    assert proposals_doc["proposals"][0]["support_count"] == 1
    assert proposals_doc["proposals"][0]["suggested_rule"]["reason_code"] == "farm_owner_gap_non_knowledge"