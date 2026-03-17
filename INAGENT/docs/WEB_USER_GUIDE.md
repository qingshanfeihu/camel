# INAGENT Web Platform 使用手册

> 版本: v0.5.0 | 更新: 2026-03-06

## 1. 概述

INAGENT Web Platform 是 INAGENT 自动化测试系统的可视化管理界面，基于 **FastAPI + 原生 JavaScript SPA** 构建，提供知识库管理、RAG 问答、测试执行、结果查看和系统运维五大功能模块。

**技术栈**:
- 后端: FastAPI + Uvicorn（端口 8010）
- 前端: 原生 HTML/CSS/JS 单页应用（无框架依赖）
- 数据库: SQLite（会话/测试记录持久化）
- 通信: REST API + WebSocket（实时日志流）

## 2. 启动与访问

### 2.1 前置条件

| 依赖项 | 说明 |
|--------|------|
| Python 3.10+ | 运行环境 |
| FastAPI / Uvicorn | Web 框架 (`pip install fastapi uvicorn`) |
| LLM Gateway | 需运行在 `http://127.0.0.1:9000`（问答/配置生成依赖） |
| `.env` 文件 | `INAGENT/.env`，包含 Gateway URL、设备 IP 等配置 |

### 2.2 启动 Web 服务

```bash
# 方式1: 使用 uvicorn 直接启动
python -m uvicorn INAGENT.web.app:app --host 0.0.0.0 --port 8010

# 方式2: 通过 PowerShell 菜单 (推荐)
.\INAGENT\run_inagent_pipeline.ps1 -Mode web

# 方式3: 通过 PowerShell 交互菜单 (选择第 6 项)
.\INAGENT\run_inagent_pipeline.ps1
```

### 2.3 访问地址

启动成功后，通过浏览器访问:

- **前端界面**: http://127.0.0.1:8010/
- **API 文档 (Swagger)**: http://127.0.0.1:8010/docs
- **健康检查**: http://127.0.0.1:8010/api/health

---

## 3. 界面总览

Web 平台采用**五模块 Tab 布局**，顶部导航栏包含:

```
┌──────────────────────────────────────────────────────────────────┐
│  INAGENT Web Platform                      Gateway | RAG | Device │
├──────────────────────────────────────────────────────────────────┤
│ [1.产品功能学习] [2.产品知识展示] [3.产品测试功能] [4.测试结果展示] [5.系统管理] │
├──────────────────────────────────────────────────────────────────┤
│                        (模块内容区域)                              │
└──────────────────────────────────────────────────────────────────┘
```

**顶栏状态指示器**（每 12 秒自动刷新）:
- **Gateway**: LLM Gateway 连通状态（绿/红）
- **RAG**: 知识库 + 向量索引就绪状态
- **Device**: NSAE 设备 SSH 连通状态

---

## 4. 模块1: 产品功能学习（知识库管理）

该模块用于管理 INAGENT 的知识库，对应 `manage_database.py` 的可视化操作。

### 4.1 知识库统计

进入模块后，自动显示当前知识库状态:
- **Chunks**: 知识块总数
- **Modules**: 产品模块分类数量
- **Protocols**: 协议类型分类数量
- **GraphRAG**: GraphRAG 索引是否就绪

### 4.2 文档导入

1. 点击 **「选择文件」** 按钮，选择 PDF 文件
2. 点击 **「上传」** 按钮，PDF 将上传到 `knowledge_base/` 目录
3. 上传成功后在「已上传文档」表格中可见

> **注意**: 上传 PDF 后需执行「DB Create」或「DB Update」才会真正解析入库。

### 4.3 数据库操作

模块提供 5 个数据库管理按钮:

