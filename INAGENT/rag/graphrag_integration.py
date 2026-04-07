# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
GraphRAG 集成模块

将 Microsoft GraphRAG 集成到 INFOAGEN 项目的 RAG 流程中。

主要功能：
1. 索引构建：从 knowledge_base.json 构建 GraphRAG 索引
2. 混合检索：结合向量检索和图检索
3. 元数据转换：确保与现有 workflow 兼容

使用方式：
```python
from INAGENT.rag.graphrag_integration import GraphRAGRetriever

# 初始化
retriever = GraphRAGRetriever(workspace_dir="graphrag_index")

# 构建索引（首次）
await retriever.build_index()

# 检索
results = await retriever.search("如何配置HTTP类型的SLB服务", search_type="local")
```
"""
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)

# 统一 RAG：向量 + GraphRAG + Rerank + 协议加权（供 workflow 等同步调用）
try:
    from INAGENT.rag.unified_rag import UnifiedRAGRetriever
except ImportError:
    UnifiedRAGRetriever = None  # type: ignore

# 添加 graphrag 到路径
GRAPHRAG_PATH = Path(__file__).parent / "graphrag"
if str(GRAPHRAG_PATH) not in sys.path:
    sys.path.insert(0, str(GRAPHRAG_PATH))


@dataclass
class GraphRAGSearchResult:
    """GraphRAG 检索结果"""
    text: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "graphrag"
    search_type: str = "local"
    entities: List[str] = field(default_factory=list)
    relationships: List[Dict[str, str]] = field(default_factory=list)


class GraphRAGRetriever:
    """
    GraphRAG 检索器
    
    封装 Microsoft GraphRAG 的索引构建和检索功能，
    适配 INFOAGEN 项目的 workflow。
    """
    
    def __init__(
        self,
        workspace_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ):
        """
        初始化 GraphRAG 检索器
        
        Args:
            workspace_dir: GraphRAG 工作空间目录
            config_path: settings.yaml 配置文件路径
        """
        self.workspace_dir = workspace_dir or Path(__file__).parent / "graphrag_index"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        
        self.config_path = config_path or self.workspace_dir / "settings.yaml"
        
        # 索引数据（延迟加载）
        self._entities: Optional[pd.DataFrame] = None
        self._relationships: Optional[pd.DataFrame] = None
        self._communities: Optional[pd.DataFrame] = None
        self._community_reports: Optional[pd.DataFrame] = None
        self._text_units: Optional[pd.DataFrame] = None
        self._config: Optional[Any] = None
        
        self._initialized = False
    
    def _ensure_initialized(self) -> bool:
        """确保索引数据已加载"""
        if self._initialized:
            return True
        
        output_dir = self.workspace_dir / "output"
        if not output_dir.exists():
            logger.warning(f"GraphRAG 输出目录不存在: {output_dir}")
            return False

        try:
            from INAGENT.rag.graphrag_adapter import validate_graphrag_index

            validation = validate_graphrag_index(self.workspace_dir)
            integrity = validation.get("checks", {}).get("referential_integrity")
            if integrity is not None and not integrity.get("ok", True):
                logger.error("GraphRAG 输出文件存在引用断链，拒绝加载: %s", integrity.get("problems", []))
                return False
        except Exception as e:
            logger.warning(f"GraphRAG 一致性校验失败，继续尝试加载索引: {e}")
        
        try:
            # 加载索引数据
            entities_path = output_dir / "entities.parquet"
            relationships_path = output_dir / "relationships.parquet"
            communities_path = output_dir / "communities.parquet"
            reports_path = output_dir / "community_reports.parquet"
            text_units_path = output_dir / "text_units.parquet"
            
            if entities_path.exists():
                self._entities = pd.read_parquet(entities_path)
                logger.info(f"加载 {len(self._entities)} 个实体")
            
            if relationships_path.exists():
                self._relationships = pd.read_parquet(relationships_path)
                logger.info(f"加载 {len(self._relationships)} 个关系")
            
            if communities_path.exists():
                self._communities = pd.read_parquet(communities_path)
                logger.info(f"加载 {len(self._communities)} 个社区")
            
            if reports_path.exists():
                self._community_reports = pd.read_parquet(reports_path)
                logger.info(f"加载 {len(self._community_reports)} 个社区报告")
            
            if text_units_path.exists():
                self._text_units = pd.read_parquet(text_units_path)
                logger.info(f"加载 {len(self._text_units)} 个文本单元")
            
            # 加载配置
            self._load_config()

            if self._config is not None:
                self._initialized = True
            else:
                logger.warning("GraphRAG initialization incomplete: config loading failed")
                return False

            self._init_context_builder()
            return True
            
        except Exception as e:
            logger.error(f"加载 GraphRAG 索引失败: {e}")
            return False
    
    def _load_config(self):
        """加载 GraphRAG 配置"""
        try:
            from graphrag.config.load_config import load_config
            self._config = load_config(self.workspace_dir)
        except Exception as e:
            logger.warning(f"加载 GraphRAG 配置失败: {e}")
            self._config = None

    def _init_context_builder(self):
        """构建 LocalSearchMixedContext，用于 local_context_build (无 LLM)。"""
        self._local_context_builder = None
        self._context_builder_params = {}
        if self._config is None or self._entities is None or self._text_units is None:
            return
        try:
            from graphrag.config.embeddings import entity_description_embedding
            from graphrag.query.context_builder.entity_extraction import EntityVectorStoreKey
            from graphrag.query.indexer_adapters import (
                read_indexer_entities,
                read_indexer_relationships,
                read_indexer_reports,
                read_indexer_text_units,
            )
            from graphrag.query.structured_search.local_search.mixed_context import (
                LocalSearchMixedContext,
            )
            from graphrag.language_model.manager import ModelManager
            from graphrag.tokenizer.get_tokenizer import get_tokenizer
            from graphrag.utils.api import get_embedding_store
            from graphrag.utils.cli import redact

            community_level = 2
            vector_store_args = {}
            for index, store in self._config.vector_store.items():
                vector_store_args[index] = store.model_dump()

            description_embedding_store = get_embedding_store(
                config_args=vector_store_args,
                embedding_name=entity_description_embedding,
            )

            communities = self._communities if self._communities is not None else pd.DataFrame()
            community_reports = self._community_reports if self._community_reports is not None else pd.DataFrame()

            entities_df = self._entities.copy()
            for col in ("degree", "frequency"):
                if col in entities_df.columns:
                    entities_df[col] = entities_df[col].fillna(0)

            entities_ = read_indexer_entities(entities_df, communities, None)
            reports_ = read_indexer_reports(community_reports, communities, community_level)
            text_units_ = read_indexer_text_units(self._text_units)
            relationships_ = read_indexer_relationships(
                self._relationships if self._relationships is not None else pd.DataFrame()
            )

            embedding_settings = self._config.get_language_model_config(
                self._config.local_search.embedding_model_id
            )
            embedding_model = ModelManager().get_or_create_embedding_model(
                name="local_context_embedding",
                model_type=embedding_settings.type,
                config=embedding_settings,
            )

            model_settings = self._config.get_language_model_config(
                self._config.local_search.chat_model_id
            )
            tokenizer = get_tokenizer(model_config=model_settings)

            ls_config = self._config.local_search
            self._local_context_builder = LocalSearchMixedContext(
                community_reports=reports_,
                text_units=text_units_,
                entities=entities_,
                relationships=relationships_,
                covariates={"claims": []},
                entity_text_embeddings=description_embedding_store,
                embedding_vectorstore_key=EntityVectorStoreKey.ID,
                text_embedder=embedding_model,
                tokenizer=tokenizer,
            )
            self._context_builder_params = {
                "text_unit_prop": ls_config.text_unit_prop,
                "community_prop": ls_config.community_prop,
                "conversation_history_max_turns": ls_config.conversation_history_max_turns,
                "conversation_history_user_turns_only": True,
                "top_k_mapped_entities": ls_config.top_k_entities,
                "top_k_relationships": ls_config.top_k_relationships,
                "include_entity_rank": True,
                "include_relationship_weight": True,
                "include_community_rank": False,
                "return_candidate_context": False,
                "embedding_vectorstore_key": EntityVectorStoreKey.ID,
                "max_context_tokens": ls_config.max_context_tokens,
            }
            logger.info("LocalSearchMixedContext 初始化成功 (context-only, 无 LLM)")
        except Exception as e:
            logger.warning(f"LocalSearchMixedContext 初始化失败，将回退到 local_search: {e}")
            self._local_context_builder = None

    async def local_context_build(
        self,
        query: str,
        top_k: int = 10,
    ) -> List[GraphRAGSearchResult]:
        """仅构建上下文（1 次 embedding，0 次 LLM），跳过 response 生成。"""
        if not self._ensure_initialized():
            return []
        if self._local_context_builder is None:
            _, results = await self.local_search(query, top_k)
            return results
        try:
            context_result = self._local_context_builder.build_context(
                query=query, **self._context_builder_params
            )
            return self._convert_context_to_results(
                context_result.context_records, "local"
            )[:top_k]
        except Exception as e:
            logger.warning(f"local_context_build 失败，回退到 local_search: {e}")
            _, results = await self.local_search(query, top_k)
            return results
    
    async def build_index(
        self,
        knowledge_base_path: Optional[Path] = None,
        force_rebuild: bool = False,
        resume: bool = False,
        fast: bool = False,
        update: bool = False,
    ) -> bool:
        """
        构建 GraphRAG 索引

        Args:
            knowledge_base_path: knowledge_base.json 路径
            force_rebuild: 是否强制重建索引（清空 cache + output）
            resume: 续跑中断的构建（保留 cache/output，已完成的 LLM 调用命中缓存）
            fast: 用 NLP 抽取代替 LLM（跳过 extract_graph LLM 调用）
            update: 增量更新（仅处理新增/变更文档，与现有索引合并）

        Returns:
            是否构建成功
        """
        output_dir = self.workspace_dir / "output"

        if not (force_rebuild or resume or update) and output_dir.exists():
            entities_path = output_dir / "entities.parquet"
            if entities_path.exists():
                logger.info("GraphRAG 索引已存在，跳过构建")
                return True

        if force_rebuild and output_dir.exists():
            import shutil
            for f in output_dir.glob("*.parquet"):
                f.unlink()
            ldb = output_dir / "lancedb"
            if ldb.exists():
                shutil.rmtree(ldb, ignore_errors=True)
            cache_dir = self.workspace_dir / "cache"
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
                cache_dir.mkdir(exist_ok=True)
            logger.info("force_rebuild: 已清理 output/ 和 cache/")
        
        # 准备输入数据
        knowledge_base_path = knowledge_base_path or (
            Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json"
        )
        
        if not knowledge_base_path.exists():
            logger.error(f"知识库文件不存在: {knowledge_base_path}")
            return False
        
        try:
            import sys
            from datetime import datetime
            def _m(msg: str, *args: object) -> None:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if args:
                    logger.info("[MILESTONE] %s | " + msg, ts, *args)
                else:
                    logger.info("[MILESTONE] %s | %s", ts, msg)
                sys.stdout.flush()
                sys.stderr.flush()

            _m("build_index: initialize_graphrag_index START")
            from INAGENT.rag.graphrag_adapter import initialize_graphrag_index
            initialize_graphrag_index(
                knowledge_base_path=knowledge_base_path,
                workspace_dir=self.workspace_dir,
            )
            _m("build_index: initialize_graphrag_index DONE")

            _m("build_index: load_config START")
            from graphrag.api import build_index
            from graphrag.config.load_config import load_config
            from graphrag.config.enums import IndexingMethod
            config = load_config(self.workspace_dir)
            _m("build_index: load_config DONE")

            method = IndexingMethod.Fast if fast else IndexingMethod.Standard
            is_update_run = update
            _m("build_index: graphrag.api.build_index(config) START | method=%s update=%s",
               method.value, is_update_run)
            results = await build_index(
                config,
                method=method,
                is_update_run=is_update_run,
            )
            _m("build_index: graphrag.api.build_index DONE")

            errors = []
            for result in results:
                if result.errors:
                    errors.extend(result.errors)
            if errors:
                logger.error("GraphRAG 索引构建出错: %s", errors)
                return False

            from INAGENT.rag.graphrag_adapter import validate_graphrag_index

            validation = validate_graphrag_index(self.workspace_dir)
            integrity = validation.get("checks", {}).get("referential_integrity")
            if integrity is not None and not integrity.get("ok", True):
                logger.error("GraphRAG 索引构建后校验失败: %s", integrity.get("problems", []))
                return False

            from INAGENT.rag.graphrag_adapter import _input_fingerprint
            fp = _input_fingerprint(self.workspace_dir)
            if fp:
                fp_path = self.workspace_dir / "output" / ".input_fingerprint"
                fp_path.write_text(fp, encoding="utf-8")

            logger.info("GraphRAG 索引构建完成")
            self._initialized = False
            return True

        except Exception as e:
            logger.error("GraphRAG 索引构建失败: %s", e, exc_info=True)
            return False
    
    async def local_search(
        self,
        query: str,
        top_k: int = 10,
        community_level: int = 2,
        response_type: str = "Multiple Paragraphs",
    ) -> Tuple[str, List[GraphRAGSearchResult]]:
        """
        本地搜索（Local Search）
        
        适用于需要理解特定实体及其关系的查询。
        
        Args:
            query: 查询字符串
            top_k: 返回结果数量
            community_level: 社区层级
            response_type: 响应类型
        
        Returns:
            (响应文本, 检索结果列表)
        """
        if not self._ensure_initialized():
            logger.warning("GraphRAG 未初始化，返回空结果")
            return "", []
        
        if self._entities is None or self._text_units is None:
            logger.warning("GraphRAG 索引数据不完整")
            return "", []
        
        try:
            from graphrag.api import local_search
            
            response, context_data = await local_search(
                config=self._config,
                entities=self._entities,
                communities=self._communities,
                community_reports=self._community_reports,
                text_units=self._text_units,
                relationships=self._relationships,
                covariates=None,
                community_level=community_level,
                response_type=response_type,
                query=query,
            )
            
            # 转换结果格式
            results = self._convert_context_to_results(context_data, "local")
            
            return response, results[:top_k]
            
        except Exception as e:
            logger.error(f"GraphRAG local_search 失败: {e}", exc_info=True)
            return "", []
    
    async def global_search(
        self,
        query: str,
        top_k: int = 10,
        community_level: int = 2,
        dynamic_community_selection: bool = True,
        response_type: str = "Multiple Paragraphs",
    ) -> Tuple[str, List[GraphRAGSearchResult]]:
        """
        全局搜索（Global Search）
        
        适用于需要理解整个语料库的高层次问题。
        
        Args:
            query: 查询字符串
            top_k: 返回结果数量
            community_level: 社区层级
            dynamic_community_selection: 是否动态选择社区
            response_type: 响应类型
        
        Returns:
            (响应文本, 检索结果列表)
        """
        if not self._ensure_initialized():
            logger.warning("GraphRAG 未初始化，返回空结果")
            return "", []
        
        if self._entities is None or self._community_reports is None:
            logger.warning("GraphRAG 索引数据不完整")
            return "", []
        
        try:
            from graphrag.api import global_search
            
            response, context_data = await global_search(
                config=self._config,
                entities=self._entities,
                communities=self._communities,
                community_reports=self._community_reports,
                community_level=community_level,
                dynamic_community_selection=dynamic_community_selection,
                response_type=response_type,
                query=query,
            )
            
            # 转换结果格式
            results = self._convert_context_to_results(context_data, "global")
            
            return response, results[:top_k]
            
        except Exception as e:
            logger.error(f"GraphRAG global_search 失败: {e}", exc_info=True)
            return "", []
    
    async def drift_search(
        self,
        query: str,
        top_k: int = 10,
        community_level: int = 2,
        response_type: str = "Multiple Paragraphs",
    ) -> Tuple[str, List[GraphRAGSearchResult]]:
        """
        DRIFT 搜索（Dynamic Reasoning and Inference with Flexible Traversal）
        
        结合本地和全局搜索的优势，适用于复杂的多跳推理查询。
        
        Args:
            query: 查询字符串
            top_k: 返回结果数量
            community_level: 社区层级
            response_type: 响应类型
        
        Returns:
            (响应文本, 检索结果列表)
        """
        if not self._ensure_initialized():
            logger.warning("GraphRAG 未初始化，返回空结果")
            return "", []
        
        try:
            from graphrag.api import drift_search
            
            response, context_data = await drift_search(
                config=self._config,
                entities=self._entities,
                communities=self._communities,
                community_reports=self._community_reports,
                text_units=self._text_units,
                relationships=self._relationships,
                community_level=community_level,
                response_type=response_type,
                query=query,
            )
            
            # 转换结果格式
            results = self._convert_context_to_results(context_data, "drift")
            
            return response, results[:top_k]
            
        except Exception as e:
            logger.error(f"GraphRAG drift_search 失败: {e}", exc_info=True)
            return "", []
    
    async def basic_search(
        self,
        query: str,
        top_k: int = 10,
    ) -> Tuple[str, List[GraphRAGSearchResult]]:
        """
        基础向量搜索
        
        使用纯向量检索，不依赖图结构。
        
        Args:
            query: 查询字符串
            top_k: 返回结果数量
        
        Returns:
            (响应文本, 检索结果列表)
        """
        if not self._ensure_initialized():
            logger.warning("GraphRAG 未初始化，返回空结果")
            return "", []
        
        if self._text_units is None:
            logger.warning("GraphRAG 文本单元数据不存在")
            return "", []
        
        try:
            from graphrag.api import basic_search
            
            response, context_data = await basic_search(
                config=self._config,
                text_units=self._text_units,
                query=query,
            )
            
            # 转换结果格式
            results = self._convert_context_to_results(context_data, "basic")
            
            return response, results[:top_k]
            
        except Exception as e:
            logger.error(f"GraphRAG basic_search 失败: {e}", exc_info=True)
            return "", []
    
    async def search(
        self,
        query: str,
        search_type: str = "local",
        top_k: int = 10,
        **kwargs
    ) -> Tuple[str, List[GraphRAGSearchResult]]:
        """
        统一搜索接口
        
        Args:
            query: 查询字符串
            search_type: 搜索类型 ("local", "global", "drift", "basic")
            top_k: 返回结果数量
            **kwargs: 其他参数
        
        Returns:
            (响应文本, 检索结果列表)
        """
        if search_type == "local":
            return await self.local_search(query, top_k, **kwargs)
        elif search_type == "global":
            return await self.global_search(query, top_k, **kwargs)
        elif search_type == "drift":
            return await self.drift_search(query, top_k, **kwargs)
        elif search_type == "basic":
            return await self.basic_search(query, top_k)
        else:
            logger.warning(f"未知的搜索类型: {search_type}，使用 local_search")
            return await self.local_search(query, top_k, **kwargs)
    
    def _convert_context_to_results(
        self,
        context_data: Any,
        search_type: str,
    ) -> List[GraphRAGSearchResult]:
        """
        将 GraphRAG 上下文数据转换为统一的结果格式
        
        GraphRAG API 返回 dict[str, pd.DataFrame]，包含以下标准键：
          local_search:  reports, entities, relationships, claims, sources
          global_search: reports
        
        Args:
            context_data: GraphRAG 返回的上下文数据
            search_type: 搜索类型
        
        Returns:
            检索结果列表
        """
        results = []
        
        if not context_data:
            return results
        
        # GraphRAG API 返回 dict[str, pd.DataFrame]
        if isinstance(context_data, dict):
            # 提取 sources（text_units，包含原始文档文本）
            sources = context_data.get("sources", None)
            if sources is not None and isinstance(sources, pd.DataFrame) and len(sources) > 0:
                for _, row in sources.iterrows():
                    text = str(row.get("text", "")) if "text" in row.index else ""
                    if text:
                        _src_meta = {
                            "id": str(row.get("id", "")) if "id" in row.index else "",
                            "title": str(row.get("title", "")) if "title" in row.index else "",
                            "source_type": "graphrag_source",
                        }
                        _inagent_json_match = re.search(
                            r"INAGENT_META_JSON\s*({[^}]+})", text
                        )
                        if _inagent_json_match:
                            try:
                                _parsed = json.loads(_inagent_json_match.group(1))
                                if "document_category" in _parsed:
                                    _src_meta["document_category"] = _parsed["document_category"]
                            except (json.JSONDecodeError, ValueError):
                                pass
                        results.append(GraphRAGSearchResult(
                            text=text,
                            score=float(row.get("score", 0.5)) if "score" in row.index else 0.5,
                            metadata=_src_meta,
                            source="graphrag_source",
                            search_type=search_type,
                        ))
            
            # 提取 entities（实体描述）
            entities = context_data.get("entities", None)
            if entities is not None and isinstance(entities, pd.DataFrame) and len(entities) > 0:
                for _, row in entities.iterrows():
                    desc = str(row.get("description", "")) if "description" in row.index else ""
                    if desc:
                        title = ""
                        for col in ("title", "entity", "name"):
                            if col in row.index and row.get(col):
                                title = str(row.get(col))
                                break
                        etype = str(row.get("type", "")) if "type" in row.index else ""
                        results.append(GraphRAGSearchResult(
                            text=desc,
                            score=float(row.get("rank", 0.3)) if "rank" in row.index else 0.3,
                            metadata={
                                "id": str(row.get("id", "")) if "id" in row.index else "",
                                "title": title,
                                "type": etype,
                                "source_type": "graphrag_entity",
                                "_graphrag_synthesized": True,
                            },
                            source="graphrag_entity",
                            search_type=search_type,
                            entities=[title] if title else [],
                        ))
            
            # 提取 relationships（关系描述）
            # 跳过裸类型标签（如 BELONGS_TO、PART_OF）——它们不含语义信息
            relationships = context_data.get("relationships", None)
            if relationships is not None and isinstance(relationships, pd.DataFrame) and len(relationships) > 0:
                for _, row in relationships.iterrows():
                    desc = str(row.get("description", "")) if "description" in row.index else ""
                    if not desc or " " not in desc:
                        continue
                        src = str(row.get("source", "")) if "source" in row.index else ""
                        tgt = str(row.get("target", "")) if "target" in row.index else ""
                        results.append(GraphRAGSearchResult(
                            text=desc,
                            score=float(row.get("rank", 0.2)) if "rank" in row.index else 0.2,
                            metadata={
                                "source_entity": src,
                                "target_entity": tgt,
                                "type": str(row.get("type", "")) if "type" in row.index else "",
                                "source_type": "graphrag_relationship",
                                "_graphrag_synthesized": True,
                            },
                            source="graphrag_relationship",
                            search_type=search_type,
                            relationships=[{
                                "source": src,
                                "target": tgt,
                                "type": str(row.get("type", "")) if "type" in row.index else "",
                            }],
                        ))
            
            # 提取 reports（社区报告，主要用于 global search）
            reports = context_data.get("reports", None)
            if reports is not None and isinstance(reports, pd.DataFrame) and len(reports) > 0:
                for _, row in reports.iterrows():
                    # 优先用 summary，然后 full_content
                    text = ""
                    for col in ["summary", "full_content", "content"]:
                        if col in row.index and pd.notna(row.get(col)):
                            text = str(row.get(col))
                            if text:
                                break
                    if text:
                        results.append(GraphRAGSearchResult(
                            text=text,
                            score=float(row.get("rank", 0.4)) if "rank" in row.index else 0.4,
                            metadata={
                                "id": str(row.get("id", "")) if "id" in row.index else "",
                                "title": str(row.get("title", "")) if "title" in row.index else "",
                                "source_type": "graphrag_report",
                                "_graphrag_synthesized": True,
                            },
                            source="graphrag_report",
                            search_type=search_type,
                        ))
        
        elif isinstance(context_data, list):
            for item in context_data:
                if isinstance(item, dict):
                    results.append(GraphRAGSearchResult(
                        text=item.get("text", item.get("content", "")),
                        score=item.get("score", item.get("rank", 0.0)),
                        metadata=item.get("metadata", {}),
                        source="graphrag",
                        search_type=search_type,
                    ))
        
        return results
    
    def get_entity_neighbors(
        self,
        entity_name: str,
        max_hops: int = 2,
    ) -> Dict[str, Any]:
        """
        获取实体的邻居节点（用于图扩展）
        
        Args:
            entity_name: 实体名称
            max_hops: 最大跳数
        
        Returns:
            邻居信息
        """
        if not self._ensure_initialized():
            return {"entities": [], "relationships": []}
        
        if self._relationships is None:
            return {"entities": [], "relationships": []}
        
        # 查找直接相关的关系
        related_rels = self._relationships[
            (self._relationships["source"] == entity_name) |
            (self._relationships["target"] == entity_name)
        ]
        
        entities = set()
        relationships = []
        
        for _, row in related_rels.iterrows():
            entities.add(row["source"])
            entities.add(row["target"])
            relationships.append({
                "source": row["source"],
                "target": row["target"],
                "type": row.get("type", "RELATED"),
                "description": row.get("description", ""),
            })
        
        # 多跳扩展
        if max_hops > 1:
            current_entities = entities.copy()
            for _ in range(max_hops - 1):
                new_entities = set()
                for entity in current_entities:
                    neighbor_rels = self._relationships[
                        (self._relationships["source"] == entity) |
                        (self._relationships["target"] == entity)
                    ]
                    for _, row in neighbor_rels.iterrows():
                        new_entities.add(row["source"])
                        new_entities.add(row["target"])
                        rel = {
                            "source": row["source"],
                            "target": row["target"],
                            "type": row.get("type", "RELATED"),
                            "description": row.get("description", ""),
                        }
                        if rel not in relationships:
                            relationships.append(rel)
                
                entities.update(new_entities)
                current_entities = new_entities - entities
        
        return {
            "entities": list(entities),
            "relationships": relationships,
        }
    
    def is_available(self) -> bool:
        """检查 GraphRAG 是否可用"""
        return self._ensure_initialized()

    # ── Write infrastructure (Farm-Owner) ─────────────────────────────────────

    def snapshot_backup(self, label: str = "") -> Path:
        import shutil
        from datetime import datetime

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_{label}" if label else ts
        output_dir = self.workspace_dir / "output"
        snap_dir = self.workspace_dir / "snapshots" / name
        snap_dir.mkdir(parents=True, exist_ok=True)

        for pq in output_dir.glob("*.parquet"):
            shutil.copy2(pq, snap_dir / pq.name)
        ldb = output_dir / "lancedb"
        if ldb.exists():
            shutil.copytree(ldb, snap_dir / "lancedb", dirs_exist_ok=True)

        manifest = {
            "timestamp": ts,
            "label": label,
            "files": [f.name for f in snap_dir.iterdir()],
        }
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("snapshot_backup → %s", snap_dir)
        return snap_dir

    def upsert_entities(self, patches: List[Dict[str, Any]]) -> int:
        output_dir = self.workspace_dir / "output"
        entities_path = output_dir / "entities.parquet"
        if not entities_path.exists():
            raise FileNotFoundError(f"entities.parquet 不存在: {entities_path}")

        df = pd.read_parquet(entities_path)
        existing_titles = set(df["title"].str.upper()) if "title" in df.columns else set()

        new_rows = []
        for p in patches:
            title = p.get("title", "")
            if title.upper() in existing_titles:
                logger.info("upsert_entities: 跳过已存在实体 %s", title)
                continue
            import uuid
            row = {
                "id": str(uuid.uuid4()),
                "title": title,
                "type": p.get("entity_type", "CONFIGURATION"),
                "description": p.get("description", ""),
                "human_readable_id": int(df["human_readable_id"].max()) + 1 + len(new_rows)
                if "human_readable_id" in df.columns
                else len(df) + len(new_rows),
            }
            # 只保留真实的 text_unit hash ID（hex 64 字符），不使用文件名作为 text_unit_id
            # 否则 _check_referential_integrity 会因断链拒绝加载整个 GraphRAG
            source_id = p.get("source_id", "")
            if source_id and len(source_id) >= 32 and "." not in source_id:
                row["text_unit_ids"] = [source_id]
            else:
                row["text_unit_ids"] = []
            new_rows.append(row)

        if not new_rows:
            return 0

        new_df = pd.DataFrame(new_rows)
        for col in df.columns:
            if col not in new_df.columns:
                new_df[col] = None
        df = pd.concat([df, new_df[df.columns]], ignore_index=True)
        df.to_parquet(entities_path, index=False)
        logger.info("upsert_entities: 新增 %d 行实体", len(new_rows))
        return len(new_rows)

    def add_entity_columns(self, columns: List[Dict[str, Any]]) -> List[str]:
        output_dir = self.workspace_dir / "output"
        entities_path = output_dir / "entities.parquet"
        if not entities_path.exists():
            raise FileNotFoundError(f"entities.parquet 不存在: {entities_path}")

        df = pd.read_parquet(entities_path)
        added = []
        for spec in columns:
            col_name = spec.get("name", "")
            if not col_name or col_name in df.columns:
                continue
            dtype_str = spec.get("dtype", "str")
            default = spec.get("default")
            if dtype_str == "bool":
                df[col_name] = bool(default) if default is not None else False
            elif dtype_str == "int":
                df[col_name] = int(default) if default is not None else 0
            elif dtype_str == "float":
                df[col_name] = float(default) if default is not None else 0.0
            else:
                df[col_name] = str(default) if default is not None else ""
            added.append(col_name)

        if added:
            df.to_parquet(entities_path, index=False)
            logger.info("add_entity_columns: 新增列 %s", added)
        return added

    def update_entity_fields(self, title: str, fields: Dict[str, Any]) -> bool:
        output_dir = self.workspace_dir / "output"
        entities_path = output_dir / "entities.parquet"
        if not entities_path.exists():
            raise FileNotFoundError(f"entities.parquet 不存在: {entities_path}")

        if not title or not fields:
            return False

        df = pd.read_parquet(entities_path)
        if "title" not in df.columns:
            raise KeyError("entities.parquet 缺少 title 列")

        mask = df["title"].fillna("").astype(str).str.upper() == title.upper()
        if not mask.any():
            logger.warning("update_entity_fields: 未找到实体 %s", title)
            return False

        for field_name, field_value in fields.items():
            if field_name not in df.columns:
                df[field_name] = None
            df.loc[mask, field_name] = field_value

        df.to_parquet(entities_path, index=False)
        logger.info("update_entity_fields: 已更新实体 %s 的字段 %s", title, list(fields.keys()))
        return True

    def add_relationships(self, patches: List[Dict[str, Any]]) -> int:
        import uuid

        output_dir = self.workspace_dir / "output"
        relationships_path = output_dir / "relationships.parquet"
        if not relationships_path.exists():
            raise FileNotFoundError(f"relationships.parquet 不存在: {relationships_path}")

        df = pd.read_parquet(relationships_path)
        existing = set()
        required_cols = ["source", "target", "type"]
        for col in required_cols:
            if col not in df.columns:
                raise KeyError(f"relationships.parquet 缺少 {col} 列")

        for _, row in df.iterrows():
            existing.add((
                str(row.get("source", "")).upper(),
                str(row.get("target", "")).upper(),
                str(row.get("type", "RELATED")).upper(),
            ))

        next_hrid = 1
        if "human_readable_id" in df.columns and not df.empty:
            next_hrid = int(df["human_readable_id"].max()) + 1

        new_rows = []
        for patch in patches:
            source = str(patch.get("source", "")).strip()
            target = str(patch.get("target", "")).strip()
            rel_type = str(patch.get("type", "RELATED")).strip() or "RELATED"
            if not source or not target:
                continue

            rel_key = (source.upper(), target.upper(), rel_type.upper())
            if rel_key in existing:
                continue

            row = {col: None for col in df.columns}
            row["source"] = source
            row["target"] = target
            row["type"] = rel_type
            if "description" in df.columns:
                row["description"] = patch.get("description", "")
            if "text_unit_ids" in df.columns and patch.get("source_id"):
                row["text_unit_ids"] = [patch["source_id"]]
            if "id" in df.columns:
                row["id"] = str(uuid.uuid4())
            if "human_readable_id" in df.columns:
                row["human_readable_id"] = next_hrid
                next_hrid += 1
            new_rows.append(row)
            existing.add(rel_key)

        if not new_rows:
            return 0

        new_df = pd.DataFrame(new_rows)
        for col in df.columns:
            if col not in new_df.columns:
                new_df[col] = None
        df = pd.concat([df, new_df[df.columns]], ignore_index=True)
        df.to_parquet(relationships_path, index=False)
        logger.info("add_relationships: 新增 %d 条关系", len(new_rows))
        return len(new_rows)

    def reembed_entities(self, entity_titles: List[str]) -> int:
        import lancedb
        import requests as _requests

        output_dir = self.workspace_dir / "output"
        entities_path = output_dir / "entities.parquet"
        ldb_path = output_dir / "lancedb"

        if not entities_path.exists() or not ldb_path.exists():
            raise FileNotFoundError("entities.parquet 或 lancedb 目录不存在")

        df = pd.read_parquet(entities_path)
        mask = df["title"].str.upper().isin([t.upper() for t in entity_titles])
        targets = df[mask]
        if targets.empty:
            return 0

        from INAGENT.utils.llm_config import get_gateway_config
        gw = get_gateway_config()
        embed_model = gw.get("embedding_model", "text-embedding-v4")
        base_url = gw.get("base_url", "").rstrip("/")
        api_key = gw.get("api_key", "local-gateway")

        texts = []
        ids = []
        titles = []
        for _, row in targets.iterrows():
            texts.append(str(row.get("description", row.get("title", ""))))
            ids.append(str(row["id"]))
            titles.append(str(row["title"]))

        resp = _requests.post(
            f"{base_url}/embeddings",
            json={"model": embed_model, "input": texts},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        embeddings = [item["embedding"] for item in resp.json()["data"]]

        db = lancedb.connect(str(ldb_path))
        table_name = "default-entity-description"
        tbl = db.open_table(table_name)

        new_records = []
        for i, emb in enumerate(embeddings):
            new_records.append({
                "id": ids[i],
                "title": titles[i],
                "text": texts[i],
                "vector": emb,
            })

        tbl.add(new_records)
        logger.info("reembed_entities: 嵌入并写入 %d 个实体到 LanceDB", len(new_records))
        return len(new_records)

    def replace_entity_embeddings(self, entity_titles: List[str]) -> int:
        import lancedb
        import requests as _requests

        output_dir = self.workspace_dir / "output"
        entities_path = output_dir / "entities.parquet"
        ldb_path = output_dir / "lancedb"

        if not entities_path.exists() or not ldb_path.exists():
            raise FileNotFoundError("entities.parquet 或 lancedb 目录不存在")

        df = pd.read_parquet(entities_path)
        mask = df["title"].str.upper().isin([t.upper() for t in entity_titles])
        targets = df[mask]
        if targets.empty:
            return 0

        from INAGENT.utils.llm_config import get_gateway_config
        gw = get_gateway_config()
        embed_model = gw.get("embedding_model", "text-embedding-v4")
        base_url = gw.get("base_url", "").rstrip("/")
        api_key = gw.get("api_key", "local-gateway")

        texts = []
        ids = []
        titles = []
        for _, row in targets.iterrows():
            texts.append(str(row.get("description", row.get("title", ""))))
            ids.append(str(row["id"]))
            titles.append(str(row["title"]))

        resp = _requests.post(
            f"{base_url}/embeddings",
            json={"model": embed_model, "input": texts},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        embeddings = [item["embedding"] for item in resp.json()["data"]]

        db = lancedb.connect(str(ldb_path))
        table_name = "default-entity-description"
        tbl = db.open_table(table_name)

        if hasattr(tbl, "delete"):
            escaped_ids = [entity_id.replace("'", "\\'") for entity_id in ids]
            if len(escaped_ids) == 1:
                filter_expr = f"id = '{escaped_ids[0]}'"
            else:
                filter_expr = "id IN (" + ", ".join(f"'{entity_id}'" for entity_id in escaped_ids) + ")"
            tbl.delete(filter_expr)

        new_records = []
        for idx, embedding in enumerate(embeddings):
            new_records.append({
                "id": ids[idx],
                "title": titles[idx],
                "text": texts[idx],
                "vector": embedding,
            })

        tbl.add(new_records)
        logger.info("replace_entity_embeddings: 替换 %d 个实体嵌入", len(new_records))
        return len(new_records)

    def reload(self):
        self._initialized = False
        self._entities = None
        self._relationships = None
        self._communities = None
        self._community_reports = None
        self._text_units = None
        self._config = None
        self._local_context_builder = None
        self._context_builder_params = {}
        self._ensure_initialized()
        logger.info("GraphRAGRetriever.reload() 完成")


class HybridGraphRAGRetriever:
    """
    混合检索器（统一 RAG：向量 + GraphRAG + Rerank + 协议加权）
    
    检索流程：
    1. 向量检索：获取初始候选
    2. 图检索：GraphRAG local_search 合并
    3. Rerank 重排序
    4. 协议加权并重排
    """
    
    def __init__(
        self,
        hybrid_retriever,
        graphrag_retriever: GraphRAGRetriever,
        reranker=None,
        vector_weight: float = 0.6,
        graph_weight: float = 0.4,
        use_protocol_boost: bool = True,
    ):
        self.hybrid_retriever = hybrid_retriever
        self.graphrag_retriever = graphrag_retriever
        self.reranker = reranker
        self.vector_weight = vector_weight
        self.graph_weight = graph_weight
        self.use_protocol_boost = use_protocol_boost
    
    async def retrieve(
        self,
        query: str,
        top_k_retrieval: int = 50,
        top_k_final: int = 10,
        use_graph: bool = True,
        max_hops: int = 2,
    ) -> List[Dict[str, Any]]:
        # 1. 向量检索
        try:
            vector_result = self.hybrid_retriever.query(
                query,
                top_k=top_k_retrieval,
                return_detailed_info=True,
            )
            vector_docs = vector_result.get("Retrieved Context", [])
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            vector_docs = []
        
        # 2. 图检索（如果启用）
        graph_docs = []
        if use_graph and self.graphrag_retriever.is_available():
            try:
                _, graph_results = await self.graphrag_retriever.local_search(
                    query, top_k=top_k_retrieval // 2
                )
                for result in graph_results:
                    graph_docs.append({
                        "text": result.text,
                        "similarity score": result.score,
                        "metadata": result.metadata,
                        "source": result.source,
                        "entities": result.entities,
                        "relationships": result.relationships,
                    })
            except Exception as e:
                logger.warning(f"图检索失败: {e}")
        
        # 3. 合并结果
        all_docs = []
        seen_texts = set()
        
        for doc in vector_docs:
            text = doc.get("text", "") if isinstance(doc, dict) else str(doc)
            text_hash = hash(text[:200])
            if text_hash not in seen_texts:
                seen_texts.add(text_hash)
                if isinstance(doc, dict):
                    doc["source_type"] = "vector"
                    all_docs.append(doc)
                else:
                    all_docs.append({
                        "text": str(doc),
                        "similarity score": 0.0,
                        "metadata": {},
                        "source_type": "vector",
                    })
        
        for doc in graph_docs:
            text = doc.get("text", "")
            text_hash = hash(text[:200])
            if text_hash not in seen_texts:
                seen_texts.add(text_hash)
                doc["source_type"] = "graph"
                all_docs.append(doc)
        
        # 4. Rerank
        if self.reranker and all_docs:
            try:
                reranked = self.reranker.query(
                    query=query,
                    retrieved_result=all_docs,
                    top_k=top_k_final,
                )
                all_docs = reranked
            except Exception as e:
                logger.warning(f"重排序失败: {e}")
                all_docs = all_docs[:top_k_final]
        else:
            all_docs = all_docs[:top_k_final]
        
        # 5. 协议加权并重排（与 unified_rag 一致）
        if self.use_protocol_boost and all_docs:
            try:
                from INAGENT.rag.unified_rag import _apply_protocol_boost
                _apply_protocol_boost(query, all_docs)
            except Exception as e:
                logger.debug("协议加权跳过: %s", e)
            all_docs = sorted(
                all_docs,
                key=lambda x: x.get("similarity score", 0.0),
                reverse=True,
            )
        
        return all_docs


def convert_knowledge_base_to_graphrag_input(
    knowledge_base_path: Path,
    output_dir: Path,
) -> Path:
    """
    将 knowledge_base.json 转换为 GraphRAG 输入格式
    
    GraphRAG 支持多种输入格式：
    - text: 纯文本文件
    - csv: CSV 文件（需要 text 列）
    - json: JSON 文件（需要 text 字段）
    
    我们使用 JSON 格式，保留元数据信息。
    
    Args:
        knowledge_base_path: knowledge_base.json 路径
        output_dir: 输出目录
    
    Returns:
        输入目录路径
    """
    input_dir = output_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    
    with open(knowledge_base_path, "r", encoding="utf-8") as f:
        kb_data = json.load(f)
    
    chunks = kb_data.get("chunks", [])
    
    # 转换为 GraphRAG 格式
    documents = []
    for i, chunk in enumerate(chunks):
        text = chunk.get("text", "")
        metadata = chunk.get("metadata", {})
        
        # 提取关键元数据并添加到文本前缀
        product_module = metadata.get("product_module", "")
        protocol_type = metadata.get("protocol_type", [])
        step_type = metadata.get("step_type", "")
        section_title = metadata.get("section_title", "")
        
        # 构建增强文本（包含元数据上下文）
        meta_parts = []
        if product_module and product_module != "unknown":
            meta_parts.append(f"[模块:{product_module}]")
        if protocol_type:
            protocols = protocol_type if isinstance(protocol_type, list) else [protocol_type]
            meta_parts.append(f"[协议:{','.join(protocols)}]")
        if step_type and step_type != "unknown":
            meta_parts.append(f"[步骤:{step_type}]")
        if section_title:
            meta_parts.append(f"[章节:{section_title}]")
        
        enriched_text = " ".join(meta_parts) + "\n\n" + text if meta_parts else text
        
        documents.append({
            "id": f"doc_{i:06d}",
            "text": enriched_text,
            "title": section_title or f"文档块 {i}",
        })
    
    # 保存为 JSON 格式
    output_path = input_dir / "documents.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)
    
    logger.info(f"已转换 {len(documents)} 个文档到 {input_dir}")
    return input_dir


if __name__ == "__main__":
    # 测试代码
    import asyncio
    
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        retriever = GraphRAGRetriever()
        
        # 检查是否可用
        if retriever.is_available():
            print("GraphRAG 索引已加载")
            
            # 测试搜索
            response, results = await retriever.local_search(
                "如何配置HTTP类型的SLB服务"
            )
            print(f"响应: {response[:200]}...")
            print(f"结果数量: {len(results)}")
        else:
            print("GraphRAG 索引未构建，需要先构建索引")
    
    asyncio.run(test())
