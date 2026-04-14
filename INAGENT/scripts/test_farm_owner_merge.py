#!/usr/bin/env python3
"""测试农场主的章节合并功能（使用真实数据）。"""

import json
import logging
import shutil
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
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    logger.info("=" * 80)
    logger.info("测试农场主章节合并功能（真实数据）")
    logger.info("=" * 80)

    # 创建测试目录（复制真实数据）
    source_dir = Path(
        "INAGENT/test_data/农场主输出/farm_owner_batch_20260413_221347/"
        "quality_delivery_only_20260413_180517"
    )
    test_dir = Path("INAGENT/scripts/test_output/farm_owner_merge_test")

    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True)

    # 复制测试文件
    source_file = source_dir / "_quality_reference_for_owner.json"
    test_file = test_dir / "test_reference.json"
    shutil.copy(source_file, test_file)

    logger.info("测试数据准备完成")
    logger.info("  源文件: %s", source_file)
    logger.info("  测试目录: %s", test_dir)

    # 创建农场主实例
    logger.info("\n创建农场主实例...")
    farm_owner = KnowledgeFarmOwnerAgent(graphrag_retriever=None)

    # 执行章节合并
    logger.info("\n执行章节合并...")
    report = farm_owner.merge_section_blocks_in_reference(
        reference_dir=test_dir,
        output_suffix="_merged",
        min_blocks_to_merge=2,
    )

    # 显示报告
    logger.info("\n" + "=" * 80)
    logger.info("合并报告")
    logger.info("=" * 80)
    logger.info("处理文件数: %d", report["processed_files"])
    logger.info("输入块总数: %d", report["total_input_chunks"])
    logger.info("输出块总数: %d", report["total_output_chunks"])
    logger.info("合并章节数: %d", report["total_merged_sections"])
    logger.info("合并块数: %d", report["total_merged_chunks"])
    logger.info("块数减少: %d (%.1f%%)",
                report["total_input_chunks"] - report["total_output_chunks"],
                (1 - report["total_output_chunks"] / report["total_input_chunks"]) * 100
                if report["total_input_chunks"] > 0 else 0)

    # 显示文件详情
    logger.info("\n文件处理详情:")
    for file_info in report["files"]:
        logger.info("  %s:", file_info["input_file"])
        logger.info("    输入: %d 块", file_info["input_chunks"])
        logger.info("    输出: %d 块", file_info["output_chunks"])
        logger.info("    合并: %d 章节 (%d 块)",
                    file_info["merged_sections"],
                    file_info["merged_chunks"])

    # 验证合并结果
    logger.info("\n验证合并结果...")
    merged_file = test_dir / "test_reference_merged.json"
    if merged_file.exists():
        merged_data = json.loads(merged_file.read_text(encoding="utf-8"))
        logger.info("合并文件已生成: %s", merged_file)
        logger.info("合并后块数: %d", len(merged_data))

        # 查找示例合并块
        merged_examples = [
            c for c in merged_data
            if c.get("metadata", {}).get("merged_from_blocks")
        ]
        logger.info("包含合并信息的块数: %d", len(merged_examples))

        # 显示几个示例
        logger.info("\n合并示例:")
        for i, chunk in enumerate(merged_examples[:3], 1):
            meta = chunk.get("metadata", {})
            logger.info("\n  [%d] %s", i, meta.get("section_title", ""))
            logger.info("      source_file: %s", meta.get("source_file"))
            logger.info("      merged_from_blocks: %s", meta.get("merged_from_blocks"))
            logger.info("      content_length: %d", len(chunk.get("page_content", "")))

    logger.info("\n" + "=" * 80)
    logger.info("测试完成")
    logger.info("=" * 80)
    logger.info("测试目录: %s", test_dir)


if __name__ == "__main__":
    main()
