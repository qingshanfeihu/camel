# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
统一 RAG 机制：GraphRAG + 向量检索 并行 + Rerank + 协议加权

将 GraphRAG 图检索、向量检索、Rerank 重排序、协议加权整合为单一流水线，
供 workflow_config_generator 与 graphrag_integration 共用。

流水线顺序（GraphRAG 与向量检索并行）：
1. [Thread-1] GraphRAG 图检索（实体/关系/社区报告 + text_units）
   [Thread-2] 向量检索（BM25 + 语义混合）
2. 合并去重
3. Rerank 重排序
4. 协议加权并重排
5. 构建上下文与约束

支持 rag_queries 多查询合并检索：将任务分解生成的精确查询分别执行，合并后统一 rerank。

参考：
- https://microsoft.github.io/graphrag/config/yaml/
- https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/
"""
import re
import json
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

from INAGENT.config.project_config import cfg_bool, cfg_float, cfg_int

logger = logging.getLogger(__name__)


@dataclass
class RetrievalConfidence:
    """Structured signal about retrieval quality."""

    top_score: float = 0.0
    score_spread: float = 0.0
    graphrag_contributed: bool = False
    keyword_coverage: float = 0.0
    result_count: int = 0

    @property
    def is_confident(self) -> bool:
        return self.top_score >= 0.5 and self.score_spread >= 0.1

    def to_prompt_hint(self) -> str:
        if self.is_confident:
            return ""
        return "[检索置信度较低] 以下知识可能不完整，请审慎引用，标注不确定的结论。"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "top_score": round(self.top_score, 4),
            "score_spread": round(self.score_spread, 4),
            "graphrag_contributed": self.graphrag_contributed,
            "keyword_coverage": round(self.keyword_coverage, 3),
            "result_count": self.result_count,
            "is_confident": self.is_confident,
        }


_NEGATIVE_EVIDENCE_PATTERNS = (
    "未找到",
    "无相关",
    "没有相关",
    "not found",
    "no relevant",
)
_CLI_HINT_PATTERNS = (
    "system tune",
    "show ",
    " no ",
    "slb ",
    "tcp ",
    "http ",
)
_GRAPH_RESPONSE_DISCLAIMER_PATTERNS = (
    "the provided data does not contain",
    "if you don't know the answer, just say so",
    "points supported by data should list their data references",
)


def _is_graph_response_noise(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return True
    return any(p in low for p in _GRAPH_RESPONSE_DISCLAIMER_PATTERNS)

# 协议加权：优先使用 rag_scoring_utils
try:
    from INAGENT.rag.scoring_utils import compute_protocol_boost
    _USE_SCORING_UTILS = True
except ImportError:
    _USE_SCORING_UTILS = False


def _parse_metadata_from_text(text: str) -> Dict[str, Any]:
    """从文本首行解析 INAGENT_META_JSON 格式的元数据"""
    if not text:
        return {}
    match = re.match(r"^INAGENT_META_JSON:(\{.*?\})\n", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return {}


def _get_score(doc: Dict[str, Any]) -> float:
    """
    从文档中提取分数，兼容多种评分键名：
    - similarity score: Reranker 设置的分数
    - rrf_score: HybridRetriever RRF 融合分数
    - score: GraphRAG 等来源的分数
    """
    if isinstance(doc, dict):
        # 优先 similarity score（reranker 设置后的值）
        score = doc.get("similarity score")
        if score is not None and score != 0.0:
            return float(score)
        # 其次 rrf_score（HybridRetriever 的融合分数，此前被忽略导致全 0）
        score = doc.get("rrf_score")
        if score is not None:
            return float(score)
        # 最后 score（GraphRAG 等来源）
        score = doc.get("score")
        if score is not None:
            return float(score)
    return 0.0


def _set_score(doc: Dict[str, Any], score: float) -> None:
    """统一设置文档分数"""
    doc["similarity score"] = score


def _compute_protocol_boost_fallback(query: str, item: Dict[str, Any]) -> float:
    """
    协议加权后备实现（当 rag_scoring_utils 不可用时）
    根据查询与文档的 protocol_type 匹配动态计算加权。
    """
    q_lower = (query or "").lower()
    meta = item.get("metadata", {}) or {}
    text = (item.get("text") or item.get("content") or "")[:500].lower()

    query_protocols = set()
    doc_protocols = set()

    meta_protocols = meta.get("protocol_type", [])
    if isinstance(meta_protocols, list):
        doc_protocols.update([p.lower() for p in meta_protocols if p])
    elif meta_protocols:
        doc_protocols.add(str(meta_protocols).lower())

    for word in ["http", "https", "tcp", "udp", "ftp", "dns", "sip", "smtp"]:
        if word in q_lower:
            query_protocols.add(word)
        if word in text:
            doc_protocols.add(word)

    exact = query_protocols & doc_protocols
    if exact:
        return 0.05
    missing = query_protocols - doc_protocols
    if missing and doc_protocols:
        return -0.03
    return 0.0


def _apply_protocol_boost(
    query: str,
    documents: List[Dict[str, Any]],
    use_scoring_utils: Optional[bool] = None,
) -> None:
    """对文档列表应用协议加权（原地修改 similarity score）"""
    if use_scoring_utils is None:
        use_scoring_utils = _USE_SCORING_UTILS
    for doc in documents:
        if use_scoring_utils:
            boost, _ = compute_protocol_boost(query, doc)
        else:
            boost = _compute_protocol_boost_fallback(query, doc)
        if boost != 0.0:
            orig = _get_score(doc)
            _set_score(doc, orig + boost)
            doc["protocol_boost"] = boost
            logger.debug(
                "[UnifiedRAG] 协议加权: %+.3f (原始: %.3f -> %.3f)",
                boost, orig, _get_score(doc),
            )


def _apply_category_filter(
    documents: List[Dict[str, Any]],
    document_category_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    按 document_category 过滤/加权文档。

    如果指定了 filter，则：
    - 精确匹配的文档加权 +0.08
    - 同一级分类（spec/* 或 test/*）的文档加权 +0.03
    - 完全不匹配的文档加权 -0.02
    如果未指定 filter，则不做任何修改。
    """
    if not document_category_filter or not documents:
        return documents

    filter_prefix = document_category_filter.split("/")[0] if "/" in document_category_filter else ""

    for doc in documents:
        meta = doc.get("metadata") or {}
        # 从 metadata 或 regex_metadata 或 INAGENT_META_JSON 获取文档分类
        doc_cat = meta.get("document_category", "")
        if not doc_cat:
            regex_meta = meta.get("regex_metadata") or {}
            doc_cat = regex_meta.get("document_category", "")
        if not doc_cat:
            text_meta = _parse_metadata_from_text(doc.get("text", ""))
            doc_cat = text_meta.get("document_category", "")

        boost = 0.0
        if doc_cat == document_category_filter:
            boost = 0.08
        elif filter_prefix and doc_cat.startswith(filter_prefix + "/"):
            boost = 0.03
        elif doc_cat and doc_cat != document_category_filter:
            boost = -0.02

        if boost != 0.0:
            orig = _get_score(doc)
            _set_score(doc, orig + boost)
            doc["category_boost"] = boost

    return documents


