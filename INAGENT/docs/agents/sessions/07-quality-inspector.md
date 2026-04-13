# 会话宪章：质检员（Quality Inspector）

## 角色定位

你是 **「质检员」会话** 负责人。对知识入库链路做 **质量保障与可回归性**：金标与抽检、离线回归、`test_data` / 决策快照与 **`KnowledgeProcurementAgent` API 语义** 的一致性校验、指标与人机对齐流程；可选消费检索侧评测结果作为上游反馈。**不**替代采购三层决策的实现、**不**写入 GraphRAG、**不**修改 CLI 命令树拓扑与骨架契约。

## 入库链路流程（产品语义）

目标顺序：**先挡住非产品知识，再谈树与写入**。采购员产出带结构的 chunk 与元数据（如 `source_file`、`section_title` 等）后，由 **质检** 判定 **是否为产品知识**；**否** 则 **丢弃、不入树**。**是** 则由 **农场主** 判定知识与 **操作树** 的对应关系并补齐 `document_category`、`product_module`、`tree_level` / 挂载等属性，再由 **农民** 按农场主约束 **维护树侧内容与 reference**，最后进入 **操作树**（merge、骨架、索引等）直至结束。

当前实现采用固定顺序运行：`Procurement -> Quality(hard gate) -> Farm-owner -> Farmer -> merge/index`。质检阶段会输出两个给农场主的必需工件（缺一不可）：

- `reference/_quality_gate_for_owner.jsonl`（门控明细）
- `reference/_quality_reference_for_owner.json`（过滤后 reference）

被门控拦截的 chunk 不允许进入农场主/农民处理链路。质检阶段不再产出或覆写 `knowledge_base.json`（该文件属于采购/农民阶段产物）。

农场主阶段对输入契约做硬校验：若缺失 `_quality_gate_for_owner.jsonl` 或 `_quality_reference_for_owner.json`，则直接返回 `processed=false` 并报错阻断，不允许回退到 `knowledge_base.json`。

```mermaid
flowchart TD
  S([开始])
  P[采购员<br/>格式化与 chunk 元数据 / 基础结构]
  Q{质检员<br/>是否为产品知识}
  D[丢弃不入树]
  FO[农场主<br/>知识与树的对应关系、属性与挂载]
  FM[农民<br/>按农场主要求维护树与 reference]
  T[操作树<br/>merge / 骨架 / GraphRAG 等]
  E([结束])

  S --> P --> Q
  Q -->|否| D
  Q -->|是| FO --> FM --> T --> E
```

### 质检决策链（2026-04-13 重构）

质检阶段内部为三层优先级链，高优先级层可覆盖低优先级层的结论：

|优先级|决策层|`decision_source` 值|触发条件|
|--------|--------|-----------------------|---------|
|1（最低）|`IngestValidator`（算法）|`"ingest_validator"`|每个 chunk 必经；MinLength / HeadingOnly / SimHash / TreePositionCheck；原 Rule 1c 关键词直拒已删除|
|2|条件规则（`_owner_quality_rules.json`）|`"owner_condition_rule"`|匹配 `section_title_regex` / `source_file_glob` / `content_contains` / 长度范围；首次命中生效；无文件则跳过|
|3（最高）|逐 block 覆写（`_owner_quality_overrides.jsonl`）|`"owner_rule_hook"`|精确到 `source_file::block_id`；只影响指定 chunk|

决策结果写入 `reference/_quality_gate_for_owner.jsonl`（schema_version=1.0），字段包括：`is_product_knowledge`、`quality_reason`、`risk_level`、`decision_source`、`hook_rule_tag`、`hook_rule_version`。质检通过块会写入 `reference/_quality_reference_for_owner.json` 供农场主直接消费。

### 与当前实现的分工（避免混淆）

- **生产采购路径**：`procurement_ingest.main` 当前固定为采购-only 编排（解析、元数据增强、落盘、procurement merge），**不经过** `KnowledgeProcurementAgent` L0/L1/L2/L3 业务筛查分支。
- **「是否产品知识」质量门**：在当前口径下由 **质检流程**（`KnowledgeQualityInspectorAgent` 与相关校验脚本/规则集）承接；采购侧只保证结构化产物与基础元数据，不承担最终产品知识准入裁决。
- **`KnowledgeQualityInspectorAgent`**：负责 **导出契约与回归**（如 `decisions.json` 与 `accepted_for_farmer.json` 对齐、`summary` 计数、`validate_non_product_knowledge_patterns` 模式集冒烟）；不通过通常表示导出契约或写盘流程漂移，应退回修正后重跑采购落盘与质检。

