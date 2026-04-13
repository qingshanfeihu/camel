# 会话宪章：树（Tree / CLI 骨架与图谱）

## 宪章维护（跨会话、职责记忆）

- **权威来源**：本文件是「树」会话职责与边界的**唯一权威**；模型上下文被压缩或换会话后，应通过重读本文（或 Cursor 规则 `kb-session-tree.mdc`）恢复角色，而不是依赖对话记忆。
- **同步义务**：在后续开发中，若「树」的职责出现 **增、删、改**（新脚本/新数据产物/农民契约字段变化等），须**同一 PR 或紧随其后的提交**内更新：
  1. 本文的「范围 / 非范围 / 交付物 / 对齐说明」中与变更相关的段落；
  2. `.cursor/rules/kb-session-tree.mdc` 中的摘要与 `globs`（使相关文件仍会挂上本规则）。
- **触发自检**：修改上列范围内路径、或用户指定由「树」会话负责时，开工前先对照本文自述职责与禁止项，避免越权改 procurement / farm_owner 等。

## 角色定位

你是 **「树」会话** 负责人。维护 **CLI 命令树、骨架索引、CLI 关键词图** 等与 `KnowledgeFarmerAgent` 树匹配、交叉引用一致的数据与脚本；保证索引格式与农民消费端契约一致。

## 职责边界：CLI 真相源与「橡皮泥」

- **树会话裁定**：**仅** CLI 结构真相源（`command_tree` XML → 图 → 骨架叶子）及与农民 **`kb_index` / `_match_tree_node` 契约** 一致的数据形态。
- **树会话不裁定**：某份产品 spec、架构文档、Bug 叙事在知识本体里算「根 / 枝 / 叶」——那是 **农民 + 农场主**（含元数据、GraphRAG、`artifact_links` 等）的塑形问题。
- **与农民 / 农场主分工**：CLI **拓扑与 `node_id` 真相源** 由树会话维护；**在 CLI 叶之上新建 GraphRAG 实体、挖槽、产品树层级** 归 **农场主**；农民 **只匹配已有叶并富化**。细则见 [`02-farmer.md`](02-farmer.md)、[`04-farm-owner.md`](04-farm-owner.md) 中的 **结构边界** 小节。
- **橡皮泥**：树提供 **可扩展载体**（图节点可选字段、`skeleton_index` 的 `artifacts` / `artifact_links`、稳定 `node_id` / `command_prefix` 等），供上层 **可选挂载**；详见 [01-tree-extensibility.md](01-tree-extensibility.md)。
- **非破坏性扩展**（强制）：任何「扩展」**不得在无显式、可回滚的重建/迁移流程下** 改变已有树的 **`node_id`、拓扑边、骨架块契约字段**；默认 **叠加、可选、向后兼容**。细则见 `01-tree-extensibility.md` §0。

## 范围（应改）

- `INAGENT/rag/skeleton_index.py`
- `INAGENT/scripts/enrich_cli_graph.py`
- `INAGENT/scripts/extract_cli_keywords_and_graph.py`
- `INAGENT/scripts/analyze_cli_graph.py`
- `INAGENT/scripts/enrich_cli_leaf_schema.py`、`rebuild_cli_docs.py`、`import_cli_graph_to_neo4j.py`（与树/图数据直接相关时）
- `INAGENT/scripts/snapshot_retrieval_baseline.py`（**检索基线**：树 + reference + GraphRAG `output/` + 本地 Qdrant 一体快照/恢复，用于污染回退）
- `INAGENT/knowledge_base/cli_keyword_graph.json`（由脚本再生成时）
- `INAGENT/agents/knowledge_farmer_agent.py` 中 **仅** 与 `_match_tree_node`、`_load_kb_index` / `_load_skeleton`、`_refine_ac_meta_command_prefix`、交叉引用检测、`farmer_tree_alias.json` 消费直接相关的逻辑（**不含** `enrich_scenario_nodes` 等场景正文 LLM；大块重构前先与农民会话对齐）

## 非范围（勿改）

