#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出单文件 HTML：CLIGraphStore 子图 + 可选骨架着色。

用法（仓库根目录，已配置 PYTHONPATH 含 INFOAGEN）::

    python -m INAGENT.tools.cli_tree_viz.export_viz --module slb --out cli_viz.html

多关键词::

    python -m INAGENT.tools.cli_tree_viz.export_viz -m slb -m directfwd --out viz.html

禁用骨架着色::

    python -m INAGENT.tools.cli_tree_viz.export_viz --module slb --no-skeleton
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from INAGENT.rag.cli_graph_store import CLIGraphStore
from INAGENT.tools.cli_tree_viz.payload import (
    _DEFAULT_KB,
    build_vis_payload,
    embed_json_for_html,
)


def export_html(
    out_path: Path,
    module_hints: list[str],
    max_nodes: int,
    max_depth: int,
    graph_path: Path | None,
    kb_path: Path | None,
    use_skeleton: bool,
) -> None:
    cli = CLIGraphStore(graph_path=graph_path)
    cli._ensure_loaded()
    kb = kb_path if use_skeleton else None
    payload = build_vis_payload(
        cli, module_hints, max_nodes=max_nodes, max_depth=max_depth, kb_path=kb
    )
    tpl_dir = Path(__file__).resolve().parent
    template = (tpl_dir / "template_export.html").read_text(encoding="utf-8")
    arg = embed_json_for_html(payload)
    html = template.replace("__GRAPH_PARSE_ARG__", arg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="导出 CLI 子图为单页 HTML（vis-network）")
    p.add_argument(
        "-m",
        "--module",
        action="append",
        dest="modules",
        default=[],
        help="模块/关键词 hint，可重复（与 CLIGraphStore.extract_subgraph 一致）",
    )
    p.add_argument(
        "--out",
        "-o",
        type=Path,
        default=Path("cli_subgraph_viz.html"),
        help="输出 HTML 路径",
    )
    p.add_argument("--max-nodes", type=int, default=80)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="cli_keyword_graph.json 路径（默认 knowledge_base 下）",
    )
    p.add_argument(
        "--kb",
        type=Path,
        default=None,
        help="knowledge_base.json 骨架路径（默认 reference/knowledge_base.json）",
    )
    p.add_argument(
        "--no-skeleton",
        action="store_true",
        help="不读取骨架，不做绿边「有文档」标记",
    )
    args = p.parse_args(argv)
    hints = args.modules or ["slb"]
    graph_path = args.graph
    kb_path = args.kb if args.kb is not None else _DEFAULT_KB
    export_html(
        args.out,
        hints,
        args.max_nodes,
        args.max_depth,
        graph_path,
        kb_path,
        use_skeleton=not args.no_skeleton,
    )
    print(f"Wrote {args.out.resolve()} ({len(hints)} hint(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
