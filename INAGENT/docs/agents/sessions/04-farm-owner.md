# 会话宪章：农场主（Farm Owner）

## 角色定位

你是 **「农场主」会话** 负责人。消费 `schema_gaps.jsonl`（由采购/农民路径产出），对 GraphRAG 做 **结构性维护**，并在 **树知情（TreeInformed）** 前提下与 CLI 图、SkeletonIndex 只读协作；写入前 **snapshot_backup**；处理结束后 **`GraphRAGRetriever.reload()`**。

**TreeInformed 决策（与实现对齐）**：

- **`_query_tree_context(entity_title)`**：懒加载 **`CLIGraphStore`**（`get_cli_graph_store()`）与 **`SkeletonIndex`**（`get_skeleton_index()`），填充只读 **`TreeContext`**（`knowledge_schema.TreeContext`）：`exists_in_tree`、`tree_level`、`hierarchy_prefix`、`parent_candidate_id`、`skeleton_module_id`、`skeleton_artifact_exists`、`enrich_snapshot` 等。树侧命中优先走 **`cli.command_exists(entity_title)`**，签名为 **`Tuple[bool, List[str]]`**（存在标志 + 相似命令 id 列表；单测 mock 须与此一致）。同一 **`process_gap_entries`** 批次内结果写入 **`_tree_context_cache`**。
- **`new_entity`**：规则优先（`_decide_new_entity_action_rules_only`）；未定条目再 **批量 LLM**（`_batch_run_decisions`，每批最多 `BATCH_SIZE=20`），合法 `action` 含 `tree_create_leaf|branch|trunk|root`、`merge_into_existing`、`needs_tree_session`、`discard`。`needs_tree_session` → **`FarmOwnerReport.deferred`**（`FillRequest.action="needs_tree_session"`），不写图。
- **`conflict` / `overflow` / `ambiguous_match`**：构造上下文后走同一套 **批量裁决**；`overflow` 在 **`TREE_ENRICH_KEYS`** 命中当前树层级时 **规则直连 merge_into**（不经 LLM）；denylist 命中则 **discard**。
- **相似度**：**overflow** 的 **纯规则兜底**（`_fallback_overflow_decision`）在无充分树上下文时仍可用 `nearest_matches[0].similarity >= 0.9` 作为 merge 提示。
- **决策缓存**：`_decision_cache` 按 `decision_type:entity:field` 去重，避免同实体同类型重复打 LLM。
- **`classify_uncovered_chunks`**：对缺 `tree_position` 的块生成 **`FillRequest`**（含 `chunk_meta_patch`、`target_block_id`）；垃圾块规则排除后 `action=discard`（`owner_excluded`）。
- **`cultivate_scenarios`**：读 **`knowledge_base/reference/knowledge_base.json`**，按 `_tree_feature_id` 与 `function_hierarchy` 识别 **branch/trunk/root** 缺口，写 **`reference/scenarios_scaffold.json`**（`_scaffold` 骨架）；可选 **`_model`** 生成 HyDE 字段（`_hyde_query` 等）。**不**写 `knowledge_base.json` 本体；合成正文由农民进 **`scenarios_synthesized.json`**（见模块头）。入口脚本：`INAGENT/scripts/run_scenario_cultivation.py`。

**构造参数**：`KnowledgeFarmOwnerAgent(graphrag, model=..., _chat_agent=..., cli_graph=..., skeleton_index=...)` — 后两者供单测注入 mock，默认从全局 getter 加载。

## 检索与数据边界（农场主能承诺的范围）

- **能承诺**：在同一运行进程内，图结构变更后执行 `GraphRAGRetriever.reload()`，经 **GraphRAG 路径** 的查询可读到新实体、新列与关系（仍受适配器与 parquet 等持久化一致性约束）。
- **不承诺（默认）**：Qdrant / BM25 / 混合向量不会自动更新；编排应在合并或回填后调用 `refresh_hybrid_vector_index()` 或 `initialize_rag_system(force_rebuild_vectors=True)`。
- **可选**：`process_gap_entries(..., refresh_hybrid_vectors=True)` 时，顺序为：`reload()` → **`merge_knowledge_base(reference_dir → knowledge_base.json)`** → **`refresh_hybrid_vector_index(force=hybrid_vectors_force)`**（需 LLM 网关）。`hybrid_vectors_force=False` 时按指纹决定是否清空 Qdrant（见下表）。

