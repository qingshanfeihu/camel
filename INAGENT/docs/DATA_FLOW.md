# INAGENT 评审管线数据流全景

> 版本 v11.0 | 2026-03-31

本文档完整记录从原始知识库到最终评审报告的全部字段、格式和数据流转关系。

---

## 目录

1. [L0 — 原始知识库](#l0--原始知识库)
2. [L1 — CLI 关键词图谱](#l1--cli-关键词图谱)
3. [L2 — RAG 检索层](#l2--rag-检索层)
4. [L3 — 评审管线 (Review Pipeline)](#l3--评审管线)
5. [L4 — 输出与后处理](#l4--输出与后处理)
6. [全局数据流图](#全局数据流图)
7. [已知断点与 Bug](#已知断点与-bug)

---

## L0 — 原始知识库

### 1.1 mineru.json — 元数据规则词典

路径: `mineru.json` (项目根)

```json
{
  "product_modules": {
    "SLB":  ["SLB", "slb", "Server Load Balancing"],
    "AAA":  ["AAA", "aaa", "authentication"],
    ...
  },
  "protocol_types": {
    "HTTP":  ["HTTP", "http"],
    "HTTPS": ["HTTPS", "https"],
    "RIP":   ["RIP", "rip"],          // ⚠ 子串匹配 bug (见 §7.1)
    ...
  }
}
```

用途: `auto_convert.py` 文档入库时做关键词→元数据映射。

### 1.2 auto_convert.py — 文档转换与元数据提取

路径: `INAGENT/data_tools/auto_convert.py`

**base_meta 输出字段**:

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `product_module` | `str` | mineru.json `product_modules` 关键词匹配 | 如 `"SLB"` |
| `protocol_type` | `List[str]` | mineru.json `protocol_types` 关键词匹配 | 如 `["HTTP", "HTTPS"]` |
| `document_category` | `str` | `document_classifier.classify()` | 如 `"cli/reference"` |
| `source_file` | `str` | 文件路径 | 原始文件名 |
| `feature_name` | `str` | 从标题/内容提取 | 功能名称 |
| `section_title` | `str` | 章节标题 | 当前章节 |
| `step_type` | `str` | 规则匹配 | 步骤类型 (配置/验证/故障排除) |

**协议匹配逻辑** (v11.0 已修复):
```python
def _match_protocols_word_boundary(text: str, protocol_map: dict) -> list:
    import re
    found = []
    lower_text = text.lower()
    for proto, keywords in protocol_map.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw.lower())}\b", lower_text):
                found.append(proto)
                break
    return found
```
使用全词匹配 (`\b`)，避免 "Description" 中 "rip" 命中 RIP。

**输出格式** — 每个文档 chunk 的 JSON 结构:
```json
{
  "text": "INAGENT_META_JSON:{...}\n实际文本内容...",
  "metadata": {
    "product_module": "SLB",
    "protocol_type": ["HTTP"],
    "document_category": "cli/reference",
    "source_file": "xxx.pdf",
    "feature_name": "...",
    "section_title": "...",
    "step_type": "..."
  }
}
```

### 1.3 document_classifier.py — 文档分类

路径: `INAGENT/data_tools/document_classifier.py`

**DOCUMENT_CATEGORIES** (knowledge_config.py 定义):
```
cli/reference, app/reference,
spec/prd, spec/func_spec, spec/design,
test/test_template, test/test_strategy, test/test_list,
review/rules, review/bug_fix,
architecture/design
```

分类规则: 基于文件路径前缀 + 文本内容关键词 (如包含 "test case" → `test/*`)。
v11.0: 新增 `review/rules` 规则 (文件名: `review.*rule|评审.*规则|评审.*标准|checklist`, 内容关键词: `评审规则|评审标准|Review Rule|质量门|checklist`)。

### 1.4 GraphRAG 输入文档准备

路径: `INAGENT/rag/graphrag_adapter.py :: prepare_input_documents()`

每个文档 chunk 的 text 增加结构化前缀:
```
[分类: {document_category}] [模块: {product_module}] [协议: {protocol_type}]
[功能: {feature_name}] [步骤: {step_type}] [章节: {section_title}]
```

**GraphRAG 实体类型** (`DEFAULT_ENTITY_TYPES`, 16种):
`product_module, protocol, feature, design_knowledge, command, parameter, step_type, configuration, config_example, requirement, scenario, test_case, test_standard, business_state, state_transition, error_code_trigger`

### 1.5 向量数据库入库

路径: `INAGENT/workflow_config_generator.py` (第330-411行)

保留到 Qdrant 的 metadata 字段:
`product_module, protocol_type, document_category, source_file, feature_name, section_title, step_type`

---

## L1 — CLI 关键词图谱

### 2.1 图文件

路径: `INAGENT/knowledge_base/cli_keyword_graph.json`
统计: 5505 nodes, 25750 edges, 101 modules, 1279 keywords

### 2.2 节点类型 (4种)

#### module 节点

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `id` | str | extract 脚本 | 模块 ID (小写), 如 `"slb"` |
| `type` | str | 固定 | `"module"` |
| `label` | str | extract | 大写模块名, 如 `"SLB"` |
| `commands_count` | int | extract | 命令数量 |
| `keywords` | List[str] | extract | 源自 help_string + top-10 命令, 最多20个 |
| `help_string` | str | XML command tree | 模块帮助文本 |
| `protocol_stack` | List[str] | enrich | 推导协议栈, 按 L7>L4>L3>L2 排序 |
| `address_family` | List[str] | enrich | 地址族 (`IPv4`, `IPv6`) |
| `layer` | str | enrich | OSI 层级, 如 `"L3/L7"` |
| `interface_types` | List[str] | enrich | 管理接口 (`CLI`, `WebUI`) |
| `related_modules` | List[str] | enrich | 通过 shares_keyword 间接关联的模块 |
| `feature_tags` | List[str] | enrich | 功能标签 |

#### command 节点

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | 命令路径 (空格→下划线) |
| `type` | str | `"command"` |
| `label` | str | 原始命令文本 |
| `parent` | str | 所属模块 id |
| `description` | str | 命令描述 (截断100字) |
| `keywords` | List[str] | 从命令文本提取 |

#### operation_command 节点

同 command，额外字段:
- `actual_module`: str — 实际功能模块 id (如 `"aaa"`)

#### operation 节点 (仅3个: clear/no/show)

| 字段 | 说明 |
|------|------|
| `commands_count` | 该操作下命令数 (clear=772, no=839, show=1075) |

### 2.3 边类型 (5种)

| type | source→target | weight | 说明 |
|------|---------------|--------|------|
| `contains` | module→command | 1.0 | 模块包含命令 |
| `operation_of` | operation→operation_command | 0.9 | 操作类型→操作命令 |
| `has_operation` | module→operation_command | 0.7 | 模块关联的操作命令 |
| `parent_of` | command→command | 0.8 | XML 层级父子 |
| `shares_keyword` | node→node | 0.5 | **连接 command 节点** (非 module) |

### 2.4 enrich_cli_graph.py — 6个推导字段

| 推导字段 | 对照表大小 | 匹配方式 |
|----------|-----------|----------|
| `protocol_stack` | PROTOCOL_KEYWORD_MAP (28项) | 命令 keywords vs 协议关键词, L7>L4>L3>L2 排序 |
| `address_family` | ADDRESS_FAMILY_KEYWORDS (6项) | 命令 keywords 匹配 |
| `layer` | PROTOCOL_LAYER (31项) | 根据 protocol_stack 推导 |
| `interface_types` | — | 始终含 CLI; 若邻居含 webui 则加 WebUI |
| `related_modules` | — | shares_keyword 边→command→module 反查 |
| `feature_tags` | FEATURE_TAG_KEYWORDS (25项→16标签) | 命令 keywords 匹配 |

**PROTOCOL_KEYWORD_MAP 完整映射** (v11.0: 全大写无分隔符):
```
L7: http→HTTP, https→HTTPS, http2→HTTP2, ftp→FTP, sip→SIP, dns→DNS,
    smtp→SMTP, snmp→SNMP, ssl→SSL, tls→SSL, radius→RADIUS,
    ldap→LDAP, rtsp→RTSP, mqtt→MQTT
L4: tcp→TCP, udp→UDP, sctp→SCTP
L3: ip→IP, ipv4→IPV4, ipv6→IPV6, ip6→IPV6, icmp→ICMP, ospf→OSPF,
    bgp→BGP, rip→RIP, vrrp→VRRP, isis→ISIS
L2: arp→ARP, vlan→VLAN, lacp→LACP, stp→STP, lldp→LLDP
```

### 2.5 cli_graph_store.py — 运行时查询

路径: `INAGENT/rag/cli_graph_store.py`
模式: Singleton (`get_cli_graph_store()`)

**核心方法**:

| 方法 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `extract_subgraph(hints, max_nodes=60, max_depth=2)` | List[str] | `{nodes, edges, modules}` | BFS 子图, 使用 `contains`/`has_operation`/`parent_of` 边 |
| `format_for_prompt(subgraph, max_chars=1500)` | dict | str | 生成 `[产品功能树]` 文本 |

**format_for_prompt 输出格式**:
```
[产品功能树]
SLB 模块 (50 commands) — SLB commands:
  [协议: HTTP, HTTPS, TCP | 地址族: IPv4, IPv6 | 层级: L3/L4/L7 | 接口: CLI, WebUI | 关联模块: AAA, SSL 等30个 | 功能标签: load-balancing, persistence]
  ├─ slb virtual — Configure virtual server
  ├─ slb real — Configure real server
  └─ slb directfwd — Direct forward
关联关系:
  SLB ←[cookie]→ HTTP
```

### 2.6 下游消费者

| 消费者 | 使用方式 |
|--------|----------|
| `run_review.py` | `tech=[proto=... \| addr=... \| layer=...]` 注入到 module 行; 子图→CLI tree |
| `product_memory.py` | 遍历全部 module 节点→VectorDBBlock (语义检索) |
| `product_skill_toolkit.py` | 4个工具函数供 agent 运行时调用 |
| `input_builder.py` | `format_for_prompt()` 注入 product_knowledge |

---

## L2 — RAG 检索层

### 3.1 UnifiedRAG 检索

路径: `INAGENT/rag/unified_rag.py`

**输入参数**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | str | 必填 | 查询文本 |
| `top_k_retrieval` | int | 50 | 每路召回数 |
| `top_k_final` | int | 8 | 最终返回数 |
| `use_graphrag` | bool | True | 是否用 GraphRAG |
| `decomposition_result` | Dict | None | 含 `rag_queries` |
| `category_whitelist` | List[str] | None | 硬过滤白名单 |

**返回值**:
```python
Tuple[str, Dict[str, Any], Dict[str, Any]]
#  context(拼接文本), constraints(元数据聚合), decomposition_result(透传)
```

**缓存 key 构成**:
```python
(query, top_k_retrieval, top_k_final, use_graphrag,
 json.dumps(decomposition_result), document_category_filter, tuple(category_whitelist))
```

注意: `category_whitelist` 不同会导致缓存 miss，相同查询可能重复执行。

### 3.2 双路检索

```
                UnifiedRAG.retrieve()
                    ┌──────┴──────┐
            _fetch_graph()    _run_vector_queries()
                 │                    │
     GraphRAGIntegration      HybridRetriever.query()
      .local_context_build()     ┌────┴────┐
         (1 embedding,        Vector    BM25
          0 LLM)              Retriever  Retriever
                                 └────┬────┘
                                 RRF Fusion
```

#### GraphRAG 路径 — `_fetch_graph()`

调用 `graphrag_integration.local_context_build(query, top_k)`。

返回 4 种类型的 `GraphRAGSearchResult`:

| 来源 | text 字段 | score | `_graphrag_synthesized` | 说明 |
|------|-----------|-------|------------------------|------|
| **sources** | 原始文档文本 | 0.5 | **不设置** | GraphRAG text_units |
| **entities** | 实体描述 | 0.3 | `True` | LLM 合成 |
| **relationships** | 关系描述 | 0.2 | `True` | LLM 合成 |
| **reports** | 社区报告摘要 | 0.4 | `True` | LLM 合成 |

#### 向量路径 — `_run_vector_queries()`

通过 `HybridRetriever.query(return_detailed_info=True)` 获取。

RRF 融合后每条结果:
```python
{
    "text": str,
    "rrf_score": float,          # vector_rrf + bm25_rrf
    "metadata": Dict[str, Any],  # 保留原始 Qdrant metadata
    "vector_rank": int,
    "bm25_rank": int,
}
```

### 3.3 统一文档格式 (合并后)

```python
{
    "text": str,                        # 文本内容
    "metadata": Dict[str, Any],         # 元数据
    "similarity score": float,          # 最终分数
    "_source_type": "graphrag"|"vector", # 来源标识
    "protocol_boost": float,            # 协议加权 (+0.05/-0.03/0.0)
    "category_boost": float,            # 分类加权
}
```

**分数读取优先级**: `similarity score` > `rrf_score` > `score` > `0.0`

### 3.4 category_whitelist 过滤

1. `_graphrag_synthesized=True` → **直接放行**
2. 精确/前缀匹配白名单 → 保留
3. 无分类文档 → 丢弃 (默认)
4. 其他 → 丢弃

**MODE_CATEGORY_WHITELIST** (knowledge_config.py):

| mode | 允许的 document_category |
|------|--------------------------|
| `test_review` | review/rules, review/bug_fix, test/test_list, spec/prd, spec/func_spec, spec/design, architecture/design, cli/reference, app/reference |
| `explain` | architecture/design, spec/design, spec/prd, spec/func_spec, app/reference, cli/reference |

### 3.5 constraints 输出

从最终文档的 metadata 聚合:
```python
{
    "product_modules": List[str],      # 命中的模块列表
    "protocol_types": List[str],       # 命中的协议列表
    "document_categories": List[str],  # 命中的分类列表
}
```

### 3.6 KnowledgeRouter

路径: `INAGENT/rag/knowledge_router.py`

**输入**: `query, mode, product_module, max_context_chars`

**输出**:
```python
{
    "context": str,               # 规则+RAG 拼接 (分隔符: "\n\n---\n\n")
    "rules_context": str,         # Rules 引擎上下文 (仅 test_write/test_review)
    "rag_context": str,           # RAG 检索上下文
    "constraints": Dict[str, Any],
    "category_whitelist": List[str],
    "layers_used": List[str],     # 兼容字段
}
```

### 3.7 知识库合并与混合向量刷新（编排顺序）

向量路径（§3.2）消费的是合并后的 `knowledge_base/reference/knowledge_base.json` 经嵌入写入的 Qdrant/BM25 索引，而不是单个 `{stem}.json`。

| 步骤 | 谁 | 行为 |
|------|-----|------|
| 写入分片 | 农民 `write_to_reference` | 只写 `reference/{stem}.json`，**不**合并、**不**刷向量 |
| 农场主回填 | 农民 `apply_fill_request(FillRequest[])` | 按 `target_block_id` 或 `target_node_id`/`entity_title` **批量**合并 chunk `metadata`（及可选骨架 `node_id`）；**不**合并、**不**刷向量 |
| 合并小文件 | `merge_knowledge_base(reference_dir, knowledge_base.json)` | 将 `reference/*.json` 聚合成检索用的 `knowledge_base.json` |
| 可选一键 | `KnowledgeFarmOwnerAgent.process_gap_entries(..., refresh_hybrid_vectors=True)` | 在 GraphRAG `reload()` **之后**，对**当时磁盘上**的 `reference/*.json` 执行上表合并，再 `refresh_hybrid_vector_index` |

**隐患（非数据污染类 bug）**：若 E2E 在「农场主且 `refresh_hybrid_vectors=True`」**之后**再执行 `write_to_reference`，则除非编排再次 `merge_knowledge_base` + `refresh_hybrid_vector_index`（或等价重建），混合检索仍看不到新 chunk。农场主路径**已**在开启该开关时前置合并，但**不能**替代「农民写分片 → 合并 → 刷向量」在时间与调用顺序上的完整闭环。

**推荐顺序**（与 `scripts/test_ircookie_e2e.py` 文档串一致）：农民 `write_to_reference` → `merge_knowledge_base` → 农场主 `process_gaps` / `process_gap_entries`（若需向量一致再开 `refresh_hybrid_vectors`）→ 若仍有农民回填写 reference，则再 merge + 刷新向量。

### 3.8 `hybrid_vectors_force` / `force_rebuild_vectors`（06 选型）

路径：`workflow_config_generator.initialize_rag_system`、`refresh_hybrid_vector_index`；农场主通过 `process_gap_entries(..., hybrid_vectors_force=...)` 传入。

| 目标 | 建议 | 代价 / 风险 |
|------|------|----------------|
| Qdrant 与合并后 `knowledge_base.json` **严格一致** | `force=True`（默认） | 全量重嵌 |
| **减少** Qdrant 写入次数 | `force=False` | 指纹未变时 **Qdrant 不更新**；BM25 仍会随当前 KB 刷新；仅改 `reference/*.json` 未改合并指纹时易 **向量滞后** |
| 按变更 **增量 upsert** 若干点 | **未实现**；由 **混合搜索（06）** 扩展 Hybrid/Qdrant API（如与 `block_id` 对齐） | 非农场主范围；见 `sessions/06-hybrid-search.md`、`sessions/04-farm-owner.md` |

---

## L3 — 评审管线

### 4.1 ReviewInput

路径: `INAGENT/review/input_builder.py`

```python
@dataclass
class ReviewInput:
    product_knowledge: str    # RAG 上下文 + CLI 功能树 + 产品架构
    test_cases_text: str      # 测试用例原文
    bug_title: str
    bug_description: str
    root_cause: str
    fixed_details: str
    regression_scope: str
    change_impact: str
    key_questions: List[str]  # LLM 生成的关键问题
    constraints: Dict         # RAG 约束 (protocol_types 等)
```

**product_knowledge 组装顺序**:
1. RAG 检索结果 (KnowledgeRouter/UnifiedRAG)
2. 功能关系视图 (额外一次 RAG 检索)
3. **CLI 功能树** (`format_for_prompt()` → `[产品功能树]` 段) ← **含 protocol_stack**
4. 产品架构背景 (architecture/design 检索)

⚠ `_extract_protocol_hint()` v11.0 已改用 `PROTOCOL_KEYWORD_MAP` (30 协议)，但仍仅用于 RAG 查询，**不传入 ReviewInput**。
⚠ `constraints` 从 UnifiedRAG 返回后写入 ReviewInput，但 SpecDecomposer **不使用此字段**（tech_context 从 CLI graph 独立获取）。

### 4.2 SpecDecomposer

路径: `INAGENT/review/spec_decomposer.py`

**输入** (v11.0 新增 `tech_context`):
```python
decompose(
    product_knowledge: str,     # ReviewInput.product_knowledge
    bug_context: str,           # title + root_cause + fixed_details
    change_impact: str,
    test_cases_text: str,
    tech_context: str = "",     # v11.0: CLI graph 自动生成的模块技术画像
)
```

**_DECOMPOSE_PROMPT 结构** (v11.0):
```
<product_knowledge>{product_knowledge}</product_knowledge>
<bug_context>{bug_context}</bug_context>
<change_impact>{change_impact}</change_impact>
<tech_context>{tech_context}</tech_context>
<test_cases>{test_cases_text}</test_cases>
```

v11.0 新增 prompt 约束 (Rule 8):
- 派生 inferred 需求时必须确认关联技术概念属于同一 OSI 层级
- 共享泛化术语（加密/安全/转发）但属于不同层级 → `source="inferred"`, `priority="low"`
- 约束通用化，不写死具体协议名

**输出 — traceability_matrix**:

每条 REQ:
```json
{
    "id": "REQ_001",
    "description": "需求描述",
    "source": "product_knowledge|bug_context|cli_reference|inferred",
    "source_evidence": "原文引用",
    "priority": "high|medium|low",
    "test_case_ids": ["#201", "#202"]
}
```

规则: `source="inferred"` + `priority="low"` 用于无法溯源的推理需求。
限制: 10-25 条 REQ。

### 4.3 ReviewPipeline (CAMEL Workforce)

路径: `INAGENT/review/pipeline.py`

**架构**:
```
fork(CoverageAnalysis, CLISyntax) → join(ReviewSynthesis)
```

#### Coverage Worker 系统 prompt 中的标签

| 标签 | 来源 |
|------|------|
| `<product_knowledge>` | ReviewInput.product_knowledge |
| `<test_cases>` | ReviewInput.test_cases_text |
| `<traceability_matrix>` | SpecDecomposer 输出 |
| `<bug_context>` | bug title/description/root_cause/fixed_details |
| `<data_isolation>` | 数据隔离规则 (禁止跨标签推理) |

**工具装备**:
- Coverage Worker: 11 tools (knowledge_toolkit 7 + product_skill_toolkit 4)
- CLI Syntax Worker: 5 tools (knowledge_toolkit 5)
- Synthesis Worker: 6 tools (knowledge_toolkit 6)

#### _OUTPUT_REQUIREMENTS (输出格式要求)

表格格式:
```
### 发现 N: 标题

| 项目 | 内容 |
|------|------|
| 涉及用例 | 模块X > 用例 #A, #B |
| 问题描述 | ... |
| 修改建议 | ... |
| 优先级 | High/Medium/Low |
```

#### Workforce 参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `task_timeout_seconds` | 700.0 | v11.0 修复: 必须超过 step_timeout (600s) |
| `share_memory` | True | 跨 agent 记忆同步 |

### 4.4 ProductMemory

路径: `INAGENT/review/product_memory.py`

VectorDBBlock 内容 = CLI graph 全部 101 个 module 的技术画像:
```
[SLB模块技术画像]
协议: HTTP, HTTPS, TCP, SSL/TLS
地址族: IPv4, IPv6
层级: L4/L7
接口: CLI, WebUI
关联模块: AAA, SSL, Health 等
功能标签: load-balancing, persistence, session-management
命令数: 50
帮助: Server Load Balancing commands
```

Agent 可通过语义检索获取相关模块画像 (如查询 "SLB cookie HTTP" → 返回 SLB 模块画像)。

### 4.5 ReviewEvaluator

路径: `INAGENT/review/review_evaluator.py`

| 维度 | 类型 | 说明 |
|------|------|------|
| `coverage_score` | PROGRAMMATIC | REQ gap ID 匹配 (traceability_matrix) |
| `structural_score` | PROGRAMMATIC | 关键词: 精简/删减/重组/合并 |
| `clarity_score` | PROGRAMMATIC | 关键词: 预期结果/模糊/可验证 |
| `specificity_score` | LLM | 严格对抗 prompt, cap 0.8 |
| `cross_cutting_score` | LLM | 严格对抗 prompt, cap 0.8 |

### 4.6 ReviewRefiner

路径: `INAGENT/review/review_refiner.py`

始终执行 1 轮 improve:
1. 初次 evaluate → baseline
2. improve (使用 traceability_matrix 和 evaluator 反馈)
3. 二次 evaluate → 测量 delta

### 4.7 AdversarialVerifier

路径: `INAGENT/review/adversarial_verifier.py`

对每条 finding 做事实核查:
- 输入: finding text + product_knowledge + test_cases
- 输出: PASS / FAIL + reasoning
- FAIL 的 finding 被标记但不自动删除

### 4.8 Toolkits

#### KnowledgeToolkit (7 tools)

路径: `INAGENT/toolkits/knowledge_toolkit.py`

| 工具 | 返回格式 | 说明 |
|------|----------|------|
| `search_knowledge` | str (RAG context) | UnifiedRAG 检索 |
| `search_test_cases` | str | 检索相关测试用例 |
| `search_rules` | str | Rules 引擎检索 |
| `get_literal_docs` | str | knowledge_base.json 全文 |
| `get_bug_context` | str | Bug 上下文 |
| `get_test_cases` | str | 测试用例全文 |
| `search_by_module` | str | 按模块名检索 |

#### ProductSkillToolkit (4 tools)

路径: `INAGENT/toolkits/product_skill_toolkit.py`

| 工具 | 输入 | 返回格式 | 说明 |
|------|------|----------|------|
| `query_module_tech_profile` | module_name | str (tech 画像文本) | 查 CLI graph module 节点 |
| `discover_cross_cutting_concerns` | module_names | str (横切面分析) | 多模块 tech 字段聚合 |
| `check_spec_constraints` | requirement | str (约束检查) | 对照规格约束 |
| `query_module_relationships` | module_name | str (CLI tree) | extract_subgraph + format_for_prompt |

---

## L4 — 输出与后处理

### 5.1 run_review.py — Global Audit

路径: `INAGENT/run_review.py`

**module 行格式**:
```
- SLB (tech=[proto=HTTP, HTTPS, TCP, SSL | addr=IPV4, IPV6 | layer=L4/L7])
```

**CLI tree section**: 直接嵌入 `format_for_prompt()` 输出。

### 5.2 Finding Parser (双格式)

支持两种格式:

**Bold 格式** (旧):
```
**涉及范围**: xxx
**问题或建议**: xxx
**依据**: xxx
**优先级**: High
```

**Table 格式** (新):
```
| 涉及用例 | xxx |
| 问题描述 | xxx |
| 修改建议 | xxx |
| 优先级 | High |
```

**字段别名映射**:
```python
field_aliases = {
    "scope":              ["涉及范围", "涉及用例", "范围", "Scope"],
    "issue_or_suggestion": ["问题或建议", "问题描述", "问题", "建议", "Issue", "Suggestion"],
    "modification":       ["修改建议"],
    "evidence":           ["依据", "证据", "Evidence"],
    "priority":           ["优先级", "Priority"],
}
```

`modification` 字段合并入 `issue_or_suggestion`。

### 5.3 后处理 (_strip_internal_markers)

5 阶段:
1. 删除 `<adversarial_verification>` 块
2. 删除 `[溯源校验摘要]` 块
3. 替换 `<product_knowledge>` 等标签为自然中文 (如 "产品知识文档")
4. 删除 `REQ_xxx` 内部 ID
5. 清理多余空行

### 5.4 最终输出文件

```
jobs/test_review/{Bug ID}/output_{version}/{Bug ID}_review/
  ├── review_List_Template.md     # 评审报告 (Markdown)
  ├── pipeline_result.json        # 管线完整结果 (JSON)
  └── debug_events.json           # 调试事件日志
```

---

## 全局数据流图

```
┌─────────────────────────────────────────────────────────────┐
│ L0: 原始知识库                                                │
│                                                              │
│ mineru.json ──→ auto_convert.py ──→ document_classifier.py   │
│    (protocol_types,    (base_meta:        (document_category)│
│     product_modules)    protocol_type,                       │
│                         product_module,                      │
│                         document_category)                   │
│         │                    │                               │
│         │          ┌────────┴────────┐                      │
│         │          ▼                 ▼                       │
│         │    Qdrant 向量库    GraphRAG Index                 │
│         │    (metadata 全保留)  (text 带结构化前缀)           │
└─────────┼──────────┼─────────────────┼──────────────────────┘
          │          │                 │
┌─────────┼──────────┼─────────────────┼──────────────────────┐
│ L1: CLI 关键词图谱  │                 │                      │
│         │          │                 │                       │
│ command_tree.xml ──→ extract_cli_keywords_and_graph.py       │
│ cli_content.json      │                                      │
│                       ▼                                      │
│              cli_keyword_graph.json (5505 nodes)             │
│                       │                                      │
│              enrich_cli_graph.py                             │
│                       │                                      │
│              +6 fields: protocol_stack, address_family,      │
│                         layer, interface_types,              │
│                         related_modules, feature_tags        │
└───────────────────────┼─────────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────────┐
│ L2: RAG 检索层         │                                     │
│                        │                                     │
│  ┌─────────────────────┤                                     │
│  │                     │                                     │
│  │    ┌────────────────┴───────────┐                        │
│  │    │ UnifiedRAG.retrieve()      │                        │
│  │    │  ┌──────────┐ ┌──────────┐ │                        │
│  │    │  │GraphRAG   │ │Vector+   │ │                        │
│  │    │  │context_   │ │BM25 RRF  │ │                        │
│  │    │  │build()    │ │          │ │                        │
│  │    │  └────┬─────┘ └────┬─────┘ │                        │
│  │    │       └──── merge ──┘       │                        │
│  │    │            │                │                        │
│  │    │    whitelist filter          │                        │
│  │    │    reranker                  │                        │
│  │    │    protocol/category boost   │                        │
│  │    │            │                │                        │
│  │    │    (context, constraints,    │                        │
│  │    │     decomposition_result)    │                        │
│  │    └────────────┬───────────────┘                        │
│  │                 │                                         │
│  │    KnowledgeRouter                                        │
│  │      + Rules Engine                                       │
│  │                 │                                         │
│  │    constraints → ⚠ 被 input_builder 丢弃 (v11.0 缓解: CLI graph 独立注入) │
│  │    protocol_hint → ⚠ 仅用于 RAG 查询, 不传下游 (v11.0 扩展到 30 协议)   │
└──┼─────────────────┼────────────────────────────────────────┘
   │                 │
┌──┼─────────────────┼────────────────────────────────────────┐
│ L3: 评审管线        │                                        │
│  │                 ▼                                         │
│  │    InputBuilder.build()                                   │
│  │      → ReviewInput                                        │
│  │         .product_knowledge  ← RAG + CLI tree + 架构       │
│  │         .test_cases_text                                  │
│  │         .constraints        ← ⚠ SpecDecomposer 不读      │
│  │                 │                                         │
│  │    ┌────────────┤                                         │
│  │    │            ▼                                         │
│  │    │   SpecDecomposer                                     │
│  │    │     + tech_context (v11.0: CLI graph 自动生成)        │
│  │    │     → traceability_matrix (REQ_001..REQ_025)         │
│  │    │     ✅ v11.0 增加 OSI 层级约束                        │
│  │    │                                                      │
│  └────┤   ProductMemory (101 module profiles → VectorDB)     │
│       │   ReviewMemory (历史评审记录)                          │
│       │                                                      │
│       ▼                                                      │
│   CAMEL Workforce (share_memory=True)                        │
│     fork:                                                    │
│       CoverageWorker (11 tools, step_timeout=600s)           │
│       CLISyntaxWorker (5 tools, step_timeout=300s)           │
│     join:                                                    │
│       SynthesisWorker (6 tools, step_timeout=600s)           │
│              │                                               │
│       ReviewRefiner (1 improve round)                        │
│       AdversarialVerifier (fact-check per finding)           │
│              │                                               │
│       _strip_internal_markers() (5-phase cleanup)            │
│       Finding Parser (bold + table dual format)              │
└──────────────┼──────────────────────────────────────────────┘
               │
┌──────────────┼──────────────────────────────────────────────┐
│ L4: 输出      │                                              │
│              ▼                                               │
│   review_List_Template.md                                    │
│     发现格式:                                                 │
│     | 涉及用例 | 模块X > 用例 #A, #B |                        │
│     | 问题描述 | ...                 |                        │
│     | 修改建议 | ...                 |                        │
│     | 优先级   | High/Medium/Low     |                        │
│                                                              │
│   pipeline_result.json                                       │
│   debug_events.json                                          │
└──────────────────────────────────────────────────────────────┘
```

---

## 已知断点与 Bug

### 7.1 ~~⚠ protocol_type 子串误匹配~~ ✅ v11.0 已修复

**位置**: `auto_convert.py` 协议匹配逻辑
**表现**: `"RIP": ["rip"]` 用 `k.lower() in lower_text` 子串匹配，"Description" 中的 "rip" 命中 RIP
**修复**: v11.0 Phase A1 — `_match_protocols_word_boundary()` 全词匹配 (`\b`)
**遗留**: 已入库文档的 metadata 需要重跑 `auto_convert` 才能修正

### 7.2 ⚠ constraints 在 input_builder 中被丢弃

**位置**: `input_builder.py:367`
```python
ctx, _constraints, _ = future.result(...)
```
**表现**: `_constraints` (含 `protocol_types`) 赋值后从未使用
**影响**: 结构化协议信息无法到达 SpecDecomposer
**缓解**: v11.0 Phase B 通过 CLI graph 独立路径注入 tech_context，不依赖 constraints

### 7.3 ⚠ protocol_hint 未传入 ReviewInput

**位置**: `input_builder._extract_protocol_hint()`
**表现**: 提取了协议提示但仅用于 RAG 查询构建，不写入 ReviewInput
**缓解**: v11.0 Phase B1 扩大到 30 协议，RAG 查询效果改善；tech_context 走 CLI graph 独立路径

### 7.4 ~~⚠ SpecDecomposer 缺乏协议层级区分~~ ✅ v11.0 已修复

**位置**: `spec_decomposer.py :: _DECOMPOSE_PROMPT`
**表现**: prompt 无协议分层指令，LLM 无法区分传输层加密 (SSL/TLS) vs 应用层加密 (IRCookie)
**修复**: v11.0 Phase B2 — `tech_context` 参数 + OSI 层级约束 (Rule 8)

### 7.5 ~~⚠ task_timeout < step_timeout 冲突~~ ✅ 已修复

**位置**: `pipeline.py` `task_timeout_seconds=700` (was 240) vs `step_timeout=600`
**修复**: task_timeout_seconds 提升到 700.0

### 7.6 ⚠ INAGENT_META_JSON 错误元数据可能误导 LLM

**位置**: product_knowledge 文本中嵌入的 `INAGENT_META_JSON:{"protocol_type": ["RIP"]}` 行
**表现**: LLM 可能被错误的 protocol_type 元数据干扰
**关联**: 由 §7.1 子串匹配 bug 导致。代码已修复，但**已入库文档需要重跑 auto_convert 才能修正 metadata**

### 7.7 ℹ GraphRAG response_text 缺少 _graphrag_synthesized tag

**位置**: `graphrag_integration.py :: local_search()`
**表现**: 如果启用 `_include_graph_response_text`，response_text 没有 `_graphrag_synthesized: True` tag → 在非 bug 场景下被白名单过滤静默丢弃
**当前影响**: 低 (该开关默认 False)
