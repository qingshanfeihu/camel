# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""KnowledgeQualityInspectorAgent 单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.agents.knowledge_quality_inspector_agent import (
    KnowledgeQualityInspectorAgent,
    chunk_decision_from_export_dict,
    filter_accepted_chunks,
    normalize_chunk_for_compare,
)


def _minimal_decision_item(action: str, idx: int, content: str) -> dict:
    return {
        "chunk_index": idx,
        "source_file": "t.pdf",
        "chunk": {
            "page_content": content,
            "metadata": {
                "source_file": "t.pdf",
                "document_category": "cli/reference",
            },
        },
        "decision": {
            "action": action,
            "target_kb": "product" if action == "accept" else "unknown",
            "confidence": 0.9,
            "reason": "test",
            "schema_gap": None,
            "suggested_value": "cli/reference" if action == "accept" else None,
            "reason_code": None,
            "rule_layer": None,
            "rule_confidence": None,
        },
    }


# QI-01 导出一致（契约层，见 test_data/质检员输出/README.md）
def test_validate_procurement_export_ok(tmp_path: Path) -> None:
    items = [
        _minimal_decision_item("reject", 0, "x" * 60),
        _minimal_decision_item("accept", 1, "y" * 60),
    ]
    dec_path = tmp_path / "decisions.json"
    dec_path.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    decisions = [chunk_decision_from_export_dict(x) for x in items]
    accepted = filter_accepted_chunks(decisions)
    acc_path = tmp_path / "accepted_for_farmer.json"
    acc_path.write_text(
        json.dumps(accepted, ensure_ascii=False), encoding="utf-8"
    )
    r = KnowledgeQualityInspectorAgent.validate_procurement_export(
        dec_path, acc_path
    )
    assert r.ok
    assert r.accept_actions_in_decisions == 1
    assert r.expected_count == 1


# QI-02 长度 / 列表不一致
def test_validate_procurement_export_length_mismatch(tmp_path: Path) -> None:
    items = [_minimal_decision_item("accept", 0, "y" * 60)]
    dec_path = tmp_path / "decisions.json"
    dec_path.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    acc_path = tmp_path / "accepted_for_farmer.json"
    acc_path.write_text("[]", encoding="utf-8")
    r = KnowledgeQualityInspectorAgent.validate_procurement_export(
        dec_path, acc_path
    )
    assert not r.ok
    assert "length mismatch" in (r.mismatch_detail or "")


# QI-03 镜像 text 与 page_content
def test_normalize_chunk_strips_mirrored_text() -> None:
    c = {"page_content": "hello", "text": "hello", "metadata": {}}
    n = normalize_chunk_for_compare(c)
    assert "text" not in n
    assert n["page_content"] == "hello"


# QI-04 summary.actions.accept 与 decisions accept 一致
def test_validate_summary_accept_count(tmp_path: Path) -> None:
    items = [_minimal_decision_item("accept", 0, "y" * 60)]
    dec_path = tmp_path / "decisions.json"
    dec_path.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    summ_path = tmp_path / "summary.json"
    summ_path.write_text(
        json.dumps({"actions": {"accept": 1}}),
        encoding="utf-8",
    )
    c = KnowledgeQualityInspectorAgent.validate_summary_accept_count(
        dec_path, summ_path
    )
    assert c.ok
    assert c.summary_accept == 1


# QI-05 non_product_section_patterns 冒烟
def test_validate_non_product_knowledge_patterns() -> None:
    insp = KnowledgeQualityInspectorAgent
    r = insp.validate_non_product_knowledge_patterns()
    assert r.ok, r.detail
    assert r.phrase_count >= 10
    assert r.content_keyword_count >= 8


def test_validate_non_product_section_patterns_alias() -> None:
    insp = KnowledgeQualityInspectorAgent
    a = insp.validate_non_product_section_patterns()
    b = insp.validate_non_product_knowledge_patterns()
    assert a.ok and b.ok
    assert a.phrase_count == b.phrase_count
    assert a.content_keyword_count == b.content_keyword_count


def test_validate_summary_mismatch(tmp_path: Path) -> None:
    items = [_minimal_decision_item("accept", 0, "y" * 60)]
    dec_path = tmp_path / "decisions.json"
    dec_path.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    summ_path = tmp_path / "summary.json"
    summ_path.write_text(
        json.dumps({"actions": {"accept": 99}}),
        encoding="utf-8",
    )
    c = KnowledgeQualityInspectorAgent.validate_summary_accept_count(
        dec_path, summ_path
    )
    assert not c.ok
