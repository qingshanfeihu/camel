# 误标为 unknown 的产品功能块总结与根因分析

## 一、数据概况

- **导出来源**：`doc_local/unknown_module_chunks_export.json`（来自 `knowledge_base.json` 中 `product_module=unknown` 的块）
- **unknown 块总数**：1141 条
- **其中**：既有合理为 unknown 的通用内容（目录、版权、关于我们等），也有**本应属于具体产品模块却被标成 unknown** 的文档块。

---

## 二、误标为 unknown 的产品功能（典型样本）

以下按「建议归属模块」分类，列出**典型误标样本**（section_title / clean_text 及现有 intent/scenario_id，便于对照）。

### 2.1 应属「用户管理 / user_management」

| 序号 | section_title / clean_text | 现有 intent/scenario_id | 说明 |
|-----|----------------------------|--------------------------|------|
| [4]  | 31.2. 管理员设置和权限管理 | intent=user_management, scenario_id=user_management | 标题和 intent 均明确为用户管理，product_module 仍为 unknown |
| [40] | 5.3. 用户访问级别 | - | 用户访问级别属用户管理 |
| [41] | 5.3.2. 权限级别 | intent=user_management | 权限级别属用户管理 |
| [44] | 最后⼀个访问级别为配置（Config）级别… | intent=user_management, scenario_id=user_config | 访问级别/Config 级别属用户管理 |
| [34] | 成功建立连接后，设备会提示管理员输入密码。系统默认用户名为 admin… | intent=user_management | 登录/默认账号属用户管理 |
| [38] | 按下回车，浏览器内出现欢迎界面，提示输入用户名和密码… | intent=user_management | WebUI 登录属用户管理 |
| [99] | RDP仅支持默认的组策略（default policy） | intent=user_management | 组策略与用户/策略管理相关 |

### 2.2 应属「高可用 / HA / high_availability」

| 序号 | section_title / clean_text | 现有 intent/scenario_id | 说明 |
|-----|----------------------------|--------------------------|------|
| [1]  | 11.2.20. 后台服务平稳关闭和温暖上线 | - | 服务优雅关闭/温暖上线属高可用或运维能力 |
| [61] | 注意：VRRP数据包在不同版本的设备之间不兼容… | intent=manage_server_group | VRRP/集群版本兼容属高可用 |
| [62] | 注意：通过以上配置，设备2将成为备份设备，因为它的优先级比设备1低 | intent=unknown, scenario_id=设备优先级管理 | 备份设备/优先级属高可用 |
| [71] | SSI功能与HA功能互斥，无法同时部署 | intent=high_availability, scenario_id=HA_FULL_FULL_CONFIG | 明确 HA，product_module 仍 unknown |
| [72] | Demo(config)#ha group enable 1 | intent=high_availability, scenario_id=HA_FULL_FULL_CONFIG | HA 命令，应属高可用 |
| [59] | 客户端与虚拟机的通信流程如下： | - | 若为 HA/虚拟化场景，应归高可用或对应模块 |

### 2.3 应属「SLB / 负载均衡」相关（含缓存、会话、策略等）

| 序号 | section_title / clean_text | 现有 intent/scenario_id | 说明 |
|-----|----------------------------|--------------------------|------|
| [2]  | 13.2.3. 缓存内容 | - | 缓存内容属 SLB/缓存功能 |
| [54] | 6.1.5. 网站分类 | intent=resource_monitoring, scenario_id=website_category | 网站分类常与 SLB/策略相关 |
| [57] | 许可证到期后，网站分类功能将不可用… | intent=resource_monitoring | 网站分类功能说明，应归具体功能模块 |
| [73] | 策略嵌套的限制 | - | 策略嵌套属 SLB/策略 |
| [74] | Persistence算法支持以下会话ID类型 | - | 会话保持属 SLB |
| [78] | 以下命令用于创建健康检查项目以及检查队列 | intent=resource_monitoring | 健康检查属 SLB |
| [79] | 下面的例子列举了HTTP请求和应答的输出结果 | protocol_type=HTTP, scenario_id=HTTP_EXAMPLE | HTTP 示例多属 SLB 配置/调试 |
| [83][84] | 透明模式的优点 / 透明模式的局限性 | - | 透明模式多属 SLB/网络 |
| [85] | 四种规则全部配置的情况下，任一规则匹配，则命中策略 | - | 规则/策略属 SLB |
| [86] | 6. 定义策略 | - | 定义策略属 SLB |
| [87][88] | Group1：轮询 / Group3：轮询 | intent=resource_monitoring | 轮询算法属 SLB |
| [92][93][94][95] | cookie 改写、后台服务器名称改写 | intent=resource_monitoring / protocol=HTTP | 内容改写/cookie 属 SLB |
| [96][97][98] | 将后台服务添加到服务组 / 建立后台服务 | intent=resource_monitoring | 后台服务/服务组属 SLB |
| [100] | 下面列出了配置策略嵌套配置所需的命令… | - | 策略嵌套属 SLB |
| [107] | 设备的监控功能（Monitor）会监控检测缓存的状态… | intent=resource_monitoring, scenario_id=monitor_cache_management | 缓存监控属 SLB/缓存 |
| [111] | 在缓存运行的过程中涉及到三种缓存失效时间 | intent=resource_monitoring, scenario_id=缓存失效时间管理 | 缓存失效时间属 SLB/缓存 |

