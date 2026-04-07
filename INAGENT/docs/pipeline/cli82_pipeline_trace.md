# cli82 全流程详细追踪文档

> 基于 `test_cli82_full_pipeline_e2e.py` 实际运行（2026-04-04 00:07~00:15）生成  
> 数据源：`cli_1-82.pdf` 82 页，通过 MinerU 离线预处理为 `cli_content_list.json`

---

## 0. 前置：MinerU 离线预处理（测试前已完成）

```
cli_1-82.pdf
   └─ MinerU (LocalMinerUReader / magic-pdf)
         ├─ PDF 解析 → 页面布局分析 → 文字/表格/图像块提取
         ├─ 输出: cli_content_list.json（12718 个 block，覆盖全 PDF）
         └─ 每个 block 字段: {type, text, page_idx, text_level, ...}
```

**重要**：此步骤与本次测试流程完全独立，`auto_convert.py` 主入口（`convert_file`）  
**未在本管线中被调用**，农民只调用了 auto_convert 的内部 `_extract_chunk_metadata`  
做元数据规则提取（见 §3.2），不做 PDF→文本的二次转换。

---

## 1. P0：加载 & 分块 (test_cli82_full_pipeline_e2e.py `phase0_load_and_snapshot`)

### 1.1 筛选

```python
blocks = [b for b in all_blocks if b.get("page_idx", 999) < 82]
# 12718 → 1892 blocks (pages 0~81)
```

页面内容分布：
| 页码 | 内容 |
|------|------|
| 0-3 | 目录 |
| 4 | 版权/商标声明 |
| 5-6 | CLI 使用说明（模式/权限） |
| 7-81 | 系统基本操作命令（OSPF/BGP/LLDP/VXLAN/…） |

### 1.2 分块（inline _chunk_blocks）

规则：
- `token_limit = 800`（约 3200 中文字，换算 rough_tokens = len/4）
- `min_chunk_tokens = 80`（过小则并入前一 chunk）
- 遇到 `text_level > 0` 或数字编号标题 → flush 当前 chunk，开新 chunk

**结果**：1892 blocks → **40 chunks**（平均 1457 tokens，最小 345，最大 3244）

每个 chunk 输出格式：
```json
{
  "page_content": "<合并文本>",
  "metadata": {
    "source_file": "cli_1-82.pdf",
    "document_category": "cli/reference",
    "section_title": "<标题>",
    "page_start": 7,
    "page_end": 12
  }
}
```

### 1.3 清理残留

```
reference/cli_1-82.json          → 删除（避免旧数据干扰去重）
logs/cli_1-82.farmer_cache.json  → 删除
logs/cli82_schema_gaps.jsonl     → 删除
```

---

## 2. P1：采购员三层筛查 (KnowledgeProcurementAgent)

### 2.1 整体流程

```
40 chunks
  ├─ Layer 1: 机械检查（无 LLM）
  │     规则：page_content 去空白后长度 < 50 → reject
  │
  ├─ Layer 2: LLM 批量判断
  │     按 source_file 分批（batch_size=30）发送给 LLM
  │     System prompt 注入：产品名 + CLI 树需求理解
  │     LLM 对每个片段返回：action / target_kb / confidence / reason / suggested_category
  │
  └─ Layer 3: 元数据合法性检查（无 LLM）
        - suggested_category 写入 metadata.suggested_value
        - 若 suggested_category 在 DOCUMENT_CATEGORIES 中 → 同时更新 metadata.document_category
```

### 2.2 LLM Prompt 结构（Layer 2）

```
[System] 你是熟悉 NSAE (InfosecOS) 负载均衡器 CLI 知识库采购员，判断片段是否应入库
[User]  来源文档：cli_1-82.pdf
        共 30 个片段，请逐一评估。
        [0] 目录 / 内容前500字：...
        [1] 命令行介绍 / ...
        ...
        请返回 JSON 数组 [{idx, action, target_kb, confidence, reason, suggested_category}, ...]
```

### 2.3 实际结果

| 批次 | chunks | accept | reject |
|------|--------|--------|--------|
| Batch 1 | 30 | 29 | 1 |
| Batch 2 | 10 | 10 | 0 |
| **合计** | **40** | **39** | **1** |

