# 树侧扩展性盘点（橡皮泥接口）

> 供 **农民 / 农场主 / 全局入库管线** 消费：树会话维护下列 **挂钩与载体**，**不**裁定某份 spec、架构或 Bug 文档在知识本体中算「根 / 枝 / 叶」。
>
> 权威职责边界见 [01-tree.md](01-tree.md) 中「职责边界：CLI 真相源与橡皮泥」。

## 0. 硬约束：扩展不得改变现有树信息

- **本文档仅为说明**：阅读它不产生任何数据变更；**不得**把「扩展性」理解为可以静默改写已发布的 `cli_keyword_graph.json`、`reference/knowledge_base.json` 骨架块或 XML 推导出的拓扑。
- **代码与数据变更原则**（树会话执行扩展时须遵守）：
  - **默认零影响**：未显式运行 **版本化脚本**（如 `extract_*` / `enrich_*` / `rebuild_cli_docs`）且未经过评审时，**不改变**已有 `node_id`、`command_prefix`、`parent_of` / `contains` 边集与叶子数量。
  - **可加不可偷换**：新能力以 **新增可选字段、新表行、新边类型（且旧消费者忽略）** 为主；禁止「同 id 改语义」式兼容破坏。
  - **外挂数据**：`skeleton_index` 的 `artifacts` / `artifact_links`、农民的 `reference/*.json` 等 **叠加层** 的写入，**不得回写** CLI 图节点必填字段或骨架契约字段，除非单独 PR 明确迁移。
- **谁塑形谁负责**：在 `artifact_links`、GraphRAG、chunk 元数据里把 spec/场景/架构挂到树上，是 **农民 / 农场主** 的写入策略；树会话 **只保证** 挂钩存在且 CLI 主干不被非 CLI 类型绑死。

## 1. 已有扩展点（可塑载体）

| 层级 | 位置 | 能力 | 典型用途（由非树会话决定） |
|------|------|------|---------------------------|
| **L1 图节点** | [cli_keyword_graph.json](INAGENT/knowledge_base/cli_keyword_graph.json) | `module` / `command` / `operation_command` 上除 `id`/`type`/`label`/`parent` 外，尚有 enrich 字段：`protocol_stack`、`address_family`、`layer`、`interface_types`、`related_modules`、`feature_tags`、`keywords`、`help_string`、`full_syntax`、`parameters`、`scope`、`operations`、`func`、`actual_module` 等 | 继续 **加可选 JSON 键**（不破坏 `parent_of`/`contains` 语义即可）作为标签或外系统 ID 的挂载点 |
| **图边** | 同上 `edges[]` | `contains`、`parent_of`、`has_operation`、`operation_of`、`shares_keyword`；权重字段 | 新边类型需 **extract/enrich 脚本与 CLIGraphStore** 同步；树会话改 schema 时负责兼容 |
| **运行时查询** | [cli_graph_store.py](INAGENT/rag/cli_graph_store.py) `CLIGraphStore` | `extract_subgraph`、`get_branch_ids`、`derive_trunk`、`l2_connected`、`get_operations_group`、`resolve_module`（经 `SkeletonIndex` 别名）等 | 上层按 **模块 hint、branch、关键词** 取子图或连通性，**不隐含**「架构文档 = 根」 |
| **骨架索引 DB** | [skeleton_index.py](INAGENT/rag/skeleton_index.py) | `modules`：`module_id`、`label`、`aliases`（JSON）、协议栈等；`features`：`feature_id`、`module_id`、`trunk_layers`/`trunk_planes`、`knowledge_sources`；`artifacts`：`register_artifact`；**`artifact_links`**：`add_link(source_id, target_id, link_type, confidence, face_type)` | 将 **任意 artifact_id**（chunk、实体、外部文档键）与 **模块 / 另一 artifact** 链接；**语义由写入方定义**（如 `references`、`implements` 等字符串约定） |
| **骨架块 metadata** | `reference/knowledge_base.json` | 稳定键：`node_id`、`command_prefix`、`product_module`、`func`、`scope`、`document_category`、`source_file` 等 | 农民 `_match_tree_node` / `kb_index` 契约；外部 chunk 与 **哪一叶** 对齐由 **农民与元数据管道** 决定 |
| **标题别名** | `reference/farmer_tree_alias.json`（可选） | slug → `node_id` | 树会话维护；农民只读；用于标题无法与 `node_id` 自动对齐时的 **固定绑定** |

## 2. 与「结构真相源」的硬边界

- **唯一 CLI 拓扑真相源**：`knowledge_base/input/command_tree-*.xml`（经脚本生成图与骨架）。  
- **不在此文件列出的「产品知识」**（spec、架构 PDF、Bug 叙事等）：其入库路径、`document_category`、GraphRAG 实体类型、是否写入 `artifact_links` → **非树宪章核心**；树只保证 **图与骨架 schema 不被非 CLI 类型绑死**。

## 3. 已知缺口 / 后续可由树或其它会话补齐

| 缺口 | 说明 | 建议负责方 |
|------|------|------------|
| **`artifact_links` 查询 API** | 当前以 `add_link` 写入为主；缺少按 `source_id`/`target_id`/`link_type` 的列表/遍历封装 | 树或 **销售员/工具层** 按需补读 API |
| **节点自定义键的 JSON Schema** | 新增图节点字段无单一 schema 文件，靠脚本约定 | 树会话在改 enrich 时补文档或轻量 schema |
| **`features.kb_fh_mappings` 与 function_hierarchy** | 种子化依赖 `knowledge_base.json`（非 reference 路径）里 `function_hierarchy` 的统计；与农民常用 `reference/knowledge_base.json` 可能不一致 | **农民/合并策略** 或树会话统一路径约定 |
| **command 节点 `id` 与 GraphRAG 实体 title** | 需人工或农场主策略对齐时，无自动校验 | **农场主** + 可选校验脚本 |

## 4. 变更同步

若增删 **图边类型**、**skeleton_index 表列**、或 **骨架 metadata 契约字段**，须同步：

1. [01-tree.md](01-tree.md)「对齐说明 / 向农民交付」  
2. `.cursor/rules/kb-session-tree.mdc`  
3. 本文档对应表格
