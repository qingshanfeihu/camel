# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农场主独立流程：消费 schema gaps，执行树结构裁决与图更新。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Dict, List

from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
from INAGENT.rag.graphrag_integration import GraphRAGRetriever
from INAGENT.rag.knowledge_schema import SchemaGapEntry
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger("farm_owner_ingest")
_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
_GRAPH_DIR = _INAGENT_ROOT / "graphrag_index"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_gap_entries(gaps_file: Path) -> List[SchemaGapEntry]:
    entries: List[SchemaGapEntry] = []
    field_names = {field.name for field in dc_fields(SchemaGapEntry)}
    for line in gaps_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            filtered = {key: value for key, value in raw.items() if key in field_names}
            entries.append(SchemaGapEntry(**filtered))
        except Exception as exc:
            logger.warning("[farm-owner] 跳过非法 gap 条目: %s", exc)
    return entries


def run_farm_owner_pipeline(reference_dir: Path | None = None) -> Dict[str, object]:
    load_inagent_env()
    reference_dir = reference_dir or _REFERENCE_DIR
    gaps_file = reference_dir / "schema_gaps.jsonl"
    if not gaps_file.exists() or gaps_file.stat().st_size == 0:
        return {"processed": False, "reason": "schema_gaps.jsonl missing or empty"}

    graphrag = GraphRAGRetriever(workspace_dir=_GRAPH_DIR)
    if not graphrag.is_available():
        return {"processed": False, "reason": "GraphRAG unavailable"}

    entries = _load_gap_entries(gaps_file)
    if not entries:
        return {"processed": False, "reason": "no valid gap entries"}

    owner = KnowledgeFarmOwnerAgent(graphrag)
    report = owner.process_gap_entries(entries)
    processed_file = gaps_file.with_suffix(".jsonl.processed")
    try:
        os.replace(gaps_file, processed_file)
    except OSError as exc:
        logger.warning("[farm-owner] 标记 processed 失败: %s", exc)

    result = {
        "processed": True,
        "gap_entries": len(entries),
        "entities_added": report.entities_added,
        "discarded_count": report.discarded_count,
        "errors": list(report.errors),
        "snapshot_dir": str(report.snapshot_dir) if report.snapshot_dir else None,
        "processed_file": str(processed_file),
    }
    logger.info("[farm-owner] done: %s", json.dumps(result, ensure_ascii=False))
    return result


async def main() -> None:
    _setup_logging()
    run_farm_owner_pipeline()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    asyncio.run(main())