唯一 reject：Chunk 0（page 0，section="目录"）  
reason: "纯目录页，仅含标题和页码，无实质内容"

### 2.4 enrich_chunk_decision_for_farmer

```python
def enrich_chunk_decision_for_farmer(cd: ChunkDecision) -> ChunkDecision:
    # 将 LLM 的 suggested_value 写回 chunk.metadata
    meta["suggested_value"] = sug               # 始终写
    if sug in DOCUMENT_CATEGORIES:
        meta["document_category"] = sug         # 若合法则覆盖
    meta["source_file"] = cd.source_file        # 确保一致
```

39 个 accepted chunks 各自携带 `suggested_category="cli/reference"` → 写入 metadata。

### 2.5 日志写入

```
logs/cli82_procurement/reject_log.jsonl    → 1 条（page 0 TOC）
logs/cli82_procurement/staging.jsonl       → 0 条
```

---

## 3. P2：农民填肉 (KnowledgeFarmerAgent.cultivate_batch)

### 3.1 整体调用链

```
cultivate_batch(39 accepted ChunkDecision)
  ├─ _structure_via_auto_convert(raw_chunks)   → 39 个 ac_meta
  ├─ _load_skeleton()                          → knowledge_base.json 骨架索引
  └─ 遍历每个 chunk：
       ├─ _step1_rules()          → 无 LLM 规则字段
       ├─ _refine_ac_meta_command_prefix()     → 精修 command_prefix
       ├─ _match_tree_node()      → 匹配骨架 node_id
       ├─ _detect_cross_refs()    → 交叉引用检测
       ├─ _diff_with_skeleton()   → 骨架 diff（匹配时）/ overflow gap（未匹配时）
       ├─ _detect_schema_gaps()   → supports_override 等特殊属性
       └─ _step3_index()          → function_index 字段
```

### 3.2 auto_convert 的真实职责（重要说明）

**农民调用 auto_convert 的方式**：

```python
# knowledge_farmer_agent.py _structure_via_auto_convert
from INAGENT.data_tools.auto_convert import (
    _apply_llm_metadata_extraction_batch,
    _extract_chunk_metadata,   # ← 只导入规则提取函数
    _load_project_config,
)

for chunk in chunks:
    text = chunk.get("page_content") or chunk.get("text")
    base_meta = chunk.get("metadata", {})
    meta = _extract_chunk_metadata(text, base_meta)  # 输入已是文本，不处理 PDF
```

**`_extract_chunk_metadata` 做的事**：
1. `clean_text`：去除多余空白、控制字符
2. 关键词规则匹配（来自 `INAGENT/mineru.json` 的 `metadata_rules` 字段）：
   - `intent`：在文本中找到 configure/show/delete 等动词 → 赋值
   - `product_module`：找到 ospf/bgp/vlan/bridge 等模块关键词 → 赋值
   - `protocol_type`：找到协议关键词（OSPF/BGP/LLDP/VXLAN/…）→ 赋值列表
   - `command_prefix`：`command_prefixes` 规则按优先级匹配 → 赋值
   - `config_mode`：找到 config/enable/shell 模式词 → 赋值
3. **LLM 元数据增强**：默认 **关闭**（`llm-aided-config.metadata_extraction.enable = false`）

**结论**：auto_convert 在本管线中**不做 PDF 解析**，只做基于关键词的元数据标注。  
如果 mineru.json 的 `command_prefixes` 规则未能匹配当前 CLI 命令格式，  
`ac_meta.command_prefix` 可能为空 → 依赖后续 `_match_tree_node` 兜底。

### 3.3 _step1_rules（无 LLM）

```python
block_id = f"{stem}_{chunk_index}_{md5(content[:50])[:8]}"  # 去重 key
fields = {
    "block_id": block_id,
    "word_count": len(content.split()),
}
if _CLI_COMMAND_LINE_RE.search(content):    # 检测 "word <cli_token>" 模式
    fields["has_code_block"] = True
# 也做 command_prefix 匹配（与 ac_meta 互补）
```

### 3.4 _match_tree_node（树节点匹配，按优先级）