def _apply_hierarchy_boost(
    documents: List[Dict[str, Any]],
    hierarchy_prefix: str,
) -> None:
    """Graph-aware relevance: 用 CLI graph 连通性替代硬编码惩罚。

    同 L1 + 同 L2 前缀: +0.05 boost
    同 L1 + 不同 L2 + shares_keyword 连通: +0.02
    同 L1 + 不同 L2 + 不连通: 0 (不罚)
    无 hierarchy / 不同 L1: 不变
    """
    if not hierarchy_prefix or not documents:
        return

    parts = [p.strip() for p in hierarchy_prefix.split(">")]
    target_l1 = parts[0] if len(parts) >= 1 else ""
    target_l2 = parts[1] if len(parts) >= 2 else ""
    if not target_l1 or not target_l2:
        return

    target_l1_lower = target_l1.lower()
    target_l2_lower = target_l2.lower()
    adjusted = 0

    cli_graph = None
    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        cli_graph = get_cli_graph_store()
    except Exception:
        pass

    _connected_cache: Dict[str, bool] = {}

    for doc in documents:
        meta = doc.get("metadata") or {}
        fh = meta.get("function_hierarchy", "")
        if not fh:
            rm = meta.get("regex_metadata") or {}
            fh = rm.get("function_hierarchy", "")
        if not fh:
            continue

        fh_parts = [p.strip() for p in fh.split(">")]
        doc_l1 = fh_parts[0].lower() if len(fh_parts) >= 1 else ""
        doc_l2 = fh_parts[1].lower() if len(fh_parts) >= 2 else ""

        if doc_l1 != target_l1_lower:
            continue

        if not doc_l2:
            continue

        orig = _get_score(doc)
        if doc_l2 == target_l2_lower:
            _set_score(doc, orig + 0.05)
            doc["hierarchy_boost"] = 0.05
            adjusted += 1
        else:
            boost = 0.0
            if cli_graph:
                cache_key = f"{target_l2_lower}|{doc_l2}"
                if cache_key not in _connected_cache:
                    _connected_cache[cache_key] = cli_graph.l2_connected(
                        target_l2_lower, doc_l2, max_hops=3
                    )
                if _connected_cache[cache_key]:
                    boost = 0.02
            _set_score(doc, orig + boost)
            doc["hierarchy_boost"] = boost
            adjusted += 1

    if adjusted > 0:
        logger.info(
            "[UnifiedRAG] hierarchy boost: prefix='%s', adjusted %d docs",
            hierarchy_prefix, adjusted,
        )


