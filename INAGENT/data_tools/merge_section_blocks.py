# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""章节块合并工具 - 将同一章节的多个块合并成一个完整块。

问题：大数据清洗后，同一章节的内容被拆分成多个块，导致上下文丢失。
解决：根据 source_file + section_path + 章节编号，将同一章节的块合并。

示例：
    app_1-40.json 的 "4.2.控制台连接" 章节被拆分成：
    - block_id 386 (page 15): "如果需要通过控制台连接设备..."
    - block_id 388 (page 16): "通过控制台终端成功连接到设备后..."

    合并后成为一个完整的块，包含完整的章节内容。
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SectionMergeReport:
    """章节合并报告。"""

    total_chunks: int = 0
    merged_sections: int = 0
    merged_chunks: int = 0
    output_chunks: int = 0
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (context, reason)


def _extract_section_number(text: str) -> str | None:
    """从文本中提取章节编号（如 "4.2", "11.3.1"）。

    Args:
        text: 包含章节编号的文本

    Returns:
        章节编号字符串，如果未找到则返回 None
    """
    # 匹配模式：数字.数字 或 数字.数字.数字 等
    # 优先匹配行首或空格后的编号
    patterns = [
        r'^(\d+(?:\.\d+)+)',  # 行首
        r'\s(\d+(?:\.\d+)+)',  # 空格后
        r'(\d+(?:\.\d+)+)',  # 任意位置
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()

    return None


def _build_merge_key(chunk: Dict[str, Any]) -> str | None:
    """构建合并键：source_file + section_path + section_number。

    Args:
        chunk: 知识块

    Returns:
        合并键，如果无法构建则返回 None
    """
    meta = chunk.get("metadata", {})
    source_file = meta.get("source_file", "")
    section_path = meta.get("section_path", "")

    if not source_file or not section_path:
        return None

    # 尝试从多个字段提取章节编号
    section_title = meta.get("section_title", "")
    clean_text = meta.get("clean_text", "")
    page_content = chunk.get("page_content", "")

    # 优先从 section_title 提取
    section_num = _extract_section_number(section_title)
    if not section_num:
        section_num = _extract_section_number(clean_text)
    if not section_num:
        section_num = _extract_section_number(page_content[:200])

    # 如果没有章节编号，使用 section_path 作为唯一标识
    if not section_num:
        return f"{source_file}::{section_path}"

    return f"{source_file}::{section_path}::{section_num}"


def merge_section_blocks(
    chunks: List[Dict[str, Any]],
    *,
    min_blocks_to_merge: int = 2,
) -> Tuple[List[Dict[str, Any]], SectionMergeReport]:
    """合并同一章节的多个块。

    Args:
        chunks: 输入的知识块列表
        min_blocks_to_merge: 最少需要多少个块才执行合并（默认2）

    Returns:
        (合并后的块列表, 合并报告)
    """
    report = SectionMergeReport(total_chunks=len(chunks))

    # 按合并键分组
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    no_key_chunks: List[Dict[str, Any]] = []

    for chunk in chunks:
        merge_key = _build_merge_key(chunk)
        if merge_key:
            groups[merge_key].append(chunk)
        else:
            no_key_chunks.append(chunk)
            report.skipped.append(
                (str(chunk.get("metadata", {}).get("block_id", "unknown")),
                 "无法构建合并键")
            )

    # 合并每个组
    merged_chunks: List[Dict[str, Any]] = []

    for merge_key, group_chunks in groups.items():
        if len(group_chunks) < min_blocks_to_merge:
            # 不需要合并，直接保留
            merged_chunks.extend(group_chunks)
            continue

        # 需要合并
        report.merged_sections += 1
        report.merged_chunks += len(group_chunks)

        # 按 page_idx 或 block_id 排序（确保顺序正确）
        sorted_chunks = sorted(
            group_chunks,
            key=lambda c: (
                c.get("metadata", {}).get("page_idx", 0),
                c.get("metadata", {}).get("block_id", 0),
            )
        )

        # 使用第一个块作为基础
        base_chunk = sorted_chunks[0].copy()
        base_meta = base_chunk.get("metadata", {}).copy()

        # 合并 page_content（去除重复的章节标题）
        section_title = base_meta.get("section_title", "")
        merged_content_parts = []

        for chunk in sorted_chunks:
            content = chunk.get("page_content", "").strip()
            if not content:
                continue

            # 移除重复的章节标题（如果内容以章节标题开头）
            if section_title and content.startswith(section_title):
                content = content[len(section_title):].strip()

            # 移除重复的章节编号行
            lines = content.split("\n")
            filtered_lines = []
            for line in lines:
                line_stripped = line.strip()
                # 跳过纯章节编号行（如 "4.2.控制台连接"）
                if line_stripped == section_title:
                    continue
                # 跳过纯编号行（如 "8.3.2. 内部端口集群配置"）
                if _extract_section_number(line_stripped) and len(line_stripped) < 100:
                    # 可能是章节标题行，检查是否与 section_title 相似
                    if section_title in line_stripped or line_stripped in section_title:
                        continue
                filtered_lines.append(line)

            content = "\n".join(filtered_lines).strip()
            if content:
                merged_content_parts.append(content)

        # 构建合并后的内容（保留章节标题）
        if section_title:
            merged_content = f"{section_title}\n\n" + "\n\n".join(merged_content_parts)
        else:
            merged_content = "\n\n".join(merged_content_parts)

        base_chunk["page_content"] = merged_content

        # 更新元数据：记录合并信息
        base_meta["merged_from_blocks"] = [
            c.get("metadata", {}).get("block_id") for c in sorted_chunks
        ]
        base_meta["merged_block_count"] = len(sorted_chunks)
        base_meta["merge_key"] = merge_key

        base_chunk["metadata"] = base_meta
        merged_chunks.append(base_chunk)

    # 添加无法合并的块
    merged_chunks.extend(no_key_chunks)

    report.output_chunks = len(merged_chunks)

    logger.info(
        "章节合并完成: 输入 %d 块, 合并 %d 章节 (%d 块), 输出 %d 块",
        report.total_chunks,
        report.merged_sections,
        report.merged_chunks,
        report.output_chunks,
    )

    return merged_chunks, report


def merge_section_blocks_file(
    input_path: Path,
    output_path: Path | None = None,
    *,
    min_blocks_to_merge: int = 2,
) -> SectionMergeReport:
    """从文件读取、合并、写入文件。

    Args:
        input_path: 输入 JSON 文件路径
        output_path: 输出 JSON 文件路径（默认覆盖输入文件）
        min_blocks_to_merge: 最少需要多少个块才执行合并

    Returns:
        合并报告
    """
    if output_path is None:
        output_path = input_path

    # 读取输入
    try:
        chunks = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("读取输入文件失败: %s", exc)
        raise

    if not isinstance(chunks, list):
        raise ValueError(f"输入文件必须是 JSON 数组: {input_path}")

    # 合并
    merged_chunks, report = merge_section_blocks(
        chunks,
        min_blocks_to_merge=min_blocks_to_merge,
    )

    # 写入输出
    try:
        output_path.write_text(
            json.dumps(merged_chunks, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("合并结果已写入: %s", output_path)
    except Exception as exc:
        logger.error("写入输出文件失败: %s", exc)
        raise

    return report
