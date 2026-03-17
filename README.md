

## INFOAGEN 项目完整流程

INFOAGEN 是一个基于 CAMEL-AI 框架的智能文档处理和自动化测试系统。以下是完整的处理流程：

### 知识库构建流程（步骤 1-8）

1. **PDF 文件识别和导入** (`auto_convert.py`)
   - 自动识别和导入 PDF 文档
   - 支持批量处理和并发处理

2. **MinerU 提取内容**
   - 使用 MinerU 工具从 PDF 中提取结构化内容
   - 生成知识块（knowledge blocks）

3. **LLM 提取 metadata** (`product_module`, `protocol_type`, `step_type`)
   - 使用大语言模型提取文档元数据
   - 识别产品模块、协议类型和步骤类型

4. **基于功能结构索引增强 metadata** (`scenario_id`, `step_type`)
   - 使用功能结构索引增强元数据
   - 识别场景 ID 和步骤类型

5. **自动识别文档模块和功能** (`auto_document_integration.py`)
   - 自动识别文档所属的产品模块
   - 识别文档的功能特性

6. **增量更新功能结构索引** (`incrementally_update_function_index`)
   - 增量更新功能结构索引
   - 支持新模块和功能的自动发现

7. **合并为 knowledge_base.json** (`merge_knowledge_base.py`)
   - 将所有知识块合并为统一的知识库
   - 生成结构化的 JSON 格式知识库

8. **RAG 索引** (`workflow_config_generator.py: initialize_rag_system`)
   - 将知识库索引到 GraphRAG 系统
   - 使用 Zhipu Batch API（免费 glm-4-flash）加速处理
   - 自动完成：提交 → 等待 → 下载 → 构建索引
   - GraphRAG 是唯一的 RAG 方案，必须构建索引后才能使用 Workflow

### Workflow 处理流程（步骤 9-17）

9. **Workflow 处理** (`workforce_config_ops.py: run_job -> _build_lb_context`)
   - 读取和处理测试任务
   - 构建负载均衡器上下文

10. **任务分解** (`task_decomposition_agent.py`)
    - 将复杂任务分解为可执行的子任务
    - 识别配置步骤和命令

11. **RAG 检索** (`workforce_config_ops.py: _retrieve_context`)
    - 基于任务需求进行 RAG 检索
    - 获取相关的配置文档和知识片段

12. **结果返回给 Agent**
    - 将检索结果和配置信息返回给 Agent
    - Agent 基于上下文生成配置

13. **Workflow 读入 jobs 下的测试任务**
    - 从 `jobs/` 目录读取测试任务文件（`.txt` 格式）
    - 批量处理多个测试任务

14. **根据测试任务分解步骤，确认每个步骤执行的命令**
    - 分析测试任务需求
    - 确定每个步骤需要执行的配置命令

15. **搭建测试环境，并准备测试使用的所有工具（toolkit）**
    - 自动搭建测试环境
    - 准备和配置测试所需的工具集

16. **根据步骤和环境构建设备配置，下发配置执行测试**
    - 根据测试步骤和环境信息生成设备配置
    - 下发配置到目标设备并执行测试

17. **检查测试结果，确认测试通过与不通过**
    - 收集测试执行结果
    - 分析测试结果并判断测试是否通过
    - 生成测试报告

### 使用方式

#### 初始化流程（步骤 1-8）

```bash
# 方式1: 使用 PowerShell 脚本
.\INAGENT\run_inagent_pipeline.ps1 -Mode init

# 方式2: 直接运行 Python 脚本
python INAGENT/initialize_pipeline.py
```

> **重要**：GraphRAG 是唯一的 RAG 方案，必须在执行 Workflow 前完成索引构建。
> 使用 Zhipu Batch API（免费 glm-4-flash）可大幅加速处理大规模文档。