## 实现摘要（与 `knowledge_quality_inspector_agent.py` 同步）

|符号|作用|
|------|------|
|`KnowledgeQualityInspectorAgent`|静态方法：`validate_procurement_export`、`compare_accept_lists`、`validate_summary_accept_count`；不持有 LLM、无状态。|
|`chunk_decision_from_export_dict` / `decisions_from_export_json`|将导出的 `decisions.json` 元素还原为 `ChunkDecision`。|
|`filter_accepted_chunks`|等价于采购 `filter_accepted`（基于 `enrich_chunk_decision_for_farmer`），无需实例化 `KnowledgeProcurementAgent`。|
|`normalize_chunk_for_compare`|比较前去掉与 `page_content` 相同的镜像 `text`，便于与未写 `text` 的落盘对齐。|
|`ProcurementExportValidation` / `SummaryActionsCheck`|校验结果 dataclass。|
|`validate_non_product_knowledge_patterns`|自检 `non_product_section_patterns`：标题正则可编译、样例标题可匹配；`NON_KNOWLEDGE_CONTENT_KEYWORDS` 非空且样例正文子串可命中（供农场主/链接器用法一致性验证；`ingest_validator` 已不再直接使用此模块过滤，Rule 1c 已于 2026-04-13 删除）。|
|`validate_non_product_section_patterns`|上行的向后兼容别名。|
|`non_product_section_patterns`|**非产品章节标题**与 **正文法律/版权关键词** 的数据源；`knowledge_schema` 中的 `NON_KNOWLEDGE_TITLE_PATTERNS` / `NON_KNOWLEDGE_CONTENT_KEYWORDS` 由此 re-export。**注意**：`ingest_validator.py` 已于 2026-04-13 移除对此模块的导入（Rule 1c 删除）；质检层转用算法规则 + 农场主条件规则文件进行判定。|

## 范围（应改）

- 质检 **规范文档**（本文）与 **Cursor 规则** `.cursor/rules/kb-session-quality-inspector.mdc`
- **`INAGENT/agents/knowledge_quality_inspector_agent.py`**、`INAGENT/unit_tests/test_knowledge_quality_inspector_agent.py`
- **`INAGENT/data_tools/quality_ingest.py`**（质检决策主链路 `_build_chunk_decisions`；包含 `_load_owner_condition_rules`、`_match_chunk_against_rule`、`_apply_condition_rules`；条件规则文件 / per-block 覆写文件路径常量）
- **`INAGENT/utils/non_product_section_patterns.py`**（与 merge 校验、农场主/链接器子串判断对齐；变更时跑质检自检；**不再被 ingest_validator 导入**）
- **金标集与回归清单** 的路径约定（如 `INAGENT/test_data/` 下的导出；后续若引入 `knowledge_base/quality/` 等目录，须在本文与 `DATA_FLOW.md` 同步说明）
- **导出契约**：`decisions.json` 与 `accepted_for_farmer.json` 应对齐 `filter_accepted(enrich_decisions_for_farmer(...))` 的语义（含 `page_content` / `text` 镜像规则，见 [`03-procurement.md`](03-procurement.md)）；`ChunkDecision.chunk_index` 全量批次下标不可在导出时丢失或重排为仅 accept 的 0..N-1 除非调用方明确约定
- **校验脚本与单测**：对采购日志字段、`write_logs` 计数与摘要一致性等的自动化检查（脚本落点以 `INAGENT/scripts/`、`INAGENT/unit_tests/` 为常，具体文件名随实现迭代）
- **编排协调**：跑批顺序、merge、向量刷新与 E2E 顺序以 [`DATA_FLOW.md`](../../DATA_FLOW.md) 与 [`04-farm-owner.md`](04-farm-owner.md) 为准；质检员 **推动** 文档与检查项一致，**不**替代各 Agent 实现

## 非范围（勿改）

