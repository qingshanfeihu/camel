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
知识路由器 — 统一 GraphRAG 检索 + 树层级策略

所有知识检索统一经过 UnifiedRAGRetriever:
1. 根据 mode 查 MODE_TREE_STRATEGY 确定允许的树层级
2. 将层级列表传入 UnifiedRAGRetriever.retrieve() 做硬过滤
3. Rules 引擎（确定性规则/模板）仍作为独立补充注入

旧的四层关键词路由已移除；所有 CLI/设计/测试文档均通过 GraphRAG 索引检索。
"""
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from INAGENT.config.project_config import cfg_bool
from INAGENT.rag.knowledge_config import MODE_TREE_STRATEGY

logger = logging.getLogger(__name__)


class KnowledgeLayer(Enum):
    """知识分层枚举（保留用于兼容日志/返回值）。"""
    CLI = "cli"
    RULES = "rules"
    DESIGN = "design"
    TEST = "test"


# ── 兼容：旧 classify 函数保留签名，供外部脚本使用 ──────────────────

def classify_query(query: str) -> List[KnowledgeLayer]:
    """兼容接口 — 统一路由不再依赖关键词分类，始终返回 [DESIGN]。"""
    return [KnowledgeLayer.DESIGN]


def classify_for_mode(mode: str) -> List[KnowledgeLayer]:
    """兼容接口 — 返回模式对应的旧层名称。"""
    if mode == "config":
        return [KnowledgeLayer.CLI, KnowledgeLayer.DESIGN]
    elif mode == "test_write":
        return [KnowledgeLayer.RULES, KnowledgeLayer.CLI, KnowledgeLayer.DESIGN, KnowledgeLayer.TEST]
    elif mode == "test_review":
        return [KnowledgeLayer.RULES, KnowledgeLayer.TEST, KnowledgeLayer.DESIGN]
    else:  # explain
        return [KnowledgeLayer.DESIGN, KnowledgeLayer.CLI]


class KnowledgeRouter:
    """
    统一知识路由器

    所有文档检索经过 UnifiedRAGRetriever（GraphRAG + 向量），
    通过 MODE_TREE_STRATEGY 约束检索范围。
    Rules 引擎（确定性规则/模板）作为独立补充。
    """

    def __init__(
        self,
        unified_rag=None,
        rules_engine=None,
        hybrid_fusion=None,
        # ── 以下参数保留兼容签名，不再使用 ──
        cli_retriever=None,
        hybrid_retriever=None,
        reranker=None,
    ):
        self.unified_rag = unified_rag
        self.rules_engine = rules_engine
        self.hybrid_fusion = hybrid_fusion
        # 兼容：如果只传了 hybrid_retriever 没传 unified_rag，
        # 保存下来供 fallback 使用
        self._hybrid_retriever = hybrid_retriever
        self._reranker = reranker

    def retrieve(
        self,
        query: str,
        mode: str = "explain",
        product_module: str = "",
        max_context_chars: int = 6000,
    ) -> Dict[str, Any]:
        """
        执行统一检索。

        Args:
            query: 用户查询
            mode: 模式 ("explain"/"config"/"test_write"/"test_review")
            product_module: 可选产品模块过滤（传入 UnifiedRAG）
            max_context_chars: 上下文最大字符数

        Returns:
            {
                "context": 合并后的上下文文本,
                "layers_used": 使用了哪些知识层名称（兼容）,
                "cli_results": [],
                "rules_context": 规则上下文（如有）,
                "rag_context": RAG 检索上下文,
                "similar_tests": [],
                "constraints": 约束信息,
                "category_whitelist": 本次使用的树层级列表（同 MODE_TREE_STRATEGY[mode]）,
            }
        """
        category_whitelist = MODE_TREE_STRATEGY.get(mode, [])
        layers_used = [l.value for l in classify_for_mode(mode)]

        result: Dict[str, Any] = {
            "context": "",
            "layers_used": layers_used,
            "cli_results": [],
            "rules_context": "",
            "rag_context": "",
            "similar_tests": [],
            "constraints": {},
            "category_whitelist": category_whitelist,
        }

        parts: List[str] = []

        # 1. Rules 引擎 — 确定性规则/模板，不走 RAG
        if self.rules_engine and mode in ("test_write", "test_review"):
            purpose = "review" if mode == "test_review" else "write"
            try:
                rules_ctx = self.rules_engine.get_rules_context(purpose=purpose)
                if rules_ctx:
                    budget = max_context_chars // 4  # 预留 25% 给规则
                    truncated = rules_ctx[:budget]
                    result["rules_context"] = truncated
                    parts.append(f"[测试规范]\n{truncated}")
            except Exception as e:
                logger.warning("Rules 引擎调用失败: %s", e)

        # 2. 统一 RAG 检索（GraphRAG + 向量），带分类白名单硬过滤
        rag_budget = max_context_chars - sum(len(p) for p in parts)
        rag_ctx, constraints = self._retrieve_unified(
            query, category_whitelist, max_chars=max(rag_budget, 2000),
        )
        if rag_ctx:
            result["rag_context"] = rag_ctx
            result["constraints"] = constraints
            parts.append(f"[知识检索]\n{rag_ctx}")

        result["context"] = "\n\n---\n\n".join(parts) if parts else ""
        return result

    def _retrieve_unified(
        self,
        query: str,
        category_whitelist: List[str],
        max_chars: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """通过 UnifiedRAGRetriever 检索，传入分类白名单做硬过滤。"""
        use_fusion = cfg_bool(
            "knowledge_router.enable_hybrid_fusion",
            True,
            env="INAGENT_ENABLE_HYBRID_FUSION",
        )
        if use_fusion and self.hybrid_fusion:
            try:
                ctx, constraints = self.hybrid_fusion.retrieve(
                    query=query,
                    category_whitelist=category_whitelist,
                    top_k_final=8,
                )
                if ctx:
                    return ctx[:max_chars], constraints
            except Exception as e:
                logger.warning("HybridKnowledgeFusion 检索失败: %s", e)

        if self.unified_rag:
            try:
                ctx, constraints, _ = self.unified_rag.retrieve(
                    query,
                    top_k_final=8,
                    category_whitelist=category_whitelist,
                )
                if ctx:
                    return ctx[:max_chars], constraints
            except Exception as e:
                logger.warning("UnifiedRAG 检索失败: %s", e)

        # fallback: hybrid_retriever + reranker（无白名单过滤）
        if self._hybrid_retriever:
            try:
                from INAGENT.rag.fallback_retrieval import _adaptive_rag_retrieval
                ctx, _, _ = _adaptive_rag_retrieval(
                    hybrid_retriever=self._hybrid_retriever,
                    reranker=self._reranker,
                    job_content=query,
                    top_k_retrieval=10,
                    top_k_rerank=5,
                )
                if ctx:
                    return ctx[:max_chars], {}
            except Exception as e:
                logger.warning("Fallback RAG 检索失败: %s", e)

        return "", {}