```
优先级 1：meta 或 ac_meta 中已有 tree_node_id / node_id → 直接用
优先级 2：对 section_title 做 normalize_heading_to_node_slug → 查 skeleton
           若无，查 farmer_tree_alias.json（heading_slug → node_id 映射表）
优先级 3：对 content 第一行做 slug 化 → 查 skeleton / alias
优先级 4：用 command_prefix → 查 kb_index（命令前缀 → node_id 索引）
优先级 5：从 content 中提取所有 CLI prefix 字符串 → longest_prefix_match 模糊匹配
```

**cli82 结果**：26/39 匹配（match_rate = 66.7%）  
13 个 unmatched → 产生 `overflow` gap。

### 3.5 _diff_with_skeleton（匹配后 diff）

对于 26 个匹配的 chunk：
- 比对 `_SKELETON_META_FIELDS`（command_prefix/document_category/func/…）
- 相同 → skip
- ac_meta 有新值、骨架为空 → 写入 `updates`（直接更新骨架节点）
- 冲突（product_module 值不同）→ 产生 `conflict` gap

**cli82 实际**：骨架字段大多已有，冲突极少 → gaps 主要来自 unmatched 的 overflow。

### 3.6 overflow gap（13 个 unmatched chunks）

```python
gaps.append(SchemaGapEntry(
    gap_type="overflow",
    entity_title="<command_prefix>",        # 如 "crontab enable"
    field_name="unmatched_chunk",
    entity_description="<ac_meta.description>",
    evidence=content[:200],
    nearest_matches=_find_nearest_skeleton_matches(content, skeleton),
))
```

`nearest_matches`：用关键词交集计算与骨架节点的相似度，返回 top 候选列表，  
供农场主在 `_process_overflows` 中决策是否创建新实体或合并到现有节点。

### 3.7 write_to_reference

```python
# 按 source_file.stem 分文件
# block_id 去重：读取已有 reference/cli_1-82.json，对比 cache
reference/cli_1-82.json   → 写入 39 个新 chunk（file 为空，全量写入）
logs/cli_1-82.farmer_cache.json → 记录已处理 block_id
```

每个 chunk 此时有完整 metadata：  
`block_id / word_count / has_code_block / command_prefix / tree_node_id / document_category / source_file / page_start / page_end / suggested_value / product_module / protocol_type / ...`

### 3.8 emit_schema_gaps

```python
# 把所有 FarmResult.schema_gaps 序列化为 JSONL
logs/cli82_schema_gaps.jsonl   → 208 条（overflow 为主）
```

每行字段：`gap_type / entity_title / field_name / entity_description / evidence / source_file / timestamp / nearest_matches / chunk_content`

---

## 4. P3：农场主处理 (KnowledgeFarmOwnerAgent.process_gap_entries)

### 4.1 入口

```python
owner = KnowledgeFarmOwnerAgent(graphrag_retriever=GraphRAGRetriever(...))
entries = [_entry_from_raw(line) for line in cli82_schema_gaps.jsonl
           if gap_type in ("new_entity","new_entity_attribute","conflict","overflow")]
# 208 条
report = owner.process_gap_entries(entries, refresh_hybrid_vectors=True)
```

### 4.2 处理分发

```python
conflict_entries   → _process_conflicts()      # 字段冲突裁决
overflow_entries   → _process_overflows()      # 未匹配 chunk 决策
new_entity_entries → _process_new_entities()   # 新命令实体添加
attribute_entries  → _process_attribute_gaps() # 新列（supports_override 等）
```

### 4.3 TreeContext 查询（performance fix：L1 缓存）

对每个 gap 条目：
1. 查 `CLIGraphStore.get_context(entity_title)` → 返回 `TreeContext`  
   包含：层级路径 / 父节点候选 / 骨架 artifact 状态 / enrich 快照
2. `_tree_context_cache[entity_title] = ctx`（同 batch 内复用）

### 4.4 FillRequest 生成

农场主对 overflow 和 conflict 条目：
- 查 GraphRAG 知识图（6814 实体、12238 关系）：检索与 entity_title 最相关的社区报告
- 规则可确定 → 直接生成 FillRequest（action=fill）
- 模糊 → 交 LLM 裁决 → LLM 返回 fill/discard/defer

