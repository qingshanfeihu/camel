# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
统一 RAG 机制：GraphRAG 优先 + 向量检索 + Rerank + 协议加权

将 GraphRAG 图检索、向量检索、Rerank 重排序、协议加权整合为单一流水线，
供 workflow_config_generator 与 graphrag_integration 共用。

流水线顺序（GraphRAG 优先）：
1. GraphRAG 图检索（实体/关系/社区报告 + text_units）
2. 向量检索（BM25 + 语义混合）
3. 合并去重
4. Rerank 重排序
5. 协议加权并重排
6. 构建上下文与约束

支持 rag_queries 多查询合并检索：将任务分解生成的精确查询分别执行，合并后统一 rerank。

参考：
- https://microsoft.github.io/graphrag/config/yaml/
- https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/
"""
import re
import json
import asyncio
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

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
        # 从 metadata 或 INAGENT_META_JSON 获取文档分类
        doc_cat = meta.get("document_category", "")
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

    def retrieve(
        self,
        query: str,
        top_k_retrieval: int = 50,
        top_k_final: int = 8,
        use_graphrag: bool = True,
        decomposition_result: Optional[Dict[str, Any]] = None,
        document_category_filter: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        执行统一 RAG 检索（GraphRAG 优先）。

        Args:
            query: 主查询字符串（job_content）
            top_k_retrieval: 每种检索方式的初始候选数
            top_k_final: 最终返回文档数（默认 8，覆盖更多命令类型）
            use_graphrag: 是否使用 GraphRAG
            decomposition_result: 任务分解结果（含 rag_queries）
            document_category_filter: 文档分类过滤（如 "test/test_list", "spec/func_spec"）

        Returns:
            (context 文本, constraints 约束字典, decomposition_result)
        """
        decomposition_result = decomposition_result or {}

        # 构建查询列表：rag_queries 精确查询 + 主 query 宽泛查询
        queries = self._build_query_list(query, decomposition_result)
        logger.info(
            "[UnifiedRAG] 执行 %d 条查询检索: %s",
            len(queries),
            [q[:60] + "..." if len(q) > 60 else q for q in queries],
        )

        all_candidates: List[Dict[str, Any]] = []
        seen_hashes: set = set()

        # 1. GraphRAG 图检索（优先执行，每条 query 分别检索）
        if use_graphrag and self.graphrag_retriever and getattr(
            self.graphrag_retriever, "is_available", lambda: False
        )():
            for q in queries:
                try:
                    graph_docs = asyncio.run(
                        self._fetch_graph(q, min(top_k_retrieval // 2, 20))
                    )
                    for g in graph_docs:
                        h = hash((g.get("text") or "")[:300])
                        if h not in seen_hashes and g.get("text"):
                            seen_hashes.add(h)
                            g["_source_type"] = "graphrag"
                            all_candidates.append(g)
                except Exception as e:
                    logger.warning(
                        "[UnifiedRAG] GraphRAG 检索失败 (query='%s...'): %s",
                        q[:40], e,
                    )

            if all_candidates:
                logger.info(
                    "[UnifiedRAG] GraphRAG 共检索 %d 条唯一候选",
                    len(all_candidates),
                )

        # 2. 向量检索（每条 query 分别检索）
        for q in queries:
            try:
                result = self.hybrid_retriever.query(
                    q,
                    top_k=top_k_retrieval,
                    return_detailed_info=True,
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
                    h = hash(text[:300])
                    if h not in seen_hashes and text:
                        seen_hashes.add(h)
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
                        all_candidates.append(candidate)
            except Exception as e:
                logger.warning(
                    "[UnifiedRAG] 向量检索失败 (query='%s...'): %s",
                    q[:40], e,
                )

        logger.info(
            "[UnifiedRAG] 合并后共 %d 条唯一候选 (GraphRAG=%d, 向量=%d)",
            len(all_candidates),
            sum(1 for d in all_candidates if d.get("_source_type") == "graphrag"),
            sum(1 for d in all_candidates if d.get("_source_type") == "vector"),
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

        # 日志：展示最终返回的文档摘要
        for i, doc in enumerate(final_docs):
            src = doc.get("_source_type", "?")
            score = _get_score(doc)
            text_preview = (doc.get("text") or "")[:80].replace("\n", " ")
            logger.info(
                "[UnifiedRAG] 最终 #%d [%s] score=%.4f: %s",
                i + 1, src, score, text_preview,
            )

        return context, constraints, decomposition_result

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
        rag_queries = decomposition_result.get("rag_queries", [])

        # 按优先级排序的 rag_queries
        if rag_queries:
            sorted_queries = sorted(
                rag_queries,
                key=lambda q: q.get("priority", 99),
            )
            for rq in sorted_queries:
                q_text = rq.get("query", "").strip()
                if q_text and q_text not in queries:
                    queries.append(q_text)

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
                    if patch_q not in queries:
                        queries.insert(0, patch_q)

        # 主 query 作为保底
        if main_query and main_query not in queries:
            if len(queries) < 2:
                queries.insert(0, main_query)
            else:
                queries.append(main_query)

        return queries if queries else [main_query or ""]

    async def _fetch_graph(
        self, query: str, top_k: int
    ) -> List[Dict[str, Any]]:
        """异步拉取 GraphRAG 本地搜索结果"""
        out: List[Dict[str, Any]] = []
        if not self.graphrag_retriever:
            return out
        try:
            response_text, graph_results = (
                await self.graphrag_retriever.local_search(query, top_k=top_k)
            )
            for r in graph_results:
                text = getattr(r, "text", "")
                if text:
                    out.append({
                        "text": text,
                        "metadata": getattr(r, "metadata", {}),
                        "similarity score": getattr(r, "score", 0.5),
                    })
            # 如果 local_search 的 response_text 有价值（LLM 综合回答），也加入候选
            if response_text and len(response_text) > 50 and not response_text.startswith("I am sorry"):
                out.append({
                    "text": response_text[:2000],
                    "metadata": {"source_type": "graphrag_response"},
                    "similarity score": 0.6,
                })
        except Exception as e:
            logger.debug("[UnifiedRAG] GraphRAG local_search 异常: %s", e)
        return out
