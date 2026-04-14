# 农场主产品过滤功能

## 概述

农场主新增产品特定过滤功能，作为知识库质量把关的最后一道防线，防止竞品产品（如华为、思科等）的知识进入NSAE知识库。

## 功能特性

### 1. 产品匹配度分析

基于产品签名配置（`INAGENT/config/product_signatures.json`），对知识块进行匹配度评分：

**评分维度（加权）：**
- 命令前缀匹配：权重 0.4
- 产品模块匹配：权重 0.3
- 产品关键词匹配：权重 0.3

**双重判断机制：**
1. **绝对判断**：`target_score < min_target_score (0.05)` 且 `competitor_score > max_competitor_score (0.1)`
2. **相对判断**：`competitor_score / target_score > competitor_advantage_threshold (2.0)`

### 2. 质检反馈处理

完整的闭环处理流程：

```
产品匹配分析 → 质检反馈inbox → 规则建议 → 删除清单 → 执行删除
```

**生成的文件：**
- `_quality_feedback_inbox.jsonl` - 质检反馈记录
- `_quality_rule_proposals.json` - 聚合的规则建议
- `_quality_purge_manifest.jsonl` - 待删除块清单

## 使用方法

### 方法1：独立调用产品匹配分析

```python
from pathlib import Path
from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

# 创建农场主实例（产品过滤不需要 graphrag_retriever）
farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

# 准备知识块（必须包含 page_content 和 metadata.source_file/block_id）
chunks = [
    {
        "page_content": "使用 slb virtual-server 命令配置虚拟服务器...",
        "metadata": {
            "source_file": "nsae_knowledge.json",
            "block_id": "nsae_001",
            "section_title": "SLB配置",
        },
    },
]

# 分析产品匹配度
feedback_records = farm_owner.analyze_product_match(chunks)

# feedback_records 包含不匹配的块，可写入 inbox
```

### 方法2：完整质检反馈处理

```python
from pathlib import Path
from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

# 处理 reference 目录下的所有知识块
reference_dir = Path("knowledge_base/reference")

report = farm_owner.process_quality_feedback(
    reference_dir=reference_dir,
    auto_analyze_product=True,  # 自动分析产品匹配度
    dry_run=True,               # 仅模拟，不实际删除
)

print(f"分析块数: {report['product_analysis']['analyzed']}")
print(f"不匹配块数: {report['product_analysis']['mismatches']}")
print(f"删除块数: {report['purge']['deleted']}")
```

### 方法3：实际执行删除

```python
# 确认无误后，设置 dry_run=False 执行实际删除
report = farm_owner.process_quality_feedback(
    reference_dir=reference_dir,
    auto_analyze_product=True,
    dry_run=False,  # 实际删除
)
```

## 配置文件

### product_signatures.json 结构

```json
{
  "target_product": {
    "name": "NSAE",
    "command_prefixes": ["slb", "nat", "ha", ...],
    "product_modules": ["SLB", "NAT", "高可用", ...],
    "product_keywords": ["NSAE", "APV", "InfosecOS", ...]
  },
  "competitor_products": [
    {
      "name": "华为",
      "command_prefixes": ["display", "system-view", ...],
      "product_keywords": ["华为", "Huawei", "CloudEngine", ...]
    },
    ...
  ],
  "filter_rules": {
    "min_target_score": 0.05,
    "max_competitor_score": 0.1,
    "competitor_advantage_threshold": 2.0
  }
}
```

### 阈值调整建议

- **min_target_score (0.05)**：目标产品最低得分，低于此值可能是竞品
- **max_competitor_score (0.1)**：竞品得分阈值，高于此值触发告警
- **competitor_advantage_threshold (2.0)**：竞品得分优势倍数，超过此倍数触发告警

根据实际运行情况调整这些阈值，平衡误报和漏报。

## 测试

运行完整流程测试：

```bash
cd C:/SynologyDrive/INFOAGEN
python -m INAGENT.scripts.test_quality_feedback_flow
```

运行产品过滤单元测试：

```bash
python -m INAGENT.scripts.test_product_filter
```

## 数据结构要求

知识块必须符合以下结构：

```python
{
    "page_content": str,  # 知识块内容
    "metadata": {
        "source_file": str,      # 源文件名（必需）
        "block_id": str,         # 块ID（必需）
        "section_title": str,    # 章节标题（可选，用于匹配）
        ...
    }
}
```

## 集成到管线

在知识库管线中的位置：

```
采购员 → 质检员 → 农场主（产品过滤） → 农民
```

农场主在接收质检员输出后，自动执行产品过滤，将竞品知识拒之门外。

## 注意事项

1. **产品签名维护**：定期更新 `product_signatures.json`，添加新的命令、模块和关键词
2. **阈值调优**：根据实际运行情况调整过滤阈值，避免误杀或漏检
3. **人工复核**：建议定期复核 `_quality_feedback_inbox.jsonl`，确认过滤准确性
4. **备份机制**：执行删除前确保有备份，`dry_run=True` 可预览删除结果
5. **增量处理**：`process_quality_feedback` 支持增量处理，多次调用会追加到 inbox

## 相关文件

- 实现：`INAGENT/agents/knowledge_farm_owner_agent.py`
- 配置：`INAGENT/config/product_signatures.json`
- 测试：`INAGENT/scripts/test_product_filter.py`
- 完整流程测试：`INAGENT/scripts/test_quality_feedback_flow.py`
- 质检反馈基础设施：`INAGENT/data_tools/quality_feedback_loop.py`
