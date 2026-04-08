#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库根目录执行：导出 CLI 子图单页 HTML。

    python INAGENT/scripts/export_cli_subgraph_viz.py --module slb -o cli_viz.html
"""
from INAGENT.tools.cli_tree_viz.export_viz import main

if __name__ == "__main__":
    raise SystemExit(main())