| 按钮 | 功能 | 说明 |
|------|------|------|
| DB Status | 查看知识库状态 | 显示当前知识库的 chunk 数量、模块分布 |
| DB Create | 全量创建 | 从 PDF 全量解析，生成 `knowledge_base.json`，自动触发 GraphRAG 构建 |
| DB Update | 增量更新 | 仅处理新增/变更的 PDF，追加到知识库 |
| DB Rebuild | 重建索引 | 清除旧数据后重新构建 |
| DB Delete | 删除知识库 | ⚠️ 危险操作，删除所有知识库数据（MinerU 中间文件将移入 `mineru_backup/`）|

操作日志实时显示在按钮下方的日志面板中。

### 4.4 知识条目浏览

在「知识条目浏览」区域可按条件检索知识块:

- **模块过滤**: 输入产品模块名（如 `slb`）
- **协议过滤**: 输入协议类型（如 `http`）
- **全文检索**: 输入关键字搜索知识块内容
- 点击 **「查询」** 显示匹配结果（每页 50 条）

表格列: ID | 模块 | 协议 | 步骤类型 | 内容片段

---

## 5. 模块2: 产品知识展示（RAG 问答）

该模块提供基于 RAG 的智能问答功能，支持多轮对话。

### 5.1 会话管理

- 左侧面板显示历史会话列表（按最近更新排序）
- 每个会话显示标题和消息数量
- 点击会话可加载历史消息
- 新对话时直接发送消息即可自动创建会话

### 5.2 问答模式

两种问答模式通过下拉菜单切换:

#### 功能解释模式 (`explain`)

- **用途**: 询问产品功能、配置概念、协议说明
- **流程**: 用户提问 → RAG 检索相关文档 → ChatAgent 基于文档生成解释
- **示例**:
  - "解释 NSAE 健康检查三步法"
  - "什么是源地址负载均衡？"
  - "HTTP 内容健康检查如何配置？"

#### 配置生成模式 (`config`)

- **用途**: 根据需求自动生成设备配置命令
- **流程**: 用户描述配置需求 → RAG 检索 → 任务分解 → LB Ops Agent 生成配置
- **输出**: 除问答内容外，还会显示:
  - **Config Commands**: 生成的配置命令列表
  - **Verify Commands**: 对应的验证命令列表
- **示例**:
  - "配置 HTTP 内容健康检查，VIP 为 10.0.1.100"
  - "创建源地址负载均衡，两个 real-server"

### 5.3 操作步骤

1. 选择问答模式（`功能解释` 或 `配置生成`）
2. 在输入框中输入问题
3. 点击 **「发送」** 或按回车
4. 等待 AI 回复（解释模式通常 5-15 秒，配置生成稍慢）
5. 配置生成模式下查看「Config Commands」和「Verify Commands」面板
6. 点击 **「清空窗口」** 可清除当前显示（不删除会话记录）

---

## 6. 模块3: 产品测试功能（Pipeline 执行）

该模块用于管理测试任务文件和启动端到端测试流水线。

### 6.1 测试任务管理

左侧面板显示所有测试任务文件（`.txt` 格式，位于 `INAGENT/jobs/` 目录）。

**创建任务**:
1. 在「任务文件名」输入框输入文件名（如 `demo.txt`）
2. 在文本区域编写测试任务内容
3. 点击 **「保存/覆盖」**

**编辑任务**:
1. 在左侧列表点击任务名
2. 右侧编辑器加载任务内容
3. 修改后点击 **「保存/覆盖」**

**删除任务**:
1. 选中要删除的任务
2. 点击 **「删除」** 按钮

### 6.2 任务文件格式

任务文件为纯文本，描述测试场景。示例:

```
测试场景: HTTP 内容健康检查
VIP 地址: 10.0.1.100
后端服务器:
  - web1: 10.0.1.10:80
  - web2: 10.0.1.20:80
健康检查方式: HTTP GET / 检查包含 "OK"
负载均衡算法: 轮询
```

### 6.3 执行测试

**执行选中任务**:
1. 在左侧列表勾选要执行的任务
2. 点击 **「执行选中任务」**

