# INAGENT 更新日志 (CHANGELOG)

本文件记录 INAGENT 项目的所有重要变更。

---

## [v0.6.0] - 2026-03-18

### 🔍 ReviewPipeline — 自主三步评审

新增 `ReviewPipeline`（plan → knowledge → review），替代之前的硬编码 prompt 评审模式。支持 CLI 批量评审和 Web 实时评审两种入口。

#### 新增/变更文件

| 文件 | 说明 |
|------|------|
| `review/pipeline.py` | 三步自主评审 Pipeline（ReviewPlan → 知识查询 → 评审生成） |
| `run_test_review.py` | CLI 批量评审入口（自动发现 Excel、按模块分批） |
| `web/routers/chat.py` | Web 评审入口改用 ReviewPipeline（SSE 流式） |
| `run_inagent_pipeline.ps1` | 新增 `review` 模式（菜单选项 6） |

#### 功能亮点

- **自主计划**: Agent 先分析测试用例生成 `ReviewPlan`（JSON），自主决定评审角度和知识查询范围
- **知识增强**: 根据 ReviewPlan 自动从 RAG + GraphRAG 检索产品知识上下文
- **模块级评审**: 按 Item/Sub Item 分组，模块整体评审、分批处理大模块（>50 条拆分子批）
- **多入口统一**: CLI (`-Mode review`)、Web (test_review 端点)、Python (`ReviewPipeline.run()`) 均走同一 Pipeline
- **Bug 定向评审**: 可选传入 `bug_profile` JSON，评审聚焦 Bug 关联覆盖度

### 📁 项目结构优化

#### 重命名

| 旧名称 | 新名称 | 说明 |
|--------|--------|------|
| `graphrag_workspace/` | `graphrag_index/` | GraphRAG 索引数据目录，避免与 `graphrag/` 子模块混淆 |
| `-Mode test` | `-Mode e2e` | Pipeline 端到端执行模式，避免与 "单元测试" 歧义 |
| `Invoke-TestExecution` | `Invoke-E2EPipeline` | PS1 函数名同步更新 |
| `GRAPHRAG_WORKSPACE` | `GRAPHRAG_INDEX_DIR` | Python 常量名同步更新 |

#### jobs 目录拆分

```
jobs/
├── config_tasks/    # 配置生成任务 (.txt)，原 jobs/ 根目录文件移入
└── test_review/     # 测试用例评审输入 (.xlsx/.xls)
```

#### 清理

- 删除根目录临时脚本：`_check_db.py`, `_check_device_clean.py`, `_check_log.py`, `_consolidate_review.py`
- 删除根目录临时日志：`_*.log`, `_device_check_output.txt`
- `_run_test_review.py` 移入 `INAGENT/run_test_review.py`（去掉 `_` 前缀）
- 评审输出目录从 `_review_results/` 改为 `INAGENT/review_results/`

---

## [v0.5.0] - 2026-03-06

### 🌐 Web Platform — 可视化管理界面

新增基于 FastAPI + 原生 JavaScript SPA 的可视化管理平台，提供知识库管理、RAG 问答、测试执行、结果查看和系统运维五大功能模块。

#### 新增文件

| 文件 | 说明 |
|------|------|
| `web/app.py` | FastAPI 应用入口（路由注册、CORS、静态文件挂载） |
| `web/deps.py` | 共享依赖管理（RAG/LLM/SQLite 单例初始化） |
| `web/models.py` | Pydantic 请求/响应数据模型 |
| `web/routers/knowledge.py` | 模块1: 知识库管理 API（文档上传、KB CRUD、条目浏览） |
| `web/routers/chat.py` | 模块2: RAG 问答 API（多轮对话、功能解释/配置生成） |
| `web/routers/testing.py` | 模块3: Pipeline 执行 API（任务 CRUD、子进程启动、WebSocket 日志流） |
| `web/routers/reports.py` | 模块4: 测试结果 API（历史列表、报告详情、统计汇总） |
| `web/routers/system.py` | 模块5: 系统管理 API（环境配置、连通性探测、日志查看） |
| `web/static/index.html` | 前端 HTML（五模块 Tab 布局） |
| `web/static/app.js` | 前端 JavaScript（API 调用、WebSocket、交互逻辑） |
| `web/static/styles.css` | 前端样式（暗色科技风格） |
| `docs/WEB_USER_GUIDE.md` | Web Platform 使用手册 |

