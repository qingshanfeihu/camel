"""MinerU Cloud API (v4): batch URL upload + poll + zip extract.

Writes the same layout as local MinerU CLI:
  ``output_root/{data_id}/hybrid_auto/{data_id}_content_list.json``

Used by auto_convert (cloud-first, fallback local) and ``mineru_cloud_parse`` script.
"""

from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from pathlib import Path
from typing import List, Optional

import requests

DEFAULT_API_BASE = "https://mineru.net/api/v4"


def _headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def batch_upload_and_put_files(
    *,
    token: str,
    pdf_paths: List[Path],
    api_base: str = DEFAULT_API_BASE,
    model_version: str = "vlm",
    language: str = "ch",
    enable_table: bool = True,
    enable_formula: bool = True,
    is_ocr: bool = False,
    no_cache: bool = False,
    page_ranges: Optional[str] = None,
    post_timeout: float = 60.0,
    put_timeout: float = 300.0,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Request upload URLs, PUT files, return ``batch_id``."""
    log = logger or logging.getLogger(__name__)
    url = f"{api_base.rstrip('/')}/file-urls/batch"
    files_payload = []
    for p in pdf_paths:
        fentry: dict = {"name": p.name, "data_id": p.stem}
        if is_ocr:
            fentry["is_ocr"] = True
        if page_ranges:
            fentry["page_ranges"] = page_ranges
        files_payload.append(fentry)
    payload: dict = {
        "files": files_payload,
        "model_version": model_version,
        "language": language,
        "enable_table": enable_table,
        "enable_formula": enable_formula,
    }
    if no_cache:
        payload["no_cache"] = True
    log.info("[mineru-cloud] requesting upload URLs for %d file(s)...", len(pdf_paths))
    resp = requests.post(url, headers=_headers(token), json=payload, timeout=post_timeout)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(result.get("msg") or "file-urls/batch API error")

    batch_id = result["data"]["batch_id"]
    file_urls = result["data"]["file_urls"]
    log.info("[mineru-cloud] batch_id=%s, %d upload URL(s)", batch_id, len(file_urls))

    for i, upload_url in enumerate(file_urls):
        pdf_path = pdf_paths[i]
        log.info(
            "[mineru-cloud] uploading %s (%.0f KB)...",
            pdf_path.name,
            pdf_path.stat().st_size / 1024,
        )
        with open(pdf_path, "rb") as f:
            put_resp = requests.put(upload_url, data=f, timeout=put_timeout)
        if put_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Upload failed for {pdf_path.name}: HTTP {put_resp.status_code} "
                f"{put_resp.text[:200]!r}"
            )
    return batch_id


def poll_batch_until_done(
    *,
    token: str,
    batch_id: str,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 3600.0,
    interval: float = 10.0,
    logger: Optional[logging.Logger] = None,
) -> list:
    """Poll ``/extract-results/batch/{batch_id}`` until all done or failed."""
    log = logger or logging.getLogger(__name__)
    url = f"{api_base.rstrip('/')}/extract-results/batch/{batch_id}"
    start = time.time()
    log.info("[mineru-cloud] polling batch %s (timeout=%ss)...", batch_id, int(timeout))

    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            raise TimeoutError(f"MinerU cloud polling timed out after {timeout}s")

        resp = requests.get(url, headers=_headers(token), timeout=60.0)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(result.get("msg") or "extract-results API error")

        items = result["data"]["extract_result"]
        states = [it["state"] for it in items]
        all_done = all(s == "done" for s in states)
        any_failed = any(s == "failed" for s in states)

        if log.isEnabledFor(logging.DEBUG):
            log.debug("[mineru-cloud] [%ds] states=%s", int(elapsed), states)

        if all_done:
            log.info("[mineru-cloud] batch completed in %.0fs", elapsed)
            return items

        if any_failed:
            for it in items:
                if it["state"] == "failed":
                    log.warning(
                        "[mineru-cloud] task failed: %s — %s",
                        it.get("file_name"),
                        it.get("err_msg"),
                    )
            done_items = [it for it in items if it["state"] == "done"]
            if done_items:
                return done_items
            raise RuntimeError(
                "MinerU cloud: all tasks failed: "
                + ", ".join(it.get("file_name", "?") for it in items)
            )

        time.sleep(interval)


def download_and_extract_zip_items(
    items: list,
    output_root: Path,
    *,
    get_timeout: float = 300.0,
    logger: Optional[logging.Logger] = None,
) -> List[Path]:
    """Download each result zip into ``output_root/{data_id}/hybrid_auto/``."""
    log = logger or logging.getLogger(__name__)
    extracted: List[Path] = []
    for item in items:
        zip_url = item.get("full_zip_url")
        file_name = item.get("file_name", "unknown")
        data_id = item.get("data_id", Path(file_name).stem)
        if not zip_url:
            log.warning("[mineru-cloud] no zip URL for %s, skipping", file_name)
            continue

        log.info("[mineru-cloud] downloading result for %s...", file_name)
        resp = requests.get(zip_url, timeout=get_timeout)
        resp.raise_for_status()

        task_dir = output_root / str(data_id) / "hybrid_auto"
        task_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            content_list_found = False
            for name in zf.namelist():
                if name.endswith("_content_list.json") or name.endswith("content_list.json"):
                    target = task_dir / f"{data_id}_content_list.json"
                    with zf.open(name) as src:
                        target.write_bytes(src.read())
                    log.info(
                        "[mineru-cloud] wrote %s (%.0f KB)",
                        target.name,
                        target.stat().st_size / 1024,
                    )
                    extracted.append(target)
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
                log.warning(
                    "[mineru-cloud] no content_list.json in zip for %s; entries=%s",
                    file_name,
                    zf.namelist()[:15],
                )

    return extracted


def mineru_cloud_parse_files(
    file_paths: List[Path],
    *,
    output_root: Path,
    token: str,
    api_base: str = DEFAULT_API_BASE,
    model_version: str = "vlm",
    language: str = "ch",
    enable_table: bool = True,
    enable_formula: bool = True,
    is_ocr: bool = False,
    no_cache: bool = False,
    page_ranges: Optional[str] = None,
    poll_timeout: float = 3600.0,
    logger: Optional[logging.Logger] = None,
) -> List[Path]:
    """Full cloud pipeline for one or more files (pdf/doc/docx/ppt/pptx/images).

    Returns paths to ``*_content_list.json``.
    """
    log = logger or logging.getLogger(__name__)
    if not file_paths:
        return []
    batch_id = batch_upload_and_put_files(
        token=token,
        pdf_paths=file_paths,  # parameter name is historical; API accepts all formats
        api_base=api_base,
        model_version=model_version,
        language=language,
        enable_table=enable_table,
        enable_formula=enable_formula,
        is_ocr=is_ocr,
        no_cache=no_cache,
        page_ranges=page_ranges,
        logger=log,
    )
    items = poll_batch_until_done(
        token=token,
        batch_id=batch_id,
        api_base=api_base,
        timeout=poll_timeout,
        logger=log,
    )
    return download_and_extract_zip_items(items, output_root, logger=log)


# Legacy alias
mineru_cloud_parse_pdfs = mineru_cloud_parse_files


def mineru_cloud_parse_one_file(
    src: Path,
    *,
    output_root: Path,
    token: str,
    api_base: str = DEFAULT_API_BASE,
    model_version: str = "vlm",
    language: str = "ch",
    enable_table: bool = True,
    enable_formula: bool = True,
    is_ocr: bool = False,
    no_cache: bool = False,
    page_ranges: Optional[str] = None,
    poll_timeout: float = 3600.0,
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """Parse a single file (pdf/doc/docx/ppt/pptx) via cloud API.

    Returns content_list path or ``None`` on failure.
    """
    log = logger or logging.getLogger(__name__)
    try:
        paths = mineru_cloud_parse_files(
            [src],
            output_root=output_root,
            token=token,
            api_base=api_base,
            model_version=model_version,
            language=language,
            enable_table=enable_table,
            enable_formula=enable_formula,
            is_ocr=is_ocr,
            no_cache=no_cache,
            page_ranges=page_ranges,
            poll_timeout=poll_timeout,
            logger=log,
        )
    except Exception as exc:
        log.warning("[mineru-cloud] failed for %s: %s", src.name, exc)
        return None
    if not paths:
        log.warning("[mineru-cloud] no content_list extracted for %s", src.name)
        return None
    return paths[0]


# Legacy alias
mineru_cloud_parse_one_pdf = mineru_cloud_parse_one_file


def validate_content_list(path: Path) -> bool:
    """Return True if file exists and is a non-empty JSON list."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, list) and len(data) > 0
