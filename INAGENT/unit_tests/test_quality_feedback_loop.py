"""Tests for closed-loop quality feedback (inbox, proposals, purge)."""

from __future__ import annotations

import json
from pathlib import Path

from INAGENT.data_tools.quality_feedback_loop import (
    apply_purge_manifest,
    build_rule_proposals_from_inbox,
    load_and_validate_inbox,
    load_and_validate_purge_manifest,
    validate_feedback_inbox_record,
    validate_purge_manifest_record,
)


def test_validate_inbox_record_ok():
    rec, err = validate_feedback_inbox_record(
        {
            "feedback_id": "f1",
            "source_file": "app_x.json",
            "block_id": "b1",
            "verdict": "reject",
            "reason_code": "false_positive_product",
        }
    )
    assert err is None
    assert rec["gate_key"] == "app_x.json::b1"


def test_validate_inbox_rejects_underscore_source():
    _, err = validate_feedback_inbox_record(
        {
            "feedback_id": "f1",
            "source_file": "_bad.json",
            "block_id": "b1",
            "verdict": "reject",
            "reason_code": "x",
        }
    )
    assert err is not None


def test_load_inbox_jsonl(tmp_path: Path):
    inbox = tmp_path / "_quality_feedback_inbox.jsonl"
    inbox.write_text(
        '{"feedback_id":"a","source_file":"f.json","block_id":"1","verdict":"flag","reason_code":"dup"}\n'
        '{"bad":true}\n',
        encoding="utf-8",
    )
    valid, errors = load_and_validate_inbox(inbox)
    assert len(valid) == 1
    assert len(errors) >= 1


def test_rule_proposals_conflict():
    records = [
        {
            "feedback_id": "f1",
            "gate_key": "f.json::1",
            "suggested_rule": {"rule_id": "a"},
        },
        {
            "feedback_id": "f2",
            "gate_key": "f.json::1",
            "suggested_rule": {"rule_id": "b"},
        },
    ]
    doc = build_rule_proposals_from_inbox(records, inbox_path="x")
    assert len(doc["conflicts"]) == 1


def test_purge_remove_chunk_apply(tmp_path: Path):
    ref = tmp_path
    ref.joinpath("doc.json").write_text(
        json.dumps(
            [
                {"page_content": "x", "metadata": {"block_id": "keep", "source_file": "doc.json"}},
                {"page_content": "y", "metadata": {"block_id": "drop", "source_file": "doc.json"}},
            ],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    recs = [
        {
            "schema_version": "1.0",
            "manifest_id": "m1",
            "source_file": "doc.json",
            "block_id": "drop",
            "action": "remove_chunk",
            "approved_by": "test",
        }
    ]
    rpt = apply_purge_manifest(ref, recs, dry_run=False, strict_inagent=False)
    assert len(rpt.applied) == 1
    data = json.loads(ref.joinpath("doc.json").read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["metadata"]["block_id"] == "keep"


def test_purge_patch_metadata(tmp_path: Path):
    ref = tmp_path
    ref.joinpath("doc.json").write_text(
        json.dumps(
            [{"page_content": "x", "metadata": {"block_id": "b1", "source_file": "doc.json"}}],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    recs = [
        {
            "schema_version": "1.0",
            "manifest_id": "m2",
            "source_file": "doc.json",
            "block_id": "b1",
            "action": "patch_metadata",
            "approved_by": "test",
            "metadata_patch": {"owner_excluded": True},
        }
    ]
    apply_purge_manifest(ref, recs, dry_run=False, strict_inagent=False)
    data = json.loads(ref.joinpath("doc.json").read_text(encoding="utf-8"))
    assert data[0]["metadata"]["owner_excluded"] is True


def test_purge_manifest_requires_approval():
    _, err = validate_purge_manifest_record(
        {
            "schema_version": "1.0",
            "manifest_id": "x",
            "source_file": "a.json",
            "block_id": "b",
            "action": "remove_chunk",
            "approved_by": "",
        }
    )
    assert err is not None


def test_load_purge_manifest_jsonl(tmp_path: Path):
    m = tmp_path / "m.jsonl"
    m.write_text(
        '{"schema_version":"1.0","manifest_id":"1","source_file":"a.json","block_id":"b","action":"remove_chunk","approved_by":"u"}\n',
        encoding="utf-8",
    )
    valid, errors = load_and_validate_purge_manifest(m)
    assert not errors
    assert len(valid) == 1
