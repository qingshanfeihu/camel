# RAG 与 GraphRAG 实施说明（统一版）

> 整合自 RAG优化实施建议、RAG_TEST_ANALYSIS、RAG_COMPARISON_ANALYSIS、docs/RAG_GRAPHRAG_METADATA_WORKFLOW。  
> GraphRAG 配置参考：[Detailed Configuration](https://microsoft.github.io/graphrag/config/yaml/)、[Manual Prompt Tuning](https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/)。

**文档版本**: v2.0  
**更新日期**: 2026-01-29

---

## 一、统一 RAG 机制（当前实现）

### 1.1 流水线：向量 + GraphRAG + Rerank + 协议加权

| 步骤 | 说明 | 实现 |
|------|------|------|
| 1. 向量检索 | 扩大候选池 | `hybrid_retriever.query(query, top_k=top_k_retrieval)` |
| 2. 可选 GraphRAG | 合并图检索结果 | `graphrag_retriever.local_search()` 与向量结果去重合并 |
| 3. Rerank | 重排序 | `reranker.query(query, retrieved_result, top_k=top_k_final)` |
| 4. 协议加权 | 动态调分后重排 | `rag_scoring_utils.compute_protocol_boost` 或 `unified_rag._apply_protocol_boost` |
| 5. 输出 | 上下文 + 约束 | 取 top_k 条拼 context，从 metadata 聚合 constraints |

### 1.2 统一入口

- **同步（Workflow 主用）**：`UnifiedRAGRetriever`（`unified_rag.py`）  
  - `retrieve(query, top_k_retrieval, top_k_final, use_graphrag, decomposition_result)` → `(context, constraints, decomposition_result)`  
  - Workflow 中优先使用；不可用时回退到内联 `_adaptive_rag_retrieval`。
- **异步（返回文档列表）**：`HybridGraphRAGRetriever`（`graphrag_integration.py`）  
  - 内部同样：向量 → 图合并 → Rerank → 协议加权，与统一流水线一致。

### 1.3 与 camel-ai 对比（camel-ai 项目 RAG 实现）

| 对比项 | camel-ai（rag_core/enhanced_rag_system） | INAGENT（当前） |
|--------|------------------------------------------|-----------------|
| **Rerank** | ✅ 先 `top_k*10` 候选再 rerank，分数回写 | ✅ 同策略，`UnifiedRAGRetriever` / `_adaptive_rag_retrieval` 均先扩大候选再 rerank |
| **协议加权** | 仅 HTTP/HTTPS 硬编码（`_compute_protocol_boost`） | ✅ 动态多协议（metadata + 文本），`rag_scoring_utils` + 泛化/惩罚 |
| **图检索** | 自建 KnowledgeGraph + GraphRAGRetriever（图扩展） | ✅ 微软 GraphRAG（local_search）+ 与向量结果合并 |
| **候选池** | `initial_top_k = search_top_k * 10` | ✅ `top_k_retrieval = top_k_rerank × RAG_RETRIEVAL_MULTIPLIER`（默认 10） |

**结论**：INAGENT 已覆盖 camel-ai 的 Rerank、协议加权、候选池策略，并采用动态协议与微软 GraphRAG，无遗漏项。

---

## 二、已完成的优化项

### 2.1 RAG 检索（P0）

| 优化项 | 实现位置 | 说明 |
|--------|----------|------|
| Reranker 正确调用 | `unified_rag.py` / `_adaptive_rag_retrieval` | 合并候选后调用 `reranker.query()`，再取 top_k |
| 10 倍候选池 | 环境变量 `RAG_RETRIEVAL_MULTIPLIER` | `top_k_retrieval = top_k_final × multiplier`（默认 50） |
| 动态协议加权 | `rag_scoring_utils.compute_protocol_boost`、`unified_rag._apply_protocol_boost` | 精确匹配加分、泛化/不匹配调权，支持多协议 |

### 2.2 微软 GraphRAG 与配置

| 组件 | 路径 | 作用 |
|------|------|------|
| GraphRAG 集成 | `graphrag_integration.py` | GraphRAGRetriever、HybridGraphRAGRetriever（含协议加权） |
| GraphRAG 适配器 | `graphrag_adapter.py` | SiliconFlow 版 settings、Prompts、输入准备 |
| **统一 RAG** | `unified_rag.py` | UnifiedRAGRetriever：向量+GraphRAG+Rerank+协议加权 单流水线 |
| 评分与协议 | `rag_scoring_utils.py` | compute_protocol_boost、图关系评分等 |
| Workflow | `workflow_config_generator.py` | `initialize_rag_system`；优先 `UnifiedRAGRetriever.retrieve()`，否则 `_adaptive_rag_retrieval` |
| GraphRAG 配置 | `graphrag_test/settings.yaml` | 按 [GraphRAG 文档](https://microsoft.github.io/graphrag/config/yaml/) 优化：chunks（type/encoding_model）、local/global/drift/basic_search（max_context_tokens、map_max_length 等） |

### 2.3 元数据与 Workflow

- **向量/Workflow**：首行 `INAGENT_META_JSON:{...}`，解析后用于 Rerank、协议加权与 constraints。
- **GraphRAG 输入**：knowledge_base 的 metadata 写入增强文本（如 `[模块:xx][协议:xx]`）与 title，GraphRAG 输出转为与向量一致的 `text`/`metadata`/`similarity score`，统一进 Rerank 与协议加权。
- **约束**：从最终文档 metadata 聚合 `product_modules`、`protocol_types`，与是否启用 GraphRAG 无关。

---

## 三、Workflow 流程

```
用户需求
  → 任务分解 Agent（scenario_id、required_steps、rag_queries）
  → RAG 检索（UnifiedRAGRetriever 或 _adaptive_rag_retrieval）
     向量 → [GraphRAG 合并] → Rerank → 协议加权 → context + constraints
  → LB Ops Agent 生成配置/验证命令
  → 返回 JSON
```

- **仅向量 + Rerank + 协议加权**：`USE_GRAPHRAG=false` 或不建索引时，不执行 GraphRAG 合并，其余不变。
- **向量 + GraphRAG + Rerank + 协议加权**：`USE_GRAPHRAG=true` 且索引可用时，在统一流水线中合并图结果。

---

## 四、环境与运行

### 4.1 环境变量（.env）

```env
USE_GRAPHRAG=false
RAG_RETRIEVAL_MULTIPLIER=10
# SILICONFLOW_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

### 4.2 GraphRAG 索引

1. 初始化：`python scripts/init_graphrag.py --init`  
2. 构建：`python scripts/init_graphrag.py --build`（需 `doc_local/reference/knowledge_base.json`）  
3. 状态：`python scripts/init_graphrag.py --status`  
4. 测试：`python scripts/init_graphrag.py --test "如何配置HTTP类型的SLB服务"`  

启用时设置 `USE_GRAPHRAG=true` 并确保已 `--init` 与 `--build`。

---

## 五、测试与验证建议

1. **回归**：`USE_GRAPHRAG=false`，运行 `test_rag_workflow_recall.py`，确认 Rerank、协议加权、候选池与设计一致。  
2. **GraphRAG**：`USE_GRAPHRAG=true` 且索引存在时，同一查询对比上下文是否增加依赖步骤/实体相关片段。  
3. **元数据**：确认参与 Rerank 的文档（含 GraphRAG 转出）具备正确 `product_module`、`protocol_type`，且 `constraints` 正确。

（详细测试记录与建议曾见 RAG_TEST_ANALYSIS.md，已整合至本文档。）

---

## 六、文件清单

| 文件 | 说明 |
|------|------|
| `unified_rag.py` | **统一 RAG**：UnifiedRAGRetriever，向量+GraphRAG+Rerank+协议加权 |
| `workflow_config_generator.py` | RAG 初始化、优先 UnifiedRAGRetriever、process_job 传参 |
| `graphrag_integration.py` | GraphRAGRetriever、HybridGraphRAGRetriever（含协议加权）、UnifiedRAGRetriever 再导出 |
| `graphrag_adapter.py` | SiliconFlow 配置、Prompts、输入准备、工作空间校验 |
| `rag_scoring_utils.py` | 协议加权、图关系评分、实体提取 |
| `siliconflow_rerank_retriever.py` | Rerank API 封装 |
| `graphrag_test/settings.yaml` | GraphRAG 配置（已按官方文档优化） |
| `scripts/init_graphrag.py` | GraphRAG 初始化/构建/状态/测试 |
| `.env` | USE_GRAPHRAG、RAG_RETRIEVAL_MULTIPLIER 等 |

---

## 七、结论

- **统一 RAG**：向量 + GraphRAG（可选）+ Rerank + 协议加权已收敛为 `UnifiedRAGRetriever` 与 `HybridGraphRAGRetriever` 两套入口，与 camel-ai 对比无功能遗漏，且协议为动态、多协议。  
- **GraphRAG**：采用微软 GraphRAG，配置已按 [config/yaml](https://microsoft.github.io/graphrag/config/yaml/) 与 [manual prompt tuning](https://microsoft.github.io/graphrag/prompt_tuning/manual_prompt_tuning/) 优化。  
- **元数据与 Workflow**：现有元数据与约束逻辑与当前方案兼容；启用 GraphRAG 仅需建索引并设置 `USE_GRAPHRAG=true`。
