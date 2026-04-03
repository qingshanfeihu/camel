#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟采购员 accept → 农民 cultivate_batch（ircookie 相关 MinerU 块）。

从 cli_content_list.json 用与 auto_convert 相同的 _extract_text_from_block 取正文，
筛 NFKC 后含子串 ircookie 的块，构造 ChunkDecision(action=accept)，跑 KnowledgeFarmerAgent。

默认不写 reference、不改 knowledge_base.json，只打印摘要并写 JSON 报告。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _INAGENT_ROOT.parent
sys.path.insert(0, str(_REPO_ROOT))

from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent  # noqa: E402
from INAGENT.agents.knowledge_procurement_agent import (  # noqa: E402
    ChunkDecision,
    ProcurementDecision,
    enrich_chunk_decision_for_farmer,
)
from INAGENT.data_tools.auto_convert import _extract_text_from_block  # noqa: E402
from INAGENT.utils.env_utils import load_inagent_env  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("farmer_ircookie_sim")

DEFAULT_MINERU = (
    _INAGENT_ROOT / "knowledge_base" / "mineru_output" / "cli" / "hybrid_auto" / "cli_content_list.json"
)
LOG_DIR = _INAGENT_ROOT / "knowledge_base" / "logs"


def _norm_lower(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").lower()


def build_accept_decisions(
    content_list_path: Path,
    *,
    source_file: str = "cli.pdf",
) -> list[ChunkDecision]:
    data = json.loads(content_list_path.read_text(encoding="utf-8"))
    decisions: list[ChunkDecision] = []
    seen_idx = 0
    for block_index, block in enumerate(data):
        if not isinstance(block, dict):
            continue
        text = _extract_text_from_block(block)
        if not text.strip():
            continue
        if "ircookie" not in _norm_lower(text):
            continue
        page_idx = block.get("page_idx")
        block_type = block.get("type", "")
        meta = {
            "source_file": source_file,
            "document_category": "cli/reference",
            "page_idx": page_idx,
            "mineru_block_index": block_index,
            "mineru_block_type": block_type,
        }
        chunk = {"page_content": text, "metadata": meta}
        decisions.append(
            ChunkDecision(
                chunk=chunk,
                decision=ProcurementDecision(
                    action="accept",
                    target_kb="product",
                    confidence=0.95,
                    reason="procurement_sim: ircookie-related MinerU block (auto accept)",
                ),
                source_file=source_file,
                chunk_index=seen_idx,
            )
        )
        seen_idx += 1
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mineru-json",
        type=Path,
        default=DEFAULT_MINERU,
        help="MinerU cli_content_list.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=LOG_DIR / "farmer_ircookie_procurement_simulation.json",
        help="Write structured report JSON",
    )
    parser.add_argument(
        "--emit-gaps",
        action="store_true",
        help="Also write schema gaps jsonl next to report (same stem)",
    )
    parser.add_argument(
        "--write-reference",
        action="store_true",
        help="Farmer write_to_reference (mutates reference/*.json)",
    )
    args = parser.parse_args()

    load_inagent_env()

    if not args.mineru_json.exists():
        logger.error("MinerU JSON not found: %s", args.mineru_json)
        return 1

    decisions = [
        enrich_chunk_decision_for_farmer(d)
        for d in build_accept_decisions(args.mineru_json)
    ]
    logger.info("采购员模拟: %d 个 accept（ircookie 相关块）", len(decisions))

    farmer = KnowledgeFarmerAgent(model=None)
    results = farmer.cultivate_batch(decisions)

    rows = []
    for r in results:
        m = r.chunk.get("metadata", {})
        gap_summary = [
            {
                "gap_type": g.gap_type,
                "field_name": g.field_name,
                "column_name": g.column_name,
                "entity_title": (g.entity_title or "")[:80],
            }
            for g in r.schema_gaps
        ]
        rows.append(
            {
                "block_id": m.get("block_id"),
                "mineru_block_index": m.get("mineru_block_index"),
                "page_idx": m.get("page_idx"),
                "mineru_block_type": m.get("mineru_block_type"),
                "matched_node_id": r.matched_node_id,
                "tree_node_id": m.get("tree_node_id"),
                "command_prefix": m.get("command_prefix"),
                "command_refs": m.get("command_refs"),
                "description_head": (m.get("description") or "")[:200],
                "enriched_fields": r.enriched_fields,
                "schema_gaps": gap_summary,
                "content_chars": len(r.chunk.get("page_content") or ""),
            }
        )
        print(
            f"  idx={m.get('mineru_block_index')} page={m.get('page_idx')} "
            f"type={m.get('mineru_block_type')} -> match={r.matched_node_id!r} "
            f"prefix={m.get('command_prefix')!r} gaps={len(r.schema_gaps)}"
        )

    report = {
        "generated_at": datetime.now().isoformat(),
        "mineru_json": str(args.mineru_json.resolve()),
        "accepted_chunks": len(decisions),
        "results": rows,
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告: %s", args.report)

    if args.emit_gaps:
        gaps_path = args.report.with_suffix(".gaps.jsonl")
        n = farmer.emit_schema_gaps(results, gaps_path)
        logger.info("schema gaps: %d 条 -> %s", n, gaps_path)

    if args.write_reference:
        counts = farmer.write_to_reference(results)
        logger.info("write_to_reference: %s", counts)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
