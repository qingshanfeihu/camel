# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Shared chunk text quality heuristics for procurement pre-clean and farm-owner uncovered paths.

Aligned with common data-prep pipelines (rule clean → dedupe/safety → quality filter);
see InternLM2-style text cleaning and RAG «dirty data» discussions. Keep patterns in one
module to avoid drift between KnowledgeProcurementAgent and KnowledgeFarmOwnerAgent.
"""
from __future__ import annotations

import re
from typing import List

_GARBAGE_PATTERNS: List[re.Pattern[str]] | None = None


def _garbage_patterns() -> List[re.Pattern[str]]:
    global _GARBAGE_PATTERNS
    if _GARBAGE_PATTERNS is None:
        _GARBAGE_PATTERNS = [
            re.compile(r"^[\s\d.。、\-—─=_*#|+/\\,，\u3000]+$"),
            re.compile(
                r"(?i)^(table\s+of\s+contents|目录|contents|copyright|版权"
                r"|all\s+rights?\s+reserved|confidential|机密|保密"
                r"|page\s*\d|第\s*\d+\s*页|图\s*\d|表\s*\d|figure\s*\d|table\s*\d)$"
            ),
            re.compile(r"(?i)^\s*(\.{3,}|…{2,}|\d+\s*\.{3,}\s*\d+)\s*$"),
        ]
    return _GARBAGE_PATTERNS


def is_garbage_page_content(page_content: str, *, min_len: int = 10) -> bool:
    """Return True if the chunk text is empty, too short, symbol-only, or boilerplate line.

    Used by farm owner ``classify_uncovered_chunks`` and procurement mechanical pre-clean
    (for chunks that already pass the minimum length gate).
    """
    text = page_content.strip()
    if not text or len(text) < min_len:
        return True
    if all(not c.isalnum() for c in text):
        return True
    for pat in _garbage_patterns():
        if pat.search(text):
            return True
    return False


def detect_quality_flags(page_content: str, section_title: str = "") -> List[str]:
    """Return lightweight quality flags used by procurement/farmer.

    Flags:
      - ``extremely_short``: content length <= 25 chars after strip.
      - ``title_content_mismatch``: section title has no char overlap with content head.
    """
    flags: List[str] = []
    content = (page_content or "").strip()
    if len(content) <= 25:
        flags.append("extremely_short")
        return flags

    st = (section_title or "").strip()
    if not st:
        return flags

    st_chars = set(st)
    content_head = set(content[:100])
    if not (st_chars & content_head - {" ", "\n", "\t"}):
        flags.append("title_content_mismatch")
    return flags
