"""cli_tree_viz 载荷构建单测（不依赖完整 cli_keyword_graph.json）。"""
import json
from pathlib import Path

from INAGENT.tools.cli_tree_viz.payload import (
    build_vis_payload,
    embed_json_for_html,
    load_skeleton_node_ids,
)


class _FakeCLI:
    def extract_subgraph(self, hints, max_nodes=60, max_depth=2):
        return {
            "nodes": [
                {"id": "mod_a", "type": "module", "label": "ModA"},
                {"id": "cmd_b", "type": "command", "label": "cmd b", "parent": "mod_a"},
            ],
            "edges": [
                {"source": "mod_a", "target": "cmd_b", "type": "contains"},
                {"source": "mod_a", "target": "orphan", "type": "contains"},
            ],
            "modules": ["mod_a"],
        }


def test_build_vis_payload_filters_orphan_edges():
    p = build_vis_payload(_FakeCLI(), ["x"], max_nodes=10, max_depth=1, kb_path=None)
    assert len(p["nodes"]) == 2
    assert len(p["edges"]) == 1
    assert p["edges"][0]["from"] == "mod_a"


def test_skeleton_coloring(tmp_path: Path):
    kb = tmp_path / "kb.json"
    kb.write_text(
        json.dumps(
            [{"metadata": {"node_id": "cmd_b", "command_prefix": "cmd b"}}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    p = build_vis_payload(_FakeCLI(), ["x"], kb_path=kb)
    by_id = {n["id"]: n for n in p["nodes"]}
    assert by_id["cmd_b"]["color"]["borderWidth"] == 3
    assert by_id["mod_a"]["color"]["borderWidth"] == 1


def test_load_skeleton_node_ids_empty_file(tmp_path: Path):
    assert load_skeleton_node_ids(tmp_path / "missing.json") == set()


def test_embed_json_for_html_roundtrip():
    payload = {"nodes": [], "edges": [], "meta": {"hints": ['a"b']}}
    s = embed_json_for_html(payload)
    assert json.loads(json.loads(s)) == payload
