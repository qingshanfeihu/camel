---
description: "面向 INFOAGEN/INAGENT 项目的通用复用提示。用于基于仓库现有架构执行代码分析、实现、调试、评审或文档编写任务。"
argument-hint: "输入你的任务目标、问题描述或要修改的模块"
agent: agent
---

你正在 INFOAGEN 工作区内工作。请基于当前仓库的真实代码和文档完成下面的任务，而不是凭空假设。

## 用户任务

{{input}}

## 项目上下文

### 工作区分层

- 当前仓库由两层组成：
  - `INAGENT/`：主业务应用，绝大多数改动优先检查这里
  - `camel/`：上游 CAMEL-AI 框架，仅在主业务逻辑确实依赖框架实现时再深入

### INAGENT 的两条主流水线

1. **端到端测试执行流水线**
   - 入口：`INAGENT/pipeline_runner.py`
   - 阶段：Stage 1-4 负责测试计划、环境规划、配置生成、预检和环境部署
   - 执行：Stage 5-9 由 `INAGENT/workforce_pipeline.py` 的 CAMEL Workforce 负责

2. **测试用例评审流水线**
   - 统一入口：`INAGENT/run_review.py`
   - 核心：`INAGENT/review/pipeline.py`
   - 形态：Manager + 5 Worker 并行（Coverage / CLI Syntax / Spec / Load-Stress / Synthesis）

### 常见入口文件

- `INAGENT/run_review.py`：测试用例批量评审
- `INAGENT/pipeline_runner.py`：端到端测试主入口
- `INAGENT/workforce_pipeline.py`：阶段 5-9 执行
- `INAGENT/interactive_cli.py`：交互式配置生成/解释
- `INAGENT/web/app.py`：FastAPI Web 平台入口
- `INAGENT/web/deps.py`：RAG / LLM / 路由等共享单例

### RAG / 知识检索架构约束

- 统一检索路径是：
  - `KnowledgeRouter.retrieve(query, mode)`
  - `MODE_TREE_STRATEGY[mode]`
  - `UnifiedRAGRetriever.retrieve(..., category_whitelist=tree_levels)`
- `UnifiedRAGRetriever` 内部能力为：GraphRAG 优先、向量检索、Rerank、协议加权
- 合法 mode 只有：`explain`、`config`、`test_write`、`test_review`
- `test_write` 和 `test_review` 才会叠加 `TestRulesEngine`

### 关键约束

- 不要重建或清空持久化知识存储，除非任务明确要求：
  - `INAGENT/vector_store/qdrant/`
  - `INAGENT/graphrag_index/`
- 不要使用 `knowledge_base/reference/backup/` 中的备份数据替代正式数据
- 不要重新引入旧的 4 层路由（CLI/RULES/DESIGN/TEST 分发）
- 产品名不能硬编码，必须从 `INAGENT/utils/env_utils.py` 的相关 helper 获取
- 共享单例修改后，要同步检查 `INAGENT/web/deps.py`

### 代码与文档风格

- 优先修根因，不做表面补丁
- 改动尽量小且与现有风格一致
- 如果任务涉及行为变化，补充必要文档或说明
- 输出要具体，优先指出问题、风险、验证结果，不给空泛建议

## 执行要求

请按下面顺序工作：

1. 先判断任务属于哪条流水线或哪个子系统
2. 先读相关代码和文档，再下结论
3. 若需要修改代码，优先修改最靠近根因的模块
4. 若涉及 RAG、ReviewPipeline、Workforce 或 Web，主动检查与其上下游的调用关系
5. 完成后说明：
   - 改了什么
   - 为什么这样改
   - 如何验证
   - 还剩什么风险或前置条件

## 任务分类提示

- 如果任务是“配置生成/命令生成/功能解释”，优先看 `interactive_cli.py`、`workflow_config_generator.py`、`agents/`、`rag/`
- 如果任务是“测试执行链路”，优先看 `pipeline_runner.py`、`workforce_pipeline.py`、`toolkits/`、`utils/`
- 如果任务是“测试用例评审”，优先看 `run_review.py`、`review/`、`rag/knowledge_router.py`、`rag/test_rules.py`
- 如果任务是“Web 平台”，优先看 `web/app.py`、`web/routers/`、`web/deps.py`
- 如果任务是“知识库构建/文档集成”，优先看 `data_tools/`、`knowledge_base/`、`scripts/build_graphrag_from_graph.py`

## 输出要求

- 用中文回答
- 结论必须基于仓库中的真实实现
- 引用文件时给出具体文件路径
- 如果发现前提不成立、数据缺失或外部依赖未配置，要直接说明影响
- 如果是代码评审，优先列出按严重程度排序的问题和风险

现在开始处理“用户任务”中的内容。