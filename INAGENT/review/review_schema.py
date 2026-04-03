from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class CaseIssue:
    """单条用例问题标注。"""

    case_seq: int
    case_id: str
    rule_id: str
    severity: str
    dimension: str
    description: str


@dataclass
class CoverageGap:
    """覆盖度缺口描述。"""

    feature: str
    gap_type: str
    suggested_action: str
    detail: str


@dataclass
class ModuleReviewResult:
    """单模块结构化评审结果。"""

    module: str
    case_count: int
    pass_count: int
    issues: List[CaseIssue] = field(default_factory=list)
    coverage_gaps: List[CoverageGap] = field(default_factory=list)
    load_stress_assessment: str = ""
    format_issues_count: int = 0
    summary: str = ""
    bug_coverage_answers: Optional[List[str]] = None

    def to_dict(self) -> dict:
        return asdict(self)
