# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
质检员 Agent (Knowledge Quality Inspector Agent)

对 **采购导出物** 与 ``KnowledgeProcurementAgent`` 的 **API 语义** 做只读校验
(不调用 LLM、不写库): ``decisions.json`` 与 ``accepted_for_farmer.json`` 应对齐
``filter_accepted`` / ``enrich_chunk_decision_for_farmer`` 的结果。

典型用法::

    from pathlib import Path
    from INAGENT.agents.knowledge_quality_inspector_agent import (
        KnowledgeQualityInspectorAgent,
    )

    r = KnowledgeQualityInspectorAgent.validate_procurement_export(
        Path(".../decisions.json"),
        Path(".../accepted_for_farmer.json"),
    )
    assert r.ok, r.mismatch_detail

职责变更时同步:
  INAGENT/docs/agents/sessions/07-quality-inspector.md
  .cursor/rules/kb-session-quality-inspector.mdc
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from INAGENT.agents.knowledge_procurement_agent import (
    ChunkDecision,
    ProcurementDecision,
    enrich_chunk_decision_for_farmer,
)

logger = logging.getLogger(__name__)


def chunk_decision_from_export_dict(item: Dict[str, Any]) -> ChunkDecision:
    """Deserialize one ``decisions.json`` element to ``ChunkDecision``."""
    d = item.get("decision") or {}
    pd = ProcurementDecision(
        action=d["action"],
        target_kb=d["target_kb"],
        confidence=float(d["confidence"]),
        reason=str(d.get("reason") or ""),
        schema_gap=d.get("schema_gap"),
        suggested_value=d.get("suggested_value"),
        reason_code=d.get("reason_code"),
        rule_layer=d.get("rule_layer"),
        rule_confidence=d.get("rule_confidence"),
    )
    return ChunkDecision(
        chunk=item.get("chunk") or {},
        decision=pd,
        source_file=str(item.get("source_file") or ""),
        chunk_index=int(item.get("chunk_index", 0)),
    )


def decisions_from_export_json(
    raw: List[Dict[str, Any]],
) -> List[ChunkDecision]:
    """Deserialize full ``decisions.json`` list."""
    return [chunk_decision_from_export_dict(x) for x in raw]


def filter_accepted_chunks(
    decisions: List[ChunkDecision],
) -> List[Dict[str, Any]]:
    """Like ``KnowledgeProcurementAgent.filter_accepted`` (no agent)."""
    return [
        enrich_chunk_decision_for_farmer(cd).chunk
        for cd in decisions
        if cd.decision.action == "accept"
    ]


def normalize_chunk_for_compare(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Copy chunk; drop ``text`` if it mirrors ``page_content``."""
    c = json.loads(json.dumps(chunk))
    pc = c.get("page_content")
    tx = c.get("text")
    if tx is not None and tx == pc:
        del c["text"]
    return c


@dataclass
class ProcurementExportValidation:
    """Result of ``validate_procurement_export`` / ``compare_accept_lists``."""

    ok: bool
    expected_count: int
    on_disk_count: int
    first_mismatch_index: Optional[int] = None
    mismatch_detail: Optional[str] = None
    accept_actions_in_decisions: int = 0


@dataclass
class SummaryActionsCheck:
    """summary actions.accept vs decisions accept count."""

    ok: bool
    summary_accept: Optional[int] = None
    decisions_accept: Optional[int] = None
    detail: Optional[str] = None


class KnowledgeQualityInspectorAgent:
    """只读质检: 采购导出与 enrich/filter_accepted 语义一致。"""

    @staticmethod
    def compare_accept_lists(
        expected: List[Dict[str, Any]],
        on_disk: List[Dict[str, Any]],
        *,
        normalize_mirrored_text: bool = True,
    ) -> ProcurementExportValidation:
        """Compare accept lists; optional normalize mirrored ``text``."""
        if len(on_disk) != len(expected):
            return ProcurementExportValidation(
                ok=False,
                expected_count=len(expected),
                on_disk_count=len(on_disk),
                first_mismatch_index=0,
                mismatch_detail=(
                    f"length mismatch: expected {len(expected)} accepts, "
                    f"on_disk {len(on_disk)}"
                ),
            )
        for i, (a, b) in enumerate(zip(expected, on_disk)):
            left = (
                normalize_chunk_for_compare(a)
                if normalize_mirrored_text
                else a
            )
            right = (
                normalize_chunk_for_compare(b)
                if normalize_mirrored_text
                else b
            )
            if left != right:
                return ProcurementExportValidation(
                    ok=False,
                    expected_count=len(expected),
                    on_disk_count=len(on_disk),
                    first_mismatch_index=i,
                    mismatch_detail=f"chunk list differs at index {i}",
                )
        return ProcurementExportValidation(
            ok=True,
            expected_count=len(expected),
            on_disk_count=len(on_disk),
        )

    @staticmethod
    def validate_procurement_export(
        decisions_path: Path,
        accepted_path: Path,
        *,
        normalize_mirrored_text: bool = True,
    ) -> ProcurementExportValidation:
        """
        Validate that ``accepted_for_farmer.json`` matches
        ``filter_accepted(enrich_*(decisions))`` from ``decisions.json``.

        Parameters
        ----------
        decisions_path:
            JSON array of export rows (chunk_index, chunk, decision, ...).
        accepted_path:
            JSON array of chunk dicts (accept only).
        normalize_mirrored_text:
            Strip mirrored ``text`` before compare if True.
        """
        raw = json.loads(decisions_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(
                f"decisions.json must be a JSON array: {decisions_path}"
            )
        decisions = decisions_from_export_json(raw)
        accept_n = sum(1 for d in decisions if d.decision.action == "accept")
        expected = filter_accepted_chunks(decisions)
        on_disk_raw = json.loads(accepted_path.read_text(encoding="utf-8"))
        if not isinstance(on_disk_raw, list):
            raise ValueError(
                "accepted_for_farmer.json must be a JSON array: "
                f"{accepted_path}"
            )
        result = KnowledgeQualityInspectorAgent.compare_accept_lists(
            expected,
            on_disk_raw,
            normalize_mirrored_text=normalize_mirrored_text,
        )
        return ProcurementExportValidation(
            ok=result.ok,
            expected_count=result.expected_count,
            on_disk_count=result.on_disk_count,
            first_mismatch_index=result.first_mismatch_index,
            mismatch_detail=result.mismatch_detail,
            accept_actions_in_decisions=accept_n,
        )

    @staticmethod
    def validate_summary_accept_count(
        decisions_path: Path,
        summary_path: Path,
    ) -> SummaryActionsCheck:
        """Compare summary accept count to decisions.json accepts."""
        raw = json.loads(decisions_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return SummaryActionsCheck(
                ok=False,
                detail=f"invalid decisions: {decisions_path}",
            )
        decisions = decisions_from_export_json(raw)
        accept_n = sum(1 for d in decisions if d.decision.action == "accept")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        actions = summary.get("actions") if isinstance(summary, dict) else None
        if not isinstance(actions, dict) or "accept" not in actions:
            return SummaryActionsCheck(
                ok=True,
                decisions_accept=accept_n,
                detail="summary has no actions.accept; skip",
            )
        sa = int(actions["accept"])
        if sa != accept_n:
            return SummaryActionsCheck(
                ok=False,
                summary_accept=sa,
                decisions_accept=accept_n,
                detail=(
                    f"summary.actions.accept={sa} != "
                    f"decisions accept count={accept_n}"
                ),
            )
        return SummaryActionsCheck(
            ok=True,
            summary_accept=sa,
            decisions_accept=accept_n,
        )