**执行全部任务**:
- 点击 **「执行全部任务」** 一键运行 `jobs/` 目录下所有 `.txt` 文件

**执行后**:
- 显示生成的 `run_id`
- 日志面板通过 **WebSocket** 实时显示 Pipeline 输出
- 流水线 9 个阶段的执行日志实时滚动

### 6.4 取消运行

- 点击 **「取消当前运行」** 终止正在执行的测试流水线
- 终止后数据库状态更新为 `cancelled`

### 6.5 WebSocket 实时日志

测试执行时，前端通过 WebSocket 连接到 `/api/testing/ws/{run_id}`，实时接收 Pipeline 的 stdout 输出。连接中断时自动重连（指数退避，最多 5 次）。

---

## 7. 模块4: 测试结果展示

该模块展示历史测试运行记录和详细报告。

### 7.1 结果统计

顶部统计卡片显示:
- **Total Runs**: 历史总运行次数
- **Running**: 当前正在运行
- **Success**: 成功次数
- **Failed**: 失败次数
- **Verdict 分布**: PASS/FAIL 统计

### 7.2 历史运行列表

表格列: Run ID | Job 文件 | 状态 | Verdict | 创建时间

- 点击任一行可查看该次运行的详细报告

### 7.3 运行详情

选中运行后显示:
- **Report (Markdown)**: 测试报告全文（对应 `*_test_report.md`）
- **Result (JSON)**: 结构化测试结果（对应 `*_test_result.json`）

报告包含:
- 9 阶段每阶段的执行输出
- 配置命令下发结果
- 流量验证结果
- 故障注入/恢复验证
- AI Verdict 综合判定（PASS/FAIL）

---

## 8. 模块5: 系统管理

该模块用于系统配置、连通性检测和日志查看。

### 8.1 环境状态

状态卡片网格显示 6 项系统检测:

| 指标 | 检测方式 |
|------|----------|
| LLM Gateway | HTTP 请求 `/v1/models` 接口 |
| NSAE Device | TCP 连接 SSH 端口 (22) |
| Test VM | TCP 连接 VM SSH 端口 |
| RAG Ready | 检查 `knowledge_base.json` + 索引文件存在 |
| GraphRAG Ready | 检查 `graphrag_index/output/` 下有 `.parquet` 文件 |
| KB Exists | 检查知识库 JSON 文件存在 |

### 8.2 详细探测

点击「Probe」按钮可获取更详细的连通性信息:

- **Probe Gateway**: 显示可用模型列表
- **Probe Device**: 显示 SSH 连接详情
- **Probe VM**: 显示 VM SSH 连接详情
- **Probe RAG**: 显示知识块数量
- **Probe GraphRAG**: 显示 Parquet 文件数量

探测结果以 JSON 格式显示在面板中。

### 8.3 关键配置

提供在线编辑关键环境变量的表单:

| 配置项 | 对应 .env 变量 | 说明 |
|--------|---------------|------|
| LLM Gateway URL | `LLM_GATEWAY_BASE_URL` | LLM 推理网关地址 |
| LLM Chat Model | `LLM_GATEWAY_CHAT_MODEL` | 默认对话模型名 |
| LB Device IP | `LB_DEVICE_IP` | NSAE 设备管理 IP |
| LB Username | `LB_USERNAME` | 设备 SSH 用户名 |
| VM Mgmt IP | `VM_MGMT_IP` | 测试虚拟机管理 IP |
| VM Username | `VM_USERNAME` | VM SSH 用户名 |

修改后点击 **「保存配置」**，配置将:
1. 写入 `.env` 文件（已有变量则更新，没有则追加）
2. 同步到当前进程环境变量
3. 自动刷新顶部状态指示器

### 8.4 日志中心

