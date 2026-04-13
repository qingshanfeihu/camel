# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""统一结构化入库主链路（固定顺序）。

顺序：采购 -> 质检 -> 农场主 -> 农民
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline
from INAGENT.data_tools.farmer_ingest import run_farmer_document_pipeline
from INAGENT.data_tools.procurement_ingest import main as procurement_main
from INAGENT.data_tools.quality_ingest import run_quality_pipeline

logger = logging.getLogger("structured_ingest_pipeline")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _build_run_id() -> str:
    return datetime.now().strftime("structured_%Y%m%d_%H%M%S")


def _archive_outputs(archive_dir: Path) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "knowledge_base.json",
        "_ingest_report.json",
        "_quality_report.json",
        "_quality_gate_for_owner.jsonl",
        "_owner_decisions_for_farmer.jsonl",
        "schema_gaps.jsonl",
    ):
        src = _REFERENCE_DIR / name
        if src.exists():
            shutil.copy2(src, archive_dir / name)


async def run_structured_ingest_pipeline(archive_to: Path | None = None) -> dict:
    result = {}

    logger.info("[pipeline] step1 procurement start")
    await procurement_main()
    result["procurement"] = {"ok": True}
    logger.info("[pipeline] step1 procurement done")

    logger.info("[pipeline] step2 quality start")
    quality_result = run_quality_pipeline()
    result["quality"] = quality_result
    logger.info("[pipeline] step2 quality done")

    logger.info("[pipeline] step3 farm-owner start")
    owner_result = run_farm_owner_pipeline()
    result["farm_owner"] = owner_result
    logger.info("[pipeline] step3 farm-owner done")

    logger.info("[pipeline] step4 farmer start")
    farmer_result = run_farmer_document_pipeline()
    result["farmer"] = farmer_result
    logger.info("[pipeline] step4 farmer done")

    if archive_to is not None:
        _archive_outputs(archive_to)
        result["archive_to"] = str(archive_to)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Optional run id for archive dir under INAGENT/test_data/runs/",
    )
    parser.add_argument(
        "--archive-to",
        type=Path,
        default=None,
        help="Optional explicit archive directory.",
    )
    args = parser.parse_args()

    _setup_logging()
    run_id = args.run_id or _build_run_id()
    archive_to = args.archive_to
    if archive_to is None:
        archive_to = _INAGENT_ROOT / "test_data" / "runs" / run_id

    try:
        result = asyncio.run(run_structured_ingest_pipeline(archive_to=archive_to))
        (archive_to / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[pipeline] done: %s", json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        logger.exception("[pipeline] failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
