"""Tests for mechanical parent/child reference merge."""

from __future__ import annotations

import json
from pathlib import Path

from INAGENT.data_tools.merge_reference_parent_child import merge_reference_parent_child


def test_merge_parent_child_moves_blocks_and_empties_child(tmp_path: Path):
    ref = tmp_path
    ref.joinpath("parent.json").write_text("[]\n", encoding="utf-8")
    ref.joinpath("child.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "from child",
                    "metadata": {"block_id": "c1", "source_file": "child.json"},
                }
            ],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = ref / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "merge_jobs": [
                    {
                        "parent": "parent.json",
                        "children": ["child.json"],
                        "child_after_merge": "empty",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = merge_reference_parent_child(ref, cfg, dry_run=False, strict_inagent=False)
    assert report.chunks_moved == 1
    assert report.child_files_emptied == 1
    parent = json.loads(ref.joinpath("parent.json").read_text(encoding="utf-8"))
    assert len(parent) == 1
    assert parent[0]["metadata"]["block_id"] == "c1"
    assert parent[0]["metadata"]["source_file"] == "parent.json"
    child = json.loads(ref.joinpath("child.json").read_text(encoding="utf-8"))
    assert child == []


def test_merge_skips_block_id_collision(tmp_path: Path):
    ref = tmp_path
    ref.joinpath("parent.json").write_text(
        json.dumps(
            [{"page_content": "p", "metadata": {"block_id": "same", "source_file": "parent.json"}}],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    ref.joinpath("child.json").write_text(
        json.dumps(
            [{"page_content": "c", "metadata": {"block_id": "same", "source_file": "child.json"}}],
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = ref / "cfg.json"
    cfg.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "merge_jobs": [{"parent": "parent.json", "children": ["child.json"]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = merge_reference_parent_child(ref, cfg, dry_run=False, strict_inagent=False)
    assert report.chunks_moved == 0
    assert report.block_id_collisions
