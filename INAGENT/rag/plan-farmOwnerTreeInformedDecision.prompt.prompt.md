# Plan: 农场主强壮 — TreeInformed Decision Engine v2

**核心思路**：农场主当前只做"盲裁"（只看 `nearest_matches` 阈值）。强壮后：每个裁决先查树→形成 `TreeContext`→映射到完整 action 分类→输出格式化 `FillRequest`。三层更新链（GraphRAG/Qdrant+BM25/SkeletonIndex）全部闭合。

---

## Phase 1 — Schema 扩展（`knowledge_schema.py`）

### 1.1 补 `OwnerAction` Literal 枚举（替代散落的字符串）

| action 值 | 语义 | 目标树层级 |
|-----------|------|-----------|
| `discard` | 丢弃，无任何写入 | — |
| `tree_create_leaf` | 新建叶节点（command/artifact） | 叶 |
| `tree_create_branch` | 新建枝节点（feature/sub-module） | 枝 |
| `merge_into_existing` | 挂载/合并到已有节点（GraphRAG+Skeleton） | 叶/枝 |
| `attr_tag_update` | 更新已有节点的 enrich 字段/标签 | 任意层 |
| `graphrag_col_update` | 更新 GraphRAG 实体自定义列值 | — |
| `skeleton_register` | 仅在 SkeletonIndex 注册 artifact | — |
| `needs_tree_session` | 结构变更超出农场主权限，挂起等待树会话 | 根/躯干 |
| `update` | 兼容旧值（等价 `graphrag_col_update`） | — |
| `create_slot` | 兼容旧值（等价 `tree_create_leaf`） | — |

### 1.2 扩展 `FillRequest`（新增字段全部有默认值，向后兼容）

```python
tree_level: str = ""           # "root"|"trunk"|"branch"|"leaf"|"none"
enrich_fields: Dict[str, Any]  # 各层 enrich key 全覆盖（见下方属性设计）
artifact_id: str = ""          # skeleton_register 时用
artifact_link: Optional[Dict]  # add_link 时的参数包
```

### 1.3 各层 enrich 字段全覆盖设计（写入 `enrich_fields` 时按层选取）

| 树层级 | enrich 键集 |
|--------|------------|
| 根（module） | `protocol_stack`, `address_family`, `layer`, `interface_types`, `related_modules`, `feature_tags`, `func`, `scope` |
| 枝（feature/branch） | 以上 + `trunk_layers`, `trunk_planes`, `knowledge_sources` |
| 叶（command/op） | `keywords`, `help_string`, `full_syntax`, `parameters`, `operations`, `actual_module` |
| GraphRAG entity | 自定义列（column_spec）+ `entity_type`, `source_id` |
| Skeleton artifact | `artifact_type`, `module_id`, `document_category`, `quality_score`, `link_type`, `face_type` |

### 1.4 新增 `TreeContext` dataclass

```python
@dataclass
class TreeContext:
    entity_title: str
    exists_in_tree: bool           # CLIGraphStore.command_exists() 精确命中
    tree_level: str                # "root"|"trunk"|"branch"|"leaf"|"unknown"
    matched_node_id: str           # 精确命中的 node_id
    hierarchy_prefix: str          # "SLB > Load Balancing Group > ..."
    trunk_info: Dict[str, List]    # layers/planes 来自 derive_trunk()
    branch_ids: List[str]          # 来自 get_branch_ids()
    parent_candidate_id: str       # 最佳父节点（新建时用）
    similar_commands: List[str]    # 模糊命中列表
    skeleton_artifact_exists: bool # SkeletonIndex.artifact_count(module_id) > 0
    skeleton_module_id: str        # resolve_module() 结果
    enrich_snapshot: Dict[str, Any] # 树上当前 enrich 字段快照
```

**涉及文件**：`INAGENT/rag/knowledge_schema.py`

---

## Phase 2 — 树查询层（`knowledge_farm_owner_agent.py`）

