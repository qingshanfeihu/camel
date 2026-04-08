#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 子图可视化 HTTP 服务（FastAPI）。

依赖（未列入主 pyproject 时须自行安装）::

    pip install fastapi uvicorn

运行（仓库根 INFOAGEN，PYTHONPATH 含当前目录）::

    python -m INAGENT.tools.cli_tree_viz.server --host 127.0.0.1 --port 8765

浏览器打开 http://127.0.0.1:8765/ ，用表单或 URL 参数 ``?module=slb&max_nodes=80`` 加载子图。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent
_DEFAULT_GRAPH = (
    Path(__file__).resolve().parent.parent.parent / "knowledge_base" / "cli_keyword_graph.json"
)
_DEFAULT_KB = (
    Path(__file__).resolve().parent.parent.parent
    / "knowledge_base"
    / "reference"
    / "knowledge_base.json"
)

_cli_store = None


def _get_cli(graph_path: Optional[Path]):
    global _cli_store
    from INAGENT.rag.cli_graph_store import CLIGraphStore

    path = graph_path or _DEFAULT_GRAPH
    if _cli_store is None or getattr(_cli_store, "_path", None) != path:
        _cli_store = CLIGraphStore(graph_path=path)
        _cli_store._ensure_loaded()
    return _cli_store


def create_app(
    graph_path: Optional[Path] = None,
    kb_path: Optional[Path] = None,
):
    try:
        from fastapi import FastAPI, Query
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "需要安装 fastapi 与 uvicorn：pip install fastapi uvicorn"
        ) from exc

    from INAGENT.tools.cli_tree_viz.payload import build_vis_payload

    graph_resolved = graph_path or Path(
        os.environ.get("CLI_KEYWORD_GRAPH", str(_DEFAULT_GRAPH))
    )
    kb_resolved = kb_path or Path(os.environ.get("CLI_VIZ_KB", str(_DEFAULT_KB)))

    app = FastAPI(title="CLI subgraph viz", version="1.0")

    @app.get("/api/subgraph")
    def api_subgraph(
        module: str = Query("slb", description="逗号分隔的 module hint，与 extract_subgraph 一致"),
        max_nodes: int = Query(80, ge=5, le=500),
        max_depth: int = Query(2, ge=1, le=8),
        skeleton: int = Query(1, ge=0, le=1),
    ):
        hints = [s.strip() for s in module.split(",") if s.strip()]
        if not hints:
            hints = ["slb"]
        kb = kb_resolved if skeleton else None
        cli = _get_cli(graph_resolved if graph_resolved.is_file() else None)
        payload = build_vis_payload(
            cli, hints, max_nodes=max_nodes, max_depth=max_depth, kb_path=kb
        )
        return JSONResponse(payload)

    index_html = (_APP_DIR / "index_server.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(index_html)

    return app


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="CLI 子图可视化 FastAPI 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--graph", type=Path, default=None, help="cli_keyword_graph.json")
    p.add_argument("--kb", type=Path, default=None, help="knowledge_base.json 骨架")
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        logger.error("需要 uvicorn：pip install uvicorn")
        return 1

    app = create_app(graph_path=args.graph, kb_path=args.kb)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
