# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Workflow: 输入需求生成配置

流程：
1. 读取jobs目录下的需求文件
2. 使用任务分解Agent分析需求
3. 使用RAG检索相关配置文档
4. 使用LB Ops Agent生成配置命令
5. 输出配置结果
"""
import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from camel.agents import ChatAgent
from camel.embeddings import OpenAICompatibleEmbedding
from camel.retrievers import HybridRetriever
from camel.storages import QdrantStorage
from camel.types import ModelPlatformType
from camel.models import ModelFactory
from qdrant_client import QdrantClient
from unstructured.documents.elements import Text, ElementMetadata

from INAGENT.utils import env_utils
from INAGENT.utils.workflow_logger import (
    setup_workflow_logger,
    get_workflow_logger,
    get_workflow_log_path,
    log_llm_input,
    log_llm_output,
    log_section,
)
from INAGENT.rag.rerank_retriever import SiliconFlowRerankRetriever
from INAGENT.utils.index_utils import load_function_structure_index
from INAGENT.agents.task_decomposition_agent import (
    build_task_decomposition_agent,
    build_task_decomposition_prompt,
)
from INAGENT.agents.lb_ops_agent import (
    build_lb_ops_agent,
    build_lb_ops_prompt,
)

# 尝试导入 GraphRAG 集成模块
try:
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever, HybridGraphRAGRetriever
    GRAPHRAG_AVAILABLE = True
except ImportError:
    GRAPHRAG_AVAILABLE = False

# 导入评分工具
try:
    from INAGENT.rag.scoring_utils import (
        apply_scoring_boost,
        extract_entities_from_query,
        compute_protocol_boost,
    )
    SCORING_UTILS_AVAILABLE = True
except ImportError:
    SCORING_UTILS_AVAILABLE = False

# 统一 RAG：向量 + GraphRAG + Rerank + 协议加权
try:
    from INAGENT.rag.unified_rag import UnifiedRAGRetriever
    UNIFIED_RAG_AVAILABLE = True
except ImportError:
    UNIFIED_RAG_AVAILABLE = False

# 尝试导入 fallback_retrieval（供回退或其他脚本使用）
try:
    from INAGENT.rag.fallback_retrieval import _adaptive_rag_retrieval, _analyze_step_coverage
except ImportError:
    # 如果workforce_config_ops不存在，定义一个改进的实现，支持metadata提取和动态协议加权
    def _compute_dynamic_protocol_boost(query: str, item: Dict[str, Any]) -> float:
        """
        动态计算协议类型加权，区别于 camel-ai 的硬编码方式
        
        核心差异：
        - camel-ai: 硬编码 HTTP/HTTPS 关键词和固定加权值
        - INFOAGEN: 从查询和文档 metadata 动态提取协议类型，自动匹配
        
        Args:
            query: 查询字符串
            item: 文档项，包含 text 和 metadata
        
        Returns:
            float: 加权值（正值表示增强，负值表示降权）
        """
        q_lower = (query or "").lower()
        
        # 从文档 metadata 和文本中动态提取协议类型
        meta = item.get("metadata", {}) or {}
        text = (item.get("text") or item.get("content") or "")[:500].lower()
        
        # 动态提取查询中的协议类型（不硬编码协议列表）
        query_protocols = set()
        doc_protocols = set()
        
        # 1. 从 metadata 中提取协议类型（INFOAGEN 特有的动态元数据）
        meta_protocols = meta.get("protocol_type", [])
        if isinstance(meta_protocols, list):
            doc_protocols.update([p.lower() for p in meta_protocols if p])
        elif meta_protocols:
            doc_protocols.add(meta_protocols.lower())
        
        # 2. 从文本中动态识别协议类型
        # 常见协议模式（但不硬编码，支持扩展）
        import re
        protocol_patterns = [
            r'\b(https?)\b',
            r'\b(tcp|udp)\b',
            r'\b(ftp|ftps)\b',
            r'\b(dns|dhcp)\b',
            r'\b(sip|rtsp)\b',
            r'\b(smtp|pop3|imap)\b'
        ]
        for pattern in protocol_patterns:
            # 从查询中提取
            query_matches = re.findall(pattern, q_lower)
            query_protocols.update(query_matches)
            # 从文档中提取
            doc_matches = re.findall(pattern, text)
            doc_protocols.update(doc_matches)
        
        # 3. 计算动态加权
        boost = 0.0
        
        # 精准匹配加权
        exact_matches = query_protocols & doc_protocols
        if exact_matches:
            boost += 0.05 * len(exact_matches)  # 每个精准匹配 +0.05
        
        # 部分匹配降权（查询要求但文档没有）
        missing_in_doc = query_protocols - doc_protocols
        if missing_in_doc and len(doc_protocols) > 0:
            # 如果文档有协议但不是查询要求的，轻微降权
            boost -= 0.03
        
        # 协议泛化支持（如 HTTP 应该也匹配 HTTPS）
        if "http" in query_protocols:
            if "https" in doc_protocols:
                boost += 0.02  # HTTPS 可以部分满足 HTTP 需求
        elif "https" in query_protocols:
            if "http" in doc_protocols:
                boost -= 0.02  # HTTP 不完全满足 HTTPS 需求（安全性差异）
        
        return boost
    
    def _adaptive_rag_retrieval(hybrid_retriever, reranker, job_content, decomposition_result=None, **kwargs):
        """
        改进的RAG检索实现，集成以下优化：
        1. 正确调用 reranker（修复 bug）
        2. 扩大候选池（默认 top_k × 10）
        3. 动态协议加权（区别于 camel-ai 硬编码）
        4. 支持 metadata 提取和过滤
        5. 可选 GraphRAG 图增强检索（合并向量+图结果）
        """
        import re
        import json
        import asyncio
        
        # 动态候选池策略：top_k_rerank × retrieval_multiplier
        top_k_rerank = kwargs.get("top_k_rerank", 5)
        retrieval_multiplier = kwargs.get("retrieval_multiplier", 10)  # 可配置，默认10倍
        top_k_retrieval = kwargs.get("top_k_retrieval") or (top_k_rerank * retrieval_multiplier)  # 默认：5 × 10 = 50
        graphrag_retriever = kwargs.get("graphrag_retriever")
        
        logger.info(f"[RAG] 使用动态候选池策略：初始检索 {top_k_retrieval} 个候选，rerank 后返回 top {top_k_rerank}")
        
        def _parse_metadata_from_text(text: str) -> Dict[str, Any]:
            """从文本中解析INAGENT_META_JSON格式的元数据"""
            match = re.match(r'^INAGENT_META_JSON:({.*?})\n', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    return {}
            return {}
        
        # 1. 混合检索（扩大候选池）
        try:
            retrieved_result = hybrid_retriever.query(
                job_content, 
                top_k=top_k_retrieval,
                return_detailed_info=True
            )
            retrieved_context = retrieved_result.get("Retrieved Context", [])
        except Exception as e:
            logger.warning(f"混合检索失败: {e}")
            retrieved_context = []
        
        # 1b. 可选：GraphRAG 图增强检索，合并结果
        if graphrag_retriever and getattr(graphrag_retriever, "is_available", lambda: False)():
            try:
                import asyncio
                _graphrag = graphrag_retriever
                async def _fetch_graph():
                    _, graph_results = await _graphrag.local_search(
                        job_content, top_k=min(top_k_retrieval // 2, 25)
                    )
                    out = []
                    for r in graph_results:
                        out.append({
                            "text": getattr(r, "text", ""),
                            "metadata": getattr(r, "metadata", {}),
                            "similarity score": getattr(r, "score", 0.0),
                        })
                    return out
                graph_docs = asyncio.run(_fetch_graph())
                if graph_docs:
                    seen = {hash((d.get("text", "") or "")[:300]): True for d in retrieved_context if isinstance(d, dict)}
                    for g in graph_docs:
                        t = (g.get("text", "") or "")[:300]
                        if t and hash(t) not in seen:
                            seen[hash(t)] = True
                            retrieved_context.append(g)
                    logger.info(f"[RAG] GraphRAG 合并 {len(graph_docs)} 个图结果，合并后共 {len(retrieved_context)} 个候选")
            except Exception as e:
                logger.warning(f"GraphRAG 检索失败: {e}，仅使用向量结果")
        
        if not retrieved_context:
            return "", {}, decomposition_result or {}
        
        # 2. 提取文本和 metadata
        documents = []
        for doc in retrieved_context:
            if isinstance(doc, dict):
                text = doc.get("text", "") or doc.get("page_content", "") or doc.get("content", "")
                metadata = doc.get("metadata", {})
                if not metadata and text:
                    metadata = _parse_metadata_from_text(text)
            else:
                text = str(doc)
                metadata = _parse_metadata_from_text(text) if text else {}
            
            if text:
                documents.append({
                    "text": text,
                    "metadata": metadata,
                    "similarity score": doc.get("similarity score", 0.0) if isinstance(doc, dict) else 0.0
                })
        
        # 3. 调用 reranker 重排序（修复：之前缺失这一步！）
        if reranker and documents:
            try:
                logger.info(f"[RAG] 调用 reranker 重排序 {len(documents)} 个文档，目标 top_{top_k_rerank}")
                reranked_docs = reranker.query(
                    query=job_content,
                    retrieved_result=documents,
                    top_k=top_k_rerank
                )
                documents = reranked_docs
                logger.info(f"[RAG] Rerank 完成，返回 {len(documents)} 个文档")
            except Exception as e:
                logger.warning(f"Rerank 失败: {e}，使用原始结果")
                documents = documents[:top_k_rerank]
        else:
            logger.warning(f"[RAG] Reranker 未启用或无文档，跳过 rerank")
            documents = documents[:top_k_rerank]
        
        # 4. 应用动态协议加权（区别于 camel-ai 的硬编码方式）
        for doc in documents:
            boost = _compute_dynamic_protocol_boost(job_content, doc)
            if boost != 0.0:
                original_score = doc.get("similarity score", 0.0)
                doc["similarity score"] = original_score + boost
                doc["protocol_boost"] = boost
                logger.debug(f"[RAG] 协议加权: {boost:+.3f} (原始: {original_score:.3f} -> {doc['similarity score']:.3f})")
        
        # 5. 重新排序（应用加权后）
        documents = sorted(documents, key=lambda x: x.get("similarity score", 0.0), reverse=True)
        
        # 6. 提取最终上下文
        texts = []
        metadata_list = []
        for doc in documents[:top_k_rerank]:
            text = doc.get("text", "")
            metadata = doc.get("metadata", {})
            if text:
                texts.append(text[:1000])
                metadata_list.append(metadata)
        
        context = "\n\n".join(texts)
        
        # 7. 从 metadata 中提取约束条件
        constraints = {}
        if metadata_list:
            product_modules = set()
            protocol_types = set()
            for meta in metadata_list:
                if meta.get("product_module"):
                    product_modules.add(meta["product_module"])
                if meta.get("protocol_type"):
                    if isinstance(meta["protocol_type"], list):
                        protocol_types.update(meta["protocol_type"])
                    else:
                        protocol_types.add(meta["protocol_type"])
            
            if product_modules:
                constraints["product_modules"] = list(product_modules)
            if protocol_types:
                constraints["protocol_types"] = list(protocol_types)
        
        return context, constraints, decomposition_result or {}
    
    def _analyze_step_coverage(*args, **kwargs):
        """占位函数"""
        return set()

# 加载环境变量
env_utils.load_inagent_env()

logger = logging.getLogger(__name__)


def _documents_to_elements(docs: list) -> list[Text]:
    """将文档转换为 Element 对象"""
    elements: list[Text] = []
    for idx, doc in enumerate(docs):
        text = getattr(doc, "page_content", "") if doc is not None else ""
        if not isinstance(text, str) or not text.strip():
            continue
        metadata = getattr(doc, "metadata", {}) or {}

        # 将关键元数据"写入文本头部"，确保即使底层检索器不回传metadata也能检索/过滤到正确域
        # 约定格式（首行）：INAGENT_META_JSON:{...json...}
        pm = metadata.get("product_module")
        pt = metadata.get("protocol_type")
        sid = metadata.get("scenario_id")
        st = metadata.get("step_type")
        dc = metadata.get("document_category")
        meta_obj = {}
        if pm and pm != "unknown":
            meta_obj["product_module"] = pm
        if pt:
            meta_obj["protocol_type"] = pt if isinstance(pt, list) else [pt]
        if sid and sid != "unknown":
            meta_obj["scenario_id"] = sid
        if st:
            meta_obj["step_type"] = st
        if dc:
            meta_obj["document_category"] = dc
        if meta_obj:
            # 单独一行JSON，避免与正文混淆，便于解析与BM25命中
            text = "INAGENT_META_JSON:" + json.dumps(meta_obj, ensure_ascii=False) + "\n" + text
        filename = (
            metadata.get("filename")
            or metadata.get("source_file")
            or metadata.get("source_pdf")
            or metadata.get("source")
        )
        element_metadata = ElementMetadata(
            filename=str(filename) if filename else None
        )
        page_idx = metadata.get("page_idx")
        if page_idx is not None:
            try:
                element_metadata.page_number = int(page_idx) + 1
            except (TypeError, ValueError):
                pass
        if metadata.get("section_title"):
            element_metadata.section_title = metadata.get("section_title")
        if metadata.get("block_id") is not None:
            element_metadata.block_id = metadata.get("block_id")
        if metadata.get("chunk_id") is not None:
            element_metadata.chunk_id = metadata.get("chunk_id")

        regex_metadata = {}
        # 重要：把检索/过滤真正需要的元数据透传进 element_metadata
        # 说明：ElementMetadata 本身字段有限，这里统一放到 regex_metadata 中，后续在检索阶段展平使用
        for key in (
            "intent",
            "config_mode",
            "required_keywords",
            "context",
            # P0/P1 过滤关键字段
            "product_module",
            "protocol_type",
            "scenario_id",
            "step_type",
            # P2 长期增强（用于约束/提示）
            "function_hierarchy",
            "description",
            # 测试/规格文档扩展字段
            "document_category",
            "test_type",
            "priority",
            "feature_name",
            "expected_result",
        ):
            value = metadata.get(key)
            if value:
                regex_metadata[key] = value
        nested_meta = metadata.get("regex_metadata")
        if isinstance(nested_meta, dict):
            regex_metadata.update(nested_meta)
        if regex_metadata:
            element_metadata.regex_metadata = regex_metadata

        element_metadata.piece_num = idx + 1
        elements.append(Text(text=text, metadata=element_metadata))
    return elements


def initialize_rag_system(use_graphrag: bool = None):
    """
    初始化RAG系统
    
    Args:
        use_graphrag: 是否启用 GraphRAG，默认从环境变量读取
    
    Returns:
        (hybrid_retriever, reranker, graphrag_retriever)
    """
    from INAGENT.utils.llm_config import get_siliconflow_config
    siliconflow_config = get_siliconflow_config()
    
    # 从配置中获取API key和base_url
    api_key = siliconflow_config.get("api_key")
    if not api_key or env_utils.is_placeholder_value(api_key):
        raise ValueError("LLM 网关未配置或无效，请在 .env 文件中配置")
    
    base_url = siliconflow_config.get("base_url")

    # 初始化嵌入模型（使用配置中的嵌入模型名称）
    embedding_model_name = siliconflow_config.get("embedding_model", "BAAI/bge-m3")
    embedding_dim_value = os.getenv("SILICONFLOW_EMBEDDING_DIM")
    vector_dim = 1024
    if embedding_dim_value:
        try:
            vector_dim = int(embedding_dim_value)
        except ValueError:
            pass

    embedding_model = OpenAICompatibleEmbedding(
        model_type=embedding_model_name,
        api_key=api_key,
        url=base_url,
    )

    # 初始化向量存储
    client = QdrantClient(":memory:")
    storage = QdrantStorage(
        vector_dim=vector_dim,
        client=client,
        collection_name="workflow_rag",
    )

    # 初始化 HybridRetriever
    hybrid_retriever = HybridRetriever(
        embedding_model=embedding_model,
        vector_storage=storage
    )

    # 加载知识库
    reference_dir = Path(__file__).parent / "knowledge_base" / "reference"
    if not reference_dir.exists():
        raise FileNotFoundError(f"知识库目录不存在: {reference_dir}")

    docs = env_utils.load_knowledge_base(reference_dir)
    elements = _documents_to_elements(docs)
    if elements:
        hybrid_retriever.process(elements)
        logger.info(f"已加载 {len(elements)} 个文档块到RAG系统")

    # 初始化 Reranker（使用配置中的重排序模型）
    reranker_model_name = siliconflow_config.get("reranker_model", "BAAI/bge-reranker-v2-m3")
    reranker = SiliconFlowRerankRetriever(
        model_name=reranker_model_name,
        api_key=api_key,
        base_url=base_url,
    )

    # GraphRAG 为可选：未构建时降级为向量检索
    if use_graphrag is None:
        use_graphrag = os.getenv("USE_GRAPHRAG", "true").lower() in (
            "true",
            "1",
            "yes",
        )
    graphrag_retriever = None

    if use_graphrag:
        if not GRAPHRAG_AVAILABLE:
            logger.warning(
                "GraphRAG 模块不可用，将仅使用向量检索。"
            )
        else:
            try:
                # 先检查 graphrag 核心包是否真正可用
                import importlib
                _gapi = importlib.util.find_spec("graphrag.api")
                if _gapi is None:
                    logger.warning(
                        "graphrag 包未安装或版本不兼容（缺少 graphrag.api），"
                        "跳过 GraphRAG 以避免重复报错。仅使用向量检索。"
                    )
                else:
                    graphrag_index = Path(__file__).parent / "graphrag_index"
                    graphrag_retriever = GraphRAGRetriever(
                        workspace_dir=graphrag_index,
                        use_siliconflow=True,
                    )
                    if not graphrag_retriever.is_available():
                        logger.warning(
                            "GraphRAG 索引未构建，将仅使用向量检索。"
                            "可执行: python INAGENT/scripts/init_graphrag.py --init --build"
                        )
                        graphrag_retriever = None
                    else:
                        logger.info("GraphRAG 检索器已初始化")
            except Exception as e:
                logger.warning(
                    "GraphRAG 初始化失败，将仅使用向量检索: %s",
                    e,
                )
                graphrag_retriever = None
    else:
        logger.info("GraphRAG 已禁用，使用向量检索")

    return hybrid_retriever, reranker, graphrag_retriever


def initialize_llm_model():
    """初始化LLM模型"""
    from INAGENT.utils.llm_config import get_siliconflow_config
    siliconflow_config = get_siliconflow_config()
    
    # 从配置中获取API key和base_url
    api_key = siliconflow_config.get("api_key")
    if not api_key or env_utils.is_placeholder_value(api_key):
        raise ValueError("LLM 网关未配置或无效，请在 .env 文件中配置")
    
    base_url = siliconflow_config.get("base_url")
    
    # 使用配置中的对话模型名称
    model_name = siliconflow_config.get("chat_model", "Qwen/Qwen3-8B")
    model = ModelFactory.create(
        model_platform=ModelPlatformType.SILICONFLOW,
        model_type=model_name,
        api_key=api_key,
        url=base_url,
        model_config_dict={"temperature": 0.0},
    )
    return model


def process_job(
    job_content: str,
    hybrid_retriever,
    reranker,
    model,
    function_index: Dict[str, Any],
    graphrag_retriever=None,
    env_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """处理单个job需求，生成配置

    Args:
        env_plan: 环境规划字典（来自 env_setup_agent），包含实际 IP 地址。
                  如果提供，将替换默认的 env_context，确保 LLM 使用正确的 IP。
    """
    logger.info("=" * 80)
    logger.info("开始处理需求")
    logger.info("=" * 80)
    logger.info(f"需求内容: {job_content}")
    logger.info("")

    # 1. 任务分解
    logger.info("=" * 80)
    logger.info("步骤1: 任务分解Agent分析需求")
    logger.info("=" * 80)
    
    decomposition_agent = build_task_decomposition_agent(model)
    decomposition_prompt = build_task_decomposition_prompt(job_content, function_index)

    wlog = get_workflow_logger()
    log_llm_input("任务分解 Agent", decomposition_prompt, wlog)

    try:
        wlog.info("调用任务分解 Agent...")
        from pydantic import BaseModel, Field
        from typing import List, Dict, Any
        
        # 定义任务分解的响应格式
        class RAGQuery(BaseModel):
            step_type: str = Field(..., description="步骤类型")
            feature: str = Field(..., description="功能名称")
            query: str = Field(..., description="RAG查询字符串")
            priority: int = Field(..., description="优先级（1-5，1最高）")
        
        class DecompositionResult(BaseModel):
            scenario_id: str = Field(..., description="场景ID")
            product_modules: List[str] = Field(..., description="产品模块列表")
            protocol_type: List[str] = Field(..., description="协议类型")
            required_steps: List[str] = Field(..., description="必需的基础配置步骤列表")
            advanced_features: List[str] = Field(..., description="高级功能列表")
            rag_queries: List[RAGQuery] = Field(..., description="RAG查询列表")
        
        decomposition_response = decomposition_agent.step(
            decomposition_prompt,
        )
        decomposition_text = decomposition_response.msgs[0].content if decomposition_response.msgs else ""
        log_llm_output("任务分解 Agent", decomposition_text, wlog)
        logger.info("")
        
        # 解析JSON
        import re
        match = re.search(r"\{[\s\S]*\}", decomposition_text)
        if match:
            decomposition_result = json.loads(match.group(0))
            logger.info("任务分解结果（解析后的JSON）:")
            logger.info(json.dumps(decomposition_result, ensure_ascii=False, indent=2))
            logger.info("")
            
            # 详细输出分解结果
            logger.info("分解结果详情:")
            logger.info(f"  - 场景ID: {decomposition_result.get('scenario_id', 'N/A')}")
            logger.info(f"  - 协议类型: {decomposition_result.get('protocol_type', 'N/A')}")
            logger.info(f"  - 产品模块: {decomposition_result.get('product_modules', [])}")
            logger.info(f"  - 必需步骤数量: {len(decomposition_result.get('required_steps', []))}")
            logger.info(f"  - 高级功能数量: {len(decomposition_result.get('advanced_features', []))}")
            logger.info(f"  - RAG查询数量: {len(decomposition_result.get('rag_queries', []))}")
            
            # 输出RAG查询列表
            rag_queries = decomposition_result.get('rag_queries', [])
            if rag_queries:
                logger.info("  - RAG查询列表:")
                for i, q in enumerate(rag_queries, 1):
                    logger.info(f"    {i}. [{q.get('step_type', 'N/A')}] {q.get('query', 'N/A')} (优先级: {q.get('priority', 'N/A')})")
        else:
            logger.warning("无法解析任务分解结果，使用默认值")
            decomposition_result = {
                "product_modules": [],
                "protocol_type": [],
                "required_steps": [],
                "rag_queries": [],
            }
    except Exception as e:
        logger.error(f"任务分解失败: {e}", exc_info=True)
        decomposition_result = {
            "product_modules": [],
            "protocol_type": [],
            "required_steps": [],
            "rag_queries": [],
        }
    
    logger.info("")

    # 2. RAG检索
    logger.info("=" * 80)
    logger.info("步骤2: RAG检索相关配置文档")
    logger.info("=" * 80)
    logger.info(f"使用任务分解结果进行自适应RAG检索...")
    logger.info(f"  - 整体查询: {job_content}")
    logger.info(f"  - 分解查询数量: {len(decomposition_result.get('rag_queries', []))}")
    logger.info("")
    
    # 使用 10 倍候选池策略（top_k_rerank × retrieval_multiplier），可选 GraphRAG
    retrieval_multiplier = int(os.getenv("RAG_RETRIEVAL_MULTIPLIER", "10"))
    top_k_retrieval = 5 * retrieval_multiplier  # 默认 50
    use_graphrag = graphrag_retriever is not None and (
        getattr(graphrag_retriever, "is_available", lambda: False)()
    )
    if use_graphrag:
        logger.info("[RAG] 使用 GraphRAG 增强检索")

    # 优先使用统一 RAG 流水线：向量 + GraphRAG + Rerank + 协议加权
    if UNIFIED_RAG_AVAILABLE:
        unified = UnifiedRAGRetriever(
            hybrid_retriever,
            reranker=reranker,
            graphrag_retriever=graphrag_retriever if use_graphrag else None,
            use_protocol_boost=True,
        )
        context, constraints, adjusted_decomposition = unified.retrieve(
            job_content,
            top_k_retrieval=top_k_retrieval,
            top_k_final=8,
            use_graphrag=use_graphrag,
            decomposition_result=decomposition_result,
        )
    else:
        context, constraints, adjusted_decomposition = _adaptive_rag_retrieval(
            hybrid_retriever,
            reranker,
            job_content,
            decomposition_result=decomposition_result,
            top_k_retrieval=top_k_retrieval,
            top_k_rerank=5,
            graphrag_retriever=graphrag_retriever if use_graphrag else None,
        )
    
    wlog.info("RAG 检索完成")
    wlog.info("  - 返回上下文长度: %d 字符", len(context))
    wlog.info("  - 约束条件: %s", constraints)
    log_section("RAG 返回的上下文", context[:2000] + ("..." if len(context) > 2000 else ""), wlog)

    # 3. 生成配置命令
    logger.info("=" * 80)
    logger.info("步骤3: LB Ops Agent生成配置命令")
    logger.info("=" * 80)
    
    lb_ops_agent = build_lb_ops_agent(model, lambda q: context)
    env_context = "默认环境配置"
    lb_ops_prompt = build_lb_ops_prompt(
        job_content,
        context,
        env_context,
        constraints=constraints,
        decomposition_result=adjusted_decomposition,
        function_index=function_index,
        env_plan=env_plan,
    )
    
    log_llm_input("LB Ops Agent（配置生成）", lb_ops_prompt, wlog)
    logger.info("调用LB Ops Agent...")

    try:
        from pydantic import BaseModel, Field
        from typing import List
        
        # 定义配置生成的响应格式
        class ConfigResult(BaseModel):
            config_commands: List[str] = Field(..., description="配置命令数组")
            verify_commands: List[str] = Field(..., description="验证命令数组")
            notes: str = Field(..., description="配置说明")
        
        config_response = lb_ops_agent.step(
            lb_ops_prompt,
        )
        config_text = config_response.msgs[0].content if config_response.msgs else ""
        log_llm_output("LB Ops Agent（配置生成）", config_text, wlog)
        
        # 解析JSON
        match = re.search(r"\{[\s\S]*\}", config_text)
        if match:
            config_result = json.loads(match.group(0))
            logger.info("配置生成结果（解析后的JSON）:")
            logger.info(json.dumps(config_result, ensure_ascii=False, indent=2))
            logger.info("")
            
            logger.info("生成的配置详情:")
            logger.info(f"  - 配置命令数量: {len(config_result.get('config_commands', []))}")
            logger.info(f"  - 验证命令数量: {len(config_result.get('verify_commands', []))}")
            if config_result.get('config_commands'):
                logger.info("  - 配置命令列表:")
                for i, cmd in enumerate(config_result.get('config_commands', []), 1):
                    logger.info(f"    {i}. {cmd}")
            if config_result.get('verify_commands'):
                logger.info("  - 验证命令列表:")
                for i, cmd in enumerate(config_result.get('verify_commands', []), 1):
                    logger.info(f"    {i}. {cmd}")
        else:
            logger.warning("无法解析配置结果")
            config_result = {
                "config_commands": [],
                "verify_commands": [],
                "notes": "配置生成失败",
            }
    except Exception as e:
        logger.error(f"配置生成失败: {e}", exc_info=True)
        config_result = {
            "config_commands": [],
            "verify_commands": [],
            "notes": f"配置生成失败: {e}",
        }
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("处理完成")
    logger.info("=" * 80)
    logger.info("")

    return {
        "job_content": job_content,
        "decomposition": decomposition_result,
        "config": config_result,
        "context_used": context[:500],  # 只保存前500字符
    }


def main():
    """主函数：处理jobs目录下的所有需求文件"""
    import argparse

    parser = argparse.ArgumentParser(description="Workflow: 输入需求生成配置")
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path(__file__).parent / "jobs" / "config_tasks",
        help="Jobs目录路径（默认: jobs/config_tasks/）"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "reports",
        help="输出目录路径"
    )
    parser.add_argument(
        "--index-path",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "function_structure_index.json",
        help="功能结构索引文件路径"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    # 创建本轮 workflow 日志文件（LLM 输入/输出等会同时打屏并写入该文件）
    setup_workflow_logger()
    try:
        logger.info("本轮 workflow 日志文件: %s", get_workflow_log_path())
    except Exception:
        pass

    # 加载功能结构索引
    if not args.index_path.exists():
        logger.warning(f"功能结构索引文件不存在: {args.index_path}")
        function_index = {}
    else:
        function_index = load_function_structure_index(args.index_path)
        logger.info(f"已加载功能结构索引: {len(function_index.get('scenarios', {}))} 个场景")

    # 初始化RAG系统
    logger.info("初始化RAG系统...")
    hybrid_retriever, reranker, graphrag_retriever = initialize_rag_system()
    
    if graphrag_retriever:
        logger.info("GraphRAG 已启用")
    else:
        logger.info("使用传统向量检索 + Rerank")

    # 初始化LLM模型
    logger.info("初始化LLM模型...")
    model = initialize_llm_model()

    # 处理jobs目录下的所有文件
    jobs_dir = args.jobs_dir
    if not jobs_dir.exists():
        logger.error(f"Jobs目录不存在: {jobs_dir}")
        return

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    job_files = list(jobs_dir.glob("*.txt"))
    if not job_files:
        logger.warning(f"Jobs目录下没有找到.txt文件: {jobs_dir}")
        return

    logger.info(f"找到 {len(job_files)} 个需求文件")

    for job_file in job_files:
        logger.info(f"处理文件: {job_file.name}")
        
        # 读取需求内容
        with open(job_file, "r", encoding="utf-8") as f:
            job_content = f.read()

        # 处理需求
        result = process_job(
            job_content,
            hybrid_retriever,
            reranker,
            model,
            function_index,
            graphrag_retriever=graphrag_retriever,
        )

        # 保存结果
        output_file = output_dir / f"{job_file.stem}_result.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(f"结果已保存到: {output_file}")

        # 打印配置命令
        print("\n" + "="*60)
        print(f"需求文件: {job_file.name}")
        print("="*60)
        config = result.get("config", {})
        config_commands = config.get("config_commands", [])
        verify_commands = config.get("verify_commands", [])
        
        if config_commands:
            print("\n配置命令:")
            for cmd in config_commands:
                print(f"  {cmd}")
        
        if verify_commands:
            print("\n验证命令:")
            for cmd in verify_commands:
                print(f"  {cmd}")
        
        notes = config.get("notes", "")
        if notes:
            print(f"\n备注: {notes}")
        print("="*60 + "\n")


if __name__ == "__main__":
    main()