### 2.4 应属「基础网络 / 网络配置 / 安装与设备环境」

| 序号 | section_title / clean_text | 现有 intent/scenario_id | 说明 |
|-----|----------------------------|--------------------------|------|
| [3]  | 24.1. 概述 | - | 需结合父章节判断；若为某功能概述则属该功能，否则可为基础网络/总览 |
| [15] | 1.2. 适用对象 | intent=network_config | 适用对象多为文档说明，可归基础或保留 unknown |
| [18] | 本章将介绍设备的安装环境、准备工作和安装步骤 | intent=network_config, scenario_id=installation_guide | 安装指导属基础/安装 |
| [19][20][21][22] | 安装设备之前请关闭电源… / 防雷保护 / 静电防护 / 设备安装环境要求 | intent=resource_monitoring 等 | 安装与机房环境应归基础网络或单独「安装/环境」 |
| [23][24][25][26] | 温度湿度及光照 / 空气质量 / 清洁度、灰尘 | intent=resource_monitoring, scenario_id=temperature_humidity_light_monitoring 等 | 机房环境监控属基础或资源监控 |
| [29] | 3.4. 安装过程 | intent=network_config, scenario_id=install_process | 安装过程属基础/安装 |
| [32] | 本章将介绍使用控制台连接、SSH连接和WebUI接口三种方式访问设备… | protocol=SSH 等 | 访问方式属基础网络/管理 |
| [63][64] | 6. 配置优先级 / 3. 配置优先级 | intent=network_config 或 resource_monitoring | 配置优先级多属基础网络或 SLB |
| [66] | 如果使用FFO Login方式登录域并进行启动时配置同步… | intent=log_analysis | 域登录/配置同步可归高可用或基础 |

### 2.5 应属「安全」或「资源监控」等

| 序号 | section_title / clean_text | 现有 intent/scenario_id | 说明 |
|-----|----------------------------|--------------------------|------|
| [12] | 信安世纪自主声明设备符合FCC规定第15条… | intent=resource_monitoring, scenario_id=FCC_15COMPLIANCE | 合规声明可归安全或资源监控 |
| [17] | 2.2. 产品优势 | - | 产品介绍，可保留 unknown 或归「概述」 |
| [60] | 下面是谨慎备份模式的工作机制 | - | 谨慎备份模式属高可用或备份功能 |

### 2.6 其他明显为产品功能但标成 unknown 的表述

- **附录 B: 附录二 XML RPC 方法**：若文档为 API/接口说明，应归对应模块（如基础网络/管理接口）。
- **流量编排**：如 [103][105]「在该场景中，流量编排功能支持」「流量编排部署指南」→ intent=app_deployment，应归具体业务/编排模块而非 unknown。
- **出向透明** [104]：12.2.3. 出向透明 → 多属 SLB/网络。
- **WebUI 相关** [36][37][39][48]：启动 WebUI、访问 WebUI、浏览器与分辨率 → 属基础/管理或控制台，不应为 unknown。
- **配置保存与重启** [49][50]：配置已保存、重新启动时运行、该功能默认为启用 → 属基础/配置管理。

---

## 三、根因分析

### 3.1 直接原因：LLM 将本可识别的功能块标成 unknown

