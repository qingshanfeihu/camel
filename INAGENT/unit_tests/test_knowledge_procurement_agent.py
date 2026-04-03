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
"""
KnowledgeProcurementAgent 单元测试

全部使用 MagicMock 替代真实 LLM，测试三层检查逻辑的正确性。
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from camel.models import BaseModelBackend

from INAGENT.agents.knowledge_procurement_agent import (
    ChunkDecision,
    KnowledgeProcurementAgent,
    ProcurementDecision,
    build_procurement_agent,
    build_procurement_prompt,
)
from INAGENT.rag.knowledge_config import DOCUMENT_CATEGORIES


@pytest.fixture
def mock_model():
    return MagicMock()


def _make_agent_with_step(step_fn) -> KnowledgeProcurementAgent:
    """Build a KnowledgeProcurementAgent bypassing ChatAgent construction."""
    mock_chat = MagicMock()
    mock_chat.step = step_fn
    return KnowledgeProcurementAgent(model=MagicMock(), _chat_agent=mock_chat)


@pytest.fixture
def agent_with_mock_llm():
    def fake_step(msg):
        n = msg.content.count("\n---\n") + 1
        result = [
            {
                "idx": i,
                "action": "accept",
                "target_kb": "product",
                "confidence": 0.9,
                "reason": "mock accept",
                "suggested_category": "spec/design",
            }
            for i in range(n)
        ]
        fake_resp = MagicMock()
        fake_resp.msgs = [MagicMock(content=json.dumps(result))]
        return fake_resp

    return _make_agent_with_step(fake_step)


def _make_chunk(
    content: str,
    source_file: str = "test.pdf",
    section_title: str = "",
    category: str = "spec/design",
    module: str = "SLB",
) -> dict:
    return {
        "page_content": content,
        "metadata": {
            "source_file": source_file,
            "section_title": section_title,
            "document_category": category,
            "product_module": module,
        },
    }


# ── Layer 1: 机械检查 ──────────────────────────────────────────────────────────

class TestLayer1Mechanical:
    def test_rejects_too_short(self, agent_with_mock_llm):
        chunk = _make_chunk("短")
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "reject"
        assert decisions[0].decision.confidence == 1.0

    def test_rejects_empty(self, agent_with_mock_llm):
        chunk = _make_chunk("")
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "reject"

    def test_passes_sufficient_content(self, agent_with_mock_llm):
        chunk = _make_chunk(
            "slb virtual http 配置说明：此命令用于创建 HTTP 类型虚拟服务，需要指定 VIP 和端口。"
        )
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "accept"

    def test_exactly_50_chars_passes(self, agent_with_mock_llm):
        chunk = _make_chunk("x" * 50)
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "accept"

    def test_49_chars_rejected(self, agent_with_mock_llm):
        chunk = _make_chunk("x" * 49)
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "reject"


# ── Layer 2: LLM 判断 ─────────────────────────────────────────────────────────

class TestLayer2LLM:
    def test_llm_reject_propagated(self):
        def fake_step(msg):
            result = [
                {
                    "idx": 0,
                    "action": "reject",
                    "target_kb": "unknown",
                    "confidence": 0.95,
                    "reason": "目录页",
                    "suggested_category": "",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "目录\n1. 章节一 ...... 1\n2. 章节二 ...... 5\n3. 章节三 ...... 10\n这是一段超过50字符的目录内容。"
        )
        decisions = agent.evaluate_batch([chunk])
        assert decisions[0].decision.action == "reject"

    def test_low_confidence_becomes_pending_review(self):
        def fake_step(msg):
            result = [
                {
                    "idx": 0,
                    "action": "accept",
                    "target_kb": "product",
                    "confidence": 0.4,
                    "reason": "不确定",
                    "suggested_category": "spec/design",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "这是一段有足够长度的产品知识内容，详细描述了某个功能模块的基本使用方法、参数说明和注意事项，请仔细阅读。"
        )
        decisions = agent.evaluate_batch([chunk])
        assert decisions[0].decision.action == "pending_review"

    def test_llm_failure_becomes_pending_review(self):
        def fake_step(msg):
            raise ConnectionError("LLM 服务不可用")

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "这是一段有足够长度的产品知识内容，详细描述了某个功能模块的基本使用方法、参数说明和注意事项，请仔细阅读。"
        )
        decisions = agent.evaluate_batch([chunk])
        assert decisions[0].decision.action == "pending_review"
        assert "LLM" in decisions[0].decision.reason

    def test_batching_by_source_file(self):
        call_count = {"n": 0}

        def fake_step(msg):
            call_count["n"] += 1
            n = msg.content.count("\n---\n") + 1
            result = [
                {
                    "idx": i,
                    "action": "accept",
                    "target_kb": "product",
                    "confidence": 0.9,
                    "reason": "ok",
                    "suggested_category": "spec/design",
                }
                for i in range(n)
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        long_content = "这是一段足够长度的产品知识内容，用于测试批处理功能是否正确工作。" * 3
        chunks = [
            _make_chunk(long_content, source_file="doc1.pdf") for _ in range(3)
        ]
        agent.evaluate_batch(chunks)
        assert call_count["n"] == 1

    def test_different_source_files_separate_llm_calls(self):
        call_count = {"n": 0}

        def fake_step(msg):
            call_count["n"] += 1
            result = [
                {
                    "idx": 0,
                    "action": "accept",
                    "target_kb": "product",
                    "confidence": 0.9,
                    "reason": "ok",
                    "suggested_category": "spec/design",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        long_content = "这是一段足够长度的产品知识内容，用于测试多文件分批调用是否正确工作。" * 3
        chunks = [
            _make_chunk(long_content, source_file="doc1.pdf"),
            _make_chunk(long_content, source_file="doc2.pdf"),
        ]
        agent.evaluate_batch(chunks)
        assert call_count["n"] == 2


# ── Layer 3: Schema 检查 ────────────────────────────────────────────────────────

class TestLayer3Schema:
    def test_unknown_category_becomes_staging(self):
        def fake_step(msg):
            result = [
                {
                    "idx": 0,
                    "action": "accept",
                    "target_kb": "product",
                    "confidence": 0.9,
                    "reason": "有价值",
                    "suggested_category": "monitoring/integration",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "SNMP v3 监控集成配置说明，支持 trap 接收端点配置和 MIB-II 标准对象标识符查询，需要指定 community string 和版本号。",
            category="monitoring/integration",
        )
        decisions = agent.evaluate_batch([chunk])
        assert decisions[0].decision.action == "staging"
        assert decisions[0].decision.schema_gap == "new_category"
        assert decisions[0].decision.suggested_value == "monitoring/integration"

    def test_known_category_stays_accept(self, agent_with_mock_llm):
        chunk = _make_chunk(
            "slb virtual http 配置说明：此命令用于创建 HTTP 类型虚拟服务，需要指定 VIP 和端口。",
            category="cli/reference",
        )
        decisions = agent_with_mock_llm.evaluate_batch([chunk])
        assert decisions[0].decision.action == "accept"

    def test_reject_not_upgraded_to_staging(self):
        def fake_step(msg):
            result = [
                {
                    "idx": 0,
                    "action": "reject",
                    "target_kb": "unknown",
                    "confidence": 0.95,
                    "reason": "无价值",
                    "suggested_category": "monitoring/new",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "这是一段被 LLM 判定为无价值的内容，超过五十个字符，但是没有任何实际产品知识。",
            category="monitoring/new",
        )
        decisions = agent.evaluate_batch([chunk])
        assert decisions[0].decision.action == "reject"


# ── 日志写入 ────────────────────────────────────────────────────────────────────

class TestWriteLogs:
    def test_reject_written_to_log(self, agent_with_mock_llm):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            chunk = _make_chunk("短")
            decisions = agent_with_mock_llm.evaluate_batch([chunk])
            counts = agent_with_mock_llm.write_logs(decisions, log_dir=log_dir)
            assert counts.get("reject", 0) == 1
            log_file = log_dir / "reject_log.jsonl"
            assert log_file.exists()
            line = json.loads(log_file.read_text(encoding="utf-8").strip())
            assert line["action"] == "reject"

    def test_accept_not_written_to_log(self, agent_with_mock_llm):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            chunk = _make_chunk(
                "slb virtual http 配置说明：此命令用于创建 HTTP 类型虚拟服务，需要指定 VIP 和端口。"
            )
            decisions = agent_with_mock_llm.evaluate_batch([chunk])
            agent_with_mock_llm.write_logs(decisions, log_dir=log_dir)
            reject_file = log_dir / "reject_log.jsonl"
            assert not reject_file.exists()

    def test_counts_returned(self, agent_with_mock_llm):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            chunks = [
                _make_chunk("短"),
                _make_chunk(
                    "这是足够长的产品知识内容，描述了 SLB 虚拟服务的配置方法和注意事项，包含完整参数说明与使用示例。"
                ),
            ]
            decisions = agent_with_mock_llm.evaluate_batch(chunks)
            counts = agent_with_mock_llm.write_logs(decisions, log_dir=log_dir)
            assert counts["reject"] == 1
            assert counts["accept"] == 1


# ── filter_accepted ────────────────────────────────────────────────────────────

class TestFilterAccepted:
    def test_only_accepted_returned(self, agent_with_mock_llm):
        chunks = [
            _make_chunk("短"),
            _make_chunk(
                "这是足够长的产品知识内容，描述了 SLB 虚拟服务的配置方法，包含完整参数说明、使用示例和注意事项。"
            ),
        ]
        decisions = agent_with_mock_llm.evaluate_batch(chunks)
        accepted = agent_with_mock_llm.filter_accepted(decisions)
        assert len(accepted) == 1

    def test_suggested_category_applied(self):
        def fake_step(msg):
            result = [
                {
                    "idx": 0,
                    "action": "accept",
                    "target_kb": "product",
                    "confidence": 0.9,
                    "reason": "ok",
                    "suggested_category": "architecture/design",
                }
            ]
            r = MagicMock()
            r.msgs = [MagicMock(content=json.dumps(result))]
            return r

        agent = _make_agent_with_step(fake_step)
        chunk = _make_chunk(
            "NSAE 采用三层架构：前端接入层、核心处理层、后端管理层。各层通过内部消息总线通信，支持水平扩展。",
            category="spec/design",
        )
        decisions = agent.evaluate_batch([chunk])
        accepted = agent.filter_accepted(decisions)
        assert accepted[0]["metadata"]["document_category"] == "architecture/design"


# ── build 辅助函数 ─────────────────────────────────────────────────────────────

class TestBuildProcurementPrompt:
    def test_contains_source_file(self):
        chunks = [_make_chunk("测试内容")]
        prompt = build_procurement_prompt("cli.pdf", chunks)
        assert "cli.pdf" in prompt

    def test_contains_chunk_count(self):
        chunks = [_make_chunk("测试内容一"), _make_chunk("测试内容二")]
        prompt = build_procurement_prompt("doc.pdf", chunks)
        assert "2" in prompt

    def test_contains_chunk_index(self):
        chunks = [_make_chunk("测试内容")]
        prompt = build_procurement_prompt("doc.pdf", chunks)
        assert "[0]" in prompt


class TestBuildProcurementAgent:
    @pytest.fixture
    def real_mock_model(self):
        """spec=BaseModelBackend 让 isinstance 通过，同时配置必要属性。"""
        from camel.models import BaseModelBackend
        m = MagicMock(spec=BaseModelBackend)
        m.model_type = MagicMock()
        m.token_limit = 4096
        return m

    def test_returns_chat_agent(self, real_mock_model):
        from camel.agents import ChatAgent

        agent = build_procurement_agent(real_mock_model, "NSAE 负载均衡器")
        assert isinstance(agent, ChatAgent)

    def test_system_message_contains_product_name(self, real_mock_model):
        agent = build_procurement_agent(real_mock_model, "NSAE 测试产品")
        assert "NSAE 测试产品" in agent.system_message.content

    def test_system_message_contains_categories(self, real_mock_model):
        agent = build_procurement_agent(real_mock_model, "NSAE")
        for cat in DOCUMENT_CATEGORIES[:3]:
            assert cat in agent.system_message.content
