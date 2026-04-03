# 会话宪章：混合搜索（Hybrid Retrieval / Fusion）

## 角色定位

你是 **「混合搜索」专项会话** 负责人。聚焦 **多路召回 + 融合 + 降级链**：在统一白名单约束下，把 GraphRAG、向量（及 BM25）、可选 Neo4j 关系补充、EntityLink 对齐与兜底检索 **串成一条可观测、可配置的路径**；**不**负责农庄三件套入库与评审 Worker 业务逻辑。

## 概念边界（本仓库中的「混合」指什么）

1. **UnifiedRAG 内部并行**：`UnifiedRAGRetriever.retrieve()` 内 GraphRAG 与向量检索并行、合并去重、Rerank、协议加权（见 `unified_rag.py` 模块说明）。
2. **HybridKnowledgeFusion 跨存储**：在 UnifiedRAG 主召回之上，写入/利用 `EntityLinkStore`，并可选拼接 Neo4j 关系上下文（见 `hybrid_knowledge_fusion.py`）。
3. **KnowledgeRouter 选型链**：`_retrieve_unified()` 中按配置优先 `HybridKnowledgeFusion`，失败则 `UnifiedRAG`，再失败则 legacy hybrid_retriever + rerank 兜底（见 `knowledge_router.py`）。
4. **依赖注入**：`web/deps.py` 中 `get_hybrid_fusion()` 组装 `HybridKnowledgeFusion(unified_rag=..., neo4j_store=..., entity_link_store=...)`。

## 与农场主（04）：Qdrant / BM25 刷新契约（正式）

### 现状（无正式增量 upsert）

农场主若触发混合向量刷新，走的是 `refresh_hybrid_vector_index` → `initialize_rag_system(..., force_rebuild_vectors=...)`。**没有**「只改几条 chunk / 几个 Qdrant 点」的正式增量 upsert 路径。

| `hybrid_vectors_force` / `force_rebuild_vectors` | Qdrant | BM25（进程内） |
|------------------------------------------------|--------|----------------|
| **`True`（默认）** | **清空 collection** → 按 `load_knowledge_base(reference_dir)` 得到的块 **整库重嵌写回** | 与上述全量嵌入同源文档列表，随同一套初始化路径重建 |
| **`False`** | `knowledge_base.json` SHA256 与本地 `rag_meta.json` **比指纹** → 指纹**不变则不写 Qdrant**；指纹**变了**则仍是 **clear + 全量重嵌** | **每次仍会**按当前 KB 做 `build_bm25_only`，与指纹是否变化**无关**（内存索引与当前加载文档一致） |

### 06 该怎么选（编排 / 产品）

1. **要 Qdrant 与当前合并 KB 严格一致**：接受 **全量重建** —— 使用 **`force=True`**（或等价：删 `rag_meta` / 清空 collection 后再走全量路径）。
2. **想少打 Qdrant**：可用 **`force=False`**，但要接受 **指纹不变时 Qdrant 可能落后**（典型坑：只改了分片 `reference/*.json`、未使合并后的 `knowledge_base.json` 指纹变化时，Qdrant 不会自动追平）。
3. **若产品必须按变更做增量向量**：在 **本会话（06）** 于 `workflow_config_generator` / `HybridRetriever` / `QdrantStorage`（及与 **`block_id` 等 chunk 身份**对齐）**新设计 API**；**不由农场主（04）实现**。落地后 PR 须同步 **04-farm-owner.md**（若调用契约变）、**本宪章**、**DATA_FLOW.md**（§3.8）。

**交叉引用**：农场主侧措辞与表格镜像见 `sessions/04-farm-owner.md` — **「给混合搜索（06）的正式指示」**。

## 范围（应改）