def _build_query_keywords(query: str) -> List[str]:
    """抽取查询中的关键 token，用于一致性过滤。"""
    if not query:
        return []
    tokens = re.findall(r"[A-Za-z0-9_/-]+|[\u4e00-\u9fff]{2,}", query.lower())
    stopwords = {
        "请", "根据", "以及", "相关", "模块", "测试", "评审", "功能",
        "check", "review", "test", "module", "case",
    }
    keywords = []
    seen = set()
    for tok in tokens:
        t = tok.strip()
        if len(t) < 2 or t in stopwords or t in seen:
            continue
        seen.add(t)
        keywords.append(t)
    return keywords[:12]


def _looks_self_contradictory(text: str) -> bool:
    """检测单段文本内“无信息”与“命令细节”并存的自相矛盾情况。"""
    if not text:
        return False
    low = text.lower()
    has_negative = any(p in low for p in _NEGATIVE_EVIDENCE_PATTERNS)
    has_cli_detail = any(p in low for p in _CLI_HINT_PATTERNS)
    return has_negative and has_cli_detail


def _is_doc_consistent(query: str, doc: Dict[str, Any]) -> bool:
    """基于关键词重叠与矛盾语句做轻量一致性校验。"""
    text = (doc.get("text") or "")[:2400].lower()
    if not text:
        return False
    if _looks_self_contradictory(text):
        return False

    keywords = _build_query_keywords(query)
    if not keywords:
        return True

    overlap = sum(1 for kw in keywords if kw in text)
    score = _get_score(doc)
    # 高分文档允许较低重叠；低分文档要求至少命中一个关键词
    if score >= 0.78:
        return True
    return overlap >= 1


def _adaptive_hybrid_query_kwargs(query: str) -> Dict[str, Any]:
    """根据查询类型动态设置 HybridRetriever 参数。"""
    q = (query or "").lower()
    is_cli_like = any(k in q for k in ("cli", "命令", "syntax", "system tune", "show ", " no "))
    if is_cli_like:
        return {
            "vector_weight": 0.65,
            "bm25_weight": 0.35,
            "vector_retriever_similarity_threshold": 0.42,
        }
    return {
        "vector_weight": 0.8,
        "bm25_weight": 0.2,
        "vector_retriever_similarity_threshold": 0.5,
    }