**cli82 结果**：
- entities_added = 2（2 个骨架中未有的命令，农场主新增）
- fill_requests = 208（208 个 FillRequest，几乎全部生成补充内容）
- deferred = 0（无需人工确认的条目）

### 4.5 snapshot_backup

在处理前调用：
```python
self.graphrag.snapshot_backup(label="farm_owner")
# graphrag_index/output/snapshot_farm_owner_YYYYMMDD_HHMMSS/ ← 备份 parquet
```

### 4.6 graphrag.reload()

处理完成后刷新当前进程内图视图（加载最新 parquet 实体表）。

---

## 5. P4：农民回填 (KnowledgeFarmerAgent.apply_fill_request)

### 5.1 匹配规则

```python
for req in fill_requests:
    if req.action == "discard": continue
    target = req.target_node_id or req.entity_title
    # 遍历 reference/*.json 中每个 chunk：
    if chunk.metadata.tree_node_id == target or chunk.metadata.node_id == target:
        chunk.metadata.update(req.fill_fields)   # merge
        # 同时尝试更新 knowledge_base.json 骨架节点
```

### 5.2 命中文件

```
reference/cli_1-82.json      → matcher 覆盖该文件中的 39 个 chunk  
reference/knowledge_base.json → 骨架节点同步更新
```

### 5.3 常见 fill_fields 内容示例

```json
{
  "func": "ospf hello interval configuration",
  "description": "该命令用于配置 OSPF 设备接口的 hello 报文发送间隔",
  "scope": "config",
  "is_variant": true
}
```

**cli82 结果**：150 records updated（包含 reference/ + 骨架各 chunk 的合计）

---

## 6. P3 刷新触发：Knowledge Base 更新 → 混合向量检索重建

`process_gap_entries(..., refresh_hybrid_vectors=True)` 末尾自动执行：

### 6.1 merge_knowledge_base

```python
merge_knowledge_base(reference_dir, reference_dir / "knowledge_base.json")
# 扫描 reference/*.json（cli_1-82.json + 其他 stem 文件）
# 按 block_id 去重合并为统一的 knowledge_base.json
# 此时： cli_1-82 的 39 条已反映回填后的元数据
```

### 6.2 刷新 Qdrant 向量索引

```python
refresh_hybrid_vector_index(force=True)
# → initialize_rag_system(force_rebuild_vectors=True)
```

流程：
```
1. 检测到 knowledge_base.json SHA256 变更（或 force=True）
2. storage.clear() → 清空 Qdrant collection "workflow_rag"
3. load_knowledge_base(reference_dir) → 读取合并后 3585 chunks
4. _documents_to_elements()           → 转换为 Text elements（含 regex_metadata）
5. hybrid_retriever.process(elements, embed_batch=50)
   ├─ 分批调用嵌入模型（SiliconFlow text-embedding-v4, 1024 dim）
   ├─ 写入 Qdrant （本地持久化路径: AppData/Local/INAGENT/vector_store/qdrant）
   └─ 写入 rag_meta.json { knowledge_base_sha256: <newFP> }  ← 指纹缓存
6. build_bm25_only(elements)          → 内存 BM25 索引更新
7. GraphRAGRetriever 初始化（加载最新 6814 实体 / 889 社区）
```

**实际耗时（00:13:31 → 00:15:00）**：约 89 秒，嵌入 3585 张量。

### 6.3 混合检索读路径（供参考）

查询时：
```
UnifiedRAGRetriever.retrieve(query)
  ├─ GraphRAG LocalSearch（优先）
  │     entity match + community report 召回
  ├─ HybridRetriever.retrieve(query)
  │     ├─ Qdrant ANN 向量检索 → top N candidates
  │     └─ BM25 关键词检索 → top M candidates
  │     merge + dedup
  └─ SiliconFlowRerankRetriever 重排 → top K 精排结果
       返回给 KnowledgeRouter → 过滤 MODE_CATEGORY_WHITELIST
                               → 返回给 ChatAgent
```

---

## 7. P5：随机 10 命令验证

### 7.1 抽样

```python
rng = random.Random(42)
sample = rng.sample(matched_results, 10)  # 从 26 个匹配结果中随机抽 10
```

### 7.2 三项检查

