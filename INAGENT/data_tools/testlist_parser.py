# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
测试列表解析器：将 xlsx/xls 测试列表转换为知识块

标准列映射（自动探测）：
  Item | Sub Item | ID | Test Types | Description | Expected Result | Priority | Release | Automated | Result | Test Build | Note

输出格式与 auto_convert.py 的 PDF 流水线兼容：
    {
        "page_content": "...",
        "metadata": { ... }
    }
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 标准列名（小写）→ metadata 字段名
_COLUMN_MAPPING: Dict[str, str] = {
    "item": "test_category",
    "sub item": "test_subcategory",
    "subitem": "test_subcategory",
    "id": "test_id",
    "test types": "test_type",
    "test type": "test_type",
    "description": "description",
    "expected result": "expected_result",
    "priority": "priority",
    "release": "release_version",
    "automated": "automated",
    "result": "result",
    "test build": "test_build",
    "note": "note",
    "bug id": "bug_id",
}

# 用于探测表头行的关键词（至少命中 3 个才认为是表头）
_HEADER_KEYWORDS = {"item", "test", "description", "expected", "priority", "result", "sub item"}


def _detect_header_row(rows: List[List[str]]) -> Tuple[int, Dict[int, str]]:
    """
    探测表头行及列映射。

    Returns:
        (header_row_index, {col_index: metadata_field_name})
    """
    for row_idx, row in enumerate(rows[:10]):  # 前 10 行
        cells = [str(c).strip().lower() for c in row]
        hits = sum(1 for c in cells if any(kw in c for kw in _HEADER_KEYWORDS))
        if hits >= 3:
            col_map: Dict[int, str] = {}
            for col_idx, cell in enumerate(cells):
                for canon, field in _COLUMN_MAPPING.items():
                    if canon == cell or canon in cell:
                        col_map[col_idx] = field
                        break
            if col_map:
                logger.info(
                    "[testlist] 探测到表头行 %d, 列映射: %s",
                    row_idx, col_map,
                )
                return row_idx, col_map
    return -1, {}


def parse_test_list(
    file_path: Path,
    product_module: Optional[str] = None,
    document_category: str = "test/test_list",
) -> List[Dict[str, Any]]:
    """
    解析 xlsx/xls 测试列表为知识块列表。

    流程：
    1. 使用 MarkItDown 转为 Markdown 表格文本
    2. 解析 Markdown 表格为行列数据
    3. 探测表头，映射到标准列名
    4. 每行生成一个知识块

    Args:
        file_path: xlsx/xls 文件路径
        product_module: 产品模块名
        document_category: 文档分类

    Returns:
        List[Dict] 兼容 knowledge_base.json 的块列表
    """
    from camel.loaders.markitdown import MarkItDownLoader

    loader = MarkItDownLoader()
    try:
        markdown_text = loader.convert_file(str(file_path))
    except Exception as e:
        logger.error("[testlist] MarkItDown 转换失败 %s: %s", file_path.name, e)
        return []

    if not markdown_text or not markdown_text.strip():
        logger.warning("[testlist] 文件内容为空: %s", file_path.name)
        return []

    # 解析 Markdown 表格
    rows = _parse_markdown_table(markdown_text)
    if not rows:
        logger.warning("[testlist] 无法解析表格: %s", file_path.name)
        return []

    header_idx, col_map = _detect_header_row(rows)
    if header_idx < 0 or not col_map:
        logger.warning("[testlist] 未探测到有效表头: %s", file_path.name)
        # 回退：将整个内容作为一个块
        return [{
            "page_content": markdown_text[:5000],
            "metadata": {
                "source_file": file_path.name,
                "source_pdf": str(file_path),
                "document_category": document_category,
                "block_type": "table",
                "block_id": 0,
                **({"product_module": product_module} if product_module else {}),
            },
        }]

    # 当前 Item / Sub Item 用于前向填充（某些行省略了 Item 列）
    current_item = ""
    current_sub_item = ""
    knowledge_blocks: List[Dict[str, Any]] = []

    for row_idx, row in enumerate(rows):
        if row_idx <= header_idx:
            continue  # 跳过表头及之前的行

        # 提取各列值
        row_data: Dict[str, str] = {}
        for col_idx, field_name in col_map.items():
            if col_idx < len(row):
                val = str(row[col_idx]).strip()
                if val and val != "-" and val.lower() != "nan":
                    row_data[field_name] = val

        # 前向填充 Item / Sub Item
        if row_data.get("test_category"):
            current_item = row_data["test_category"]
        else:
            row_data["test_category"] = current_item
        if row_data.get("test_subcategory"):
            current_sub_item = row_data["test_subcategory"]
        else:
            row_data["test_subcategory"] = current_sub_item

        # 跳过空行
        description = row_data.get("description", "")
        if not description and not row_data.get("expected_result"):
            continue

        # 构建 page_content：自然语言描述
        text_parts = []
        if row_data.get("test_category"):
            text_parts.append(f"测试项: {row_data['test_category']}")
        if row_data.get("test_subcategory"):
            text_parts.append(f"子项: {row_data['test_subcategory']}")
        if row_data.get("test_type"):
            text_parts.append(f"测试类型: {row_data['test_type']}")
        if description:
            text_parts.append(f"描述: {description}")
        if row_data.get("expected_result"):
            text_parts.append(f"期望结果: {row_data['expected_result']}")
        if row_data.get("priority"):
            text_parts.append(f"优先级: {row_data['priority']}")

        page_content = "\n".join(text_parts)
        if not page_content.strip():
            continue

        # metadata
        meta: Dict[str, Any] = {
            "source_file": file_path.name,
            "source_pdf": str(file_path),
            "block_type": "table_row",
            "block_id": len(knowledge_blocks),
            "document_category": document_category,
        }
        if product_module:
            meta["product_module"] = product_module

        # 将所有解析到的列值放入 metadata
        for field, value in row_data.items():
            if value:
                meta[field] = value

        # 从 test_category 构建 section_title
        section_parts = []
        if row_data.get("test_category"):
            section_parts.append(row_data["test_category"])
        if row_data.get("test_subcategory"):
            section_parts.append(row_data["test_subcategory"])
        if section_parts:
            meta["section_title"] = " > ".join(section_parts)

        knowledge_blocks.append({
            "page_content": page_content,
            "metadata": meta,
        })

    logger.info(
        "[testlist] %s -> %d 个测试用例块 (category=%s)",
        file_path.name, len(knowledge_blocks), document_category,
    )
    return knowledge_blocks


def _parse_markdown_table(markdown_text: str) -> List[List[str]]:
    """
    解析 Markdown 文本中的表格为行列数据。
    支持标准 Markdown 表格格式（| 分隔）。
    """
    rows: List[List[str]] = []
    for line in markdown_text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        # 跳过分隔行 (|---|---|...)
        if re.match(r"^\|[\s\-:]+\|$", line) or re.match(r"^\|(\s*[-:]+\s*\|)+\s*$", line):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]  # 去掉首尾空
        if cells:
            rows.append(cells)
    return rows
