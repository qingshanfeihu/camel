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
    # 正文与标题无字符重叠，且正文首行不像 CLI（纯说明性中文）
    body = "存储卷的日常维护包括快照创建与回滚步骤说明。" * 3
    flags = detect_quality_flags(body, "BGP路由策略")
    assert "title_content_mismatch" in flags


def test_detect_quality_flags_cli_exempt_from_title_mismatch():
    body = ("slb virtual http <name> <vip> <port>\n配置说明与参数表。" * 2)
    flags = detect_quality_flags(body, "版权声明")
    assert "title_content_mismatch" not in flags