- 提示词已要求「只有真正通用的内容（目录、版权等）才使用 unknown」，并给出了「从章节标题、命令前缀、内容关键词识别 product_module」的策略。
- 但在以下情况下，LLM 仍大量返回 `product_module: unknown`：
  1. **块内容过短**：仅章节编号+标题（如「11.2.20. 后台服务平稳关闭和温暖上线」「13.2.3. 缓存内容」），缺乏正文关键词。
  2. **标题与规则关键词不一致**：例如「管理员设置和权限管理」未命中「用户管理」的常见写法，或「缓存内容」「温暖上线」等未出现在当前 product_modules 的关键词列表中。
  3. **不确定时倾向保守**：模型在无法明确对应到某一条「Valid Product Modules」时，更倾向于选择 unknown，而不是推断为「用户管理」「高可用」「SLB」等。

### 3.2 规则增强无法补救：仅依赖「块内文本 × product_modules 关键词」

- **逻辑位置**：`auto_convert.py` 中规则增强（约 1669–1674 行）：
  - 对当前块的 `clean_text`（或 `lower_text`）做关键词匹配；
  - 仅当某模块的 **metadata_rules.product_modules** 关键词出现在**该块文本**中时，才用该模块覆盖 `product_module`。
- **局限**：
  1. **只认块内文本**：若块只有标题、无正文（如「31.2. 管理员设置和权限管理」），而「用户管理」「权限」等关键词在 mineru 的 product_modules 中不存在或未覆盖「管理员」「权限管理」等表述，则规则不会触发。
  2. **product_modules 覆盖不足**：当前 `doc_local/mineru_output/mineru.json` 的 `metadata_rules.product_modules` 仅包含：**SLB、GSLB、LLB、基础网络、安全**。缺少例如：
     - 用户管理（用户、权限、访问级别、管理员、admin）
     - 高可用（HA、VRRP、备份、集群、主备、温暖上线、平稳关闭）
     - 资源监控 / 监控（监控、缓存、健康检查、温度湿度、空气质量）
     - 缓存（缓存、cache、缓存内容、缓存失效）
     - 安装/设备环境（安装、机房、防雷、静电、温度湿度）
  因此即使块内出现「权限」「缓存」「HA」「VRRP」等，规则也无法将其映射到对应模块。

### 3.3 未利用已有结构化字段反推 product_module

- 不少块的 **intent**、**scenario_id**、**required_keywords** 已正确（如 user_management、high_availability、HA_FULL_FULL_CONFIG、monitor_cache_management），但：
  - 当前流水线**没有**在 LLM 与规则之后，根据 intent/scenario_id 做一次「若 product_module 为 unknown，则从 intent/scenario_id 映射到模块」的补救逻辑。
- 因此出现「intent=user_management / scenario_id=user_management 且 section_title=管理员设置和权限管理，但 product_module 仍为 unknown」的明显矛盾。

### 3.4 缺乏上下文推断

- 当前是按**单块**做元数据抽取与规则增强，没有：
  - 利用**父章节、兄弟块或文档层级**（如 parent_section、同一节下的其他块）来推断当前块所属模块；
  - 也没有「同一章节内若多数块已归属某模块，则将该章节下 unknown 块默认归入该模块」的传播策略。
- 导致例如「24.1. 概述」这类强依赖父章节才能判断模块的块，更容易被标成 unknown。

### 3.5 Valid Product Modules 在提示中仅为参考、非强制

- 提示中「Valid Product Modules」以列举+关键词描述形式给出，要求「根据内容自主识别，不要依赖预设列表」，但未强制「必须从下列模块中择一」。
- 模型在不确定时没有「尽量从列表中选最接近的一项」的硬约束，容易退回到 unknown。

---

## 四、根因归纳（简要）

| 层级 | 根因 |
|------|------|
| **现象** | 大量本属「用户管理、高可用、SLB（含缓存/策略/会话/健康检查）、基础网络、安装与环境、安全」等的块被标成 product_module=unknown。 |
| **直接原因** | LLM 在块内容短、标题与预设关键词不完全一致或不确定时，倾向返回 unknown。 |
| **规则未补救** | 规则增强仅做「块内文本 × metadata_rules.product_modules 关键词」匹配，且 product_modules 未包含用户管理、高可用、资源监控、缓存、安装环境等模块及其关键词。 |
| **未用已有信号** | 未用 intent / scenario_id / required_keywords 等已正确标注的字段对 product_module=unknown 做二次推断或覆盖。 |
| **缺少上下文** | 未利用父章节/同节其他块/文档结构做模块传播或默认归属。 |
| **提示约束不足** | Valid Product Modules 为参考而非强制择一，模型易在不确定时选 unknown。 |

