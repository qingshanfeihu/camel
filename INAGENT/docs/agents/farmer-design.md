# KnowledgeFarmerAgent 设计文档

> 版本：v1.0 · 2026-04-03  
> 源文件：`INAGENT/agents/knowledge_farmer_agent.py`

---

## 1. 角色定位

**农民（Farmer）** 是知识加工流水线中位于「采购员（Procurement）」之后、「农场主（FarmOwner）」之前的执行层。

核心职责：
- 对采购员放行（`accept`）的裸 chunk 做结构化元数据补全
- 将 chunk 匹配到知识骨架（`knowledge_base.json`）叶节点
- 逐字段 diff，直接写入可对齐部分，将冲突 / 溢出 / 无匹配上报为 `SchemaGapEntry`
- 收到农场主下发的 `FillRequest` 后执行骨架回填

**不属于农民的工作**：
- 创建骨架新节点（农场主负责）
- 修改 GraphRAG 结构（农场主负责）
- 修改采购决策逻辑（采购员负责）
- 修改树拓扑 / `node_id`（树会话负责）

---

## 2. 主要数据类型

### 2.1 输入：`ChunkDecision`

来自 `KnowledgeProcurementAgent`，每条代表一个已决策 chunk。

| 字段 | 类型 | 说明 |
|------|------|------|
| `chunk` | `Dict` | 原始文档片段，含 `page_content` 与 `metadata` |
| `decision` | `ProcurementDecision` | 包含 `action`（accept/reject/staging）、`target_kb`、`confidence` |
| `source_file` | `str` | 来源文件路径 |
| `chunk_index` | `int` | 片段在文件中的序号 |

农民只处理 `action == "accept"` 的条目。

### 2.2 中间产物：`FarmResult`

```python
@dataclass
class FarmResult:
    chunk: Dict           # 已富化后的 chunk（含更新后的 metadata）
    source_file: str      # 来源文件
    block_id: str         # 去重唯一键（stem_idx_hash8）
    enriched_fields: List[str]      # 本次新增 / 更新的字段名列表
    schema_gaps: List[SchemaGapEntry]  # 需农场主裁决的 gap 列表
    matched_node_id: str  # 匹配到的骨架 node_id，无匹配时为空
```

### 2.3 Gap 类型：`SchemaGapEntry`

| `gap_type` | 触发条件 | 典型处理 |
|------------|---------|---------|
| `conflict` | 字段在骨架和 chunk 中均有值但不同 | 农场主裁决保留哪个 |
| `overflow` | 字段骨架无此列，或 chunk 无法匹配任何节点 | 农场主决定是否扩展骨架 |
| `new_entity_attribute` | 文本中检测到 `supports_override` 等新语义 | 农场主决定是否新增字段 |

### 2.4 农场主响应：`FillRequest`

```python
@dataclass
class FillRequest:
    entity_title: str          # 目标实体名称（用于索引回查）
    fill_fields: Dict[str, Any]# 要写入的字段与值
    action: str                # "update" / "discard"
    target_node_id: str        # 明确指定骨架 node_id（优先于 entity_title 匹配）
    source_evidence: str       # 支持依据（可选）
    new_node_template: Optional[Dict]  # 新节点模板（仅农场主填充，农民不处理）
```

---

## 3. 操作流程

