# INFOAGEN 项目更新日志

> 最后更新时间：2026-01-30

## 目录

- [2026-01-30 LLM Gateway 并行处理优化](#2026-01-30-llm-gateway-并行处理优化)
- [2026-01-30 完整流程整合](#2026-01-30-完整流程整合)
- [核心架构升级](#核心架构升级)
- [RAG检索能力优化方案](#rag检索能力优化方案)
- [数据库重构完成](#数据库重构完成)
- [动态模块树生成](#动态模块树生成)
- [限速和故障转移](#限速和故障转移)

---

## 2026-01-30 LLM Gateway 并行处理优化

### 问题背景

在 GraphRAG 索引构建过程中，观察到：
- LLM Gateway 日志显示只使用单一模型 `THUDM/GLM-Z1-9B-0414`
- 原 `concurrent_requests: 2` 配置过低，未充分利用并行能力

### 关键发现：Gateway 自动路由机制

经代码分析确认，**LLM Gateway 完全忽略客户端传入的 `model` 参数**：

```python
# gateway.py - chat_completions 端点
result = await chat_caller.call(messages=request.messages, ...)
# 注意：request.model 被忽略，由 MultiModelCaller 按 balance/race 模式自动选择
```

**Balance 模式的真实行为**：
- 多个并发请求时，轮询分配到不同模型
- 3 个模型形成 3 个独立处理队列
- 自动故障转移：某模型失败自动切换到下一个

### 优化内容

#### 1. 保持 Balance 模式（高效且节省配额）

```yaml
# llm_gateway/config.yaml
calling_mode: "balance"  # 轮询分配，不浪费 RPM/TPM
```

**并行处理架构**：
```
GraphRAG (concurrent_requests: 8)
         │
         ├── 请求1 ──→ Gateway ──→ GLM-Z1-9B
         ├── 请求2 ──→ Gateway ──→ Qwen3-8B
         ├── 请求3 ──→ Gateway ──→ DeepSeek
         ├── 请求4 ──→ Gateway ──→ GLM-Z1-9B (轮询)
         └── ...
```

#### 2. GraphRAG 对模型"无感"

```yaml
# INAGENT/graphrag_workspace/settings.yaml
default_chat_model:
  model: gateway-auto  # 占位符，Gateway 会忽略并自动路由
  concurrent_requests: 8  # 提高并发数
```

#### 3. 并发配置优化

| 配置项 | 优化前 | 优化后 | 说明 |
|--------|--------|--------|------|
| concurrent_requests | 2 | **8** | 充分利用 3 个模型的并行能力 |
| requests_per_minute | 20 | **100** | 匹配 Gateway 的多模型容量 |
| tokens_per_minute | 20000 | **50000** | 提高吞吐量 |

### Balance vs Race 模式对比

| 特性 | Balance 模式 ✅ | Race 模式 |
|------|-----------------|-----------|
| 请求分配 | 轮询到单个模型 | 并发调用所有模型 |
| 配额消耗 | 1x | 最多 3x |
| 响应速度 | 依赖选中模型 | 取最快响应 |
| 适用场景 | 批量处理、高并发 | 对延迟敏感的单次请求 |

**结论**：Balance 模式更适合 GraphRAG 批量处理场景，既能并行利用多模型，又不浪费 API 配额。

---

## 2026-01-30 完整流程整合

### 完整流程架构（LLM Gateway → PDF → RAG → Workflow）

本次更新完成了 **TRANSFORMATION_PLAN.md** 规划的所有阶段，实现端到端的智能文档处理流水线：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      INFOAGEN 完整流程架构图                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐    ┌─────────────────┐    ┌──────────────────────────────┐ │
│  │ LLM Gateway │───→│ PDF Processing  │───→│ Unified RAG + GraphRAG      │ │
│  │  端口 9000   │    │ MinerU 提取     │    │ (可选, 有则增强/无则回退)    │ │
│  │  Race 模式  │    │ content_list    │    │ HybridRetriever + Rerank    │ │
│  └─────────────┘    └─────────────────┘    └──────────────────────────────┘ │
│         │                   │                          │                    │
│         ▼                   ▼                          ▼                    │
│  ┌─────────────┐    ┌─────────────────┐    ┌──────────────────────────────┐ │
│  │ 多模型并发  │    │ Section 预扫描  │    │ Workflow Jobs 生成           │ │
│  │ GLM-Z1-9B  │    │ 产品模块注册表  │    │ Config → 执行 → 测试         │ │
│  │ Qwen3-8B   │    │ 层级结构分析    │    │ product_modules_registry     │ │
│  │ DeepSeek   │    │ 目录级分段      │    │ postprocess_step_extract.py  │ │
│  └─────────────┘    └─────────────────┘    └──────────────────────────────┘ │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 关键更新要点

| 阶段 | 功能 | 文件/位置 |
|------|------|-----------|
| **Phase 0** | GraphRAG 可选机制 | `unified_rag.py`, `graphrag_integration.py`, `rag_config.py` |
| **Phase 1** | 产品模块注册表 | `config/product_modules_registry.json`, `postprocess_step_extract.py` |
| **Phase 2** | Section 预扫描 | `workflow_rules_config.py`, `section_tree_analyzer.py` |
| **Phase 4** | 脚本文档完善 | `run_inagent_pipeline.bat`, `llm_gateway/start_gateway.bat` |

### GraphRAG 可选行为

- **行为变更**：GraphRAG 索引未构建时，系统自动回退到纯向量检索 + Rerank
- **日志提示**：`[INFO] GraphRAG index not found, using vector-only retrieval`
- **使用建议**：运行 `python INAGENT/scripts/init_graphrag.py --init --build` 构建索引以获得最佳效果

### 产品模块注册表

- **文件位置**：`INAGENT/config/product_modules_registry.json`
- **用途**：模糊匹配产品模块名称，支持别名和显示名
- **扩展方式**：直接编辑 JSON 文件，无需修改代码

### 脚本入口文档完善

| 脚本 | 更新内容 |
|------|----------|
| `run_inagent_pipeline.bat` | 添加完整流程概览、5个菜单选项说明、使用示例 |
| `llm_gateway/start_gateway.bat` | 添加功能特性、API端点(/health, /v1/chat/completions)、流程说明 |
| `run_inagent_pipeline.ps1` | GraphRAG回退消息和日志完善 |

---

## 核心架构升级

### RAG检索能力优化方案

#### 一、核心差异分析：INFOAGEN vs camel-ai

**1. 协议字段处理差异**

| 维度 | camel-ai | INFOAGEN（当前） | INFOAGEN（优化后） |
|------|----------|-----------------|------------------|
| **协议识别** | 硬编码（HTTP/HTTPS固定） | LLM动态识别 | ✅ LLM动态识别 + 图索引 |
| **协议加权** | `_compute_protocol_boost()` | ❌ 缺失 | ✅ 动态协议加权 |
| **候选池策略** | top_k × 10（动态） | top_k × 1（固定） | ✅ top_k × 10（动态） |
| **重排序模型** | BAAI/bge-reranker-v2-m3 | netease-youdao/bce-reranker-base_v1 | ✅ 升级到 v2-m3 |

**2. camel-ai的硬编码协议逻辑**

```python
# camel-ai: enhanced_rag_system.py:1037-1064
def _compute_protocol_boost(self, query: str, item: Dict[str, Any]) -> float:
    q = (query or "").lower()
    boost = 0.0
    has_http = "http" in q
    has_https = "https" in q
    mentions_http = "http" in doc_text
    mentions_https = "https" in doc_text
    
    # 硬编码的协议匹配
    if has_http and not has_https:
        if mentions_http and not mentions_https:
            boost += 0.05  # ✅ 精准匹配
    # ...
    return boost
```

**问题**：
- ❌ 只支持预设的协议类型（HTTP、HTTPS）
- ❌ 无法动态扩展到新协议（SIP、RTSP等）
- ❌ 需要手动维护协议列表

**3. INFOAGEN的优化方案：动态协议识别 + 图索引**

```python
# INFOAGEN优化实现：workflow_config_generator.py:49-122
def _compute_dynamic_protocol_boost(query: str, item: Dict[str, Any]) -> float:
    """
    动态计算协议类型加权，区别于 camel-ai 的硬编码方式
    
    核心差异：
    - camel-ai: 硬编码 HTTP/HTTPS 关键词和固定加权值
    - INFOAGEN: 从查询和文档 metadata 动态提取协议类型，自动匹配
    """
    q_lower = (query or "").lower()
    meta = item.get("metadata", {}) or {}
    text = (item.get("text") or "")[:500].lower()
    
    query_protocols = set()
    doc_protocols = set()
    
    # 1. 从 metadata 中提取协议类型（INFOAGEN 特有的动态元数据）
    meta_protocols = meta.get("protocol_type", [])
    if isinstance(meta_protocols, list):
        doc_protocols.update([p.lower() for p in meta_protocols if p])
    
    # 2. 从文本中动态识别协议类型（不硬编码协议列表）
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
        query_matches = re.findall(pattern, q_lower)
        query_protocols.update(query_matches)
        doc_matches = re.findall(pattern, text)
        doc_protocols.update(doc_matches)
    
    # 3. 计算动态加权
    boost = 0.0
    exact_matches = query_protocols & doc_protocols
    if exact_matches:
        boost += 0.05 * len(exact_matches)  # 每个精准匹配 +0.05
    
    # 4. 协议泛化支持（如 HTTP 应该也匹配 HTTPS）
    if "http" in query_protocols and "https" in doc_protocols:
        boost += 0.02
    elif "https" in query_protocols and "http" in doc_protocols:
        boost -= 0.02  # HTTPS 需求不应返回 HTTP 文档
    
    return boost
```

**优势**：
- ✅ 完全动态识别协议类型（不限制协议列表）
- ✅ 支持扩展到新协议（SIP、RTSP、DNS等）
- ✅ 从 metadata 和文本双重提取，提高准确性
- ✅ 支持协议泛化（HTTP ⇄ HTTPS）

#### 二、10倍候选池策略

**camel-ai 实现**：

```python
search_top_k = top_k if top_k is not None else self.config.top_k  # 例如 5
initial_top_k = search_top_k * 10 if self.reranker else search_top_k  # 50 个候选
```

**工作流程**：

```
查询 "如何配置HTTP类型的SLB服务"
    ↓
向量检索: 获取 top 50 个候选文档（而非5个）
    ↓
Rerank: 用高质量重排序模型重新评分
    ↓
Protocol Boost: 对HTTP关键词再次加权
    ↓
最终返回: top 5 个最相关文档
```

**INFOAGEN 当前实现**：

```python
# workflow_config_generator.py:136-143
top_k_rerank = kwargs.get("top_k_rerank", 5)
retrieval_multiplier = kwargs.get("retrieval_multiplier", 10)
top_k_retrieval = top_k_rerank * retrieval_multiplier  # 默认：5 × 10 = 50
```

**优势**：
- ✅ 候选池更大（50 vs 20），覆盖面更广
- ✅ 降低"遗漏相关文档"的概率

#### 三、引入 GraphRAG 做图的向量索引

**camel-ai 的 GraphRAG 支持**：

```python
if use_graph and self.graphrag_retriever:
    results = self.graphrag_retriever.retrieve(
        query,
        top_k=initial_top_k,
        expand_graph=self.config.enable_graph_expansion,  # 图扩展
        similarity_threshold=self.config.similarity_threshold,
        source_weights=source_weights  # 来源权重
    )
```

**功能**：
- 基于知识图谱扩展相关实体和关系
- 发现配置步骤之间的依赖关系
- 支持多跳推理

**INFOAGEN 集成方案**：

> ✅ GraphRAG 为可选能力：未构建索引时 workflow 会自动降级为仅向量检索。
> 可执行 `python INAGENT/scripts/init_graphrag.py --init --build` 进行构建。

> ✅ 产品模块修正支持主配置与后处理：
> `INAGENT/doc_local/product_modules_registry.json` 维护模块/关键词/映射，
> `INAGENT/scripts/postprocess_unknown_product_module.py` 可对 unknown 进行批量修正。

##### 3.1 知识图谱构建

**图结构设计**：

```
配置知识图谱
│
├─ 功能模块节点
│  ├─ SLB
│  ├─ LLB
│  └─ 基础网络
│
├─ 步骤类型节点
│  ├─ slb_virtual_services
│  ├─ slb_backend_servers
│  └─ slb_health_checks
│
├─ 协议类型节点
│  ├─ HTTP
│  ├─ HTTPS
│  └─ TCP
│
└─ 关系边
   ├─ DEPENDS_ON (依赖关系)
   │  - slb_backend_servers → slb_virtual_services
   │  - slb_health_checks → slb_backend_servers
   │
   ├─ SUPPORTS_PROTOCOL (协议支持)
   │  - slb_virtual_services → HTTP
   │  - slb_virtual_services → HTTPS
   │
   └─ BELONGS_TO (归属关系)
      - slb_virtual_services → SLB
      - llb_health_checks → LLB
```

##### 3.2 图索引实现

```python
# graphrag_retriever.py（新增）
class GraphRAGRetriever:
    """基于知识图谱的检索器"""
    
    def __init__(
        self, 
        graph_storage,  # 图数据库（如Neo4j）
        embedding_model,  # 嵌入模型
        vector_storage   # 向量存储
    ):
        self.graph = graph_storage
        self.embedding_model = embedding_model
        self.vector_storage = vector_storage
    
    def retrieve(
        self, 
        query: str, 
        top_k: int = 50,
        expand_graph: bool = True,
        max_hops: int = 2
    ) -> List[Dict[str, Any]]:
        """
        图增强检索
        
        步骤：
        1. 向量检索：获取初始候选文档
        2. 实体识别：从查询和文档中提取实体
        3. 图扩展：基于知识图谱扩展相关节点
        4. 重排序：综合向量相似度和图关系评分
        """
        # 1. 向量检索
        vector_results = self._vector_search(query, top_k=top_k * 2)
        
        # 2. 提取实体
        query_entities = self._extract_entities(query)
        doc_entities = [self._extract_entities(doc["text"]) for doc in vector_results]
        
        # 3. 图扩展（如果启用）
        if expand_graph:
            # 从查询实体出发，沿依赖关系扩展
            expanded_nodes = self._expand_graph(
                query_entities, 
                max_hops=max_hops,
                relation_types=["DEPENDS_ON", "SUPPORTS_PROTOCOL"]
            )
            
            # 获取扩展节点的相关文档
            expanded_docs = self._get_docs_by_entities(expanded_nodes)
            
            # 合并结果
            all_results = vector_results + expanded_docs
        else:
            all_results = vector_results
        
        # 4. 图关系评分
        scored_results = []
        for doc in all_results:
            doc_entities = self._extract_entities(doc["text"])
            
            # 向量相似度
            vector_score = doc.get("similarity_score", 0.0)
            
            # 图关系评分
            graph_score = self._compute_graph_score(
                query_entities, 
                doc_entities,
                max_hops=max_hops
            )
            
            # 综合评分
            final_score = vector_score * 0.6 + graph_score * 0.4
            doc["final_score"] = final_score
            scored_results.append(doc)
        
        # 5. 排序并返回 top_k
        scored_results = sorted(
            scored_results, 
            key=lambda x: x["final_score"], 
            reverse=True
        )
        return scored_results[:top_k]
    
    def _expand_graph(
        self, 
        entities: List[str], 
        max_hops: int = 2,
        relation_types: List[str] = None
    ) -> List[str]:
        """
        从实体出发，沿关系扩展
        
        示例：
        查询：如何配置HTTP类型的SLB服务
        实体：[HTTP, SLB]
        
        扩展路径（max_hops=2）：
        HTTP → SUPPORTS_PROTOCOL → slb_virtual_services
        SLB → BELONGS_TO → slb_virtual_services
        slb_virtual_services → DEPENDS_ON → slb_backend_servers
        slb_virtual_services → DEPENDS_ON → slb_health_checks
        
        返回：[HTTP, SLB, slb_virtual_services, slb_backend_servers, slb_health_checks]
        """
        expanded = set(entities)
        current_level = set(entities)
        
        for hop in range(max_hops):
            next_level = set()
            for entity in current_level:
                # 查询图数据库，获取相关节点
                neighbors = self.graph.get_neighbors(
                    entity, 
                    relation_types=relation_types
                )
                next_level.update(neighbors)
            
            expanded.update(next_level)
            current_level = next_level
        
        return list(expanded)
    
    def _compute_graph_score(
        self, 
        query_entities: List[str], 
        doc_entities: List[str],
        max_hops: int = 2
    ) -> float:
        """
        计算图关系评分
        
        评分策略：
        - 直接匹配（0跳）：1.0
        - 1跳关系：0.5
        - 2跳关系：0.25
        """
        score = 0.0
        
        for q_entity in query_entities:
            for d_entity in doc_entities:
                # 计算最短路径
                path_length = self.graph.shortest_path(q_entity, d_entity)
                
                if path_length == 0:
                    score += 1.0
                elif path_length == 1:
                    score += 0.5
                elif path_length == 2:
                    score += 0.25
        
        # 归一化
        if query_entities and doc_entities:
            score /= (len(query_entities) * len(doc_entities))
        
        return score
```

##### 3.3 集成到现有流程

```python
# workflow_config_generator.py 修改
def initialize_rag_system(use_graphrag: bool = True):
    """初始化RAG系统（支持GraphRAG）"""
    # ... 现有代码 ...
    
    # 初始化 HybridRetriever
    hybrid_retriever = HybridRetriever(
        embedding_model=embedding_model,
        vector_storage=storage
    )
    
    # 如果启用GraphRAG，初始化图检索器
    if use_graphrag:
        from INAGENT.graphrag_retriever import GraphRAGRetriever
        
        # 加载图数据库
        graph_storage = load_graph_storage()
        
        # 创建GraphRAG检索器
        graphrag_retriever = GraphRAGRetriever(
            graph_storage=graph_storage,
            embedding_model=embedding_model,
            vector_storage=storage
        )
        
        return hybrid_retriever, reranker, graphrag_retriever
    else:
        return hybrid_retriever, reranker, None
```

#### 四、完整的RAG检索流程（优化后）

```python
def _adaptive_rag_retrieval_v2(
    hybrid_retriever, 
    reranker, 
    graphrag_retriever,  # 新增
    job_content, 
    decomposition_result=None, 
    **kwargs
):
    """
    改进的RAG检索实现，集成以下优化：
    1. GraphRAG 图扩展检索
    2. 10倍候选池策略
    3. 动态协议加权
    4. 更好的重排序模型
    """
    top_k_rerank = kwargs.get("top_k_rerank", 5)
    retrieval_multiplier = kwargs.get("retrieval_multiplier", 10)
    top_k_retrieval = top_k_rerank * retrieval_multiplier  # 50
    use_graphrag = kwargs.get("use_graphrag", graphrag_retriever is not None)
    
    logger.info(f"[RAG] 使用动态候选池策略：初始检索 {top_k_retrieval} 个候选，rerank 后返回 top {top_k_rerank}")
    
    # 1. 混合检索（向量 + BM25） 或 GraphRAG
    if use_graphrag:
        logger.info("[RAG] 使用 GraphRAG 检索")
        retrieved_context = graphrag_retriever.retrieve(
            job_content,
            top_k=top_k_retrieval,
            expand_graph=True,
            max_hops=2
        )
    else:
        logger.info("[RAG] 使用混合检索（向量 + BM25）")
        retrieved_result = hybrid_retriever.query(
            job_content, 
            top_k=top_k_retrieval,
            return_detailed_info=True
        )
        retrieved_context = retrieved_result.get("Retrieved Context", [])
    
    if not retrieved_context:
        return "", {}, decomposition_result or {}
    
    # 2. 提取文本和 metadata
    documents = []
    for doc in retrieved_context:
        if isinstance(doc, dict):
            text = doc.get("text", "") or doc.get("page_content", "") or doc.get("content", "")
            metadata = doc.get("metadata", {})
        else:
            text = str(doc)
            metadata = {}
        
        if text:
            documents.append({
                "text": text,
                "metadata": metadata,
                "similarity score": doc.get("similarity score", 0.0) if isinstance(doc, dict) else 0.0,
                "graph_score": doc.get("graph_score", 0.0) if isinstance(doc, dict) else 0.0
            })
    
    # 3. 调用 reranker 重排序
    if reranker and documents:
        try:
            logger.info(f"[RAG] 调用 reranker 重排序 {len(documents)} 个文档，目标 top_{top_k_rerank}")
            reranked_docs = reranker.query(
                query=job_content,
                retrieved_result=documents,
                top_k=top_k_rerank * 2  # 保留 2 倍，用于后续协议加权
            )
            documents = reranked_docs
            logger.info(f"[RAG] Rerank 完成，返回 {len(documents)} 个文档")
        except Exception as e:
            logger.warning(f"Rerank 失败: {e}，使用原始结果")
            documents = documents[:top_k_rerank * 2]
    
    # 4. 应用动态协议加权
    logger.info("[RAG] 应用动态协议加权")
    for doc in documents:
        boost = _compute_dynamic_protocol_boost(job_content, doc)
        if boost != 0.0:
            original_score = doc.get("similarity score", 0.0)
            doc["similarity score"] = original_score + boost
            doc["protocol_boost"] = boost
            logger.debug(f"[RAG] 协议加权: {boost:+.3f} (原始: {original_score:.3f} -> {doc['similarity score']:.3f})")
    
    # 5. 重新排序（应用加权后）
    documents = sorted(documents, key=lambda x: x.get("similarity score", 0.0), reverse=True)
    
    # 6. 提取最终上下文（top_k_rerank）
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
```

#### 五、性能提升预期

| 优化项 | 预期提升 | 实施难度 | 优先级 |
|-------|---------|---------|-------|
| 动态协议加权 | +15-20% 准确率 | ⭐ 低 | 🔥 **P0** |
| 10倍候选池策略 | +10-15% 召回率 | ⭐ 低 | 🔥 **P0** |
| 升级 Rerank 模型 | +10-15% 相关性 | ⭐⭐ 中 | 🔥 **P0** |
| GraphRAG 图索引 | +20-30% 复杂查询 | ⭐⭐⭐ 高 | ⭐ P1 |

**综合预期**：实施 P0 + P1 优化后，RAG 召回准确率提升 **40-50%**。

#### 六、实施路线图

**Phase 1: 快速优化（1-2天）**

1. ✅ 添加 `_compute_dynamic_protocol_boost()` 函数
2. ✅ 修改候选池大小为 50
3. ✅ 在重排序后应用协议加权
4. ✅ 测试验证效果

**Phase 2: 模型升级（3-5天）**

1. 🔄 升级 Rerank 模型到 BAAI/bge-reranker-v2-m3
2. 🔄 更新配置并全面回归测试

**Phase 3: GraphRAG 集成（1-2周）**

1. 📋 构建知识图谱（配置依赖关系）
2. 📋 实现 GraphRAGRetriever
3. 📋 集成到现有 workflow
4. 📋 A/B 测试对比效果

---

## 数据库重构完成

### 一、已完成的修改

#### 1. auto_convert.py 修改

##### 1.1 添加去除章节编号功能
- **新增函数**：`_remove_section_number(title: str) -> str`
  - 移除开头的数字编号（如"11.3.1. "、"11.3.1 "、"第11章 "等）
  - 只保留有意义的标题内容
  - 示例："11.3.1. HTTP配置" -> "HTTP配置"

##### 1.2 改进LLM Prompt
- **强调核心元数据元素（P0）**：
  - `product_module`：必须首先确定，完全基于内容分析，不能是协议类型或章节编号
  - `protocol_type`：协议类型是约束条件，不是功能模块
  - `step_type`：使用通用名称，后续添加模块前缀
  - `section_title`：**必须去除编号**
  - `parent_section`：**必须去除编号**
  - `function_hierarchy`：格式："SLB > Health Check > HTTP"（不包含编号）

##### 1.3 移除硬编码验证
- **修改前**：如果LLM返回的模块不在`valid_product_modules`列表中，会被强制设置为"基础网络"
- **修改后**：信任LLM的判断结果，只做基本的规范化处理（处理空字符串、unknown等）
- **效果**：支持动态识别新模块，不限制在预设列表中

#### 2. build_function_structure_index.py 修改

##### 2.1 动态模块统计
- **过滤协议类型**：在统计`product_module`时，过滤掉协议类型（HTTP、TCP等）和unknown
- **添加模块前缀**：为步骤类型添加模块前缀（如`slb_health_checks`）
- **收集协议类型**：为每个模块收集使用的协议类型
- **收集章节标题**：为每个模块收集出现的章节标题（用于层级推断）

##### 2.2 新增函数：_build_prefixed_step_type
- **功能**：构建带模块前缀的步骤类型
- **格式**：`{module_name}_{base_step_type}`
- **示例**：
  - SLB的健康检查：`slb_health_checks`
  - LLB的健康检查：`llb_health_checks`
  - 基础网络的健康检查：`network_health_checks`

##### 2.3 新增函数：infer_module_hierarchy
- **功能**：动态推断模块的层级和依赖关系
- **策略**：
  1. 基于章节标题和内容推断层级（基础层/应用层/安全层/高级功能层）
  2. 基于CLI命令前缀推断模块关系
  3. 基于配置示例推断依赖关系
- **输出**：为每个模块添加`level`、`order`、`depends_on`字段

### 二、功能树结构（v3.0）

```json
{
  "version": "3.0",
  "modules": {
    "基础网络": {
      "module_name": "基础网络",
      "level": "基础层",
      "order": 1,
      "depends_on": [],
      "step_types": {
        "network_network_basics": {
          "count": 292,
          "base_step_type": "network_basics",
          "is_advanced": false,
          "order": 1
        },
        "network_routing_config": {
          "count": 58,
          "base_step_type": "routing_config",
          "is_advanced": false,
          "order": 2
        }
      },
      "protocol_types": ["IP", "IPv4", "IPv6"],
      "keywords": [...]
    },
    "SLB": {
      "module_name": "SLB",
      "level": "应用层",
      "order": 2,
      "depends_on": ["基础网络"],
      "step_types": {
        "slb_virtual_services": {
          "count": 116,
          "base_step_type": "virtual_services",
          "is_advanced": false,
          "order": 1,
          "protocol_support": ["HTTP", "HTTPS", "TCP", "UDP"]
        },
        "slb_backend_servers": {
          "count": 85,
          "base_step_type": "backend_servers",
          "is_advanced": false,
          "order": 2
        },
        "slb_health_checks": {
          "count": 67,
          "base_step_type": "health_checks",
          "is_advanced": false,
          "order": 3,
          "related_step_types": ["llb_health_checks", "ha_health_checks"]
        },
        "slb_cookie_persistence": {
          "count": 15,
          "base_step_type": "cookie_persistence",
          "is_advanced": true,
          "order": 4
        }
      },
      "protocol_types": ["HTTP", "HTTPS", "TCP", "UDP", "FTP"],
      "keywords": [...]
    }
  },
  "scenarios": {
    "SLB_HTTP_FULL_CONFIG": {
      "scenario_id": "SLB_HTTP_FULL_CONFIG",
      "product_modules": ["SLB"],
      "protocol_types": ["HTTP"],
      "required_steps": [
        "network_network_basics",
        "slb_virtual_services",
        "slb_backend_servers",
        "slb_health_checks"
      ],
      "advanced_features": [
        "slb_cookie_persistence",
        "slb_qos"
      ],
      "configuration_order": [
        "network_network_basics",
        "slb_virtual_services",
        "slb_backend_servers",
        "slb_health_checks",
        "slb_cookie_persistence"
      ]
    }
  }
}
```

### 三、关键改进点

#### 1. 去除章节编号
- ✅ `section_title`和`parent_section`不再包含编号
- ✅ `function_hierarchy`不包含编号
- ✅ 功能树节点显示名称不包含编号

#### 2. 动态模块识别
- ✅ LLM完全自主识别功能模块，不依赖预设列表
- ✅ 支持新模块自动发现
- ✅ 过滤掉协议类型作为product_module的情况

#### 3. 模块前缀
- ✅ 步骤类型使用带模块前缀的格式（如`slb_health_checks`）
- ✅ 可以区分不同模块的同名步骤类型
- ✅ 场景定义中的`required_steps`使用带前缀的格式

#### 4. 层级和依赖关系
- ✅ 每个模块包含`level`（基础层/应用层/安全层/高级功能层）
- ✅ 每个模块包含`order`（配置顺序）
- ✅ 每个模块包含`depends_on`（依赖的模块列表）

#### 5. 协议类型分离
- ✅ 场景定义中，`product_modules`只包含功能模块
- ✅ 协议类型单独列在`protocol_types`字段中
- ✅ 每个模块包含`protocol_types`列表，记录支持的协议类型

---

## 动态模块树生成

### 核心问题

#### 问题1: 硬编码的模块列表
- `auto_convert.py`中从`mineru.json`读取`product_modules`列表，限制了LLM只能识别预设的模块
- Prompt中硬编码了SLB、GSLB、LLB等模块名称，LLM被引导识别这些模块
- 如果LLM返回的模块不在验证列表中，会被强制设置为"基础网络"

#### 问题2: 先入为主的模块名称
- 验证逻辑限制了LLM的自主识别能力
- 无法动态发现新模块

#### 问题3: 模块树静态生成
- 功能树基于预设的模块列表构建，无法动态发现新模块

### 解决方案

#### 方案1: 移除硬编码，让LLM完全自主识别

**修改 LLM Prompt**：

```python
prompt = (
    "You are analyzing a technical documentation chunk. "
    "Extract comprehensive metadata from the content.\n\n"
    
    "CRITICAL RULES FOR 'product_module' (功能模块识别):\n"
    "1. **完全基于内容分析**：不要依赖预设列表，根据数据块标题和内容自主判断功能模块\n"
    "2. **识别策略**（按优先级）：\n"
    "   a) 从章节标题识别：分析章节标题中的功能关键词\n"
    "   b) 从命令前缀识别（CLI文档）：slb命令 -> SLB模块\n"
    "   c) 从内容关键词识别：virtual service/backend service -> 负载均衡模块\n"
    "   d) 从配置示例识别：配置示例中提到的功能特性 -> 对应功能模块\n"
    "3. **模块命名规则**：\n"
    "   - 使用简洁的功能名称（如SLB、LLB、基础网络等）\n"
    "   - 如果无法确定具体模块，使用'基础网络'作为默认值\n"
    "4. **协议类型 vs 功能模块**：\n"
    "   - HTTP、HTTPS、TCP、UDP等是协议类型，不是功能模块\n"
    "   - 功能模块应该是功能性的（如SLB、LLB、基础网络等）\n"
)
```

**移除验证逻辑限制**：

```python
# 移除硬编码验证，信任LLM的判断
if llm_meta.get("product_module"):
    pm_value = str(llm_meta["product_module"]).strip()
    if pm_value.lower() in ["unknown", "未知", ""]:
        meta["product_module"] = "unknown"
    else:
        # 直接使用LLM识别的模块名称
        meta["product_module"] = pm_value
else:
    meta["product_module"] = "基础网络"
```

#### 方案2: 动态构建模块树

```python
def extract_metadata_statistics(kb_path: Path) -> Dict[str, Any]:
    """
    从knowledge_base.json统计实际使用的metadata，完全动态提取模块
    """
    # 动态收集所有product_module（不限制）
    product_modules: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "keywords": set(),
        "step_types": defaultdict(int),
        "protocol_types": set(),
        "section_titles": set(),
    })
    
    for chunk in chunks:
        metadata = chunk.get("metadata", {})
        pm = metadata.get("product_module")
        
        if pm and pm != "unknown":  # 排除unknown
            product_modules[pm]["count"] += 1
            # ... 统计其他信息 ...
            
            # 收集协议类型
            pt = metadata.get("protocol_type")
            if pt:
                if isinstance(pt, list):
                    product_modules[pm]["protocol_types"].update(pt)
                else:
                    product_modules[pm]["protocol_types"].add(pt)
            
            # 收集章节标题
            section_title = metadata.get("section_title")
            if section_title:
                product_modules[pm]["section_titles"].add(section_title)
    
    return {
        "product_modules": {
            k: {
                "count": v["count"],
                "keywords": limit_keywords(v["keywords"]),
                "step_types": dict(v["step_types"]),
                "protocol_types": list(v["protocol_types"]),
                "section_titles": list(v["section_titles"])[:10],
            }
            for k, v in product_modules.items()
        }
    }
```

#### 方案3: 动态推断模块层级和依赖关系

```python
def infer_module_hierarchy(
    product_modules_stats: Dict[str, Any],
    cli_hierarchy: Dict[str, Any],
    document_hierarchy: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """
    动态推断模块的层级和依赖关系
    """
    module_hierarchy = {}
    
    for module_name, module_data in product_modules_stats.items():
        section_titles = module_data.get("section_titles", [])
        keywords = module_data.get("keywords", [])
        
        # 推断层级
        level = "其他"
        order = 999
        
        title_text = " ".join(section_titles).lower()
        keyword_text = " ".join(keywords).lower()
        
        if any(kw in title_text or kw in keyword_text for kw in ["基础", "网络", "路由", "接口"]):
            level = "基础层"
            order = 1
        elif any(kw in title_text or kw in keyword_text for kw in ["负载均衡", "slb", "llb", "gslb"]):
            level = "应用层"
            order = 2
        elif any(kw in title_text or kw in keyword_text for kw in ["安全", "firewall", "acl", "ssl"]):
            level = "安全层"
            order = 3
        
        # 推断依赖关系
        depends_on = []
        if level == "应用层":
            depends_on.append("基础网络")
        elif level == "安全层":
            depends_on.extend(["基础网络", "SLB"])
        
        module_hierarchy[module_name] = {
            "level": level,
            "order": order,
            "depends_on": depends_on,
        }
    
    return module_hierarchy
```

---

## 限速和故障转移

### 核心功能

#### 1. 滑动窗口限速器

```python
class _SlidingWindowRateLimiter:
    """
    滑动窗口限速器
    
    功能：
    - 实现 RPM（每分钟请求数）和 TPM（每分钟 Token 数）的滑动窗口限速
    - 自动清理过期的时间戳
    - 同时检查 RPM 和 TPM 限制
    - 需要等待时自动休眠
    """
```

#### 2. 统一 API 调用函数

```python
def _call_llm_with_retry_and_fallback(
    client: Any,
    base_url: str,
    model: str,
    messages: List[Dict[str, str]],
    max_retries: int = 6,           # 最大重试次数
    initial_delay: float = 1.0,     # 初始延迟（秒）
    exponential_base: float = 2.0,  # 指数退避基数
    jitter: bool = True,            # 是否添加随机抖动
) -> Any:
    """
    统一的 LLM 调用函数
    
    核心特性：
    - ✅ 限速控制：自动应用硅基流动限速器
    - ✅ 指数退避重试：参考硅基流动文档实现
    - ✅ 错误处理：区分限速错误和其他错误
    """
```

#### 3. 限速标准（硅基流动）

| 模型类型 | RPM | TPM |
|---------|-----|-----|
| 对话模型 | 1000 | 50000 |
| 嵌入模型 | 2000 | 500000 |
| 重排序模型 | 2000 | 500000 |

### 指数退避重试算法

```python
# 延迟计算
delay = initial_delay * (exponential_base ^ num_retries)
if jitter:
    delay_with_jitter = delay * (1 + random.random())

# 重试流程
while num_retries < max_retries:
    try:
        return api_call()
    except RateLimitError:
        num_retries += 1
        time.sleep(delay_with_jitter)
        delay *= exponential_base
```

**优势**：
- 快速首次重试（1秒）
- 逐渐增加延迟（2秒、4秒、8秒...）
- 随机抖动避免重试风暴
- 达到最大重试次数后抛出异常

### 环境变量配置

```env
# 硅基流动 API 配置
SILICONFLOW_API_KEY=your-api-key
SILICONFLOW_API_BASE_URL=https://api.siliconflow.cn/v1

# 模型配置
SILICONFLOW_CHAT_MODEL=deepseek-ai/DeepSeek-R1-0528-Qwen3-8B
SILICONFLOW_EMBEDDING_MODEL=BAAI/bge-m3
SILICONFLOW_RERANKER_MODEL=BAAI/bge-reranker-v2-m3

# 可选：限速参数
SILICONFLOW_CHAT_RPM=1000
SILICONFLOW_CHAT_TPM=50000
SILICONFLOW_EMBEDDING_RPM=2000
SILICONFLOW_EMBEDDING_TPM=500000
SILICONFLOW_RERANKER_RPM=2000
SILICONFLOW_RERANKER_TPM=500000
```

---

## 参考文档

### 核心文档

1. **RAG对比分析**
   - 位置：`RAG_对比分析报告.md`
   - 内容：camel-ai vs INFOAGEN RAG实现对比

2. **动态模块树生成**
   - 位置：`INAGENT/动态模块树生成方案.md`
   - 内容：移除硬编码、动态识别模块

3. **功能树生成流程**
   - 位置：`INAGENT/功能树生成流程分析.md`
   - 内容：功能树构建流程和问题分析

4. **核心元数据元素**
   - 位置：`INAGENT/核心元数据元素与功能树设计.md`
   - 内容：P0元数据元素定义和功能树设计

5. **模块树结构设计**
   - 位置：`INAGENT/模块树结构设计推荐.md`
   - 内容：完整模块树结构和推荐实现

6. **数据库重构总结**
   - 位置：`INAGENT/数据库重构完成总结.md`
   - 内容：已完成的修改和关键改进点

7. **限速和故障转移**
   - 位置：`INAGENT/限速和故障转移完整实现说明.md`
   - 内容：硅基流动限速和指数退避重试实现

### 外部参考

- [硅基流动限速处理文档](https://github.com/siliconflow/siliconcloud-cookbook/blob/main/examples/rate-limit/how-to-handle-rate-limit-in-siliconcloud.ipynb)
- [camel-ai Enhanced RAG System](https://github.com/camel-ai/camel)

---

## 下一步计划

### P0 优先级（立即实施）

1. ✅ 动态协议加权
2. ✅ 10倍候选池策略
3. ✅ 升级 Rerank 模型
4. ⏳ 测试验证效果

### P1 优先级（1-2周）

1. ⏳ GraphRAG 知识图谱构建
2. ⏳ GraphRAGRetriever 实现
3. ⏳ 集成到现有 workflow
4. ⏳ A/B 测试对比效果

### P2 优先级（长期优化）

1. 📋 动态限速调整
2. 📋 监控和统计
3. 📋 缓存机制
4. 📋 批量处理优化

---

**更新时间**：2026-01-29  
**版本**：v3.0  
**维护人**：INFOAGEN Team
