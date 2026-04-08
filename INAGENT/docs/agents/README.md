# 知识库 Agent 会话（五角色 + 混合搜索）

本目录为 **INAGENT 智能知识库** 拆分的独立实现会话提供「宪章」：**五角色**（树 / 农民 / 采购 / 农场主 / 销售员）各管一条职责链；**混合搜索** 专项管多路召回与融合链路。并行开发时按会话边界改代码，减少职责污染。

## 使用方式

1. 在 Cursor 中 **新开 Agent/Chat 会话**，将对应 `sessions/*.md` 全文粘贴为第一条用户消息；或
2. 编辑下方所列文件时，Cursor 会通过 `.cursor/rules/kb-session-*.mdc` 自动附带对应规则（可在规则面板手动启用）。
3. **农场主**：全仓库注入 `.cursor/rules/kb-session-farm-owner-anchor.mdc`（`alwaysApply: true`），便于上下文压缩后仍识别职责；详规见同目录 `kb-session-farm-owner.mdc` 与 [sessions/04-farm-owner.md](sessions/04-farm-owner.md)。

## 会话索引

| 角色 | 宪章文件 | 主要代码与数据 |
|------|-----------|----------------|
| 树 (Tree) | [sessions/01-tree.md](sessions/01-tree.md) | `rag/skeleton_index.py`、`scripts/*cli*`、`knowledge_base/cli_keyword_graph.json`、农民侧树匹配 |
| 农民 (Farmer) | [sessions/02-farmer.md](sessions/02-farmer.md) | `agents/knowledge_farmer_agent.py`、相关单测 |
| 采购 (Procurement) | [sessions/03-procurement.md](sessions/03-procurement.md) | `agents/knowledge_procurement_agent.py`、相关单测 |
| 农场主 (Farm Owner) | [sessions/04-farm-owner.md](sessions/04-farm-owner.md) | `agents/knowledge_farm_owner_agent.py`、GraphRAG 结构写入适配 |
| 销售员 (Salesperson) | [sessions/05-salesperson.md](sessions/05-salesperson.md) | `rag/knowledge_router.py`、`unified_rag.py`、`hybrid_knowledge_fusion.py`、`toolkits/knowledge_toolkit.py`、Web/API 检索面 |
| 混合搜索 (Hybrid) | [sessions/06-hybrid-search.md](sessions/06-hybrid-search.md) | `hybrid_knowledge_fusion.py`、`unified_rag.py`（并行/合并）、`knowledge_router._retrieve_unified`、EntityLink/Neo4j、`deps.get_hybrid_fusion`、兜底 hybrid/rerank |

**分工提示**：销售员会话偏 **产品化检索面**；混合搜索会话偏 **Fusion + UnifiedRAG + Router 降级链** 的实现细节。二者重叠时以 `06-hybrid-search.md` 为准做算法与契约，以 `05-salesperson.md` 为准做工具与 API 暴露。

## 共享契约（勿在未协调时破坏）

- `INAGENT/rag/knowledge_schema.py`：`SchemaGapEntry`、`FillRequest`、`FarmOwnerReport` 等
- `INAGENT/rag/knowledge_config.py`：`MODE_TREE_STRATEGY`（mode→`tree_level` 白名单）、`MODE_CATEGORY_WHITELIST`（同义别名）、`DOCUMENT_CATEGORIES` / `CATEGORY_TO_TREE_LEVEL`（兼容与推导）
- `INAGENT/docs/DATA_FLOW.md`：L0 chunk 与元数据字段约定

## 与 ARCHITECTURE 中「五层评审栈」的区别

`docs/ARCHITECTURE.md` 的 Layer 0–4 描述的是 **评审流水线**（数据 / 记忆 / 工具 / 质量 / 全局审计），与本目录的 **知识库角色会话** 是不同切片，文档中请勿混用术语。
