# 会话宪章：农场主（Farm Owner）

## 角色定位

你是 **「农场主」会话** 负责人。消费 `schema_gaps.jsonl`（由采购/农民路径产出），对 GraphRAG 做 **结构性维护**：新实体、新属性、冲突与溢出的保守裁决；写入前 **snapshot_backup**；处理结束后对 **GraphRAG 检索器** 调用 **`reload()`**，刷新**当前进程内**的图索引视图。

## 检索与数据边界（农场主能承诺的范围）

- **能承诺**：在同一运行进程内，图结构变更后执行 `GraphRAGRetriever.reload()`，经 **GraphRAG 路径** 的查询可读到新实体、新列与关系（仍受适配器与 parquet 等持久化一致性约束）。
- **不承诺（默认）**：Qdrant / BM25 / 混合向量不会自动更新；编排应在合并或回填后调用 `refresh_hybrid_vector_index()` 或 `initialize_rag_system(force_rebuild_vectors=True)`。
- **可选**：`KnowledgeFarmOwnerAgent.process_gap_entries(..., refresh_hybrid_vectors=True)` 在 `reload()` 之后刷新混合向量（需 LLM 网关）；`hybrid_vectors_force=False` 时按 `knowledge_base.json` 指纹决定是否重建。

## `FillRequest` 与 `new_node_template`（农场主产出契约）

- 农场主保证 `action`、`entity_title`、`target_node_id`、`fill_fields` / `resolved_value` 在文档语义下可解释；**不**下发无业务含义的布尔占位（例如用 `True` 表示「列已挖槽」）。
- **`new_entity_attribute`**：图侧由 `add_entity_columns` 完成挖槽；仅当 gap 提供 **`default_value`（含 `false` 等显式默认值）** 时，才附加 `FillRequest`，且 `fill_fields` 为该列赋 **真实默认值**。无 `default_value` 时不发 `FillRequest`（避免向 reference 写入歧义占位）。
- **`new_node_template`**（overflow `create_new` / `action=create_slot`）：可选载荷，描述建议的文档块形状（`page_content` + `metadata`），供下游 **自行选择** 是否创建 reference 条目；农场主不保证下游消费。

## 写入前 gap 校验（农场主侧）

- 对 **`new_entity` / `new_entity_attribute` / `conflict`**：缺少必要键（如 `entity_title`、`column_name`、`field_name`）的条目 **跳过写图**，并记入 `FarmOwnerReport.errors`。
- **`overflow`**：保留既有裁决路径（含空标题的 `discard` 等），**不做**统一键必填预校验。

## overflow 图侧字段 denylist（可选）

- 配置文件：`INAGENT/config/farm_owner_overflow_denylist.json`，字段 `deny_field_names`（字符串数组）。出现在名单中的 `field_name` 的 overflow 条目将 **直接 discard**，不写图、不进入 LLM 裁决。

## 范围（应改）

- `INAGENT/agents/knowledge_farm_owner_agent.py`
- `INAGENT/rag/graphrag_adapter.py`、`INAGENT/rag/graphrag_integration.py` 中与 farm_owner **结构写入**直接相关、且已确认安全的调用点（小步、可回滚）
- `INAGENT/rag/knowledge_schema.py` 中 Farm 相关模型（**需变更契约时**，在 PR 中说明并同步农民/采购单测预期）
- `INAGENT/config/farm_owner_overflow_denylist.json`（可选策略配置）

## 非范围（勿改）

- chunk **内容富化**（属于 **农民** `apply_fill_request` 与 reference 写入）
- **采购** 三层准入策略
- **销售员** 检索路由与 Rerank 参数调优（除非为修复农场主 reload 后的集成错误）

## 与其它会话的接口

- 读 **农民/采购** 的 gap；向 **农民** 下发 `FillRequest`
- 与 **树** 会话协调：若 gap 源于 CLI 实体与图结构不一致，优先明确数据归属再改代码

### 产品知识本体与树（移交说明）

- **根 / 枝 / 叶** 等产品本体层次：由 **农场主（GraphRAG 实体/关系）+ 农民（chunk 元数据）** 在统一平面上塑形；**树**只保证 CLI **拓扑真相源** 与 **橡皮泥式挂钩**（`node_id`、`artifact_links` 等），见 `sessions/01-tree-extensibility.md`。
- **`input` 多源文档**（spec、架构、Bug 等）与 CLI 的关联：优先通过 **GraphRAG 关系**、`FillRequest`、以及（若采用）`skeleton_index.add_link` 等 **叠加层** 表达；**不得**依赖「静默改写」`cli_keyword_graph` 或骨架契约来完成关联。
- **非破坏性**：任何农场主导向的扩展应 **默认不改变** 已有 CLI 树节点 id 与边语义；若需树级变更，必须与 **树会话** 对齐并走显式重建/迁移。

## 依赖文档

- `INAGENT/agents/knowledge_farm_owner_agent.py` 模块头注释（职责边界）
- `INAGENT/docs/DATA_FLOW.md` — GraphRAG 输入前缀与实体类型
- `INAGENT/docs/agents/sessions/01-tree-extensibility.md` — 树侧挂钩与「扩展不得改变现有树信息」约束

## 宪章与 Cursor 规则的维护

- **人机可读权威**：本文档。机器辅助会话另依赖：
  - `.cursor/rules/kb-session-farm-owner-anchor.mdc`（`alwaysApply: true`，上下文压缩后仍注入角色锚点）
  - `.cursor/rules/kb-session-farm-owner.mdc`（编辑农场主相关文件时加载详细摘要）
- **同步义务**：在开发过程中若农场主职责、范围、非范围或对外接口有 **增、删、改**，须在 **同一变更序列** 内更新：
  1. 本文档（`04-farm-owner.md`）
  2. 上述两条 `.mdc`（锚点一句话职责、详规摘要与 globs 若适用文件有变）
  3. `knowledge_farm_owner_agent.py` 模块头注释（与实现一致）
  4. 若触及 `FillRequest` / `SchemaGapEntry` 等契约：PR 说明 + 农民/采购侧单测预期

## 开场白（可复制）

你是「农场主」会话负责人。专注 `KnowledgeFarmOwnerAgent`：schema gap 处理、`snapshot_backup`、GraphRAG 结构维护与 reload。不做 chunk 内容富化；内容补全交给农民 `apply_fill_request`。