### 2.1 `__init__` 新增可选参数
- `cli_graph: Optional[CLIGraphStore] = None`
- `skeleton_index: Optional[SkeletonIndex] = None`
- **懒加载**：`_get_cli_graph()` / `_get_skeleton_index()` 内部分别从 `get_cli_graph_store()` 和全局 `SkeletonIndex` 取单例，**调用方无需传参**；显式传入则覆盖（便于测试 mock）

### 2.2 `_query_tree_context(entity_title) -> TreeContext`

调用顺序（**纯读，无副作用**）：

1. `cli_graph.command_exists(entity_title)` → `exists_in_tree`, `similar_commands`
2. 若命中 → `cli_graph.get_hierarchy_prefix(module_hint)` → `hierarchy_prefix` + 推断 `tree_level`
3. `cli_graph.derive_trunk(module_id)` → `trunk_info`
4. `cli_graph.get_branch_ids(module_id)` → `branch_ids`, 推断 `parent_candidate_id`
5. `skeleton_index.resolve_module(entity_title)` → `skeleton_module_id`
6. 若有 `skeleton_module_id` → `skeleton_index.artifact_count()` → `skeleton_artifact_exists`

树层级推断规则：
- node type = `"module"` → `"root"`
- 有 `trunk_info` + 有 `branch_ids` → `"trunk"`
- 有 parent_module 但无子 → `"branch"` 或 `"leaf"`（按 command type 判断）

**涉及文件**：`INAGENT/agents/knowledge_farm_owner_agent.py`

---

## Phase 3 — 决策逻辑升级（`knowledge_farm_owner_agent.py`）

### 3.1 `_process_new_entities` 升级

**决策顺序**：规则先行，规则无法判断时 → LLM 裁决。

```
对每个 new_entity：
  ctx = _query_tree_context(entity_title)
  ┌ exists_in_tree + artifact_exists       → merge_into_existing (仅加 GraphRAG 关系 + skeleton link)
  ├ exists_in_tree + no artifact           → merge_into_existing + skeleton_register
  ├ not exists + parent_candidate found    → tree_create_leaf (叶) 或 tree_create_branch (枝)
  │    → upsert_entities + reembed + skeleton_register
  ├ not exists + no parent                 → [规则无法判断] → LLM 裁决
  │    LLM 输入：TreeContext + gap entry → 输出 action + enrich_fields + reason
  │    · LLM → needs_tree_session         → report.deferred（超出农场主权限）
  │    · LLM → discard                    → 跳过
  │    · LLM → tree_create_leaf/branch    → 执行写入
  └ LLM 异常 / 置信度低                    → discard + 记入 report.errors
```

### 3.2 `_process_overflows` 升级（替换当前纯阈值逻辑）

**废除 hard-code 阈值**（如原来的 `similarity >= 0.9`）。规则处理确定性分支，模糊情况交 LLM。

```
对每个 overflow entry：
  若 field_name in denylist → discard (不变，黑名单是显式配置非 hard-code)
  ctx = _query_tree_context(entity_title)
  ┌ exists_in_tree:
  │   field_name 在该层 enrich 键集中      → attr_tag_update (规则确定)
  │   field_name 归属不明                  → [规则无法判断] → LLM 裁决
  │       LLM 输入：ctx + field_name + nearest_matches + chunk_content
  │       LLM 输出：action(tree_create_leaf/branch/attr_tag_update/discard) + enrich_fields
  ├ not exists:
  │   nearest_matches 明确有高相似合并目标  → LLM 确认是否 merge_into_existing
  │   有 parent candidate                  → LLM 裁决 create_leaf vs create_branch vs discard
  └ LLM 异常 / 无法判断                    → discard + 记入 report.errors
```

### 3.3 `_process_conflicts` 升级（LLM prompt 注入 TreeContext）

冲突裁决**天然是 LLM 决策**，TreeContext 作为背景注入 prompt 提升准确度。

在现有 LLM 裁决 prompt 中追加：
```
树层级：{ctx.tree_level}
层级路径：{ctx.hierarchy_prefix}
已有 enrich 字段快照：{ctx.enrich_snapshot}
父节点候选：{ctx.parent_candidate_id}
```
决策输出 JSON 增加 `tree_level`、`enrich_fields` 建议值字段，以及 `reason`（裁决理由，便于 audit）。

