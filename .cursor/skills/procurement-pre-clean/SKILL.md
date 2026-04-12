---
name: procurement-pre-clean
description: >-
  Applies INAGENT procurement mechanical pre-clean (L0+L1) rules and documents how they
  align with auto_convert, farmer, and farm-owner filters. Use when editing
  KnowledgeProcurementAgent, chunk admission, reject_log reasons, data cleaning for RAG,
  or when the user mentions 预清理、采购机械层、垃圾块、chunk 准入.
---

# 采购预清理（机械层）Skill

## 在流水线中的位置

```text
原始文档 → auto_convert（解析/扫盘/前置页/空块）→ 采购 L0+L1 → L2 LLM → L3 元数据 → 农民 …
```

- **auto_convert**：文件级与解析级（排除 `reference`、`mineru_output`、`_layout.pdf`、缓存跳过整文件、空文本块等），并把 frontmatter 结果透传到 chunk metadata（默认 tag-only，不直接删页）。
- **采购机械层**（本 Skill）：**chunk 级**、**无 LLM**；统一实现于 `INAGENT/agents/procurement_pre_clean.py` 与 `INAGENT/utils/chunk_text_quality.py`。
- **农民**：对采购 **accept** 做结构化；`write_to_reference` 做 block 去重（不是预清理）。
- **农场主**：`classify_uncovered_chunks` 对无 `tree_level` 的块用 **同一套** `is_garbage_page_content` 发 `discard`；与采购 L0 互补（后置结构清理）。

## L0 + L1 规则（须与代码一致）

1. **L1 长度**：`page_content` / `text` strip 后 **&lt; 50 字符** → `reject`，`confidence=1.0`。
2. **L0 垃圾启发式**：`is_garbage_page_content`（过短、纯非字母数字、版式/声明单行）→ `reject`。
3. **frontmatter 分级**（读取 auto_convert 透传字段）：
   - 高置信（如 `frontmatter_confidence>=0.95`）→ `reject`
   - 其余命中 → `pending_review`
4. **质量信号分级**：`detect_quality_flags` 命中 `title_content_mismatch` → `pending_review`；命中 `extremely_short` 通常已被长度门槛覆盖。

## 修改规则时

1. 改 `chunk_text_quality.is_garbage_page_content` 时，确认农场主 `classify_uncovered` 与采购行为仍一致。
2. 改长度门槛时，只改 `procurement_pre_clean.MIN_CHUNK_CHARS` 并保持宪章 `03-procurement.md`、`.cursor/rules/kb-session-procurement.mdc`、`knowledge_procurement_agent.py` 模块头同步。

## 业界对齐（摘要）

- 预训练/ RAG 常见管线：**格式化 → 规则清洗 → 去重/安全 → 质量过滤**（如 InternLM2 文本清洗、Volcengine/聚类预处理类文章中的缺失与异常处理、RAG text-cleaning playbook 中的 boilerplate 剥离）。
- 本仓库将 **chunk 级规则清洗** 收敛到采购机械层，避免脏块进入 LLM 批处理。
