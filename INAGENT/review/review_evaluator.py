"""ReviewEvaluator -- Hybrid programmatic + LLM 5-dimension review scoring.

Provides quality feedback for the ReviewRefiner self-improve loop.

Key design (following Huang et al. ICLR 2024):
- coverage_score, structural_score, clarity_score: PROGRAMMATIC (no LLM inflation)
- specificity_score, cross_cutting_score: LLM-judged with strict adversarial prompt
- External grounding: traceability_matrix gaps + review_experience entries

Reference: camel/models/reward/evaluator.py  evaluate() -> Dict[str,float]
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage

logger = logging.getLogger(__name__)

# ── LLM prompt for specificity + cross_cutting only ──────────────────

_LLM_EVAL_PROMPT = """\
你是一位严格的测试评审质量评估专家。请仅评估以下两个维度：

## 维度 1: specificity_score — 具体性 (0.0-1.0)
逐条检查每个"发现"是否引用了具体的:
- case ID（如 #64, #197）
- product_knowledge 段落（带引号的原文）
- CLI 命令名称（如 `slb mode ircookie`）
- REQ 编号（如 REQ_006）

评分标准（必须严格执行）:
- 统计总发现数 N，其中引用了至少 2 种以上具体证据的发现数 M
- specificity_score = M / N
- 如果某发现仅笼统提及"product_knowledge 提到..."但未引用原文，该发现不计入 M

## 维度 2: cross_cutting_score — 横切面覆盖 (0.0-1.0)
检查评审是否**独立**覆盖了以下横切维度（每个维度需有独立发现，不可合并在同一发现中）:
- HTTP 版本兼容性（HTTP/1.1 vs HTTP/2）
- 地址族（IPv4 vs IPv6）
- 配置层级（global vs group）
- HA 主备同步
- SSL/TLS offload
- 配置持久化（save/restore/upgrade）

评分标准:
- 统计上述 6 个维度中，有多少个被**独立发现**覆盖（非合并提及）
- cross_cutting_score = 已覆盖维度数 / 6
- 若某维度仅在某个发现的"涉及范围"中被提及但未展开分析，不计入

## 待评估的评审报告
<review_report>
{review_text}
</review_report>

