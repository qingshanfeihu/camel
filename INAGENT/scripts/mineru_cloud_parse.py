"""Upload PDFs to MinerU Cloud API (v4) and download parsed results.

Usage:
    python -m INAGENT.scripts.mineru_cloud_parse

Flow: see ``INAGENT.data_tools.mineru_cloud_client``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from INAGENT.data_tools.mineru_cloud_client import mineru_cloud_parse_pdfs

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "knowledge_base"
INPUT_DIR = KB_DIR / "input"
MINERU_OUTPUT_DIR = KB_DIR / "mineru_output"


def _load_token() -> str:
    t = os.environ.get("MINERU_API_TOKEN", "").strip()
    if t:
        return t
    t = os.environ.get("MINERU_API_KEY", "").strip()
    if t:
        return t
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("MINERU_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("MINERU_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def validate_results(extracted_paths: list) -> None:
    for p in extracted_paths:
        p = Path(p)
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            count = len(data)
            page_indices = {
                block["page_idx"]
                for block in data
                if isinstance(block, dict) and "page_idx" in block
            }
            if page_indices:
                print(
                    f"[validate] {p.name}: {count} blocks, {len(page_indices)} pages "
                    f"(range {min(page_indices)}-{max(page_indices)})"
                )
            else:
                print(f"[validate] {p.name}: {count} blocks")
        else:
            print(f"[validate] {p.name}: not a list, type={type(data).__name__}")


def main():
    token = _load_token()
    if not token:
        print(
            "ERROR: No MINERU_API_TOKEN / MINERU_API_KEY. "
            "Set in environment or INAGENT/.env"
        )
        sys.exit(1)

    pdfs = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: No PDF files found in {INPUT_DIR}")
        sys.exit(1)

    print(f"Found {len(pdfs)} PDF(s):")
    for p in pdfs:
        print(f"  {p.name} ({p.stat().st_size / 1024:.0f} KB)")

    extracted = mineru_cloud_parse_pdfs(
        pdfs,
        output_root=MINERU_OUTPUT_DIR,
        token=token,
    )

    if extracted:
        validate_results(extracted)
        print(f"\nDone! {len(extracted)} file(s) ready in {MINERU_OUTPUT_DIR}")
        print("Next: run auto_convert to process into knowledge base")
    else:
        print("WARNING: No content_list.json files extracted")


if __name__ == "__main__":
    main()
