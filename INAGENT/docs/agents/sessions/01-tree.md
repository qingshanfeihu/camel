# 会话宪章：树（Tree / CLI 骨架与图谱）

## 宪章维护（跨会话、职责记忆）

- **权威来源**：本文件是「树」会话职责与边界的**唯一权威**；模型上下文被压缩或换会话后，应通过重读本文（或 Cursor 规则 `kb-session-tree.mdc`）恢复角色，而不是依赖对话记忆。
- **同步义务**：在后续开发中，若「树」的职责出现 **增、删、改**（新脚本/新数据产物/农民契约字段变化等），须**同一 PR 或紧随其后的提交**内更新：
  1. 本文的「范围 / 非范围 / 交付物 / 对齐说明」中与变更相关的段落；
  2. `.cursor/rules/kb-session-tree.mdc` 中的摘要与 `globs`（使相关文件仍会挂上本规则）。
- **触发自检**：修改上列范围内路径、或用户指定由「树」会话负责时，开工前先对照本文自述职责与禁止项，避免越权改 procurement / farm_owner 等。

## 角色定位

你是 **「树」会话** 负责人。维护 **CLI 命令树、骨架索引、CLI 关键词图** 等与 `KnowledgeFarmerAgent` 树匹配、交叉引用一致的数据与脚本；保证索引格式与农民消费端契约一致。

## 范围（应改）

- `INAGENT/rag/skeleton_index.py`
- `INAGENT/scripts/enrich_cli_graph.py`
- `INAGENT/scripts/extract_cli_keywords_and_graph.py`
- `INAGENT/scripts/analyze_cli_graph.py`
- `INAGENT/scripts/enrich_cli_leaf_schema.py`、`rebuild_cli_docs.py`、`import_cli_graph_to_neo4j.py`（与树/图数据直接相关时）
- `INAGENT/knowledge_base/cli_keyword_graph.json`（由脚本再生成时）
- `INAGENT/agents/knowledge_farmer_agent.py` 中 **仅** 与 `_match_tree_node`、骨架加载、`kb_index`、交叉引用检测直接相关的逻辑（大块重构前先与农民会话对齐）

## 非范围（勿改）

- `KnowledgeProcurementAgent` 的 accept/reject/staging 决策逻辑
- `KnowledgeFarmOwnerAgent` 的 GraphRAG 结构写入与 snapshot 流程
- 评审流水线 `review/pipeline.py` 中 Worker 业务（除非修复树数据导致的阻塞且范围极小）

## 交付物

- 树 / 骨架 / `cli_keyword_graph` 与 `reference` chunk 的 **对齐说明**（PR 描述或本目录补充文档）
- 向 **农民** 会话交付：索引字段含义、匹配失败时建议上报的 gap 类型（与 `knowledge_schema.SchemaGapEntry` 一致）

## 依赖文档

- `INAGENT/docs/DATA_FLOW.md` — L1 CLI 关键词图谱
- `INAGENT/docs/DESIGN.md` — 知识层概览

## 开场白（可复制）

你是「树」会话负责人。只处理 CLI/骨架/关键词图与农民侧树匹配相关代码与数据。不修改 procurement / farm_owner 的核心决策逻辑。目标：保证 skeleton、kb_index、cli_keyword_graph 与 `knowledge_farmer_agent` 的匹配语义一致，并输出给其他会话的接口说明。

---

## 对齐说明（树 / 骨架 / 图 ↔ reference chunk）

| 产物 | 路径 / 存储 | 在农民侧的用途 |
|------|-------------|----------------|
| **骨架 + kb 索引源** | `INAGENT/knowledge_base/reference/knowledge_base.json` | `_load_skeleton()` 以 `metadata.node_id` 为键；`_load_kb_index()` 扫描同一文件，构建 `command_prefix → node_id` |
| **L1 CLI 图** | `INAGENT/knowledge_base/cli_keyword_graph.json` | 模块/命令/关键词拓扑；`skeleton_index.seed_from_cli_graph()` 等跨存储注册，不直接参与 `_match_tree_node`，但与「命令前缀、模块标签」语义应对齐（见 `DATA_FLOW.md` L1） |
| **骨架索引 DB** | `INAGENT/vector_store/skeleton_index.db` | Module/Feature/Artifact 注册表，连接 Qdrant/GraphRAG；农民当前以 JSON 骨架为主，树会话负责与图数据一致地维护该注册表 |
| **reference 分文件** | `INAGENT/knowledge_base/reference/*.json` | 农民写入 enrich 后的 chunk；**树匹配仍以 `reference/knowledge_base.json` 为准**，二者需在 `node_id` / `command_prefix` 上可核对 |

