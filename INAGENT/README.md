# INAGENT 项目技术文档目录

## 项目概述

INAGENT 是基于 CAMEL-AI 框架的智能网络设备自动化测试系统，集成 LLM Gateway、RAG 检索、GraphRAG 知识图谱、多 Agent 协作等能力，实现从测试任务生成到故障注入再到结果分析的全流程自动化。

v0.4.0 起，测试执行阶段 (Stage 5-9) 由 **CAMEL Workforce Pipeline** 驱动，5 个 Worker 并行协作。

v0.5.0 新增 **Web Platform**（FastAPI + SPA），提供知识库管理、RAG 问答、测试执行、结果查看和系统运维五大可视化模块。

v0.6.0 新增 **ReviewPipeline**（自主三步评审：plan → knowledge → review），支持 CLI 批量评审和 Web 实时评审。重命名 `graphrag_workspace` → `graphrag_index`，`test` 模式 → `e2e` 以消除歧义。

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | **系统架构设计文档** |
| [docs/WEB_USER_GUIDE.md](docs/WEB_USER_GUIDE.md) | **Web Platform 使用手册** |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 项目更新日志（完整变更记录） |
| [docs/TRANSFORMATION_PLAN.md](docs/TRANSFORMATION_PLAN.md) | auto_convert/GraphRAG 一体化改造计划 |
| [docs/UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md](docs/UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md) | unknown 模块误标分析 |

## 核心目录结构

```
INAGENT/
├── agents/                     # Agent 模块
│   ├── test_plan_agent.py      # 测试计划生成 Agent (Stage 1)
│   ├── env_setup_agent.py      # 环境搭建 Agent (Stage 2)
│   ├── task_decomposition_agent.py  # 任务分解 Agent (Stage 3)
│   ├── lb_ops_agent.py         # 负载均衡操作 Agent (Stage 3)
│   ├── task_analysis_agent.py  # 测试结果分析 Agent (deprecated)
│   └── cleanup_agent.py        # 环境清理 Agent (deprecated)
├── toolkits/                   # ★ Workforce Toolkit (v0.4.0 新增)
│   ├── nsae_device_toolkit.py  # NSAE 设备 SSH 工具集 (4 tools)
│   ├── vm_controller_toolkit.py # 测试 VM 控制工具集 (9 tools)
│   └── traffic_verify_toolkit.py # HTTP 流量验证工具集 (4 tools)
├── rag/                        # RAG 检索模块
│   ├── unified_rag.py          # 统一 RAG 检索器（向量+GraphRAG+Rerank+协议加权）
│   ├── graphrag_integration.py # GraphRAG 检索集成
│   ├── graphrag_adapter.py     # GraphRAG 适配器（实体提取/配置生成）
│   ├── fallback_retrieval.py   # RAG 回退检索逻辑
│   ├── rerank_retriever.py     # SiliconFlow Rerank 检索器
│   └── scoring_utils.py        # 协议加权评分
├── utils/                      # 通用工具模块
│   ├── ssh_client.py           # SSH 设备连接客户端
│   ├── vm_controller.py        # 测试 VM 控制器
│   ├── env_utils.py            # 环境变量管理
│   ├── index_utils.py          # 功能结构索引加载
│   ├── llm_config.py           # LLM 配置管理
│   └── workflow_logger.py      # Workflow 日志记录
├── data_tools/                 # 数据处理工具
│   ├── auto_convert.py         # PDF → JSON 文档转换
│   ├── auto_document_integration.py # 自动文档集成
│   ├── build_function_structure_index.py  # 功能结构索引构建
│   └── merge_knowledge_base.py # 知识库合并
├── scripts/                    # 工具脚本
│   ├── model_helpers.py        # 模型辅助工具
│   ├── init_graphrag.py        # GraphRAG 初始化/构建
│   ├── graphrag_batch_manager.py  # GraphRAG 批量管理
│   └── ...                     # 其他辅助脚本
├── docs/                       # 项目文档
│   ├── ARCHITECTURE.md         # 系统架构设计
│   ├── CHANGELOG.md            # 更新日志
│   └── ...                     # 其他技术文档
├── unit_tests/                 # 单元测试（pytest）
├── jobs/                       # 任务作业目录
│   ├── config_tasks/           # 配置生成任务 (.txt)
│   └── test_review/            # 测试用例评审输入 (.xlsx/.xls)
├── knowledge_base/             # 知识库数据
│   ├── reference/              # 参考数据 (app.json, cli.json, knowledge_base.json)
│   ├── logs/                   # 运行日志
│   └── mineru_output/          # MinerU PDF 解析输出
├── graphrag_index/         # GraphRAG 索引数据
├── graphrag/                   # GraphRAG 第三方库 & 配置
├── reports/                    # 测试输出报告 (*_test_report.md, *_test_result.json)
├── output/                     # 历史运行记录 (run6-run13)
├── web/                        # ★ Web Platform (v0.5.0 新增)
│   ├── app.py                  # FastAPI 应用入口
│   ├── deps.py                 # 共享依赖管理 (RAG/LLM/DB)
│   ├── models.py               # Pydantic 数据模型
│   ├── routers/                # 5 个 API 路由模块
│   └── static/                 # 前端 SPA (HTML/JS/CSS)
│
├── review/                     # ★ ReviewPipeline (v0.6.0 新增)
│   ├── pipeline.py             # 三步自主评审 (plan → knowledge → review)
│   └── PLAN.md                 # 设计规划
├── review_results/             # 评审结果输出
├── pipeline_runner.py          # ★ 主入口：端到端测试流水线
├── run_test_review.py          # ★ 测试用例批量评审入口
├── workforce_pipeline.py       # ★ Workforce Pipeline (阶段 5-9)
├── workflow_config_generator.py # ★ RAG + Agent 配置生成
├── interactive_cli.py          # 交互式配置生成器
├── manage_database.py          # 数据库管理 CLI
├── initialize_pipeline.py      # Pipeline 初始化
├── run_inagent_pipeline.ps1    # PowerShell 菜单入口
└── .env                        # 环境配置
```