```
采购员 ChunkDecision（accept）
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  cultivate_batch()                                                       │
│                                                                         │
│  对每条 chunk：                                                          │
│                                                                         │
│  Step 1  _step1_rules()         ← 零 LLM，纯规则提取                    │
│          · block_id = stem_idx_hash8                                    │
│          · word_count / has_code_block                                  │
│          · command_prefix（mineru.json 规则）                            │
│                                                                         │
│  Step 2  _structure_via_auto_convert()  ← 调 auto_convert 工具          │
│          Phase 1：规则提取（_extract_chunk_metadata）                   │
│          Phase 2：批量 LLM（可选，受 mineru.json llm-aided-config 控制） │
│          提取：product_module / protocol_type / intent /                │
│               config_mode / description / chunk_type 等                 │
│                                                                         │
│  Step 3  _refine_ac_meta_command_prefix()                               │
│          按 kb_index 对 auto_convert 输出的 command_prefix 做精确对齐     │
│                                                                         │
│  Step 4  _match_tree_node()     ← 四级匹配策略（见 §4）                  │
│          → tree_node_id                                                 │
│                                                                         │
│  Step 5  _detect_cross_refs()   ← 正则检测跨命令引用                    │
│          → command_refs[]                                               │
│                                                                         │
│  Step 6  diff / gap 分支                                                │
│          ├── 有匹配 → _diff_with_skeleton()                             │
│          │   · 可对齐字段 → updates{}                                   │
│          │   · 冲突 / 溢出 → SchemaGapEntry（conflict / overflow）      │
│          └── 无匹配 → overflow gap（unmatched_chunk）                   │
│              含 _find_nearest_skeleton_matches() 提供候选参考            │
│                                                                         │
│  Step 7  _detect_schema_gaps()  ← 检测 supports_override 等新属性语义  │
│          → new_entity_attribute gap                                     │
│                                                                         │
│  Step 8  _step3_index()         ← function_structure_index 字段补全    │
│                                                                         │
│  → FarmResult[]                                                         │
└─────────────────────────────────────────────────────────────────────────┘
        │
        ├─→  write_to_reference()     写入 reference/{stem}.json（block_id 去重）
        │                             写入 logs/{stem}.farmer_cache.json
        │
        └─→  emit_schema_gaps()       追加写入 gaps JSONL 文件 → 农场主收件箱

────── 农场主审批 ──────────────────────────────────────────────────────────

农民接收 FillRequest 列表（结构化 JSON）：
        │
        ▼
apply_fill_request()
        · 按 target_node_id（优先）或 entity_title → kb_index → slug 定位骨架节点
        · 将 fill_fields 写入匹配节点的 metadata
        · 仅更新已有节点；不创建新节点（节点创建由农场主负责）
        · 同时扫描 reference/*.json，对匹配 entity_title 的 chunk 写入字段
        · 返回更新节点数
```

---

## 4. 树节点匹配策略（`_match_tree_node`）

按优先级依次尝试：

| 优先级 | 方法 | 说明 |
|--------|------|------|
| 1 | `metadata.tree_node_id` | chunk 已由上游明确标注，直接采用 |
| 2 | `metadata.node_id` | 原始元数据已包含 node_id |
| 3 | `farmer_tree_alias.json` | 树会话维护的 slug→node_id 映射（只读） |
| 4 | `section_title` slug 化 → skeleton 精确匹配 | `_normalize_heading_to_node_slug` 转换 |
| 5 | `kb_index` 最长前缀匹配 | 按 `command_prefix` 在命令前缀索引中查找 |
| 6 | CLI 行提取后最长前缀匹配 | `_extract_cli_prefix_strings` 提炼 CLI 写法 |

匹配结果写入 `metadata.tree_node_id`，并计入 `FarmResult.matched_node_id`。

---

## 5. 工具与依赖

### 5.1 `auto_convert`（核心外部工具）

- 路径：`INAGENT/data_tools/auto_convert.py`
- 作用：通用文档元数据抽取，不感知 CLI 图谱
- 调用入口：`_structure_via_auto_convert()`
- Phase 1（无 LLM）：`_extract_chunk_metadata()`
- Phase 2（可选 LLM）：`_apply_llm_metadata_extraction_batch()`，受 `mineru.json` `llm-aided-config.metadata_extraction.enable` 控制

### 5.2 `mineru.json`（配置文件）

位于工作区根目录，供农民读取两类配置：

```json
{
  "metadata_rules": {
    "command_prefixes": {
      "前缀名": ["匹配字符串1", "匹配字符串2"]
    }
  },
  "llm-aided-config": {
    "metadata_extraction": {
      "enable": false
    }
  }
}
```

### 5.3 `knowledge_base.json`（骨架 / 只读索引）

- 路径：`INAGENT/knowledge_base/reference/knowledge_base.json`
- 农民以 `node_id` 为键构建内存索引（`_skeleton`）
- 农民可对已有节点 **原地更新 metadata**（通过 `apply_fill_request`）
- **不创建新节点**，不修改 `page_content` 结构

### 5.4 `function_structure_index.json`

- 路径：`INAGENT/knowledge_base/function_structure_index.json`
- 用于 Step 8（`_step3_index`）补全函数层级字段

### 5.5 `farmer_tree_alias.json`（可选）

- 路径：`INAGENT/knowledge_base/reference/farmer_tree_alias.json`
- 由树会话维护，农民只读
- 提供 `heading_slug → node_id` 映射，用于匹配策略第 3 级

### 5.6 `ChatAgent`（可选 LLM）

- 仅在 `model` 参数非 None 时初始化
- 角色固定为 `"Metadata Extractor"`
- 产品名注入系统提示（`product_name` 参数）

---

## 6. 持久化产物

