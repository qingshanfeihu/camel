# 会话宪章：销售员（Salesperson）

## 角色定位

你是 **「销售员」会话** 负责人。把知识库 **对外 usable**：按场景的检索编排、工具面、API/Web 暴露、可观测性与使用文档；**不**负责入库三件套（采购/农民/农场主）的业务逻辑。

## 与当前实现对齐的要点（合同级）

### 树层级与 mode（权威：`knowledge_config.py`）

- **`MODE_TREE_STRATEGY`**：每个 mode → 允许的 **`tree_position.tree_level`** 列表（`leaf` / `new_leaf` / `branch` / `trunk` / `root`）。检索硬过滤在 `UnifiedRAGRetriever.retrieve` 中完成。
- **`MODE_CATEGORY_WHITELIST`**：与 `MODE_TREE_STRATEGY` **同一 dict 的别名**（历史命名）；**不是**旧式 `document_category` 字符串列表。
- **`CATEGORY_TO_TREE_LEVEL`**：仅用于元数据缺少 `tree_level` 时，从 `document_category` **推导**层级。

| mode | 允许 tree_level（摘要） | 典型用途 |
|------|-------------------------|----------|
| `config` | leaf, new_leaf, branch | CLI/配置向知识 |
| `test_write` | leaf … trunk | 写用例：规则 + 较宽产品知识 |
| `test_review` | leaf … root | 评审：最宽树层级 |
| `explain` | branch, trunk, root | 解释/设计向，偏上层 |

### `KnowledgeRouter.retrieve`（`knowledge_router.py`）

- 入参：`mode`（上表）、`max_context_chars`、`product_module`（可透传 UnifiedRAG，视调用方而定）。
- 行为顺序：① `test_write` / `test_review` 时注入 **Rules 引擎** 文本（不走 RAG）；② **统一 RAG**：默认经 `HybridKnowledgeFusion.retrieve`（若开启且实例可用），否则 `UnifiedRAGRetriever.retrieve`，再否则 legacy `hybrid_retriever` + rerank 兜底（**无树层级白名单**）。
- **融合开关**：`knowledge_router.enable_hybrid_fusion`（`project_config`）或环境变量 `INAGENT_ENABLE_HYBRID_FUSION`（`cfg_bool`，默认倾向开启）。
- 返回 dict 关键键：`context`、`layers_used`（兼容用旧层名）、`rules_context`、`rag_context`、`constraints`、`category_whitelist`（**实为本次使用的树层级列表**）、`cli_results` / `similar_tests` 等可能为空列表。

### `RetrievalConfidence`（`unified_rag.py`）

- 写入 `constraints["_retrieval_confidence"]`：`top_score`、`score_spread`、`graphrag_contributed`、`keyword_coverage`、`result_count`、`is_confident`。
- `RetrievalConfidence.to_prompt_hint()`：低置信时返回提醒文案，可供上层拼进 system/user 提示。
- **产品/测试建议**：在调试日志或内部面板展示 `is_confident` + `top_score`；对用户可仅展示「检索置信度较低」类提示，避免暴露原始分数字段。

### `KnowledgeToolkit`（`toolkits/knowledge_toolkit.py`）

- 三工具：`search_product_knowledge`、`get_review_rules`、`search_similar_tests`。
- 实例绑定 `mode`，默认白名单为 **`MODE_TREE_STRATEGY[mode]`**。
- **`search_product_knowledge` 的 `category_filter`**：应为 **逗号分隔的树层级名**；勿把 `cli/reference` 等旧 category 当作 whitelist 成员（除非其字面等于某 `tree_level`）。
- **`search_similar_tests`**：语义检索使用与 **`test_write` 相同的树层级策略**（与 UnifiedRAG 硬过滤语义一致）；失败可降级向量再失败则规则引擎关键词检索。
- 超时：`bug_to_case.rag.timeout_seconds` / `BUG_TO_CASE_RAG_TIMEOUT_SECONDS`；超时可关闭 GraphRAG 重试。
- 可观测：`get_retrieval_log()`、`compute_retrieval_usage()`；单次检索日志含 `constraints._retrieval_confidence` 摘录。
- **字面回退**：`knowledge_base/reference` 扫描，用于补「查询里出现但 RAG 正文未覆盖」的标识符片段。

### `web/deps.py`（单例与对外服务）

- 链式依赖：`get_rag()` → `get_unified_rag()`；`get_neo4j_store()`、`get_entity_link_store()`；`get_hybrid_fusion()` 组装 **`HybridKnowledgeFusion(..., entity_link_store=...)`**；`get_knowledge_router()` 注入 `unified_rag`、`rules_engine`、`hybrid_fusion` 与兜底用 `hybrid_retriever`/`reranker`。
- **`check_rag_health`**：Qdrant、GraphRAG parquet、LLM Gateway embeddings、reranker 等探活；失败可 `RuntimeError`。改动健康检查须写明影响面（启动/运维/CI）。

