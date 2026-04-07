"""Upload PDFs to MinerU Cloud API (v4) and download parsed results.

Usage:
    python -m INAGENT.scripts.mineru_cloud_parse

Flow:
    1. Batch upload local PDFs via /api/v4/file-urls/batch (vlm model)
    2. PUT files to returned OSS URLs
    3. Poll /api/v4/extract-results/batch/{batch_id} until done
    4. Download zip, extract *_content_list.json into
       mineru_output/{stem}/hybrid_auto/  (compatible with auto_convert)
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import List, Optional

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "knowledge_base"
INPUT_DIR = KB_DIR / "input"
MINERU_OUTPUT_DIR = KB_DIR / "mineru_output"

API_BASE = "https://mineru.net/api/v4"
TOKEN = os.environ.get("MINERU_API_TOKEN", "").strip()


def _load_token() -> str:
    if TOKEN:
        return TOKEN
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("MINERU_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def batch_upload(token: str, pdf_paths: List[Path], model_version: str = "vlm") -> str:
    url = f"{API_BASE}/file-urls/batch"
    files_payload = []
    for p in pdf_paths:
        files_payload.append({
            "name": p.name,
            "data_id": p.stem,
        })
    data = {
        "files": files_payload,
        "model_version": model_version,
        "language": "ch",
        "enable_table": True,
        "enable_formula": False,
    }
    print(f"[upload] Requesting upload URLs for {len(pdf_paths)} file(s)...")
    resp = requests.post(url, headers=_headers(token), json=data, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"API error: {result.get('msg')}")

    batch_id = result["data"]["batch_id"]
    file_urls = result["data"]["file_urls"]
    print(f"[upload] batch_id={batch_id}, got {len(file_urls)} upload URL(s)")

    for i, upload_url in enumerate(file_urls):
        pdf_path = pdf_paths[i]
        print(f"[upload] Uploading {pdf_path.name} ({pdf_path.stat().st_size / 1024:.0f} KB)...")
        with open(pdf_path, "rb") as f:
            put_resp = requests.put(upload_url, data=f, timeout=120)
        if put_resp.status_code in (200, 201):
            print(f"[upload] {pdf_path.name} uploaded OK")
        else:
            raise RuntimeError(
                f"Upload failed for {pdf_path.name}: HTTP {put_resp.status_code} {put_resp.text[:200]}"
            )

    return batch_id


def poll_batch(token: str, batch_id: str, timeout: int = 600, interval: int = 10) -> list:
    url = f"{API_BASE}/extract-results/batch/{batch_id}"
    start = time.time()
    print(f"[poll] Waiting for batch {batch_id} (timeout={timeout}s)...")

    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout:
            raise TimeoutError(f"Polling timed out after {timeout}s")

        resp = requests.get(url, headers=_headers(token), timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"API error: {result.get('msg')}")

        items = result["data"]["extract_result"]
        states = [it["state"] for it in items]
        all_done = all(s == "done" for s in states)
        any_failed = any(s == "failed" for s in states)

        progress_parts = []
        for it in items:
            name = it.get("file_name", "?")
            state = it["state"]
            if state == "running":
                ep = it.get("extract_progress", {})
                progress_parts.append(
                    f"{name}: {ep.get('extracted_pages', '?')}/{ep.get('total_pages', '?')} pages"
                )
            else:
                progress_parts.append(f"{name}: {state}")
        print(f"[poll] [{elapsed}s] {' | '.join(progress_parts)}")

        if all_done:
            print("[poll] All tasks done!")
            return items

        if any_failed:
            for it in items:
                if it["state"] == "failed":
                    print(f"[poll] FAILED: {it.get('file_name')} - {it.get('err_msg')}")
            failed_names = [it["file_name"] for it in items if it["state"] == "failed"]
            done_items = [it for it in items if it["state"] == "done"]
            if done_items:
                print(f"[poll] Returning {len(done_items)} successful result(s), {len(failed_names)} failed")
                return done_items
            raise RuntimeError(f"All tasks failed: {failed_names}")

        time.sleep(interval)


def download_and_extract(items: list) -> List[Path]:
    extracted_paths = []
    for item in items:
        zip_url = item.get("full_zip_url")
        file_name = item.get("file_name", "unknown")
        data_id = item.get("data_id", Path(file_name).stem)
        if not zip_url:
            print(f"[download] No zip URL for {file_name}, skipping")
            continue

        print(f"[download] Downloading {file_name} result...")
        resp = requests.get(zip_url, timeout=120)
        resp.raise_for_status()

        task_dir = MINERU_OUTPUT_DIR / data_id / "hybrid_auto"
        task_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            content_list_found = False
            for name in zf.namelist():
                if name.endswith("_content_list.json") or name.endswith("content_list.json"):
                    target = task_dir / f"{data_id}_content_list.json"
                    with zf.open(name) as src:
                        target.write_bytes(src.read())
                    size_kb = target.stat().st_size / 1024
                    print(f"[extract] {target.name} ({size_kb:.0f} KB)")
                    extracted_paths.append(target)
                    content_list_found = True

                if name.endswith("_middle.json") or name.endswith("middle.json"):
                    target = task_dir / f"{data_id}_middle.json"
                    with zf.open(name) as src:
                        target.write_bytes(src.read())

                if name.endswith(".md"):
                    target = task_dir / f"{data_id}.md"
                    with zf.open(name) as src:
                        target.write_bytes(src.read())

                if "/images/" in name and not name.endswith("/"):
                    img_dir = task_dir / "images"
                    img_dir.mkdir(exist_ok=True)
                    img_name = Path(name).name
                    with zf.open(name) as src:
                        (img_dir / img_name).write_bytes(src.read())

            if not content_list_found:
                print(f"[extract] WARNING: No content_list.json found in zip for {file_name}")
                print(f"[extract] Zip contents: {zf.namelist()[:20]}")

    return extracted_paths


def validate_results(extracted_paths: List[Path]) -> None:
    for p in extracted_paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            count = len(data)
            page_indices = set()
            for block in data:
                if isinstance(block, dict) and "page_idx" in block:
                    page_indices.add(block["page_idx"])
            print(f"[validate] {p.name}: {count} blocks, {len(page_indices)} pages (range {min(page_indices)}-{max(page_indices)})")
        else:
            print(f"[validate] {p.name}: not a list, type={type(data).__name__}")


def main():
    token = _load_token()
    if not token:
        print("ERROR: No MINERU_API_TOKEN found. Set it in environment or INAGENT/.env")
        sys.exit(1)

    pdfs = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: No PDF files found in {INPUT_DIR}")
        sys.exit(1)

    print(f"Found {len(pdfs)} PDF(s):")
    for p in pdfs:
        print(f"  {p.name} ({p.stat().st_size / 1024:.0f} KB)")

    batch_id = batch_upload(token, pdfs)
    items = poll_batch(token, batch_id)
    extracted = download_and_extract(items)

    if extracted:
        validate_results(extracted)
        print(f"\nDone! {len(extracted)} file(s) ready in {MINERU_OUTPUT_DIR}")
        print("Next: run auto_convert to process into knowledge base")
    else:
        print("WARNING: No content_list.json files extracted")


if __name__ == "__main__":
    main()