| 文件 | 路径 | 内容 | 写入时机 |
|------|------|------|---------|
| `{stem}.json` | `knowledge_base/reference/` | 富化后的 chunk 列表（block_id 去重追加） | `write_to_reference()` |
| `{stem}.farmer_cache.json` | `knowledge_base/logs/` | 已处理 block_id → 时间戳映射 | `write_to_reference()` |
| gaps JSONL | 调用方指定路径 | 所有 `SchemaGapEntry` 序列化 | `emit_schema_gaps()` |
| `knowledge_base.json` | `knowledge_base/reference/` | 骨架叶节点 metadata 原地更新 | `apply_fill_request()` |

---

## 7. 农民 ↔ 农场主 交互协议

### 7.1 农民上报（gap 提交）

农民通过 `emit_schema_gaps()` 将结构化条目写入 JSONL，每行格式：

```json
{
  "gap_type": "overflow | conflict | new_entity_attribute",
  "entity_title": "命令名或节点 ID",
  "entity_description": "描述（可选）",
  "field_name": "发生问题的字段",
  "skeleton_value": "骨架中的原值（conflict 时）",
  "new_value": "chunk 中的新值",
  "column_name": "新属性列名（new_entity_attribute 时）",
  "column_dtype": "bool | str | ...",
  "evidence": "触发片段（前后各 40 字符）",
  "source_file": "来源文件",
  "timestamp": "ISO 8601",
  "nearest_matches": [{"node_id": "...", "similarity": 3}],
  "chunk_content": "chunk 前 500 字符"
}
```

### 7.2 农场主回复（`FillRequest` 列表）

农场主裁决后以相同结构化格式下发，必含 `target_node_id` 明确指定要更新的叶节点。  
示例（附录 C trunk 审批通过）：

```json
{
  "status": "APPROVED",
  "target_node_id": "slb_override",
  "fill_fields": {
    "supports_override": true,
    "override_scope": "virtual/group/policy"
  },
  "action": "update"
}
```

### 7.3 农民执行回填

`apply_fill_request()` 定位规则（按优先级）：

1. `FillRequest.target_node_id` 直接匹配骨架 `node_id`
2. `entity_title` → `kb_index`（command_prefix → node_id）查找
3. `entity_title` → slug 化后查找

找到节点后将 `fill_fields` 合并写入 `metadata`，并同步更新 `reference/*.json` 中匹配的 chunk。

---

## 8. 公开 API 一览

| 方法 | 入参 | 返回 | 说明 |
|------|------|------|------|
| `cultivate_batch(decisions)` | `List[ChunkDecision]` | `List[FarmResult]` | 主流程：结构化 → 匹配 → diff |
| `write_to_reference(results)` | `List[FarmResult]` | `Dict[str, int]` | 写 reference JSON，返回各 stem 新增数量 |
| `emit_schema_gaps(results, path)` | `List[FarmResult], Path` | `int` | 写 gap JSONL，返回 gap 总数 |
| `apply_fill_request(requests)` | `List[FillRequest]` | `int` | 执行农场主回填，返回更新节点数 |

---

## 9. 关键约束

- **幂等性**：`write_to_reference` 以 `block_id` 去重，重跑不产生重复
- **只读骨架拓扑**：农民不新增 `node_id`、不修改树结构
- **无状态骨架缓存**：`_skeleton` / `_kb_index` 在实例生命周期内缓存，跨批次重用同一 `KnowledgeFarmerAgent` 实例前请确认骨架未变
- **LLM 可选**：所有核心路径均可在无 LLM 状态下运行（降级为规则提取）
- **产品名动态**：始终通过 `product_name` 参数传入，不在代码中硬编码

---

## 10. 模拟验证

脚本：`INAGENT/scripts/sim_ircookie_farmer.py`

模拟了以 ircookie 相关 6 段 CLI 文档为例的完整流程：

| Stage | 内容 |
|-------|------|
| Stage 1 | 构造 6 条采购员 ChunkDecision |
| Stage 2 | `cultivate_batch` 结构化 + 匹配 + gap 产出 |
| Stage 3 | `write_to_reference` 写入临时隔离目录 |
| Stage 4 | `emit_schema_gaps` 写入 gap JSONL |
| Stage 5 | 模拟农场主审批（JSON 结构化决策），`apply_fill_request` 回填骨架叶节点 |

运行（隔离临时目录，不污染线上骨架）：

```bash
python INAGENT/scripts/sim_ircookie_farmer.py
```

---