---

## 五、完整修改方案

以下从「模块生命周期与问题修正」「MinerU 与章节上下文」「归属判断」「GraphRAG/workflow 融入」「其他重点」五方面给出可落地的修改方案。

---

### 5.1 功能模块生命周期与问题发现后的补充/修改

**现状**：产品模块来源有两路——(1) **mineru.json 的 metadata_rules.product_modules**（规则关键词，用于规则增强）；(2) **LLM 自动生成**（可返回不在列表中的模块名，代码已接受）。`build_function_structure_index` 从 `knowledge_base.json` 的 metadata 聚合出 `metadata_statistics.product_modules`，再供 task_decomposition_agent 的「可用产品模块」使用。发现问题时，目前没有统一流程：要么改 mineru.json 后全量重跑 MinerU+auto_convert，要么手动改 kb。

**目标**：发现问题时能**补充新模块**、**修正误标**，且修改有单源、可复现、可回归。

| 环节 | 建议 |
|------|------|
| **模块定义单源** | 新增/维护一份 **产品模块主配置**（如 `doc_local/product_modules_registry.yaml` 或合并进 mineru 的 metadata_rules），包含：`module_id`、`display_name`、`keywords`（块内文本+章节标题用）、可选 `section_title_patterns`、可选 `intent_scenario_mapping`（intent/scenario_id → 本模块）。mineru.json 的 product_modules 在构建/部署时从该配置生成或直接引用，避免两处不一致。 |
| **发现新模块时** | 1）在「产品模块主配置」中新增条目（名称+关键词+可选 section 模式）；2）若需规则先于 LLM 命中，则更新 mineru 所用 product_modules（或从主配置生成）；3）对**已有 kb**：可只跑**后处理脚本**（见下）用新关键词/映射表覆盖 unknown，不必全量重跑 LLM；4）重跑 `build_function_structure_index`，使「可用产品模块」和场景推断包含新模块。 |
| **修正误标（unknown→某模块）时** | 1）优先用 **intent/scenario_id → product_module 映射表** 做后处理（见 5.3）；2）不足时再扩展 **product_modules 关键词** 或 **section_title 匹配**，必要时扩展主配置；3）对已有 kb：运行后处理脚本，只重算 product_module（及依赖字段），无需重跑 MinerU；4）重跑 `build_function_structure_index`。 |
| **人工审核清单** | 定期导出 `product_module=unknown` 的块（如现有 `export_unknown_module_chunks.py`），产出「待审核列表」：标注应为某模块 / 确认为通用内容。审核结果可反哺：新增/调整主配置中的模块与关键词、或加入 intent→module 映射表。 |

---

### 5.2 是否充分利用 MinerU 原始数据与章节/上下文

**现状**：MinerU 的 content_list 每块有 `type`、`text`、`text_level`（部分）、`page_idx`、`bbox` 等，**没有**直接给出 parent_section。当前 auto_convert 只把 `page_idx`、`type` 等写入 base_meta，**未用 text_level 与编号标题构建章节层级**，parent_section/section_title 完全依赖 LLM 或从 clean_text 正则抽取，导致章节边界与父子关系不清晰，上下文未被系统化利用。

**目标**：用 MinerU 的**顺序 + text_level + 编号标题**显式构建章节树，并在规则与传播中利用。