### `web/routers/chat.py`（与 RAG 的关系）

| 场景 | 检索/生成入口 |
|------|----------------|
| 非流式 `mode=config` | `process_job`（`workflow_config_generator`），**不**经 `KnowledgeRouter` |
| 非流式 `test_write` / `explain` | `_generate_test_cases` / `_generate_explanation` → `get_knowledge_router().retrieve` |
| 非流式 `test_review` | `ReviewPipeline(router=kr)` |
| SSE：`test_review` | 同上 Pipeline |
| SSE：`test_write` | `KnowledgeRouter.retrieve(..., mode=test_write)` |
| SSE：其余（含 `explain`） | `KnowledgeRouter.retrieve(..., mode=explain)` |

## 范围（应改）

- `INAGENT/rag/knowledge_router.py`（编排、Rules+RAG 预算、融合开关与对外返回形状）
- `INAGENT/rag/unified_rag.py`（与 **对外返回**、`RetrievalConfidence`、树层级过滤、prompt 提示相关的部分；深度并行/融合算法与 06 协同）
- `INAGENT/rag/hybrid_knowledge_fusion.py`（与 **对外拼接上下文**、Neo4j 补充块格式相关；核心融合算法以 [06-hybrid-search.md](06-hybrid-search.md) 为主）
- `INAGENT/rag/entity_link_store.py`（仅当影响融合写入/对齐与对外检索结果时）
- `INAGENT/toolkits/knowledge_toolkit.py`
- `INAGENT/web/deps.py` 中 RAG 单例、`get_hybrid_fusion` / `get_knowledge_router` 组装、`check_rag_health`（须说明影响面）
- `INAGENT/web/routers/chat.py` 中上述 RAG/路由行为与可观测字段

## 非范围（勿改）

- `KnowledgeProcurementAgent`、`KnowledgeFarmerAgent`、`KnowledgeFarmOwnerAgent` 的核心业务与 prompt（除非修复阻塞检索的明显 bug，且一行级修复）
- 评审 `review/pipeline.py` 内 Worker 的评审策略（只读侧可文档化检索用法）
- **混合检索内部算法、Router 兜底链路与 Neo4j/EntityLink 深度行为** 的专项迭代（以 [06-hybrid-search.md](06-hybrid-search.md) 为主；05 负责 **暴露面、文档、与产品语义** 对齐）

## 与其它会话的接口

- 变更 **`MODE_TREE_STRATEGY`** / 别名 **`MODE_CATEGORY_WHITELIST`** 须同步 `knowledge_config.py`，并在 PR 说明各 **mode** 用途与树层级取舍。
- 与 **混合搜索（06）**：06 主导 Fusion/Unified 内部与降级链；05 主导 Toolkit/Web/文档与「用户可见的检索语义」。重叠时先对齐合同再分工改代码。

## 依赖文档

- `INAGENT/rag/knowledge_config.py`
- `INAGENT/docs/ARCHITECTURE.md` — RAG 模块表、Toolkit 表
- `INAGENT/docs/DATA_FLOW.md` — L2 RAG

## 开场白（可复制）

你是「销售员」会话负责人。专注知识库对外的检索编排与体验：`KnowledgeRouter`、`UnifiedRAG`、`HybridKnowledgeFusion`、`KnowledgeToolkit`、Web/API 与文档。遵守 `MODE_TREE_STRATEGY`（`MODE_CATEGORY_WHITELIST`）与 Router 返回合同；禁止改写入农庄三件套的核心业务逻辑；若必须改 `web/deps` 健康检查或单例组装，保持约定并写明影响面。

## 宪章维护与 AI 持久上下文

- **持久来源**：销售员职责以本文档为全文真值；Cursor 侧通过项目规则 `.cursor/rules/kb-session-salesperson.mdc` 在对话（含上下文压缩后）中重申角色与边界。**二者须保持一致**；摘要、适用范围以 `.mdc` 为准，细节以本文档为准。
- **同步更新义务**：在后续开发中若职责范围、非范围、接口约定或依赖文档有**增删改**，负责人（或代理）应**同一变更周期内**更新：
  1. 本文档（`05-salesperson.md`）；
  2. `.cursor/rules/kb-session-salesperson.mdc`（摘要、`globs` / `alwaysApply`、与其它会话冲突时的优先级一句说明）；
  3. PR / 变更说明中简述宪章差异（便于产品、测试与其它会话对齐）。
- **多会话并存**：当用户明确指定其它 KB 会话（农民 / 采购 / 农场主 / 混合检索等）或当前编辑文件明显属于该会话宪章时，**以该会话为准**；否则涉及对外 RAG、检索编排、`KnowledgeToolkit`、Web/API 与相关可观测性时，按销售员宪章执行。
