# -*- coding: utf-8 -*-
"""
Workforce 配置相关 RAG 与检索逻辑。

提供：查询增强、metadata 过滤、自适应 RAG 检索、步骤覆盖分析等。
与 UnifiedRAGRetriever / GraphRAG 配合使用；当主流程要求必须使用 GraphRAG 时，
由 workflow_config_generator 使用 UnifiedRAGRetriever（内含 GraphRAG），
本模块的 _adaptive_rag_retrieval 可作为回退或供其他脚本调用。
"""
import json
import logging
from typing import Dict, Any, List, Optional

from camel.retrievers import HybridRetriever

logger = logging.getLogger(__name__)


def _build_enhanced_query(
    base_query: str,
    decomposition_result: Optional[Dict[str, Any]] = None
) -> str:
    """
    构建增强的查询，参考 GraphRAG 的查询增强方式。
    在查询中明确包含 metadata 信息，提高检索精度。
    """
    enhanced_query = base_query
    if decomposition_result:
        product_modules = decomposition_result.get("product_modules", [])
        if product_modules:
            enhanced_query += f" [产品模块: {', '.join(product_modules)}]"
        protocol_type = decomposition_result.get("protocol_type", "")
        if protocol_type:
            enhanced_query += f" [协议类型: {protocol_type}]"
        scenario_id = decomposition_result.get("scenario_id", "")
        if scenario_id and scenario_id != "unknown":
            enhanced_query += f" [场景: {scenario_id}]"
        step_type = decomposition_result.get("step_type", "")
        if step_type:
            enhanced_query += f" [步骤类型: {step_type}]"
    return enhanced_query


def _should_include_result(
    metadata: Dict[str, Any],
    decomposition_result: Optional[Dict[str, Any]] = None,
    filter_mode: str = "strict"
) -> bool:
    """判断是否应该包含该检索结果（参考 GraphRAG 的 metadata 过滤）。"""
    if not decomposition_result or filter_mode == "none":
        return True
    result_module = metadata.get("product_module", "")
    required_modules = decomposition_result.get("product_modules", [])
    if required_modules:
        if filter_mode == "strict":
            if not result_module or result_module not in required_modules:
                return False
        elif result_module and result_module not in required_modules:
            return False
    result_protocols = metadata.get("protocol_type", [])
    if not isinstance(result_protocols, list):
        result_protocols = [result_protocols] if result_protocols else []
    required_protocol = decomposition_result.get("protocol_type", "")
    if required_protocol:
        if filter_mode == "strict":
            if not result_protocols or required_protocol not in result_protocols:
                return False
        elif result_protocols and required_protocol not in result_protocols:
            return False
    if filter_mode == "strict":
        result_scenario = metadata.get("scenario_id", "")
        required_scenario = decomposition_result.get("scenario_id", "")
        if required_scenario and required_scenario != "unknown":
            if result_scenario and result_scenario != required_scenario:
                return False
    return True