### 给混合搜索（06）的正式指示：向量通道无「按条 diff」增量 upsert

农场主链路里若触发混合向量刷新，底层是 `refresh_hybrid_vector_index` → `initialize_rag_system(..., force_rebuild_vectors=...)`。**当前实现不提供**「只更新变更的那几条 chunk / 几个 Qdrant 点」的正式 API；混合搜索侧请按下面两种模式对接预期，**不要**向农场主要求未实现的增量向量语义。

| 参数 / 行为 | Qdrant | BM25（进程内） |
|-------------|--------|----------------|
| `hybrid_vectors_force=True`（`refresh_hybrid_vector_index` 默认、`process_gap_entries` 默认） | **清空 collection**，再按 `load_knowledge_base(reference_dir)` 得到的文档块 **整库重嵌写回** | 与全量嵌入共用同一批文档，随 `hybrid_retriever.process` 路径重建 |
| `hybrid_vectors_force=False` | 比较 `knowledge_base.json` SHA256 与本地 `rag_meta.json`：**指纹不变 → 不向 Qdrant 写入**（认为向量集已与上次全量一致）；**指纹变了 → clear + 全量重嵌** | **仍会**用当前 KB 调 `build_bm25_only`，即 **BM25 与指纹是否变无关，每次初始化都会按当前 KB 刷新内存索引** |

**结论（请 06 按此执行）**：

1. **需要 Qdrant 与合并后的 `knowledge_base.json` 严格一致时**：编排使用 **`force=True`（或删指纹后全量）**，接受 **全量重建** 成本。
2. **希望少打 Qdrant、但能接受「仅 BM25 反映最新 KB、Qdrant 仍为旧全量」的风险时**：可用 **`force=False`**，并理解 **指纹不变时 Qdrant 不会追平** `reference/*.json` 单独改动（除非先合并进 `knowledge_base.json` 使指纹变）。
3. **若产品必须「按变更增量 upsert Qdrant」**：由 **混合搜索（06）** 在 `workflow_config_generator` / `HybridRetriever` / `QdrantStorage`（及与 `block_id` 等 chunk 身份对齐）**另行设计与实现**；**不属于**农场主交付范围。落地后需在 PR 中同步本段与 `06-hybrid-search.md`、`DATA_FLOW.md`（§3.8）。

### 混合向量刷新与农民的调用顺序（非 bug，易漏）

- 农民 `write_to_reference` **只**更新 `knowledge_base/reference/{stem}.json`（按 `block_id` 去重追加），**不**调用 `merge_knowledge_base`，**不**刷新 Qdrant/BM25。
- `refresh_hybrid_vectors=True` 时，农场主在**本次** `process_gap_entries` 收尾处对**当前已落盘**的 `reference/*.json` 执行 `merge_knowledge_base` → `refresh_hybrid_vector_index`；**不会**自动等待或重跑之后农民再写入的文件。
- **E2E 约束**：若在「农场主且 `refresh_hybrid_vectors=True`」这一步**之后**还有 `write_to_reference`，编排层必须再显式合并并刷新混合向量，否则检索仍基于旧的 `knowledge_base.json` 与向量索引。**推荐**：所有需要的 `write_to_reference` **先于** `process_gap_entries(..., refresh_hybrid_vectors=True)`（与 `scripts/test_ircookie_e2e.py` 注释中的顺序一致：先农民写 reference → merge → 农场主挖槽 → 再按需回填与向量重建）。

## `schema_gaps.jsonl` 支持的 `gap_type`（与 `SchemaGapKind` 一致）

`new_entity` · `new_entity_attribute` · `conflict` · `overflow` · **`ambiguous_match`**（`ambiguous_candidates` 列表；单候选规则自动落 `tree_node_id`，多候选批量 LLM `pick`）。

## `FillRequest` / `FarmOwnerReport`（与 `knowledge_schema` 一致）