#### 功能亮点

- **模块1 (产品功能学习)**: PDF 文档上传 + 知识库统计 + 条目过滤检索 + DB 操作可视化（对应 `manage_database.py`）
- **模块2 (产品知识展示)**: 多轮对话会话管理 + 两种问答模式（功能解释 / 配置生成）+ 配置/验证命令输出面板
- **模块3 (产品测试功能)**: 任务文件 CRUD 编辑器 + Pipeline 子进程管理 + WebSocket 实时日志流 + 取消运行
- **模块4 (测试结果展示)**: 运行历史表格 + Verdict 统计 + Markdown 报告 / JSON 结果查看
- **模块5 (系统管理)**: 6 项连通性检测（Gateway/Device/VM/RAG/GraphRAG/KB）+ .env 在线编辑 + 日志中心

#### 安全性

- 所有接受文件名参数的端点（6 处）已添加路径遍历防护（`.resolve()` + `startswith()` 校验）
- WebSocket 实时日志支持指数退避自动重连（最多 5 次）
- `.env` 更新支持新变量自动追加，空字符串可清除已有值

### 🗄️ MinerU 中间文件备份机制

MinerU PDF 解析生成的中间文件不再直接删除，改为移入 `mineru_backup/` 备份目录（MinerU 重新生成耗时较大）。

#### 变更文件

| 文件 | 说明 |
|------|------|
| `data_tools/auto_convert.py` | `_cleanup_mineru_intermediate()` 改为 `shutil.move()` 至 `mineru_backup/` |
| `data_tools/auto_convert.py` | `_cleanup_orphan_files()` 孤立目录移至备份 |
| `data_tools/auto_convert.py` | 新增 `_cleanup_old_logs()` 30 天日志轮转 |
| `manage_database.py` | `action_delete(clean_mineru=True)` 改为移至备份 |
| `.gitignore` | 新增 `mineru_output/`、`mineru_backup/`、`reference/`、`logs/` 等排除规则 |

---

## [v0.4.0] - 2026-03-06

### 🚀 CAMEL Workforce Pipeline 迁移 (阶段 5-9)

将测试执行阶段 5-9（配置下发、流量验证、SSH 验证、AI 分析、环境清理）从单体函数链重构为 **CAMEL Workforce** 多 Agent 协作流水线，5 个 Worker 各司其职，由 Coordinator Agent 统一调度。

#### 架构变更

**Pipeline 拓扑** (阶段 5-9):
```
Deploy ──► fork ─┬── VerifyShow ──┐
                 └── Traffic ─────┘── join → Analysis ──► Cleanup
```

**5 个 Workforce Worker**:
| Worker | 职责 | 配备 Toolkit |
|--------|------|-------------|
| DeployWorker | 配置下发 + show 命令验证 | NSAEDeviceToolkit |
| VerifyShowWorker | SSH show 命令验证配置生效 | NSAEDeviceToolkit |
| TrafficWorker | HTTP 流量探测 + 故障注入 | VMControllerToolkit + TrafficVerifyToolkit |
| AnalysisWorker | 综合 Deploy/Verify/Traffic 输出判定 PASS/FAIL | — (纯 LLM 推理) |
| CleanupWorker | 生成 no/delete 命令清理环境 | NSAEDeviceToolkit + VMControllerToolkit |

#### 新增文件
| 文件 | 说明 |
|------|------|
| `workforce_pipeline.py` | Workforce Pipeline 主入口，构建 Worker 拓扑与 Task 编排 |
| `toolkits/__init__.py` | Toolkit 包导出 |
| `toolkits/nsae_device_toolkit.py` | NSAE 设备 SSH 操作工具集 (4 tools) |
| `toolkits/vm_controller_toolkit.py` | 测试 VM 控制工具集 (9 tools) |
| `toolkits/traffic_verify_toolkit.py` | HTTP 流量验证工具集 (4 tools) |
| `docs/ARCHITECTURE.md` | 系统架构设计文档 |

