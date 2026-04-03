# Bug-to-Case 输入文档使用说明

本文档说明 Bug-to-Case 流程中，`input` 相关文档在各阶段如何被使用、是否会进入待评审对象，以及对评审结果的影响边界。

## 1. 流程总览

1. 预处理阶段：`prepare_bug_to_case.py`
2. 执行阶段：`run_review.py`
3. 结构化输入构建：`review/input_builder.py`
4. 多 worker 评审：`review/pipeline.py`
5. 输出阶段：`review_*.md`、`_summary.md`、`review_debug_*.jsonl`、`review_findings_*.json`

## 2. 输入文档与用途矩阵

| 输入文档 | 来源 | 进入方式 | 用途 | 是否作为“待评审用例” |
|---|---|---|---|---|
| Excel 测试用例（各 Sheet） | `Bug xxx` 目录中的 `.xlsx/.xls` | 由 `prepare_bug_to_case.py` 解析为 `bug_to_case.json.sheets[*].modules[*].cases`，再由 `run_review.py` 拼装为 `test_cases_text` | 评审主对象（逐模块） | 是 |
| Bug fix detail 文本 | `*fix*detail*.txt` / `*bug*detail*.txt` | 由 `prepare_bug_to_case.py` 同时解析为结构化字段和原文段落，再映射到 `bug_info` + `bug_context` + `bug_profile` | 提供根因、修复建议、关键问题，并保留原文语义 | 否（仅上下文） |
| `bug_profile.json`（可选） | bug 目录（手工补充） | 由 `prepare_bug_to_case.py` 合并覆盖自动解析字段 | 修正/增强 bug 上下文 | 否（仅上下文） |
| 评审规则文档 | 知识库 `review/rules` | `ReviewInputBuilder -> get_review_rules()` | R01-R18 规则约束 | 否（规则） |
| 产品规格/设计文档 | 知识库 `spec/prd`、`spec/func_spec`、`spec/design` | `ReviewInputBuilder -> search_product_knowledge()` | 功能点与覆盖参考 | 否（知识） |
| CLI 参考文档 | 知识库 `cli/reference` | `ReviewInputBuilder -> search_product_knowledge(category_filter='cli/reference')` | CLI 语法校验参考 | 否（知识） |
| 相似测试用例文档（含 HTTP2 等历史用例） | 知识库 `test/test_list` | `ReviewInputBuilder -> search_similar_tests()` | 对比参考、补充建议依据 | 否（参考，不是本次待评审对象） |

## 3. 关键边界：什么是“待评审对象”

当前流程中，真正的待评审对象只有：

- 当前模块的 `test_cases_text`（来自本次 Excel 模块数据）

另外，人工评审意见（例如 `manual_review_notes.txt`）仅允许用于离线对比分析，不允许进入在线评审生成输入。
具体约束如下：

- 不得写入 `bug_profile`（例如 `Manual_Review_Notes`）
- 不得拼接到 `Key_Questions`、`Review_Focus`
- 不得注入 manager/worker 提示词（包含 taskpack）

其他文档（包括 HTTP2 历史用例）属于“知识参考上下文”，用于：

- 判断是否缺失场景
- 提出补充建议
- 校验命令/规范一致性

不应被当作本次输入用例去做“逐条判错”。

## 4. 你关心的问题：HTTP2 用例是否被当成新输入

结论：

- 设计上，HTTP2 等历史用例来自 `similar_tests`，属于参考库数据，不是本次待评审清单。
- 风险点在于：如果上下文提示不够明确，模型可能把参考内容误混入“本次用例”的叙述中。

当前代码中，`pipeline.py` 会把以下内容注入 worker 任务正文：

- `test_cases`（本次模块）
- `product_knowledge`
- `similar_tests`（可能含 HTTP2 参考）
- `cli_reference`
- `bug_context`

因此，语义边界依赖提示词约束，而不是天然隔离。

## 5. 建议的边界强化（建议你确认）

为避免“参考集误判为评审集”，建议在任务正文和输出要求里增加硬约束：

1. 明确声明：`similar_tests/product_knowledge/cli_reference` 仅用于参考，不属于待评审样本。
2. 输出问题必须仅引用 `test_cases` 的 `#` 编号。
3. 若引用参考集，只能出现在“覆盖缺口建议”小节，且标注“参考依据”。

## 6. 各阶段输入消费明细

### 6.1 `prepare_bug_to_case.py`

- 解析 Excel 为结构化 cases（按 sheet/module 分组）
- 解析 bug fix txt 为 `bug_info`，同时保留 `bug_detail_raw` 和 `bug_detail_sections`
- 生成 `bug_context.key_questions` 与 `sheet_roles`
- 输出 `bug_to_case.json`

### 6.2 `run_review.py`

- 逐 sheet、逐 module 构造 `test_cases_text`
- 将 bug 信息映射到 `bug_profile`（含 `Summary`、`Bug ID`、`Key_Questions`、`Sheet_Context`、`Bug_Detail_Raw`）
- 调用 `ReviewPipeline.run(...)`
- 额外输出 `review_findings_{sheet}.json` 结构化问题清单

### 6.3 `review/input_builder.py`

- 生成 `review_rules`、`product_knowledge`、`cli_reference`、`similar_tests`
- 构建 `rag_queries`（模块、协议、bug 根因维度）
- 输出 `ReviewInput`（统一输入包）

### 6.4 `review/pipeline.py`

- 将 `ReviewInput` 内容裁剪后注入各 worker 任务正文
- 将 `bug_detail_raw` 片段注入 `bug_context`，降低 bug 语义在摘要阶段被压缩丢失的风险
- 执行 coverage/cli/spec/load/synthesis 多任务
- 返回 `review` 与 `rag_status`

## 7. 双通道 bug detail 说明

当前 bug detail 在评审链路中按“双通道”传递：

- 结构化通道：提取 Description、Root Cause、Condition of Occurrence、Fixed Details、Testing Suggestions 等字段，供查询构造和提示词摘要使用
- 原文通道：保留 `bug_detail_raw` 和 `bug_detail_sections`，供 `run_review.py`、`input_builder.py`、`pipeline.py` 在需要时直接引用，减少重要细节在摘要过程中丢失

这种设计是增量兼容的：旧字段继续保留，新字段仅作为补充，不改变既有 `bug_info` 的核心消费方式。

## 8. 输出兼容性说明

本轮改造没有替换原有输出，而是在原链路上新增结构化产物：

- `review_*.md` 仍是人工阅读主报告
- `review_debug_*.jsonl` 仍是调试主入口，并新增了调度告警和 `structured_findings` 事件
- `review_findings_*.json` 是新增扩展输出，适合自动回归、统计和后续评测

如果现有流程只消费 Markdown 或 debug JSONL，可以保持不变。

## 9. 检查建议（验收时可直接看）

请优先检查最终报告中的两类语句：

- 若出现“用例 #X”之外的编号或直接点评 HTTP2 历史用例条目，说明边界被污染。
- 若“覆盖缺口”引用了 HTTP2 场景但未标注“参考依据/非本次输入”，也建议视为不合规。

---

如需，我可以在下一步直接把第 5 节的三条边界约束固化进 `pipeline.py` 的任务模板和输出模板，确保模型无法把参考集当成本次待评审集合。
