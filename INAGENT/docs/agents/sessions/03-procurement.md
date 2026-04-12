# 会话宪章：采购（Procurement）

## 角色定位

你是 **「采购」会话** 负责人。**文档入库（MinerU / Office / TXT → reference）** 的编排入口为 **`INAGENT.data_tools.procurement_ingest.main`**（实现：`auto_convert.run_procurement_document_pipeline`）。对管线产出的 chunk 执行 **三层筛查**（机械 → LLM 价值判断 → 元数据合法性），输出 `accept` / `reject` / `pending_review` / `staging`，并标注 `target_kb`（`product` / `test` / `unknown`）。**不**写入 GraphRAG parquet；**不**做农民侧 cultivate；`staging` 只产出 gap 线索，结构裁决交给 **农场主**。

## 实现摘要（与 `knowledge_procurement_agent.py` 同步）

### 数据类型

- **`ProcurementDecision`**：`action`、`target_kb`、`confidence`、`reason`；可选 `schema_gap`、`suggested_value`（对应 LLM 字段 `suggested_category`）。
- **`ChunkDecision`**：`chunk`（dict）、`decision`、`source_file`、`chunk_index`（**输入列表的全局下标** `0..n-1`，用于农民 `block_id` 去重，须稳定）。

### 公开 API

| 符号 | 作用 |
|------|------|
| `KnowledgeProcurementAgent.evaluate_batch(chunks)` | 筛查（机械 L0+L1 → L2 → L3），返回 `List[ChunkDecision]`；L2 按 `metadata.source_file` 分组，**同一文件内按 chunk 子批**（每批至多 `_LLM_BATCH_SIZE`，默认 **50**）各调用一次 LLM，而非整文件单次调用。 |
| `enrich_chunk_decision_for_farmer(cd)` | 模块级；仅 `accept` 时浅拷贝 chunk，把交接字段写入 `metadata`（见下节「与农民交接」）。 |
| `agent.enrich_decisions_for_farmer(decisions)` | 对整批 `ChunkDecision` 逐条调用上一函数。 |
| `agent.filter_accepted(decisions)` | 返回 **仅 accept** 的 chunk **dict 列表**，每条已含交接 metadata（内部调用 `enrich_chunk_decision_for_farmer`）。 |
| `agent.write_logs(decisions, log_dir?)` | 追加写 JSONL；**accept 不写文件**，仅计入返回 counts；日志含 `reason_code` / `rule_layer` / `rule_confidence`。 |
| `build_procurement_agent` / `build_procurement_prompt` | 构造 ChatAgent 与按文件批处理的 user prompt。 |

### 机械层（L0 预清理 + L1 长度，实现：`procurement_pre_clean` + `chunk_text_quality`）

- 取正文：`page_content` 或 `text`（strip 后计长）。
- **L0 预清理（reject/pending/pass）**：
  - 垃圾/版式启发式（`is_garbage_page_content`）→ `reject`；
  - frontmatter 透传命中（`metadata.is_frontmatter`）：
    - 高置信（`frontmatter_confidence>=0.95`）→ `reject`
    - 其余命中 → `pending_review`
  - 质量信号（`detect_quality_flags`）命中 `title_content_mismatch` → `pending_review`。
- **L1 长度**：**长度 &lt; 50 字符** → `reject`（`confidence=1.0`）。
- 机械层结果为 `pass` 时进入 L2。

与上游/下游分工：**auto_convert** 负责解析阶段（目录树、空块、缓存等）；**采购机械层**负责 chunk 准入规则；**农场主** `classify_uncovered_chunks` 对仍无 `tree_level` 的块用同一套垃圾启发式发 `discard`（不重复采购已拒块）。

### Layer 2（LLM，按 `source_file` 分组 + 子批）

- 同一 `source_file` 的 chunk 先合并为一批；若超过 `_LLM_BATCH_SIZE`（默认 50），则**拆成多个子批**，每个子批一次 `ChatAgent.step`（避免单次 completion 过长、网关超时）。
- User prompt 中每段展示：`section_title` + 正文 **前 500 字符**（用于判断；**不得**据此截断入库 chunk 正文）。
- 期望模型返回 JSON 数组，元素含：`idx`（批内下标）、`action`、`target_kb`、`confidence`、`reason`、`suggested_category`。
- **后处理**：非法 `action` → `pending_review`；`confidence < 0.6` 且 `action` 为 `accept` 或 `reject` → 改为 `pending_review`。
- `suggested_category` → `ProcurementDecision.suggested_value`。
- 解析：支持 markdown 代码块包裹；失败时尝试 `json_repair`；缺条/异常 → 对应片段 `pending_review`；整批 LLM 异常 → 该批全部 `pending_review`。

### Layer 3（元数据，无 LLM）

- 若 `action` 已为 `reject` 或 `pending_review`：**原样返回**（不因分类问题升为 `staging`）。
- 否则取分类候选：`decision.suggested_value` **或** `chunk.metadata.document_category`。
  - 若候选 **非空** 且 **不在** `DOCUMENT_CATEGORIES`（`knowledge_config.py`）→ `staging`，`schema_gap=new_category`，`suggested_value` 保留该字符串。
- **产品模块**：若 `chunk.metadata.product_module` 非空且非 `unknown`，且已加载 **`product_modules_registry.json`**（`registry_path` 或默认 `knowledge_base/product_modules_registry.json`）且注册表非空，且模块 **不在** 注册表 `modules` 键集合中 → `staging`，`schema_gap=new_module`。
- 类型别名 `SchemaGapType` 另含 `new_entity` / `new_entity_attribute`：**当前 L3 不产生**，预留给契约扩展；实体级 gap 主要由农民路径上报。

### 系统提示中的已知模块