#### Workflow 处理（步骤 9-17）

  ```bash
# 方式1: 使用 PowerShell 脚本
.\INAGENT\run_inagent_pipeline.ps1 -Mode jobs

# 方式2: 直接运行 Python 脚本
python INAGENT/workflow_config_generator.py --jobs-dir INAGENT/jobs --output-dir INAGENT/output
```

#### 交互式测试

   ```bash
# 交互式测试任务分解和配置生成
python INAGENT/test_task_decomposition.py
```

### 相关文件说明
 
- `INAGENT/auto_convert.py`: PDF 识别和导入，MinerU 内容提取
- `INAGENT/auto_document_integration.py`: 自动识别文档模块和功能
- `INAGENT/merge_knowledge_base.py`: 合并知识库
- `INAGENT/workflow_config_generator.py`: Workflow 处理和配置生成
- `INAGENT/agents/task_decomposition_agent.py`: 任务分解 Agent
- `INAGENT/initialize_pipeline.py`: 完整的初始化流程脚本
- `INAGENT/scripts/postprocess_unknown_product_module.py`: unknown 模块后处理脚本
- `INAGENT/doc_local/product_modules_registry.json`: 产品模块主配置（关键词/映射）

## 元数据提取和使用

### LLM 提取的元数据字段（13个）

系统使用大语言模型从文档中提取以下元数据字段。**核心元数据元素（P0）必须在LLM提取阶段就确定**，用于后续的功能树构建和数据块分类。

#### 核心元数据元素（P0 - 必须首先确定）⭐

这些元素在LLM提取阶段就必须确定，用于功能树构建和数据块分类：

| 字段名 | 数据类型 | 提取规则 | 用途 | 关键要求 |
|--------|---------|---------|------|---------|
| `product_module` | string | LLM根据数据块标题和内容自主识别 | 功能树节点、RAG过滤 | ✅ 必须是功能性的，不能是协议类型或章节编号 |
| `protocol_type` | list[string] | LLM提取所有提到的协议类型 | RAG过滤、约束条件 | ✅ 协议类型是约束条件，不是功能模块 |
| `step_type` | string | LLM根据内容推断步骤类型 | 步骤识别、配置顺序 | ✅ 使用通用名称，后续添加模块前缀 |
| `section_title` | string | 从`clean_text`提取，**去除章节编号** | 节点显示、层级关系 | ✅ **必须去除编号**（如"11.3.1. HTTP配置" -> "HTTP配置"） |
| `parent_section` | string | 从MinerU提取或推断，**去除章节编号** | 父子关系、树结构 | ✅ **必须去除编号**，只保留有意义的父章节名称 |
| `function_hierarchy` | string | LLM从`section_title`和`parent_section`构建 | 功能定位、分类决策 | ✅ 格式："SLB > Health Check > HTTP"（不包含编号） |

**数据块分类决策**：
- LLM需要判断数据块应该添加到新功能里面去，还是应该挂在树上某个功能下面
- 判断依据：`function_hierarchy`的层级数、`parent_section`的存在、`product_module`是否已存在

#### 辅助元数据元素（P1 - 推荐）

| 字段名 | 数据类型 | 用途 | 使用情况 |
|--------|---------|------|---------|
| `intent` | string | 配置意图（command_syntax/configuration_example等） | ✅ RAG约束条件 |
| `config_mode` | string | 配置模式（cli/console/api） | ✅ RAG约束条件 |
| `required_keywords` | list[string] | 重要技术术语 | ✅ RAG约束条件 |
| `command_prefix` | string | CLI命令前缀（slb/gslb等） | ⚠️ 未使用 |
| `command_structure` | object | CLI命令结构 | ⚠️ 未使用 |
| `description` | string | 文档块描述（max 50 words） | ⚠️ 部分使用 |

#### 增强元数据元素（P2 - 可选）

| 字段名 | 数据类型 | 提取方式 | 用途 |
|--------|---------|---------|------|
| `scenario_id` | string | LLM推断或增强阶段匹配 | 场景匹配、RAG过滤 |

### 元数据增强阶段

