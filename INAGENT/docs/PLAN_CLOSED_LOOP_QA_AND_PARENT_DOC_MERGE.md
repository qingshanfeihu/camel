# 正式需求计划：闭环质检与父子文档合并

| 项 | 内容 |
|----|------|
| 状态 | **草案 + 骨架已落地（A1–A3、B2）**；A4/B3/B4 仍待办 |
| 基线说明 | 当前缺口见 [`agents/sessions/04-farm-owner.md`](agents/sessions/04-farm-owner.md) § **已知缺口与设计边界** |
| 关联宪章 | [`07-quality-inspector.md`](agents/sessions/07-quality-inspector.md)、[`02-farmer.md`](agents/sessions/02-farmer.md)、[`DATA_FLOW.md`](DATA_FLOW.md) §8 |

本文档将两项能力列为 **产品级正式需求**，给出目标、契约草图、责任边界与分阶段交付，便于排期与 PR 拆分；**不**替代具体接口定稿（定稿应在各宪章与 `knowledge_schema` 中落地）。

---

## 1. 背景与原则

- **闭环质检**：在现有 **前向** 路径（`quality_ingest` → `_quality_gate_for_owner.jsonl` → `farm_owner_ingest` 过滤）之上，增加 **反馈—规则—执行** 闭环，使离线抽检、金标 diff、误收报告能 **可追溯地** 收敛到规则与落盘修正。
- **父子文档合并**：在现有 **图侧** `merge_into_existing` 与 **树上下文** 注入之外，增加 **reference 层** 可编排的「子文档并入父文档」策略，减少碎片化 chunk 与重复检索命中，且 **不破坏** `block_id` 与合并 KB 指纹语义。
- **原则**：编排显式、工件可版本化、默认可回滚；**物理删除**与 **规则变更** 必须可审计；优先扩展 **数据工具与农民**，再评估是否引入 **农场主** 仅作结构化意图（`FillRequest`）产出。

---

## 2. Epic A：闭环质检

### 2.1 需求陈述（正式）

1. **反馈摄入**：系统能消费一种或多种 **质检产物**（例如：抽检报告 JSON、金标 diff、`decisions.json` 与 reference 的对照结论），识别 **无用/误收/应降级** 的知识单元，并稳定映射到 **`source_file` + `block_id`**（或与现有 gate key `source_file::block_id` 对齐）。
2. **归因与建议**：对每条反馈给出 **machine-readable** 的归因标签（如 `false_positive_product`、`wrong_module`、`duplicate_chunk`、`legal_boilerplate`）及可选 **规则建议**（模式、元数据条件、章节标题模式等），供人审或自动合并。
3. **向质检侧下发规则**：存在 **版本化** 的「规则补丁」工件（例如扩展现有 `quality_ingest` 的 condition rules / owner overrides 格式），由 **编排或专用模块** 写入约定路径；**质检链路**负责校验 schema、合并策略（覆盖/追加）、并输出 **变更日志**（谁、何时、基于哪次反馈）。
4. **基于反馈的库修正**：在人工或策略门控批准后，能对 **reference** 与（按需）**合并 KB** 执行 **标记剔除 / 物理删除 / 元数据修正**，并触发约定下游（`merge_knowledge_base`、向量 `force` 策略、GraphRAG 增量或全量，**另表**）。

### 2.2 建议工件（契约草图，落地时固化）

| 工件 | 方向 | 说明 |
|------|------|------|
| `reference/_quality_feedback_inbox.jsonl` | 入 | 每条：反馈 id、`source_file`、`block_id`、verdict、`reason_code`、可选 `suggested_rule`、来源 run_id |
| `reference/_quality_rule_proposals.json` | 出（待审） | 聚合后的规则草案 + 冲突检测提示 |
| `config/quality_condition_rules.patched.json`（示例名） | 出（已审） | 与 `quality_ingest` 现有加载方式对齐的 **正式规则文件**；需 schema_version |
| `reference/_quality_purge_manifest.jsonl` | 出（已批） | 批准删除/修正的块列表；**农民或专用 purge 脚本** 消费 |

