# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""按正式采购入口执行的测试包装器。

本脚本不再实现任何自建 PDF/Office/TXT 解析分支，不做额外筛查判断。
它仅调用权威入口 ``INAGENT.data_tools.procurement_ingest.main``，
用于验证采购员正式流程（MinerU/Office/TXT -> reference/merge）是否正常执行。

说明：
- ``--input-dir`` 仅用于一致性校验，正式流程固定使用 ``INAGENT/knowledge_base/input``。
- ``--run-id`` 仅用于归档本次运行的元信息与日志副本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from INAGENT.data_tools.procurement_ingest import main as procurement_ingest_main  # noqa: E402
from INAGENT.utils.env_utils import load_inagent_env  # noqa: E402

logger = logging.getLogger("procurement_test_harness")

TEST_DATA_ROOT = REPO_ROOT / "INAGENT" / "test_data"
DEFAULT_INPUT_DIR = REPO_ROOT / "INAGENT" / "knowledge_base" / "input"
DEFAULT_OUTPUT_ROOT = TEST_DATA_ROOT / "采购输出"
AUTO_CONVERT_LOG = REPO_ROOT / "INAGENT" / "knowledge_base" / "logs" / "auto_convert.log"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _clear_procurement_data(repo_root: Path) -> None:
    """Clear all cached procurement data so the pipeline runs from scratch.

    Clears:
    - knowledge_base/reference/*.json  (except reserved files)
    - knowledge_base/doc_local_reference/  (entire directory)
    - knowledge_base/logs/*.cache.json
    - knowledge_base/mineru_output/  (all task subdirectories)
    """
    kb_root = repo_root / "INAGENT" / "knowledge_base"
    reserved_files = {"knowledge_base.json", "commandtree_base.json"}

    # 1. reference/*.json
    reference_dir = kb_root / "reference"
    if reference_dir.exists():
        removed = 0
        for f in reference_dir.glob("*.json"):
            if f.name not in reserved_files:
                f.unlink()
                removed += 1
        logger.info("[clear] removed %d reference JSON files", removed)

    # 2. doc_local_reference/
    doc_ref_dir = kb_root / "doc_local_reference"
    if doc_ref_dir.exists():
        shutil.rmtree(doc_ref_dir)
        logger.info("[clear] removed doc_local_reference/")

    # 3. logs/*.cache.json
    logs_dir = kb_root / "logs"
    if logs_dir.exists():
        removed = sum(
            1 for f in logs_dir.glob("*.cache.json")
            if not f.unlink() or True  # unlink returns None, always True
        )
        logger.info("[clear] removed %d cache files from logs/", removed)

    # 4. mineru_output task subdirectories (keep the directory itself)
    mineru_output_dir = kb_root / "mineru_output"
    if mineru_output_dir.exists():
        removed = 0
        for item in mineru_output_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                removed += 1
            elif item.is_file() and item.name != "mineru.json":
                item.unlink()
                removed += 1
        logger.info("[clear] removed %d items from mineru_output/", removed)

    logger.info("[clear] procurement cache cleared")


def _validate_input_dir(input_dir: Path) -> None:
    expected = DEFAULT_INPUT_DIR.resolve()
    actual = input_dir.resolve()
    if not actual.exists():
        raise FileNotFoundError(f"输入目录不存在: {actual}")
    if actual != expected:
        raise ValueError(
            "正式采购流程入口固定扫描 INAGENT/knowledge_base/input；"
            f"当前传入: {actual}，期望: {expected}"
        )


async def _run(args: argparse.Namespace) -> int:
    load_inagent_env()
    _validate_input_dir(args.input_dir)

    run_id = args.run_id or _build_run_id()
    run_dir = args.output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now().isoformat()
    status = "ok"
    err_text = ""
    err_tb = ""

    logger.info("按正式采购入口执行: procurement_ingest.main")
    logger.info("输入目录(校验): %s", args.input_dir.resolve())
    logger.info("run_id: %s", run_id)

    logger.info("[clear] 清空旧采购数据和cache，确保与事实源文件一致再处理...")
    _clear_procurement_data(REPO_ROOT)

    # 采购测试不需要 LLM — 直接跳过 gateway preflight 和 metadata LLM 提取，
    # 避免产生无意义的 HTTP 重试延迟（AUTO_CONVERT_SKIP_LLM=1）。
    os.environ["AUTO_CONVERT_SKIP_LLM"] = "1"
    os.environ["AUTO_CONVERT_ASSUME_YES"] = "1"
    logger.info("[test] AUTO_CONVERT_SKIP_LLM=1：跳过 LLM gateway preflight 及 metadata 提取")

    try:
        await procurement_ingest_main()
    except Exception as exc:
        status = "failed"
        err_text = f"{type(exc).__name__}: {exc}"
        err_tb = traceback.format_exc()
        logger.exception("正式采购流程执行失败")

    copied_log = None
    if AUTO_CONVERT_LOG.exists():
        copied_log = run_dir / "auto_convert.log"
        shutil.copy2(AUTO_CONVERT_LOG, copied_log)

    # Copy reference output files to run_dir/reference/
    ref_src = REPO_ROOT / "INAGENT" / "knowledge_base" / "reference"
    if ref_src.exists():
        ref_dst = run_dir / "reference"
        ref_dst.mkdir(parents=True, exist_ok=True)
        copied_refs = 0
        for json_file in ref_src.glob("*.json"):
            shutil.copy2(json_file, ref_dst / json_file.name)
            copied_refs += 1
        logger.info("[output] copied %d reference JSON files -> %s", copied_refs, ref_dst)

    # Copy doc_local_reference snapshot if present
    doc_ref_src = REPO_ROOT / "INAGENT" / "knowledge_base" / "doc_local_reference"
    if doc_ref_src.exists():
        doc_ref_dst = run_dir / "doc_local_reference"
        if doc_ref_dst.exists():
            shutil.rmtree(doc_ref_dst)
        shutil.copytree(doc_ref_src, doc_ref_dst)
        logger.info("[output] copied doc_local_reference/ -> %s", doc_ref_dst)

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "mode": "official_procurement_pipeline",
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "input_dir": str(args.input_dir.resolve()),
        "official_entry": "INAGENT.data_tools.procurement_ingest.main",
        "auto_convert_log": str(copied_log) if copied_log else None,
        "error": err_text or None,
    }
    _write_json(run_dir / "manifest.json", manifest)

    if err_text:
        _write_json(
            run_dir / "error.json",
            {
                "error": err_text,
                "traceback": err_tb,
            },
        )

    logger.info("[done] run_id=%s status=%s", run_id, status)
    return 0 if status == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", type=str, default="")
    args = parser.parse_args()

    _setup_logging()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