在LLM提取后，系统会基于功能结构索引（`function_structure_index.json`）进一步增强元数据：

- **`scenario_id`**: 如果LLM未识别，会通过匹配功能结构索引中的场景定义来补充
- **`step_type`**: 如果LLM未识别，会通过关键词匹配来补充

### 各模块对元数据的使用

#### 1. 任务分解Agent

**使用的字段**：
- `function_structure_index.scenarios[].required_steps` ⭐ 关键
- `function_structure_index.scenarios[].product_modules` ✅
- `function_structure_index.scenarios[].protocol_types` ✅
- `function_structure_index.step_types` ✅

**说明**：任务分解Agent主要使用功能结构索引，不直接使用chunk metadata。

#### 2. RAG检索

**使用的字段**：
- `product_module` ✅ 用于过滤
- `protocol_type` ✅ 用于过滤
- `scenario_id` ⚠️ 可用于过滤（优先级P1）
- `step_type` ⚠️ 可用于过滤（优先级P1）
- `config_mode` ✅ 提取约束条件
- `required_keywords` ✅ 提取约束条件
- `intent` ✅ 提取约束条件

**说明**：RAG检索使用metadata进行过滤和约束，提高检索准确性。

#### 3. LB Ops Agent

**使用的字段**：
- `context` ⭐ 主要输入（文本内容）
- `constraints.config_modes` ✅ 约束条件
- `constraints.required_keywords` ✅ 约束条件
- `constraints.intents` ✅ 约束条件

**说明**：LB Ops Agent通过constraints间接使用metadata字段。

### MinerU提取的字段（11个）

| 字段名 | 提取来源 | 用途 | 是否被LLM使用 |
|--------|---------|------|-------------|
| `source_pdf` | MinerU | 文档来源标识 | ❌ 未使用 |
| `source_file` | MinerU | 文档类型（cli/app） | ✅ 用于判断文档类型 |
| `page_idx` | MinerU | 页码定位 | ❌ 未使用 |
| `block_type` | MinerU | 块类型（text/image/table） | ✅ 用于判断是否提取section_title |
| `block_id` | MinerU | 块唯一标识 | ❌ 未使用 |
| `mineru_task_id` | MinerU | MinerU任务ID | ❌ 未使用 |
| `clean_text` | MinerU | 清理后的文本 | ✅ 用于提取section_title |
| `page_content` | MinerU | 页面内容 | ✅ LLM提取的主要输入 |
| `text` | MinerU | 文本内容 | ✅ LLM提取的主要输入 |
| `parent_section` | MinerU（可选） | 父章节标题 | ✅ 用于提取parent_section |
| `source_pdf_fingerprint` | 计算生成 | PDF指纹 | ❌ 未使用 |

### 元数据提取流程

1. **MinerU提取**：从PDF中提取基础字段和文本内容
2. **LLM提取**：使用大语言模型从文本中提取13个元数据字段
3. **增强阶段**：基于功能结构索引增强`scenario_id`和`step_type`
4. **存储**：元数据与知识块一起存储到`knowledge_base.json`
5. **索引**：元数据用于RAG索引和检索过滤

### 元数据问题修复与优化

#### 问题分析

在测试中发现，部分元数据字段（section_title、parent_section、scenario_id）在knowledge_base.json中缺失（0%覆盖率），但通过RAG检索时可以从嵌入的`INAGENT_META_JSON`格式中解析出metadata信息。

**核心结论**：section_title、parent_section、scenario_id缺失不是召回不准确的根本原因，但它们是导致无法进行精确过滤和匹配的关键因素。

**测试结果**（查询："如何配置HTTP类型的SLB服务"）：
- ✅ 内容召回相关性：100%（10/10）
- ✅ RAG检索metadata字段覆盖率：product_module 90%、protocol_type 70%、step_type 60%
- ✅ 配置生成成功率：100%（1个配置命令）
- ✅ 场景识别：32个场景（修复前为0个）
- ⚠️ 知识库中字段覆盖率：section_title 0%、parent_section 0%、scenario_id 0%（待强制重建数据库）

