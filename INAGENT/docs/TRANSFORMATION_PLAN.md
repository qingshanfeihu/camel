# INFOAGEN 详细改造计划：auto_convert、GraphRAG 与 workflow 一体化

> 参考：README.md、What's New.MD、UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md  
> 目标：功能完整、启动脚本正常使用、改造步骤可执行且每步可验证。
> 
> **状态：✅ 已完成（2026-01-30）**

---

## 实施完成状态

| 阶段 | 状态 | 完成日期 | 说明 |
|------|------|----------|------|
| **阶段 0** | ✅ 完成 | 2026-01-30 | GraphRAG 可选机制、README/What's New 同步 |
| **阶段 1** | ✅ 完成 | 2026-01-30 | product_modules_registry.json + postprocess_step_extract.py |
| **阶段 2** | ✅ 完成 | 2026-01-30 | Section 预扫描 + 规则增强 |
| **阶段 3** | ✅ 完成 | 2026-01-30 | 索引与 GraphRAG 一致性确认 |
| **阶段 4** | ✅ 完成 | 2026-01-30 | 脚本文档完善 (run_inagent_pipeline.bat, start_gateway.bat) |

### 主要变更文件

```
# GraphRAG 可选机制 (Phase 0)
INAGENT/rag/unified_rag.py
INAGENT/rag/graphrag_integration.py  
INAGENT/scripts/rag_config.py
INAGENT/run_inagent_pipeline.ps1

# 产品模块注册表 (Phase 1)
INAGENT/config/product_modules_registry.json
INAGENT/scripts/postprocess_step_extract.py

# Section 预扫描 (Phase 2)
INAGENT/workflow/workflow_rules_config.py

# 脚本文档 (Phase 4)
run_inagent_pipeline.bat
llm_gateway/start_gateway.bat
llm_gateway/README.md

# 文档同步
INAGENT/WHATS_NEW.md
What's New.MD
```

---

## 一、当前项目逻辑与数据流（梳理）

### 1.1 入口与脚本

| 入口 | 脚本/命令 | 作用 |
|------|-----------|------|
| 菜单 / 直接模式 | `run_inagent_pipeline.ps1 -Mode db/init/interactive/test/jobs` | 统一入口，根据 Mode 调用不同流程 |
| 数据库管理 | `manage_database.py --action status|create|update|rebuild|delete` | 维护 kb + function_structure_index；PS1 在 create/update/rebuild 后自动执行 GraphRAG init+build |
| 全量初始化 | `initialize_pipeline.py` | 步骤 1–7：auto_convert；步骤 8：initialize_rag_system（向量 RAG + 可选 GraphRAG） |
| GraphRAG | `scripts/init_graphrag.py --init --build` | 从 knowledge_base.json 准备输入、构建 GraphRAG 索引 |
| Workflow/Jobs | `workflow_config_generator.py` | 任务分解 → RAG 检索（向量 + GraphRAG + Rerank）→ 配置生成 |

### 1.2 数据流（关键文件与依赖）

```
doc_local/*.pdf
    ↓ MinerU (content_list)
doc_local/mineru_output/<task_id>/*.json
    ↓ auto_convert（规则 + LLM 元数据，合并、增量更新索引）
doc_local/reference/*.json → merge → doc_local/reference/knowledge_base.json
doc_local/function_structure_index.json  (由 auto_convert 内 incrementally_update_function_index 或 manage_database 内 _build_index_from_kb 生成)
    ↓ init_graphrag.py --init --build (prepare_input_documents 读 kb)
graphrag_index/input/documents.json → GraphRAG 构建
graphrag_index/output/*.parquet
    ↓ workflow 检索 (UnifiedRAGRetriever: 向量 + GraphRAG + Rerank)
constraints (product_modules, protocol_types) → task_decomposition_agent (function_structure_index 的 product_modules / scenarios)
```

### 1.3 auto_convert 与 GraphRAG、workflow 的交互点