- **`FillRequest.action`** 合法集见 `OwnerAction`（含 `tree_create_*`、`merge_into_existing`、`needs_tree_session`、`discard`、`update`、`create_slot` 等）。
- **常用扩展字段**：`tree_level`、`enrich_fields`、`chunk_meta_patch`（`document_category` / `tree_position` / `owner_excluded` 等补丁）、`target_block_id`（按块回填）、`new_node_template`（overflow 新建图实体时的可选文档形状）、`artifact_id` / `artifact_link`（契约保留；当前主路径以 **图写 + `SkeletonIndex.register_artifact`** 为主）。
- **`new_entity_attribute`**：`add_entity_columns` 挖槽；仅当 **`default_value is not None`** 时附加 `FillRequest`（真实默认值，无布尔占位）。
- **`FarmOwnerReport`**：`fill_requests`、`deferred`（仅 `needs_tree_session`）、`operation_log` + **`operation_summary()`** / **`dump_operation_log()`**、`tree_mutations`（`TreeMutation`）、`discarded_count` / `discarded_entities`、`errors`。

### 农民执行 `FillRequest`（编排契约，非农场主代码内调用）

- 农场主只产出 `FillRequest`；**落盘**由编排调用 **`KnowledgeFarmerAgent.apply_fill_request`**。
- 农民侧：**reference 为主**——按 `target_block_id` 或 `target_node_id` / `entity_title` 匹配 chunk 的 `metadata`；合并 `fill_fields`、`enrich_fields`、`chunk_meta_patch`；`action=needs_tree_session` **跳过**；`discard` 可在带 `chunk_meta_patch` 等补丁时仍写 reference；`create_slot` 可追加新 reference 条目。
- 若默认 `knowledge_base/reference/knowledge_base.json` 存在，同步合并到骨架中 **`metadata.node_id` 命中**的条目。
- **不**替代 `merge_knowledge_base` / 混合向量刷新（见上文 § 混合向量刷新与农民的调用顺序）。

## 写入前 gap 校验（农场主侧）

- 对 **`new_entity` / `new_entity_attribute` / `conflict`**：缺少必要键（如 `entity_title`、`column_name`、`field_name`）的条目 **跳过写图**，并记入 `FarmOwnerReport.errors`。
- **`overflow`**：保留既有裁决路径（含空标题的 `discard` 等），**不做**统一键必填预校验。

## overflow 图侧字段 denylist（可选）

- 配置文件：`INAGENT/config/farm_owner_overflow_denylist.json`，字段 `deny_field_names`（字符串数组）。出现在名单中的 `field_name` 的 overflow 条目将 **直接 discard**，不写图、不进入 LLM 裁决。

## 范围（应改）

- `INAGENT/agents/knowledge_farm_owner_agent.py`
- `INAGENT/rag/graphrag_adapter.py`、`INAGENT/rag/graphrag_integration.py` 中与 farm_owner **结构写入**直接相关、且已确认安全的调用点（小步、可回滚）
- `INAGENT/rag/knowledge_schema.py` 中 Farm / Tree / `FillRequest` / `SchemaGapEntry` 等契约（变更时 PR 说明 + 同步农民/采购/树相关单测）
- `INAGENT/config/farm_owner_overflow_denylist.json`（可选）
- `INAGENT/scripts/run_scenario_cultivation.py`（枝干场景骨架入口，与 `cultivate_scenarios` 对齐）
- 农场主在 **`refresh_hybrid_vectors=True`** 时 **会调用** `INAGENT/data_tools/merge_knowledge_base.py` 与 `workflow_config_generator.refresh_hybrid_vector_index`；改动合并或向量语义时需协调 **混合搜索（06）** 宪章
- `INAGENT/rag/cli_graph_store.py`、`INAGENT/rag/skeleton_index.py`：农场主 **只读**；若改查询契约需同步本文档与农民/树会话

## 非范围（勿改）

- chunk **内容富化**（属于 **农民** `apply_fill_request` 与 reference 写入）
- **采购** 三层准入策略
- **销售员** 检索路由与 Rerank 参数调优（除非为修复农场主 reload 后的集成错误）

## 与其它会话的接口

- 读 **农民/采购** 的 gap；向 **农民** 下发 `FillRequest`
- 与 **树** 会话协调：若 gap 源于 CLI 实体与图结构不一致，优先明确数据归属再改代码
- **质检员**：可度量 `schema_gaps` / `FillRequest` **闭环**与 `FarmOwnerReport` 字段（见 [`07-quality-inspector.md`](07-quality-inspector.md)）；**GraphRAG 结构裁决与写入**仍属 **农场主**，质检不替代 `process_gap_entries` 等实现。

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