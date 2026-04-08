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
"""Unit tests for KnowledgeFarmerAgent."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.agents.knowledge_farmer_agent import (
    FarmResult,
    KnowledgeFarmerAgent,
    _longest_prefix_match_node_id,
    _normalize_heading_to_node_slug,
)
from INAGENT.agents.knowledge_procurement_agent import (
    ChunkDecision,
    ProcurementDecision,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_decision(
    content: str,
    source_file: str = "cli.pdf",
    chunk_index: int = 0,
    section_title: str = "测试章节",
    document_category: str = "cli/reference",
    action: str = "accept",
    command_prefix: str = "",
) -> ChunkDecision:
    meta = {
        "source_file": source_file,
        "section_title": section_title,
        "document_category": document_category,
    }
    if command_prefix:
        meta["command_prefix"] = command_prefix
    return ChunkDecision(
        chunk={
            "page_content": content,
            "metadata": meta,
        },
        decision=ProcurementDecision(
            action=action,
            target_kb="product",
            confidence=0.9,
            reason="test",
        ),
        source_file=source_file,
        chunk_index=chunk_index,
    )


def _make_agent(ac_metas=None) -> KnowledgeFarmerAgent:
    """Build a KnowledgeFarmerAgent with mocked auto_convert."""
    agent = KnowledgeFarmerAgent(
        model=MagicMock(),
        _chat_agent=MagicMock(),
    )
    agent._function_index = None
    agent._skeleton = {}
    if ac_metas is not None:
        agent._structure_via_auto_convert = lambda chunks: ac_metas
    else:
        agent._structure_via_auto_convert = lambda chunks: [{} for _ in chunks]
    return agent


# ── TestStep1Rules ────────────────────────────────────────────────────────────

class TestStep1Rules:
    def test_block_id_format(self):
        agent = _make_agent()
        chunk = {
            "page_content": "slb virtual http test 1.2.3.4 80",
            "metadata": {"source_file": "cli.pdf"},
        }
        fields = agent._step1_rules(chunk, chunk_index=5)
        bid = fields["block_id"]
        assert bid.startswith("cli_5_")
        assert len(bid.split("_")) == 3
        assert len(bid.split("_")[2]) == 8

    def test_block_id_is_stable(self):
        agent = _make_agent()
        chunk = {
            "page_content": "show ip statistics - 显示 IP 统计信息。",
            "metadata": {"source_file": "manual.pdf"},
        }
        f1 = agent._step1_rules(chunk, chunk_index=0)
        f2 = agent._step1_rules(chunk, chunk_index=0)
        assert f1["block_id"] == f2["block_id"]

    def test_block_id_differs_by_index(self):
        agent = _make_agent()
        chunk = {
            "page_content": "show ip statistics - 显示 IP 统计信息。",
            "metadata": {"source_file": "manual.pdf"},
        }
        f0 = agent._step1_rules(chunk, chunk_index=0)
        f1 = agent._step1_rules(chunk, chunk_index=1)
        assert f0["block_id"] != f1["block_id"]

    def test_word_count(self):
        agent = _make_agent()
        chunk = {
            "page_content": "one two three four five",
            "metadata": {"source_file": "x.pdf"},
        }
        fields = agent._step1_rules(chunk, 0)
        assert fields["word_count"] == 5

    def test_has_code_block_detected(self):
        agent = _make_agent()
        chunk = {
            "page_content": "slb virtual http name 1.2.3.4 80\n配置虚拟服务。",
            "metadata": {"source_file": "cli.pdf"},
        }
        fields = agent._step1_rules(chunk, 0)
        assert fields.get("has_code_block") is True

    def test_has_code_block_absent_for_plain_text(self):
        agent = _make_agent()
        chunk = {
            "page_content": "这是一段没有任何 CLI 命令的纯中文描述性文字，不含代码。",
            "metadata": {"source_file": "spec.pdf"},
        }
        fields = agent._step1_rules(chunk, 0)
        assert "has_code_block" not in fields

    def test_command_prefix_not_in_step1(self):
        """command_prefix 不在 _step1_rules 提取，由树匹配阶段确定。"""
        agent = _make_agent()
        chunk = {
            "page_content": "slb virtual http test 1.2.3.4 80",
            "metadata": {"source_file": "cli.pdf"},
        }
        fields = agent._step1_rules(chunk, 0)
        assert "command_prefix" not in fields

    def test_no_command_prefix_for_plain_text(self):
        agent = _make_agent()
        with patch(
            "INAGENT.agents.knowledge_farmer_agent._get_metadata_rules",
            return_value={"command_prefixes": {"slb": ["slb "]}, "intents": {}, "config_modes": {}, "product_modules": {}, "protocol_types": {}},
        ):
            chunk = {
                "page_content": "这段内容没有任何命令。",
                "metadata": {"source_file": "cli.pdf"},
            }
            fields = agent._step1_rules(chunk, 0)
        assert "command_prefix" not in fields


# ── TestAutoConvertIntegration ────────────────────────────────────────────────

class TestAutoConvertIntegration:
    def test_fields_applied_from_auto_convert(self):
        ac_metas = [{
            "product_module": "SLB",
            "protocol_type": ["HTTP"],
            "intent": "manage_server_group",
            "config_mode": "cli",
            "description": "Configure HTTP virtual service",
        }]
        agent = _make_agent(ac_metas=ac_metas)
        cd = _make_decision(
            "slb virtual http svc 10.0.0.1 80\n配置 HTTP 虚拟服务示例内容。"
        )
        results = agent.cultivate_batch([cd])
        assert len(results) == 1
        meta = results[0].chunk["metadata"]
        assert meta["product_module"] == "SLB"
        assert meta["protocol_type"] == ["HTTP"]
        assert meta["config_mode"] == "cli"
        assert meta["description"] == "Configure HTTP virtual service"
        assert "product_module" in results[0].enriched_fields

    def test_existing_field_not_overwritten(self):
        ac_metas = [{"product_module": "LLB"}]
        agent = _make_agent(ac_metas=ac_metas)
        cd = _make_decision("show ip statistics - 显示统计信息。完整内容描述。")
        cd.chunk["metadata"]["product_module"] = "SLB"
        results = agent.cultivate_batch([cd])
        assert results[0].chunk["metadata"]["product_module"] == "SLB"

    def test_auto_convert_failure_raises(self):
        agent = _make_agent()
        agent._structure_via_auto_convert = MagicMock(
            side_effect=RuntimeError("network error")
        )
        cd = _make_decision("show ip statistics - 显示统计信息。完整内容描述。")
        with pytest.raises(RuntimeError):
            agent.cultivate_batch([cd])

    def test_non_accept_decisions_skipped(self):
        agent = _make_agent()
        reject_cd = _make_decision(
            "目录 版权声明 商标声明", action="reject"
        )
        accept_cd = _make_decision(
            "slb virtual http svc 10.0.0.1 80\n配置 HTTP 虚拟服务。"
        )
        results = agent.cultivate_batch([reject_cd, accept_cd])
        assert len(results) == 1
        assert results[0].source_file == "cli.pdf"

    def test_unknown_module_not_applied(self):
        ac_metas = [{"product_module": "unknown"}]
        agent = _make_agent(ac_metas=ac_metas)
        cd = _make_decision("show ip statistics - 显示统计信息内容完整描述。")
        results = agent.cultivate_batch([cd])
        assert "product_module" not in results[0].chunk["metadata"]


# ── TestStep3Index ────────────────────────────────────────────────────────────

class TestStep3Index:
    def test_scenario_id_filled_from_index(self):
        agent = _make_agent()
        agent._function_index = {
            "scenarios": {
                "slb_http": {
                    "product_modules": ["SLB"],
                    "protocol_types": ["HTTP"],
                }
            }
        }
        meta = {"product_module": "SLB", "protocol_type": ["HTTP"], "section_title": "SLB 配置"}
        fields = agent._step3_index(meta)
        assert fields.get("scenario_id") == "slb_http"

    def test_no_scenario_when_index_missing(self):
        agent = _make_agent()
        agent._function_index = None
        meta = {"product_module": "SLB"}
        fields = agent._step3_index(meta)
        assert fields == {}

    def test_existing_scenario_id_not_overwritten(self):
        agent = _make_agent()
        agent._function_index = {
            "scenarios": {
                "slb_http": {"product_modules": ["SLB"], "protocol_types": []}
            }
        }
        meta = {"product_module": "SLB", "scenario_id": "existing_id"}
        fields = agent._step3_index(meta)
        assert "scenario_id" not in fields


# ── TestWriteToReference ──────────────────────────────────────────────────────

class TestWriteToReference:
    def test_creates_new_file(self, tmp_path):
        agent = _make_agent()
        result = FarmResult(
            chunk={"page_content": "内容", "metadata": {"source_file": "cli.pdf", "block_id": "cli_0_aabbccdd"}},
            source_file="cli.pdf",
            block_id="cli_0_aabbccdd",
        )
        counts = agent.write_to_reference([result], ref_dir=tmp_path, log_dir=tmp_path)
        assert counts["cli"] == 1
        out = tmp_path / "cli.json"
        assert out.exists()
        data = json.loads(out.read_text("utf-8"))
        assert len(data) == 1
        assert data[0]["metadata"]["block_id"] == "cli_0_aabbccdd"

    def test_dedup_on_existing_block_id(self, tmp_path):
        agent = _make_agent()
        existing = [{"page_content": "old", "metadata": {"block_id": "cli_0_aabbccdd"}}]
        (tmp_path / "cli.json").write_text(json.dumps(existing), "utf-8")

        result = FarmResult(
            chunk={"page_content": "new", "metadata": {"source_file": "cli.pdf", "block_id": "cli_0_aabbccdd"}},
            source_file="cli.pdf",
            block_id="cli_0_aabbccdd",
        )
        counts = agent.write_to_reference([result], ref_dir=tmp_path)
        assert counts["cli"] == 0
        data = json.loads((tmp_path / "cli.json").read_text("utf-8"))
        assert len(data) == 1

    def test_new_block_appended(self, tmp_path):
        agent = _make_agent()
        existing = [{"page_content": "old", "metadata": {"block_id": "cli_0_aaaaaaaa"}}]
        (tmp_path / "cli.json").write_text(json.dumps(existing), "utf-8")

        result = FarmResult(
            chunk={"page_content": "new", "metadata": {"source_file": "cli.pdf", "block_id": "cli_1_bbbbbbbb"}},
            source_file="cli.pdf",
            block_id="cli_1_bbbbbbbb",
        )
        counts = agent.write_to_reference([result], ref_dir=tmp_path, log_dir=tmp_path)
        assert counts["cli"] == 1
        data = json.loads((tmp_path / "cli.json").read_text("utf-8"))
        assert len(data) == 2

    def test_cache_prevents_duplicate_run(self, tmp_path):
        from datetime import datetime

        agent = _make_agent()
        cache = {"cli_0_aabbccdd": datetime.now().isoformat()}
        (tmp_path / "cli.farmer_cache.json").write_text(json.dumps(cache), "utf-8")
        (tmp_path / "cli.json").write_text(json.dumps([
            {"page_content": "x", "metadata": {"source_file": "cli.pdf", "block_id": "cli_0_aabbccdd"}}
        ]), "utf-8")

        result = FarmResult(
            chunk={"page_content": "x", "metadata": {"source_file": "cli.pdf", "block_id": "cli_0_aabbccdd"}},
            source_file="cli.pdf",
            block_id="cli_0_aabbccdd",
        )
        counts = agent.write_to_reference([result], ref_dir=tmp_path, log_dir=tmp_path)
        assert counts["cli"] == 0

    def test_stale_cache_cleared_when_ref_missing(self, tmp_path):
        from datetime import datetime

        agent = _make_agent()
        cache = {"cli_0_aabbccdd": datetime.now().isoformat()}
        (tmp_path / "cli.farmer_cache.json").write_text(json.dumps(cache), "utf-8")

        result = FarmResult(
            chunk={"page_content": "x", "metadata": {"source_file": "cli.pdf", "block_id": "cli_0_aabbccdd"}},
            source_file="cli.pdf",
            block_id="cli_0_aabbccdd",
        )
        counts = agent.write_to_reference([result], ref_dir=tmp_path, log_dir=tmp_path)
        assert counts["cli"] == 1
        assert not (tmp_path / "cli.farmer_cache.json.stale").exists()

    def test_write_to_reference_uses_log_dir(self, tmp_path):
        ref_dir = tmp_path / "ref"
        log_dir = tmp_path / "logs"
        ref_dir.mkdir()
        log_dir.mkdir()

        agent = KnowledgeFarmerAgent.__new__(KnowledgeFarmerAgent)
        agent._chat_agent = MagicMock()
        agent._function_index = None

        result = FarmResult(
            chunk={"page_content": "内容", "metadata": {"source_file": "doc.pdf", "block_id": "doc_0_12345678"}},
            source_file="doc.pdf",
            block_id="doc_0_12345678",
        )

        # Patch _KB_LOGS_DIR so the agent writes cache to log_dir
        import INAGENT.agents.knowledge_farmer_agent as mod
        original = mod._KB_LOGS_DIR
        mod._KB_LOGS_DIR = log_dir
        try:
            agent.write_to_reference([result], ref_dir=ref_dir)
        finally:
            mod._KB_LOGS_DIR = original

        assert (log_dir / "doc.farmer_cache.json").exists()


# ── TestTreeMatchHeuristics ───────────────────────────────────────────────────

class TestTreeMatchHeuristics:
    def test_normalize_heading_to_node_slug(self):
        assert _normalize_heading_to_node_slug("SLB Mode Ircookie") == "slb_mode_ircookie"
        assert _normalize_heading_to_node_slug("") == ""

    def test_longest_prefix_prefers_longest_key(self):
        idx = {"slb": "n0", "slb mode ircookie": "n1"}
        assert _longest_prefix_match_node_id(idx, "slb mode ircookie x") == "n1"
        assert _longest_prefix_match_node_id(idx, "zzz") is None

    def test_match_tree_node_meta_tree_node_id(self):
        sk = {
            "leaf_a": {
                "metadata": {"node_id": "leaf_a", "command_prefix": "a b"},
                "page_content": "",
            },
        }
        agent = _make_agent()
        agent._skeleton = sk
        agent._kb_index = {"a b": "leaf_a"}
        assert agent._match_tree_node("", {"tree_node_id": "leaf_a"}, {}) == "leaf_a"

    def test_match_section_title_slug(self):
        sk = {
            "slb_mode_ircookie": {
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "command_prefix": "slb mode ircookie",
                },
                "page_content": "",
            },
        }
        agent = _make_agent()
        agent._skeleton = sk
        agent._kb_index = {"slb mode ircookie": "slb_mode_ircookie"}
        assert (
            agent._match_tree_node("", {"section_title": "SLB Mode Ircookie"}, {})
            == "slb_mode_ircookie"
        )

    def test_match_ac_meta_section_title(self):
        sk = {
            "slb_mode_ircookie": {
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "command_prefix": "slb mode ircookie",
                },
                "page_content": "",
            },
        }
        agent = _make_agent()
        agent._skeleton = sk
        agent._kb_index = {"slb mode ircookie": "slb_mode_ircookie"}
        assert (
            agent._match_tree_node(
                "",
                {},
                {"section_title": "slb mode ircookie"},
            )
            == "slb_mode_ircookie"
        )

    def test_match_chinese_leadin_english_cli_line(self):
        agent = _make_agent()
        agent._skeleton = {
            "slb_mode_ircookie": {
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "command_prefix": "slb mode ircookie",
                },
                "page_content": "",
            },
        }
        agent._kb_index = {"slb mode ircookie": "slb_mode_ircookie"}
        content = "该命令用于配置。\nslb mode ircookie <ircookie_mode> [g]\n更多说明"
        assert agent._match_tree_node(content, {}, {}) == "slb_mode_ircookie"

    def test_refine_ac_meta_command_prefix_upgrades_coarse_label(self):
        agent = _make_agent()
        agent._kb_index = {"slb mode ircookie": "slb_mode_ircookie"}
        ac = {"command_prefix": "slb"}
        agent._refine_ac_meta_command_prefix(
            "slb mode ircookie <ircookie_mode> [g]",
            ac,
        )
        assert ac["command_prefix"] == "slb mode ircookie"

    def test_farmer_tree_alias_resolves_section_title(self, tmp_path):
        alias_path = tmp_path / "farmer_tree_alias.json"
        alias_path.write_text(
            json.dumps({"pdf_heading_slug": "slb_mode_ircookie"}),
            encoding="utf-8",
        )
        sk = {
            "slb_mode_ircookie": {
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "command_prefix": "slb mode ircookie",
                },
                "page_content": "",
            },
        }
        with patch(
            "INAGENT.agents.knowledge_farmer_agent._FARMER_TREE_ALIAS_PATH",
            alias_path,
        ):
            agent = _make_agent()
            agent._skeleton = sk
            agent._kb_index = {}
            meta = {"section_title": "PDF Heading Slug"}
            assert agent._match_tree_node("", meta, {}) == "slb_mode_ircookie"


# ── TestSkeletonDiff ──────────────────────────────────────────────────────────

class TestSkeletonDiff:
    def test_matched_node_enriches_description(self):
        skeleton = {
            "slb_mode_ircookie": {
                "page_content": "[命令] slb mode ircookie\n[说明] Set ircookie mode\n语法: slb mode ircookie <mode>",
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "command_prefix": "slb mode ircookie",
                    "product_module": "SLB",
                    "document_category": "cli/reference",
                    "source_file": "cli_keyword_graph.json",
                },
            }
        }
        ac_metas = [{"description": "设置 ircookie 工作模式", "config_mode": "cli"}]
        agent = _make_agent(ac_metas=ac_metas)
        agent._skeleton = skeleton
        agent._kb_index = {"slb mode ircookie": "slb_mode_ircookie"}

        cd = _make_decision(
            "slb mode ircookie <mode>\n设置 ircookie 工作模式。",
            command_prefix="slb mode ircookie",
        )
        results = agent.cultivate_batch([cd])
        assert len(results) == 1
        assert results[0].matched_node_id == "slb_mode_ircookie"
        assert results[0].chunk["metadata"].get("_enriched_description") == "设置 ircookie 工作模式"

    def test_conflict_gap_on_field_mismatch(self):
        skeleton = {
            "test_node": {
                "page_content": "[命令] test cmd",
                "metadata": {
                    "node_id": "test_node",
                    "command_prefix": "test cmd",
                    "scope": "global",
                    "document_category": "cli/reference",
                    "source_file": "g.json",
                },
            }
        }
        ac_metas = [{"scope": "system"}]
        agent = _make_agent(ac_metas=ac_metas)
        agent._skeleton = skeleton
        agent._kb_index = {"test cmd": "test_node"}

        cd = _make_decision("test cmd some-args", command_prefix="test cmd")
        results = agent.cultivate_batch([cd])
        conflict_gaps = [g for g in results[0].schema_gaps if g.gap_type == "conflict"]
        assert len(conflict_gaps) == 1
        assert conflict_gaps[0].field_name == "scope"
        assert conflict_gaps[0].skeleton_value == "global"
        assert conflict_gaps[0].new_value == "system"

    def test_overflow_gap_for_extra_fields(self):
        skeleton = {
            "test_node": {
                "page_content": "[命令] test cmd",
                "metadata": {
                    "node_id": "test_node",
                    "command_prefix": "test cmd",
                    "document_category": "cli/reference",
                    "source_file": "g.json",
                },
            }
        }
        ac_metas = [{"scenario_id": "slb_http"}]
        agent = _make_agent(ac_metas=ac_metas)
        agent._skeleton = skeleton
        agent._kb_index = {"test cmd": "test_node"}

        cd = _make_decision("test cmd args", command_prefix="test cmd")
        results = agent.cultivate_batch([cd])
        overflow_gaps = [g for g in results[0].schema_gaps if g.gap_type == "overflow"]
        assert any(g.field_name == "scenario_id" for g in overflow_gaps)

    def test_unmatched_chunk_gets_overflow(self):
        agent = _make_agent(ac_metas=[{"command_prefix": "new cmd", "description": "new"}])
        agent._skeleton = {}
        agent._kb_index = {}

        cd = _make_decision("new cmd args", command_prefix="new cmd")
        results = agent.cultivate_batch([cd])
        overflow = [g for g in results[0].schema_gaps if g.gap_type == "overflow"]
        assert len(overflow) >= 1
        assert overflow[0].entity_title == "new cmd"

    def test_product_module_case_canonicalized_no_conflict(self):
        skeleton = {
            "t1": {
                "page_content": "",
                "metadata": {
                    "node_id": "t1",
                    "command_prefix": "test cmd",
                    "product_module": "slb",
                    "document_category": "cli/reference",
                    "source_file": "g.json",
                },
            },
        }
        ac_metas = [{"product_module": "SLB", "description": "desc"}]
        agent = _make_agent(ac_metas=ac_metas)
        agent._skeleton = skeleton
        agent._kb_index = {"test cmd": "t1"}
        cd = _make_decision("test cmd args", command_prefix="test cmd")
        results = agent.cultivate_batch([cd])
        assert results[0].chunk["metadata"]["product_module"] == "slb"
        conflicts = [g for g in results[0].schema_gaps if g.gap_type == "conflict"]
        assert not any(g.field_name == "product_module" for g in conflicts)

    def test_product_module_semantic_mismatch_is_conflict(self):
        skeleton = {
            "t1": {
                "page_content": "",
                "metadata": {
                    "node_id": "t1",
                    "command_prefix": "test cmd",
                    "product_module": "slb",
                    "document_category": "cli/reference",
                    "source_file": "g.json",
                },
            },
        }
        ac_metas = [{"product_module": "health", "description": "d"}]
        agent = _make_agent(ac_metas=ac_metas)
        agent._skeleton = skeleton
        agent._kb_index = {"test cmd": "t1"}
        cd = _make_decision("test cmd args", command_prefix="test cmd")
        results = agent.cultivate_batch([cd])
        conflicts = [g for g in results[0].schema_gaps if g.gap_type == "conflict"]
        assert any(g.field_name == "product_module" for g in conflicts)


# ── TestUpdateSkeleton ────────────────────────────────────────────────────────

class TestUpdateSkeleton:
    def test_update_skeleton_enriches_description(self, tmp_path):
        kb_data = [{
            "page_content": "[命令] slb mode ircookie\n[说明] Set mode\n语法: slb mode ircookie <m>",
            "metadata": {"node_id": "slb_mode_ircookie", "command_prefix": "slb mode ircookie"},
        }]
        kb_path = tmp_path / "knowledge_base.json"
        kb_path.write_text(json.dumps(kb_data, ensure_ascii=False), "utf-8")

        agent = _make_agent()
        result = FarmResult(
            chunk={
                "page_content": "...",
                "metadata": {
                    "node_id": "slb_mode_ircookie",
                    "_enriched_description": "设置 ircookie 工作模式",
                },
            },
            source_file="cli.pdf",
            block_id="cli_0_abc",
            matched_node_id="slb_mode_ircookie",
        )
        updated = agent.update_skeleton([result], kb_path=kb_path)
        assert updated == 1
        saved = json.loads(kb_path.read_text("utf-8"))
        assert "设置 ircookie 工作模式" in saved[0]["page_content"]


# ── TestCultivateBatch (integration with mocked auto_convert) ─────────────────

class TestCultivateBatch:
    def test_full_pipeline_two_chunks(self):
        ac_metas = [
            {"product_module": "SLB", "protocol_type": ["HTTP"],
             "intent": "manage_server_group", "config_mode": "cli",
             "description": "HTTP virtual service config"},
            {"product_module": "SLB", "protocol_type": ["HTTP", "TCP"],
             "intent": "resource_monitoring", "config_mode": "cli",
             "description": "Health check HTTP probe"},
        ]
        agent = _make_agent(ac_metas=ac_metas)
        decisions = [
            _make_decision(
                "slb virtual http svc 10.0.0.1 80\n配置 HTTP 虚拟服务，端口 80。",
                chunk_index=0,
            ),
            _make_decision(
                "health check http probe\n配置 HTTP 健康检查。",
                chunk_index=1,
            ),
        ]
        results = agent.cultivate_batch(decisions)
        assert len(results) == 2
        for r in results:
            assert r.block_id
            meta = r.chunk["metadata"]
            assert meta.get("product_module") == "SLB"
            assert "word_count" in meta
            assert "block_id" in meta

    def test_empty_decisions_returns_empty(self):
        agent = _make_agent()
        assert agent.cultivate_batch([]) == []

    def test_all_rejected_returns_empty(self):
        agent = _make_agent()
        decisions = [
            _make_decision("目录 版权声明", action="reject"),
            _make_decision("版权声明文字", action="pending_review"),
        ]
        assert agent.cultivate_batch(decisions) == []