- `KnowledgeProcurementAgent` 的 accept/reject/staging 决策逻辑
- `KnowledgeFarmOwnerAgent` 的 GraphRAG 结构写入与 snapshot 流程
- 评审流水线 `review/pipeline.py` 中 Worker 业务（除非修复树数据导致的阻塞且范围极小）

## 交付物

- 树 / 骨架 / `cli_keyword_graph` 与 `reference` chunk 的 **对齐说明**（PR 描述或本目录补充文档）
- 向 **农民** 会话交付：索引字段含义、匹配失败时建议上报的 gap 类型（与 `knowledge_schema.SchemaGapEntry` 一致）
- **[01-tree-extensibility.md](01-tree-extensibility.md)**：树侧 **橡皮泥接口** 盘点、已知缺口与 **「扩展不得改变现有树信息」** 硬约束

## 检索基线快照（污染回退 → 恢复高质量混合检索）

**问题**：农民改 `reference/`、合并 `knowledge_base.json`，或农场主改 GraphRAG 结构，均可能污染「树 + 索引」；仅依赖 `graphrag_integration.snapshot_backup()` **只覆盖 GraphRAG 的 parquet/LanceDB**，**不包含** `cli_keyword_graph.json`、`reference/*.json`、**本地 Qdrant 向量目录**，无法单独恢复整条混合检索链路。

**做法**：在重大批处理或实验前创建基线；出问题时一键恢复（须先停服务）。

```text
python INAGENT/scripts/snapshot_retrieval_baseline.py create --label hq_cli
python INAGENT/scripts/snapshot_retrieval_baseline.py restore INAGENT/knowledge_base/retrieval_baselines/<时间戳>_hq_cli
```

**快照目录**（默认 `knowledge_base/retrieval_baselines/<UTC时间戳>[_标签]/`）含：

| 子目录/文件 | 作用 |
|-------------|------|
| `reference/*.json` | 合并后的 chunk 与 `cli.json` 等 |
| `tree/cli_keyword_graph.json` | L1 CLI 图（可用 `--skip-cli-graph` 跳过极大文件） |
| `tree/skeleton_index.db` | 骨架索引 SQLite（若存在） |
| `graphrag_output/` | 与 `graphrag_index/output` 对齐的 parquet + `lancedb/` |
| `qdrant_local/` | **仅本地**持久化 Qdrant（`QDRANT_LOCAL_DIR` 或默认 `%LocalAppData%/INAGENT/vector_store/qdrant`） |
| `manifest.json` | 校验和、chunk 数、`qdrant` 为远端时的说明 |

**限制**：若配置 **远端 Qdrant**（`QDRANT_URL`），脚本**不会**拷贝向量数据，manifest 会标注；需在服务端做快照或恢复源数据后 **重跑向量导入**。恢复后请 **重启服务** 并对 GraphRAG 调用 **`reload()`**（若进程内已缓存）。

**与农场主**：`KnowledgeFarmOwnerAgent` 写入前的 `snapshot_backup` 仍保留，作为 **GraphRAG 增量写入前** 的保险；**检索基线**是更广的运维操作，由树会话维护脚本，农民/农场主在高风险操作前可手动执行 `create`。

### 与质检员

- **质检员**（[`07-quality-inspector.md`](07-quality-inspector.md)）可对 `kb_index`、骨架与农民侧树匹配 **消费契约** 做只读校验与契约测试；**不**修改 `cli_keyword_graph.json`、骨架拓扑或 `node_id`。
- CLI **结构真相源**与数据变更仍由 **树** 会话负责；质检不替代树会话交付。

## 依赖文档

- `INAGENT/docs/DATA_FLOW.md` — L1 CLI 关键词图谱
- `INAGENT/docs/DESIGN.md` — 知识层概览
- `INAGENT/docs/agents/sessions/01-tree-extensibility.md` — 扩展挂钩与非破坏性约束

## 开场白（可复制）

