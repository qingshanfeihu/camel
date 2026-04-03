from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RunLedger:
    """Per-run quality record, persisted as JSON for cross-run analysis."""

    run_id: str = ""
    bug_id: str = ""
    timestamp: str = ""
    total_seconds: float = 0.0

    module_hints: List[str] = field(default_factory=list)
    chunk_count: int = 0

    retrieval_confidence: Dict[str, Any] = field(default_factory=dict)
    graphrag_used: bool = False

    spec_requirements: int = 0
    spec_covered: int = 0
    spec_gaps: int = 0

    worker_findings: Dict[str, int] = field(default_factory=dict)

    refiner_initial_score: float = 0.0
    refiner_final_score: float = 0.0

    adversarial_confirmed: int = 0
    adversarial_refuted: int = 0
    adversarial_uncertain: int = 0

    judge_scores: Dict[str, Any] = field(default_factory=dict)
    judge_overall: float = 0.0

    retrieval_usage: Dict[str, Any] = field(default_factory=dict)

    findings_count: int = 0
    all_gates_passed: bool = False

    def save(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "run_ledger.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        logger.info("RunLedger saved: %s", path)
        return path

    @classmethod
    def load(cls, path: Path) -> Optional["RunLedger"]:
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception as e:
            logger.warning("Failed to load RunLedger from %s: %s", path, e)
            return None

    @classmethod
    def load_history(cls, jobs_dir: Path, last_n: int = 20) -> List["RunLedger"]:
        ledgers = []
        for ledger_file in sorted(jobs_dir.rglob("run_ledger.json"), reverse=True):
            entry = cls.load(ledger_file)
            if entry:
                ledgers.append(entry)
            if len(ledgers) >= last_n:
                break
        return ledgers

    def suggest_adjustments(self, history: List["RunLedger"]) -> Dict[str, str]:
        if len(history) < 3:
            return {}
        suggestions: Dict[str, str] = {}
        waste_ratios = [
            h.retrieval_usage.get("waste_ratio", 0.5)
            for h in history
            if h.retrieval_usage
        ]
        if waste_ratios and sum(waste_ratios) / len(waste_ratios) > 0.6:
            suggestions["top_k_final"] = "reduce from 8 to 5 (high retrieval waste)"
        judge_coverages = [
            h.judge_scores.get("scores", {}).get("coverage", 3)
            for h in history
            if h.judge_scores and "scores" in h.judge_scores
        ]
        if judge_coverages and sum(judge_coverages) / len(judge_coverages) < 2.5:
            suggestions["graphrag"] = "consider rebuilding GraphRAG index (low coverage scores)"
        return suggestions
