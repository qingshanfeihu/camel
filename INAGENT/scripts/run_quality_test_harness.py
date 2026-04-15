# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""按正式质检入口执行的测试包装器。

本脚本复用 ``INAGENT.data_tools.quality_ingest.run_quality_pipeline``，
用于验证质检员能否正确处理最新采购输出，并将结果归档到
``INAGENT/test_data/质检员输出/<run_id>``。

说明：
- 默认自动选择 ``INAGENT/test_data/采购输出`` 下最新且成功的批次作为输入。
- 会清空旧的 ``test_data/质检员输出`` 目录，确保回归结果干净可比。
- 采购批次的 ``reference`` 会复制到 ``<run_id>/ingest`` 作为质检输入，
  不直接污染 ``knowledge_base/reference``。
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from INAGENT.data_tools.quality_ingest import run_quality_pipeline  # noqa: E402
from INAGENT.utils.env_utils import load_inagent_env  # noqa: E402
from INAGENT.utils.quality_test_output import prepare_run_dir  # noqa: E402

logger = logging.getLogger("quality_test_harness")

TEST_DATA_ROOT = REPO_ROOT / "INAGENT" / "test_data"
PROCUREMENT_OUTPUT_ROOT = TEST_DATA_ROOT / "采购输出"
QUALITY_OUTPUT_ROOT = TEST_DATA_ROOT / "质检员输出"


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
    return datetime.now().strftime("quality_%Y%m%d_%H%M%S")


def _clear_quality_output(output_root: Path) -> None:
    if output_root.exists():
        shutil.rmtree(output_root)
        logger.info("[clear] removed previous quality output: %s", output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    logger.info("[clear] quality output root ready: %s", output_root)


def _load_manifest(run_dir: Path) -> Dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_latest_successful_procurement_run(procurement_root: Path) -> Path:
    if not procurement_root.exists():
        raise FileNotFoundError(f"采购输出目录不存在: {procurement_root}")

    candidates = sorted(
        [p for p in procurement_root.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    for run_dir in candidates:
        manifest = _load_manifest(run_dir)
        if manifest.get("status") == "ok" and (run_dir / "reference").exists():
            return run_dir

    raise FileNotFoundError(
        f"未找到成功的采购批次（需要 manifest.status=ok 且存在 reference/）: {procurement_root}"
    )


def _copy_procurement_contract(procurement_run_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "auto_convert.log", "error.json"):
        src = procurement_run_dir / name
        if src.exists():
            shutil.copy2(src, target_dir / name)


def _validate_procurement_reference(procurement_run_dir: Path) -> Path:
    reference_dir = procurement_run_dir / "reference"
    kb_path = reference_dir / "knowledge_base.json"
    if not reference_dir.exists():
        raise FileNotFoundError(f"采购批次缺少 reference 目录: {reference_dir}")
    if not kb_path.exists():
        raise FileNotFoundError(f"采购批次缺少 knowledge_base.json: {kb_path}")
    return reference_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--procurement-run-id",
        type=str,
        default="",
        help="指定采购批次目录名；为空时自动选择最新成功批次",
    )
    parser.add_argument(
        "--procurement-output-root",
        type=Path,
        default=PROCUREMENT_OUTPUT_ROOT,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=QUALITY_OUTPUT_ROOT,
    )
    parser.add_argument("--run-id", type=str, default="")
    args = parser.parse_args()

    _setup_logging()
    load_inagent_env()

    run_id = args.run_id or _build_run_id()
    started_at = datetime.now().isoformat()
    status = "ok"
    err_text = ""
    err_tb = ""

    logger.info("按正式质检入口执行: quality_ingest.run_quality_pipeline")
    _clear_quality_output(args.output_root.resolve())

    procurement_root = args.procurement_output_root.resolve()
    if args.procurement_run_id:
        procurement_run_dir = procurement_root / args.procurement_run_id
    else:
        procurement_run_dir = _find_latest_successful_procurement_run(procurement_root)

    procurement_manifest = _load_manifest(procurement_run_dir)
    reference_dir = _validate_procurement_reference(procurement_run_dir)
    logger.info("[input] procurement_run=%s", procurement_run_dir)

    run_root = prepare_run_dir(
        run_id,
        reference_dir=reference_dir,
        out_base=args.output_root.resolve(),
        copy_reference=True,
    )
    ingest_dir = run_root / "ingest"
    contract_dir = run_root / "procurement_contract"
    _copy_procurement_contract(procurement_run_dir, contract_dir)

    quality_result: Optional[Dict[str, object]] = None
    try:
        quality_result = run_quality_pipeline(
            reference_dir=ingest_dir,
            archive_to=run_root,
        )
    except Exception as exc:
        status = "failed"
        err_text = f"{type(exc).__name__}: {exc}"
        err_tb = traceback.format_exc()
        logger.exception("正式质检流程执行失败")

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "mode": "official_quality_pipeline",
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "official_entry": "INAGENT.data_tools.quality_ingest.run_quality_pipeline",
        "procurement_run_id": procurement_run_dir.name,
        "procurement_manifest": procurement_manifest,
        "input_reference_dir": str(reference_dir),
        "ingest_dir": str(ingest_dir),
        "archive_dir": str(run_root),
        "error": err_text or None,
    }
    _write_json(run_root / "manifest.json", manifest)

    if quality_result is not None:
        _write_json(run_root / "_quality_harness_result.json", quality_result)

    if err_text:
        _write_json(
            run_root / "error.json",
            {"error": err_text, "traceback": err_tb},
        )

    logger.info("[done] run_id=%s status=%s", run_id, status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())