| 检查 | 方法 | 通过条件 |
|------|------|----------|
| tree_exists | `CLIGraphStore.command_exists(cmd_prefix)` | tree 中存在该命令节点 |
| ref_match | reference/cli_1-82.json 中有 `tree_node_id == matched_node_id` 的 chunk | 文件中存在 |
| orig_match | content 关键词 ∩ 原始页面文字 / content 关键词 ≥ 0.3 | 内容与原文重叠率达标 |

`passed = tree_exists AND ref_match AND (orig_match or None)`

### 7.3 唯一 FAIL 分析

```
command_prefix: "crontab"
tree_node_id:   "vxlan_mode"   ← 错误匹配
orig_overlap:   0.258 < 0.30
```

**根因**：`crontab` 片段的 section_title 是 "crontab" 但 slug 化后未在骨架中找到  
→ 落到 first_line 匹配 → 匹配到 "vxlan_mode" 节点（first_line 恰好以 vxlan 相关词开头）  
→ orig_overlap 低（vxlan_mode 的参考内容与 crontab 原文无交集）  
**影响**：该 crontab 内容虽被写入 reference，但 tree_node_id 标注错误，  
回填时更新了 vxlan_mode 的 reference 条目而非 crontab 的。

---

## 8. P6：最终 VERDICT

| 指标 | 数值 | 阈值 | 结论 |
|------|------|------|------|
| procurement reject > 0 | 1 > 0 | ✓ | PASS |
| accept > reject | 39 > 1 | ✓ | PASS |
| farmer match_rate | 66.7% | ≥ 30% | PASS |
| verify pass/total | 9/10 = 90% | ≥ 70% | PASS |
| **VERDICT** | — | — | **PASS** |

---

## 9. 问题 & 待改进点

### 9.1 auto_convert 元数据产出不足

- 规则引擎（`mineru.json metadata_rules.command_prefixes`）匹配粗略  
  → 多数 chunk 的 `command_prefix` 由较后的 `_step1_rules` 或 `_match_tree_node` 补全  
- LLM 元数据增强默认关闭，对无规则覆盖的新模块（crontab/vxlan）无法提取精确前缀  
- **建议**：在 mineru.json 中扩充 `command_prefixes` 的覆盖范围，或启用 LLM 增强

### 9.2 crontab → vxlan_mode 错误匹配

- `_match_tree_node` 的 first_line slug 化优先级过高  
  当 crontab chunk 的 first_line 是乱序内容时，匹配到不相关的 vxlan_mode  
- **建议**：first_line 匹配前验证 orig_overlap ≥ 阈值，否则降级到 command_prefix 匹配

### 9.3 write_to_reference 早于 refresh_hybrid_vectors 的编排警告

- 当前 P2 `write_to_reference` 写入 `cli_1-82.json` 后，P4 又做 `apply_fill_request`  
  修改了同一文件，最终 `merge_knowledge_base` 在 P3 末尾**只调用一次**（全量合并）  
- 如果 P4 在 P3 的 `refresh_hybrid_vectors=True` 之后执行，向量库不反映 P4 变更  
  （当前顺序：P2→P3→P4，P3 末尾刷新，P4 的 apply 在刷新后 → 混合检索少了回填内容）  
- **建议**：P4 apply_fill_request 完成后，再追加一次 `merge + refresh_hybrid_vector_index`

---

## 附：关键文件路径

| 文件 | 说明 |
|------|------|
| `knowledge_base/mineru_output/cli/hybrid_auto/cli_content_list.json` | MinerU 预处理输出 |
| `knowledge_base/reference/cli_1-82.json` | 农民写入的 39 个结构化 chunk |
| `knowledge_base/reference/knowledge_base.json` | 合并骨架（所有 stem 合并） |
| `knowledge_base/logs/cli82_schema_gaps.jsonl` | 208 条 schema gap（overflow 为主） |
| `knowledge_base/logs/cli82_procurement/reject_log.jsonl` | 采购弃件 |
| `knowledge_base/logs/cli82_pipeline_report_20260404_001330.json` | 完整测试报告 |
| `~/AppData/Local/INAGENT/vector_store/qdrant/` | Qdrant 本地持久化 |
| `graphrag_index/output/` | GraphRAG 实体/关系/社区 parquet |