| 环节 | 交互内容 | 说明 |
|------|----------|------|
| auto_convert → kb | 每个 chunk 的 metadata（product_module, protocol_type, step_type, section_title, parent_section, scenario_id, intent 等） | GraphRAG 的 prepare_input_documents 用 product_module/step_type/section_title 写入口文档前缀和 metadata；workflow 用 metadata 做约束与过滤 |
| auto_convert → function_structure_index | 增量更新由 auto_document_integration.incrementally_update_function_index 完成；全量由 build_function_structure_index（manage_database 调用） | task_decomposition_agent 的「可用产品模块」、scenarios、required_steps 来自此索引 |
| kb → GraphRAG | graphrag_adapter.prepare_input_documents(kb) | 仅读 kb，不读 function_structure_index；product_module 等写入 documents.json 的 metadata 与文本前缀 |
| workflow 检索 | UnifiedRAGRetriever(hybrid_retriever, reranker, graphrag_retriever) | 向量检索 → 可选 GraphRAG 合并 → Rerank → _build_constraints_from_metadata(metadata_list) → constraints["product_modules"] 等 |
| workflow 分解 | task_decomposition_agent 使用 function_structure_index 的 metadata_statistics.product_modules、scenarios、step_types | product_modules 列表约束 LLM 输出；required_steps 等来自场景定义 |

结论：**product_module 等元数据由 auto_convert 写入 kb → 同时影响 GraphRAG 输入与 workflow 的约束/过滤；function_structure_index 由 kb 统计/推断得到，供分解与场景匹配。** 改造时需保证：  
1）auto_convert 输出（含 product_module 修正）一致写入 kb；  
2）索引（function_structure_index、GraphRAG）在 kb 更新后按现有脚本可重建/同步；  
3）启动脚本顺序与可选 GraphRAG 行为一致，避免首次 create 时因 GraphRAG 未建而失败。

---

## 二、当前存在的问题（与改造目标）

### 2.1 已知问题

1. **product_module 误标 unknown**  
   大量本属用户管理、高可用、SLB、基础网络等的块被标为 unknown；根因见 `UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md`（LLM 保守、规则仅块内关键词、未用 intent/scenario_id 反推、未用 MinerU 章节层级）。

2. **初始化与 GraphRAG 顺序**  
   - `initialize_pipeline.py` 步骤 8 调用 `initialize_rag_system()`，当前实现**强制** GraphRAG 可用（`USE_GRAPHRAG=true` 且索引未建则报错）。  
   - `manage_database.py` 的 create：若 kb 不存在会执行 `_run_initialize_pipeline()`，即先 auto_convert 再 initialize_rag_system；GraphRAG 的 `--init --build` 是在 **manage_database 返回后** 由 PS1 执行的。  
   - 因此**首次 create 时**：kb 刚生成，GraphRAG 尚未构建，步骤 8 会报错「GraphRAG 索引未构建」，导致 init 失败。

3. **initialize_pipeline 返回值解包**  
   `initialize_pipeline.py` 中 `hybrid_retriever, reranker = initialize_rag_system()` 只解包两个值，但 `initialize_rag_system()` 返回 `(hybrid_retriever, reranker, graphrag_retriever)`，存在不一致（若未来使用第三项会漏掉；当前因步骤 8 可能先报错而未暴露）。

4. **MinerU 章节未充分利用**  
   content_list 的 text_level/编号标题未用于预填 parent_section/section_title，规则增强也未对 section_title/parent_section 做关键词匹配，导致「仅标题无正文」的块易标 unknown。

5. **功能模块单源与修正流程**  
   产品模块与关键词分布在 mineru 的 metadata_rules；发现误标或新增模块时，缺少统一主配置与「仅改 kb 不重跑 LLM」的后处理脚本，且与 GraphRAG/workflow 的衔接未成文。

### 2.2 改造目标