- 点击 **「刷新日志」** 获取 `knowledge_base/logs/` 目录下的日志文件列表
- 从下拉菜单选择日志文件
- 点击 **「查看日志」** 加载最近 300 行日志内容
- 日志内容显示在下方面板中

### 8.5 LLM Gateway 控制

该面板允许直接从 Web UI 管理本地 LLM Gateway 进程，无需切换终端窗口。

#### 操作按钮

| 按钮 | 功能 |
|------|------|
| **启动 Gateway** | 在后台启动 `llm_gateway/start.py`（监听 `0.0.0.0:9000`） |
| **停止 Gateway** | 终止正在运行的 Gateway 进程 |
| **刷新状态** | 查询 Gateway 运行状态、已加载模型列表和请求指标 |

#### 模式切换

从下拉菜单选择调度模式，点击 **「设置模式」** 即可生效:

| 模式 | 说明 |
|------|------|
| `balance` | 轮询负载均衡（默认） |
| `race` | 竞速模式，取最快响应 |
| `hybrid` | 混合模式，兼顾速度与均衡 |

#### 状态面板

刷新后显示 JSON 格式的状态信息，包含:
- `running_locally` — 是否由本 Web 进程托管
- `pid` — 子进程 PID
- `connected` — 是否可连通 Gateway API
- `health` — `/health` 接口返回
- `models` — 已注册模型列表（来自 `/admin/models`）
- `mode` — 当前调度模式
- `metrics` — 请求统计（来自 `/metrics/requests`）

#### 实时日志

点击 **「加载日志」** 拉取最近 150 行 Gateway 输出日志（仅限本次由 Web 进程启动的 Gateway 会话）。

> **注意**: 若 Gateway 已在外部独立启动（如直接运行 `python llm_gateway/start.py`），Web UI 仍可连接并读取状态/切换模式，但「启动 Gateway」和「停止 Gateway」按钮仅管理由 Web 进程启动的实例。

#### 对应 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/system/gateway/status` | 查询运行状态 |
| POST | `/api/system/gateway/start` | 启动 Gateway 子进程 |
| POST | `/api/system/gateway/stop` | 停止 Gateway 子进程 |
| POST | `/api/system/gateway/mode` | 切换调度模式 `{"mode":"balance"}` |
| GET | `/api/system/gateway/logs?tail=100` | 获取最近 N 行日志 |

---

## 9. API 参考

所有 API 端点均以 `/api/` 为前缀。启动服务后可通过 Swagger UI (`/docs`) 查看完整文档。

### 9.1 知识库管理 (`/api/knowledge/`)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/stats` | 知识库统计 |
| GET | `/chunks?offset=0&limit=50&module=&protocol=&search=` | 知识条目浏览 |
| POST | `/db` | 数据库操作 (body: `{"action": "create\|update\|rebuild\|delete\|status"}`) |
| POST | `/upload` | 上传 PDF 文件 |
| GET | `/documents` | 已上传文档列表 |
| DELETE | `/documents/{filename}` | 删除已上传文档 |

### 9.2 知识问答 (`/api/chat/`)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/sessions` | 聊天会话列表 |
| DELETE | `/sessions/{id}` | 删除会话 |
| GET | `/sessions/{id}/messages` | 获取会话消息 |
| POST | `/ask` | 发送问题 (body: `{"message": "...", "mode": "explain\|config", "session_id": null}`) |

### 9.3 测试执行 (`/api/testing/`)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/jobs` | 任务文件列表 |
| GET | `/jobs/{name}` | 获取任务内容 |
| POST | `/jobs` | 创建任务 |
| PUT | `/jobs/{name}` | 更新任务 |
| DELETE | `/jobs/{name}` | 删除任务 |
| POST | `/run` | 启动测试 (body: `{"job_files": ["a1.txt"]}`) |
| POST | `/cancel/{run_id}` | 取消运行 |
| WS | `/ws/{run_id}` | 实时日志流 |

