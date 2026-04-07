# Workspace Guide for AI Agents

This workspace has two layers: **CAMEL-AI** (upstream framework in `camel/`) and **INAGENT** (custom application in `INAGENT/`). Most active development happens in `INAGENT/`.

---

## CAMEL-AI Framework (`camel/`)

CAMEL is a Python multi-agent framework. See [CONTRIBUTING.md](../CONTRIBUTING.md) and [docs.camel-ai.org](https://docs.camel-ai.org) for full details.

**Key patterns**:
- All agents inherit `BaseAgent` with `reset()` / `step()` methods
- Use `ModelFactory.create(model_platform=..., model_type=...)` — never instantiate model classes directly
- Tools follow OpenAI function-calling schema; use `@FunctionTool` or inherit `BaseToolkit`
- Absolute imports: `from camel.agents import ChatAgent`
- All public APIs require type hints; every `.py` file starts with the CAMEL Apache license header

**Build & test (CAMEL layer)**:
```bash
make format        # ruff + isort
make ruff          # lint
pytest test/       # unit tests (mock external deps)
```

---

## INAGENT Application (`INAGENT/`)

INAGENT is a network device automated-testing system for NSAE (InfosecOS) load balancers. It uses CAMEL-AI as its agent runtime.

See [INAGENT/README.md](../INAGENT/README.md) for the full directory map and changelog, and [INAGENT/docs/ARCHITECTURE.md](../INAGENT/docs/ARCHITECTURE.md) for system design.

### Entry Points

| Script | Purpose |
|--------|---------|
| `INAGENT/run_review.py` | Batch test-case review (Excel input → Markdown reports) |
| `INAGENT/pipeline_runner.py` | End-to-end test pipeline (Stages 1-4: planning/env/config/precheck) |
| `INAGENT/workforce_pipeline.py` | Workforce execution (Stages 5-9: run/verify/analyze) |
| `INAGENT/interactive_cli.py` | Interactive config generator with RAG |
| `INAGENT/web/app.py` | FastAPI web platform entry point |
| `INAGENT/run_inagent_pipeline.ps1` | PowerShell menu launcher |

### Architecture Layers

```
run_review.py / web API
       ↓
ReviewPipeline (review/pipeline.py)
  Stage 1: LLM planning → ReviewPlan JSON
  Stage 1.5: Knowledge acquisition via KnowledgeRouter
  Stage 2: Workforce (5 Workers: Coverage/CLI/Spec/Load → Synthesis)
       ↓
KnowledgeRouter (rag/knowledge_router.py)
  mode → MODE_TREE_STRATEGY → UnifiedRAGRetriever
  + TestRulesEngine (test_write / test_review modes only)
       ↓
UnifiedRAGRetriever (rag/unified_rag.py)
  GraphRAG (priority) → Vector (Qdrant) → Rerank (SiliconFlow BGE) → Protocol boost
```

### RAG System

- **Config**: `INAGENT/rag/knowledge_config.py` — `MODE_TREE_STRATEGY` maps each mode (`explain` / `config` / `test_write` / `test_review`) to allowed tree levels (leaf/new_leaf/branch/trunk/root)
- **Vector store**: persistent Qdrant at `INAGENT/vector_store/qdrant/` — do NOT wipe without intent
- **GraphRAG index**: `INAGENT/graphrag_index/` (parquet files); rebuild with `python -m INAGENT.scripts.build_graphrag_from_graph --embed` (zero LLM, direct from cli_keyword_graph.json; 5401 entities, 22985 edges, 100% KB coverage). Do **NOT** run `graphrag index` manually — it overwrites parquet and breaks referential integrity.
- **Knowledge base source**: `INAGENT/knowledge_base/reference/` (JSON files per document type)
- **Unified retrieval direction**: all modes go through `UnifiedRAGRetriever`; the old 4-layer CLI/RULES/DESIGN/TEST dispatch is removed

### Review Pipeline (v0.7+)

- Entry: `INAGENT/run_review.py` (merges former `run_test_review.py` and `run_bug_to_case.py`)
- Core: `INAGENT/review/pipeline.py` — 5-Worker Workforce pattern
- Input: Excel files in `INAGENT/jobs/test_review/`; optional bug profile JSON via `--bug-profile`
- Output: Markdown reports in `INAGENT/reports/`
- Knowledge scope map (internal to pipeline): `product → explain`, `test → test_review`, `command → config`

### Environment

All secrets and endpoints live in `INAGENT/.env`:
- `LLM_GATEWAY_BASE_URL` / `LLM_GATEWAY_API_KEY` — LLM proxy endpoint
- `INAGENT_PRODUCT_NAME` — injected into all LLM prompts (default: `NSAE (InfosecOS) 负载均衡器`)
- `LB_DEVICE_IP` / `VM_MGMT_IP` — device access for test execution

Get model/product helpers from `INAGENT/utils/env_utils.py` and `INAGENT/utils/llm_config.py`.

Shared singletons (RAG, LLM model, rules engine) are managed in `INAGENT/web/deps.py`; use `get_knowledge_router()` / `get_unified_rag()` / `get_llm_model()`.

### Tests

```bash
pytest INAGENT/unit_tests/
```

### Common Pitfalls

- **Do not rebuild the vector store or GraphRAG index** unless explicitly asked — they are persistent and expensive to rebuild
- **Never use backup data from `reference/backup/`** — backup files may come from a different source PDF (e.g., full `app.pdf` vs. partial `app_1-40.pdf`). Always regenerate from the actual PDFs in `knowledge_base/input/` using auto_convert. If auto_convert output is unusable, ask the user — do not silently fall back to backup files.
- **`run_test_review.py` / `run_bug_to_case.py` are deleted** — use `run_review.py`
- **KnowledgeRouter no longer dispatches by layer** — it routes through `UnifiedRAGRetriever` with tree-level filtering (`MODE_TREE_STRATEGY`); do not re-introduce the old 4-layer or document_category pattern
- **Product name is dynamic** — never hardcode "NSAE"; read from `get_product_name()`
- **`graphrag_workspace/` is renamed** to `graphrag_index/`; update any path references accordingly