| 环节 | 建议 |
|------|------|
| **从 MinerU content_list 构建章节栈** | 在 auto_convert 中，在调用 `_extract_chunk_metadata` 之前增加**预扫**：遍历 content_list，根据 `text_level`（若有）或「编号标题」正则（如 `^\d+(\.\d+)*\.\s+.+`）维护 section_stack（level → title），为每个 block 预先计算 `parent_section`、`section_title`（当前节）、可选 `section_path`（根到当前）。将这三项写入 base_meta，再进入规则+LLM；LLM 仍可覆盖，但有了可靠默认值。 |
| **规则增强使用章节** | 规则层（`_extract_chunk_metadata` 及 LLM 后的规则覆盖）中，**除块内 clean_text 外**，对 `section_title`、`parent_section`（以及 section_path 若存在）做同样的 product_modules 关键词匹配；任一处命中即允许覆盖 product_module。这样「仅标题无正文」的块也能被规则打上模块。 |
| **章节内 unknown 传播（可选）** | 在**后处理阶段**（见 5.3）：按 (source_file, parent_section) 或 (source_file, section_path) 分组，若某节内非 unknown 块中**多数**属于同一模块 M，则将该节内 product_module=unknown 的块覆盖为 M；可设阈值（如 ≥50% 且至少 2 块）并排除「通用内容」关键词（目录、版权等），避免误传播。 |
| **与 build_function_structure_index 一致** | 若文档层级从 MinerU 预扫得到，build 阶段可复用同一逻辑或同一 section_path 信息，用于场景推断（按章节聚合模块/协议）时更一致。 |

---

### 5.3 已明确功能模块的归属判断（如何判断某块属于某模块）

**目标**：在「规则 + LLM + 后处理」三层中，明确「属于某模块」的判定方式，并统一扩展点。

| 层级 | 判断方式 | 建议 |
|------|----------|------|
| **规则层（先于 LLM）** | 块内文本 + **section_title / parent_section** 与 product_modules 关键词匹配 | 扩展 metadata_rules.product_modules（或主配置）为多模块+多关键词；规则增强时对 `clean_text`、`section_title`、`parent_section` 做 `any(k in lower_text_or_section for k in keywords)`，先匹配先得。 |
| **LLM 层** | 提示中要求「必须从功能模块中择一，仅当确认为目录/版权/关于我们等通用内容时才填 unknown」；Valid Product Modules 从主配置或 mineru 生成，并增加「若仅见章节标题，根据标题语义推断模块」的说明。 | 提示词收紧；可选在 response 校验中若为 unknown 则要求 description 中说明理由，便于后续分析。 |
| **后处理层（LLM 之后）** | 1）**intent / scenario_id → product_module 映射表**；2）章节内多数块归属传播；3）可选：模块定义表中的 section_title_patterns（正则）匹配。 | 见下。 |

**intent / scenario_id → product_module 映射表（后处理必做）**  
在配置中维护一张表，例如：

```yaml
# 示例：product_module_from_intent_scenario（可放在主配置或 mineru 扩展）
intent_to_module:
  user_management: 用户管理
  high_availability: 高可用
  manage_server_group: 高可用
  network_config: 基础网络
  resource_monitoring: SLB   # 或单列「资源监控」模块
  log_analysis: 基础网络
  app_deployment: SLB
scenario_id_to_module:
  user_management: 用户管理
  user_config: 用户管理
  HA_FULL_FULL_CONFIG: 高可用
  HTTP_SLB_CONFIG: SLB
  HTTP_EXAMPLE: SLB
  monitor_cache_management: SLB
  installation_guide: 基础网络
  install_process: 基础网络
  website_category: SLB
  # ...
```

后处理逻辑：对每条 chunk，若 `product_module == "unknown"` 且 (`intent` 或 `scenario_id`) 在映射表中，则用映射结果覆盖 `product_module`。这样无需改 LLM 即可修复大量误标。

**「属于某模块」的扩展点**：新增模块或新 intent/scenario 时，只需更新主配置中的 keywords 与 intent_scenario_mapping，再跑后处理或重跑 pipeline 即可。

---

### 5.4 新方案融入 GraphRAG 检索与 workflow 流程

**现状**：  
- **GraphRAG**：`graphrag_adapter.prepare_input_documents` 从 kb 读 chunk，用 `metadata.product_module` 等写入口文档的 meta 前缀和 metadata；检索时依赖这些元数据在文本中的呈现与 GraphRAG 内部索引。  
- **Workflow**：`workflow_config_generator` 从检索结果 metadata 汇总 `constraints["product_modules"]`；`task_decomposition_agent` 从 `function_structure_index` 的 `metadata_statistics.product_modules` 与 `modules` 得到「可用产品模块」列表，约束 LLM 只从该列表选；`workforce_config_ops` 用 decomposition 的 `product_modules` 做查询增强与过滤。

