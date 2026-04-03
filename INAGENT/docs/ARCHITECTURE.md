# INAGENT 系统架构设计文档

> 版本: v10.1 | 更新: 2026-04-02

## 1. 系统概述

INAGENT 是基于 [CAMEL-AI](https://github.com/camel-ai/camel) 框架的**智能网络设备自动化测试系统**。它将 LLM 推理、RAG 知识检索、GraphRAG 知识图谱和多 Agent 协作有机结合，对 NSAE (InfosecOS) 网络负载均衡器设备执行端到端自动化测试。

**核心能力**:
- 自然语言测试任务 → 自动生成设备配置命令
- SSH 自动下发配置 + show 命令验证
- HTTP 端到端流量探测 + 故障注入/恢复
- LLM 驱动的测试结果分析 (Verdict: PASS/FAIL)
- 多 Agent 协作测试用例评审 (Bug-to-Case)
- 全流程 Markdown 报告生成

## 2. 整体架构

系统包含两条主要流水线：

**Pipeline A: 端到端测试执行** (Stage 1-9)
```
┌──────────────────────────────────────────────────┐
│                   用户入口                        │
│  run_inagent_pipeline.ps1 / .bat                 │
│  pipeline_runner.py (CLI)                        │
└──────────────┬───────────────────────────────────┘
               │
       ┌───────▼───────┐     ┌─────────────────┐
       │  LLM Gateway  │◄───►│  LLM 后端       │
       │  :9000/v1     │     │  (SiliconFlow/   │
       └───────┬───────┘     │   Qwen/OpenAI)   │
               │              └─────────────────┘
       ┌───────▼────────────────────────────────────────┐
       │              pipeline_runner.py                  │
       │                                                  │
       │  Stage 1: 测试计划 ──► Test Plan Agent + RAG    │
       │  Stage 2: 环境规划 ──► Env Setup Agent          │
       │  Stage 3: 配置生成 ──► RAG + LB Ops Agent       │
       │  Stage 3.5: VIP 冲突预检                        │
       │  Stage 4: VM 部署 ──► VMController (SSH)        │
       │                                                  │
       │  Stage 5-9: ═══► workforce_pipeline.py ═══►     │
       └──────────────────────┬─────────────────────────┘
                              │
       ┌──────────────────────▼─────────────────────────┐
       │          CAMEL Workforce Pipeline               │
       │                                                  │
       │  Deploy ──► fork ─┬── VerifyShow ──┐            │
       │                   └── Traffic ─────┘            │
       │                          │                       │
       │                   join → Analysis ──► Cleanup    │
       └──────────────────────────────────────────────────┘
               │              │              │
       ┌───────▼───┐  ┌──────▼────┐  ┌──────▼────┐
       │ NSAE 设备  │  │  测试 VM   │  │ Knowledge │
       │ SSH :22    │  │  SSH :22   │  │ Base (RAG)│
       └───────────┘  └───────────┘  └───────────┘
```

**Pipeline B: 测试用例评审** (Bug-to-Case, v0.6.0+, v0.8.0 架构增强)
```
┌─ prepare_bug_to_case.py ─┐
│ Excel + Bug详情 解析      │──► bug_to_case.json
└──────────┬───────────────┘
           ▼
┌─ run_review.py (Manager) ─┐
│ 全局审计 → 模块分流        │──► skip / light / full
│ (tech features per module) │     ← CLI graph tech profiles
└──────────┬────────────────┘
           ▼
┌─ ReviewInputBuilder ──────┐    ┌── Knowledge Base ──┐
│ 构建 ReviewInput:          │◄──►│ UnifiedRAG         │
│ rules, knowledge, CLI ref  │    │ TestRulesEngine    │
└──────────┬────────────────┘    └────────────────────┘
           ▼
┌─ ReviewPipeline (Workforce, share_memory=True) ─────┐
│                                                       │
│  ┌───────────────────────────┐                       │
│  │ ProductArchitectureMemory │  (VectorDB: 101模块画像) │
│  │ ProductSkillToolkit (4)   │  (主动查询产品架构)     │
│  │ KnowledgeToolkit (3)      │  (RAG检索/规则查询)     │
│  │ NoteTakingToolkit (4)     │  (共享笔记读写)         │
│  └──────────┬────────────────┘                       │
│             ▼                                         │
│  fork ─┬── CoverageWorker [11 tools + memory] ──┐   │
│        ├── CLISyntaxWorker [5 tools] ────────────┤   │
│        ├── SpecWorker [5 tools] ─────────────────┤   │
│        └── LoadStressWorker [5 tools] ───────────┘   │
│                    │                                   │
│             join → SynthesisWorker [6 tools]          │
└─────────────┬─────────────────────────────────────────┘
              ▼
       ReviewJudge (LLM-as-Judge 自动评分)
              ▼
       review_*.md (per module)
```

## 3. 流水线阶段详解

### 3.1 Stage 1-4: 准备阶段 (pipeline_runner.py)

| Stage | 名称 | 入口函数 | 核心依赖 |
|-------|------|---------|---------|
| 1 | 测试计划生成 | `generate_test_plan()` | Test Plan Agent + RAG |
| 2 | 网络环境规划 | `build_env_setup_agent()` | Env Setup Agent |
| 3 | 配置生成 | `process_job()` | workflow_config_generator + RAG + LB Ops Agent |
| 3.5 | VIP 冲突预检 | 内联逻辑 | ARP 探测 |
| 4 | VM 环境部署 | `VMController` | SSH + HTTP 服务部署 |

**数据流**: 测试任务文本 → RAG 检索上下文 → LLM 生成配置命令 + 验证命令 → 环境规划字典

### 3.2 Stage 5-9: CAMEL Workforce Pipeline (workforce_pipeline.py)

Stage 5-9 由 CAMEL Workforce 框架编排，5 个 Worker 各持专用 Toolkit：

| Worker | Stage | 职责 | Toolkit | 关键工具 |
|--------|-------|------|---------|---------|
| **DeployWorker** | 5 | 配置命令下发 + 即时验证 | NSAEDeviceToolkit | `execute_config_commands`, `execute_show_commands` |
| **VerifyShowWorker** | 7 | SSH show 命令验证配置生效 | NSAEDeviceToolkit | `execute_show_commands`, `get_module_config` |
| **TrafficWorker** | 6+6.5 | HTTP 流量探测 + 故障注入 | TrafficVerifyToolkit | `verify_vip_traffic`, `run_fault_injection_step` |
| **AnalysisWorker** | 8 | 综合分析判定 PASS/FAIL | — (纯 LLM) | Verdict 推理 |
| **CleanupWorker** | 9 | 撤销配置 + 停止服务 | NSAEDevice + VMController | `execute_config_commands`, `stop_http_server` |

**Pipeline 拓扑**:
```
stage5_deploy ──► fork ─┬── stage7_verify  ──┐
                        └── stage6_traffic ──┘
                               │
                        join → stage8_analysis ──► stage9_cleanup
```

- Deploy 完成后，VerifyShow 和 Traffic **并行**执行
- 两者均完成后 join 到 Analysis 进行综合判定
- 最后 Cleanup 清理环境

### 3.3 Coordinator 质量控制

Workforce 由 **Coordinator Agent** 监督 Worker 质量：
- Deploy 无 error
- VerifyShow 提供配置生效的正面证据
- Traffic HTTP 探测成功率合理
- Analysis 给出有理有据的 Verdict

不合格输出会触发 retry，Worker 需补充更多数据。

## 4. 模块结构

### 4.1 Agent 模块 (`agents/`)

| 文件 | 用途 | 使用阶段 |
|------|------|---------|
| `test_plan_agent.py` | 测试计划生成 | Stage 1 |
| `env_setup_agent.py` | 网络环境规划 | Stage 2 |
| `task_decomposition_agent.py` | 测试需求分解 + RAG 查询 | Stage 3 |
| `lb_ops_agent.py` | NSAE CLI 命令生成 | Stage 3 |
| `task_analysis_agent.py` | 测试结果分析 (legacy) | Stage 8 (deprecated) |
| `cleanup_agent.py` | 环境清理 (legacy) | Stage 9 (deprecated) |

> **注意**: `task_analysis_agent.py` 和 `cleanup_agent.py` 在 Workforce 模式下已被 AnalysisWorker 和 CleanupWorker 替代，保留作为回退安全网。

### 4.2 Toolkit 模块 (`toolkits/`)

为 CAMEL Workforce Worker 提供的工具集，每个 Toolkit 封装一类操作的多个工具函数。

| Toolkit | 工具数 | 说明 |
|---------|-------|------|
| `NSAEDeviceToolkit` | 4 | SSH 连接 NSAE 设备：执行配置命令、show 命令、获取模块配置、获取运行状态 |
| `VMControllerToolkit` | 9 | SSH 控制测试 VM：启停 HTTP 服务、修改页面内容、配置 IP、防火墙规则等 |
| `TrafficVerifyToolkit` | 4 | HTTP 流量验证：VIP 探测、故障注入执行、健康收敛等待、设备探针状态查询 |
| `KnowledgeToolkit` | 3 | 知识库工具：`search_product_knowledge`, `get_review_rules`, `search_similar_tests` |
| `ProductSkillToolkit` | 4 | **v0.8.0 新增** 产品架构技能：`query_module_tech_profile`, `discover_cross_cutting_concerns`, `check_spec_constraints`, `query_module_relationships` |

所有 Toolkit 共享同一组 SSH/VM 连接（P3 优化），避免重复建连。

### 4.3 RAG 模块 (`rag/`)

| 文件 | 说明 |
|------|------|
| `unified_rag.py` | 统一 RAG 检索器：向量检索 + GraphRAG + Rerank + 协议加权 |
| `knowledge_router.py` | 知识路由器：根据 mode 白名单选择检索路径 (HybridFusion → UnifiedRAG → Fallback) |
| `knowledge_config.py` | 文档分类体系 + mode 白名单定义 (config/test_write/test_review/explain) |
| `knowledge_schema.py` | 知识实体数据模型 (`KnowledgeEntity`, `KnowledgeNodeType`) |
| `hybrid_knowledge_fusion.py` | 跨存储融合：UnifiedRAG 主召回 + Neo4j 关系补充 + EntityLink 对齐 |
| `entity_link_store.py` | SQLite 跨存储实体对齐索引 (entity_id → graphrag/qdrant/neo4j IDs) |
| `neo4j_store.py` | Neo4j 图数据库封装 (知识实体/关系的存取和 Cypher 搜索) |
| `graphrag_integration.py` | GraphRAG 知识图谱检索集成 |
| `graphrag_adapter.py` | GraphRAG 适配器（实体提取/配置生成） |
| `fallback_retrieval.py` | RAG 回退检索逻辑 |
| `rerank_retriever.py` | SiliconFlow / LLM Gateway Rerank 检索器 (bge-reranker-v2-m3) |
| `scoring_utils.py` | 协议加权评分 |
| `test_rules.py` | 评审规则引擎 (TestRulesEngine) |
| `cli_reference.py` | CLI 参考文档检索 |
| `cli_graph_store.py` | **v0.7.0+** CLI 关键字图谱：5505节点、101模块，BFS子图提取 + 技术画像输出 |

### 4.4 工具模块 (`utils/` + `config/`)

| 文件 | 说明 |
|------|------|
| `config/project_config.py` | 项目级 YAML 配置加载器 (env var > project.yaml > default 三级优先) |
| `ssh_client.py` | NSAE 设备 SSH 客户端 (paramiko)，支持 `create_ssh_client_from_env()` 工厂方法 |
| `vm_controller.py` | 测试 VM SSH 控制器，支持 `VMController.from_env()` 工厂方法 |
| `env_utils.py` | `.env` 文件和环境变量加载 |
| `llm_config.py` | LLM 配置管理 |
| `index_utils.py` | 功能结构索引 (function_structure_index.json) 加载 |
| `workflow_logger.py` | Workflow 日志工具 |

### 4.5 数据处理 (`data_tools/`)

| 文件 | 说明 |
|------|------|
| `auto_convert.py` | PDF → JSON 文档转换 (MinerU) |
| `auto_document_integration.py` | 自动文档集成 (LLM metadata 提取) |
| `build_function_structure_index.py` | 功能结构索引构建 |
| `merge_knowledge_base.py` | 知识库合并为 knowledge_base.json |

### 4.6 评审模块 (`review/`) — v0.6.0+, v0.8.0 架构增强, v0.9.0 Self-Refine

| 文件 | 说明 |
|------|------|
| `pipeline.py` | ReviewPipeline: 5-Worker Workforce 协作评审，agents 装备 tools+memory，share_memory=True |
| `input_builder.py` | ReviewInputBuilder: 构建结构化 ReviewInput (rules, knowledge, CLI ref, similar tests) |
| `review_schema.py` | 结构化评审数据模型 (`CaseIssue`, `CoverageGap`, `ModuleReviewResult`) |
| `product_memory.py` | **v0.8.0** ProductArchitectureMemory: 从 CLI 图谱构建 LongtermAgentMemory (VectorDB 语义检索模块画像) |
| `review_judge.py` | **v0.8.0** ReviewJudge: LLM-as-Judge 自动评估评审报告 (5维度评分) |
| `review_evaluator.py` | **v0.9.0** ReviewEvaluator: 混合程序化+LLM 评分（防 LLM 自评膨胀） |
| `review_refiner.py` | **v0.9.0** ReviewRefiner: 1 次迭代改进循环 |
| `review_memory.py` | **v10.0** ReviewMemory: VectorDBBlock 使用 Gateway embedding |
| `adversarial_verifier.py` | **v0.9.0** 对抗性验证器 |
| `spec_decomposer.py` | **v0.9.0** 规格分解器 |
| `__init__.py` | 包导出 |

### 4.7 脚本入口

| 文件 | 说明 |
|------|------|
| `pipeline_runner.py` | **主入口**: 端到端测试流水线 (Stage 1-9) |
| `workforce_pipeline.py` | Workforce Pipeline 实现 (Stage 5-9) |
| `workflow_config_generator.py` | RAG + Agent 配置生成核心 |
| `interactive_cli.py` | 交互式配置生成器 (多轮对话) |
| `manage_database.py` | 数据库管理 CLI (status/create/update/rebuild/delete) |
| `initialize_pipeline.py` | Pipeline 初始化 (PDF 导入 → 索引构建) |
| `run_review.py` | **Bug-to-Case 评审入口**: Manager 统一编排 → 模块分流 → ReviewPipeline |
| `run_inagent_pipeline.ps1` | PowerShell 菜单入口 |

## 5. 数据流

```
测试任务文本 (.txt)
       │
       ▼
  ┌─ Stage 1 ─┐    ┌── Knowledge Base ──┐
  │ Test Plan  │◄──►│ knowledge_base.json │
  │ Agent      │    │ function_index.json │
  └─────┬──────┘    │ GraphRAG workspace  │
        │           └─────────────────────┘
        ▼
  ┌─ Stage 2-3 ─┐
  │ Env Planning │──► config_commands[]
  │ Config Gen   │──► verify_commands[]
  └──────┬───────┘    env_plan {}
         │
         ▼
  ┌─ Stage 4 ──┐
  │ VM Deploy  │──► HTTP 服务就绪
  └──────┬─────┘
         │
         ▼
  ┌─ Workforce Pipeline (5-9) ─┐
  │ Deploy → Verify/Traffic    │──► task_results {}
  │ → Analysis → Cleanup      │──► verdict: PASS/FAIL
  └─────────┬──────────────────┘
            │
            ▼
  ┌─ Report Generation ─┐
  │ *_test_result.json   │
  │ *_test_report.md     │
  └──────────────────────┘
```

## 6. 关键设计决策

### 6.1 为什么使用 CAMEL Workforce?

| 方面 | 之前（v0.3 单体函数链） | 现在（v0.4 Workforce） |
|------|----------------------|---------------------|
| 并行度 | 串行执行 5-9 | VerifyShow + Traffic 并行 |
| 可观测性 | 散落的 logger.info | WorkforceCallback 统一生命周期事件 |
| 质量控制 | 无 | Coordinator Agent 质量评估 + retry |
| 扩展性 | 改函数 + 重新串接 | 添加 Worker + 调整拓扑 |
| 上下文传递 | 函数参数逐级传递 | Task.additional_info 结构化上下文 |

### 6.2 共享连接 (P3)

所有 Toolkit 共享同一对 SSH/VM 连接:
```python
ssh_client = create_ssh_client_from_env()
vm = VMController.from_env()

nsae_tk = NSAEDeviceToolkit(ssh_client=ssh_client)
vm_tk   = VMControllerToolkit(vm=vm)
traffic_tk = TrafficVerifyToolkit(ssh_client=ssh_client, vm=vm)
```
避免 5 个 Worker 各自建 SSH 连接导致的会话冲突和资源浪费。

### 6.3 Verdict 机制

AnalysisWorker 输出必须包含 `Verdict: PASS` 或 `Verdict: FAIL`，由 `workforce_pipeline.py` 解析后映射为中文 "成功"/"失败"。MD 报告 §9 据此渲染整体判定。

## 7. 环境依赖

| 组件 | 说明 |
|------|------|
| Python | 3.10+ |
| CAMEL-AI | 主框架 (agents, workforce, models, tasks) |
| LLM Gateway | `llm_gateway/` 模块，代理 LLM API 请求 (默认 :9000) |
| LLM 后端 | SiliconFlow (qwen-plus) / OpenAI 兼容 |
| Embedding | text-embedding-v4 (DashScope via LLM Gateway) |
| Reranker | bge-reranker-v2-m3 (via LLM Gateway) |
| Paramiko | SSH 连接 |
| Qdrant | 向量存储 (本地模式, 3570 points, 1024 dim) |
| GraphRAG | 知识图谱 (5426 entities, 5852 relationships, 332 communities) |
| LanceDB | GraphRAG 向量索引 (entity/community/text_unit, 1024 dim) |

## 8. 配置

所有配置通过 `INAGENT/.env` 文件管理：

```env
# LLM Gateway
LLM_GATEWAY_URL=http://127.0.0.1:9000/v1

# NSAE 设备
LB_DEVICE_IP=172.16.6.215
LB_USERNAME=admin
LB_PASSWORD=admin
LB_SSH_PORT=22
LB_ENABLE_PUSH=true

# 测试 VM
LB_VM_IP=172.16.6.247
LB_VM_USER=root
LB_VM_PASSWORD=click1

# 默认网络规划
LB_REAL_SERVER_IP=10.0.3.10
LB_VIP=27.16.9.100
```

## 9. v0.8.0 评审架构增强 — CAMEL 全模块协作

### 9.1 设计动机

v0.7.x 评审 pipeline 仅使用 CAMEL 的 5/16 模块（ChatAgent, BaseMessage, Workforce, Task, NoteTakingToolkit），所有 Worker Agent 的 `tools=[]`（无工具），无法主动查询产品架构。v0.8.0 的目标是让 Agent **通过工具和记忆主动理解产品**，而非仅靠 prompt 注入的静态上下文。

### 9.2 CAMEL 模块使用情况

| CAMEL 模块 | v0.7.x | v0.8.0 | 具体用途 |
|------------|--------|--------|---------|
| agents (ChatAgent) | :white_check_mark: | :white_check_mark: | Worker/Coordinator/Judge Agent |
| messages (BaseMessage) | :white_check_mark: | :white_check_mark: | 消息构造 |
| societies.workforce | :white_check_mark: | :white_check_mark: + share_memory | Worker 编排 + 跨 Agent 记忆共享 |
| tasks (Task) | :white_check_mark: | :white_check_mark: | 任务定义 / fork-join 拓扑 |
| toolkits (NoteTaking) | :white_check_mark: | :white_check_mark: | 共享笔记 |
| toolkits (FunctionTool) | :x: | :white_check_mark: | ProductSkillToolkit (4) + KnowledgeToolkit (3) |
| memories (LongtermAgentMemory) | :x: | :white_check_mark: | ProductArchitectureMemory (VectorDB 101模块画像) |
| memories (VectorDBBlock) | :x: | :white_check_mark: | 语义检索模块技术特征 |
| memories (ScoreBasedContextCreator) | :x: | :white_check_mark: | 记忆上下文创建策略 |

### 9.3 五层架构

```
Layer 4 ─ Global Audit (run_review.py)
            模块列表附带 tech features, CLI 功能树注入

Layer 3 ─ Quality (ReviewJudge)
            LLM-as-Judge 5维评分: evidence/coverage/spec/action/trace

Layer 2 ─ Skills & Tools (pipeline.py)
            ProductSkillToolkit (4 tools) — 架构查询
            KnowledgeToolkit (3 tools) — RAG/规则检索
            NoteTakingToolkit (4 tools) — 共享笔记

Layer 1 ─ Memory (ProductArchitectureMemory)
            LongtermAgentMemory = ChatHistoryBlock + VectorDBBlock
            101 模块画像预写入, 语义检索 retrieve_limit=5

Layer 0 ─ Data (enriched CLI graph)
            cli_keyword_graph.json: 5505 nodes, 25750 edges
            每模块: protocol_stack, address_family, layer,
                    interface_types, related_modules, feature_tags
```

### 9.4 Agent 工具分配

| Agent | 工具集 (数量) | 记忆 | 关键能力 |
|-------|-------------|------|---------|
| CoverageWorker | PSK(4)+KN(3)+Note(4) = 11 | ProductMemory | 主动查询模块画像、发现横切面、RAG 检索 |
| CLISyntaxWorker | KN:search(1)+Note(4) = 5 | — | 按需检索产品知识验证命令语义 |
| SpecComplianceWorker | KN:rules(1)+Note(4) = 5 | — | 按需查询评审规则 |
| LoadStressWorker | KN:search(1)+Note(4) = 5 | — | 按需检索产品知识 |
| SynthesisWorker | PSK:cross+spec(2)+Note(4) = 6 | — | 横切面分析 + 规格约束查询 |

### 9.5 数据驱动的横切面推理

v0.8.0 不硬编码"SLB 必须测 HTTP 协议版本"，而是：
1. `enrich_cli_graph.py` 自动从 CLI 命令关键字推导 `protocol_stack=["HTTP","HTTPS",...]`
2. Agent 通过 `discover_cross_cutting_concerns("SLB ircookie")` 主动查询
3. 工具返回结构化横切面维度（协议版本、地址族、配置层级等）
4. Agent 基于这些数据自主判断是否存在覆盖缺口

### 9.6 横切面模块 Scope 保护 (v0.8.1)

全局审计 (Global Audit) 由 LLM 判断模块是否与 bug 修复相关，将无关模块标为 `out_of_scope` 跳过评审。但横切面测试模块（如 HTTP 版本、IPv6、语言/主题、集成 turbo、HA、Stress）验证的是新功能在不同环境条件下的行为，不应被跳过。

**双层防护**:
1. **Prompt 层**: 全局审计 prompt 明确指示横切面模块不得标为 out_of_scope
2. **Code 层**: `_CROSS_CUTTING_KEYWORDS` 关键词集合自动将误标模块从 out_of_scope 救回 in_scope

**CoverageWorker 配置并存检查**: 当存在多种配置维度（如 group 与 global、多个 group）时，检查是否有用例验证不同配置并存时互不影响。

**效果**: Bug 121100 评审 out_of_scope 从 15 模块降至 6 模块，命中率从 72% 提升至 83%。

## 10. v0.9.0 Self-Refine 自我改进循环

### 10.1 ReviewEvaluator — 混合评分

对评审报告执行 5 维度评分，采用"程序化 + LLM"混合策略避免 LLM 自评膨胀（Huang 2024）：

| 维度 | 策略 | 说明 |
|------|------|------|
| coverage_score | 程序化 | REQ gap ID 在 traceability_matrix 中的匹配率 |
| structural_score | 程序化 | 关键词检测（精简/删减/重组/合并） |
| clarity_score | 程序化 | 关键词检测（预期结果/模糊/可验证） |
| specificity_score | LLM | 严格对抗性提示，上限 0.8 |
| cross_cutting_score | LLM | 严格对抗性提示，上限 0.8 |

### 10.2 ReviewRefiner — 迭代改进

始终执行 1 次改进迭代：first eval → improve → second eval。典型提升：初始 0.74 → 最终 0.85。

### 10.3 报告清洗 (run_review.py)

5 阶段后处理（`_strip_internal_markers()`）：
1. 剥离 `<adversarial_verification>` 块
2. 剥离 `[溯源校验摘要]` 块
3. `<product_knowledge>` 等标签替换为自然中文
4. 移除 `REQ_xxx` 内部 ID
5. Synthesis prompt 输出自然语言，无内部标签

## 11. v10.0 RAG 基础设施全面升级

### 11.1 并行检索 (unified_rag.py)

`retrieve()` 方法改造为 `ThreadPoolExecutor(max_workers=2)` 并行：
```
Thread-1: GraphRAG local_search
Thread-2: 向量检索 (BM25 + semantic)
合并去重 → Rerank → 协议加权
```
总延迟 = max(GraphRAG_time, Vector_time) 而非 sum。

### 11.2 Fail-Stop 健康检查 (web/deps.py)

`check_rag_health()` 在评审启动前检查 4 项：

| 检查项 | 策略 |
|--------|------|
| Qdrant | collection 存在 + point_count > 0 |
| GraphRAG | entities.parquet 可读 + entity_count > 0 |
| LLM Gateway | GET /health 返回 200 |
| Reranker | POST /v1/rerank 返回 200 (可降级，不 fail-stop) |

关键服务不可用时抛出 RuntimeError 并 `sys.exit(1)`。

### 11.3 Qdrant 锁清理 (run_review.py)

`_find_qdrant_lock_holder()` 通过 PowerShell 查找持有 `.lock` 文件的进程 PID，交互式确认后 kill。解决异常退出后 portalocker 残留锁问题。

### 11.4 向量库 Metadata 修复

`workflow_config_generator.py` 传递 `should_chunk=False` 给 `hybrid_retriever.process()`，保留 `regex_metadata.document_category`（之前被 `chunk_by_title` 重分块丢失）。

向量库数据：3570 points（cli/reference），SHA256 指纹机制（`rag_meta.json`）检测变更触发重建。

### 11.5 GraphRAG 索引构建

索引规模：5426 entities, 5852 relationships, 332 communities, 129 text_units。
配置：chunk_size=64000 tokens, concurrent=15, max_tokens=8192, batch_size=10。

> 详见 §12 知识图谱结构与快照。

### 11.6 Gateway 并发 Embedding

`/v1/embeddings` 端点改造：大批量输入拆分为 MAX_EMBED_BATCH=10 的子批，通过 `asyncio.gather` + `Semaphore(5)` 5 路并发调用 DashScope。客户端 `embed_batch` 从 10 提升至 50。

### 11.7 Gateway 用量统计

`GET /metrics/usage?days=30` 端点，按模型按天统计 token 使用量（`_usage_by_day` / `_usage_totals`）。

## 12. 知识图谱结构与快照

本节记录 GraphRAG 知识图谱的完整结构（实体、关系、社区、文本单元），以及混合检索的双通路实现和快照回滚机制。

### 12.1 知识图谱总览

```
Source: knowledge_base.json (3570 CLI reference chunks)
           │
     GraphRAG Build (entity extraction + Leiden clustering)
           │
     ┌─────┴──────────────────────────────────────────┐
     │             GraphRAG Index                      │
     │                                                 │
     │  Entities ─────── 5426                          │
     │  Relationships ── 5852                          │
     │  Communities ──── 332  (3 levels)               │
     │  Text Units ───── 129                           │
     │  Community Reports 332                          │
     │                                                 │
     │  LanceDB (向量索引)                              │
     │    entity-description ── 5426 rows, 1024 dim    │
     │    community-full_content ── 332 rows            │
     │    text_unit-text ── 129 rows                    │
     └─────────────────────────────────────────────────┘
           │                          │
      GraphRAG 检索通路          Qdrant 向量检索通路
      (entity embedding           (BM25 + dense)
       → context build)           (3570 points)
           │                          │
           └────────┬─────────────────┘
                    │
              Rerank (bge-reranker-v2-m3)
                    │
              协议加权 → 最终结果
```

### 12.2 实体 (Entities)

**Parquet Schema**: `entities.parquet`

| 列名 | 类型 | 说明 |
|------|------|------|
| `id` | string | 全局唯一 ID (UUID hash) |
| `human_readable_id` | int | 自增序号 |
| `title` | string | 实体名称 (如 `AAA_LDAP_BIND_STATIC`, `LDAP_SERVER_NAME`) |
| `type` | string | 实体类型，见下表 |
| `description` | string | LLM 生成的中文语义描述 |
| `text_unit_ids` | list[string] | 关联的源文本块 ID |
| `frequency` | int | 在不同文本块中出现的频次 |
| `degree` | int | 图中连接边数 (0=孤立, max=562) |
| `x` | float | 2D 布局坐标 |
| `y` | float | 2D 布局坐标 |

**实体类型分布** (2026-04-02 基线):

| Type | Count | 占比 | 语义 |
|------|-------|------|------|
| COMMAND | 3358 | 61.9% | CLI 命令 (如 `SLB_REAL_HTTP_HEALTH_CHECK`) |
| PARAMETER | 1176 | 21.7% | 命令参数 (如 `LDAP_SERVER_NAME`, `DN_PREFIX`) |
| CONFIGURATION | 368 | 6.8% | 配置块 (如 `AAA_LDAP_CONFIGURATION`) |
| FEATURE | 250 | 4.6% | 功能特性 (如 `AUTHENTICATION`, `SINGLE_SIGN_ON`) |
| PRODUCT_MODULE | 126 | 2.3% | 产品模块 (如 `AAA`, `SLB`, `SSL`) |
| PROTOCOL | 57 | 1.1% | 协议 (如 `LDAP`, `RADIUS`, `SAML`) |
| *(empty)* | 91 | 1.7% | 类型未提取 (边缘实体) |

**Degree 分布**: degree=0: 1804 (33.2%), degree=1: 1715 (31.6%), degree=2-5: 1500 (27.7%), degree≥6: 407 (7.5%), mean=2.15

### 12.3 关系 (Relationships)

**Parquet Schema**: `relationships.parquet`

| 列名 | 类型 | 说明 |
|------|------|------|
| `id` | string | 全局唯一 ID |
| `human_readable_id` | int | 自增序号 |
| `source` | string | 源实体 title |
| `target` | string | 目标实体 title |
| `description` | string | 关系类型标签 (非自然语言) |
| `weight` | float | 关系权重 [1.0, 30.0], mean=7.91 |
| `combined_degree` | int | source.degree + target.degree |
| `text_unit_ids` | list[string] | 关联的源文本块 ID |

**关系类型分布** (存储在 `description` 字段):

| 关系类型 | Count | 语义 |
|---------|-------|------|
| BELONGS_TO | 2470 | 参数/命令归属于模块 |
| PART_OF | 1690 | 实体是另一实体的组成部分 |
| CONFIGURES | 829 | 命令配置某功能/参数 |
| SUPPORTS_PROTOCOL | 263 | 模块/功能支持某协议 |
| IS_VARIANT_OF | 215 | 命令是另一命令的变体 (如 no/show/clear) |
| PRECEDES | 168 | 命令执行的前置依赖顺序 |
| DEPENDS_ON | 89 | 功能依赖关系 |
| *混合标签* | 128 | 多标签组合 (如 `CONFIGURES \| PART_OF`) |

> **已知问题**: 所有关系的 description 字段均为裸类型标签而非自然语言描述。检索时在 `_convert_context_to_results()` 中过滤 (规则: `" " not in desc`)。

### 12.4 社区 (Communities)

**Parquet Schema**: `communities.parquet`

| 列名 | 类型 | 说明 |
|------|------|------|
| `id` | string | 全局唯一 ID |
| `community` | int | 社区编号 |
| `level` | int | Leiden 层级 (0/1/2) |
| `parent` | string/null | 父社区 ID |
| `children` | list[string] | 子社区 ID 列表 |
| `title` | string | 社区标题 |
| `entity_ids` | list[string] | 包含的实体 ID |
| `relationship_ids` | list[string] | 包含的关系 ID |
| `text_unit_ids` | list[string] | 关联的文本块 |
| `size` | int | 社区包含的实体数 |

**社区层级结构**:

| Level | 社区数 | size min | size max | size mean | 含义 |
|-------|--------|----------|----------|-----------|------|
| 0 | 34 | 15 | 516 | 96.1 | 粗粒度: 产品模块级 (如整个 AAA/SLB) |
| 1 | 230 | 2 | 252 | 14.0 | 中粒度: 功能子模块级 |
| 2 | 68 | 2 | 242 | 9.3 | 细粒度: 命令组级 |

**Top 5 社区 (by size)**: #23 (516), #22 (259), #185 (252), #317 (242), #9 (219)

**孤儿实体**: 2159 个实体 (39.8%) 不属于任何 Leiden 社区。类型分布: COMMAND 1088, PARAMETER 649, CONFIGURATION 200, FEATURE 128, PRODUCT_MODULE 68, PROTOCOL 23。

> **修复**: `_init_context_builder()` 中 `read_indexer_entities(entities, communities, None)` 跳过社区过滤，确保孤儿实体参与向量检索。Community reports 仍使用 `community_level=2`。

### 12.5 文本单元 (Text Units)

**Parquet Schema**: `text_units.parquet`

| 列名 | 类型 | 说明 |
|------|------|------|
| `id` | string | 全局唯一 ID |
| `text` | string | 源文本内容 (length: 130~49538, mean=8244) |
| `n_tokens` | int | Token 数 |
| `document_ids` | list[string] | 来源文档 ID |
| `entity_ids` | list[string] | 从中提取的实体 |
| `relationship_ids` | list[string] | 从中提取的关系 |

### 12.6 混合检索实现

```
UnifiedRAGRetriever.retrieve(query, category_whitelist, top_k_final)
    │
    ├── Thread-1: GraphRAG Local Search
    │     graphrag_integration.local_context_build(query, top_k)
    │       → LanceDB entity embedding 相似度检索
    │       → map_query_to_entities (向量匹配)
    │       → _build_community_context (社区报告)
    │       → _build_local_context (实体描述 + 关系)
    │       → _build_text_unit_context (源文本)
    │       → _convert_context_to_results
    │           过滤: desc 无空格 → 跳过 (bare type labels)
    │
    ├── Thread-2: Vector Search (Qdrant)
    │     HybridRetriever.search(query)
    │       → BM25 sparse + Dense embedding
    │       → category_whitelist 过滤
    │       → RRF 融合
    │
    └── 合并去重 (by content hash)
          → Rerank (bge-reranker-v2-m3)
          → 协议加权 (scoring_utils)
          → top_k_final 截断
```

**GraphRAG → Vector 互补**: GraphRAG 擅长结构化定位（"aaa ldap bind" → 精确定位 LDAP 静态/动态绑定命令），Vector 擅长语义模糊匹配（"怎么配置健康检查" → 相关文档段落）。

### 12.7 快照与回滚机制

快照的目的是在知识库发生污染时（如错误的文档混入、实体提取质量下降），根据时间和内容快速回滚到已验证的基线，避免概念漂移。

**快照目录**: `INAGENT/graphrag_index/snapshots/<snapshot_id>/`

**快照内容**:
```
snapshots/20260402_cli_reference_v1/
├── manifest.json              # 快照元数据 + 校验和
├── entities.parquet           # 实体表
├── relationships.parquet      # 关系表
├── communities.parquet        # 社区表
├── community_reports.parquet  # 社区报告
├── documents.parquet          # 文档表
├── text_units.parquet         # 文本单元
└── lancedb/                   # LanceDB 向量索引
    ├── default-entity-description.lance/
    ├── default-community-full_content.lance/
    └── default-text_unit-text.lance/
```

**manifest.json 结构**:
- `snapshot_id`: 快照标识 (`<date>_<source>_<version>`)
- `source`: 源知识库路径、chunk 数、md5 校验和
- `graphrag_index`: 实体/关系/社区/文本单元统计 + 类型分布
- `vector_store`: Qdrant collection 信息 + point 数
- `lancedb`: 三张表的行数和向量维度
- `parquet_checksums`: 每个 parquet 文件的 size 和 md5
- `recall_validation`: 召回测试结果 (轮次、覆盖模块、通过率)
- `known_issues`: 当前已知问题的记录

**回滚流程**:
1. 选择目标快照: `snapshots/<snapshot_id>/`
2. 校验 manifest.json 中的 parquet md5
3. 用快照 parquet 覆盖 `graphrag_index/output/` 目录
4. 用快照 lancedb 覆盖 `graphrag_index/output/lancedb/`
5. 重启服务使 GraphRAG integration 重新加载索引

**基线快照**:

| ID | 日期 | 源数据 | 实体 | 关系 | 社区 | 召回验证 |
|----|------|--------|------|------|------|----------|
| 20260402_cli_reference_v1 | 2026-04-02 | knowledge_base.json (3570 chunks, md5=cfdf9d) | 5426 | 5852 | 332 | 35/35 OK (3 rounds) |

## 13. 相关文档

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 更新日志 |
| [BUG_TO_CASE_INPUT_USAGE.md](BUG_TO_CASE_INPUT_USAGE.md) | Bug-to-Case 输入格式说明 |
| [DESIGN.md](DESIGN.md) | 详细设计文档（知识库/检索层/Agent 层/GraphRAG 集成） |
| [PLAN_HISTORY.md](PLAN_HISTORY.md) | ReviewPipeline 历史设计（v1/v2） |
| [TRANSFORMATION_PLAN.md](TRANSFORMATION_PLAN.md) | auto_convert/GraphRAG 一体化改造计划 |
| [UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md](UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md) | unknown 模块误标分析 |

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 更新日志 |
| [BUG_TO_CASE_INPUT_USAGE.md](BUG_TO_CASE_INPUT_USAGE.md) | Bug-to-Case 输入格式说明 |
| [DESIGN.md](DESIGN.md) | 详细设计文档（知识库/检索层/Agent 层/GraphRAG 集成） |
| [PLAN_HISTORY.md](PLAN_HISTORY.md) | ReviewPipeline 历史设计（v1/v2） |
| [TRANSFORMATION_PLAN.md](TRANSFORMATION_PLAN.md) | auto_convert/GraphRAG 一体化改造计划 |
| [UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md](UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md) | unknown 模块误标分析 |
