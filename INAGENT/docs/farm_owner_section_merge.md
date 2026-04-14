# 农场主章节合并功能

## 问题背景

大数据清洗后，同一章节的内容被拆分成多个块，导致上下文丢失，不利于农民后续处理。

**示例问题：**
- "4.2.控制台连接" 章节被拆分成 block_id 386 (page 15) 和 388 (page 16)
- "概述" 章节被拆分成 6 个块（block_id 334, 384, 433, 528, 530, 799）
- 每个块都重复包含章节标题，内容不连贯

## 解决方案

通过 **路径注入 + 章节编号识别 + 自动合并**，将同一章节的多个块合并成一个完整块。

### 合并策略

**合并键构建：** `source_file + section_path + section_number`

1. **source_file**: 源文件名（如 app_1-40.json）
2. **section_path**: 章节路径（如 "集群 > 集群配置示例 > 内部端口集群配置"）
3. **section_number**: 章节编号（如 "4.2", "11.3.1"）

**章节编号提取：**
- 优先从 `section_title` 提取
- 其次从 `clean_text` 提取
- 最后从 `page_content` 前200字符提取
- 支持多级编号：`\d+(\.\d+)+`（如 4.2, 11.3.1）

**合并逻辑：**
1. 按合并键分组
2. 组内按 `page_idx` 和 `block_id` 排序
3. 使用第一个块作为基础
4. 合并所有块的 `page_content`，去除重复的章节标题
5. 在 `metadata` 中记录合并信息

## 使用方法

### 方法1：农场主集成方法

```python
from pathlib import Path
from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

# 创建农场主实例
farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

# 合并 reference 目录下的所有 JSON 文件
reference_dir = Path("knowledge_base/reference")

report = farm_owner.merge_section_blocks_in_reference(
    reference_dir=reference_dir,
    output_suffix="_merged",      # 输出文件后缀
    min_blocks_to_merge=2,        # 最少需要2个块才合并
)

print(f"处理文件数: {report['processed_files']}")
print(f"输入块总数: {report['total_input_chunks']}")
print(f"输出块总数: {report['total_output_chunks']}")
print(f"合并章节数: {report['total_merged_sections']}")
```

### 方法2：独立工具函数

```python
from pathlib import Path
from INAGENT.data_tools.merge_section_blocks import merge_section_blocks_file

# 合并单个文件
input_file = Path("reference/test_reference.json")
output_file = Path("reference/test_reference_merged.json")

report = merge_section_blocks_file(
    input_path=input_file,
    output_path=output_file,
    min_blocks_to_merge=2,
)

print(f"输入: {report.total_chunks} 块")
print(f"输出: {report.output_chunks} 块")
print(f"合并: {report.merged_sections} 章节")
```

### 方法3：内存中合并

```python
import json
from INAGENT.data_tools.merge_section_blocks import merge_section_blocks

# 加载数据
chunks = json.load(open("reference/test_reference.json", encoding="utf-8"))

# 执行合并
merged_chunks, report = merge_section_blocks(
    chunks,
    min_blocks_to_merge=2,
)

print(f"合并前: {len(chunks)} 块")
print(f"合并后: {len(merged_chunks)} 块")
print(f"合并章节: {report.merged_sections}")
```

## 合并效果

**真实数据测试结果：**
- 输入：6026 块
- 输出：4085 块
- 合并：169 个章节（2110 块）
- **块数减少：32.2%**

**合并示例：**

1. **产品概述**：2 块 → 1 块
   - 合并前：block_id 327, 328
   - 合并后：完整的产品概述内容

2. **概述**：6 块 → 1 块
   - 合并前：block_id 334, 384, 433, 528, 530, 799
   - 合并后：完整的概述内容

3. **4.2.控制台连接**：2 块 → 1 块
   - 合并前：block_id 386 (page 15), 388 (page 16)
   - 合并后：完整的控制台连接说明

## 合并后的元数据

合并后的块会在 `metadata` 中添加以下字段：

```json
{
  "metadata": {
    "source_file": "app_1-40.json",
    "block_id": 386,
    "section_title": "4.2.控制台连接",
    "section_path": "4.2.控制台连接",
    "merged_from_blocks": [386, 388],
    "merged_block_count": 2,
    "merge_key": "app_1-40.json::4.2.控制台连接::4.2",
    ...
  },
  "page_content": "4.2.控制台连接\n\n如果需要通过控制台连接设备...\n\n通过控制台终端成功连接到设备后..."
}
```

## 集成到管线

在知识库管线中的位置：

```
采购员 → 质检员 → 农场主（产品过滤 + 章节合并） → 农民
```

**推荐流程：**

```python
# 1. 产品过滤
farm_owner.process_quality_feedback(
    reference_dir=reference_dir,
    auto_analyze_product=True,
    dry_run=False,
)

# 2. 章节合并
farm_owner.merge_section_blocks_in_reference(
    reference_dir=reference_dir,
    output_suffix="_merged",
)

# 3. 使用合并后的文件进行后续处理
# 农民读取 *_merged.json 文件进行知识填充
```

## 配置参数

### output_suffix

输出文件后缀，默认 `"_merged"`。

- 输入：`test_reference.json`
- 输出：`test_reference_merged.json`

### min_blocks_to_merge

最少需要多少个块才执行合并，默认 `2`。

- 如果章节只有1个块，不会合并
- 如果章节有2个或更多块，会合并成1个块

## 测试

运行完整测试：

```bash
cd C:/SynologyDrive/INFOAGEN

# 测试独立工具
python -m INAGENT.scripts.test_section_merge

# 测试农场主集成
python -m INAGENT.scripts.test_farm_owner_merge
```

## 注意事项

1. **文件命名**：合并后的文件会添加后缀（默认 `_merged`），原文件保持不变
2. **幂等性**：重复执行会跳过已合并的文件（文件名包含 `output_suffix`）
3. **元数据保留**：合并后保留第一个块的所有元数据，并添加合并信息
4. **章节标题去重**：自动去除重复的章节标题和编号行
5. **排序保证**：按 `page_idx` 和 `block_id` 排序，确保内容顺序正确

## 相关文件

- 核心实现：`INAGENT/data_tools/merge_section_blocks.py`
- 农场主集成：`INAGENT/agents/knowledge_farm_owner_agent.py` (merge_section_blocks_in_reference 方法)
- 独立测试：`INAGENT/scripts/test_section_merge.py`
- 集成测试：`INAGENT/scripts/test_farm_owner_merge.py`

## 未来优化

1. **智能章节识别**：使用 LLM 识别更复杂的章节结构
2. **跨文件合并**：支持父子文档的章节合并
3. **增量合并**：只合并新增或修改的块
4. **合并质量评估**：评估合并后的内容连贯性
