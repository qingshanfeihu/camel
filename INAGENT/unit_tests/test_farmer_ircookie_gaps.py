# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""T1~T4: 农民 ircookie 断链修复单元测试。

T1 — _match_tree_node: 命中 knowledge_base.json 中 node_id
T2 — _detect_cross_refs: 检测跨命令引用
T3 — emit_schema_gaps: 检测 "系统命令覆盖" → supports_override gap
T4 — FillRequest 循环: farm owner → farmer apply_fill_request
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.agents.knowledge_farmer_agent import (
    FarmResult,
    KnowledgeFarmerAgent,
)
from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
from INAGENT.agents.knowledge_procurement_agent import (
    ChunkDecision,
    ProcurementDecision,
)
from INAGENT.rag.knowledge_schema import (
    FarmOwnerReport,
    FillRequest,
    SchemaGapEntry,
)
from INAGENT.rag.graphrag_integration import GraphRAGRetriever


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_decision(
    content: str,
    source_file: str = "cli.pdf",
    chunk_index: int = 0,
    command_prefix: str = "",
    document_category: str = "cli/reference",
) -> ChunkDecision:
    meta = {
        "source_file": source_file,
        "document_category": document_category,
    }
    if command_prefix:
        meta["command_prefix"] = command_prefix
    return ChunkDecision(
        chunk={"page_content": content, "metadata": meta},
        decision=ProcurementDecision(
            action="accept", target_kb="product", confidence=0.9, reason="test",
        ),
        source_file=source_file,
        chunk_index=chunk_index,
    )


def _make_farmer(kb_index=None) -> KnowledgeFarmerAgent:
    mock_chat = MagicMock()
    mock_resp = MagicMock()
    mock_msg = MagicMock()
    mock_msg.content = "[]"
    mock_resp.msgs = [mock_msg]
    mock_chat.step.return_value = mock_resp

    agent = KnowledgeFarmerAgent(model=MagicMock(), _chat_agent=mock_chat)
    agent._function_index = None
    agent._skeleton = {}
    agent._structure_via_auto_convert = lambda chunks: [{} for _ in chunks]
    if kb_index is not None:
        agent._kb_index = kb_index
        for cmd, node_id in kb_index.items():
            agent._skeleton[node_id] = {
                "page_content": f"[命令] {cmd}",
                "metadata": {
                    "node_id": node_id,
                    "command_prefix": cmd,
                    "document_category": "cli/reference",
                    "source_file": "cli_keyword_graph.json",
                },
            }
    return agent


CHUNK_A = (
    "slb mode ircookie <ircookie_mode> [group_name] [password]\n"
    "设置 ircookie 工作模式。\n"
    "参数说明:\n"
    "ircookie_mode — plainname | hexname | ip | enc_name | enc_ip\n"
    "group_name — 服务组名称（仅 enc_name/enc_ip 模式）\n"
    "password — 加密密码\n"
)

CHUNK_B = (
    "slb group method ic\n"
    "设置服务组的调度方式为智能 cookie（IC）模式。\n"
    "cookie的值由命令slb mode ircookie确定。\n"
)

CHUNK_C = (
    "系统命令覆盖功能列表\n"
    "以下命令可被系统命令覆盖：\n"
    "slb mode ircookie\n"
    "slb virtual http\n"
)


# ── T1: _match_tree_node ─────────────────────────────────────────────────────