## 输出格式（仅 JSON，无其他文本）
```json
{{
  "specificity_score": 0.0,
  "cross_cutting_score": 0.0,
  "specificity_reasoning": "N条发现中M条有具体引用: ...",
  "cross_cutting_reasoning": "覆盖维度: ..., 未覆盖: ...",
  "missing_dimensions": ["维度名称"]
}}
```
"""

# ── Structural keywords for programmatic scoring ─────────────────────

_STRUCTURAL_KEYWORDS = [
    "精简", "删减", "重组", "合并", "调整重心", "调整权重",
    "冗余", "重复用例", "可合并", "过度覆盖", "减少",
    "用例瘦身", "测试重心", "权重调整", "回归范围收窄",
]

_CLARITY_KEYWORDS = [
    "预期结果", "Expected Result", "模糊", "不明确", "不可验证",
    "不具体", "泛泛", "可量化", "可验证", "grep",
    "配置成功", "显示成功", "需要细化", "应明确",
]


@dataclass
class EvaluationResult:
    """Structured evaluation result from ReviewEvaluator."""

    coverage_score: float = 0.0
    specificity_score: float = 0.0
    cross_cutting_score: float = 0.0
    structural_score: float = 0.0
    clarity_score: float = 0.0
    uncovered_gaps: List[str] = field(default_factory=list)
    weak_findings: List[str] = field(default_factory=list)
    missing_dimensions: List[str] = field(default_factory=list)

    @property
    def overall_score(self) -> float:
        scores = [
            self.coverage_score,
            self.specificity_score,
            self.cross_cutting_score,
            self.structural_score,
            self.clarity_score,
        ]
        return sum(scores) / len(scores)

    @property
    def scores_dict(self) -> Dict[str, float]:
        return {
            "coverage": self.coverage_score,
            "specificity": self.specificity_score,
            "cross_cutting": self.cross_cutting_score,
            "structural": self.structural_score,
            "clarity": self.clarity_score,
            "overall": self.overall_score,
        }

    def generate_feedback(self) -> str:
        """Convert evaluation into structured feedback for the refine step."""
        parts: List[str] = []
        parts.append("<evaluation_feedback>")

        if self.uncovered_gaps:
            parts.append("【未覆盖的需求缺口 -- 请在改进中补充对应发现】")
            for gap in self.uncovered_gaps:
                parts.append(f"  - {gap}")

        if self.weak_findings:
            parts.append("【证据不足的发现 -- 请补充具体引用或删除】")
            for wf in self.weak_findings:
                parts.append(f"  - {wf}")

        if self.missing_dimensions:
            parts.append("【缺失的横切面维度 -- 请作为独立发现补充】")
            for dim in self.missing_dimensions:
                parts.append(f"  - {dim}")

        if self.structural_score < 0.5:
            parts.append(
                "【结构性建议不足 (score=%.2f)】当前评审仅建议'增加用例'，"
                "请增加用例精简/重组/删减级别的建议。考虑：\n"
                "  - 已有功能 vs 新功能的用例权重是否合理？\n"
                "  - 是否有冗余/重复用例可合并？\n"
                "  - 回归范围是否可以收窄？" % self.structural_score
            )

        if self.clarity_score < 0.5:
            parts.append(
                "【预期结果明确性未充分审视 (score=%.2f)】请检查测试用例的 "
                "Expected Result 是否具体可验证。常见问题：\n"
                "  - '配置成功' → 应细化为具体效果和可验证标准\n"
                "  - '显示成功' → 应说明预期输出内容\n"
                "  - CLI configure 用例还需说明配置是否可 save/restore" % self.clarity_score
            )

        parts.append("</evaluation_feedback>")
        return "\n".join(parts)


class ReviewEvaluator:
    """Hybrid programmatic + LLM review quality evaluator.

    - coverage_score: PROGRAMMATIC (REQ gap matching)
    - structural_score: PROGRAMMATIC (keyword detection)
    - clarity_score: PROGRAMMATIC (keyword detection)
    - specificity_score: LLM-judged (strict adversarial prompting)
    - cross_cutting_score: LLM-judged (strict adversarial prompting)
    """

    def __init__(self, model: Any):
        self._model = model

    def evaluate(
        self,
        review_text: str,
        traceability_text: str = "",
        experience_ctx: str = "",
    ) -> EvaluationResult:
        """Score review across 5 dimensions (3 programmatic + 2 LLM)."""

        # ── Programmatic dimensions ──────────────────────────────
        coverage_score, uncovered_gaps = self._compute_coverage(
            review_text, traceability_text
        )
        structural_score = self._compute_structural(review_text)
        clarity_score = self._compute_clarity(review_text)

        # ── LLM dimensions (specificity + cross_cutting) ─────────
        specificity_score = 0.5  # default if LLM fails
        cross_cutting_score = 0.5
        missing_dimensions: List[str] = []
        weak_findings: List[str] = []

        try:
            llm_result = self._llm_evaluate(review_text)
            specificity_score = llm_result.get("specificity_score", 0.5)
            cross_cutting_score = llm_result.get("cross_cutting_score", 0.5)
            missing_dimensions = llm_result.get("missing_dimensions", [])
        except Exception as e:
            logger.warning("ReviewEvaluator LLM eval failed: %s", e)

        result = EvaluationResult(
            coverage_score=coverage_score,
            specificity_score=specificity_score,
            cross_cutting_score=cross_cutting_score,
            structural_score=structural_score,
            clarity_score=clarity_score,
            uncovered_gaps=uncovered_gaps,
            weak_findings=weak_findings,
            missing_dimensions=missing_dimensions,
        )

        logger.info(
            "ReviewEvaluator: coverage=%.2f(prog), specificity=%.2f(llm), "
            "cross_cutting=%.2f(llm), structural=%.2f(prog), clarity=%.2f(prog), "
            "overall=%.2f",
            coverage_score, specificity_score, cross_cutting_score,
            structural_score, clarity_score, result.overall_score,
        )
        return result

    # ── Programmatic: coverage ────────────────────────────────────

    @staticmethod
    def _compute_coverage(review_text: str, traceability_text: str) -> tuple:
        """Programmatically compute coverage by checking GAP mentions.

        Extracts [REQ_xxx] gap IDs from traceability_text, then checks
        which ones appear in the review_text.
        Returns (score, list_of_uncovered_gap_descriptions).
        """
        if not traceability_text:
            return 0.5, []  # no traceability → can't judge

        # Extract all REQ identifiers from traceability text
        # Patterns: [REQ_006], REQ_006, REQ-006
        all_reqs = set(re.findall(r"REQ[_\-]?(\d+)", traceability_text))
        if not all_reqs:
            return 0.5, []

        # Extract lines containing "缺口" or "gap" near each REQ
        gap_reqs: Dict[str, str] = {}
        for line in traceability_text.split("\n"):
            line_lower = line.lower()
            if "缺口" in line or "gap" in line_lower or "未覆盖" in line:
                req_matches = re.findall(r"REQ[_\-]?(\d+)", line)
                for req_id in req_matches:
                    gap_reqs[req_id] = line.strip()[:200]

        # If no explicit gaps found, treat all REQs as potential gaps
        if not gap_reqs:
            for req_id in all_reqs:
                gap_reqs[req_id] = f"REQ_{req_id}"

        total_gaps = len(gap_reqs)
        if total_gaps == 0:
            return 0.5, []

        # Check which gap REQs appear in the review text
        review_upper = review_text.upper()
        covered = 0
        uncovered_descriptions: List[str] = []
        for req_id, description in gap_reqs.items():
            # Check for REQ_xxx or REQ-xxx or REQ xxx in review
            patterns = [
                f"REQ_{req_id}",
                f"REQ-{req_id}",
                f"REQ{req_id}",
            ]
            found = any(p in review_upper for p in [x.upper() for x in patterns])
            if found:
                covered += 1
            else:
                uncovered_descriptions.append(description)

        score = covered / total_gaps
        logger.info(
            "ReviewEvaluator coverage: %d/%d gaps covered (%.2f)",
            covered, total_gaps, score,
        )
        return round(score, 2), uncovered_descriptions

    # ── Programmatic: structural ──────────────────────────────────

    @staticmethod
    def _compute_structural(review_text: str) -> float:
        """Check if review contains structural suggestions (not just 'add cases').

        Returns 0.0 if no structural keywords found,
        0.3 if mentioned but not in a finding context,
        0.7 if present in a finding with concrete suggestion,
        1.0 if multiple structural suggestions found.
        """
        text_lower = review_text.lower()

        # Count structural keyword mentions in finding context
        # Split review into findings
        findings = re.split(r"###\s*发现\s*\d+", review_text)
        structural_findings = 0
        for finding in findings:
            finding_lower = finding.lower()
            if any(kw in finding_lower for kw in _STRUCTURAL_KEYWORDS):
                structural_findings += 1

        if structural_findings == 0:
            # Check if any keyword appears at all
            if any(kw in text_lower for kw in _STRUCTURAL_KEYWORDS):
                return 0.3
            return 0.0
        elif structural_findings == 1:
            return 0.7
        else:
            return 1.0

    # ── Programmatic: clarity ─────────────────────────────────────

    @staticmethod
    def _compute_clarity(review_text: str) -> float:
        """Check if review examines Expected Result specificity.

        Returns 0.0 if review doesn't mention clarity issues,
        0.5 if mentions clarity concerns,
        1.0 if has dedicated finding about clarity.
        """
        text_lower = review_text.lower()

        # Check for clarity-related keywords
        clarity_mentions = sum(
            1 for kw in _CLARITY_KEYWORDS
            if kw.lower() in text_lower
        )

        # Check if there's a dedicated finding about clarity
        findings = re.split(r"###\s*发现\s*\d+", review_text)
        dedicated_clarity_finding = False
        for finding in findings:
            finding_lower = finding.lower()
            # A finding is "dedicated to clarity" if its title or main body
            # focuses on Expected Result / 预期结果
            if ("预期结果" in finding_lower or "expected result" in finding_lower):
                # And also mentions actionable terms
                if any(kw.lower() in finding_lower for kw in [
                    "模糊", "不明确", "不具体", "可量化", "可验证",
                    "需要细化", "应明确", "配置成功",
                ]):
                    dedicated_clarity_finding = True
                    break

        if dedicated_clarity_finding:
            return 1.0
        elif clarity_mentions >= 3:
            return 0.5
        elif clarity_mentions >= 1:
            return 0.3
        return 0.0

    # ── LLM: specificity + cross_cutting ──────────────────────────

    def _llm_evaluate(self, review_text: str) -> Dict[str, Any]:
        """Use LLM to evaluate specificity and cross_cutting dimensions."""
        prompt = _LLM_EVAL_PROMPT.format(review_text=review_text)

        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="ReviewEvaluator",
                content=(
                    "你是严格的评审质量评估专家。你的打分必须保守：\n"
                    "- 不要给出高于 0.8 的分数，除非有充分的量化论证\n"
                    "- 默认假设评审有遗漏，除非你能逐维度证明覆盖完整\n"
                    "- 仅输出 JSON 对象，不要输出其他文本"
                ),
            ),
            model=self._model,
        )

        response = agent.step(
            BaseMessage.make_user_message(
                role_name="Pipeline",
                content=prompt,
            )
        )
        raw = (response.msg.content or "").strip()
        return self._parse_llm_result(raw)

    @staticmethod
    def _parse_llm_result(raw: str) -> Dict[str, Any]:
        """Parse LLM JSON output."""
        fence_m = re.search(r"```(?:json)?\s*(\{.+\})\s*```", raw, re.DOTALL)
        if fence_m:
            json_str = fence_m.group(1)
        else:
            json_str = ""
            depth = 0
            start = -1
            for i, ch in enumerate(raw):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start >= 0:
                        json_str = raw[start:i + 1]
                        break
            if not json_str:
                logger.warning("ReviewEvaluator LLM: no JSON found")
                return {}

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning("ReviewEvaluator LLM: JSON error: %s", e)
            return {}

        def _clamp(val: Any) -> float:
            try:
                v = float(val)
            except (TypeError, ValueError):
                return 0.5
            return max(0.0, min(1.0, v))

        return {
            "specificity_score": _clamp(data.get("specificity_score")),
            "cross_cutting_score": _clamp(data.get("cross_cutting_score")),
            "missing_dimensions": data.get("missing_dimensions") or [],
        }