## 11. 待实现 Agent 需求：采购员（Procurement Agent）

> 本节描述未来自动化实现采购员 Agent 时的设计要求，重点在于**结构化通信协议**与**容错机制**。

### 11.1 输入格式

采购员从上游（文档 Loader / auto_convert 管线）接收原始 chunk 批次，输入格式：

```json
{
  "batch_id": "cli_pdf_20260403_001",
  "source_file": "cli.pdf",
  "chunks": [
    {
      "page_content": "...",
      "metadata": {
        "source_file": "cli.pdf",
        "document_category": "cli/reference",
        "page_idx": 257
      }
    }
  ]
}
```

### 11.2 输出格式（标准成功报告）

采购员向农民输出一份完整的批次决策报告：

```json
{
  "status": "COMPLETE",
  "batch_id": "cli_pdf_20260403_001",
  "summary": {
    "total": 6,
    "accept": 5,
    "reject": 0,
    "staging": 1
  },
  "decisions": [
    {
      "chunk_index": 0,
      "action": "accept",
      "target_kb": "product",
      "confidence": 0.95,
      "reason": "CLI 命令文档，格式合规，内容密度高"
    },
    {
      "chunk_index": 5,
      "action": "staging",
      "target_kb": "product",
      "confidence": 0.62,
      "reason": "附录性内容，需农场主确认是否扩展骨架字段"
    }
  ]
}
```

### 11.3 输出格式（失败 / 不完整报告）

当采购员因工具不可用、外部依赖失败等原因无法完成决策时，**不得抛出致命异常**，须返回带状态码的结构化报告：

```json
{
  "status": "INCOMPLETE",
  "batch_id": "cli_pdf_20260403_001",
  "reason": "LLM 网关不可达，L2 价值判断无法执行，已降级至规则层",
  "partial_data": {
    "completed_indices": [0, 1, 2],
    "pending_indices": [3, 4, 5],
    "fallback_mode": "rule_only"
  }
}
```

| `status` 值 | 含义 |
|-------------|------|
| `COMPLETE` | 全部 chunk 决策完成 |
| `INCOMPLETE` | 部分完成，含失败原因与已完成索引 |
| `FAILED` | 致命错误，批次整体无法处理 |

### 11.4 错误反刍机制（Error Regurgitation）

采购员 Agent 内部必须实现**拦截层（Interceptor）**，禁止底层错误直接中断流程：

- **工具不可用**：当某工具（如 LLM 调用、文档解析器）不可用时，拦截层将错误包装为上下文消息退回给 Agent 自身：

  ```
  Error: Tool 'LLMGateway' is not available in your current sandbox.
  Available tools are: [RuleExtractor, MetadataParser].
  Please rethink your approach using only available tools.
  ```

- **降级重试**：Agent 收到上述提示后自动切换至规则提取路径（Phase 1），不等待 LLM 恢复

- **重试上限**：同一工具调用失败超过 **3 次**后，强制停止重试并将当前状态计入 `partial_data`，以 `INCOMPLETE` 状态上报

### 11.5 死循环防护

```python
MAX_ITERATIONS = 10      # 单批次最大决策轮次
MAX_TOOL_RETRIES = 3     # 单个工具最大重试次数
```

- 每轮迭代递增计数器；达到 `MAX_ITERATIONS` 后强制终止并输出超时报告：

  ```json
  {
    "status": "INCOMPLETE",
    "reason": "Exceeded max_iterations (10). Possible loop on chunk_index=3.",
    "partial_data": { "completed_indices": [0, 1, 2] }
  }
  ```

- 超时报告传回主控 Orchestrator，由 Orchestrator 决定是否重试或跳过该批次

---

## 12. 待实现 Agent 需求：农场主（Farm Owner Agent）

> 本节描述未来自动化实现农场主 Agent 时的设计要求，重点在于**结构化审批流程**与**容错机制**。

### 12.1 输入格式（农民上报的 gap 批次）

农场主从农民接收 `SchemaGapEntry` 列表，输入为 JSONL 文件或批次 JSON：

```json
{
  "batch_id": "gaps_20260403_001",
  "source": "emit_schema_gaps",
  "entries": [
    {
      "gap_type": "new_entity_attribute",
      "entity_title": "slb mode ircookie",
      "column_name": "supports_override",
      "column_dtype": "bool",
      "default_value": false,
      "evidence": "启用命令覆盖功能后，以下命令可以对同名配置进行直接覆盖",
      "source_file": "cli.pdf",
      "timestamp": "2026-04-03T20:07:31"
    }
  ]
}
```