你是「树」会话负责人。只处理 CLI/骨架/关键词图与农民侧树匹配相关代码与数据。不修改 procurement / farm_owner 的核心决策逻辑。目标：保证 skeleton、kb_index、cli_keyword_graph 与 `knowledge_farmer_agent` 的匹配语义一致，并输出给其他会话的接口说明。扩展能力以 **不破坏现有树信息** 为前提（见 `01-tree-extensibility.md` §0）。

---

## 对齐说明（树 / 骨架 / 图 ↔ reference chunk）

| 产物 | 路径 / 存储 | 在农民侧的用途 |
|------|-------------|----------------|
| **骨架 + kb 索引源** | `INAGENT/knowledge_base/reference/knowledge_base.json` | `_load_skeleton()` 以 `metadata.node_id` 为键；`_load_kb_index()` 扫描同一文件，构建 `command_prefix → node_id` |
| **L1 CLI 图** | `INAGENT/knowledge_base/cli_keyword_graph.json` | 模块/命令/关键词拓扑；`skeleton_index.seed_from_cli_graph()` 等跨存储注册，不直接参与 `_match_tree_node`，但与「命令前缀、模块标签」语义应对齐（见 `DATA_FLOW.md` L1） |
| **骨架索引 DB** | `INAGENT/vector_store/skeleton_index.db` | Module/Feature/Artifact 注册表，连接 Qdrant/GraphRAG；农民当前以 JSON 骨架为主，树会话负责与图数据一致地维护该注册表 |
| **reference 分文件** | `INAGENT/knowledge_base/reference/*.json` | 农民写入 enrich 后的 chunk；**树匹配仍以 `reference/knowledge_base.json` 为准**，二者需在 `node_id` / `command_prefix` 上可核对 |

**匹配链路（农民，实现于 `knowledge_farmer_agent._match_tree_node`）**

1. `_load_kb_index()`：仅当条目中 **同时存在** `metadata.node_id` 与 `metadata.command_prefix` 时，才把 `command_prefix` 编入索引。
2. **优先级（高 → 低）**（与源码 docstring 一致）：
   - **1** `meta` / `ac_meta` 中 `tree_node_id` 或 `node_id`：若在骨架字典中存在则直接返回。
   - **2** `section_title` → `_normalize_heading_to_node_slug`：先与骨架键比，再查 **`farmer_tree_alias.json`**（slug → `node_id`）。
   - **2b** `section_title` 去掉尾部参数语法（`{…}` / `<…>` / `[…(` 起头之后）→ `kb_index` **精确键**；否则取 **最短 `command_prefix` 键** 的同族匹配（`startswith(st_base + " ")`）。
   - **3** `command_prefix`（`meta` 或 `ac_meta`）在 `kb_index` 精确命中时：正文非空则要求 **前 300 字符**（小写）含该前缀，防跨节误标；正文为空则信任 meta。
   - **4** 正文 **首行**（ASCII 字母起头）slug：与骨架/别名命中后，若与 `command_prefix` 的最长前缀节点 **不一致** → 记 `_last_ambiguous_candidates` 并返回 `None`（农民产出 **`ambiguous_match` gap**）。
   - **5** 仅当 `ac_meta["chunk_type"]` 为空或为 **`single_command`** 时，对 `_extract_cli_prefix_strings(content)` 做 **最长前缀** 命中 `kb_index`；否则跳过（防多命令块误绑一叶）。
3. **命中后**：`cultivate_batch` 会用该 `node_id` 在 `kb_index` 中 **最长** 的 `command_prefix` 回填 `meta["command_prefix"]`（若当前前缀不在该节点的键集合中）。
4. `_refine_ac_meta_command_prefix`：`cultivate_batch` 在匹配前调用；用正文对 `kb_index` **最长前缀** 将 `ac_meta["command_prefix"]` 升为骨架 label（仅当正文以该 label 为前缀且新 label 更长）。
5. **结构化阶段**：`cultivate_batch` 使用 `_structure_via_auto_convert` → **`_extract_chunk_metadata(..., skip_llm=True)`**（当前实现 **不** 在批处理路径调 LLM；缺字段走 gap / 农场主）。
6. **可选别名表** `reference/farmer_tree_alias.json`：JSON 对象 slug → `node_id`，**树会话**维护，农民只读。

