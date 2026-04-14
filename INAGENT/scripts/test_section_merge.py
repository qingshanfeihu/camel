#!/usr/bin/env python3
"""测试章节块合并功能。

使用农场主输出的真实数据测试章节合并。
"""

import json
import logging
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """主函数。"""
    from INAGENT.data_tools.merge_section_blocks import (
        merge_section_blocks,
        merge_section_blocks_file,
    )

    logger.info("=" * 80)
    logger.info("测试章节块合并功能")
    logger.info("=" * 80)

    # 使用农场主输出的真实数据
    input_file = Path(
        "INAGENT/test_data/农场主输出/farm_owner_batch_20260413_221347/"
        "quality_delivery_only_20260413_180517/_quality_reference_for_owner.json"
    )

    if not input_file.exists():
        logger.error("输入文件不存在: %s", input_file)
        return

    # 测试1：直接合并（不写文件）
    logger.info("\n测试1：加载并分析数据")
    chunks = json.loads(input_file.read_text(encoding="utf-8"))
    logger.info("输入块数: %d", len(chunks))

    # 分析需要合并的章节
    from collections import defaultdict
    from INAGENT.data_tools.merge_section_blocks import _build_merge_key

    groups = defaultdict(list)
    for chunk in chunks[:500]:  # 只分析前500个
        key = _build_merge_key(chunk)
        if key:
            groups[key].append(chunk)

    multi_block_sections = {k: v for k, v in groups.items() if len(v) > 1}
    logger.info("前500块中，需要合并的章节数: %d", len(multi_block_sections))

    # 显示示例
    logger.info("\n需要合并的章节示例：")
    for i, (key, blocks) in enumerate(list(multi_block_sections.items())[:5], 1):
        logger.info("  [%d] %s", i, key)
        logger.info("      块数: %d", len(blocks))
        for b in blocks[:2]:
            meta = b.get("metadata", {})
            logger.info(
                "        block_id=%s, page=%s, content=%s",
                meta.get("block_id"),
                meta.get("page_idx"),
                b.get("page_content", "")[:60].replace("\n", " "),
            )

    # 测试2：执行合并
    logger.info("\n测试2：执行章节合并")
    merged_chunks, report = merge_section_blocks(chunks)

    logger.info("\n" + "=" * 80)
    logger.info("合并报告")
    logger.info("=" * 80)
    logger.info("输入块数: %d", report.total_chunks)
    logger.info("合并章节数: %d", report.merged_sections)
    logger.info("合并块数: %d", report.merged_chunks)
    logger.info("输出块数: %d", report.output_chunks)
    logger.info("跳过块数: %d", len(report.skipped))

    # 显示合并示例
    logger.info("\n合并后的块示例：")
    merged_examples = [c for c in merged_chunks if c.get("metadata", {}).get("merged_from_blocks")]
    for i, chunk in enumerate(merged_examples[:3], 1):
        meta = chunk.get("metadata", {})
        logger.info("\n[%d] 合并块", i)
        logger.info("  source_file: %s", meta.get("source_file"))
        logger.info("  section_path: %s", meta.get("section_path"))
        logger.info("  merged_from_blocks: %s", meta.get("merged_from_blocks"))
        logger.info("  merged_block_count: %d", meta.get("merged_block_count", 0))
        logger.info("  content_length: %d", len(chunk.get("page_content", "")))
        logger.info("  content_preview: %s", chunk.get("page_content", "")[:200].replace("\n", " "))

    # 测试3：写入文件
    output_file = Path("INAGENT/scripts/test_output/merged_reference.json")
    output_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("\n测试3：写入合并结果到文件")
    report2 = merge_section_blocks_file(
        input_path=input_file,
        output_path=output_file,
    )

    logger.info("\n文件合并报告:")
    logger.info("  输入: %s", input_file)
    logger.info("  输出: %s", output_file)
    logger.info("  输入块数: %d", report2.total_chunks)
    logger.info("  输出块数: %d", report2.output_chunks)
    logger.info("  合并章节数: %d", report2.merged_sections)

    logger.info("\n" + "=" * 80)
    logger.info("测试完成")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
