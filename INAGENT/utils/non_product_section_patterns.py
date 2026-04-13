# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Non-product heuristics: section titles + body keywords (single source).

Titles: merge/IngestValidator regex on section_title; farm_owner and
knowledge_linker substring on title. Body: legal phrases in chunk text.

See KnowledgeQualityInspectorAgent.validate_non_product_knowledge_patterns.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import FrozenSet, Pattern

# Former ingest regex alternates + former knowledge_schema titles (merged).
_NON_PRODUCT_SECTION_TITLE_PHRASES: tuple[str, ...] = (
    # Former ingest long-regex alternates
    "版权",
    "商标",
    "合格声明",
    "免责",
    "关于我们",
    "公司简介",
    "编写目的",
    "适用对象",
    "读者对象",
    "电源要求",
    "防静电",
    "温湿度",
    "温度湿度",
    "空气质量",
    "通风条件",
    "安全措施",
    "安装过程",
    "安装环境",
    "设备上电",
    "内网连接",
    "外线连接",
    "连接电源",
    "命令行介绍",
    "命令行符号含义",
    "命令行快捷键",
    "访问控制的级别",
    # Former knowledge_schema extras (EN + longer titles)
    "版权声明",
    "商标声明",
    "联系我们",
    "附录",
    "版本说明",
    "前言",
    "修订记录",
    "文档约定",
    "免责声明",
    "about us",
    "contact us",
    "disclaimer",
    "revision history",
)

# farm_owner / knowledge_linker substring membership
NON_KNOWLEDGE_TITLE_PATTERNS: FrozenSet[str] = frozenset(
    _NON_PRODUCT_SECTION_TITLE_PHRASES
)


@lru_cache(maxsize=1)
def non_product_section_title_regex() -> Pattern[str]:
    """Compile merged regex; longer phrases first."""
    ordered = sorted(
        set(_NON_PRODUCT_SECTION_TITLE_PHRASES),
        key=len,
        reverse=True,
    )
    return re.compile("|".join(re.escape(p) for p in ordered))


# Former knowledge_schema NON_KNOWLEDGE_CONTENT_KEYWORDS (body / legal signals)
_NON_KNOWLEDGE_CONTENT_KEYWORDS_PHRASES: tuple[str, ...] = (
    "版权所有",
    "商标声明",
    "保留所有权利",
    "未经许可",
    "不得复制",
    "copyright",
    "trademark",
    "all rights reserved",
    "registered trademark",
    "注册商标",
)

NON_KNOWLEDGE_CONTENT_KEYWORDS: FrozenSet[str] = frozenset(
    _NON_KNOWLEDGE_CONTENT_KEYWORDS_PHRASES
)


__all__ = [
    "NON_KNOWLEDGE_TITLE_PATTERNS",
    "NON_KNOWLEDGE_CONTENT_KEYWORDS",
    "non_product_section_title_regex",
    "_NON_PRODUCT_SECTION_TITLE_PHRASES",
    "_NON_KNOWLEDGE_CONTENT_KEYWORDS_PHRASES",
]
