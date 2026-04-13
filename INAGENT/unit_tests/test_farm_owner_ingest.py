# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for farm_owner_ingest stage wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline
from INAGENT.rag.knowledge_schema import FillRequest


class _FakeGraphRAGRetriever:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def is_available(self) -> bool:
        return True


class _FakeOwner:
    last_model = None

    def __init__(self, graphrag, model=None) -> None:
        self.graphrag = graphrag
        _FakeOwner.last_model = model

    def classify_uncovered_chunks(self, chunks, report):
        if not chunks:
            return []
        return [
            FillRequest(
                entity_title="CLI 概述",
                action="update",
                target_block_id="blk-1",
                chunk_meta_patch={
                    "tree_level": "branch",
                    "tree_position": {
                        "tree_level": "branch",
                        "linked_nodes": [],
                        "confidence": 0.7,
                        "knowledge_role": "structural",
                    },
                },
            )
        ]


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

    fake_model = object()
    with patch(
        "INAGENT.data_tools.farm_owner_ingest.GraphRAGRetriever",
        _FakeGraphRAGRetriever,
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest.KnowledgeFarmOwnerAgent",
        _FakeOwner,
    ), patch(
        "INAGENT.data_tools.farm_owner_ingest.get_llm_model",
        return_value=fake_model,
    ), patch(
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
    assert result["classified_blocks"] == 1
    assert _FakeOwner.last_model is fake_model

    owner_decisions = (ref / "_owner_decisions_for_farmer.jsonl").read_text(
        encoding="utf-8",
    ).strip().splitlines()
    assert len(owner_decisions) == 1

    row = json.loads(owner_decisions[0])
    assert row["action"] == "update"
    assert row["chunk_meta_patch"]["description"] == "由农场主阶段补充的 LLM 描述"
    assert row["chunk_meta_patch"]["command_prefix"] == "slb"
    assert row["chunk_meta_patch"]["tree_level"] == "branch"
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

    with patch(
        "INAGENT.data_tools.farm_owner_ingest.GraphRAGRetriever",
        _FakeGraphRAGRetriever,
    ):
        result = run_farm_owner_pipeline(reference_dir=ref)

    assert result["processed"] is False
    assert result["reason"] == "missing quality filtered reference"
    assert result["required_file"].endswith("_quality_reference_for_owner.json")