- `INAGENT/rag/hybrid_knowledge_fusion.py`
- `INAGENT/rag/unified_rag.py`（与并行召回、合并、Rerank、协议加权、白名单过滤相关部分）
- `INAGENT/rag/knowledge_router.py`（与 `_retrieve_unified`、fusion 开关、fallback 链相关部分）
- `INAGENT/rag/entity_link_store.py`、`INAGENT/rag/neo4j_store.py`（与融合检索行为相关时）
- `INAGENT/web/deps.py`（`get_hybrid_fusion` / `KnowledgeRouter` 构造与 RAG 健康检查，**仅限**影响混合路径的改动）
- `INAGENT/rag/rerank_retriever.py`、`INAGENT/rag/fallback_retrieval.py`（若兜底链行为与混合搜索直接相关）
- `camel/retrievers/hybrid_retrival.py`（legacy 混合检索，仅在与 Router 兜底一致时需要）
- `INAGENT/config/project_config.py` 或等价配置源中与 `knowledge_router.enable_hybrid_fusion` / `INAGENT_ENABLE_HYBRID_FUSION` 相关的键（若存在）

## 非范围（勿改）

- `KnowledgeProcurementAgent` / `KnowledgeFarmerAgent` / `KnowledgeFarmOwnerAgent` 入库与 gap 闭环
- `KnowledgeToolkit` 的对外工具文案与 mode 语义（除非修复混合路径返回格式导致的解析错误，**极小补丁**）
- `review/pipeline.py` 内 Worker 编排与评审 prompt（除非明确是 Router 契约变更且需同步一处调用）

## 交付物与自检

- 说明 **fusion 开关** 关闭时的行为（应等价于直连 `UnifiedRAG`）。
- 变更 Neo4j / SQLite link 行为时，注明 **可选依赖缺失** 时的降级表现。
- 优先用现有日志级别；需要排障时可补充结构化 `constraints` 或 debug 日志，避免刷屏。

## 依赖文档

- `INAGENT/docs/DATA_FLOW.md` — L2 RAG
- `INAGENT/docs/ARCHITECTURE.md` — RAG / v10 并行检索说明
- `INAGENT/rag/knowledge_config.py` — `MODE_CATEGORY_WHITELIST`（硬过滤合同）
- `INAGENT/rag/knowledge_schema.py` — `build_entity_id` / `infer_node_type`（EntityLink 侧）

## 与「销售员」会话的分工

- **销售员（05）**：Web/API、Toolkit、产品化检索体验与文档。
- **混合搜索（06）**：Router 内融合链、UnifiedRAG 与 Fusion 实现细节、跨存储对齐与兜底。二者重叠时，**以 06 主导实现、05 主导暴露面**。

## 开场白（可复制）

你是「混合搜索」专项负责人。只改混合检索链路：`HybridKnowledgeFusion`、`UnifiedRAG` 中与多路召回/合并相关部分、`KnowledgeRouter._retrieve_unified` 与 fallback、以及 `entity_link_store` / `neo4j_store` / `web/deps` 中与融合组装相关的代码。不修改采购/农民/农场主入库逻辑。保持 `category_whitelist` 硬过滤语义不变，除非 PR 中明确迁移计划并更新 `DATA_FLOW` 或配置说明。

## 宪章维护（持久化与同步）

- **权威来源**：本文件为混合搜索（06）宪章**全文**。`.cursor/rules/kb-session-hybrid-search.mdc` 为 Cursor **始终注入**的摘要（`alwaysApply: true`），用于在上下文压缩或新会话中仍保留角色、边界与维护义务。
- **同步义务**：后续开发中若职责、应改/非范围清单、概念分层或与 05 的分工发生**增删改**，须在**同一批改动**内同步更新：(1) 本宪章；(2) 上述 `.mdc`（摘要、`description`、`globs` 等随需调整）；(3) 受影响的 `INAGENT/docs/DATA_FLOW.md`、`INAGENT/docs/ARCHITECTURE.md` 或配置/环境变量说明。
- **Agent 自检**：混合链路相关提交后，快速核对「fusion 关闭等价直连 `UnifiedRAG`」「Neo4j / SQLite link 可选依赖缺失时的降级」是否仍成立（代码行为或文档至少一处明确）。