**结论**：只要 **kb 中的 product_module 正确**，且 **function_structure_index 已更新**，GraphRAG 与 workflow 会自然使用新模块，无需改检索或 decomposition 的核心逻辑。融入方式如下。

| 步骤 | 操作 |
|------|------|
| 1. 更新模块定义与规则 | 维护产品模块主配置；mineru 的 product_modules 与之同步或由主配置生成。 |
| 2. 提升 kb 中 product_module 质量 | 方案 A：重跑 MinerU + auto_convert（含 5.2 的章节预扫 + 5.3 的规则扩展 + 后处理映射表）。方案 B：不重跑 LLM，仅运行**后处理脚本**（见下）对已有 kb 做 unknown→模块 覆盖 + 可选章节传播。 |
| 3. 重建 function_structure_index | 运行 `build_function_structure_index`（输入 kb），使 `metadata_statistics.product_modules` 与各模块的 keywords/step_types 等与最新 kb 一致；task_decomposition_agent 的「可用产品模块」即更新。 |
| 4. GraphRAG | 若 kb 已更新，重新执行 `prepare_input_documents`（或整体 `initialize_graphrag_index`），必要时重新 build GraphRAG 索引，使入口文档与索引中的模块标签一致。 |
| 5. Workflow | 无需改代码；检索结果中的 metadata 已带正确 product_module，constraints 自然包含新模块；decomposition 使用的「可用产品模块」来自步骤 3。 |

**后处理脚本（推荐实现）**：  
- 输入：`knowledge_base.json`、产品模块主配置（含 intent/scenario 映射表）、可选「章节传播」开关。  
- 逻辑：遍历每个 chunk；若 `product_module == "unknown"`，则 (1) 用 intent/scenario_id 映射表覆盖；(2) 用扩展后的 product_modules 关键词匹配 section_title/parent_section/clean_text 覆盖；(3) 可选按节做多数块传播。  
- 输出：写回 `knowledge_base.json` 或输出新文件，并打日志（修正条数、按模块分布）。这样在发现误标或新增模块后，可只跑该脚本 + build_function_structure_index + 可选 GraphRAG 重建，无需全量重跑 LLM。

---

### 5.5 其他重点

| 项 | 说明 |
|----|------|
| **单源与复用** | 产品模块名称与关键词、intent/scenario→module 映射、通用内容黑名单（目录、版权等）集中在一份主配置中；mineru 规则、后处理脚本、build_function_structure_index 的「模块列表」均从中读取或生成，避免多处改漏。 |
| **评估与回归** | 1）统计 kb 中 `product_module=unknown` 占比，作为回归指标；2）对 unknown 做抽样人工标注「应为某模块 / 确认为通用」，计算误标率或与 intent 一致性；3）CI 中可选：跑后处理脚本 + build 后检查 unknown 占比是否低于阈值。 |
| **通用内容保留 unknown** | 后处理与章节传播中，若块内容或 section_title 命中「目录、版权、关于我们、联系我们、商标声明、合格声明」等，应保留 unknown，不覆盖为功能模块。 |
| **新模块的 LLM 接受度** | 当前代码已接受 LLM 返回的未在 valid_product_modules 中的模块名；若主配置新增模块但 mineru 的 Valid Product Modules 未同步，LLM 仍可能返回该名并被接受。为减少 unknown，建议 Valid Product Modules 与主配置同步更新，便于 LLM 直接选择。 |

---

### 5.6 实施顺序建议

1. **配置与数据**：新增产品模块主配置，补全「用户管理、高可用、资源监控/缓存、基础网络、安装环境」等模块及关键词；加入 intent/scenario_id→product_module 映射表。  
2. **auto_convert**：实现 MinerU 章节预扫，写入 parent_section/section_title；规则增强中增加对 section_title、parent_section 的关键词匹配。  
3. **后处理脚本**：实现「unknown → 映射表 + 关键词 + 可选章节传播」，支持只更新 kb 不重跑 LLM。  
4. **提示词**：收紧 unknown 使用条件，Valid Product Modules 与主配置一致。  
5. **流水线**：按需重跑后处理 → build_function_structure_index → GraphRAG prepare/build；workflow 无需改代码即可使用新模块与更准的 product_module。

以上为误标为 unknown 的产品功能整理、根因总结与完整修改方案，便于按步骤落地并与 GraphRAG/workflow 一体化使用。
