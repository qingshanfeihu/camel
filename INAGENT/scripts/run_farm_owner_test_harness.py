# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Run farm-owner pipeline against a quality run and archive split outputs."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_INAGENT_ROOT.parent))

from INAGENT.data_tools.farm_owner_ingest import run_farm_owner_pipeline
from INAGENT.utils.env_utils import load_inagent_env

_QUALITY_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "质检员输出"
_FARM_OWNER_OUTPUT_ROOT = _INAGENT_ROOT / "test_data" / "农场主输出"
_TEMP_REFERENCE_DIR = _INAGENT_ROOT / ".tmp_farm_owner_harness"
logger = logging.getLogger("farm_owner_test_harness")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_latest_successful_quality_run(root: Path) -> Path:
    candidates = sorted([path for path in root.iterdir() if path.is_dir()])
    for candidate in reversed(candidates):
        manifest = candidate / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = _load_json(manifest)
        except Exception:
            continue
        if data.get("status") == "ok":
            return candidate
    raise FileNotFoundError("No successful quality run found")


def _clear_output_root(output_root: Path) -> None:
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)


def _prepare_reference_dir(quality_run_dir: Path) -> Path:
    if _TEMP_REFERENCE_DIR.exists():
        shutil.rmtree(_TEMP_REFERENCE_DIR)
    _TEMP_REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    required = [
        "_quality_gate_for_owner.jsonl",
        "_quality_reference_for_owner.json",
    ]
    optional = [
        "schema_gaps.jsonl",
        "_quality_report.json",
        "_ingest_report.json",
        "manifest.json",
        "summary.json",
    ]

    for name in required + optional:
        src = quality_run_dir / name
        if src.exists():
            shutil.copy2(src, _TEMP_REFERENCE_DIR / name)

    return _TEMP_REFERENCE_DIR


def _collect_outputs(run_root: Path, reference_dir: Path, result: dict) -> None:
    farmer_dir = run_root / "给农民"
    inspector_dir = run_root / "给质检员"
    farmer_dir.mkdir(parents=True, exist_ok=True)
    inspector_dir.mkdir(parents=True, exist_ok=True)

    farmer_files = [
        "_owner_decisions_for_farmer.jsonl",
    ]
    inspector_files = [
        "_quality_rule_proposals.json",
        "schema_gaps.jsonl",
        "_quality_gate_for_owner.jsonl",
    ]

    for name in farmer_files:
        src = reference_dir / name
        if src.exists():
            shutil.copy2(src, farmer_dir / name)

    for name in inspector_files:
        src = reference_dir / name
        if src.exists():
            shutil.copy2(src, inspector_dir / name)

    (run_root / "_farm_owner_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_harness(quality_run_dir: Path, output_root: Path) -> Path:
    load_inagent_env()
    logger.info("[farm-owner-harness] quality run: %s", quality_run_dir)
    _clear_output_root(output_root)

    reference_dir = _prepare_reference_dir(quality_run_dir)
    logger.info("[farm-owner-harness] prepared temp reference: %s", reference_dir)
    result = run_farm_owner_pipeline(reference_dir=reference_dir)
    logger.info("[farm-owner-harness] farm owner pipeline finished")

    run_id = f"farm_owner_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_root = output_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    _collect_outputs(run_root, reference_dir, result)

    manifest = {
        "status": "ok",
        "mode": "farm_owner_test_harness",
        "quality_run_dir": str(quality_run_dir),
        "run_root": str(run_root),
        "farmer_output_dir": str(run_root / "给农民"),
        "inspector_output_dir": str(run_root / "给质检员"),
        "farm_owner_result": result,
    }
    (run_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-run-id", help="Quality run directory name")
    args = parser.parse_args()
    _setup_logging()

    quality_run_dir = (
        _QUALITY_OUTPUT_ROOT / args.quality_run_id
        if args.quality_run_id
        else _find_latest_successful_quality_run(_QUALITY_OUTPUT_ROOT)
    )
    logger.info("[farm-owner-harness] selected quality run: %s", quality_run_dir)
    run_root = run_harness(quality_run_dir, _FARM_OWNER_OUTPUT_ROOT)
    logger.info("[farm-owner-harness] output root: %s", run_root)
    print(run_root)


if __name__ == "__main__":
    main()