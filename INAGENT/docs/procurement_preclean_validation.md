# 采购预清理 A/B 与全流程验收口径

本文件用于执行「采购预清理统一规划」的验证，分两层：

1) A/B 对比口径（观察预清理前置效果）  
2) 唯一验收标准（4-way 全流程，必须通过）

## A/B 对比口径（实施阶段）

### A 组（基线）
- 不启用采购前置分级（或仅长度阈值）。
- 记录以下指标：
  - `reject_log.jsonl` 数量
  - `pending_review.jsonl` 数量
  - 采购 L2 实际评估 chunk 数量
  - 最终 accept 数量

### B 组（新策略）
- 启用采购机械层分级（reject/pending/pass）：
  - 高置信 frontmatter / 垃圾块：`reject`
  - 中置信 frontmatter / `title_content_mismatch`：`pending_review`
  - 其余：进入 L2
- 记录同上指标，并对比 A 组差异。

## 唯一验收标准（必须执行）

> 唯一通过条件：完整执行采购→农民→农场主→4-way 诊断全流程，并满足：
> 1) 向量与 GraphRAG 召回率 100%  
> 2) 节点 MD 无错误分类、无错误知识

### 执行步骤

1. 恢复原始 command tree 生成的树苗快照。  
2. 删除全部缓存（含 auto_convert、doc_local_reference、logs cache、farmer_cache）。  
3. 从采购开始导入 4 份文档：  
   - `cli_1-82.pdf`
   - `app_1-40.pdf`
   - `ustack设计架构V3.pdf`
   - `app_65-72.pdf`
4. 运行完整流程：采购 → 农民 → 农场主（含树更新、向量重建、GraphRAG reload）。  
5. 运行 4-way 并核验：
   - 原始 command tree 是否有结构问题
   - 向量检索与 GraphRAG 检索
   - CLI 随机 10 条：导入、树生成、向量、GraphRAG
   - APP(`app_1-40`) 随机 5 场景：导入、树生成、向量、GraphRAG
   - `ustack`：内容识别、树生成、向量、GraphRAG
   - `app_65-72`：导入、树生成、向量、GraphRAG
6. 导出树节点到 MD，逐项检查分类与知识归属。

### 结果记录模板

- run_id:
- snapshot_name:
- cache_cleared: yes/no
- docs_imported: yes/no
- commandtree_issue_found: yes/no (detail)
- vector_recall: xx%
- graphrag_recall: xx%
- cli_10_random_pass: xx/10
- app_5_random_pass: xx/5
- ustack_pass: yes/no
- app65_72_pass: yes/no
- md_node_check_pass: yes/no
- final_verdict: PASS/FAIL