class TestMatchTreeNode:
    def test_match_existing_node(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        meta = {"command_prefix": "slb mode ircookie"}
        assert farmer._match_tree_node("", meta) == "slb_mode_ircookie"

    def test_no_match_returns_none(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        meta = {"command_prefix": "slb virtual http"}
        assert farmer._match_tree_node("", meta) is None

    def test_no_prefix_returns_none(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        assert farmer._match_tree_node("", {}) is None

    def test_content_based_match(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        content = "slb mode ircookie <ircookie_mode> [group_name]"
        assert farmer._match_tree_node(content, {}) == "slb_mode_ircookie"

    def test_cultivate_sets_tree_node_id(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        cd = _make_decision(CHUNK_A, command_prefix="slb mode ircookie")
        results = farmer.cultivate_batch([cd])
        assert len(results) == 1
        assert results[0].chunk["metadata"]["tree_node_id"] == "slb_mode_ircookie"
        assert "tree_node_id" in results[0].enriched_fields


# ── T2: _detect_cross_refs ───────────────────────────────────────────────────

class TestDetectCrossRefs:
    def test_detects_ircookie_ref(self):
        farmer = _make_farmer()
        refs = farmer._detect_cross_refs(CHUNK_B, own_prefix="slb group method ic")
        assert "slb mode ircookie" in refs

    def test_excludes_own_prefix(self):
        farmer = _make_farmer()
        refs = farmer._detect_cross_refs(
            "配置由命令slb mode ircookie确定",
            own_prefix="slb mode ircookie",
        )
        assert "slb mode ircookie" not in refs

    def test_no_refs_returns_empty(self):
        farmer = _make_farmer()
        refs = farmer._detect_cross_refs("一段普通文字没有命令引用", own_prefix=None)
        assert refs == []

    def test_cultivate_sets_command_refs(self):
        farmer = _make_farmer(kb_index={})
        cd = _make_decision(CHUNK_B, command_prefix="slb group method ic")
        results = farmer.cultivate_batch([cd])
        meta = results[0].chunk["metadata"]
        assert "command_refs" in meta
        assert "slb mode ircookie" in meta["command_refs"]


# ── T3: emit_schema_gaps ─────────────────────────────────────────────────────

class TestEmitSchemaGaps:
    def test_detects_override_gap(self):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        gaps = farmer._detect_schema_gaps(CHUNK_C, {
            "tree_node_id": "slb_mode_ircookie",
            "source_file": "cli.pdf",
        })
        assert len(gaps) == 1
        g = gaps[0]
        assert g.gap_type == "new_entity_attribute"
        assert g.column_name == "supports_override"
        assert g.column_dtype == "bool"
        assert g.default_value is False
        assert g.entity_title == "slb_mode_ircookie"

    def test_no_override_no_gap(self):
        farmer = _make_farmer()
        gaps = farmer._detect_schema_gaps(CHUNK_A, {"source_file": "cli.pdf"})
        assert gaps == []

    def test_emit_writes_jsonl(self, tmp_path):
        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        cd = _make_decision(CHUNK_C, command_prefix="slb mode ircookie")
        results = farmer.cultivate_batch([cd])
        assert any(r.schema_gaps for r in results)

        gaps_file = tmp_path / "gaps" / "schema_gaps.jsonl"
        count = farmer.emit_schema_gaps(results, gaps_file)
        assert count >= 1
        assert gaps_file.exists()

        lines = gaps_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == count
        entry = json.loads(lines[0])
        assert entry["column_name"] == "supports_override"
        assert entry["column_dtype"] == "bool"


# ── T4: FillRequest cycle ────────────────────────────────────────────────────

class TestFillRequestCycle:
    def test_farm_owner_returns_fill_requests(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 0
        mock_graphrag.add_entity_columns.return_value = ["supports_override"]
        mock_graphrag.reembed_entities.return_value = 0

        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entries = [
            SchemaGapEntry(
                gap_type="new_entity_attribute",
                entity_title="slb_mode_ircookie",
                column_name="supports_override",
                column_dtype="bool",
                default_value=False,
                evidence="系统命令覆盖功能列表",
                source_file="cli.pdf",
            ),
        ]
        report = owner.process_gap_entries(entries)
        assert len(report.fill_requests) == 1
        fr = report.fill_requests[0]
        assert fr.entity_title == "slb_mode_ircookie"
        assert fr.fill_fields == {"supports_override": False}
        assert fr.action == "update"

    def test_apply_fill_request_updates_reference(self, tmp_path):
        ref_dir = tmp_path / "reference"
        ref_dir.mkdir()
        data = [
            {
                "page_content": "slb mode ircookie...",
                "metadata": {
                    "tree_node_id": "slb_mode_ircookie",
                    "source_file": "cli.pdf",
                },
            },
            {
                "page_content": "other stuff",
                "metadata": {
                    "tree_node_id": "slb_virtual_http",
                    "source_file": "cli.pdf",
                },
            },
        ]
        (ref_dir / "cli.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        farmer = _make_farmer()
        filled = farmer.apply_fill_request(
            [FillRequest(
                entity_title="slb_mode_ircookie",
                fill_fields={"supports_override": False},
            )],
            ref_dir=ref_dir,
            kb_path=tmp_path / "nonexistent_kb.json",
        )
        assert filled == 1

        updated = json.loads((ref_dir / "cli.json").read_text(encoding="utf-8"))
        assert updated[0]["metadata"]["supports_override"] is False


class TestGraphRAGWriteOps:
    def test_update_entity_fields_updates_case_insensitive_title(self, tmp_path):
        workspace = tmp_path / "graphrag_index"
        output_dir = workspace / "output"
        output_dir.mkdir(parents=True)
        entities_path = output_dir / "entities.parquet"
        pd.DataFrame([
            {
                "id": "1",
                "title": "slb_mode_ircookie",
                "type": "CONFIGURATION",
                "description": "old",
                "human_readable_id": 1,
            },
        ]).to_parquet(entities_path, index=False)

        retriever = GraphRAGRetriever(workspace_dir=workspace)
        updated = retriever.update_entity_fields(
            "SLB_MODE_IRCOOKIE",
            {"description": "new", "product_module": "SLB"},
        )

        assert updated is True
        saved = pd.read_parquet(entities_path)
        assert saved.loc[0, "description"] == "new"
        assert saved.loc[0, "product_module"] == "SLB"

    def test_add_relationships_dedupes_existing(self, tmp_path):
        workspace = tmp_path / "graphrag_index"
        output_dir = workspace / "output"
        output_dir.mkdir(parents=True)
        relationships_path = output_dir / "relationships.parquet"
        pd.DataFrame([
            {
                "id": "1",
                "human_readable_id": 1,
                "source": "slb_mode_ircookie",
                "target": "slb_mode_icookie",
                "type": "RELATED",
                "description": "existing",
                "text_unit_ids": ["cli.pdf"],
            },
        ]).to_parquet(relationships_path, index=False)

        retriever = GraphRAGRetriever(workspace_dir=workspace)
        added = retriever.add_relationships([
            {
                "source": "SLB_MODE_IRCOOKIE",
                "target": "slb_mode_icookie",
                "type": "related",
                "description": "duplicate",
            },
            {
                "source": "slb_mode_ircookie",
                "target": "slb_virtual_http",
                "type": "BELONGS_TO",
                "description": "new",
                "source_id": "cli.pdf",
            },
        ])

        assert added == 1
        saved = pd.read_parquet(relationships_path)
        assert len(saved) == 2
        assert set(saved["target"].tolist()) == {"slb_mode_icookie", "slb_virtual_http"}

    def test_replace_entity_embeddings_deletes_then_adds(self, tmp_path):
        workspace = tmp_path / "graphrag_index"
        output_dir = workspace / "output"
        output_dir.mkdir(parents=True)
        (output_dir / "lancedb").mkdir()
        entities_path = output_dir / "entities.parquet"
        pd.DataFrame([
            {
                "id": "entity-1",
                "title": "slb_mode_ircookie",
                "type": "CONFIGURATION",
                "description": "desc",
                "human_readable_id": 1,
            },
        ]).to_parquet(entities_path, index=False)

        delete_calls = []
        add_calls = []

        class _FakeTable:
            def delete(self, expr):
                delete_calls.append(expr)

            def add(self, rows):
                add_calls.append(rows)

        fake_db = MagicMock()
        fake_db.open_table.return_value = _FakeTable()
        fake_lancedb = types.SimpleNamespace(connect=lambda _: fake_db)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
        mock_resp.raise_for_status.return_value = None

        retriever = GraphRAGRetriever(workspace_dir=workspace)
        with patch.dict(sys.modules, {"lancedb": fake_lancedb}):
            with patch("requests.post", return_value=mock_resp):
                count = retriever.replace_entity_embeddings(["slb_mode_ircookie"])

        assert count == 1
        assert delete_calls == ["id = 'entity-1'"]
        assert add_calls[0][0]["id"] == "entity-1"
        assert add_calls[0][0]["title"] == "slb_mode_ircookie"


class TestConflictAndOverflowHandling:
    def _make_owner(self, graphrag=None, chat_content=None):
        graphrag = graphrag or MagicMock()
        graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        graphrag.reload.return_value = None
        if chat_content is None:
            return KnowledgeFarmOwnerAgent(graphrag)

        mock_chat = MagicMock()
        mock_resp = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = chat_content
        mock_resp.msgs = [mock_msg]
        mock_chat.step.return_value = mock_resp
        return KnowledgeFarmOwnerAgent(graphrag, _chat_agent=mock_chat)

    def test_conflict_accept_new_updates_entity_and_returns_fill_request(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1

        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "accept_new",
                "resolved_value": "SLB",
            }),
        )
        report = owner.process_gap_entries([
            SchemaGapEntry(
                gap_type="conflict",
                entity_title="slb_mode_ircookie",
                field_name="product_module",
                skeleton_value="no_slb_mode_ircookie",
                new_value="SLB",
                evidence="new evidence",
            ),
        ])

        mock_graphrag.update_entity_fields.assert_called_once_with(
            "slb_mode_ircookie",
            {"product_module": "SLB"},
        )
        mock_graphrag.replace_entity_embeddings.assert_called_once_with([
            "slb_mode_ircookie",
        ])
        assert report.conflicts_resolved == 1
        assert report.fill_requests[0].action == "update"
        assert report.fill_requests[0].resolved_value == {"product_module": "SLB"}

    def test_process_gaps_parses_conflict_jsonl(self, tmp_path):
        gaps_file = tmp_path / "schema_gaps.jsonl"
        gaps_file.write_text(
            json.dumps({
                "gap_type": "conflict",
                "entity_title": "slb_mode_ircookie",
                "field_name": "product_module",
                "skeleton_value": "old",
                "new_value": "new",
                "evidence": "evidence",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = tmp_path / "snap"
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1

        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "keep_skeleton",
                "resolved_value": "old",
            }),
        )
        report = owner.process_gaps(gaps_file)

        assert report.conflicts_resolved == 1
        assert report.fill_requests[0].fill_fields == {"product_module": "old"}

    def test_overflow_create_new_adds_relationship_and_returns_template(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 1
        mock_graphrag.add_relationships.return_value = 1
        mock_graphrag.reembed_entities.return_value = 1

        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "create_new",
                "new_entity": {
                    "title": "slb_mode_new_cookie",
                    "description": "new cookie mode",
                    "entity_type": "CONFIGURATION",
                },
            }),
        )
        report = owner.process_gap_entries([
            SchemaGapEntry(
                gap_type="overflow",
                entity_title="slb_mode_new_cookie",
                entity_description="new cookie mode",
                entity_type="CONFIGURATION",
                evidence="overflow evidence",
                source_file="cli.pdf",
                chunk_content="slb mode new-cookie ...",
                nearest_matches=[{"node_id": "slb_mode_ircookie", "similarity": 0.4}],
            ),
        ])

        mock_graphrag.upsert_entities.assert_called_once()
        mock_graphrag.add_relationships.assert_called_once()
        mock_graphrag.reembed_entities.assert_called_once_with(["slb_mode_new_cookie"])
        assert report.entities_added == 1
        assert report.overflows_handled == 1
        assert report.fill_requests[0].action == "create_slot"
        assert report.fill_requests[0].new_node_template["metadata"]["node_id"] == "slb_mode_new_cookie"

    def test_overflow_merge_into_returns_update_request(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")

        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "merge_into",
                "target_node_id": "slb_mode_ircookie",
            }),
        )
        report = owner.process_gap_entries([
            SchemaGapEntry(
                gap_type="overflow",
                entity_title="candidate_node",
                evidence="overflow evidence",
                chunk_content="candidate content",
                nearest_matches=[{"node_id": "slb_mode_ircookie", "similarity": 0.95}],
            ),
        ])

        assert report.overflows_handled == 1
        assert report.fill_requests[0].action == "update"
        assert report.fill_requests[0].target_node_id == "slb_mode_ircookie"
        assert report.fill_requests[0].fill_fields == {"overflow_content": "candidate content"}

    def test_should_trigger_rebuild(self):
        report = FarmOwnerReport(
            fill_requests=[
                FillRequest(entity_title="a", fill_fields={"x": True}),
            ],
        )
        filled_count = 1
        should_rebuild = filled_count >= len(report.fill_requests) > 0
        assert should_rebuild is True

    def test_no_rebuild_when_no_fills(self):
        report = FarmOwnerReport(fill_requests=[])
        should_rebuild = 0 >= len(report.fill_requests) > 0
        assert should_rebuild is False

    def test_full_cycle(self, tmp_path):
        ref_dir = tmp_path / "reference"
        ref_dir.mkdir()
        data = [{
            "page_content": "slb mode ircookie...",
            "metadata": {"tree_node_id": "slb_mode_ircookie", "source_file": "cli.pdf"},
        }]
        (ref_dir / "cli.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        farmer = _make_farmer(kb_index={"slb mode ircookie": "slb_mode_ircookie"})
        cd = _make_decision(CHUNK_C, command_prefix="slb mode ircookie")
        results = farmer.cultivate_batch([cd])

        gaps_file = tmp_path / "schema_gaps.jsonl"
        farmer.emit_schema_gaps(results, gaps_file)

        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = tmp_path / "snap"
        mock_graphrag.upsert_entities.return_value = 0
        mock_graphrag.add_entity_columns.return_value = ["supports_override"]
        mock_graphrag.reembed_entities.return_value = 0

        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        report = owner.process_gaps(gaps_file)
        assert len(report.fill_requests) == 1

        filled = farmer.apply_fill_request(
            report.fill_requests, ref_dir=ref_dir,
            kb_path=tmp_path / "nonexistent_kb.json",
        )
        assert filled == 1

        should_rebuild = filled >= len(report.fill_requests) > 0
        assert should_rebuild is True

        updated = json.loads((ref_dir / "cli.json").read_text(encoding="utf-8"))
        assert updated[0]["metadata"]["supports_override"] is False


# ── T5: FarmOwner edge-cases ─────────────────────────────────────────────────

class TestFarmOwnerEdgeCases:
    """构造 mock 数据，系统验证农场主各路径的实现正确性。"""

    def _make_owner(self, graphrag=None, chat_content=None):
        graphrag = graphrag or MagicMock()
        graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        graphrag.reload.return_value = None
        if chat_content is None:
            return KnowledgeFarmOwnerAgent(graphrag)
        mock_chat = MagicMock()
        mock_resp = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = chat_content
        mock_resp.msgs = [mock_msg]
        mock_chat.step.return_value = mock_resp
        return KnowledgeFarmOwnerAgent(graphrag, _chat_agent=mock_chat)

    # ── process_gaps 文件处理 ──────────────────────────────────────────────────

    def test_process_gaps_missing_file_returns_error(self, tmp_path):
        owner = self._make_owner()
        report = owner.process_gaps(tmp_path / "nonexistent.jsonl")
        assert len(report.errors) == 1
        assert "文件不存在" in report.errors[0]
        assert report.fill_requests == []

    def test_process_gaps_empty_file_returns_empty_report(self, tmp_path):
        gaps_file = tmp_path / "gaps.jsonl"
        gaps_file.write_text("", encoding="utf-8")
        owner = self._make_owner()
        report = owner.process_gaps(gaps_file)
        assert report.fill_requests == []
        assert report.errors == []

    def test_process_gaps_blank_lines_ignored(self, tmp_path):
        gaps_file = tmp_path / "gaps.jsonl"
        gaps_file.write_text("\n\n\n", encoding="utf-8")
        owner = self._make_owner()
        report = owner.process_gaps(gaps_file)
        assert report.fill_requests == []

    def test_process_gaps_filters_unknown_gap_type(self, tmp_path):
        gaps_file = tmp_path / "gaps.jsonl"
        gaps_file.write_text(
            json.dumps({"gap_type": "unknown_type", "entity_title": "X"}) + "\n",
            encoding="utf-8",
        )
        owner = self._make_owner()
        report = owner.process_gaps(gaps_file)
        assert report.fill_requests == []

    def test_process_gaps_invalid_json_line_raises(self, tmp_path):
        """当前实现 json.loads 无 try/except，遇非法行会抛 JSONDecodeError。
        这是一个已知 bug —— 此测试记录当前行为。"""
        gaps_file = tmp_path / "gaps.jsonl"
        gaps_file.write_text(
            json.dumps({"gap_type": "new_entity_attribute", "entity_title": "X",
                        "column_name": "col"}) + "\n"
            + "NOT VALID JSON\n",
            encoding="utf-8",
        )
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = tmp_path / "snap"
        owner = self._make_owner(graphrag=mock_graphrag)
        with pytest.raises(json.JSONDecodeError):
            owner.process_gaps(gaps_file)

    # ── snapshot_backup / reload 异常 ─────────────────────────────────────────

    def test_snapshot_backup_failure_returns_early(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.side_effect = RuntimeError("disk full")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity_attribute", entity_title="X", column_name="col"
        )
        report = owner.process_gap_entries([entry])
        assert any("snapshot_backup" in e for e in report.errors)
        assert report.fill_requests == []

    def test_reload_failure_still_returns_report(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.add_entity_columns.return_value = ["col"]
        mock_graphrag.reload.side_effect = RuntimeError("reload error")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity_attribute", entity_title="X", column_name="col"
        )
        report = owner.process_gap_entries([entry])
        assert any("reload" in e for e in report.errors)
        assert report.fill_requests == []

    # ── conflict 校验与写操作 ─────────────────────────────────────────────────

    def test_conflict_missing_entity_title_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="",
            field_name="product_module",
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        assert any("已跳过" in e for e in report.errors)
        assert report.conflicts_resolved == 0
        assert report.fill_requests == []

    def test_conflict_missing_field_name_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="",
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        assert any("已跳过" in e for e in report.errors)
        assert report.conflicts_resolved == 0

    def test_conflict_keep_skeleton_does_not_write(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({"decision": "keep_skeleton", "resolved_value": "old_val"}),
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value="old_val",
            new_value="new_val",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_not_called()
        mock_graphrag.replace_entity_embeddings.assert_not_called()
        assert report.conflicts_resolved == 1
        assert report.fill_requests[0].resolved_value == {"product_module": "old_val"}

    def test_conflict_merge_calls_update_entity_fields(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({"decision": "merge", "resolved_value": "merged_val"}),
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value="old",
            new_value="new",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_called_once_with(
            "slb_mode_ircookie", {"product_module": "merged_val"}
        )
        assert report.conflicts_resolved == 1

    def test_conflict_update_entity_fields_returns_false_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = False
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({"decision": "accept_new", "resolved_value": "SLB"}),
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="nonexistent_entity",
            field_name="product_module",
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        assert any("未命中" in e for e in report.errors)
        assert report.conflicts_resolved == 1  # 虽然未命中，仍计入已裁决

    def test_conflict_update_entity_fields_raises_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.side_effect = RuntimeError("disk error")
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({"decision": "accept_new", "resolved_value": "SLB"}),
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        assert any("conflict 更新失败" in e for e in report.errors)

    # ── fallback 决策规则 ─────────────────────────────────────────────────────

    def test_fallback_conflict_new_value_none_keeps_skeleton(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)  # 无 chat_agent 触发 fallback
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value="old_val",
            new_value=None,
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_not_called()
        assert report.fill_requests[0].resolved_value == {"product_module": "old_val"}

    def test_fallback_conflict_skeleton_none_accepts_new(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value=None,
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_called_once_with(
            "slb_mode_ircookie", {"product_module": "SLB"}
        )
        assert report.fill_requests[0].resolved_value == {"product_module": "SLB"}

    def test_fallback_conflict_both_equal_keeps_skeleton(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="E",
            field_name="f",
            skeleton_value="same",
            new_value="same",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_not_called()
        assert report.fill_requests[0].resolved_value == {"f": "same"}

    def test_fallback_overflow_high_similarity_merges(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="candidate",
            chunk_content="some content",
            nearest_matches=[{"node_id": "existing_node", "similarity": 0.95}],
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests[0].action == "update"
        assert report.fill_requests[0].target_node_id == "existing_node"
        assert report.overflows_handled == 1

    def test_fallback_overflow_low_similarity_with_title_creates_new(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 1
        mock_graphrag.add_relationships.return_value = 1
        mock_graphrag.reembed_entities.return_value = 1
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="new_entity",
            entity_description="desc",
            nearest_matches=[{"node_id": "existing", "similarity": 0.5}],
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests[0].action == "create_slot"
        mock_graphrag.upsert_entities.assert_called_once()

    def test_fallback_overflow_no_nearest_matches_with_title_creates_new(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 1
        mock_graphrag.add_relationships.return_value = 0
        mock_graphrag.reembed_entities.return_value = 1
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="brand_new_entity",
            entity_description="no matches at all",
            nearest_matches=[],
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests[0].action == "create_slot"

    def test_fallback_overflow_no_data_discards(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="",
            entity_description="",
            chunk_content="",
            nearest_matches=[],
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests[0].action == "discard"
        assert report.overflows_handled == 1  # discard 也计入 overflows_handled

    # ── LLM 响应解析 ─────────────────────────────────────────────────────────

    def test_llm_returns_invalid_json_falls_back_to_rules(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content="Sorry, I cannot help with that.",
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value=None,
            new_value="SLB",
        )
        report = owner.process_gap_entries([entry])
        # fallback: skeleton None → accept_new → 应调用 update
        mock_graphrag.update_entity_fields.assert_called_once()
        assert report.conflicts_resolved == 1

    def test_llm_returns_code_fence_json_parsed(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        fence_content = (
            '```json\n'
            '{"decision": "accept_new", "resolved_value": "fenced_val"}\n'
            '```'
        )
        owner = self._make_owner(graphrag=mock_graphrag, chat_content=fence_content)
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value="old",
            new_value="fenced_val",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_called_once_with(
            "slb_mode_ircookie", {"product_module": "fenced_val"}
        )

    def test_llm_returns_dict_resolved_value_with_field_name_key(self):
        """LLM 返回 resolved_value 为 dict 且含 field_name key，应提取对应值。"""
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "accept_new",
                "resolved_value": {"product_module": "SLB_FROM_DICT"},
            }),
        )
        entry = SchemaGapEntry(
            gap_type="conflict",
            entity_title="slb_mode_ircookie",
            field_name="product_module",
            skeleton_value="old",
            new_value="SLB_FROM_DICT",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.update_entity_fields.assert_called_once_with(
            "slb_mode_ircookie", {"product_module": "SLB_FROM_DICT"}
        )

    # ── new_entity / attribute 错误路径 ──────────────────────────────────────

    def test_upsert_entities_raises_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.side_effect = RuntimeError("db error")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity",
            entity_title="new_entity",
            entity_description="desc",
            entity_type="CONFIGURATION",
        )
        report = owner.process_gap_entries([entry])
        assert any("upsert_entities" in e for e in report.errors)
        assert report.entities_added == 0

    def test_new_entity_reembed_not_called_when_zero_added(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 0  # 已存在，无新增
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity",
            entity_title="existing_entity",
            entity_description="desc",
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.reembed_entities.assert_not_called()

    def test_add_entity_columns_raises_adds_error(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.add_entity_columns.side_effect = RuntimeError("columns error")
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity_attribute",
            entity_title="X",
            column_name="col1",
        )
        report = owner.process_gap_entries([entry])
        assert any("add_entity_columns" in e for e in report.errors)

    def test_attribute_entry_without_column_name_produces_no_fill_request(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.add_entity_columns.return_value = []
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity_attribute",
            entity_title="X",
            column_name="",  # 无 column_name
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests == []

    def test_attribute_without_default_value_no_fill_request_still_adds_columns(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.add_entity_columns.return_value = ["col1"]
        owner = self._make_owner(graphrag=mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="new_entity_attribute",
            entity_title="N",
            column_name="col1",
            column_dtype="str",
        )
        report = owner.process_gap_entries([entry])
        assert report.fill_requests == []
        mock_graphrag.add_entity_columns.assert_called_once()

    def test_overflow_denylist_discards_without_graph_write(self, monkeypatch):
        monkeypatch.setattr(
            "INAGENT.agents.knowledge_farm_owner_agent._load_overflow_field_denylist",
            lambda: frozenset({"secret_meta"}),
        )
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="node1",
            field_name="secret_meta",
            new_value="x",
            evidence="e",
        )
        report = owner.process_gap_entries([entry])
        assert len(report.fill_requests) == 1
        assert report.fill_requests[0].action == "discard"
        assert report.overflows_handled == 1
        mock_graphrag.upsert_entities.assert_not_called()

    # ── 混合 gap 类型 + overflow create_new 无 parent_node ───────────────────

    def test_mixed_gap_types_all_processed(self):
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 1
        mock_graphrag.add_entity_columns.return_value = ["col1"]
        mock_graphrag.reembed_entities.return_value = 1
        mock_graphrag.update_entity_fields.return_value = True
        mock_graphrag.replace_entity_embeddings.return_value = 1
        owner = KnowledgeFarmOwnerAgent(mock_graphrag)
        entries = [
            SchemaGapEntry(
                gap_type="new_entity",
                entity_title="E1",
                entity_description="desc",
            ),
            SchemaGapEntry(
                gap_type="new_entity_attribute",
                entity_title="E2",
                column_name="col1",
                column_dtype="str",
                default_value="filled",
            ),
            SchemaGapEntry(
                gap_type="conflict",
                entity_title="E3",
                field_name="field1",
                skeleton_value=None,
                new_value="SLB",
            ),
        ]
        report = owner.process_gap_entries(entries)
        assert report.entities_added == 1
        assert "col1" in report.columns_added
        assert report.conflicts_resolved == 1
        assert len(report.fill_requests) == 2  # new_entity 不产生 fill_request，只有 attribute+conflict

    def test_overflow_create_new_no_parent_skips_add_relationships(self):
        """overflow create_new 当无 nearest_matches 时不应调用 add_relationships。"""
        mock_graphrag = MagicMock()
        mock_graphrag.snapshot_backup.return_value = Path("/tmp/snap")
        mock_graphrag.upsert_entities.return_value = 1
        mock_graphrag.reembed_entities.return_value = 1
        owner = self._make_owner(
            graphrag=mock_graphrag,
            chat_content=json.dumps({
                "decision": "create_new",
                "new_entity": {
                    "title": "isolated_entity",
                    "description": "no parent",
                    "entity_type": "CONFIGURATION",
                },
            }),
        )
        entry = SchemaGapEntry(
            gap_type="overflow",
            entity_title="isolated_entity",
            entity_description="no parent",
            nearest_matches=[],  # 无父节点
        )
        report = owner.process_gap_entries([entry])
        mock_graphrag.add_relationships.assert_not_called()
        assert report.fill_requests[0].action == "create_slot"
