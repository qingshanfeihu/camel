# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
规格文档解析器：将 docx/doc 文档按章节切分为知识块

支持的文档类型：
- SW Functional Specification（固定标题层级）
- Product Requirement Document
- Software Design Document

输出格式与 auto_convert.py 的 PDF 流水线兼容：
    {
        "page_content": "...",
        "metadata": { ... }
    }
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _split_markdown_sections(markdown_text: str) -> List[Dict[str, Any]]:
    """
    将 Markdown 文本按标题切分为 section 列表。

    每个 section: {
        "heading": "...",
        "level": int,      # 标题层级 (1-6)
        "content": "...",   # 本节内容（不含子节标题行）
        "parent_heading": "...",
        "section_path": "A > B > C",
    }
    """
    lines = markdown_text.split("\n")
    sections: List[Dict[str, Any]] = []
    # heading_stack 存储归一化标题（已去编号），包含当前节点自身
    # 结构: [...祖先, 当前]  → parent = stack[-2], path = join(stack)
    heading_stack: List[str] = []

    current_heading = ""   # 原始标题（含编号），用于 page_content 显示
    current_level = 0
    current_lines: List[str] = []

    def _flush():
        nonlocal current_heading, current_level, current_lines
        content = "\n".join(current_lines).strip()
        if content or current_heading:
            # heading_stack[-1] 是当前节点（已归一化）
            # heading_stack[-2] 是直接父节点（已归一化），不存在则为 ""
            parent = heading_stack[-2] if len(heading_stack) >= 2 else ""
            path = " > ".join(heading_stack) if heading_stack else ""
            sections.append({
                "heading": current_heading,
                "level": current_level,
                "content": content,
                "parent_heading": parent,
                "section_path": path,
            })
        current_lines = []

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)", line)
        if m:
            _flush()
            level = len(m.group(1))
            heading = m.group(2).strip()
            normalized = _remove_section_number(heading)
            # 维护 heading_stack（归一化，包含当前节点）
            while len(heading_stack) >= level:
                heading_stack.pop()
            current_heading = heading
            current_level = level
            heading_stack.append(normalized)
        else:
            current_lines.append(line)

    _flush()
    return sections


def parse_spec_document(
    file_path: Path,
    document_category: str,
    product_module: Optional[str] = None,
    min_chunk_chars: int = 50,
    max_chunk_chars: int = 3000,
) -> List[Dict[str, Any]]:
    """
    解析规格文档（docx/doc）为知识块列表。

    流程：
    1. 使用 MarkItDown 转为 Markdown
    2. 按标题切分为 section
    3. 小段合并，长段截断
    4. 生成兼容 auto_convert 的 JSON chunk

    Args:
        file_path: 文件路径
        document_category: 文档分类 (spec/func_spec, spec/prd, spec/design 等)
        product_module: 产品模块名 (HTTP2, SLB 等)
        min_chunk_chars: 最小块字符数
        max_chunk_chars: 最大块字符数

    Returns:
        List[Dict] 兼容 knowledge_base.json 的块列表
    """
    # .doc 文件由采购管线通过 MinerU 云端处理，spec_parser 只处理 .docx
    if file_path.suffix.lower() == ".doc":
        raise ValueError(
            f"[spec_parser] {file_path.name} 为 .doc 格式，"
            "应通过 MinerU 云端管线处理，不应直接传入 spec_parser。"
        )

    from camel.loaders.markitdown import MarkItDownLoader

    loader = MarkItDownLoader()
    try:
        markdown_text = loader.convert_file(str(file_path))
    except Exception as e:
        logger.warning("[spec_parser] MarkItDown 转换失败 %s: %s", file_path.name, e)
        return []

    if not markdown_text or not markdown_text.strip():
        logger.warning("[spec_parser] 文档内容为空: %s", file_path.name)
        return []

    sections = _split_markdown_sections(markdown_text)
    logger.info("[spec_parser] %s 切分为 %d 段", file_path.name, len(sections))

    knowledge_blocks: List[Dict[str, Any]] = []
    block_id = 0

    # 合并过短的相邻 section
    merged_sections: List[Dict[str, Any]] = []
    buffer: Optional[Dict[str, Any]] = None

    for sec in sections:
        content = sec["content"]
        if not content.strip():
            continue
        if buffer is None:
            buffer = dict(sec)
            continue
        if len(buffer["content"]) < min_chunk_chars:
            # 合入下一段
            buffer["content"] += "\n\n" + content
            if sec["heading"]:
                buffer["section_path"] = sec["section_path"]
        else:
            merged_sections.append(buffer)
            buffer = dict(sec)
    if buffer:
        merged_sections.append(buffer)

    for sec in merged_sections:
        content = sec["content"]
        heading = sec["heading"]
        # 如果内容过长，按 max_chunk_chars 截断
        chunks = _split_long_text(content, max_chunk_chars)
        for i, chunk_text in enumerate(chunks):
            if not chunk_text.strip():
                continue
            # 把标题内容也拼入 page_content 方便检索
            if heading and heading not in chunk_text[:200]:
                full_text = f"## {heading}\n\n{chunk_text}"
            else:
                full_text = chunk_text

            meta: Dict[str, Any] = {
                "source_file": file_path.name,
                "source_pdf": str(file_path),  # 兼容旧字段名
                "block_type": "text",
                "block_id": block_id,
                "section_title": _remove_section_number(heading),
                "parent_section": sec["parent_heading"],
                "section_path": sec["section_path"],
                "document_category": document_category,
            }
            if product_module:
                meta["product_module"] = product_module
            if len(chunks) > 1:
                meta["chunk_part"] = i + 1

            knowledge_blocks.append({
                "page_content": full_text,
                "metadata": meta,
            })
            block_id += 1

    logger.info(
        "[spec_parser] %s -> %d 个知识块 (category=%s)",
        file_path.name, len(knowledge_blocks), document_category,
    )
    return knowledge_blocks


def _split_long_text(text: str, max_chars: int) -> List[str]:
    """将过长文本按段落边界切分"""
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def _remove_section_number(title: str) -> str:
    """去除章节标题开头的编号前缀。

    支持格式：
    - 数字编号（含可选末尾点）: "4. " "4.1 " "1.2.3. "
    - 大写字母附录编号: "A. " "B.1 "
    - 括号数字: "(1) " "1) "
    - 中文章节: "第4章 " "第四章 " "第4节 "
    """
    if not title:
        return title
    # 数字编号
    title = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", title)
    # 大写字母附录编号（如 "A. Overview"）
    title = re.sub(r"^[A-Z](?:\.\d+)*\.?\s+", "", title)
    # 括号数字: "(1) " 或 "1) "
    title = re.sub(r"^\(\d+\)\s+", "", title)
    title = re.sub(r"^\d+\)\s+", "", title)
    # 中文章节（支持阿拉伯数字和中文数字）
    title = re.sub(r"^第[0-9一二三四五六七八九十百千]+[章节篇]\s*", "", title)
    return title.strip()