### 3.4 `_process_attribute_gaps` 补 skeleton_register

`add_entity_columns` 成功后，若有 `skeleton_module_id`，调用：
```python
skeleton_index.register_artifact(
    artifact_id=entity_title,
    artifact_type="graphrag_entity",
    module_id=ctx.skeleton_module_id,
    graphrag_entity_id=entity_title,
    title=entity_title,
)
```

### 3.5 `FarmOwnerReport` 新增 `deferred` 字段

```python
deferred: List[FillRequest] = field(default_factory=list)
# needs_tree_session 条目放此处，与 fill_requests 隔离，便于编排层识别需人工干预的条目
```

> **决策依据**：有脑子的农场主会把「超出自己权限、需要树会话介入」的条目单独隔离，而不是把它混入农民可执行的 `fill_requests` 中。`deferred` 是农场主判断力的外显——它知道自己不该做什么。

---

## Phase 4 — 更新链闭合（`knowledge_farm_owner_agent.py`）

在 `process_gap_entries` 的 `refresh_hybrid_vectors` 分支中，插入 merge 步骤：

```python
# 当前（有数据断层）：
refresh_hybrid_vector_index(force=hybrid_vectors_force)  # 读 stale knowledge_base.json

# 修复后：
from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base
reference_dir = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
kb_path = reference_dir / "knowledge_base.json"
merge_knowledge_base(reference_dir, kb_path)   # Step 1: 先同步合并
refresh_hybrid_vector_index(force=hybrid_vectors_force)  # Step 2: 再基于最新 KB 重建
```

三路更新链状态：

| 通道 | 修复前 | 修复后 |
|-----|--------|--------|
| GraphRAG parquet/LanceDB | ✅ 每次写入 | ✅ 不变 |
| GraphRAG 进程内视图 | ✅ `reload()` | ✅ 不变 |
| Qdrant + BM25 | ⚡ 读 stale KB | ✅ merge→rebuild |
| Skeleton artifacts | ❌ 无 | ✅ Phase 3 补齐 |

---

## Phase 5 — 宪章同步

- `04-farm-owner.md`：更新 `FillRequest` 契约段（列出完整 action taxonomy）+ 新增 `TreeContext` 到"依赖文档"
- `knowledge_farm_owner_agent.py` 模块头注释：同步 `__init__` 新参数 + `deferred` 字段说明
- `.cursor/rules/kb-session-farm-owner.mdc` 同步摘要

---

## 已决策

| # | 问题 | 决策 |
|---|------|------|
| 1 | `cli_graph` / `skeleton_index` 注入方式 | ✅ **懒加载**：`_get_cli_graph()` 从 `get_cli_graph_store()` 取全局单例；显式传入时覆盖（供测试 mock） |
| 2 | `needs_tree_session` 去处 | ✅ **`report.deferred`** 单独字段：农场主有脑子，知道哪些事超出自己权限，需要隔离而非混入农民执行队列 |
| 3 | 规则 vs LLM | ✅ **规则先行、LLM 兜底**：所有 `_process_*` 方法——规则能确定的走规则，规则无法判断的一律调 LLM；**禁止 hard-code 阈值作为唯一决策机制** |

## 待决策

- **提交顺序**：Phase 1（schema）→ Phase 2-3（决策层）→ Phase 4（更新链） 分三步，每步有独立测试通过点

---

## 涉及文件清单

| 文件 | Phase |
|------|-------|
| `INAGENT/rag/knowledge_schema.py` | 1 |
| `INAGENT/agents/knowledge_farm_owner_agent.py` | 2, 3, 4 |
| `INAGENT/docs/agents/sessions/04-farm-owner.md` | 5 |
| `.cursor/rules/kb-session-farm-owner.mdc` | 5 |
| `.cursor/rules/kb-session-farm-owner-anchor.mdc` | 5 |
