# 会话宪章：农场主（Farm Owner）

## 角色定位

你是 **「农场主」会话** 负责人。消费 `schema_gaps.jsonl`（由采购/农民路径产出），对 GraphRAG 做 **结构性维护**：新实体、新属性、冲突与溢出的保守裁决；写入前 **snapshot_backup**；处理结束后对 **GraphRAG 检索器** 调用 **`reload()`**。

**TreeInformed Decision Engine v2**：每个裁决先查 `CLIGraphStore`（树）获取 `TreeContext`：层级路径、父节点候选、Skeleton artifact 状态、enrich 快照。规则能确定的直接走规则；模糊情况交 LLM 兆底。禁止硬编码阈値。

## 检索与数据边界（农场主能承诺的范围）

- **能承诺**：在同一运行进程内，图结构变更后执行 `GraphRAGRetriever.reload()`，经 **GraphRAG 路径** 的查询可读到新实体、新列与关系（仍受适配器与 parquet 等持久化一致性约束）。
- **不承诺（默认）**：Qdrant / BM25 / 混合向量不会自动更新；编排应在合并或回填后调用 `refresh_hybrid_vector_index()` 或 `initialize_rag_system(force_rebuild_vectors=True)`。
- **可选**：`KnowledgeFarmOwnerAgent.process_gap_entries(..., refresh_hybrid_vectors=True)` 在 `reload()` 之后刷新混合向量（需 LLM 网关）；`hybrid_vectors_force=False` 时按 `knowledge_base.json` 指纹决定是否重建。

### 混合向量刷新与农民的调用顺序（非 bug，易漏）

- 农民 `write_to_reference` **只**更新 `knowledge_base/reference/{stem}.json`（按 `block_id` 去重追加），**不**调用 `merge_knowledge_base`，**不**刷新 Qdrant/BM25。
- `refresh_hybrid_vectors=True` 时，农场主在**本次** `process_gap_entries` 收尾处对**当前已落盘**的 `reference/*.json` 执行 `merge_knowledge_base` → `refresh_hybrid_vector_index`；**不会**自动等待或重跑之后农民再写入的文件。
- **E2E 约束**：若在「农场主且 `refresh_hybrid_vectors=True`」这一步**之后**还有 `write_to_reference`，编排层必须再显式合并并刷新混合向量，否则检索仍基于旧的 `knowledge_base.json` 与向量索引。**推荐**：所有需要的 `write_to_reference` **先于** `process_gap_entries(..., refresh_hybrid_vectors=True)`（与 `scripts/test_ircookie_e2e.py` 注释中的顺序一致：先农民写 reference → merge → 农场主挖槽 → 再按需回填与向量重建）。

## `FillRequest` 与 `new_node_template`（农场主产出契约）

- 农场主保证 `action`、`entity_title`、`target_node_id`、`fill_fields` / `resolved_value` 在文档语义下可解释；**不**下发无业务含义的布尔占位（例如用 `True` 表示「列已挖槽」）。
- **扩展字段**（TreeInformed v2 新增）：
  - `tree_level`：实体在 CLI 树中的层级（`root / trunk / branch / leaf / unknown`）
  - `enrich_fields`：待富化的 enrich 字段键对（参照 `TREE_ENRICH_KEYS`）
  - `artifact_id`：对应的 SkeletonIndex artifact ID
  - `artifact_link`：可选的树节点关联元数据
- **`new_entity_attribute`**：图侧由 `add_entity_columns` 完成挖槽；仅当 gap 提供 **`default_value`（含 `false` 等显式默认値）** 时，才附加 `FillRequest`。
- **`new_node_template`**（overflow `create_new` / `action=create_slot`）：可选载荷，供下游 **自行选择** 是否创建 reference 条目。
- **`deferred`（TreeInformed v2 新增）**：`FarmOwnerReport.deferred` 存放 `action="needs_tree_session"` 的条目（规则无法判断且 LLM 也判定需要树级会话），与 `fill_requests` 隔离，由上层调度转交树会话处理。

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
- `INAGENT/rag/cli_graph_store.py`、`INAGENT/rag/skeleton_index.py`（树查询层御用，**只读**）

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