### 9.4 测试结果 (`/api/reports/`)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/runs?status=&limit=50&offset=0` | 历史运行列表 |
| GET | `/runs/{id}` | 运行详情 |
| GET | `/summary` | 结果统计汇总 |
| GET | `/files` | 报告文件列表 |
| GET | `/files/{filename}` | 报告文件内容 |

### 9.5 系统管理 (`/api/system/`)

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/status` | 系统状态检测 |
| GET | `/probe/{target}` | 详细探测 (target: `llm_gateway\|nsae_device\|test_vm\|rag\|graphrag`) |
| GET | `/env` | 获取环境配置 |
| PUT | `/env` | 更新环境配置 |
| GET | `/logs` | 日志文件列表 |
| GET | `/logs/{filename}?tail=200` | 查看日志（最后 N 行） |
| GET | `/gateway/status` | Gateway 运行状态（pid / connected / health / models / mode / metrics） |
| POST | `/gateway/start` | 启动本地 Gateway 子进程 |
| POST | `/gateway/stop` | 停止本地 Gateway 子进程 |
| POST | `/gateway/mode` | 切换 Gateway 调度模式（body: `{"mode":"balance\|race\|hybrid"}`） |
| GET | `/gateway/logs?tail=100` | 获取 Gateway 输出日志（最后 N 行） |

---

## 10. 常见问题

### Q1: 启动后访问 http://127.0.0.1:8010 页面空白？

检查:
1. 确认 `INAGENT/web/static/` 目录下有 `index.html`、`app.js`、`styles.css`
2. 查看终端是否有 FastAPI 启动错误
3. 尝试访问 `/api/health` 确认后端正常

### Q2: 问答功能返回"处理失败"？

检查:
1. LLM Gateway 是否已启动（顶栏 Gateway 状态是否为绿色）
2. 知识库是否已创建（`knowledge_base/reference/knowledge_base.json` 是否存在）
3. 向量索引是否已构建（`function_structure_index.json` 是否存在）

### Q3: 测试执行一直没有日志输出？

检查:
1. `INAGENT/pipeline_runner.py` 是否存在
2. `.env` 中的设备 IP 和 VM IP 是否配置正确
3. 设备和 VM 的 SSH 连接是否正常（在系统管理模块中点击 Probe 检测）

### Q4: 数据库操作超时？

- DB Create / DB Rebuild 涉及 MinerU PDF 解析，处理大量 PDF 可能需要较长时间
- 默认超时为 600 秒（10 分钟）
- GraphRAG 构建超时为 900 秒（15 分钟）
- 建议通过命令行执行大批量操作: `python INAGENT/manage_database.py --action create`

### Q5: 配置保存后环境变量未生效？

- Web 端修改 `.env` 会同步到当前进程的 `os.environ`
- 但 **不会** 影响其他已运行的进程
- 如需所有组件生效，请重启 Web 服务

---

## 11. 文件结构

```
INAGENT/web/
├── app.py              # FastAPI 应用入口（路由注册、CORS、静态文件挂载）
├── deps.py             # 共享依赖管理（RAG/LLM/DB 单例初始化）
├── models.py           # Pydantic 请求/响应数据模型
├── inagent.db          # SQLite 数据库（自动创建）
├── routers/
│   ├── knowledge.py    # 模块1 路由: 知识库 CRUD + 文档上传
│   ├── chat.py         # 模块2 路由: RAG 问答 + 配置生成
│   ├── testing.py      # 模块3 路由: 任务管理 + Pipeline 执行 + WebSocket
│   ├── reports.py      # 模块4 路由: 历史报告查询 + 统计
│   └── system.py       # 模块5 路由: 环境配置 + 连通性探测 + 日志
└── static/
    ├── index.html      # 前端 HTML (五模块 Tab 布局)
    ├── app.js          # 前端 JavaScript (API 调用 + 交互逻辑)
    └── styles.css      # 前端样式 (暗色科技风格)
```