#### 已实施的修复

1. **元数据提取逻辑修复**（`auto_convert.py`）
   - 修复section_title提取：检查LLM返回是否为空字符串，使用fallback逻辑
   - 修复parent_section提取：检查LLM返回是否为空字符串，使用MinerU提供的值
   - 修复scenario_id提取：即使LLM未返回，也至少设置为"unknown"

2. **RAG检索metadata传递修复**（`workflow_config_generator.py`）
   - 修改`_adaptive_rag_retrieval`函数，支持从`INAGENT_META_JSON`格式解析metadata
   - 使用`return_detailed_info=True`获取详细信息
   - 从metadata中提取约束条件（product_modules、protocol_types）

3. **功能结构索引场景生成修复**（`build_function_structure_index.py`）
   - 修复场景生成逻辑，支持从knowledge_base.json自动生成场景
   - 场景数量从0增加到32个
   - 支持基于metadata统计的场景生成

#### 修复效果

**修复前**：
- 场景数量：0
- RAG metadata字段覆盖率：0%
- 配置生成成功率：0%

**修复后**：
- 场景数量：32个 ✅
- 功能模块结构（按基础到高级功能级别，带模块前缀）：
  ```
  LLB (基础层, order=1)
    支持的协议: DNS, FTP, HTTPS, ICMP, TCP
    ├─ llb_virtual_services (count=3, base=virtual_services, order=1)
    ├─ llb_network_basics (count=9, base=network_basics, order=1)
    ├─ llb_app_wizard (count=50, base=app_wizard, order=2)
    ├─ llb_policies_and_algorithms (count=21, base=policies_and_algorithms, order=2)
    └─ llb_health_checks (count=20, base=health_checks, order=3)
  
  基础网络 (安全层, order=3)
    依赖: 基础网络, SLB
    支持的协议: ARP, BGP, DNS, FTP, HTTP
    ├─ network_network_basics (count=292, base=network_basics, order=1)
    ├─ network_backend_servers (count=21, base=backend_servers, order=1)
    ├─ network_virtual_services (count=40, base=virtual_services, order=1)
    ├─ network_policies_and_algorithms (count=28, base=policies_and_algorithms, order=2)
    ├─ network_app_wizard (count=97, base=app_wizard, order=2)
    └─ network_health_checks (count=137, base=health_checks, order=3)
  
  SLB (安全层, order=3)
    依赖: 基础网络, SLB
    支持的协议: ARP, DNS, FTP, HTTP, HTTPS
    ├─ slb_virtual_services (count=116, base=virtual_services, order=1)
    ├─ slb_backend_servers (count=145, base=backend_servers, order=1)
    ├─ slb_network_basics (count=37, base=network_basics, order=1)
    ├─ slb_policies_and_algorithms (count=39, base=policies_and_algorithms, order=2)
    ├─ slb_app_wizard (count=218, base=app_wizard, order=2)
    └─ slb_health_checks (count=67, base=health_checks, order=3)
  
  安全 (安全层, order=3)
    依赖: 基础网络, SLB
    支持的协议: DNS, FTP, HTTP, HTTPS, ICMP
    ├─ security_virtual_services (count=12, base=virtual_services, order=1)
    ├─ security_network_basics (count=75, base=network_basics, order=1)
    ├─ security_backend_servers (count=18, base=backend_servers, order=1)
    ├─ security_policies_and_algorithms (count=17, base=policies_and_algorithms, order=2)
    ├─ security_app_wizard (count=85, base=app_wizard, order=2)
    └─ security_health_checks (count=25, base=health_checks, order=3)
  
  GSLB (其他, order=999)
    ├─ gslb_policies_and_algorithms (count=1, base=policies_and_algorithms, order=2)
    └─ gslb_health_checks (count=1, base=health_checks, order=3)
  ```
  ✅ 共5个功能模块，所有步骤类型都使用带模块前缀的格式（如`slb_health_checks`、`llb_health_checks`），可以区分不同模块的同名步骤类型
  
  **关键改进**：
  - ✅ 步骤类型使用带模块前缀的格式，可以区分不同模块的同名步骤（如`slb_health_checks` vs `llb_health_checks`）
  - ✅ 每个模块包含层级信息（level）、配置顺序（order）、依赖关系（depends_on）
  - ✅ 每个模块包含支持的协议类型列表（protocol_types）
  - ✅ 每个步骤类型包含基础步骤类型（base_step_type）、是否高级功能（is_advanced）、配置顺序（order）
