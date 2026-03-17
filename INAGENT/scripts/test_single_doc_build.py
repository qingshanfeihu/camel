"""
独立脚本：跳过 auto-init，直接调用 graphrag.api.build_index(config)
用于单文档测试，documents.json 已人工准备好。
"""
import asyncio
import sys
from pathlib import Path

# 确保项目根路径在 sys.path
ROOT = Path(__file__).resolve().parent.parent.parent  # INFOAGEN/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "INAGENT" / "graphrag"))

WORKSPACE = ROOT / "INAGENT" / "graphrag_index"


async def main():
    from graphrag.api import build_index
    from graphrag.config.load_config import load_config

    print(f"[INFO] Loading config from {WORKSPACE}")
    config = load_config(WORKSPACE)

    print("[INFO] Starting build_index (single-doc test) ...")
    results = await build_index(config)

    errors = []
    for r in results:
        print(f"  workflow={r.workflow:<30s}  errors={r.errors}")
        if r.errors:
            errors.extend(r.errors)

    if errors:
        print(f"\n[ERROR] Build had errors: {errors}")
        sys.exit(1)

    print("\n[OK] Build completed. Checking output ...")

    import pandas as pd
    output = WORKSPACE / "output"
    for name in ("documents", "text_units", "entities", "relationships",
                 "communities", "community_reports"):
        p = output / f"{name}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            print(f"  {name:25s}: {len(df):>6d} rows")
        else:
            print(f"  {name:25s}: NOT FOUND")


if __name__ == "__main__":
    asyncio.run(main())
