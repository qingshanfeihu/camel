# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""质检独立流程：对知识库与采购导出物执行只读/准只读质量校验。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
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

    if decisions_path and accepted_path and decisions_path.exists() and accepted_path.exists():
        export_check = KnowledgeQualityInspectorAgent.validate_procurement_export(
            decisions_path,
            accepted_path,
        )
        result["procurement_export"] = {
            "ok": export_check.ok,
            "expected_count": export_check.expected_count,
            "on_disk_count": export_check.on_disk_count,
            "detail": export_check.mismatch_detail,
        }
        if summary_path and summary_path.exists():
            summary_check = KnowledgeQualityInspectorAgent.validate_summary_accept_count(
                decisions_path,
                summary_path,
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
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[quality] report written -> %s", report_path)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=None)
    parser.add_argument("--accepted", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args()

    _setup_logging()
    run_quality_pipeline(
        decisions_path=args.decisions,
        accepted_path=args.accepted,
        summary_path=args.summary,
    )


if __name__ == "__main__":
    asyncio.run(main())