**树会话的维护约束**

- 新增/重命名 CLI 叶子时：保证 `knowledge_base.json` 中 `node_id` 稳定、`command_prefix` 与产品 CLI 文本一致（含空格规则），避免重复 `command_prefix` 覆盖（索引构建为顺序覆盖，后写者胜）。
- 改 `cli_keyword_graph` 或 enrich 脚本时：核对模块 `id`/`label` 与骨架里 `product_module`、`func` 等字段不出现系统性漂移（详见 `DESIGN.md` 知识层）。

---

## 向农民会话交付：索引字段与 gap 类型

### `kb_index` / 骨架 `metadata` 关键字段（与 `_SKELETON_META_FIELDS` 一致）

| 字段 | 含义 | 树侧注意 |
|------|------|----------|
| `node_id` | 骨架节点唯一 ID，亦为 `skeleton` 字典键 | 与 GraphRAG/artifact 引用保持一致时用同一 ID |
| `command_prefix` | 参与 `_match_tree_node` 的主键（CLI 命令前缀） | 必须与文档正文可前缀匹配；多节点勿共用同一前缀 |
| `document_category` | 文档分类（如 `cli/reference`） | 影响 `infer_node_type` 等上游语义 |
| `product_module` | 产品模块 | 与骨架 **仅大小写** 不同则回填骨架写法，不报 `conflict`；语义不同仍报 `conflict` |
| `func` | 功能/特性名 | 同上参与骨架 diff |
| `scope` | 适用范围等 | 同上 |
| `is_variant` | 是否变体命令 | 同上 |
| `source_file` | 来源文件名 | 写入 gap 的 `source_file` |

### 匹配失败或结构不一致时：建议上报的 `SchemaGapEntry.gap_type`

与 `INAGENT/rag/knowledge_schema.py` 中 `SchemaGapKind` 一致：`new_entity` | `new_entity_attribute` | `conflict` | `overflow` | `ambiguous_match`。

| 场景 | 当前农民行为（`cultivate_batch` / `_diff_with_skeleton`） | 农民会话上报/排错时可标注 |
|------|----------------------------------------------------------|---------------------------|
| 命中骨架节点，某 `_SKELETON_META_FIELDS` 字段骨架有值且新值不同 | `gap_type="conflict"`，`field_name` / `skeleton_value` / `new_value` | **冲突**：需农场主或人工裁决 |
| 命中骨架节点，auto_convert 出现骨架 `metadata` 中**不存在**的字段 | `gap_type="overflow"` | **字段溢出**：是否升维 schema |
| 首行 slug 与 `command_prefix` 指向 **不同** `node_id`（`_last_ambiguous_candidates`） | `gap_type="ambiguous_match"`，`field_name="tree_node_id"`，`ambiguous_candidates` 列表 | **歧义**：农场主裁决；树侧可核对别名与索引 |
| 未命中骨架但有 `command_prefix`（且无歧义候选） | `gap_type="overflow"`，`entity_title`=前缀，`nearest_matches` 辅助 | **无树节点**：多为缺 `knowledge_base.json` 条目或前缀不一致；**树会话优先补骨架/前缀** |
| 正文中出现覆盖类表述（如「支持覆盖」等，见 `_SCHEMA_GAP_OVERRIDE_RE`） | `gap_type="new_entity_attribute"`，`column_name="supports_override"` | **新属性**：农场主侧 schema 扩展 |
| `new_entity` | 农民流水线**默认不**从纯无匹配路径发出（无匹配用 `overflow`）；农场主仍处理来自其他来源的 `new_entity` | 若希望无匹配统一标为 `new_entity`，需与农民会话**联合改代码**，不在树会话单独改决策逻辑 |

**交叉引用**：`command_refs` 由 `_detect_cross_refs` 从正文解析；树/文档侧应保持「相关命令」写法可被正则捕获，且与被引用命令的 `command_prefix` 一致。