- RAG metadata字段覆盖率：product_module 90%、protocol_type 70%、step_type 60% ✅
- 配置生成成功率：100% ✅
- 任务分解能正确识别scenario_id：SLB_HTTP_FULL_CONFIG ✅

#### 测试验证结果

**测试查询**："如何配置HTTP类型的SLB服务"

| 指标 | 修复前 | 修复后 | 改进 |
|------|--------|--------|------|
| 场景数量 | 0 | 32 | ✅ +32 |
| 功能模块结构 | - | 6个模块，按基础到高级功能级别组织（见下方树状结构） | ✅ |
| scenario_id识别 | unknown | SLB_HTTP_FULL_CONFIG | ✅ 正确识别 |
| RAG metadata字段覆盖率 | | | |
| - product_module | 0% | 90% | ✅ +90% |
| - protocol_type | 0% | 70% | ✅ +70% |
| - step_type | 0% | 60% | ✅ +60% |
| 配置生成成功率 | 0% | 100% | ✅ +100% |
| 配置命令数量 | 0 | 1 | ✅ |
| 内容召回相关性 | 100% | 100% | ✅ 保持 |

**关键改进**：
- ✅ 场景生成从0个增加到32个，任务分解能正确识别场景ID
- ✅ 功能模块按基础到高级功能级别组织，形成清晰的树状结构
- ✅ RAG检索metadata字段覆盖率显著提升（从0%提升到60-90%）
- ✅ 配置生成从失败到成功（生成1个配置命令和1个验证命令）
- ⚠️ 知识库中section_title、parent_section、scenario_id仍为0%（需要强制清除缓存后重新处理）

**功能模块层级说明**：
- **基础网络**：提供网络基础配置功能（network_basics最多，292个）
- **SLB**：服务器负载均衡，核心应用层功能（virtual_services、backend_servers最多）
- **GSLB**：全局服务器负载均衡，高级功能（仅health_checks和policies_and_algorithms）
- **LLB**：链路负载均衡，链路层功能
- **安全**：安全相关功能，覆盖所有步骤类型
- **unknown**：未分类功能，作为兜底模块

### 完整模块树结构设计

#### 模块树应该包含的元素

**顶层结构**：
- `metadata_statistics`: 元数据统计信息
- `cli_hierarchy`: CLI命令层级
- `document_hierarchy`: 文档结构
- `scenarios`: 场景定义
- `modules`: 模块树（核心）
- `step_types_registry`: 步骤类型注册表（推荐新增）
- `protocol_types_registry`: 协议类型注册表（推荐新增）

**父节点（模块）应该包含**：
1. **基本信息**
   - `module_name`: 模块名称
   - `level`: 层级（基础层/应用层/全局层/链路层/安全层）
   - `description`: 模块描述
   - `order`: 配置顺序（数字，越小越先配置）

2. **关系信息**
   - `depends_on`: 依赖的模块列表（如SLB依赖基础网络）
   - `supported_protocols`: 支持的协议类型列表

3. **功能信息**
   - `step_types`: 步骤类型字典（键为带模块前缀的步骤类型名，如`slb_health_checks`）
   - `advanced_features`: 高级功能字典（如cookie_persistence、qos）
   - `cli_commands`: CLI命令列表

