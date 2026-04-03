# CLI 知识库叶子节点结构说明

> 最后更新: 2026-04-03  
> 数据来源: `INAGENT/knowledge_base/reference/knowledge_base.json`  
> 生成脚本: `INAGENT/scripts/rebuild_cli_docs.py`  
> 图谱源文件: `INAGENT/knowledge_base/cli_keyword_graph.json`

## 概览

knowledge_base.json 共 **3576 个文档块**，由两部分组成：

| 来源 | 块数 | 说明 |
|------|------|------|
| 骨架 (rebuild_cli_docs.py) | 3570 | 从 cli_keyword_graph.json 的 XML 叶子节点自动生成 |
| 农民富化 (cli.json) | 6 | KnowledgeFarmerAgent 从 cli.pdf 提取并富化的 ircookie 相关块 |

骨架块覆盖 3570 个 CLI 命令（含 set/no/show/clear 变体），是知识库主体。

### 变体分布

| 类型 | 数量 | node_id 前缀 | is_variant |
|------|------|-------------|------------|
| set（基本命令） | 1425 | 无特殊前缀 | `false` |
| show | 880 | `show_` | `true` |
| no | 654 | `no_` | `true` |
| clear | 617 | `clear_` | `true` |

### 适用范围分布

| scope | 数量 |
|-------|------|
| global | 3390 |
| group | 128 |
| global,group | 52 |

---

## 骨架块结构（3570 块）

### page_content 格式

固定行结构，由 `node_to_chunk()` 函数生成：

```
[命令] {label}                        ← 所有块都有
[说明] {help_string}                  ← 所有块都有（来自 XML help_string）
语法: {full_syntax}                   ← 所有块都有
参数:                                 ← 2263 块有（有参数时才输出）
  <param_name>  必填  类型=string     ←   必选参数用 <>
  [param_name]  可选  类型=integer    ←   可选参数用 []
适用范围: global                       ← 所有块都有
相关操作: clear / no / set / show      ← 728 个基本命令有（变体不输出）
内部函数: {func}                      ← 所有块都有
```

参数行格式：`{bracket}{name}{bracket}  {必填|可选}  类型={type}  [可选值: ...]  [默认=...]  [范围=...]  [# description]`

### metadata 字段

| 字段 | 出现率 | 数据来源 | 值示例 | 说明 |
|------|--------|----------|--------|------|
| `document_category` | 3570/3570 | 硬编码 | `"cli/reference"` | 文档分类，用于 UnifiedRAG 白名单过滤 |
| `source_file` | 3570/3570 | 硬编码 | `"cli_keyword_graph.json"` | 标识数据来自 CLI 图谱 |
| `product_module` | 3570/3570 | 图谱 root_id（向上追溯到无父节点） | `"aaa"`, `"slb"`, `"health"` | 顶层模块标识，用于模块过滤。**变体命令的粒度异常：** `clear_aaa_all` 的 root_id 是 `"clear_aaa_all"` 而非 `"aaa"`（因为变体节点的 parent 是 `"clear"` 不是原模块） |
| `command_prefix` | 3570/3570 | 图谱 node.label | `"aaa ldap attribute group"` | 命令全名（空格分隔），用于 BM25 关键词匹配 |
| `scope` | 3570/3570 | 图谱 node.scope[]，逗号拼接 | `"global"` / `"group"` / `"global,group"` | 命令适用范围 |
| `node_id` | 3570/3570 | 图谱 node.id | `"aaa_ldap_attribute_group"` | 图谱节点唯一标识（下划线分隔） |
| `is_variant` | 3570/3570 | node_id 前缀检测 (`no_`/`show_`/`clear_`/`display_`) | `true` / `false` | 是否为操作变体（非基本 set 命令） |
| `func` | 3570/3570 | 图谱 node.func（来自 XML） | `"aaa_ldap_attribute_group"` | CLI 内部函数名，XML 中的唯一标识 |

### 图谱源节点字段映射

