# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农民独立流程：读取采购落盘结果，做结构挂载并产出 schema gaps。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, List

from INAGENT.agents.knowledge_farmer_agent import FarmResult, KnowledgeFarmerAgent
from INAGENT.agents.knowledge_procurement_agent import ChunkDecision, ProcurementDecision
from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("farmer_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
_LOG_DIR = _INAGENT_ROOT / "knowledge_base" / "logs"

_EXCLUDED_REFERENCE_FILES = {
    "knowledge_base.json",
    "commandtree_base.json",
    "farmer_tree_alias.json",
    "scenarios_scaffold.json",
    "scenarios_synthesized.json",
}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _iter_procurement_files(reference_dir: Path) -> List[Path]:
    files: List[Path] = []
    for path in sorted(reference_dir.glob("*.json")):
        if path.name in _EXCLUDED_REFERENCE_FILES:
            continue
        files.append(path)
    return files


def _infer_target_kb(meta: Dict[str, object]) -> str:
    category = str(meta.get("document_category") or "").lower()
    if category.startswith("test/"):
        return "test"
    return "product"


def _build_accept_decisions(chunks: List[Dict], source_file: str) -> List[ChunkDecision]:
    decisions: List[ChunkDecision] = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            chunk["metadata"] = meta
        decisions.append(
            ChunkDecision(
                chunk=chunk,
                decision=ProcurementDecision(
                    action="accept",
                    target_kb=_infer_target_kb(meta),
                    confidence=1.0,
                    reason="farmer_ingest accepted procurement chunk",
                ),
                source_file=source_file,
                chunk_index=index,
            )
        )
    return decisions


def _group_results_by_source(results: List[FarmResult]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for result in results:
        grouped.setdefault(result.source_file, []).append(result.chunk)
    return grouped


def run_farmer_document_pipeline(reference_dir: Path | None = None) -> Dict[str, object]:
    load_inagent_env()
    reference_dir = reference_dir or _REFERENCE_DIR
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    farmer = KnowledgeFarmerAgent(model=None)
    source_files = _iter_procurement_files(reference_dir)
    summary: Dict[str, object] = {
        "files": [],
        "files_processed": 0,
        "chunks_in": 0,
        "chunks_out": 0,
        "schema_gaps": 0,
    }

    all_results: List[FarmResult] = []

    for source_path in source_files:
        try:
            chunks = json.loads(source_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[farmer] 读取失败 %s: %s", source_path.name, exc)
            continue
        if not isinstance(chunks, list) or not chunks:
            continue

        decisions = _build_accept_decisions(chunks, source_path.name)
        results = farmer.cultivate_batch(decisions)
        grouped = _group_results_by_source(results)
        updated_chunks = grouped.get(source_path.name, chunks)
        source_path.write_text(
            json.dumps(updated_chunks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        all_results.extend(results)
        file_gaps = sum(len(result.schema_gaps) for result in results)
        summary["files"].append(
            {
                "source_file": source_path.name,
                "chunks_in": len(chunks),
                "chunks_out": len(updated_chunks),
                "schema_gaps": file_gaps,
            }
        )
        summary["files_processed"] = int(summary["files_processed"]) + 1
        summary["chunks_in"] = int(summary["chunks_in"]) + len(chunks)
        summary["chunks_out"] = int(summary["chunks_out"]) + len(updated_chunks)
        summary["schema_gaps"] = int(summary["schema_gaps"]) + file_gaps
        logger.info(
            "[farmer] %s: %d -> %d chunks, schema_gaps=%d",
            source_path.name,
            len(chunks),
            len(updated_chunks),
            file_gaps,
        )

    gaps_file = reference_dir / "schema_gaps.jsonl"
    gap_count = farmer.emit_schema_gaps(all_results, gaps_file)
    summary["schema_gaps_file"] = str(gaps_file)
    summary["schema_gaps_written"] = gap_count

    merge_knowledge_base(
        reference_dir,
        reference_dir / "knowledge_base.json",
        deduplicate=True,
        logger=logger,
        validate_ingest=False,
    )
    logger.info("[farmer] merged knowledge_base.json after farmer stage")
    return summary


async def main() -> None:
    _setup_logging()
    result = run_farmer_document_pipeline()
    logger.info("[done] farmer stage finished: %s", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    import asyncio

    asyncio.run(main())