def _retrieve_context(
    hybrid_retriever: Any,
    reranker: Any,
    query: str,
    decomposition_result: Optional[Dict[str, Any]] = None,
    top_k_retrieval: int = 10,
    top_k_rerank: int = 2,
    max_snippet_chars: int = 300,
    max_context_chars: int = 800,
    filter_mode: str = "strict",
) -> tuple:
    """检索上下文并返回 (context, constraints)。"""
    enhanced_query = _build_enhanced_query(query, decomposition_result)
    if enhanced_query != query:
        logger.info("[_retrieve_context] 查询增强: %s -> %s", query, enhanced_query)

    vec_results = []
    bm25_results = []
    try:
        vec_results = hybrid_retriever.vr.query(enhanced_query, top_k=top_k_retrieval * 2)
    except Exception as e:
        logger.warning("向量检索失败: %s", e)
    try:
        bm25_results = hybrid_retriever.bm25.query(enhanced_query, top_k=top_k_retrieval * 2)
    except Exception as e:
        logger.warning("BM25检索失败: %s", e)

    def _infer_meta_from_text(text: str) -> Dict[str, Any]:
        if not isinstance(text, str) or "INAGENT_META_JSON:" not in text:
            return {}
        first_line = text.splitlines()[0].strip()
        if not first_line.startswith("INAGENT_META_JSON:"):
            return {}
        payload = first_line[len("INAGENT_META_JSON:"):].strip()
        if not payload:
            return {}
        try:
            if payload[0] != "{":
                start = payload.find("{")
                if start == -1:
                    return {}
                payload = payload[start:]
            end = payload.find("}")
            if end == -1:
                return {}
            obj = json.loads(payload[: end + 1])
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}
        if "protocol_type" in obj and not isinstance(obj.get("protocol_type"), list):
            obj["protocol_type"] = [obj["protocol_type"]]
        return obj

    def _flatten_metadata(meta: Dict[str, Any], text: str) -> Dict[str, Any]:
        if not isinstance(meta, dict):
            meta = {}
        nested = meta.get("regex_metadata")
        if isinstance(nested, dict):
            merged = dict(meta)
            merged.update(nested)
            meta = merged
        inferred = _infer_meta_from_text(text)
        if inferred:
            merged2 = dict(meta)
            merged2.update(inferred)
            return merged2
        return meta

    candidates = []
    seen_texts = set()
    for r in vec_results + bm25_results:
        text = r.get("text", "") if isinstance(r, dict) else str(r)
        metadata = r.get("metadata", {}) if isinstance(r, dict) else {}
        metadata = _flatten_metadata(metadata, text)
        if _should_include_result(metadata, decomposition_result, filter_mode):
            if text and text not in seen_texts:
                candidates.append({"text": text, "metadata": metadata})
                seen_texts.add(text)

    if len(candidates) == 0 and filter_mode == "strict":
        logger.warning("[_retrieve_context] 严格过滤后无结果，放宽过滤条件")
        candidates = []
        seen_texts = set()
        for r in vec_results + bm25_results:
            text = r.get("text", "") if isinstance(r, dict) else str(r)
            metadata = r.get("metadata", {}) if isinstance(r, dict) else {}
            metadata = _flatten_metadata(metadata, text)
            if _should_include_result(metadata, decomposition_result, "relaxed"):
                if text and text not in seen_texts:
                    candidates.append({"text": text, "metadata": metadata})
                    seen_texts.add(text)

    if not candidates:
        return "", {"config_modes": [], "required_keywords": [], "intents": []}

    try:
        reranked = reranker.query(enhanced_query, candidates, top_k=top_k_rerank)
    except Exception as e:
        logger.warning("重排序失败: %s", e)
        reranked = candidates[:top_k_rerank]

    context_parts = []
    total_chars = 0
    for item in reranked:
        text = item.get("text", "") if isinstance(item, dict) else str(item)
        if not text:
            continue
        snippet = text[:max_snippet_chars]
        if total_chars + len(snippet) > max_context_chars:
            break
        context_parts.append(snippet)
        total_chars += len(snippet)
    context = "\n\n".join(context_parts)

    constraints = {"config_modes": [], "required_keywords": [], "intents": []}
    for item in reranked[:top_k_rerank]:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        if metadata.get("config_mode"):
            constraints["config_modes"].append(metadata["config_mode"])
        if metadata.get("required_keywords"):
            kw = metadata["required_keywords"]
            constraints["required_keywords"].extend(kw if isinstance(kw, list) else [kw])
        if metadata.get("intent"):
            constraints["intents"].append(metadata["intent"])
    constraints["config_modes"] = list(set(constraints["config_modes"]))
    constraints["required_keywords"] = list(set(constraints["required_keywords"]))
    constraints["intents"] = list(set(constraints["intents"]))
    return context, constraints