def _build_constraints_from_metadata(metadata_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从元数据列表汇总约束条件"""
    constraints: Dict[str, Any] = {}
    product_modules = set()
    protocol_types = set()
    document_categories = set()
    for meta in metadata_list or []:
        if meta.get("product_module"):
            product_modules.add(meta["product_module"])
        pt = meta.get("protocol_type")
        if pt:
            if isinstance(pt, list):
                protocol_types.update(pt)
            else:
                protocol_types.add(pt)
        dc = meta.get("document_category")
        if dc:
            document_categories.add(dc)
    if product_modules:
        constraints["product_modules"] = list(product_modules)
    if protocol_types:
        constraints["protocol_types"] = list(protocol_types)
    if document_categories:
        constraints["document_categories"] = list(document_categories)
    return constraints


class UnifiedRAGRetriever:
    """
    统一 RAG 检索器（GraphRAG 优先）

    流水线：
    1. 构建查询列表（rag_queries 精确查询 + 主 query 宽泛查询）
    2. GraphRAG local_search（每条 query 分别检索）
    3. 向量检索（每条 query 分别检索）
    4. 合并去重
    5. Rerank 重排序（用主 query）
    6. 协议加权
    7. 构建上下文与约束
    
    关键改进：
    - GraphRAG 优先：先执行图检索获取结构化知识
    - 多查询合并：将 rag_queries 逐条执行，获取更精确的候选
    - 分数修复：正确读取 rrf_score（HybridRetriever 的真实分数）
    - 增大返回量：top_k_final 默认 8（覆盖更多命令类型）
    - 上下文加宽：每文档截取 1500 字符（比原来 1000 更完整）
    """

    def __init__(
        self,
        hybrid_retriever,
        reranker=None,
        graphrag_retriever=None,
        use_protocol_boost: bool = True,
    ):
        self.hybrid_retriever = hybrid_retriever
        self.reranker = reranker
        self.graphrag_retriever = graphrag_retriever
        self.use_protocol_boost = use_protocol_boost
        self._cache_enabled = cfg_bool(
            "bug_to_case.rag.enable_cache",
            True,
            env="BUG_TO_CASE_RAG_ENABLE_CACHE",
        )
        self._cache_max_entries = max(
            1,
            cfg_int(
                "bug_to_case.rag.cache_max_entries",
                256,
                env="BUG_TO_CASE_RAG_CACHE_MAX_ENTRIES",
            ),
        )
        self._verbose_doc_logs = cfg_bool(
            "bug_to_case.rag.verbose_doc_logs",
            False,
            env="BUG_TO_CASE_RAG_VERBOSE_DOC_LOGS",
        )
        self._include_graph_response_text = cfg_bool(
            "bug_to_case.rag.include_graph_response_text",
            False,
            env="BUG_TO_CASE_RAG_INCLUDE_GRAPH_RESPONSE_TEXT",
        )
        self._max_rag_queries = max(
            1,
            cfg_int(
                "bug_to_case.rag.max_rag_queries",
                5,
                env="BUG_TO_CASE_MAX_RAG_QUERIES",
            ),
        )
        self._max_graph_queries_per_retrieve = max(
            1,
            cfg_int(
                "bug_to_case.rag.max_graph_queries_per_retrieve",
                4,
                env="BUG_TO_CASE_MAX_GRAPH_QUERIES_PER_RETRIEVE",
            ),
        )
        self._graph_timeout_cooldown_seconds = max(
            0.0,
            cfg_float(
                "bug_to_case.rag.graph_timeout_cooldown_seconds",
                120.0,
                env="BUG_TO_CASE_GRAPH_TIMEOUT_COOLDOWN_SECONDS",
            ),
        )
        self._graph_timeout_until_by_query: Dict[str, float] = {}
        self._retrieve_cache: Dict[
            Tuple[Any, ...], Tuple[str, Dict[str, Any], Dict[str, Any]]
        ] = {}
        self._graphrag_context_cache: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}

    def _build_cache_key(
        self,
        *,
        query: str,
        top_k_retrieval: int,
        top_k_final: int,
        use_graphrag: bool,
        decomposition_result: Dict[str, Any],
        document_category_filter: Optional[str],
        category_whitelist: Optional[List[str]],
        function_hierarchy_prefix: Optional[str] = None,
    ) -> Tuple[Any, ...]:
        try:
            decomp_key = json.dumps(decomposition_result or {}, sort_keys=True, ensure_ascii=False)
        except Exception:
            decomp_key = repr(decomposition_result)
        return (
            query,
            top_k_retrieval,
            top_k_final,
            use_graphrag,
            decomp_key,
            document_category_filter or "",
            tuple(category_whitelist or []),
            function_hierarchy_prefix or "",
        )

    def _cache_get(
        self, key: Tuple[Any, ...]
    ) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
        if not self._cache_enabled:
            return None
        return self._retrieve_cache.get(key)

    def _cache_set(
        self,
        key: Tuple[Any, ...],
        value: Tuple[str, Dict[str, Any], Dict[str, Any]],
    ) -> None:
        if not self._cache_enabled:
            return
        if len(self._retrieve_cache) >= self._cache_max_entries:
            # 简单 FIFO 弹出最早写入项，保持实现轻量且可预测。
            oldest_key = next(iter(self._retrieve_cache))
            self._retrieve_cache.pop(oldest_key, None)
        self._retrieve_cache[key] = value

    @staticmethod
    def _normalize_query_key(query: str) -> str:
        return re.sub(r"\s+", " ", (query or "")).strip().lower()

    def retrieve(
        self,
        query: str,
        top_k_retrieval: int = 50,
        top_k_final: int = 8,
        use_graphrag: bool = True,
        decomposition_result: Optional[Dict[str, Any]] = None,
        document_category_filter: Optional[str] = None,
        category_whitelist: Optional[List[str]] = None,
        function_hierarchy_prefix: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        执行统一 RAG 检索（GraphRAG 优先）。

        Args:
            query: 主查询字符串（job_content）
            top_k_retrieval: 每种检索方式的初始候选数
            top_k_final: 最终返回文档数（默认 8，覆盖更多命令类型）
            use_graphrag: 是否使用 GraphRAG
            decomposition_result: 任务分解结果（含 rag_queries）
            document_category_filter: 单个文档分类过滤（如 "test/test_list"）— 旧接口
            category_whitelist: 允许的文档分类列表 — 新接口，硬过滤

        Returns:
            (context 文本, constraints 约束字典, decomposition_result)
        """
        decomposition_result = decomposition_result or {}
        cache_key = self._build_cache_key(
            query=query,
            top_k_retrieval=top_k_retrieval,
            top_k_final=top_k_final,
            use_graphrag=use_graphrag,
            decomposition_result=decomposition_result,
            document_category_filter=document_category_filter,
            category_whitelist=category_whitelist,
            function_hierarchy_prefix=function_hierarchy_prefix,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.info("[UnifiedRAG] 缓存命中: query='%s...'", query[:40])
            return cached[0], dict(cached[1]), dict(cached[2])

        # 构建查询列表：rag_queries 精确查询 + 主 query 宽泛查询
        queries = self._build_query_list(query, decomposition_result)
        logger.info(
            "[UnifiedRAG] 执行 %d 条查询检索: %s",
            len(queries),
            [q[:60] + "..." if len(q) > 60 else q for q in queries],
        )

        all_candidates: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        # ── 并行检索: GraphRAG + 向量检索 ──────────────────────────
        _do_graph = (
            use_graphrag
            and self.graphrag_retriever
            and getattr(self.graphrag_retriever, "is_available", lambda: False)()
        )

        def _run_graph_queries() -> List[Dict[str, Any]]:
            """在独立线程中执行所有 GraphRAG 查询。"""
            results: List[Dict[str, Any]] = []
            graph_queries = queries[: self._max_graph_queries_per_retrieve]
            loop = asyncio.new_event_loop()
            try:
                for q in graph_queries:
                    q_key = self._normalize_query_key(q)
                    now = time.time()
                    cooldown_until = self._graph_timeout_until_by_query.get(q_key, 0.0)
                    if now < cooldown_until:
                        logger.info(
                            "[UnifiedRAG] GraphRAG 冷却中，跳过 query='%s...' (remaining=%.1fs)",
                            q[:40], cooldown_until - now,
                        )
                        continue
                    try:
                        graph_docs = loop.run_until_complete(
                            self._fetch_graph(q, min(top_k_retrieval // 2, 20))
                        )
                        for g in graph_docs:
                            if g.get("text"):
                                g["_source_type"] = "graphrag"
                                results.append(g)
                    except Exception as e:
                        logger.warning(
                            "[UnifiedRAG] GraphRAG 检索失败 (query='%s...'): %s",
                            q[:40], e,
                        )
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()
            if len(queries) > len(graph_queries):
                logger.info(
                    "[UnifiedRAG] GraphRAG 查询已截断: %d -> %d",
                    len(queries), len(graph_queries),
                )
            return results

        def _run_vector_queries() -> List[Dict[str, Any]]:
            """在独立线程中执行所有向量查询。"""
            results: List[Dict[str, Any]] = []
            for q in queries:
                try:
                    hybrid_kwargs = _adaptive_hybrid_query_kwargs(q)
                    result = self.hybrid_retriever.query(
                        q,
                        top_k=top_k_retrieval,
                        return_detailed_info=True,
                        **hybrid_kwargs,
                    )
                    retrieved = result.get("Retrieved Context", [])
                    for doc in retrieved:
                        if isinstance(doc, dict):
                            text = (
                                doc.get("text")
                                or doc.get("page_content")
                                or doc.get("content")
                                or ""
                            )
                        else:
                            text = str(doc)
                        if text:
                            candidate = {
                                "text": text,
                                "metadata": (
                                    doc.get("metadata")
                                    or _parse_metadata_from_text(text)
                                )
                                if isinstance(doc, dict)
                                else _parse_metadata_from_text(text),
                                "similarity score": _get_score(doc)
                                if isinstance(doc, dict)
                                else 0.0,
                                "_source_type": "vector",
                            }
                            results.append(candidate)
                except Exception as e:
                    logger.warning(
                        "[UnifiedRAG] 向量检索失败 (query='%s...'): %s",
                        q[:40], e,
                    )
            return results

        # 并行执行（总延迟 = max(GraphRAG, Vector) 而非 sum）
        from concurrent.futures import ThreadPoolExecutor

        graph_candidates: List[Dict[str, Any]] = []
        vector_candidates: List[Dict[str, Any]] = []

        if _do_graph:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag") as pool:
                fut_graph = pool.submit(_run_graph_queries)
                fut_vector = pool.submit(_run_vector_queries)
                try:
                    graph_candidates = fut_graph.result(timeout=120)
                except Exception as e:
                    logger.warning("[UnifiedRAG] GraphRAG 线程异常: %s", e)
                try:
                    vector_candidates = fut_vector.result(timeout=120)
                except Exception as e:
                    logger.warning("[UnifiedRAG] 向量检索线程异常: %s", e)
        else:
            # GraphRAG 不可用时，单线程跑向量检索即可
            vector_candidates = _run_vector_queries()

        # 合并去重
        for g in graph_candidates:
            h = hash((g.get("text") or "")[:300])
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_candidates.append(g)
        for v in vector_candidates:
            h = hash((v.get("text") or "")[:300])
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_candidates.append(v)

        logger.info(
            "[UnifiedRAG] 合并后共 %d 条唯一候选 (GraphRAG=%d, 向量=%d)%s",
            len(all_candidates),
            sum(1 for d in all_candidates if d.get("_source_type") == "graphrag"),
            sum(1 for d in all_candidates if d.get("_source_type") == "vector"),
            " [并行]" if _do_graph else "",
        )

        if not all_candidates:
            return "", {}, decomposition_result

        # 3. 归一化为统一格式 {text, metadata, similarity score}
        documents = []
        for doc in all_candidates:
            text = doc.get("text") or ""
            if not text:
                continue
            meta = doc.get("metadata") or _parse_metadata_from_text(text)
            documents.append({
                "text": text,
                "metadata": meta,
                "similarity score": _get_score(doc),
                "_source_type": doc.get("_source_type", "unknown"),
            })

        # 3.5 分类白名单硬过滤 — 只保留 document_category 在白名单中的文档
        if category_whitelist and documents:
            docs_before_whitelist = list(documents)
            allow_uncategorized_docs = cfg_bool(
                "bug_to_case.rag.allow_uncategorized_docs",
                False,
                env="BUG_TO_CASE_RAG_ALLOW_UNCATEGORIZED_DOCS",
            )
            whitelist_set = set(category_whitelist)
            # 提取每级前缀也纳入匹配（如 "spec/prd" → "spec" 也算命中）
            whitelist_prefixes = {c.split("/")[0] for c in category_whitelist if "/" in c}
            before_count = len(documents)
            filtered = []
            for doc in documents:
                meta = doc.get("metadata") or {}
                if meta.get("_graphrag_synthesized"):
                    filtered.append(doc)
                    continue
                doc_cat = meta.get("document_category", "")
                if not doc_cat:
                    regex_meta = meta.get("regex_metadata") or {}
                    doc_cat = regex_meta.get("document_category", "")
                if not doc_cat:
                    doc_cat = _parse_metadata_from_text(doc.get("text", "")).get("document_category", "")
                if doc_cat in whitelist_set:
                    filtered.append(doc)
                elif doc_cat and "/" in doc_cat and doc_cat.split("/")[0] in whitelist_prefixes:
                    filtered.append(doc)
                elif not doc_cat and allow_uncategorized_docs:
                    filtered.append(doc)
            documents = filtered
            logger.info(
                "[UnifiedRAG] 分类白名单硬过滤: %d -> %d (whitelist=%s, allow_uncategorized=%s)",
                before_count, len(documents), category_whitelist, allow_uncategorized_docs,
            )
            if not documents:
                logger.warning(
                    "[UnifiedRAG] 白名单过滤后结果为空 (query=%s, whitelist=%s)，返回空",
                    query[:80], category_whitelist,
                )
                return "", {}, decomposition_result

        # 4. Rerank（用主 query 做 rerank）
        if self.reranker and documents:
            try:
                logger.info(
                    "[UnifiedRAG] Rerank %d 条 -> top %d",
                    len(documents), top_k_final,
                )
                documents = self.reranker.query(
                    query=query,
                    retrieved_result=documents,
                    top_k=top_k_final,
                )
            except Exception as e:
                logger.warning(
                    "[UnifiedRAG] Rerank 失败: %s，使用分数排序",
                    e,
                )
                documents = sorted(
                    documents,
                    key=lambda x: _get_score(x),
                    reverse=True,
                )[:top_k_final]
        else:
            documents = sorted(
                documents,
                key=lambda x: _get_score(x),
                reverse=True,
            )[:top_k_final]

        # 5. 协议加权并重排
        if self.use_protocol_boost and documents:
            _apply_protocol_boost(
                query, documents, use_scoring_utils=_USE_SCORING_UTILS
            )
            documents = sorted(
                documents,
                key=lambda x: _get_score(x),
                reverse=True,
            )

        # 5.5 文档分类加权（可选）
        if document_category_filter and documents:
            _apply_category_filter(documents, document_category_filter)
            documents = sorted(
                documents,
                key=lambda x: _get_score(x),
                reverse=True,
            )

        # 5.6 function_hierarchy 子功能过滤（防止同产品模块内交叉污染）
        if function_hierarchy_prefix and documents:
            _apply_hierarchy_boost(documents, function_hierarchy_prefix)
            documents = sorted(
                documents,
                key=lambda x: _get_score(x),
                reverse=True,
            )

        # 5.8 低分截断 + 证据一致性过滤（抑制噪声与污染）
        if documents:
            min_score = cfg_float(
                "bug_to_case.rag.min_score",
                0.08,
                env="BUG_TO_CASE_RAG_MIN_SCORE",
            )
            before_count = len(documents)
            quality_filtered = [
                doc
                for doc in documents
                if _get_score(doc) >= min_score and _is_doc_consistent(query, doc)
            ]
            # 兜底：避免阈值过严导致空结果
            if not quality_filtered:
                quality_filtered = [
                    doc for doc in documents if _is_doc_consistent(query, doc)
                ][: max(1, min(3, top_k_final))]
            if quality_filtered:
                documents = quality_filtered
            logger.info(
                "[UnifiedRAG] 质量过滤: %d -> %d (min_score=%.2f)",
                before_count,
                len(documents),
                min_score,
            )

        # 6. 构建上下文与约束
        final_docs = documents[:top_k_final]
        texts = [
            d.get("text", "")[:1500]
            for d in final_docs
            if d.get("text")
        ]
        context = "\n\n".join(texts)
        metadata_list = [d.get("metadata", {}) for d in final_docs]
        constraints = _build_constraints_from_metadata(metadata_list)

        # 6.5 RetrievalConfidence 计算
        scores = [_get_score(d) for d in final_docs]
        query_tokens = set(query.lower().split())
        all_text_lower = context.lower()
        matched_tokens = sum(1 for t in query_tokens if t in all_text_lower) if query_tokens else 0
        has_graphrag = any(d.get("_source_type") == "graphrag" for d in final_docs)
        confidence = RetrievalConfidence(
            top_score=scores[0] if scores else 0.0,
            score_spread=(scores[0] - scores[min(4, len(scores) - 1)]) if len(scores) >= 2 else 0.0,
            graphrag_contributed=has_graphrag,
            keyword_coverage=matched_tokens / len(query_tokens) if query_tokens else 0.0,
            result_count=len(final_docs),
        )
        constraints["_retrieval_confidence"] = confidence.to_dict()

        # 文档详情日志默认关闭以减少 I/O 开销；统计日志仍保留。
        if self._verbose_doc_logs:
            for i, doc in enumerate(final_docs):
                src = doc.get("_source_type", "?")
                score = _get_score(doc)
                text_preview = (doc.get("text") or "")[:80].replace("\n", " ")
                logger.info(
                    "[UnifiedRAG] 最终 #%d [%s] score=%.4f: %s",
                    i + 1, src, score, text_preview,
                )

        output = (context, constraints, decomposition_result)
        self._cache_set(cache_key, output)
        return output

    # 触发内容匹配健康检查补丁查询的关键词
    _CONTENT_HEALTH_KEYWORDS = [
        "内容", "关键词", "字符串匹配", "content", "keyword", "string match",
        "响应内容", "页面内容", "chinamobile", "content-based", "web页面关键词",
        "content_based_health_check", "string_match_health_check",
    ]
    # 内容匹配健康检查的专用补丁查询（保证 health request/response/server 被检索到）
    _CONTENT_HEALTH_PATCH_QUERIES = [
        "health request health response health server HTTP关键词内容匹配健康检查三步配置命令",
        "Web页面关键词检查 health response期望响应内容 health request自定义GET请求 health server绑定命令示例",
    ]

    def _build_query_list(
        self,
        main_query: str,
        decomposition_result: Dict[str, Any],
    ) -> List[str]:
        """
        构建查询列表：
        1. 优先使用 rag_queries 中的精确查询（按 priority 排序）
        2. 自动检测内容匹配健康检查场景，注入专用补丁查询（保证 health request/response/server 被检索）
        3. 主 query（job_content）作为补充
        
        每条 rag_query 将被单独执行检索，确保精确命中。
        """
        queries: List[str] = []
        normalized_seen = set()
        rag_queries = decomposition_result.get("rag_queries", [])

        # 按优先级排序的 rag_queries
        if rag_queries:
            sorted_queries = sorted(
                rag_queries,
                key=lambda q: q.get("priority", 99),
            )
            for rq in sorted_queries:
                q_text = rq.get("query", "").strip()
                norm = re.sub(r"\s+", " ", q_text).strip().lower()
                if q_text and norm not in normalized_seen:
                    normalized_seen.add(norm)
                    queries.append(q_text)
                if len(queries) >= self._max_rag_queries:
                    break

        # 自动检测内容匹配健康检查场景，注入 health request/response/server 专用补丁查询
        advanced = decomposition_result.get("advanced_features", [])
        adv_str = " ".join(str(a) for a in advanced).lower() if isinstance(advanced, list) else str(advanced).lower()
        check_corpus = (main_query or "").lower() + " " + adv_str + " " + " ".join(q.lower() for q in queries)

        if any(kw.lower() in check_corpus for kw in self._CONTENT_HEALTH_KEYWORDS):
            has_health_sub_query = any(
                any(kw in q.lower() for kw in ["health request", "health response", "health server", "三步"])
                for q in queries
            )
            if not has_health_sub_query:
                logger.info(
                    "[UnifiedRAG] 检测到内容匹配健康检查场景，注入 2 条 health request/response/server 专用查询"
                )
                for patch_q in reversed(self._CONTENT_HEALTH_PATCH_QUERIES):
                    norm = re.sub(r"\s+", " ", patch_q).strip().lower()
                    if norm not in normalized_seen:
                        normalized_seen.add(norm)
                        queries.insert(0, patch_q)

        # 主 query 作为保底
        main_norm = re.sub(r"\s+", " ", (main_query or "")).strip().lower()
        if main_query and main_norm and main_norm not in normalized_seen:
            normalized_seen.add(main_norm)
            if len(queries) < 2:
                queries.insert(0, main_query)
            else:
                queries.append(main_query)
        if len(queries) > self._max_rag_queries:
            queries = queries[: self._max_rag_queries]
        return queries if queries else [main_query or ""]

    async def _fetch_graph(
        self, query: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """异步拉取 GraphRAG 本地搜索结果"""
        if not self.graphrag_retriever:
            return []
        cache_key = (self._normalize_query_key(query), top_k)
        cached = self._graphrag_context_cache.get(cache_key)
        if cached is not None:
            return cached
        out: List[Dict[str, Any]] = []
        try:
            timeout_seconds = cfg_float(
                "bug_to_case.rag.graph_query_timeout_seconds",
                45.0,
                env="BUG_TO_CASE_GRAPH_QUERY_TIMEOUT_SECONDS",
            )
            graph_results = await asyncio.wait_for(
                self.graphrag_retriever.local_context_build(query, top_k=top_k),
                timeout=timeout_seconds,
            )
            for r in graph_results:
                text = getattr(r, "text", "")
                if text:
                    out.append({
                        "text": text,
                        "metadata": getattr(r, "metadata", {}),
                        "similarity score": getattr(r, "score", 0.5),
                    })
        except asyncio.TimeoutError:
            logger.warning("[UnifiedRAG] GraphRAG local_search 超时: query='%s...'", query[:40])
            if self._graph_timeout_cooldown_seconds > 0:
                q_key = self._normalize_query_key(query)
                self._graph_timeout_until_by_query[q_key] = (
                    time.time() + self._graph_timeout_cooldown_seconds
                )
        except Exception as e:
            logger.warning("[UnifiedRAG] GraphRAG local_search 异常: %s", e, exc_info=True)
        if len(self._graphrag_context_cache) >= self._cache_max_entries:
            self._graphrag_context_cache.pop(next(iter(self._graphrag_context_cache)))
        self._graphrag_context_cache[cache_key] = out
        return out
