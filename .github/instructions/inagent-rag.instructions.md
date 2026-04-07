---

## description: "Use when modifying INAGENT RAG retrieval, knowledge routing, vector store, GraphRAG index, or any file under INAGENT/rag/. Covers safety constraints, architecture rules, and category whitelist patterns."

applyTo: "INAGENT/rag/**"

# INAGENT RAG 改动规范

## 硬性约束

- **不得重建向量库或 GraphRAG 索引**，除非用户明确要求。两者均为持久化存储，重建耗时且不可逆：
  - 向量库：`INAGENT/vector_store/qdrant/`
  - GraphRAG：`INAGENT/graphrag_index/`（rebuild 命令：`python -m INAGENT.scripts.init_graphrag --build`）
- **禁止使用 backup 数据**。`reference/backup/` 中的文件可能来自不同版本的源 PDF，与当前 `knowledge_base/input/` 中的 PDF 不对应。必须始终从 `input/` 中的 PDF 使用 auto_convert 重新生成。如果 auto_convert 输出不可用，向用户报告并等待指示，不得静默回退到 backup。
- **不要重新引入旧 4 层路由**（CLI/RULES/DESIGN/TEST 并发分发）。当前架构已统一为 `UnifiedRAGRetriever` + `category_whitelist` 过滤。
- **产品名称不得硬编码**。始终从 `get_product_name()` 读取（`INAGENT/utils/env_utils.py`）。

## 架构规则

检索路径（唯一正确路径）：

```
KnowledgeRouter.retrieve(query, mode)
  → MODE_TREE_STRATEGY[mode]  (knowledge_config.py)
  → UnifiedRAGRetriever.retrieve(query, category_whitelist=tree_levels)
      ├── GraphRAG (priority)
      ├── Vector (Qdrant)
      ├── Rerank (SiliconFlow BGE)
      └── Protocol boost
  + TestRulesEngine (test_write / test_review 模式专用)
```

**树层级体系**（由 `knowledge_linker.py` 在入库时标注到 `tree_position.tree_level`）：

- `leaf` — 直接匹配到 command tree 叶子节点
- `new_leaf` — LLM 判定需新增的叶子节点
- `branch` — 匹配到模块/功能层级分支
- `trunk` — 产品规格/设计层
- `root` — 架构/顶层设计

## 修改 knowledge_config.py 时

- `MODE_TREE_STRATEGY` 是唯一控制各模式检索范围的配置（值为树层级列表如 `["leaf", "branch"]`）。
- 旧 `MODE_CATEGORY_WHITELIST` 是 `MODE_TREE_STRATEGY` 的别名，保留向后兼容。
- `CATEGORY_TO_TREE_LEVEL` 映射旧 `document_category` 到树层级，供运行时迁移未重新入库的历史数据。
- 新增/修改 mode 的树层级时，同步检查 `unified_rag.py` 中 step 3.5 的过滤逻辑。
- 四个合法 mode：`explain` / `config` / `test_write` / `test_review`。

## 修改 knowledge_router.py 时

- `rules_engine` 仅在 `test_write` 和 `test_review` 模式下调用，其他模式不触发。
- `unified_rag` 单例由 `INAGENT/web/deps.py` 的 `get_unified_rag()` 提供；修改构造参数后需同步更新 `deps.py`。

## 修改 unified_rag.py 时

- `category_whitelist` 参数现在接收树层级列表（如 `["leaf", "branch"]`），由路由器传入。
- step 3.5 过滤优先读 `meta["tree_position"]["tree_level"]`，对旧数据通过 `CATEGORY_TO_TREE_LEVEL` 映射兼容。
- `retrieve()` 不自行决定白名单。
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


| 文件                            | 职责                                         |
| ----------------------------- | ------------------------------------------ |
| `rag/knowledge_config.py`     | mode → 树层级策略 (`MODE_TREE_STRATEGY`)        |
| `rag/knowledge_router.py`     | 路由器：tree_levels → UnifiedRAG + RulesEngine |
| `rag/unified_rag.py`          | GraphRAG + 向量 + Rerank + 协议加权              |
| `rag/graphrag_integration.py` | GraphRAG parquet 检索封装                      |
| `rag/rerank_retriever.py`     | SiliconFlow BGE reranker                   |
| `rag/test_rules.py`           | 确定性规则引擎（test 模式专用）                         |
| `rag/scoring_utils.py`        | 协议加权评分                                     |
| `web/deps.py`                 | 单例管理（修改签名时同步）                              |