- **功能完整**：unknown 修正、模块可扩展、章节层级与 intent/scenario 反推落地；GraphRAG 与 workflow 继续使用同一 kb 与索引。  
- **启动脚本正常**：  
  - 菜单 1（db create/update/rebuild）后，GraphRAG 在 kb 就绪后再构建，且 init 流程在 GraphRAG 未建时可选降级（不强制报错）。  
  - 菜单 4（Full init）、菜单 5（Jobs）与现有文档描述一致，可正常运行。  
- **可维护**：产品模块主配置（含 intent/scenario→module 映射）、后处理脚本、文档与 README/What's New 对齐。

---

## 三、详细改造步骤与每步修改内容

### 阶段 0：准备与兼容性（先做，保证脚本不崩）

#### 步骤 0.1：修复初始化与 GraphRAG 的先后与解包

**目标**：首次 db create 时，步骤 8 不因 GraphRAG 未构建而失败；解包与返回值一致。

**修改**：

1. **workflow_config_generator.py `initialize_rag_system()`**  
   - 当 `use_graphrag=True`（或环境变量为 true）但 GraphRAG 索引未构建时：**不 raise**，改为将 `graphrag_retriever=None`，并打 logger.warning，提示「GraphRAG 未构建，将仅使用向量检索；可执行 init_graphrag.py --init --build 后再次运行 workflow」。  
   - 保持返回 `(hybrid_retriever, reranker, graphrag_retriever)`。

2. **initialize_pipeline.py**  
   - 解包改为：`hybrid_retriever, reranker, graphrag_retriever = initialize_rag_system()`（若返回三元组）。  
   - 若暂时保留两元组返回，则需在 workflow_config_generator 中保证在「GraphRAG 不可用时」返回两元组或第三项为 None，并在 initialize_pipeline 中兼容两种返回（例如 `result = initialize_rag_system(); hybrid_retriever, reranker = result[0], result[1]`）。

**验收**：  
- 无 kb、无 GraphRAG 时：`run_inagent_pipeline.ps1 -Mode db -DbAction create` 能跑通（步骤 8 不报错）；随后 PS1 执行 `init_graphrag.py --init --build` 成功。  
- 有 kb 且已 build GraphRAG 时：菜单 4 全量初始化、菜单 5 Jobs 正常。

---

#### 步骤 0.2：README 与脚本说明对齐

**目标**：README.md / What's New.MD 与真实行为一致。

**修改**：

1. **README.md**  
   - 在「使用方式」或「初始化流程」中注明：  
     - 方式 1：先「数据库管理 → create」（会生成 kb 并可选构建 GraphRAG）；若使用 GraphRAG，需在 create/update/rebuild 后自动或手动执行 `init_graphrag.py --init --build`。  
     - 方式 2：全量初始化（步骤 4）包含步骤 1–8；若未先构建 GraphRAG，RAG 将仅使用向量检索，构建 GraphRAG 后再次运行 Jobs 即可使用图检索。  
   - 明确「步骤 8」包含：向量 RAG 索引 + 可选 GraphRAG（若已构建）。

2. **What's New.MD**  
   - 若有描述「必须使用 GraphRAG」或「索引未构建则报错」，改为「推荐构建 GraphRAG；未构建时 workflow 降级为仅向量检索」。

**验收**：文档与「步骤 0.1」行为一致，无矛盾表述。

---

### 阶段 1：产品模块主配置与 unknown 后处理（不破坏现有流水线）

#### 步骤 1.1：新增产品模块主配置

**目标**：单源维护模块名、关键词、intent/scenario→module 映射；供 mineru 规则、后处理脚本、后续规则增强共用。

**修改**：

1. **新建** `INAGENT/doc_local/product_modules_registry.yaml`（或同名 JSON）：  
   - 结构示例：
     - `modules`: 列表或字典，每项含 `id`、`display_name`、`keywords`（列表，用于块内 + section_title/parent_section 匹配）。  
     - `intent_to_module`: 字典，intent → product_module。  
     - `scenario_id_to_module`: 字典，scenario_id → product_module。  
     - `generic_content_keywords`: 列表（目录、版权、关于我们等），命中则保留 unknown。  
   - 内容：将 UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY 中建议的模块（用户管理、高可用、SLB、资源监控/缓存、基础网络、安装环境等）及 intent/scenario 映射表写入。

