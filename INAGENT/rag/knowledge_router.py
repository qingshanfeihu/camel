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
知识路由器 — 按查询意图分层调度检索

将用户查询路由到合适的知识层:
1. **CLI 层**: 命令语法查询 → CLIReferenceRetriever（精确匹配）
2. **Rules 层**: 测试规范/模板/评审标准 → TestRulesEngine（确定性规则）
3. **Design/Knowledge 层**: 产品知识/设计文档 → UnifiedRAG（向量+GraphRAG）
4. **Test 层**: 已有测试用例参考 → TestRulesEngine.search_similar_tests

路由策略基于关键词启发式规则（不需要 LLM 判断），保持低延迟。
"""
import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class KnowledgeLayer(Enum):
    """知识分层枚举。"""
    CLI = "cli"
    RULES = "rules"
    DESIGN = "design"
    TEST = "test"


# ── 路由关键词 ─────────────────────────────────────────────────────

# CLI 层关键词
_CLI_KEYWORDS = [
    r'\b(cli|命令|command|syntax|语法)\b',
    r'\b(slb|llb|gslb|ha|ssl)\s+(virtual|real|health|group|policy)',
    r'\b(show|no|enable|disable|clear)\s+\w+',
    r'\b(health\s+request|health\s+response|health\s+server)\b',
    r'\b(ip\s+route|ip\s+arp|ip\s+dhcp)\b',
    r'\b(http2\s+settings)\b',
    r'命令[格式语法参数]',
    r'配置命令',
    r'\bcli\s+ref',
]

# Rules 层关键词
_RULES_KEYWORDS = [
    r'测试[规范模板标准模版]',
    r'用例[模板模版格式规范]',
    r'\b(case\s*id|测试编号)\b',
    r'测试类型',
    r'优先级[定义标准分配]',
    r'评审[标准检查清单]',
    r'\b(review|checklist)\b',
    r'(boundary|negative|configuration|functional|load|integration)\s*[测试类]',
    r'测试[分类编写]规[则范]',
]

# Test 层关键词（查已有测试用例）
_TEST_KEYWORDS = [
    r'已有[的]?测试',
    r'现有[的]?[测试用例]',
    r'类似[的]?测试',
    r'参考.*测试',
    r'测试[列表清单]',
    r'test\s*(list|case)',
    r'相似[的]?用例',
    r'重复.*用例',
]

# Design/Knowledge 层（默认层，或显式触发）
_DESIGN_KEYWORDS = [
    r'设计文档',
    r'(功能|产品)[规格说明描述]',
    r'func.*spec',
    r'architecture',
    r'(原理|机制|实现)[是什么]',
    r'(什么是|如何|怎样|为什么)',
    r'(prd|需求)',
]


def classify_query(query: str) -> List[KnowledgeLayer]:
    """
    对查询进行知识层分类。

    返回按优先级排序的知识层列表。一个查询可能涉及多个层
    （如 "为 http2 settings headertablesize 编写 boundary 测试用例"
    → [RULES, CLI, DESIGN, TEST]）。
    """
    q_lower = query.lower()
    layers: List[KnowledgeLayer] = []
    scores: Dict[KnowledgeLayer, float] = {
        KnowledgeLayer.CLI: 0,
        KnowledgeLayer.RULES: 0,
        KnowledgeLayer.DESIGN: 0,
        KnowledgeLayer.TEST: 0,
    }

    for pat in _CLI_KEYWORDS:
        if re.search(pat, q_lower):
            scores[KnowledgeLayer.CLI] += 1

    for pat in _RULES_KEYWORDS:
        if re.search(pat, q_lower):
            scores[KnowledgeLayer.RULES] += 1

    for pat in _TEST_KEYWORDS:
        if re.search(pat, q_lower):
            scores[KnowledgeLayer.TEST] += 1

    for pat in _DESIGN_KEYWORDS:
        if re.search(pat, q_lower):
            scores[KnowledgeLayer.DESIGN] += 1

    # 按分数排序
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    for layer, score in ranked:
        if score > 0:
            layers.append(layer)

    # 默认至少返回 design 层
    if not layers:
        layers.append(KnowledgeLayer.DESIGN)

    return layers


def classify_for_mode(mode: str) -> List[KnowledgeLayer]:
    """
    根据预设模式返回知识层组合。

    Args:
        mode: "explain" / "config" / "test_write" / "test_review"
    """
    if mode == "config":
        return [KnowledgeLayer.CLI, KnowledgeLayer.DESIGN]
    elif mode == "test_write":
        return [KnowledgeLayer.RULES, KnowledgeLayer.CLI, KnowledgeLayer.DESIGN, KnowledgeLayer.TEST]
    elif mode == "test_review":
        return [KnowledgeLayer.RULES, KnowledgeLayer.TEST]
    else:  # explain
        return [KnowledgeLayer.DESIGN, KnowledgeLayer.CLI]


class KnowledgeRouter:
    """
    知识路由器

    整合 CLI 检索、Rules 引擎、RAG 检索和已有测试检索，
    根据查询意图/模式提供分层合并的上下文。
    """

    def __init__(
        self,
        cli_retriever=None,
        rules_engine=None,
        unified_rag=None,
        hybrid_retriever=None,
        reranker=None,
    ):
        self.cli_retriever = cli_retriever
        self.rules_engine = rules_engine
        self.unified_rag = unified_rag
        self.hybrid_retriever = hybrid_retriever
        self.reranker = reranker

    def retrieve(
        self,
        query: str,
        mode: str = "explain",
        product_module: str = "",
        max_context_chars: int = 6000,
    ) -> Dict[str, Any]:
        """
        执行分层检索并整合上下文。

        Args:
            query: 用户查询
            mode: 模式 ("explain"/"config"/"test_write"/"test_review")
            product_module: 可选产品模块过滤
            max_context_chars: 上下文最大字符数

        Returns:
            {
                "context": 合并后的上下文文本,
                "layers_used": 使用了哪些知识层,
                "cli_results": CLI 检索结果（如有）,
                "rules_context": 规则上下文（如有）,
                "rag_context": RAG 检索上下文（如有）,
                "similar_tests": 相似测试项（如有）,
                "constraints": 约束信息,
            }
        """
        # 确定需要查询哪些层
        mode_layers = classify_for_mode(mode)
        query_layers = classify_query(query)
        # 合并：mode 层优先，query 层补充
        layers = list(mode_layers)
        for l in query_layers:
            if l not in layers:
                layers.append(l)

        result: Dict[str, Any] = {
            "context": "",
            "layers_used": [l.value for l in layers],
            "cli_results": [],
            "rules_context": "",
            "rag_context": "",
            "similar_tests": [],
            "constraints": {},
        }

        # 按层分配字符预算
        budget = self._allocate_budget(layers, max_context_chars, mode)
        parts: List[str] = []

        # 1. Rules 层
        if KnowledgeLayer.RULES in layers and self.rules_engine:
            purpose = "review" if mode == "test_review" else "write"
            rules_ctx = self.rules_engine.get_rules_context(purpose=purpose)
            if rules_ctx:
                truncated = rules_ctx[:budget.get(KnowledgeLayer.RULES, 1500)]
                result["rules_context"] = truncated
                parts.append(f"[测试规范]\n{truncated}")

        # 2. CLI 层
        if KnowledgeLayer.CLI in layers and self.cli_retriever:
            cli_results = self.cli_retriever.search(
                query, product_module=product_module, max_results=8,
            )
            if cli_results:
                result["cli_results"] = cli_results
                cli_text = self.cli_retriever.format_results(
                    cli_results, max_chars=budget.get(KnowledgeLayer.CLI, 1500),
                )
                parts.append(f"[CLI 参考]\n{cli_text}")

        # 3. Design/Knowledge 层 (RAG)
        if KnowledgeLayer.DESIGN in layers:
            rag_ctx, constraints = self._retrieve_rag(
                query, product_module,
                max_chars=budget.get(KnowledgeLayer.DESIGN, 2000),
            )
            if rag_ctx:
                result["rag_context"] = rag_ctx
                result["constraints"] = constraints
                parts.append(f"[产品知识/设计文档]\n{rag_ctx}")

        # 4. Test 层
        if KnowledgeLayer.TEST in layers and self.rules_engine:
            similar = self.rules_engine.search_similar_tests(
                query, product_module=product_module, max_results=5,
            )
            if similar:
                result["similar_tests"] = similar
                test_text = self.rules_engine.format_similar_tests(
                    similar, max_chars=budget.get(KnowledgeLayer.TEST, 1000),
                )
                parts.append(f"[已有测试参考]\n{test_text}")

        result["context"] = "\n\n---\n\n".join(parts) if parts else ""
        return result

    def _allocate_budget(
        self,
        layers: List[KnowledgeLayer],
        total: int,
        mode: str,
    ) -> Dict[KnowledgeLayer, int]:
        """按模式和层数分配字符预算。"""
        if not layers:
            return {}

        # 各模式下的权重分配
        weights = {
            "explain": {
                KnowledgeLayer.DESIGN: 5,
                KnowledgeLayer.CLI: 3,
                KnowledgeLayer.RULES: 1,
                KnowledgeLayer.TEST: 1,
            },
            "config": {
                KnowledgeLayer.CLI: 5,
                KnowledgeLayer.DESIGN: 3,
                KnowledgeLayer.RULES: 1,
                KnowledgeLayer.TEST: 1,
            },
            "test_write": {
                KnowledgeLayer.RULES: 3,
                KnowledgeLayer.CLI: 2,
                KnowledgeLayer.DESIGN: 3,
                KnowledgeLayer.TEST: 2,
            },
            "test_review": {
                KnowledgeLayer.RULES: 4,
                KnowledgeLayer.TEST: 3,
                KnowledgeLayer.CLI: 2,
                KnowledgeLayer.DESIGN: 1,
            },
        }

        mode_weights = weights.get(mode, weights["explain"])
        active_weights = {l: mode_weights.get(l, 1) for l in layers}
        w_total = sum(active_weights.values()) or 1
        return {l: int(total * w / w_total) for l, w in active_weights.items()}

    def _retrieve_rag(
        self,
        query: str,
        product_module: str,
        max_chars: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """执行 RAG 检索（优先 UnifiedRAG，否则 fallback）。"""
        # 优先使用 UnifiedRAG
        if self.unified_rag:
            try:
                ctx, constraints, _ = self.unified_rag.retrieve(
                    query, top_k_final=6,
                )
                if ctx:
                    return ctx[:max_chars], constraints
            except Exception as e:
                logger.warning("UnifiedRAG 检索失败: %s", e)

        # fallback: hybrid_retriever + reranker
        if self.hybrid_retriever:
            try:
                from INAGENT.rag.fallback_retrieval import _adaptive_rag_retrieval
                ctx, _, _ = _adaptive_rag_retrieval(
                    hybrid_retriever=self.hybrid_retriever,
                    reranker=self.reranker,
                    job_content=query,
                    top_k_retrieval=10,
                    top_k_rerank=5,
                )
                if ctx:
                    return ctx[:max_chars], {}
            except Exception as e:
                logger.warning("Fallback RAG 检索失败: %s", e)

        return "", {}