#### 核心优化
- **P3 共享连接**: SSH/VM 连接通过 `create_ssh_client_from_env()` 和 `VMController.from_env()` 在 Worker 间共享，避免重复建连
- **O1 Task 对象**: 使用 CAMEL `Task` 对象携带 `additional_info` 结构化上下文，替代纯字符串传递
- **O3 Callback**: `INAGENTPipelineCallback(WorkforceCallback)` 提供 9 个生命周期回调方法，记录任务分配、完成、失败等事件到日志
- **Verdict 机制**: AnalysisWorker 输出 `Verdict: PASS/FAIL`，pipeline_runner.py 据此生成 MD 报告总结

#### MD 报告适配
`pipeline_runner.py` 的 `generate_md_report()` 已完成 Workforce 模式渲染：
- §5 (配置下发): 渲染 DeployWorker 文本输出
- §6 (流量验证): 渲染 TrafficWorker 文本输出
- §6.8 (故障注入): Workforce 模式下提示 "已由 TrafficWorker 统一执行"
- §7 (SSH 验证): 渲染 VerifyShowWorker 文本输出
- §9 (总结): Verdict 驱动的整体判定 + 7 阶段状态表

#### 废弃函数
以下 `pipeline_runner.py` 中的函数已标记 `DEPRECATED`，功能由 Workforce Worker 替代：
- `deploy_config_commands()` → DeployWorker
- `verify_test_results()` → VerifyShowWorker
- `verify_traffic()` → TrafficWorker
- `execute_fault_injection()` → TrafficWorker
- `cleanup_test_environment()` → CleanupWorker
- `analyze_test_result()` → AnalysisWorker

#### 测试验证
首次 Workforce 端到端测试 (2026-03-06):
- **a1.txt** (HTTP 内容健康检查): Verdict ✅ PASS — 18 条配置 + 5 步故障注入
- **a2.txt** (源地址负载均衡): Verdict ✅ PASS — 15 条配置 + hi 算法验证
- 总计: 2/2 成功，0 失败

#### 项目结构整理
- 设计文档 (`TRANSFORMATION_PLAN.md`, `UNKNOWN_MODULE_MISCLASSIFICATION_SUMMARY.md`) 移入 `docs/`
- 新增 `docs/ARCHITECTURE.md` 系统架构文档
- `README.md` 更新目录结构和流水线阶段描述
- `run_inagent_pipeline.ps1` / `run_inagent_pipeline.bat` 更新阶段 5-9 描述

---

## [v0.3.0] - 2025-07-08

### ✅ Run10 全流程测试通过

经过多轮调试优化，**Run10** 实现了端到端全流程自动化测试通过，包括 SLB 配置下发、流量验证、故障注入与恢复验证，LLM 分析判定：**【成功】**。

#### 🔧 NSAE 设备 CLI 语法修复
- **问题**: Run9 中 `interface port3` 命令返回 `^` 错误 —— NSAE (InfosecOS) 不支持 Cisco 风格的接口子模式
- **修复**: 所有接口地址配置改为扁平命令格式：
  ```
  # 错误 (Cisco 风格):
  interface port3
    ip address 10.0.3.1 255.255.255.0
  exit

  # 正确 (NSAE 扁平格式):
  ip address port3 10.0.3.1 255.255.255.0
  ```
- **涉及文件**: `lb_ops_agent.py` — CLI Reference 中的示例模板
- **涉及文件**: `pipeline_runner.py` — `_classify_config_commands()` 简化，移除接口块追踪逻辑

#### 🔧 pipeline_runner.py 命令分类简化
- `_classify_config_commands()` 不再使用 `interface_block_tracking` 模式
- 直接按关键字前缀匹配分类: `ip route static` → 网络层, `slb` → 应用层, 其余 → 网络层
- 命令发送统一在 `config` 模式下执行，无需 `interface` 子模式切换

#### 🔧 健康检查验证 (Content-Based Check)
- 4 步验证逻辑: `show slb virtual-server` → `show slb service-group` → `show slb server` → SSH HTTP curl
- 支持 UP/DOWN 状态检测与端口级别状态验证