2. **加载方式**：在 `auto_convert.py` 或公共配置模块中增加 `load_product_modules_registry()`，返回上述结构；若文件不存在则回退为 `{}`，不报错。

**验收**：  
- 存在该文件时，能正确解析；  
- 后续步骤可引用该配置。

---

#### 步骤 1.2：intent/scenario_id → product_module 后处理脚本

**目标**：对已有 kb 中 product_module=unknown 的块，用映射表覆盖，不重跑 LLM；写回 kb 或输出新文件。

**修改**：

1. **新建** `INAGENT/scripts/postprocess_unknown_product_module.py`（或放入 `INAGENT/scripts/` 下现有脚本）：  
   - 输入：`knowledge_base.json` 路径、产品模块主配置路径（或默认 doc_local/product_modules_registry.yaml）。  
   - 逻辑：  
     - 读取 kb（list 或 dict with chunks）；  
     - 遍历每个 chunk，若 `metadata.product_module == "unknown"`：  
       - 若 `intent` 在 intent_to_module 中，则覆盖 product_module；  
       - 否则若 `scenario_id` 在 scenario_id_to_module 中，则覆盖 product_module；  
       - 若块内容或 section_title 命中 generic_content_keywords，则不覆盖（保留 unknown）。  
     - 写回原文件或 `--output` 指定路径；打日志（修正条数、按模块分布）。

2. **依赖**：使用步骤 1.1 的 `product_modules_registry`；无则仅打印警告并跳过映射。

**验收**：  
- 对当前 doc_local/reference/knowledge_base.json 运行一次，unknown 中 intent/scenario 明确的块被覆盖；  
- 目录、版权等块仍为 unknown；  
- 不改变其他 metadata 与文本内容。

---

#### 步骤 1.3：将后处理接入数据库/流水线（可选）

**目标**：db update/rebuild 或 init 后，可选执行一次 unknown 后处理，再建索引与 GraphRAG。

**修改**：

1. **manage_database.py**  
   - 在 `action_create`、`action_update`、`action_rebuild` 中，在「合并 kb / 生成 kb」之后、「_build_index_from_kb 之前」增加可选步骤：若存在 `postprocess_unknown_product_module.py` 且配置或环境变量允许（如 `POSTPROCESS_UNKNOWN_MODULE=true`），则调用该脚本，对 REFERENCE_DIR 下的 knowledge_base.json 做一次后处理并写回。  
   - 或仅在文档中说明：用户可在 update/rebuild 后手动执行 `python INAGENT/scripts/postprocess_unknown_product_module.py`，再运行 GraphRAG build。

2. **run_inagent_pipeline.ps1**  
   - 保持现有逻辑；若后续希望「db update 后自动后处理」，再在 Invoke-DbAction 的 update/rebuild 分支中调用一次 postprocess 脚本（需与 manage_database 的职责划分一致，避免重复）。

**验收**：  
- 开启可选后处理时，kb 中 unknown 比例下降；  
- 未开启时行为与改造前一致。

---

### 阶段 2：auto_convert 增强（章节层级 + 规则扩展）

#### 步骤 2.1：MinerU content_list 预扫，写入 parent_section / section_title

**目标**：利用 MinerU 的 text_level 或编号标题，为每个 block 预填 parent_section、section_title（及可选 section_path），再进入规则与 LLM；LLM 仍可覆盖。

**修改**：