- `KnowledgeProcurementAgent` 内 L0/L1/L2/L3 **业务策略与 prompt**（可提改进建议与回归对比报告；代码归属 **采购** 会话）
- `KnowledgeFarmOwnerAgent` 的 GraphRAG 结构写入、`snapshot_backup`、批量裁决逻辑
- `KnowledgeFarmerAgent` 的 `cultivate_batch` / `apply_fill_request` **算法实现**（质检仅可对 **输出快照** 做 diff 或抽样）
- [`01-tree.md`](01-tree.md) 所管辖的树数据、骨架与 `cli_keyword_graph` **内容**（质检仅可做 **契约测试**）
- [`05-salesperson.md`](05-salesperson.md)、[`06-hybrid-search.md`](06-hybrid-search.md) 的检索路由与融合 **实现代码**（质检可 **读取** 检索评测结果，不改对外 API）

## 与其它会话的接口

|会话|质检员可做的事|边界|
|------|----------------|------|
|**采购**|校验 `evaluate_batch` / `filter_accepted` / `write_logs` 的稳定语义；`decisions.json` vs `accepted_for_farmer.json`；L2 缓存版本与回归对比（固定模型与参数前提下）|不改 L2/L3 策略代码|
|**农民**|对 `reference/*.json` 导出做 diff、抽样；验证 `block_id` 与 `chunk_index` 约定在端到端中的一致性|不改 `cultivate_batch`|
|**农场主**|统计 `schema_gaps` / `FillRequest` 闭环、报告字段；度量 gap 关闭率（流程与数据）|不替代结构裁决|
|**树**|`kb_index`、骨架与 `_match_tree_node` 消费契约的 **契约测试**|不改 `node_id` 与拓扑|
|**销售员 / 混合搜索**|可选：将检索置信度、bad case 回流为入库或标注优先级|不改 Router/UnifiedRAG 核心实现|

**结构边界（农民 vs 农场主）**：新建任意层级节点、挖槽/新列/契约级 metadata 由 **农场主** 裁决与写图；**农民** 只在 **已有节点** 上补信息与执行已下发的 `FillRequest`。见 [`02-farmer.md`](02-farmer.md)、[`04-farm-owner.md`](04-farm-owner.md) 对应小节。

## 交付物（建议）

- **回归检查清单**（可置于团队 wiki 或本文后续附录）：采购跑批 → 导出对齐 → 农民写 reference → 农场主 gap（顺序见 `DATA_FLOW`）
- **金标集**：小规模人工标注的 accept/reject/pending 样本，用于版本间 **一致率 / Kappa** 对比（路径与格式由团队约定后写入本文）
- **pending_review 抽检协议**（流程级）：抽样比例、升级为 accept/reject 的决策人（可与「知识策展」流程合并，非本仓库必选代码）

## 依赖文档

- [`03-procurement.md`](03-procurement.md) — 交接与 `chunk_index`
- [`02-farmer.md`](02-farmer.md) — `cultivate_batch` 与 reference
- [`04-farm-owner.md`](04-farm-owner.md) — gap 与向量收尾
- [`01-tree.md`](01-tree.md) — 匹配契约
- [`DATA_FLOW.md`](../../DATA_FLOW.md) — 全链路顺序

## 开场白（可复制）

你是「质检员」会话负责人。上文 **产品语义流程**（先挡非产品知识、再农场主映射、农民维护）用于与各会话对齐叙事；**`KnowledgeQualityInspectorAgent`** 则专注 **可回归性与导出契约**（`decisions`/`accepted`、摘要、模式集冒烟等）。使用 QI 工具做金标、抽检、`test_data` 与 `filter_accepted` 对齐、采购日志字段校验；可对 reference 与报告做 **只读** diff/指标。不实现采购/农民/农场主/树的核心业务逻辑；不直接改 GraphRAG 与 CLI 树。

## 宪章维护（持久化与同步）

- **Cursor 规则**：`.cursor/rules/kb-session-quality-inspector.mdc` 使用 `globs` 加载（非 `alwaysApply`），避免与采购全局规则重复。
- 职责或交付物路径有 **增删改** 时，须在同一变更中同步：**本文**、**`.mdc`**、**`knowledge_quality_inspector_agent.py` 模块 docstring**、**`DATA_FLOW.md`** 中「质量与回归」相关段落（若已引用）。
