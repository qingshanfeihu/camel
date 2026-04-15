# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Section-context resolution tests for auto_convert."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import INAGENT.data_tools.auto_convert as ac  # noqa: E402
from INAGENT.data_tools.auto_convert import (  # noqa: E402
    _build_section_context_map,
    _infer_section_level_from_heading,
)


def test_section_context_prefers_numbering_over_mineru_text_level() -> None:
    blocks = [
        {
            "type": "title",
            "text_level": 1,
            "text": "9.8.1. 场景1：Active/Standby",
        },
        {
            "type": "title",
            "text_level": 1,
            "text": "9.8.1.1. 配置目标",
        },
        {
            "type": "text",
            "text": "说明：启用连接同步后，新节点可以接管已有连接。",
        },
    ]

    context = _build_section_context_map(blocks)

    assert context[1]["section_title"] == "配置目标"
    assert context[1]["parent_section"] == "场景1：Active/Standby"
    assert context[1]["section_path"] == "场景1：Active/Standby > 配置目标"
    assert context[1]["section_strategy"] == "numbering"
    assert context[1]["section_source"] == "numbering"
    assert context[1]["section_health_score"] >= 0.7

    assert context[2]["section_title"] == "配置目标"
    assert context[2]["parent_section"] == "场景1：Active/Standby"
    assert context[2]["section_source"] == "numbering"


def test_section_context_falls_back_to_mineru_for_unnumbered_heading() -> None:
    blocks = [
        {
            "type": "title",
            "text_level": 1,
            "text": "1. 总览",
        },
        {
            "type": "title",
            "text_level": 2,
            "text": "部署要求",
        },
        {
            "type": "text",
            "text": "需要先配置业务接口与缺省路由。",
        },
    ]

    context = _build_section_context_map(blocks)

    assert context[1]["section_title"] == "部署要求"
    assert context[1]["parent_section"] == "总览"
    assert context[1]["section_path"] == "总览 > 部署要求"
    assert context[1]["section_strategy"] == "mixed"
    assert context[1]["section_source"] == "mineru"
    assert context[1]["section_health_score"] >= 0.45

    assert context[2]["section_title"] == "部署要求"
    assert context[2]["parent_section"] == "总览"
    assert context[2]["section_source"] == "mineru"


def test_section_context_leaves_undecidable_blocks_empty() -> None:
    blocks = [
        {
            "type": "text",
            "text": "这是正文，不应该被识别成标题。",
        },
        {
            "type": "title",
            "text": "这是一个很长的说明句子，因为末尾有句号，所以仍应视为正文。",
        },
    ]

    context = _build_section_context_map(blocks)

    assert context[0]["section_title"] == ""
    assert context[0]["parent_section"] == ""
    assert context[0]["section_path"] == ""
    assert context[0]["section_strategy"] == "empty"
    assert context[0]["section_source"] == "empty"
    assert context[0]["section_health_score"] == 0.0

    assert context[1]["section_title"] == ""
    assert context[1]["parent_section"] == ""
    assert context[1]["section_path"] == ""
    assert context[1]["section_strategy"] == "empty"
    assert context[1]["section_source"] == "empty"
    assert context[1]["section_health_score"] == 0.0


@pytest.mark.parametrize(
    ("title", "expected_level"),
    [
        ("A.1.2. 附录配置", 3),
        ("I. 总体说明", 1),
        ("（三）部署模式", 2),
        ("1）安装前检查", 1),
        ("一、系统要求", 1),
    ],
)
def test_infer_section_level_supports_richer_numbering_patterns(
    title: str,
    expected_level: int,
) -> None:
    assert _infer_section_level_from_heading(title) == expected_level


def test_section_context_chaotic_numbering_prefers_mineru_strategy() -> None:
    blocks = [
        {"type": "title", "text_level": 1, "text": "1. 总览"},
        {"type": "title", "text_level": 2, "text": "A）部署要求"},
        {"type": "title", "text_level": 2, "text": "（三）运行约束"},
        {"type": "text", "text": "正文说明。"},
    ]

    context = _build_section_context_map(blocks)

    assert context[1]["section_strategy"] == "mineru"
    assert context[1]["section_source"] == "mineru"
    assert context[1]["parent_section"] == "总览"
    assert context[2]["section_strategy"] == "mineru"
    assert context[2]["section_source"] == "mineru"
    assert context[2]["section_health_score"] < 1.0


def test_iter_mineru_extra_files_supports_images_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_root = tmp_path / "knowledge_base"
    kb_root.mkdir()
    (kb_root / "topology.png").write_text("png", encoding="utf-8")
    (kb_root / "manual.html").write_text("<html></html>", encoding="utf-8")
    (kb_root / "notes.txt").write_text("txt", encoding="utf-8")
    reference_dir = kb_root / "reference"
    reference_dir.mkdir()
    logs_dir = kb_root / "logs"
    logs_dir.mkdir()
    backup_dir = kb_root / "backups"
    backup_dir.mkdir()

    monkeypatch.setattr(ac, "DOC_LOCAL_DIR", kb_root)
    monkeypatch.setattr(ac, "REFERENCE_DIR", reference_dir)

    files = ac._iter_mineru_extra_files()
    names = sorted(path.name for path in files)

    assert names == ["topology.png"]