4. **检索信息**
   - `keywords`: 关键词列表（用于RAG检索）

**子节点（步骤类型）应该包含**：
1. **标识信息**
   - 键名格式：`{module_name}_{base_step_type}`（如`slb_health_checks`）
   - `base_step_type`: 基础步骤类型名（如`health_checks`）
   - `module`: 所属模块

2. **统计信息**
   - `count`: 使用次数
   - `keywords`: 关键词列表

3. **配置信息**
   - `is_advanced`: 是否高级功能（true/false）
   - `order`: 在该模块内的配置顺序
   - `depends_on_steps`: 依赖的其他步骤类型（可选）

4. **协议支持**
   - `supported_protocols`: 支持的协议类型（可选）

#### 推荐的完整模块树结构

```
功能模块树
│
├─ 基础层 (level=1, order=1)
│  └─ 基础网络
│     ├─ 基础网络_network_basics (order=1, is_advanced=false)
│     ├─ 基础网络_routing_config (order=2, is_advanced=false)  # 推荐新增
│     └─ 基础网络_ha_config (order=3, is_advanced=false)        # 推荐新增
│
├─ 应用层 (level=2, order=2, depends_on=["基础网络"])
│  ├─ SLB
│  │  ├─ 基础步骤
│  │  │  ├─ slb_virtual_services (order=1, is_advanced=false)
│  │  │  ├─ slb_backend_servers (order=2, is_advanced=false)
│  │  │  └─ slb_health_checks (order=3, is_advanced=false)
│  │  └─ 高级功能 (is_advanced=true)
│  │     ├─ slb_cookie_persistence (order=4)
│  │     ├─ slb_qos (order=5)
│  │     ├─ slb_session_affinity (order=6)
│  │     └─ slb_load_balancing_algorithm (order=7)
│  │
│  ├─ LLB (depends_on=["基础网络"])
│  │  ├─ llb_virtual_services
│  │  ├─ llb_health_checks
│  │  └─ llb_load_balancing_algorithm
│  │
│  └─ GSLB (depends_on=["基础网络", "SLB"])
│     ├─ gslb_health_checks
│     └─ gslb_policies_and_algorithms
│
└─ 安全层 (level=3, order=3, depends_on=["基础网络", "SLB"])
   └─ 安全
      ├─ 安全_policies_and_algorithms
      ├─ 安全_access_control
      └─ 安全_authentication
```

#### 场景定义推荐结构

**当前问题**：
- `product_modules`包含协议类型（如`['SLB', 'HTTP', 'IP']`）
- `required_steps`没有模块前缀，无法区分模块归属

**推荐修复**：
```json
{
  "SLB_HTTP_FULL_CONFIG": {
    "scenario_id": "SLB_HTTP_FULL_CONFIG",
    "product_modules": ["SLB"],              // ✅ 只包含功能模块
    "protocol_types": ["HTTP"],              // ✅ 协议类型单独列出
    "required_steps": [                      // ✅ 使用带模块前缀的步骤类型
      "基础网络_network_basics",
      "slb_virtual_services",
      "slb_backend_servers",
      "slb_health_checks"
    ],
    "configuration_order": [                 // ✅ 明确的配置顺序
      "基础网络_network_basics",
      "slb_virtual_services",
      "slb_backend_servers",
      "slb_health_checks"
    ],
    "optional_advanced_features": [         // ✅ 可选的高级功能
      "slb_cookie_persistence",
      "slb_qos"
    ]
  }
}
```

#### 基于测试结果的发现

从`conversion.MD`测试结果可以看到：
1. **Agent输出问题**：`product_modules`包含了协议类型（HTTP、DNS、TCP等），应该只包含功能模块（SLB）
2. **步骤类型问题**：`step_type`是通用名称（如`network_basics`），无法区分是哪个模块的
3. **配置顺序问题**：Agent提问时考虑了层级顺序，但功能树结构没有支持这种层级查询

