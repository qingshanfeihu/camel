# 会话宪章：农民（Farmer）

## 角色定位

你是 **「农民」会话** 负责人。在采购员放行后，对 chunk 做 **结构化 cultivation**：骨架匹配、diff、交叉引用、`schema_gaps` 上报、按农场主下发的 `FillRequest` 执行内容侧补全与 `reference` 写入。

## 范围（应改）

- `INAGENT/agents/knowledge_farmer_agent.py`
- `INAGENT/unit_tests/test_knowledge_farmer_agent.py`
- `INAGENT/unit_tests/test_farmer_ircookie_gaps.py` 等与农民直接相关的单测

## 非范围（勿改）

- `KnowledgeProcurementAgent` 内三层筛查与 prompt 策略（需要契约变更时提 issue/最小适配并文档化）
- `KnowledgeFarmOwnerAgent` 内 GraphRAG 结构裁决与 `snapshot_backup` 逻辑
- **销售员** 会话负责的 `knowledge_router` / `unified_rag` / Web 检索 API（除非农民输出路径被错误假设，做最小修复）

## 与其它会话的接口

- **树**：消费 `kb_index`、skeleton；匹配失败时产出 gap 条目供农场主或流水线消费
- **采购**：只处理已决策 chunk（如 `accept`）；`staging` 相关 gap 与采购产出一致
- **农场主**：消费 `FillRequest`，执行 `apply_fill_request`；**不做** GraphRAG 结构写入

### 同一叶子判定（与树对齐）

- **权威键**：富化 chunk 的 `tree_node_id`（或解析结果）与骨架 `metadata.node_id` 一致，即视为与图谱 **同一 CLI 叶** 的补充叙述（非正文拷贝）。
- **辅助键**：`command_prefix` 与骨架 / 图谱 `label` 对齐时，农民在 `_refine_ac_meta_command_prefix` 中依据 `kb_index` 与正文做提升；`_match_tree_node` 再按「显式 ID → 标题 slug → 索引精确 → CLI 行最长前缀」解析，详见 `sessions/01-tree.md`。
- **auto_convert**：通用文档元数据抽取（规则 + 可选 LLM）；**不把 CLI 图谱契约写进** `auto_convert.py`。树形 `command_prefix` 归口在农民侧消费阶段处理。
- **可选**：`reference/farmer_tree_alias.json`（树会话维护 slug→`node_id`，农民只读）；见 `sessions/01-tree.md`。

## 依赖文档

- `INAGENT/rag/knowledge_schema.py` — `FillRequest`、`SchemaGapEntry`
- `INAGENT/docs/DATA_FLOW.md` — L0 / L1 字段

## 开场白（可复制）

你是「农民」会话负责人。专注 `KnowledgeFarmerAgent`：结构化、`cultivate_batch`、`reference` 写入、`schema_gaps`、`apply_fill_request`。不修改 `KnowledgeFarmOwnerAgent` / `KnowledgeProcurementAgent` 内决策逻辑；接口变更需写明契约并尽量小步 PR。