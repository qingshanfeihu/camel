# 采购员交给质检员的具体材料位置

## 三个关键位置

### 位置1: `INAGENT/knowledge_base/reference/`

#### `knowledge_base.json` (7.32 MB)
- **内容**: 6071条已通过验证的知识块
- **用途**: 质检员最主要审查对象
- **数据结构示例**:
```json
{
  "page_content": "信安世纪APV 10.4.5用户手册\n...",
  "metadata": {
    "source_file": "app_1-40.json",
    "source_pdf": "C:\\...\\app_1-40.pdf",
    "section_title": "信安世纪APV 10.4.5用户手册",
    "document_category": "app/reference",
    "product_module": "用户管理",
    ...
  }
}
```

#### `_ingest_report.json` (281 字符)
- **内容**: 采购流程的统计指标
- **关键数据**:
  - `input_count: 7023` - 输入总块
  - `accepted: 6071` - 入选块（86.44%）
  - `rejected_short: 693` - 短内容拒绝
  - `rejected_category: 0` - 分类错误拒绝
  - `rejected_excluded: 58` - 农场主明确排除的块
  - `duplicates: 201` - 去重块数
  - `tree_linked: 6272` - 成功关联树的块
  - `hierarchy_backfilled: 2515` - 回填了层级的块
  - `module_inferred: 17` - 推断模块的块

#### 文档级JSON文件
- `cli_1-82.json` (2677.9 KB) - CLI命令树的块集合
- `app_1-40.json` (824.8 KB) - 应用配置的块集合
- `app_65-72.json` (145.3 KB) - 高级功能的块集合

---

### 位置2: `INAGENT/test_data/runs/<run_id>/`

例如: `verify_final_fix_20260412/`

#### `manifest.json` (0.5 KB)
- **内容**: 运行元信息
- **包含**:
  - `run_id`: 运行ID
  - `status`: 运行状态 (ok/failed)
  - `started_at` / `finished_at`: 时间戳
  - `error`: 错误信息（如有）
  - `official_entry`: 调用的官方入口

#### `auto_convert.log` (12.5 KB)
- **内容**: 完整的文档转换日志
- **用途**: 追踪PDF/Office文档解析过程
- **包含内容**: MinerU缓存状态、Office处理等

#### `diagnostic_report.json` (1.9 KB)
- **内容**: 质量诊断报告（4个维度）
- **维度**:
  1. **baseline**: manifest检查、日志完整性、HuaWei失败检测
  2. **ingest**: acceptance_rate、rejection_ratio、metadata覆盖率
  3. **metadata_alignment**: document_category、product_module覆盖率
  4. **file_coverage**: 各文档的块数是否在预期范围内

---

### 位置3: `INAGENT/knowledge_base/logs/`

#### `auto_convert.log`
- **内容**: auto_convert.py的主日志（与test_data版本相同）
- **持久化**: 每次运行都追加模式，运行开始时会清空

---

## 质检员的检查流程

### 第1步: 检查采购元数据
```
INAGENT/test_data/runs/verify_final_fix_20260412/manifest.json
├─ status: ok (无错误) ✓
├─ started_at: 2026-04-12T23:18:18.298880
├─ finished_at: 2026-04-12T23:18:34.407886 (耗时16秒)
└─ error: null ✓
```

### 第2步: 检查采购统计
```
INAGENT/knowledge_base/reference/_ingest_report.json
├─ 入库率: 6071/7023 = 86.44% ⚠️ (低于90%目标)
├─ 拒绝率: 10.68% (短693 + 分类0 + 排除58)
├─ 去重: 201条重复被过滤
├─ 树关联: 89.31% (6272/7023)
└─ 质量无问题: 0隔离、0分类错误
```

### 第3步: 抽检知识库内容（采购员职责仅基础结构）
```
INAGENT/knowledge_base/reference/knowledge_base.json
├─ 总块数: 6071条
├─ 采购员基础结构检查:
│  ├─ source_file: ✓ 每条必须有
│  ├─ source_pdf: ✓ 每条必须有
│  ├─ section_title: ✓ 每条必须有
│  └─ page_content/text: ✓ 至少一个非空
├─ 非采购员强制项（可由后续流程补齐）:
│  ├─ tree_position
│  ├─ function_hierarchy
│  └─ tree_attached
└─ 示例块:
   source_file: app_1-40.json
   section_title: "信安世纪APV 10.4.5用户手册"
   document_category: "app/reference"
   product_module: "用户管理"
```

### 第4步: 检查转换日志
```
INAGENT/test_data/runs/verify_final_fix_20260412/auto_convert.log
├─ PDF处理: ✓ 已缓存
├─ Office处理: ✓ docx/xlsx已正确处理
└─ 合并: ✓ 文档已正确合并
```

### 第5步: 检查诊断报告
```
INAGENT/test_data/runs/verify_final_fix_20260412/diagnostic_report.json
├─ baseline: ✓ OK (log_completion_mark_count=1)
├─ ingest: ❌ acceptance 86.44% < 90% (BLOCKING!)
├─ metadata: ✓ OK (0% unknown category)
└─ coverage: ℹ️ INFO (块数20-25%偏低)
```

---

## 快速查询命令

### 查看入库率
```bash
python -c "
import json
r = json.load(open('INAGENT/knowledge_base/reference/_ingest_report.json'))
print(f'入库率: {r[\"accepted\"]}/{r[\"input_count\"]} = {r[\"accepted\"]/r[\"input_count\"]:.2%}')
"
```

### 查看知识库块数
```bash
python -c "
import json
kb = json.load(open('INAGENT/knowledge_base/reference/knowledge_base.json'))
print(f'总块数: {len(kb)}')
print(f'首条块内容: {kb[0][\"metadata\"][\"section_title\"]}')
"
```

### 运行质量诊断
```bash
python -m INAGENT.scripts.procurement_quality_diagnostic --run-id verify_final_fix_20260412
```

---

## 文件大小总览

| 文件 | 大小 | 用途 |
|------|------|------|
| knowledge_base.json | 7.32 MB | 主知识库（6071块） |
| _ingest_report.json | 281 B | 采购统计指标 |
| cli_1-82.json | 2.6 MB | CLI文档块集 |
| app_1-40.json | 824.8 KB | 应用配置块集 |
| app_65-72.json | 145.3 KB | 高级功能块集 |
| manifest.json | 0.5 KB | 运行元数据 |
| auto_convert.log | 12.5 KB | 转换日志 |
| diagnostic_report.json | 1.9 KB | 质量诊断 |

---

## 关键指标速查

| 指标 | 值 | 目标 | 状态 |
|------|-----|------|------|
| 入库率 (acceptance_rate) | 86.44% | ≥90% | ❌ 未达 |
| 隔离率 (quarantine_rate) | 0.00% | ≤3% | ✓ 优秀 |
| 去重率 (duplicate_rate) | 2.86% | <5% | ✓ 良好 |
| 树关联率 (tree_linked_rate) | 89.31% | ≥85% | ✓ 良好 |
| L0拒绝率 (rejection_ratio) | 11.01% | <10% | ⚠️ 警告 |

---

## 质检员签收检查单

- [ ] manifest.json 状态 = ok
- [ ] _ingest_report.json 无异常
- [ ] knowledge_base.json 块完整性抽检（10%）
- [ ] auto_convert.log 无关键错误
- [ ] diagnostic_report.json 无blocker
- [ ] 采购员交接完成，质检员可接收

**接收条件**: acceptance_rate ≥ 90% 或经人工审核确认