**匹配链路（农民，实现于 `knowledge_farmer_agent.py`）**

1. `_load_kb_index()`：仅当条目中 **同时存在** `metadata.node_id` 与 `metadata.command_prefix` 时，才把 `command_prefix` 编入索引。
2. `_match_tree_node(content, meta, ac_meta)`（`ac_meta` 为 auto_convert 输出，带 `section_title` 等）：
   - `meta` / `ac_meta` 中 `tree_node_id` 或 `node_id` 已在骨架中存在则直接采用；
   - `section_title`（及首行形似英文 CLI 的正文）经规范化（小写、空格转下划线）后与骨架键 **精确** 匹配；
   - `command_prefix` 来自 `_step1_rules` 或 `ac_meta`，**精确** 命中索引则采用；
   - 否则从正文抽取 CLI 候选行（含 `<>`/`[]` 的行、`_CLI_COMMAND_LINE_RE` 命中行，最后整段正文），对索引 key 做 **最长前缀** 匹配；若同长度多条命中且对应 **不同** `node_id` 则放弃（返回未匹配）。
3. `_refine_ac_meta_command_prefix`（农民侧）：在 auto_convert 给出 **粗粒度** `command_prefix` 时，若正文对 `kb_index` 存在 **唯一最长前缀** 解析，则把 `ac_meta["command_prefix"]` 提升为对应骨架 label（`auto_convert` 本身保持通用抽取，不与树契约绑死）。
4. **可选别名表** `reference/farmer_tree_alias.json`：JSON 对象，键为 `_normalize_heading_to_node_slug(section_title)` 形式的 **slug**，值为骨架 `node_id`。当标题 slug 无法与骨架键直接相等、但需固定绑到某叶时，由 **树会话** 维护该文件，农民只读。

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

与 `INAGENT/rag/knowledge_schema.py` 中 `SchemaGapKind` 一致：`new_entity` | `new_entity_attribute` | `conflict` | `overflow`。

| 场景 | 当前农民行为（`cultivate_batch` / `_diff_with_skeleton`） | 农民会话上报/排错时可标注 |
|------|----------------------------------------------------------|---------------------------|
| 命中骨架节点，某 `_SKELETON_META_FIELDS` 字段骨架有值且新值不同 | `gap_type="conflict"`，`field_name` / `skeleton_value` / `new_value` | **冲突**：需农场主或人工裁决 |
| 命中骨架节点，auto_convert 出现骨架 `metadata` 中**不存在**的字段 | `gap_type="overflow"` | **字段溢出**：是否升维 schema |
| 未命中骨架但有 `command_prefix` | `gap_type="overflow"`，`entity_title`=前缀，`nearest_matches` 辅助 | **无树节点**：多为缺 `knowledge_base.json` 条目或前缀不一致；**树会话优先补骨架/前缀** |
| 正文中出现覆盖类表述（如「支持覆盖」等，见 `_SCHEMA_GAP_OVERRIDE_RE`） | `gap_type="new_entity_attribute"`，`column_name="supports_override"` | **新属性**：农场主侧 schema 扩展 |
| `new_entity` | 农民流水线**默认不**从纯无匹配路径发出（无匹配用 `overflow`）；农场主仍处理来自其他来源的 `new_entity` | 若希望无匹配统一标为 `new_entity`，需与农民会话**联合改代码**，不在树会话单独改决策逻辑 |

**交叉引用**：`command_refs` 由 `_detect_cross_refs` 从正文解析；树/文档侧应保持「相关命令」写法可被正则捕获，且与被引用命令的 `command_prefix` 一致。