**推荐修复优先级**：
1. **P0**: 修复场景定义，过滤`product_modules`中的协议类型
2. **P1**: 为步骤类型添加模块前缀（如`slb_health_checks`）
3. **P2**: 引入层级结构（`level`、`order`、`depends_on`）
4. **P3**: 优化Agent提问逻辑，按层级顺序组织RAG查询

### 动态模块树生成与生成树式配置

#### 核心设计理念

**1. 完全动态识别功能模块**
- ❌ **当前问题**：功能模块（SLB、LLB等）在代码中硬编码，LLM被限制只能识别预设的模块
- ✅ **推荐方案**：功能模块应该完全由LLM根据数据块标题和内容自动判断并生成
- **实现方式**：
  - 移除LLM Prompt中的硬编码模块列表
  - 移除`valid_product_modules`的验证限制
  - 引导LLM基于章节标题、命令前缀、内容关键词自主识别功能模块

**2. 生成树式配置遍历**
- **概念**：SLB配置类似于生成树问题，需要遍历树找到所有基础配置，然后根据需求添加高级配置
- **配置顺序**：
  1. **基础层**：网络基础配置、路由配置、双机环境（如需要）
  2. **应用层**：业务功能的基础配置（SLB的virtual_services、backend_servers等）
  3. **高级功能层**：根据需求添加的高级功能（健康检查、会话保持、QoS、安全策略等）

**3. 动态模块树构建**
- **当前问题**：模块树基于预设的模块列表构建，无法动态发现新模块
- **推荐方案**：
  - 从`knowledge_base.json`动态统计所有`product_module`（不限制）
  - 基于章节标题、关键词、CLI命令前缀自动推断模块层级和依赖关系
  - 动态构建模块树，支持新模块自动发现

#### 推荐的模块树生成流程

```
1. LLM自主识别（auto_convert.py）
   └─ 根据数据块标题和内容识别功能模块
      └─ 输出：每个chunk的product_module（动态识别）

2. 动态统计（build_function_structure_index.py）
   └─ 从knowledge_base.json统计所有product_module
      └─ 输出：metadata_statistics（包含所有动态识别的模块）

3. 层级推断（build_function_structure_index.py）
   └─ 基于内容推断模块层级和依赖关系
      └─ 输出：module_hierarchy（level、order、depends_on）

4. 构建模块树（build_function_structure_index.py）
   └─ 基于统计结果和层级推断构建modules结构
      └─ 输出：function_structure_index.json（动态模块树）

5. 生成树遍历（workflow_config_generator.py）
   └─ 按"基础 → 应用 → 高级功能"顺序遍历配置步骤
      └─ 输出：有序的配置步骤列表
```

#### 关键修改点

**修改1: LLM Prompt（auto_convert.py）**
- 移除硬编码的模块名称列表
- 改为引导LLM基于内容自主识别
- 强调区分功能模块和协议类型

**修改2: 移除验证限制（auto_convert.py）**
- 移除`valid_product_modules`的硬编码验证
- 信任LLM的判断结果

**修改3: 动态统计（build_function_structure_index.py）**
- 动态收集所有product_module，不限制
- 统计每个模块的协议类型、章节标题等信息

**修改4: 层级推断（build_function_structure_index.py）**
- 基于内容自动推断模块层级和依赖关系
- 为每个模块添加`level`、`order`、`depends_on`字段

**修改5: 生成树遍历（workflow_config_generator.py）**
- 实现配置生成树遍历算法
- 按依赖关系和配置顺序组织步骤

### 核心元数据元素与功能树设计

#### 核心元数据元素（P0 - 必须首先确定）

这些元素在LLM提取阶段就必须确定，用于功能树构建和数据块分类：

1. **`product_module`**（功能模块）⭐
   - **规则**：必须是功能性的（SLB、LLB、基础网络等），不能是协议类型或章节编号
   - **用途**：功能树的主要节点、RAG过滤