1. **auto_convert.py**  
   - 在「1. 预处理：收集所有有效的 Block 和基础元数据」阶段，不再只给 base_meta 以 `page_idx`、`type`；先对当前 task 的 content_list 做一次**顺序预扫**：  
     - 若 block 有 `text_level`，按 level 维护 section_stack（level → title）；  
     - 若无 text_level，用正则识别「编号标题」（如 `^\d+(\.\d+)*\.\s+.+`），推断层级并维护 section_stack；  
     - 对每个 block 计算当前节 title、父节 title，写入 base_meta：`parent_section`、`section_title`，可选 `section_path`。  
   - 预扫逻辑可抽成独立函数（如 `_build_section_stack_from_content_list(blocks) -> list of (parent_section, section_title) per block`），便于单测。

2. **与 LLM 的配合**  
   - 现有 LLM 提示中已要求输出 parent_section、section_title；保留「若 LLM 返回则采用，否则用预扫结果」的 fallback（当前已有类似逻辑，确保预扫结果作为 base_meta 传入即可）。

**验收**：  
- 对现有 app/content_list 跑一次，生成的 kb 中多数块具备非空 parent_section 或 section_title；  
- 与现有 LLM 输出不冲突（预扫仅作默认值）。

---

#### 步骤 2.2：规则增强中增加 section_title / parent_section 的关键词匹配

**目标**：规则层对 product_module 的判定不仅看块内 clean_text，也看 section_title、parent_section，减少「仅标题无正文」被标 unknown。

**修改**：

1. **auto_convert.py `_extract_chunk_metadata()`**  
   - 在「Rule-based Product Module Extraction」段：除 `lower_text = clean_text.lower()` 外，构造 `lower_section = (meta.get("section_title") or "") + " " + (meta.get("parent_section") or "")`，转为小写。  
   - 对 product_modules_map 的每个 module/keywords：若 `any(k.lower() in lower_text for k in keywords)` 或 `any(k.lower() in lower_section for k in keywords)`，则设置 `meta["product_module"] = module` 并 break。  
   - 注意：若 base_meta 已带 section_title/parent_section（步骤 2.1），需在调用 _extract_chunk_metadata 时传入或在 meta 中可用；若在 LLM 之后再做一次规则覆盖，需在 _apply_llm_metadata_extraction 之后的「规则覆盖」中同样使用 section_title/parent_section（当前是否有二次规则覆盖需看代码而定）。

2. **metadata_rules.product_modules 扩展**  
   - 从产品模块主配置（步骤 1.1）生成或手写扩展 mineru 的 product_modules，增加「用户管理、高可用、资源监控、缓存、基础网络、安装环境」等及对应关键词（与 UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY 一致）。  
   - 若 mineru 配置由脚本从主配置生成，在文档中说明生成方式与时机。

**验收**：  
- 仅含标题的块（如「31.2. 管理员设置和权限管理」）在规则层能命中「用户管理」等模块；  
- 与现有「块内文本」规则兼容，不破坏已正确标注的块。

---

#### 步骤 2.3：LLM 提示词收紧（unknown 使用条件）

**目标**：减少 LLM 随意返回 unknown；仅在通用内容时使用 unknown。

**修改**：

1. **auto_convert.py** 中 LLM 元数据提取的 prompt：  
   - 明确写：「只有在确认为目录、版权、关于我们、联系我们、商标声明、合格声明等通用内容时，才将 product_module 设为 unknown；否则必须从功能模块（含 Valid Product Modules）中择一，或根据章节标题语义推断最接近的模块。」  
   - Valid Product Modules 列表与主配置或 mineru 的 product_modules 一致（若主配置存在则优先从主配置生成该列表）。

**验收**：  
- 重跑少量块或抽样检查，unknown 比例下降；  
- 通用内容仍为 unknown。

---

### 阶段 3：function_structure_index 与 GraphRAG 一致性

#### 步骤 3.1：索引构建依赖 kb 且与主配置一致

**目标**：build_function_structure_index 从 kb 统计 product_modules 的逻辑不变；若存在产品模块主配置，不在索引中硬编码「模块白名单」，仅用 kb 中已有 product_module 统计；主配置用于规则与后处理，不强制限制索引中模块集合。