### 12.2 输出格式（标准审批报告）

农场主对每个 gap 给出裁决，以结构化批次下发：

```json
{
  "status": "COMPLETE",
  "batch_id": "gaps_20260403_001",
  "decisions": [
    {
      "gap_type": "new_entity_attribute",
      "entity_title": "slb mode ircookie",
      "target_node_id": "slb_mode_ircookie",
      "action": "update",
      "fill_fields": {
        "supports_override": false
      },
      "rationale": "命令在附录 C 列表中，默认值为 false，待实际覆盖启用时更新"
    }
  ],
  "graphrag_ops": [
    {
      "op": "add_entity_columns",
      "entity": "slb mode ircookie",
      "columns": [{"name": "supports_override", "dtype": "bool", "default": false}]
    }
  ]
}
```

### 12.3 输出格式（失败 / 不完整报告）

农场主无法完成裁决时（GraphRAG 不可用、LLM 超时等），不得抛出致命异常：

```json
{
  "status": "INCOMPLETE",
  "batch_id": "gaps_20260403_001",
  "reason": "GraphRAG index not available. Cannot verify entity existence before writing.",
  "partial_data": {
    "approved": ["slb_mode_ircookie"],
    "pending": ["no_slb_mode_ircookie", "clear_slb_mode_ircookie"],
    "skipped_ops": ["add_entity_columns"]
  }
}
```

### 12.4 错误反刍机制

农场主 Agent 内部拦截层行为：

- **GraphRAG 不可达**：不写图，仅以 `INCOMPLETE` 报告告知 Orchestrator，等待 Orchestrator 重新调度
- **LLM 超时**：降级至规则裁决（基于 `farm_owner_overflow_denylist.json` 配置）
- **错误包装格式**（退回 Agent 上下文）：

  ```
  Error: Tool 'GraphRAGWriter' failed after 3 retries.
  Reason: Connection refused to parquet store.
  Available fallback: [RuleDenylist, SkipAndReport].
  Please rethink your approach using only available tools.
  ```

### 12.5 死循环防护

```python
MAX_GAP_ITERATIONS = 20   # 单批次最大裁决轮次
MAX_TOOL_RETRIES = 3      # 单个工具最大重试次数
SNAPSHOT_BEFORE_WRITE = True  # 写图前强制 snapshot_backup
```

- 达到 `MAX_GAP_ITERATIONS` 后：
  1. 停止当前批次剩余 gap 的处理
  2. 输出超时报告（`status: INCOMPLETE`）
  3. 已写入图的操作**不回滚**（以 `snapshot_backup` 为安全点）

### 12.6 农场主不做的事（硬边界）

农场主 Agent 在任何情况下都**不得**：

| 禁止行为 | 原因 |
|---------|------|
| 写入 chunk 内容到 `reference/*.json` | 属于农民职责（`apply_fill_request`） |
| 不经 `snapshot_backup` 直接写图 | 不可逆操作必须有安全点 |
| 创建新骨架节点后不通知农民 | 农民需要更新 `_skeleton` 缓存 |
| 向采购员上游反向注入决策 | 职责单向流动：采购 → 农民 → 农场主 |

---

## 13. 三角协作总览

```
文档 Loader
     │  原始 chunk 批次（batch JSON）
     ▼
KnowledgeProcurementAgent（采购员）
     │  ChunkDecision 列表（COMPLETE / INCOMPLETE 报告）
     │  ← 错误反刍 + 降级至规则层
     │  ← 死循环防护 MAX_ITERATIONS=10
     ▼
KnowledgeFarmerAgent（农民）        已实现 ✓
     │  FarmResult[] + SchemaGapEntry JSONL
     │  ← 骨架只读，不创建节点
     │  ← block_id 幂等去重
     ▼
KnowledgeFarmOwnerAgent（农场主）
     │  FillRequest 批次（COMPLETE / INCOMPLETE 报告）
     │  ← 写图前 snapshot_backup
     │  ← 错误反刍 + 死循环防护 MAX_ITERATIONS=20
     ▼
KnowledgeFarmerAgent.apply_fill_request()  已实现 ✓
     │  骨架叶节点 metadata 原地更新
     └→  reference/*.json chunk 字段同步
```

所有 Agent 间通信均采用**结构化 JSON**，`status` 字段是 Orchestrator 路由的唯一依据：
- `COMPLETE` → 继续下游
- `INCOMPLETE` → Orchestrator 决定重试策略或跳过
- `FAILED` → 告警，人工介入
