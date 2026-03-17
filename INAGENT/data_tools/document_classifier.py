# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
文档分类器：根据文件名模式和内容自动分类文档

分类体系：
- spec/prd: 产品需求文档
- spec/func_spec: 软件功能规格书
- spec/design: 软件设计文档
- test/test_list: 测试列表（xlsx/xls 表格）
- test/test_strategy: 测试策略/计划文档
- test/test_template: 测试用例模板
"""
import re
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 文件名 → 分类规则（按优先级排序, 先匹配先命中）
_FILENAME_PATTERNS: list[Tuple[str, str]] = [
    # test 类
    (r"test.*list|测试.*列表|测试.*清单", "test/test_list"),
    (r"test.*template|测试.*模板|用例.*模板", "test/test_template"),
    (r"test.*strateg|test.*plan|测试.*策略|测试.*计划", "test/test_strategy"),
    # spec 类
    (r"func.*spec|功能.*规格|sw.*functional", "spec/func_spec"),
    (r"prd|product.*require|产品.*需求", "spec/prd"),
    (r"design|设计.*文档|sw.*design|软件.*设计", "spec/design"),
]

# 内容关键词 → 分类（当文件名无法判断时，用内容辅助）
_CONTENT_KEYWORDS: Dict[str, list[str]] = {
    "spec/func_spec": [
        "Function Specification",
        "Testing Consideration",
        "功能规格",
        "Definitions",
        "SW Functional Specification",
    ],
    "spec/prd": [
        "Product Requirement",
        "产品需求",
        "PRD",
        "需求说明",
    ],
    "spec/design": [
        "Software Design",
        "设计文档",
        "Design Document",
        "架构设计",
    ],
    "test/test_list": [
        "Test Types",
        "Expected Result",
        "Sub Item",
        "Test Build",
        "测试类型",
        "期望结果",
    ],
    "test/test_strategy": [
        "Test Strategy",
        "测试策略",
        "Test Plan",
        "测试计划",
    ],
    "test/test_template": [
        "XXX子功能",
        "YYY子功能",
        "测试用例模板",
    ],
}


def classify_document(
    file_path: Path,
    content_preview: str = "",
) -> Tuple[str, float]:
    """
    分类文档，返回 (document_category, confidence)。

    Args:
        file_path: 文件路径
        content_preview: 可选的内容预览（前 2000 字符）

    Returns:
        (document_category, confidence)  confidence 0.0~1.0
    """
    filename = file_path.stem.lower()
    suffix = file_path.suffix.lower()

    # 1. 文件名模式匹配（高置信度）
    for pattern, category in _FILENAME_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            logger.info("[classify] 文件名匹配: %s -> %s", file_path.name, category)
            return category, 0.9

    # 2. 扩展名启发
    # xlsx/xls 且文件名包含 test 相关词 → test/test_list
    if suffix in {".xlsx", ".xls"} and re.search(r"test|测试|check|验证", filename, re.IGNORECASE):
        return "test/test_list", 0.8

    # 3. 内容关键词匹配
    if content_preview:
        preview_lower = content_preview.lower()
        best_cat: Optional[str] = None
        best_hits = 0
        for cat, keywords in _CONTENT_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw.lower() in preview_lower)
            if hits > best_hits:
                best_hits = hits
                best_cat = cat
        if best_cat and best_hits >= 2:
            logger.info(
                "[classify] 内容匹配: %s -> %s (命中 %d 关键词)",
                file_path.name, best_cat, best_hits,
            )
            return best_cat, 0.7

    # 4. 扩展名回退
    if suffix in {".xlsx", ".xls"}:
        return "test/test_list", 0.4
    if suffix in {".docx", ".doc"}:
        return "spec/func_spec", 0.3

    return "unknown", 0.0


def infer_product_module_from_path(file_path: Path) -> Optional[str]:
    """
    从文件路径推断产品模块名。
    例如: .../HTTP2/... → HTTP2,  .../SLB/... → SLB
    """
    parts = file_path.parts
    # 搜索路径中可能的模块名目录
    known_modules = {
        "http", "http2", "https", "slb", "gslb", "llb", "waf",
        "ssl", "sip", "tcp", "udp", "ftp", "dns", "smtp",
    }
    for part in reversed(parts):
        if part.lower() in known_modules:
            return part.upper()
    return None