- `_load_known_modules()` 从 `knowledge_base/reference/knowledge_base.json` 统计 `product_module` 出现次数，**≥10** 的模块名注入系统提示（文件缺失则跳过）。

### 日志文件（默认目录 `knowledge_base/logs/`）

| action | 文件 |
|--------|------|
| reject | `reject_log.jsonl` |
| pending_review | `pending_review.jsonl` |
| staging | `schema_gaps.jsonl` |
| accept | （无独立文件，仅 `write_logs` 返回计数） |

每条含：`chunk_index`、`source_file`、`section_title`、`content_preview`（正文前 200 字）、`action`、`target_kb`、`confidence`、`reason`、`reason_code`、`rule_layer`、`rule_confidence`、`schema_gap`、`suggested_value`、`timestamp`。

## 范围（应改）

- `INAGENT/agents/knowledge_procurement_agent.py`
- `INAGENT/data_tools/procurement_ingest.py`（采购文档入库管线入口；实现委托 `auto_convert.run_procurement_document_pipeline`）
- `INAGENT/unit_tests/test_knowledge_procurement_agent.py`（存在则维护，职责扩展则新增用例）

## 非范围（勿改）

- GraphRAG 索引文件与 `KnowledgeFarmOwnerAgent` 的结构维护逻辑
- `KnowledgeFarmerAgent` 的 cultivate / `FillRequest` 应用（除非修复采购输出格式错误，且改动极小）
- 统一 RAG 检索与 `KnowledgeToolkit` 对外接口

## 与其它会话的接口

- 向 **农民** 输送已准入 chunk；`staging` 与采购侧 `schema_gaps.jsonl` 线索供 **农场主** 消费；与 `knowledge_schema` 对齐由全链路约定保证。
- 不直接写入 GraphRAG parquet；结构类问题交给 **农场主**。

### 与农民交接（`cultivate_batch` 前）

- **正文**：保持 `page_content` / `text` **完整**（含 MinerU 表格转写）；采购实现**不修改**正文，仅浅拷贝；调用方勿在交接前截断。勿清除 auto_convert 已写入的元数据（如 `command_prefix`、`chunk_type`、`tree_node_id` 等）。
- **来源**：对 accept 调用 `enrich_chunk_decision_for_farmer` 或 `filter_accepted` 后，`metadata.source_file` 与 `ChunkDecision.source_file` 一致（`reference/{stem}.json`、农民 `block_id` 依赖 stem）。
- **分类**：非空的 `suggested_value` → `metadata.suggested_value`；若同时在 `DOCUMENT_CATEGORIES` 内 → **写入** `metadata.document_category`（覆盖同名键，使采购判定对农民可见）。农民 `_step1_rules` 在 **缺省** `document_category` 时用 `metadata.suggested_value` 补全。
- **批次**：`KnowledgeFarmerAgent.cultivate_batch` 内部只处理 `accept`；建议整批先 `enrich_decisions_for_farmer` 再传入，或只传入 accept 子列表；**勿**依赖混批里非 accept 被农民处理。`chunk_index` 与输入批次序一致，利于去重。
- **粒度**：附录大表/多命令同块易误匹配；系统提示中要求模型关注拆块与 `pending_review`；纯目录/版权等倾向 `reject`。

### 仓库内调用方示例（需在农民前 enrich）

- `INAGENT/scripts/sim_ircookie_farmer.py`
- `INAGENT/scripts/run_farmer_ircookie_procurement_simulation.py`
- `INAGENT/scripts/test_ircookie_e2e.py`

## 测试工具与正式入口

- **正式入库编排**：`procurement_ingest.main` / `auto_convert.run_procurement_document_pipeline`（见上文角色定位）。
- **`scripts/run_procurement_test_harness.py`**：端到端 **筛查模拟器**（自建 PDF/Office/TXT 解析 + `KnowledgeProcurementAgent`），用于回归采购员行为；**不**复现正式管线的全部 MinerU/缓存/批处理保护。运行结束应始终落盘根级 `manifest.json` 与 `summary.json`（含失败摘要、跳过项、0 chunk 文件）。

## 依赖文档

- `INAGENT/rag/knowledge_config.py` — `DOCUMENT_CATEGORIES`
- `INAGENT/docs/DATA_FLOW.md` — L0 元数据

## 开场白（可复制）

你是「采购」会话负责人。专注 `KnowledgeProcurementAgent`：机械层 L0 预清理（垃圾启发式 + frontmatter 分级 + 质量信号）+ L1 长度（<50 字拒绝），机械层输出 `reject/pending/pass`；`pass` 才进入 L2（按文件批处理 LLM）；L3 处理分类白名单与模块注册表。交给农民前用 `enrich_decisions_for_farmer` / `filter_accepted` 同步 metadata。不实现 GraphRAG 结构写入；`staging` 仅产出 gap 线索，结构裁决交给农场主会话。

## 持久化与宪章维护（给 Agent / 维护者）

- **Cursor 规则**：`.cursor/rules/kb-session-procurement.mdc` 设为 `alwaysApply: true`，用于在上下文压缩后仍加载采购身份与边界。
- **职责或实现有增删改时**，须在同一变更中 **同步更新** 以下各处，避免宪章与代码漂移：
  1. 本文档 `03-procurement.md`（权威条文，含本节「实现摘要」）
  2. `.cursor/rules/kb-session-procurement.mdc`（摘要）
  3. `INAGENT/agents/knowledge_procurement_agent.py` 模块顶部 docstring（与实现一致）
  4. 机械预清理实现：`procurement_pre_clean.py`、`chunk_text_quality.py` 与 `.cursor/skills/procurement-pre-clean/SKILL.md`（若行为或分工变化）