**修改**：

1. **build_function_structure_index.py**  
   - 保持从 kb 的 metadata 统计 product_modules、protocol_types、step_types；  
   - 若有「过滤 unknown 不参与统计」等逻辑，保持或按需微调；不引入对主配置的强依赖（避免无主配置时索引构建失败）。  
   - 若后续希望「索引中的模块列表与主配置合并」，可在此步增加：读取主配置的 modules 的 id/display_name，与 kb 统计结果合并去重，用于 scenarios 推断等；为可选增强。

**验收**：  
- 后处理或 auto_convert 更新 kb 后，重新运行 _build_index_from_kb（或 manage_database update/rebuild），function_structure_index 的 product_modules 与 kb 一致；  
- task_decomposition_agent 的「可用产品模块」包含新修正的模块。

---

#### 步骤 3.2：GraphRAG 输入与 workflow 使用

**目标**：确认 prepare_input_documents 仅读 kb；kb 中 product_module 修正后，重跑 init_graphrag --init --build 即可更新 GraphRAG；workflow 无需改代码。

**修改**：

1. **graphrag_adapter.py**  
   - `prepare_input_documents` 已从 kb 读 chunk，并将 `metadata.product_module` 等写入 documents 的 metadata 与文本前缀；无需改逻辑，仅确认：若 metadata 中无 product_module 则用 "unknown"；若为 "unknown" 仍写入，不丢弃。  
   - 确保写出的 documents.json 可被 GraphRAG 下游正常消费（已有逻辑保持不变即可）。

2. **workflow_config_generator.py / unified_rag.py**  
   - `_build_constraints_from_metadata` 已从检索结果的 metadata 汇总 product_modules、protocol_types；只要 kb 与检索结果中 metadata 正确，constraints 即正确。  
   - 无需改代码；在改造计划文档中注明：**修正 kb 的 product_module 后，需重跑 function_structure_index 构建与 GraphRAG init+build，workflow 即可使用新模块与更准的约束。**

**验收**：  
- 对修正后的 kb 执行 init_graphrag --init --build，GraphRAG 输出无报错；  
- 运行 Jobs，检索结果与 constraints 中包含修正后的模块。

---

### 阶段 4：启动脚本与流水线最终检查

#### 步骤 4.1：run_inagent_pipeline.ps1 与 manage_database 流程

**目标**：菜单 1（db status/create/update/rebuild/delete）行为明确且可成功执行。

**修改**：

1. **run_inagent_pipeline.ps1**  
   - 保持：db create/update/rebuild 成功后执行 `init_graphrag.py --init --build`；delete 时清理 GraphRAG output。  
   - 在注释或日志中说明：create 时若首次无 kb，会先执行初始化流程（auto_convert + initialize_rag_system）；若 GraphRAG 尚未构建，RAG 步骤将不报错（见步骤 0.1），随后本脚本会执行 GraphRAG init+build。

2. **manage_database.py**  
   - create：若 kb 不存在，继续调用 _run_initialize_pipeline()；若希望「先只生成 kb 再建 GraphRAG 再索引」，可拆成：先只跑 auto_convert（需在 auto_convert 或 manage_database 中提供「只跑 MinerU+元数据+合并、不跑 initialize_rag_system」的入口），再 _build_index_from_kb，再由 PS1 执行 GraphRAG。当前若采用步骤 0.1 的降级策略，可不拆，仅保证 initialize_rag_system 在 GraphRAG 不可用时返回 None 且不抛错。

**验收**：  
- 全新环境：Mode=db, DbAction=create → 成功生成 kb 与 function_structure_index，步骤 8 不报错；随后 PS1 执行 GraphRAG init+build 成功。  
- 之后 Mode=jobs 或菜单 5 可正常跑。

---

#### 步骤 4.2：README 与 What's New 最终同步

**目标**：文档中流程与阶段 0–4 一致，并注明可选后处理与主配置。

