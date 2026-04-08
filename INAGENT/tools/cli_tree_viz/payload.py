# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""从 CLIGraphStore 子图与可选骨架构建 vis-network 载荷。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_KB = (
    Path(__file__).resolve().parent.parent.parent
    / "knowledge_base"
    / "reference"
    / "knowledge_base.json"
)


def load_skeleton_node_ids(kb_path: Optional[Path]) -> Set[str]:
    """收集骨架中出现过 metadata.node_id 的集合。"""
    if not kb_path or not kb_path.is_file():
        return set()
    try:
        data = json.loads(kb_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("无法读取骨架 %s: %s", kb_path, exc)
        return set()
    out: Set[str] = set()
    if not isinstance(data, list):
        return out
    for entry in data:
        meta = entry.get("metadata") if isinstance(entry, dict) else None
        if not isinstance(meta, dict):
            continue
        nid = meta.get("node_id") or meta.get("tree_node_id")
        if nid:
            out.add(str(nid))
    return out


def _node_color(ntype: str, has_skeleton: bool) -> Dict[str, Any]:
    base = {
        "module": {"background": "#4A90D9", "border": "#2E5A8A"},
        "command": {"background": "#7CB342", "border": "#558B2F"},
        "operation_command": {"background": "#FF9800", "border": "#E65100"},
    }.get(ntype, {"background": "#9E9E9E", "border": "#616161"})
    if has_skeleton:
        return {
            "background": base["background"],
            "border": "#00C853",
            "borderWidth": 3,
        }
    return {
        "background": base["background"],
        "border": base["border"],
        "borderWidth": 1,
    }


def _tooltip(node: Dict[str, Any], has_skeleton: bool) -> str:
    lines = [
        f"type: {node.get('type', '')}",
        f"id: {node.get('id', '')}",
        f"label: {node.get('label', '')}",
        f"skeleton: {'yes' if has_skeleton else 'no'}",
    ]
    desc = node.get("description")
    if desc:
        lines.append("description: " + str(desc)[:400])
    parent = node.get("parent")
    if parent:
        lines.append(f"parent: {parent}")
    return "\n".join(lines)


def build_vis_payload(
    cli_graph_store: Any,
    module_hints: List[str],
    max_nodes: int = 80,
    max_depth: int = 2,
    kb_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """调用 ``extract_subgraph``，输出 vis-network 可用的 nodes/edges 与 meta。"""
    hints = [h.strip() for h in module_hints if h and str(h).strip()]
    if not hints:
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "modules": [],
                "hints": [],
                "skeleton_node_count": 0,
                "skeleton_loaded": bool(kb_path and kb_path.is_file()),
            },
        }

    sg = cli_graph_store.extract_subgraph(
        hints, max_nodes=max_nodes, max_depth=max_depth
    )
    raw_nodes: List[Dict[str, Any]] = sg.get("nodes") or []
    raw_edges: List[Dict[str, Any]] = sg.get("edges") or []

    sk_ids = load_skeleton_node_ids(kb_path)
    node_ids = {str(n.get("id", "")) for n in raw_nodes if n.get("id")}

    vis_nodes: List[Dict[str, Any]] = []
    for n in raw_nodes:
        nid = str(n.get("id", ""))
        if not nid:
            continue
        ntype = str(n.get("type", ""))
        has_sk = nid in sk_ids
        label = str(n.get("label", nid))
        if len(label) > 48:
            label = label[:45] + "..."
        vis_nodes.append(
            {
                "id": nid,
                "label": label,
                "title": _tooltip(n, has_sk),
                "group": ntype,
                "color": _node_color(ntype, has_sk),
            }
        )

    vis_edges: List[Dict[str, Any]] = []
    seen_e = set()
    for e in raw_edges:
        src, tgt = str(e.get("source", "")), str(e.get("target", ""))
        if not src or not tgt or src not in node_ids or tgt not in node_ids:
            continue
        et = str(e.get("type", ""))
        key = (src, tgt, et)
        if key in seen_e:
            continue
        seen_e.add(key)
        vis_edges.append(
            {
                "from": src,
                "to": tgt,
                "title": et,
                "label": et[:20] if et else "",
                "arrows": "to",
                "font": {"align": "middle", "size": 10},
            }
        )

    return {
        "nodes": vis_nodes,
        "edges": vis_edges,
        "meta": {
            "modules": sg.get("modules") or [],
            "hints": hints,
            "skeleton_node_count": len(sk_ids),
            "skeleton_loaded": bool(kb_path and kb_path.is_file()),
            "max_nodes": max_nodes,
            "max_depth": max_depth,
        },
    }


def embed_json_for_html(payload: Dict[str, Any]) -> str:
    """生成可嵌入 ``JSON.parse(...)`` 的 JS 参数字符串（已转义）。"""
    inner = json.dumps(payload, ensure_ascii=False)
    return json.dumps(inner)
