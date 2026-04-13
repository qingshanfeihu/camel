# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Quality ingest: validate knowledge_base.json and optional exports."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Optional

from INAGENT.agents.knowledge_quality_inspector_agent import (
    KnowledgeQualityInspectorAgent,
)
from INAGENT.data_tools.ingest_validator import IngestValidator
from INAGENT.rag.cli_graph_store import get_cli_graph_store
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("quality_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_quality_pipeline(
    reference_dir: Path | None = None,
    decisions_path: Optional[Path] = None,
    accepted_path: Optional[Path] = None,
    summary_path: Optional[Path] = None,
    archive_to: Optional[Path] = None,
) -> Dict[str, object]:
    load_inagent_env()
    reference_dir = reference_dir or _REFERENCE_DIR
    kb_path = reference_dir / "knowledge_base.json"
    result: Dict[str, object] = {}

    if kb_path.exists():
        raw_chunks = json.loads(kb_path.read_text(encoding="utf-8"))
        cli_graph = get_cli_graph_store()
        validator = IngestValidator(cli_graph_store=cli_graph)
        validated = validator.validate_batch(raw_chunks)
        kb_path.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validator.save_report(reference_dir)
        result["ingest_report"] = validator.report.to_dict()
        result["knowledge_base_path"] = str(kb_path)
        logger.info(
            "[quality] knowledge_base validated: accepted=%d rejected=%d",
            validator.report.accepted,
            validator.report.rejected_short
            + validator.report.duplicates
            + validator.report.quarantined
            + validator.report.rejected_excluded,
        )
    else:
        result["ingest_report"] = None

    has_exports = (
        decisions_path
        and accepted_path
        and decisions_path.exists()
        and accepted_path.exists()
    )
    if has_exports:
        export_check = (
            KnowledgeQualityInspectorAgent.validate_procurement_export(
                decisions_path,
                accepted_path,
            )
        )
        result["procurement_export"] = {
            "ok": export_check.ok,
            "expected_count": export_check.expected_count,
            "on_disk_count": export_check.on_disk_count,
            "detail": export_check.mismatch_detail,
        }
        if summary_path and summary_path.exists():
            summary_check = (
                KnowledgeQualityInspectorAgent.validate_summary_accept_count(
                    decisions_path,
                    summary_path,
                )
            )
            result["summary_accept_count"] = {
                "ok": summary_check.ok,
                "summary_accept": summary_check.summary_accept,
                "decisions_accept": summary_check.decisions_accept,
                "detail": summary_check.detail,
            }
    else:
        result["procurement_export"] = None

    report_path = reference_dir / "_quality_report.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[quality] report written -> %s", report_path)

    if archive_to is not None:
        archive_to.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            shutil.copy2(report_path, archive_to / "_quality_report.json")
        ir = reference_dir / "_ingest_report.json"
        if ir.exists():
            shutil.copy2(ir, archive_to / "_ingest_report.json")
        summ = {
            "reference_dir": str(reference_dir),
            "archive_to": str(archive_to),
            "ingest_report": result.get("ingest_report"),
            "procurement_export": result.get("procurement_export"),
        }
        (archive_to / "summary.json").write_text(
            json.dumps(summ, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[quality] archived -> %s", archive_to)

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=None)
    parser.add_argument("--accepted", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument(
        "--archive-to",
        type=Path,
        default=None,
        help="Archive dir for copied quality and ingest JSON reports",
    )
    args = parser.parse_args()

    _setup_logging()
    run_quality_pipeline(
        decisions_path=args.decisions,
        accepted_path=args.accepted,
        summary_path=args.summary,
        archive_to=args.archive_to,
    )


if __name__ == "__main__":
    asyncio.run(main())
