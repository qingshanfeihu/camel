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
KnowledgeToolkit — 将 INAGENT 知识库暴露为 CAMEL FunctionTool

支持 3 个工具函数：
- search_product_knowledge: 通过 UnifiedRAGRetriever 检索产品知识
- get_review_rules: 通过 TestRulesEngine 获取评审/编写规范
- search_similar_tests: 通过 TestRulesEngine 搜索已有测试用例

每个 KnowledgeToolkit 实例绑定一个 mode（config / test_write /
test_review / explain），自动携带 ``MODE_TREE_STRATEGY[mode]`` 树层级白名单
（``MODE_CATEGORY_WHITELIST`` 为其别名）。
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import List, Optional

from camel.toolkits.base import BaseToolkit
from camel.toolkits.function_tool import FunctionTool

from INAGENT.config.project_config import cfg_float
from INAGENT.rag.knowledge_config import (
    DOCUMENT_CATEGORIES,
    MODE_CATEGORY_WHITELIST,
    MODE_TREE_STRATEGY,
)

logger = logging.getLogger(__name__)


class KnowledgeToolkit(BaseToolkit):
    r"""Toolkit that exposes the INAGENT knowledge base as callable tools
    for LLM agents.

    Each instance is bound to a *mode* (``"config"``, ``"test_write"``,
    ``"test_review"``, ``"explain"``).  The mode automatically constrains
        the tree-level whitelist that ``search_product_knowledge`` passes to
        UnifiedRAG (see :data:`MODE_TREE_STRATEGY` / ``MODE_CATEGORY_WHITELIST``).

    Args:
        mode: Operating mode that determines default category whitelist.
        unified_rag: A pre-initialised
            :class:`~INAGENT.rag.unified_rag.UnifiedRAGRetriever` instance.
            When *None*, the singleton from ``deps`` is used on first call.
        rules_engine: A pre-initialised
            :class:`~INAGENT.rag.test_rules.TestRulesEngine` instance.
            When *None*, the singleton is used on first call.
        timeout: Timeout threshold inherited from
            :class:`~camel.toolkits.base.BaseToolkit`.
    """

    def __init__(
        self,
        mode: str = "explain",
        unified_rag=None,
        rules_engine=None,
        timeout: Optional[float] = None,
        hierarchy_prefix: str = "",
    ):
        super().__init__(timeout=timeout)
        self.mode = mode
        self._unified_rag = unified_rag
        self._rules_engine = rules_engine
        self._literal_docs_cache = None
        self._hierarchy_prefix = hierarchy_prefix
        self._query_cache: dict = {}
        self._retrieval_log: list = []

    # ── 惰性获取单例 ──────────────────────────────────────────────

    def _get_unified_rag(self):
        if self._unified_rag is None:
            from INAGENT.web.deps import get_unified_rag
            self._unified_rag = get_unified_rag()
        return self._unified_rag

    def _get_rules_engine(self):
        if self._rules_engine is None:
            from INAGENT.rag.test_rules import get_test_rules_engine
            self._rules_engine = get_test_rules_engine()
        return self._rules_engine

    def get_retrieval_log(self) -> list:
        return list(self._retrieval_log)

    def compute_retrieval_usage(self, review_text: str) -> dict:
        """Check which retrieved chunks were cited in review_text."""
        total = len(self._retrieval_log)
        if total == 0:
            return {"total": 0, "cited": 0, "waste_ratio": 0.0}
        cited = 0
        for entry in self._retrieval_log:
            terms = entry.get("distinctive_terms", [])
            if terms and any(t in review_text.lower() for t in terms[:3]):
                entry["was_used"] = True
                cited += 1
            else:
                entry["was_used"] = False
        return {
            "total": total,
            "cited": cited,
            "waste_ratio": round((total - cited) / total, 3) if total > 0 else 0.0,
        }

    def _extract_literal_terms(self, query: str) -> List[str]:
        # 仅提取英文/数字标识符，避免把自然语言长句作为“字面词”。
        terms = re.findall(r"[A-Za-z][A-Za-z0-9_/-]{2,}", query or "")
        seen = set()
        out: List[str] = []
        for t in terms:
            k = t.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
            if len(out) >= 6:
                break
        return out

    def _literal_fallback(self, terms: List[str], max_hits: int = 4) -> str:
        if not terms:
            return ""
        try:
            if self._literal_docs_cache is None:
                from pathlib import Path
                from INAGENT.utils import env_utils

                reference_dir = (
                    Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
                )
                self._literal_docs_cache = env_utils.load_knowledge_base(reference_dir)
            docs = self._literal_docs_cache or []
            hits = []
            for d in docs:
                text = str(getattr(d, "page_content", "") or "")
                low = text.lower()
                matched = [t for t in terms if t in low]
                if not matched:
                    continue
                meta = getattr(d, "metadata", {}) or {}
                src = str(meta.get("source_file") or meta.get("source_pdf") or "unknown")
                cat = str(meta.get("document_category") or "")
                snippet = re.sub(r"\s+", " ", text).strip()
                if len(snippet) > 220:
                    snippet = snippet[:220] + "..."
                hits.append(
                    f"[literal:{','.join(matched)}][{cat}] {src}\n{snippet}"
                )
                if len(hits) >= max_hits:
                    break
            return "\n\n".join(hits)
        except Exception as e:
            logger.debug("literal fallback failed: %s", e)
            return ""

    def _retrieve_with_timeout(
        self,
        *,
        query: str,
        top_k_final: int,
        category_whitelist: Optional[List[str]] = None,
        decomposition_result=None,
        use_graphrag: bool = True,
        timeout_seconds: Optional[float] = None,
    ):
        if timeout_seconds is None:
            timeout_seconds = cfg_float(
                "bug_to_case.rag.timeout_seconds",
                120.0,
                env="BUG_TO_CASE_RAG_TIMEOUT_SECONDS",
            )
        unified = self._get_unified_rag()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                unified.retrieve,
                query,
                50,
                top_k_final,
                use_graphrag,
                decomposition_result,
                None,
                category_whitelist,
                self._hierarchy_prefix or None,
            )
            ctx, constraints, decomp = future.result(timeout=timeout_seconds)

        conf = constraints.get("_retrieval_confidence", {})
        terms = [t for t in query.lower().split() if len(t) >= 2][:5]
        self._retrieval_log.append({
            "query": query[:200],
            "distinctive_terms": terms,
            "top_score": conf.get("top_score", 0),
            "result_count": conf.get("result_count", 0),
        })
        return ctx, constraints, decomp

    # ── 工具函数 ──────────────────────────────────────────────────

    def search_product_knowledge(
        self,
        query: str,
        category_filter: str = "",
        max_results: int = 8,
        use_graphrag: bool = True,
    ) -> str:
        r"""Search the product knowledge base using GraphRAG + vector
        retrieval.

        Use this tool to look up CLI command syntax, product design
        specifications, functional requirements, test strategies, or any
        other product documentation stored in the knowledge base.

        Args:
            query: Natural-language search query describing the knowledge
                you need.  Be as specific as possible (e.g. include the
                feature name, CLI command, or requirement ID).
            category_filter: Optional comma-separated **tree levels** matching
                ``knowledge_config.TREE_LEVELS`` (e.g. ``"leaf,branch"``).
                These are passed to ``UnifiedRAGRetriever`` as
                ``category_whitelist`` and filter on
                ``metadata.tree_position.tree_level``.
                When empty, ``MODE_TREE_STRATEGY[mode]`` is
                used.
            max_results: Maximum number of top results to include in the
                response (default 8).

        Returns:
            A text block containing the retrieved product knowledge, or
            a message indicating no results were found.
        """
        # Determine category whitelist
        if category_filter:
            raw = [c.strip() for c in category_filter.split(",") if c.strip()]
            valid_cats = {k for k in DOCUMENT_CATEGORIES}
            whitelist = [c for c in raw if c in valid_cats]
            if not whitelist:
                whitelist = MODE_CATEGORY_WHITELIST.get(self.mode, [])
                logger.debug(
                    "category_filter %r contained no valid categories, "
                    "falling back to mode whitelist %s",
                    category_filter, whitelist,
                )
        else:
            whitelist = MODE_CATEGORY_WHITELIST.get(self.mode, [])

        cache_key = f"{query}|{','.join(whitelist)}|{max_results}"
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            logger.debug("KnowledgeToolkit query cache hit: %s", query[:60])
            return cached

        try:
            ctx, _constraints, _ = self._retrieve_with_timeout(
                query=query,
                top_k_final=max_results,
                category_whitelist=whitelist,
                use_graphrag=use_graphrag,
            )
            terms = self._extract_literal_terms(query)
            if ctx:
                low_ctx = ctx.lower()
                missing = [t for t in terms if t not in low_ctx]
                if missing:
                    extra = self._literal_fallback(missing, max_hits=3)
                    if extra:
                        result = f"{ctx}\n\n[字面证据补充]\n{extra}"
                        self._query_cache[cache_key] = result
                        return result
                self._query_cache[cache_key] = ctx
                return ctx
            extra = self._literal_fallback(terms, max_hits=4)
            if extra:
                result = f"[字面证据补充]\n{extra}"
                self._query_cache[cache_key] = result
                return result
            return "(未找到相关知识)"
        except FutureTimeout:
            logger.warning(
                "search_product_knowledge 超时，降级为向量检索: query='%s...'",
                query[:60],
            )
            try:
                ctx, _constraints, _ = self._retrieve_with_timeout(
                    query=query,
                    top_k_final=max_results,
                    category_whitelist=whitelist,
                    use_graphrag=False,
                    timeout_seconds=30.0,
                )
                if ctx:
                    return ctx
                return "(知识检索超时，向量降级未命中)"
            except Exception as e2:
                logger.warning("search_product_knowledge 向量降级失败: %s", e2)
                return f"(知识检索超时且降级失败: {e2})"
        except Exception as e:
            logger.warning("search_product_knowledge 失败: %s", e)
            return f"(知识检索出错: {e})"

    def get_review_rules(self, purpose: str = "") -> str:
        r"""Get the deterministic test case rules and review checklist.

        Use this tool when you need the standard test case template,
        field definitions, naming conventions, priority levels, test type
        definitions, or the review checklist (R01–R14).

        Args:
            purpose: ``"review"`` to include the review checklist, or
                ``"write"`` to include writing guidelines.  When empty,
                defaults to ``"review"`` for ``test_review`` mode and
                ``"write"`` otherwise.

        Returns:
            A structured text block containing the test rules, template
            fields, and (optionally) the review checklist.
        """
        if not purpose:
            purpose = "review" if self.mode == "test_review" else "write"

        try:
            engine = self._get_rules_engine()
            return engine.get_rules_context(purpose=purpose)
        except Exception as e:
            logger.warning("get_review_rules 失败: %s", e)
            return f"(获取规则失败: {e})"

    def search_similar_tests(
        self,
        query: str,
        max_results: int = 10,
        use_graphrag: bool = True,
    ) -> str:
        r"""Search existing test cases for similar items.

        Use this tool to find test cases that may already cover the same
        functionality, to check for duplicates, or to use as format
        references when writing new test cases.

        Args:
            query: Keywords or description of the test scenario to search
                for (e.g. ``"HTTP2 HPACK header compression boundary"``).
            max_results: Maximum number of similar test items to return
                (default 10).

        Returns:
            Formatted text listing similar existing test cases, or a
            message indicating none were found.
        """
        # 与 test_write 相同的树层级策略（硬过滤的是 tree_level，非旧式 document_category 字符串）
        similar_whitelist = list(MODE_TREE_STRATEGY.get("test_write", ["leaf", "branch"]))
        try:
            ctx, _constraints, _decomp = self._retrieve_with_timeout(
                query=query,
                top_k_final=max_results,
                category_whitelist=similar_whitelist,
                use_graphrag=use_graphrag,
            )
            if ctx:
                return ctx
        except FutureTimeout:
            logger.warning(
                "search_similar_tests 语义检索超时，降级为向量检索: query='%s...'",
                query[:60],
            )
            try:
                ctx, _constraints, _decomp = self._retrieve_with_timeout(
                    query=query,
                    top_k_final=max_results,
                    category_whitelist=similar_whitelist,
                    use_graphrag=False,
                    timeout_seconds=30.0,
                )
                if ctx:
                    return ctx
            except Exception as e:
                logger.warning("search_similar_tests 向量降级失败: %s", e)
        except Exception as e:
            logger.warning("search_similar_tests 语义检索失败，回退关键词检索: %s", e)

        try:
            engine = self._get_rules_engine()
            items = engine.search_similar_tests(
                query=query,
                max_results=max_results,
            )
            if not items:
                return "(未找到相似测试项)"
            return engine.format_similar_tests(items, max_chars=4000)
        except Exception as e:
            logger.warning("search_similar_tests 回退检索失败: %s", e)
            return f"(搜索相似测试失败: {e})"

    # ── BaseToolkit 接口 ──────────────────────────────────────────

    def get_tools(self) -> List[FunctionTool]:
        r"""Return the list of knowledge tools available in this toolkit."""
        return [
            FunctionTool(self.search_product_knowledge),
            FunctionTool(self.get_review_rules),
            FunctionTool(self.search_similar_tests),
        ]
