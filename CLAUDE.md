# INFOAGEN Project

## Overview

INAGENT: 基于 CAMEL-AI 框架的智能网络设备 (NSAE/InfosecOS) 自动化测试系统。包含两条主流水线:
- **Pipeline A**: 端到端测试执行 (Stage 1-9, Workforce Pipeline)
- **Pipeline B**: 测试用例评审 (Bug-to-Case ReviewPipeline)

## Project Structure

```
INAGENT/                    # 核心业务模块
├── review/                 # ReviewPipeline (多Agent Workforce评审)
│   ├── pipeline.py         # Workforce PIPELINE mode, fork/join 拓扑
│   ├── input_builder.py    # 构建 ReviewInput
│   ├── spec_decomposer.py  # 需求分解 (REQ提取)
│   ├── review_refiner.py   # Self-Refine 迭代改进
│   ├── review_evaluator.py # 混合评分 (程序化+LLM)
│   ├── adversarial_verifier.py # CLI命令验证
│   └── product_memory.py   # 产品架构记忆 (VectorDB 101模块)
├── rag/                    # RAG检索模块
│   ├── unified_rag.py      # 统一检索 (向量+GraphRAG+Rerank)
│   ├── cli_graph_store.py  # CLI关键词图 (5505 nodes)
│   ├── knowledge_router.py # 知识路由
│   └── knowledge_config.py # 文档分类体系
├── agents/                 # Agent模块
├── toolkits/               # Workforce Toolkit (FunctionTool)
├── data_tools/             # 数据处理 (auto_convert, ingest_validator)
├── scripts/                # 工具脚本
├── utils/                  # 通用工具
├── web/                    # Web Platform (FastAPI + SPA)
├── run_review.py           # 评审入口 (统一)
├── pipeline_runner.py      # 端到端测试入口
├── workforce_pipeline.py   # Workforce Pipeline (Stage 5-9)
└── knowledge_base/         # 知识库数据
camel/                      # CAMEL-AI框架 (上游fork, 有本地patch)
llm_gateway/                # LLM Gateway (多模型路由, :9000/v1)
```

## Key Entry Points

- `INAGENT/run_review.py` — 测试用例批量评审
- `INAGENT/pipeline_runner.py` — 端到端测试流水线
- `INAGENT/workforce_pipeline.py` — Workforce multi-agent pipeline
- `llm_gateway/start.py` — 启动 LLM Gateway
- 知识库会话宪章（含 **质检员**）：`INAGENT/docs/agents/sessions/07-quality-inspector.md`；实现：`INAGENT/agents/knowledge_quality_inspector_agent.py`；Cursor：`.cursor/rules/kb-session-quality-inspector.mdc`

## Development

- **Python**: 3.10 (`>=3.10,<3.15`)
- **Lint**: ruff (pre-commit hook, `--fix --exit-non-zero-on-fix`)
- **Format**: ruff-format
- **Type check**: mypy (`--namespace-packages -p camel -p test -p apps`)
- **Test**: pytest (`INAGENT/unit_tests/`)
- **Package manager**: uv (uv.lock)
- **Style**: PEP 8 (yapf based_on_style = pep8)

## Important Constraints

### Python 3.10 Gotchas
- f-string expressions 不能包含反斜杠 — 用中间变量
- f-string 分隔符必须用 ASCII `"` — Unicode smart quotes 会 SyntaxError

### CAMEL Framework Conventions
- `FunctionTool` name: 用 `t.get_function_name()` 不是 `t.name`
- `BaseToolkit.get_tools()` 是 abstract — 子类必须实现, 返回 `List[FunctionTool]`
- `ChatAgent(model=...)` 接受 backend instance 或 spec
- `single_agent_worker.py` max_iteration 已调至 30 (默认15不够)

### Code Patterns
- `pipeline.py` 用 `\u201c`/`\u201d` Unicode escapes 表示中文引号 — 是有意的, 不是bug
- `run_review.py` 包含 Chinese smart quotes (U+201C/U+201D) 作为字符串内容 — 是有意的
- Workforce `share_memory=True` 跨Agent同步 MemoryRecords

### Known Pre-existing Issues
- `INAGENT/unit_tests/test_auto_document_integration.py` 和 `test_data_integration.py` 有编码错误 (non-UTF-8 bytes)

## Environment

- `.env` 在 `INAGENT/.env` — 包含 API keys, 不要读取或修改
- `QDRANT_LOCAL_DIR` 环境变量控制向量库路径
- LLM Gateway 默认端口 9000

## Testing

```bash
# 单元测试
cd INAGENT && python -m pytest unit_tests/ -v

# 4-way pipeline 测试
python INAGENT/scripts/test_4way_pipeline.py
```

## Git Conventions

- 分支: `cursor/` 前缀用于 Cursor 开发分支
- 提交信息: `feat:` / `fix:` / `docs:` / `chore:` 前缀
- 主分支: `master`