#### 📊 Run10 测试结果
- 11 条 SLB 配置命令全部下发成功
- 故障注入 5 步全部通过:
  - Step A: 停止 web1 服务 → real-server DOWN ✓
  - Step B: 恢复 web1 → real-server UP ✓
  - Step C: 修改 web2 页面内容 → content-check DOWN ✓
  - Step D: 恢复 web2 内容 → content-check UP ✓
  - Step E: 全部恢复后验证 → 所有 real-server UP ✓
- 报告: `reports/a1_test_report.md`, `reports/a1_test_result.json`

---

### 🚀 脚本优化与中文编码修复

#### 解决终端中文乱码问题
- **根本原因**: PowerShell 的 `2>&1 | ForEach-Object` 管道不会将 Python 子进程的 UTF-8 输出正确解码，导致中文显示为 Mojibake
- **修复方案**:
  - `run_inagent_pipeline.bat`: 设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1` 环境变量，PowerShell 调用增加 `[Console]::OutputEncoding = UTF8`
  - `run_inagent_pipeline.ps1`: 顶部统一设置 `[Console]::InputEncoding`、`[Console]::OutputEncoding`、`$OutputEncoding`、`chcp 65001`
  - 新增 `Invoke-PythonWithLog` 辅助函数，使用 `System.Diagnostics.Process` API 启动 Python 子进程，显式指定 `StandardOutputEncoding = UTF8` 和 `StandardErrorEncoding = UTF8`
  - 替换所有原有的 `python -u ... 2>&1 | ForEach-Object` 模式

#### 统一日志格式
- 终端输出与日志文件统一使用 `[timestamp] [LEVEL] message` 格式
- `Invoke-PythonWithLog` 自动将 Python stdout/stderr 实时输出到终端，同时写入日志文件 (UTF-8 编码)

#### 脚本流程适配 (已更新到最新 9 阶段流水线)
- **菜单更新**: 主菜单描述与实际流水线阶段对齐
- **阶段列表**: `Invoke-TestExecution` 执行前显示完整 9 阶段列表
- **移除过时内容**: 删除 "Batch API (FREE, fully automatic)" 等过时引用
- **文档同步**: README.md 流水线阶段从旧编号 (1→2→2.5→3→3.5→3.8→4→5→6) 更新为实际编号 (1→2→3→3.5→4→5→6→6.5→7→8→9)

#### 受影响文件
| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `run_inagent_pipeline.bat` | 修改 | 编码环境变量 + PowerShell UTF-8 启动参数 |
| `run_inagent_pipeline.ps1` | 重构 | 编码修复 + Invoke-PythonWithLog + 全函数更新 |
| `README.md` | 更新 | 流水线阶段编号同步 |
| `pipeline_runner.py` | 修改 | 命令分类简化 + NSAE 语法适配 |
| `lb_ops_agent.py` | 修改 | CLI Reference 修正 |
| `docs/CHANGELOG.md` | 新增 | 本文件 |

---

## [v0.2.0] - 2025-07-01

### ✅ GraphRAG + RAG 统一流水线

- 完成 GraphRAG 知识图谱构建与检索集成
- 实现 Unified RAG 检索器: 向量检索 + GraphRAG + Rerank + 协议加权
- 支持 SiliconFlow / Qwen 嵌入与重排序模型
- LLM Gateway 超时问题修复 (详见 `docs/TIMEOUT_FIX_REPORT.md`)
- Pydantic 验证修复 (详见 `docs/PYDANTIC_FIX_REPORT.md`)

### 🔧 环境搭建自动化

- VM 测试环境自动部署 (VMController SSH)
- 支持 Kali Linux 测试 VM 的 IP 配置、Apache 服务部署
- 环境清理阶段自动回收 VM 资源

---

## [v0.1.0] - 2025-06-15

### 🎉 初始版本

- 基于 CAMEL-AI 框架的多 Agent 协作架构
- Test Plan Agent: 自动生成测试计划
- Task Decomposition Agent: 任务分解与配置生成
- LB Ops Agent: 负载均衡操作执行
- SSH Client: 设备配置下发与命令执行
- 支持 LLM Gateway (本地代理) 与多模型路由
- 支持 JSON 任务文件驱动测试
- PowerShell 交互式菜单入口