2. **`protocol_type`**（协议类型）⭐
   - **规则**：必须是协议类型（HTTP、HTTPS、TCP等），作为约束条件，不是功能模块
   - **用途**：RAG过滤、场景定义的约束条件

3. **`step_type`**（步骤类型）⭐
   - **规则**：使用通用名称（如`health_checks`），后续添加模块前缀（如`slb_health_checks`）
   - **用途**：步骤识别、配置顺序

4. **`section_title`**（章节标题，去除编号）⭐
   - **规则**：**必须去除章节编号**（"11.3.1. HTTP配置" -> "HTTP配置"）
   - **用途**：功能树节点的显示名称、层级关系

5. **`parent_section`**（父章节，去除编号）⭐
   - **规则**：**必须去除章节编号**，只保留有意义的父章节名称
   - **用途**：建立功能树的父子关系

6. **`function_hierarchy`**（功能层级）⭐
   - **规则**：格式："SLB > Health Check > HTTP"（不包含编号）
   - **用途**：功能定位、数据块分类决策（新功能 vs 已有功能的子功能）

#### 数据块分类决策

LLM需要判断数据块应该添加到新功能里面去，还是应该挂在树上某个功能下面：

**判断依据**：
- `function_hierarchy`的层级数：多层通常是子功能
- `parent_section`的存在：有父章节通常是子功能
- `product_module`是否已存在：新出现的可能是新功能

**分类结果**：
- `new_module`：新功能模块
- `sub_function`：已有功能的子功能
- `step_type`：已有功能的步骤类型

#### 模块关联性和重复命名处理

**步骤类型命名规则**：
- 格式：`{module_name}_{base_step_type}`
- 示例：
  - SLB的健康检查：`slb_health_checks`
  - LLB的健康检查：`llb_health_checks`
  - HA的健康检查：`ha_health_checks`

**模块关联性表示**：
```json
{
  "modules": {
    "SLB": {
      "related_modules": ["基础网络", "安全"],  // 关联的模块
      "depends_on": ["基础网络"],              // 依赖的模块
      "step_types": {
        "slb_health_checks": {
          "related_step_types": ["llb_health_checks", "ha_health_checks"],  // 相关步骤类型
          "protocol_support": ["HTTP", "HTTPS", "TCP"]  // 协议支持
        }
      }
    }
  }
}
```

#### 功能树结构（不包含章节编号）

```
功能模块树
│
├─ 基础网络 (功能模块节点)
│  ├─ 网络基础配置 (功能节点，来自section_title去编号后)
│  │  └─ 基础网络_network_basics (步骤类型，带模块前缀)
│  └─ 路由配置 (功能节点)
│     └─ 基础网络_routing_config (步骤类型)
│
├─ SLB (功能模块节点)
│  ├─ 虚拟服务配置 (功能节点)
│  │  └─ slb_virtual_services (步骤类型)
│  └─ 健康检查配置 (功能节点)
│     └─ slb_health_checks (步骤类型，区分于llb_health_checks)
│
└─ LLB (功能模块节点)
   └─ 健康检查配置 (功能节点)
      └─ llb_health_checks (步骤类型，区分于slb_health_checks)
```

**关键点**：
- 功能树节点使用去除编号后的`section_title`
- 步骤类型使用带模块前缀的命名（如`slb_health_checks`）
- 协议类型作为约束条件，不体现在功能树节点中

#### 待优化项

1. **知识库字段覆盖率**：section_title、parent_section、scenario_id在knowledge_base.json中仍为0%
   - 原因：数据库重建时使用了缓存的PDF处理结果（"skip app.pdf, content unchanged"）
   - 解决方案：删除缓存文件后强制重新处理，或等待下次PDF文件更新时自动处理

2. **步骤类型匹配**：虽然metadata可以解析，但步骤类型匹配率仍为0%
   - 原因：需要实现基于metadata的RAG过滤逻辑
   - 解决方案：在`_adaptive_rag_retrieval`中添加基于step_type的过滤

<br>

