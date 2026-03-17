# INAGENT 系统架构设计文档

> 版本: v0.4.0 | 更新: 2026-03-06

## 1. 系统概述

INAGENT 是基于 [CAMEL-AI](https://github.com/camel-ai/camel) 框架的**智能网络设备自动化测试系统**。它将 LLM 推理、RAG 知识检索、GraphRAG 知识图谱和多 Agent 协作有机结合，对 NSAE (InfosecOS) 网络负载均衡器设备执行端到端自动化测试。

**核心能力**:
- 自然语言测试任务 → 自动生成设备配置命令
- SSH 自动下发配置 + show 命令验证
- HTTP 端到端流量探测 + 故障注入/恢复
- LLM 驱动的测试结果分析 (Verdict: PASS/FAIL)
- 全流程 Markdown 报告生成

## 2. 整体架构

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

所有 Toolkit 共享同一组 SSH/VM 连接（P3 优化），避免重复建连。

### 4.3 RAG 模块 (`rag/`)

| 文件 | 说明 |
|------|------|
| `unified_rag.py` | 统一 RAG 检索器：向量检索 + GraphRAG + Rerank + 协议加权 |
| `graphrag_integration.py` | GraphRAG 知识图谱检索集成 |
| `graphrag_adapter.py` | GraphRAG 适配器（实体提取/配置生成） |
| `fallback_retrieval.py` | RAG 回退检索逻辑 |
| `rerank_retriever.py` | SiliconFlow Rerank 检索器 |
| `scoring_utils.py` | 协议加权评分 |

### 4.4 工具模块 (`utils/`)

| 文件 | 说明 |
|------|------|
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

### 4.6 脚本入口

| 文件 | 说明 |
|------|------|
| `pipeline_runner.py` | **主入口**: 端到端测试流水线 (Stage 1-9) |
| `workforce_pipeline.py` | Workforce Pipeline 实现 (Stage 5-9) |
| `workflow_config_generator.py` | RAG + Agent 配置生成核心 |
| `interactive_cli.py` | 交互式配置生成器 (多轮对话) |
| `manage_database.py` | 数据库管理 CLI (status/create/update/rebuild/delete) |
| `initialize_pipeline.py` | Pipeline 初始化 (PDF 导入 → 索引构建) |
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
| Embedding | bge-m3 (via LLM Gateway) |
| Reranker | bge-reranker-v2-m3 (via SiliconFlow) |
| Paramiko | SSH 连接 |
| Qdrant | 向量存储 (本地模式) |
| GraphRAG | 知识图谱 (可选) |

## 8. 配置

所有配置通过 `INAGENT/.env` 文件管理：

```env
# LLM Gateway
LLM_GATEWAY_URL=http://127.0.0.1:9000/v1

# NSAE 设备
LB_DEVICE_IP=<your-device-ip>
LB_USERNAME=admin
LB_PASSWORD=<your-password>
LB_SSH_PORT=22
LB_ENABLE_PUSH=true

# 测试 VM
LB_VM_IP=<your-vm-ip>
LB_VM_USER=root
LB_VM_PASSWORD=<your-password>

# 默认网络规划
LB_REAL_SERVER_IP=10.0.3.10
LB_VIP=<your-vip>
```

## 9. 相关文档

| 文档 | 说明 |
|------|------|
| [CHANGELOG.md](CHANGELOG.md) | 更新日志 |
| [TRANSFORMATION_PLAN.md](TRANSFORMATION_PLAN.md) | auto_convert/GraphRAG 一体化改造计划 |
| [UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md](UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md) | unknown 模块误标分析 |