**修改**：

1. **README.md**  
   - 在「知识库构建流程」或「使用方式」中补充：  
     - 产品模块与 unknown 修正：见 doc_local/UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md 与 TRANSFORMATION_PLAN.md；  
     - 可选：运行 `scripts/postprocess_unknown_product_module.py` 对 kb 做 unknown→模块 后处理，再重建 function_structure_index 与 GraphRAG。  
   - 明确「步骤 8」为向量 RAG + 可选 GraphRAG（若已构建）。

2. **What's New.MD**  
   - 若有「RAG 检索能力优化」「GraphRAG 集成」等节，补充：GraphRAG 未构建时 workflow 降级为向量检索；产品模块可通过主配置与后处理脚本扩展与修正。

**验收**：  
- 读者按 README 操作可与实际脚本行为一致；  
- 无「必须先 build GraphRAG 否则 init 失败」的过时描述（在完成步骤 0.1 的前提下）。

---

## 四、每步需考虑与注意点汇总

| 阶段/步骤 | 需考虑点 | 可能影响 |
|-----------|----------|----------|
| 0.1 初始化与 GraphRAG | 是否允许「GraphRAG 未构建时仅用向量」；若允许，workflow 在无图时的行为与日志要清晰 | 首次部署、CI、用户环境无 GraphRAG 时 |
| 0.2 文档 | 中英文一致；避免「必须」「否则报错」与实现不符 | 用户预期与支持成本 |
| 1.1 主配置 | 文件格式 YAML/JSON、编码 UTF-8；缺失时各模块降级策略 | 未提供主配置的现有用户 |
| 1.2 后处理脚本 | 只改 product_module，不改其他字段；写回前备份或 --dry-run | kb 一致性、可回滚 |
| 1.3 接入流水线 | 后处理在「合并 kb 之后、建索引之前」执行；环境变量或配置开关 | 自动化与手动流程选择 |
| 2.1 MinerU 预扫 | content_list 结构因 MinerU 版本可能不同；正则与 text_level 兼容 | 不同 PDF/任务结构 |
| 2.2 规则 section 匹配 | 与现有「块内文本」规则顺序（先块内还是先 section）要统一，避免覆盖已有正确标注 | 准确率与召回 |
| 2.3 提示词 | 不要过度限制导致 LLM 不敢填新模块名；「择一」指尽量从列表选，非强制禁止新名 | 新模块发现与 unknown 平衡 |
| 3.1 索引 | 不依赖主配置存在；主配置仅作可选增强 | 无主配置环境 |
| 3.2 GraphRAG/workflow | 不改检索与分解核心逻辑，只保证数据源（kb）正确后重建索引 | 回归与稳定性 |
| 4.1 脚本 | Windows/PowerShell 路径与编码；python -u 与 PYTHONIOENCODING | 跨平台与日志 |
| 4.2 文档 | 中英文同步更新 | 维护成本 |

---

## 五、实施顺序建议（与 UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY 对齐）

1. **阶段 0**（步骤 0.1、0.2）：先做，保证启动脚本与文档不崩、与实现一致。  
2. **阶段 1**（步骤 1.1、1.2、1.3）：主配置与后处理脚本；可独立运行验证，不依赖 auto_convert 改动。  
3. **阶段 2**（步骤 2.1、2.2、2.3）：auto_convert 章节预扫与规则/提示词增强；可分批上线（先 2.1+2.2，再 2.3）。  
4. **阶段 3**（步骤 3.1、3.2）：确认索引与 GraphRAG 行为，无代码改动或仅小改动。  
5. **阶段 4**（步骤 4.1、4.2）：脚本与文档收尾。

完成上述步骤后，功能完整、启动脚本正常使用、auto_convert / GraphRAG / workflow 一体化改造可闭环；后续若增加「章节内 unknown 传播」或「模块定义表 section_title_patterns」等，可在本计划基础上按同一数据流扩展。