## 测试流水线

```
Stage 1   → 测试计划生成 (Test Plan Agent + RAG)
Stage 2   → 网络环境规划 (Env Setup Agent)
Stage 3   → 任务分解 + 配置生成 (RAG + LB Ops Agent)
Stage 3.5 → VIP 冲突预检 (ARP 探测)
Stage 4   → VM 测试环境部署 (SSH + HTTP 服务)
            ═══════════════════════════════════════
Stage 5-9 → CAMEL Workforce Pipeline (多 Agent 并行)
            ├─ Stage 5:   DeployWorker      配置下发
            ├─ Stage 6:   TrafficWorker     流量验证 + 故障注入  ┐ 并行
            ├─ Stage 7:   VerifyShowWorker  SSH 验证             ┘
            ├─ Stage 8:   AnalysisWorker    AI 分析判定
            └─ Stage 9:   CleanupWorker     环境清理
```

> 详细架构说明参见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 快速开始

```bash
# 1. 启动 LLM Gateway
python llm_gateway/start.py

# 2a. 运行端到端测试 (命令行)
python INAGENT/pipeline_runner.py --jobs-dir INAGENT/jobs --output-dir INAGENT/reports

# 2b. 启动 Web Platform (可视化)
python -m uvicorn INAGENT.web.app:app --host 0.0.0.0 --port 8010
# 或: .\INAGENT\run_inagent_pipeline.ps1 -Mode web
# 打开浏览器访问 http://127.0.0.1:8010

# 3. 查看报告
cat INAGENT/reports/a1_test_report.md

# 或使用 PowerShell 菜单
.\INAGENT\run_inagent_pipeline.ps1 -Mode e2e

# 4. 测试用例评审
# 将 .xlsx/.xls 放入 INAGENT/jobs/test_review/ 后:
.\INAGENT\run_inagent_pipeline.ps1 -Mode review
```