具体文件名与 schema 以 PR 中 **`07-quality-inspector.md` + `DATA_FLOW.md`** 更新为准。

### 2.3 责任边界（建议）

| 会话 / 组件 | 职责 |
|-------------|------|
| **质检员（07）** | 定义反馈与规则文件的 **schema**、回归用例、合并/校验脚本入口；**不**独自实现农民写盘算法 |
| **农民（02）** | `apply_fill_request` 或专用 API：**按 manifest 删除/改写块**、写 `chunk_meta_patch`（如 `owner_excluded`）；保证 `block_id` 稳定性策略在文档中写明 |
| **编排 / data_tools** | 串联：读 inbox → 生成 proposals → 人审闸门 → 写规则文件 → 可选触发 purge → `merge_knowledge_base` + 向量 |
| **农场主（04）** | **默认不**负责闭环；若未来需「图实体与误收块一致性 purge」，仅通过 **显式 gap 或新工件** 触发，不隐式扫盘 |

### 2.4 分阶段交付

| 阶段 | 交付物 | 验收要点 |
|------|--------|----------|
| **A1** | 反馈 inbox schema + 只读校验脚本 + 文档 | ✅ `quality_feedback_loop.validate_*` + `scripts/validate_quality_feedback_inbox.py` + `DATA_FLOW` §8 |
| **A2** | rule_proposals 生成器（规则建议聚合）+ 质检侧合并规则 PR 流程 | ✅ `build_rule_proposals_from_inbox` + `scripts/build_quality_rule_proposals.py`；合并至 `_owner_quality_rules.json` 仍 **人工** |
| **A3** | purge manifest + 农民/脚本执行删除或打标 + 单测 | ✅ `apply_purge_manifest` + `scripts/apply_quality_purge_manifest.py`（默认 dry-run）+ `unit_tests/test_quality_feedback_loop.py` |
| **A4**（可选） | GraphRAG 侧与 purge 对齐（实体归档/删边） | 与 `graphrag_adapter` 能力评估后立项 |

### 2.5 非目标（本 Epic）

- 不强制要求 **农场主 LLM** 自动提炼规则（可作为 A2 的 **可选** 助手）。
- 不在未定义 **批准闸门** 的情况下自动物理删除生产库。

---

## 3. Epic B：父子文档合并

### 3.1 需求陈述（正式）

1. **可配置的合并策略**：当 CLI 树或元数据指明 **父—子文档** 关系（例如同一 `function_hierarchy` 前缀、或 `cli_graph` 的 `parent_of` 与 reference 文件/模块绑定）时，编排可将 **子 reference 文档中的块** 按策略 **并入父 reference 文档**（或并入指定「容器」文档），并更新子侧 **指针/占位**（删除、stub 或 `merged_into` 元数据），避免检索重复与维护分裂。
2. **与现有合并管道兼容**：合并后的 reference 再进入现有 **`merge_knowledge_base` → `knowledge_base.json`**；**block_id** 冲突策略、去重键、以及 **混合向量指纹** 行为必须在 `DATA_FLOW` 与 [`06-hybrid-search.md`](agents/sessions/06-hybrid-search.md) 中 **显式**说明。
3. **与图侧语义区分**：本 Epic 处理的是 **向量与 reference 文档拓扑**；**GraphRAG 实体合并** 仍遵循农场主 `merge_into_existing` 等现有语义，**不**混为一谈，但可通过编排 **先后** 执行并约定顺序。

### 3.2 触发条件与数据来源（建议）

- **主来源**：`CLIGraphStore` 父链 + reference 侧 `metadata`（如 `product_module`、`tree_position`、`source_file` 命名约定）。
- **策略配置**：例如 `config/parent_child_doc_merge.yaml`（父子匹配键、是否保留子文件空壳、最大块数阈值）。

### 3.3 责任边界（建议）

