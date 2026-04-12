# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.utils.chunk_text_quality import (
    detect_quality_flags,
    is_garbage_page_content,
)


def test_garbage_empty_and_short():
    assert is_garbage_page_content("")
    assert is_garbage_page_content("   ")
    assert is_garbage_page_content("x" * 9)


def test_garbage_symbol_only_long():
    assert is_garbage_page_content(" " * 50)


def test_garbage_boilerplate_line():
    assert is_garbage_page_content("目录")
    assert is_garbage_page_content("Copyright")


def test_not_garbage_substantive():
    text = (
        "slb virtual http 配置说明：此命令用于创建 HTTP 类型虚拟服务，需要指定 VIP 和端口。"
        "示例：slb virtual http test_vip 80。"
    )
    assert not is_garbage_page_content(text)


def test_detect_quality_flags_short():
    flags = detect_quality_flags("short text", "任意标题")
    assert "extremely_short" in flags


def test_detect_quality_flags_title_mismatch():
    flags = detect_quality_flags("slb virtual command syntax and examples." * 3, "版权声明")
    assert "title_content_mismatch" in flags
