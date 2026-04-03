# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""MinerU table_body → plain text (auto_convert)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.data_tools.auto_convert import (  # noqa: E402
    _extract_text_from_block,
    _table_body_html_to_plain_text,
)


def test_table_body_html_preserves_ircookie_cross_ref():
    html = (
        "<table><tr><td>cookie_name</td><td>插入cookie的值…"
        "cookie的值由命令“slb mode ircookie”确定,默认使用ASCII值。</td></tr></table>"
    )
    plain = _table_body_html_to_plain_text(html)
    assert "slb mode ircookie" in plain
    assert "cookie_name" in plain


def test_extract_text_from_block_table_type():
    block = {
        "type": "table",
        "page_idx": 143,
        "table_caption": ["参数说明"],
        "table_footnote": [],
        "table_body": (
            "<table><tr><td>a</td><td>desc a</td></tr>"
            "<tr><td>b</td><td>desc b</td></tr></table>"
        ),
    }
    out = _extract_text_from_block(block)
    assert "参数说明" in out
    assert "a — desc a" in out
    assert "b — desc b" in out


def test_extract_text_from_block_prefers_text_over_table():
    block = {
        "type": "table",
        "text": "  heading line  ",
        "table_body": "<table><tr><td>x</td><td>y</td></tr></table>",
    }
    assert _extract_text_from_block(block).strip() == "heading line"


def test_empty_table_body_falls_through_to_content():
    block = {
        "type": "table",
        "table_body": "   ",
        "content": {
            "paragraph_content": [{"content": "fallback"}],
        },
    }
    assert "fallback" in _extract_text_from_block(block)