| 会话 / 组件 | 职责 |
|-------------|------|
| **编排 / data_tools** | 新增 **`merge_reference_parent_child`**（示例名）或扩展 `merge_knowledge_base` 前置步骤；**幂等**与 dry-run |
| **农民（02）** | 若需 LLM 润色合并正文，走 **显式** `cultivate_batch`/填充任务，**不**默认静默改写 |
| **农场主（04）** | 可选：对「合并后图实体冗余」产出 gap，**不**承担文件级合并 |
| **质检（07）** | 对合并前后 reference 做 **快照 diff** 与抽检指标 |

### 3.4 分阶段交付

| 阶段 | 交付物 | 验收要点 |
|------|--------|----------|
| **B1** | 设计文档：合并键、block_id 迁移表、回滚策略 | 评审通过后再写代码 |
| **B2** | 纯机械合并（无 LLM）：同前缀/配置驱动的块搬迁 + 单测 | ✅ `merge_reference_parent_child` + `scripts/merge_reference_parent_child.py` + `config/parent_child_doc_merge.example.yaml` + `test_merge_reference_parent_child.py` |
| **B3** | 与 `merge_knowledge_base`、向量刷新顺序写入 `DATA_FLOW` | ✅ §8 推荐顺序；**E2E 脚本** 可后续补 |
| **B4**（可选） | 合并后 GraphRAG 去重/别名 | 依赖 A4 与图侧能力 |

### 3.5 风险与约束

- **block_id 变更**会导致历史 gate 键失效；优先策略是 **保留子块 block_id 迁入父文件** 或维护 **别名表**（须在计划中二选一并在宪章写明）。
- **向量全量成本**：大规模合并后可能需 **`force_rebuild_vectors=True`**，应在编排层显式开关。

---

## 4. 横切事项

- **编排顺序**：闭环 purge 与父子合并 **谁先谁后** 须在 `DATA_FLOW.md` 用一节流程图或步骤表固定；默认建议：**规则审阅** → **reference 修正（农民/purge）** → **父子合并（若启用）** → **`merge_knowledge_base`** → **向量** → **农场主图维护（若仍有 gap）**。
- **单测**：每个 Epic 至少 `unit_tests/` + 一条最小 `tmp_path` fixture 流水线测试。
- **安全**：任何删除类 manifest 必须 **双键确认**（路径白名单 + 产品根）以防误删。

---

## 5. 落地时须同步的文档清单

- [`04-farm-owner.md`](agents/sessions/04-farm-owner.md)：更新「已知缺口」表状态或改为「部分已实现」并链到本节子节。
- [`07-quality-inspector.md`](agents/sessions/07-quality-inspector.md)：闭环工件与质检负责项。
- [`02-farmer.md`](agents/sessions/02-farmer.md)：purge / 合并正文的实现归属。
- [`DATA_FLOW.md`](DATA_FLOW.md)：新工件路径与全链路顺序。
- 若触及向量语义：[`06-hybrid-search.md`](agents/sessions/06-hybrid-search.md)。

---

## 6. 实现索引（代码）

| 组件 | 路径 |
|------|------|
| Inbox / proposals / purge 核心 | `INAGENT/data_tools/quality_feedback_loop.py` |
| 父子 reference 合并 | `INAGENT/data_tools/merge_reference_parent_child.py` |
| CLI | `INAGENT/scripts/validate_quality_feedback_inbox.py`、`build_quality_rule_proposals.py`、`apply_quality_purge_manifest.py`、`merge_reference_parent_child.py` |
| 配置样例 | `INAGENT/config/parent_child_doc_merge.example.yaml` |
| 单测 | `INAGENT/unit_tests/test_quality_feedback_loop.py`、`test_merge_reference_parent_child.py` |

---

## 7. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-04-03 | 初稿：闭环质检 + 父子文档合并正式需求与分阶段计划 |
| 2026-04-03 | 落地 A1–A3、B2 骨架与文档/单测 |
