# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
INAGENT Web 启动入口

用法:
  python INAGENT/run_inagent_web.py
  python INAGENT/run_inagent_web.py --host 0.0.0.0 --port 8010 --reload
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


# 允许通过 `python INAGENT/run_inagent_web.py` 直接启动。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 INAGENT Web 平台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", default=8010, type=int, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    missing = []
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")
    try:
        import multipart  # noqa: F401  # python-multipart package
    except ImportError:
        missing.append("python-multipart")

    if missing:
        deps = " ".join(missing)
        raise SystemExit(
            f"缺少依赖: {deps}。请先安装: pip install {' '.join(missing)}"
        )

    import uvicorn

    uvicorn.run(
        "INAGENT.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