def _adaptive_rag_retrieval(
    hybrid_retriever: HybridRetriever,
    reranker: Any,
    job_content: str,
    decomposition_result: Optional[Dict[str, Any]] = None,
    top_k_retrieval: int = 10,
    top_k_rerank: int = 2,
    max_snippet_chars: int = 300,
    max_context_chars: int = 800,
) -> tuple:
    """
    自适应 RAG 检索：渐进式查询和动态任务调整。
    Returns:
        tuple: (context, constraints, adjusted_decomposition)
    """
    from INAGENT.utils.index_utils import generate_query_from_decomposition

    adjusted_decomposition = decomposition_result.copy() if decomposition_result else None
    overall_queries = generate_query_from_decomposition(decomposition_result) if decomposition_result else [job_content]
    overall_context = ""
    overall_constraints = {"config_modes": [], "required_keywords": [], "intents": []}
    overall_coverage = set()

    if overall_queries:
        try:
            overall_context, overall_constraints = _retrieve_context(
                hybrid_retriever, reranker, overall_queries[0],
                decomposition_result=decomposition_result,
                top_k_retrieval=top_k_retrieval, top_k_rerank=top_k_rerank,
                max_snippet_chars=max_snippet_chars, max_context_chars=max_context_chars,
                filter_mode="strict",
            )
            if overall_context and decomposition_result:
                overall_coverage = _analyze_step_coverage(overall_context, decomposition_result)
                required_steps = set(decomposition_result.get("required_steps", []))
                if required_steps and overall_coverage >= required_steps:
                    return overall_context, overall_constraints, adjusted_decomposition
        except Exception as e:
            logger.warning("[Adaptive RAG] 整体查询失败: %s", e)

    if not decomposition_result:
        context, constraints = _retrieve_context(
            hybrid_retriever, reranker, job_content, decomposition_result=None,
            top_k_retrieval=top_k_retrieval, top_k_rerank=top_k_rerank,
            max_snippet_chars=max_snippet_chars, max_context_chars=max_context_chars,
            filter_mode="none",
        )
        return context, constraints, None

    all_contexts = [overall_context] if overall_context else []
    all_constraints = [overall_constraints]
    covered_steps = overall_coverage.copy()
    rag_queries = sorted(decomposition_result.get("rag_queries", []), key=lambda x: x.get("priority", 5))

    for q_info in rag_queries:
        step_type = q_info.get("step_type")
        query_text = q_info.get("query")
        if (step_type and step_type in covered_steps) or not query_text:
            continue
        try:
            step_decomposition = decomposition_result.copy()
            if step_type:
                step_decomposition["step_type"] = step_type
            step_context, step_constraints = _retrieve_context(
                hybrid_retriever, reranker, query_text, decomposition_result=step_decomposition,
                top_k_retrieval=top_k_retrieval, top_k_rerank=top_k_rerank,
                max_snippet_chars=max_snippet_chars, max_context_chars=max_context_chars,
                filter_mode="strict",
            )
            if step_context:
                all_contexts.append(step_context)
                all_constraints.append(step_constraints)
                step_coverage = _analyze_step_coverage(step_context, decomposition_result)
                covered_steps.update(step_coverage)
                if step_type:
                    covered_steps.add(step_type)
                if adjusted_decomposition and step_coverage - covered_steps:
                    remaining = [s for s in adjusted_decomposition.get("required_steps", []) if s not in covered_steps]
                    adjusted_decomposition["required_steps"] = remaining
        except Exception as e:
            logger.warning("[Adaptive RAG] 查询步骤 %s 失败: %s", step_type, e)

    merged_context = "\n\n".join([c for c in all_contexts if c])
    merged_constraints = {"config_modes": set(), "required_keywords": set(), "intents": set()}
    for d in all_constraints:
        merged_constraints["config_modes"].update(d.get("config_modes", []))
        merged_constraints["required_keywords"].update(d.get("required_keywords", []))
        merged_constraints["intents"].update(d.get("intents", []))
    merged_constraints = {
        "config_modes": sorted(merged_constraints["config_modes"]),
        "required_keywords": sorted(merged_constraints["required_keywords"]),
        "intents": sorted(merged_constraints["intents"]),
    }
    return merged_context, merged_constraints, adjusted_decomposition


def _analyze_step_coverage(
    context: str,
    decomposition_result: Optional[Dict[str, Any]] = None,
) -> set:
    """分析上下文覆盖了哪些配置步骤。"""
    if not decomposition_result:
        return set()
    from INAGENT.utils.index_utils import get_step_type_keywords, get_advanced_feature_keywords
    step_keywords = get_step_type_keywords()
    feature_keywords = get_advanced_feature_keywords()
    covered = set()
    context_lower = context.lower()
    for step_type in decomposition_result.get("required_steps", []):
        for keyword in step_keywords.get(step_type, []):
            if keyword.lower() in context_lower:
                covered.add(step_type)
                break
    for feature in decomposition_result.get("advanced_features", []):
        for keyword in feature_keywords.get(feature, []):
            if keyword.lower() in context_lower:
                if "policies_and_algorithms" in decomposition_result.get("required_steps", []):
                    covered.add("policies_and_algorithms")
                break
    return covered
