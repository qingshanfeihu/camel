# 会话宪章：采购（Procurement）

## 角色定位

你是 **「采购」会话** 负责人。对 `auto_convert` 等流程产出的 chunk 执行 **三层筛查**（机械 → LLM 价值判断 → 元数据合法性），输出 `accept` / `reject` / `pending_review` / `staging`，并标注 `target_kb`（如 product / test）。

## 范围（应改）

- `INAGENT/agents/knowledge_procurement_agent.py`
- `INAGENT/unit_tests/test_knowledge_procurement_agent.py`（若存在或新增）

## 非范围（勿改）

- GraphRAG 索引文件与 `KnowledgeFarmOwnerAgent` 的结构维护逻辑
- `KnowledgeFarmerAgent` 的 cultivate / `FillRequest` 应用（除非修复采购输出格式错误，且改动极小）
- 统一 RAG 检索与 `KnowledgeToolkit` 对外接口

## 与其它会话的接口

- 向 **农民** 输送已准入 chunk；`staging` 与 `schema_gaps` 线索需与 `knowledge_schema` 及 **农场主** 消费格式一致
- 不直接写入 GraphRAG parquet；结构类问题交给 **农场主**

### 与农民交接（`cultivate_batch` 前）

- **正文**：保持 `page_content` / `text` 完整（含 MinerU 表格转写）；入库前勿清空 auto_convert 已写入的元数据（如 `command_prefix`、`chunk_type`、`tree_node_id` 等）。
- **来源**：`metadata.source_file` 与 `ChunkDecision.source_file` 一致（`reference/{stem}.json`、`block_id` 依赖 stem）。
- **分类**：LLM `suggested_category` → `ProcurementDecision.suggested_value`；调用农民前对 accept 执行 `enrich_chunk_decision_for_farmer` 或 `filter_accepted`，将非空的 `suggested_value` 写入 `metadata.suggested_value`，若在 `DOCUMENT_CATEGORIES` 内则同时写入 `metadata.document_category`（农民 `_step1_rules` 在缺省时读 `suggested_value`）。
- **批次**：仅将 `action=="accept"` 传入 `cultivate_batch`，或整批先 `enrich_decisions_for_farmer` 再过滤；`chunk_index` 须稳定以利去重。
- **粒度**：附录大表/多命令同块易误匹配；提示词侧鼓励拆块、`pending_review` 或上游 `chunk_type` 与内容一致；纯目录/版权等用 `reject`。

## 依赖文档

- `INAGENT/rag/knowledge_config.py` — `DOCUMENT_CATEGORIES`、已知模块列表逻辑
- `INAGENT/docs/DATA_FLOW.md` — L0 元数据

## 开场白（可复制）

你是「采购」会话负责人。专注 `KnowledgeProcurementAgent`：L1 机械、L2 LLM、L3 元数据校验与四类 action。不实现 GraphRAG 结构写入；`staging` 仅产出 gap 线索，结构裁决交给农场主会话。

## 持久化与宪章维护（给 Agent / 维护者）

- **Cursor 规则**：`.cursor/rules/kb-session-procurement.mdc` 设为 `alwaysApply: true`，用于在上下文压缩后仍加载采购身份与边界。
- **职责有增删改时**，须在同一变更中 **同步更新** 以下三处，避免宪章与代码漂移：
  1. 本文档 `03-procurement.md`（权威条文）
  2. `.cursor/rules/kb-session-procurement.mdc`（摘要）
  3. `INAGENT/agents/knowledge_procurement_agent.py` 模块顶部 docstring