| knowledge_base.json 字段 | cli_keyword_graph.json 源字段 | 转换逻辑 |
|--------------------------|-------------------------------|----------|
| page_content `[命令]` | `node.label` | 直接取值 |
| page_content `[说明]` | `node.help_string` | 直接取值（英文，来自 XML） |
| page_content `语法:` | `node.full_syntax` | 直接取值，含 `<>` `[]` `{x\|y}` 语法标记 |
| page_content `参数:` | `node.parameters[]` | 逐个格式化：name/required/type/values/default/constraint/description |
| page_content `适用范围:` | `node.scope[]` | 用 ` / ` 拼接 |
| page_content `相关操作:` | `node.operations{}` | keys 排序后用 ` / ` 拼接，仅基本命令输出 |
| page_content `内部函数:` | `node.func` | 直接取值 |
| metadata.product_module | `build_root_map()` → root_id | 沿 parent_of 边向上找到无父节点的根 |
| metadata.command_prefix | `node.label` | 直接取值 |
| metadata.scope | `node.scope[]` | 用 `,` 拼接 |
| metadata.node_id | `node.id` | 直接取值 |
| metadata.is_variant | `node.id` | 前缀匹配：`no_`/`show_`/`clear_`/`display_` |
| metadata.func | `node.func` | 直接取值 |

---

## 农民富化块结构（6 块）

来自 `cli.json`，由 KnowledgeFarmerAgent 从 `cli.pdf` 提取并语义富化。目前仅覆盖 ircookie 相关命令。

### page_content 格式

自由文本，包含中文命令说明和语法。无固定行结构。

```
slb mode ircookie <ircookie_mode> [group_name] [password]
该命令⽤于配置使⽤Insert Cookie、Rewrite Cookie或Embed Cookie算法时...
```

### metadata 字段

| 字段 | 数据来源 | 值示例 | 说明 |
|------|----------|--------|------|
| `document_category` | 硬编码 | `"cli/reference"` | 同骨架 |
| `source_file` | 原始 PDF | `"cli.pdf"` | 标识来自 PDF 提取 |
| `section_title` | PDF 章节标题 | `"slb mode ircookie"` | 命令名 |
| `page_idx` | PDF 页码 | `257` | 原始 PDF 页码 |
| `block_id` | 自动生成 | `"cli_5000_1f37e6cb"` | 文档块唯一 ID |
| `word_count` | 自动统计 | `11` | 词数（偏低，因分词不准） |
| `has_code_block` | 内容检测 | `true` | 是否包含代码块 |
| `command_prefix` | LLM 推断 | `"slb"` | 命令前缀（粒度粗，仅第一级） |
| `tree_node_id` | LLM 推断 | `"slb_mode_ircookie"` | 对应图谱节点 ID |
| `product_module` | LLM 推断 | `"SLB"` | 产品模块（大写，与骨架小写不一致） |
| `config_mode` | LLM 推断 | `"cli"` | 配置模式 |
| `intent` | LLM 推断 | `"manage_server_group"` | 命令意图分类 |
| `description` | LLM 生成 | `"Configures cookie format..."` | 英文语义描述 |
| `protocol_type` | LLM 推断 | `["HTTP"]` | 涉及的协议类型 |

---

## 「同一叶子」判定（数据契约）

- **强贴合**：富化块 `metadata.tree_node_id`（或农民解析结果）与骨架 `metadata.node_id` 一致，即与 `cli_keyword_graph` 中该 CLI 叶 **同一节点**；正文为互补叙述，非骨架文本拷贝。
- **辅助键**：`command_prefix` 应对齐骨架中的图谱 `label`（整行、空格分隔）。农民在 `cultivate_batch` 中用 `_refine_ac_meta_command_prefix`、`_match_tree_node` 等将粗粒度前缀与正文对齐到 `knowledge_base.json` 索引，详见 `docs/agents/sessions/01-tree.md`。
- **可选别名**：若 PDF 标题规范化后仍无法命中骨架键，可由树会话在 `reference/farmer_tree_alias.json` 提供 `{ "heading_slug": "node_id" }`，农民只消费该映射。

---

## 已知问题

1. **product_module 大小写**：历史农民块曾用大写 `"SLB"`、骨架用小写 `"slb"`。当前 `KnowledgeFarmerAgent._diff_with_skeleton` 对 **仅大小写** 差异会回填骨架写法，不报 `conflict`；语义不一致仍报 `conflict`。
2. **变体命令的 product_module 粒度异常**：`clear_aaa_all` 的 root_id 是 `"clear_aaa_all"` 而非 `"aaa"`，因为 operation_command 节点在图谱中的 parent 指向 `"clear"` 而非原始模块
3. **help_string 全英文**：骨架块的 `[说明]` 全部是英文（来自 XML），缺少中文说明
4. **农民块仅覆盖 6 条命令**：ircookie 相关，其余 3570 条均为骨架自动生成
5. **缺少参数语法标记传承**：`full_syntax` 中的 `<>` `[]` `{x|y}` 信息在 metadata 中无独立字段，仅存在于 page_content 文本中
