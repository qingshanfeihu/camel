---
description: "Use when modifying INAGENT RAG retrieval, knowledge routing, vector store, GraphRAG index, or any file under INAGENT/rag/. Covers safety constraints, architecture rules, and category whitelist patterns."
applyTo: "INAGENT/rag/**"
---

# INAGENT RAG 改动规范

## 硬性约束

- **不得重建向量库或 GraphRAG 索引**，除非用户明确要求。两者均为持久化存储，重建耗时且不可逆：
  - 向量库：`INAGENT/vector_store/qdrant/`
  - GraphRAG：`INAGENT/graphrag_index/`（rebuild 命令：`python -m INAGENT.scripts.init_graphrag --build`）
- **不要重新引入旧 4 层路由**（CLI/RULES/DESIGN/TEST 并发分发）。当前架构已统一为 `UnifiedRAGRetriever` + `category_whitelist` 过滤。
- **产品名称不得硬编码**。始终从 `get_product_name()` 读取（`INAGENT/utils/env_utils.py`）。

## 架构规则

检索路径（唯一正确路径）：
```
KnowledgeRouter.retrieve(query, mode)
  → MODE_CATEGORY_WHITELIST[mode]  (knowledge_config.py)
  → UnifiedRAGRetriever.retrieve(query, category_whitelist)
      ├── GraphRAG (priority)
      ├── Vector (Qdrant)
      ├── Rerank (SiliconFlow BGE)
      └── Protocol boost
  + TestRulesEngine (test_write / test_review 模式专用)
```

## 修改 knowledge_config.py 时

- `MODE_CATEGORY_WHITELIST` 是唯一控制各模式检索范围的配置。
- 新增/修改 mode 的文档分类时，同步检查 `unified_rag.py` 中 step 3.5 的过滤逻辑（保留无分类元数据的文档）。
- 四个合法 mode：`explain` / `config` / `test_write` / `test_review`。

## 修改 knowledge_router.py 时

- `rules_engine` 仅在 `test_write` 和 `test_review` 模式下调用，其他模式不触发。
- `unified_rag` 单例由 `INAGENT/web/deps.py` 的 `get_unified_rag()` 提供；修改构造参数后需同步更新 `deps.py`。

## 修改 unified_rag.py 时

- `category_whitelist` 由路由器传入，`retrieve()` 不自行决定白名单。
- `top_k_retrieval=50`（初始候选），`top_k_final=8`（最终输出），调整时需评估召回率/精度权衡。
- GraphRAG 结果与向量结果合并后去重（hash 级别），不要破坏去重逻辑。

## 共享单例（deps.py）

```python
get_knowledge_router()   # KnowledgeRouter，含 unified_rag 和 rules_engine
get_unified_rag()        # UnifiedRAGRetriever 单例
get_llm_model()          # BaseModelBackend 单例
```

修改任何 `__init__` 签名后，必须同步更新 `deps.py` 中对应的工厂函数。

## 关键文件索引

| 文件 | 职责 |
|------|------|
| `rag/knowledge_config.py` | mode → document category 白名单 |
| `rag/knowledge_router.py` | 路由器：whitelist → UnifiedRAG + RulesEngine |
| `rag/unified_rag.py` | GraphRAG + 向量 + Rerank + 协议加权 |
| `rag/graphrag_integration.py` | GraphRAG parquet 检索封装 |
| `rag/rerank_retriever.py` | SiliconFlow BGE reranker |
| `rag/test_rules.py` | 确定性规则引擎（test 模式专用） |
| `rag/scoring_utils.py` | 协议加权评分 |
| `web/deps.py` | 单例管理（修改签名时同步） |
