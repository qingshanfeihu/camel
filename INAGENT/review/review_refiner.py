"""ReviewRefiner -- Multi-round Self-Refine loop for review quality improvement.

Implements the SELF-REFINE pattern (Madaan et al. 2023):
  Synthesis output -> Evaluate -> Feedback -> Improve (tool-grounded)

Key design principles (following Huang et al. ICLR 2024):
- Feedback comes from traceability_matrix gaps + review_experience (external)
- Improve step uses structured feedback (no pure introspection)
- Always run at least 1 improve iteration (first eval only establishes baseline)
- Max 2 iterations to balance quality vs latency

Reference: camel/datagen/self_improving_cot.py  SelfImprovingCoTPipeline
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage

from INAGENT.review.review_evaluator import EvaluationResult, ReviewEvaluator

logger = logging.getLogger(__name__)

_IMPROVE_PROMPT = """\
你是资深测试评审改进专家。请根据评估反馈，改进以下评审报告。

## 改进原则
1. 仅针对反馈中指出的具体问题进行改进，不要删除已有的高质量发现
2. 补充遗漏的需求时，必须有产品知识原文支撑，禁止臆造需求
3. 检查 Expected Result 的明确性，指出模糊表述并给出具体改写建议
4. 每条建议必须定位到具体用例编号和字段，写清从什么改为什么
5. 禁止使用术语：横切面维度、结构性建议、未覆盖、覆盖缺口
6. 保持表格格式输出:
### 发现 N: <标题>
| 项目 | 内容 |
|------|------|
| 涉及用例 | 模块名 > 用例 #A, #B, #C |
| 问题描述 | ... |
| 修改建议 | ... |
| 优先级 | High/Medium/Low |
7. 输出中禁止引用内部数据标签名（如 product_knowledge_excerpt、current_review、test_cases_excerpt），改用自然语言："根据产品知识文档"、"当前用例"、"评审报告"等
8. 禁止输出 emoji 字符（包括但不限于 ✅❌⚠💡，用文字替代）
9. 直接输出评审发现，不要包含"根据评估反馈中指出的..."等元叙述前言
10. 硬约束: 改进后发现数量不得少于当前报告。可增加不可合并删除
11. 补充遗漏需求时追加新 "### 发现 N+1:"，不修改已有发现的核心结论
12. 若建议新增用例，"涉及用例"写为 "全局缺失（当前无相关用例）"

## 当前评审报告
<current_review>
{review_text}
</current_review>

## 评估反馈
{feedback}

## 测试用例（参考）
<test_cases_excerpt>
{test_cases_excerpt}
</test_cases_excerpt>

## 产品知识（参考）
<product_knowledge_excerpt>
{product_knowledge_excerpt}
</product_knowledge_excerpt>

请输出改进后的完整评审报告（保持 "### 发现 N:" 格式，重新编号）。
"""


@dataclass
class RefineResult:
    """Result of the Self-Refine loop."""

    improved_text: str = ""
    iterations: int = 0
    initial_scores: Dict[str, float] = field(default_factory=dict)
    final_scores: Dict[str, float] = field(default_factory=dict)
    evaluations: List[EvaluationResult] = field(default_factory=list)


class ReviewRefiner:
    """Multi-round Self-Refine loop: Evaluate -> Feedback -> Improve.

    Unlike SelfImprovingCoTPipeline, feedback comes from external signals
    (traceability gaps + review experience), and evaluation uses hybrid
    programmatic + LLM scoring to avoid Huang 2024 degradation.

    The first evaluation establishes a baseline — improvement always runs
    at least once, regardless of the initial score.
    """

    def __init__(
        self,
        model: Any,
        tools: Optional[List] = None,
        max_iterations: int = 1,
        quality_threshold: float = 0.7,
    ):
        self._model = model
        self._tools = tools or []
        self._max_iterations = max_iterations
        self._quality_threshold = quality_threshold
        self._evaluator = ReviewEvaluator(model=model)

    def refine(
        self,
        review_text: str,
        traceability_text: str = "",
        experience_ctx: str = "",
        test_cases_text: str = "",
        product_knowledge: str = "",
    ) -> RefineResult:
        """Run the evaluate -> feedback -> improve loop.

        The first iteration ALWAYS runs improvement (baseline eval + improve).
        Subsequent iterations (if max_iterations > 1) check quality threshold.
        """
        result = RefineResult(improved_text=review_text)
        current_text = review_text

        for iteration in range(self._max_iterations):
            # Step 1: Evaluate current review
            eval_result = self._evaluator.evaluate(
                review_text=current_text,
                traceability_text=traceability_text,
                experience_ctx=experience_ctx,
            )
            result.evaluations.append(eval_result)

            if iteration == 0:
                result.initial_scores = eval_result.scores_dict

            logger.info(
                "ReviewRefiner iteration %d: overall=%.2f, scores=%s",
                iteration + 1,
                eval_result.overall_score,
                eval_result.scores_dict,
            )

            # Step 2: For iteration > 0, check if quality improved enough
            # First iteration ALWAYS proceeds to improve (establishes baseline)
            if iteration > 0 and eval_result.overall_score >= self._quality_threshold:
                logger.info(
                    "ReviewRefiner: quality threshold met (%.2f >= %.2f) after "
                    "improvement, stopping",
                    eval_result.overall_score,
                    self._quality_threshold,
                )
                result.final_scores = eval_result.scores_dict
                result.iterations = iteration + 1
                result.improved_text = current_text
                return result

            # Step 3: Generate feedback from evaluation
            feedback = eval_result.generate_feedback()
            feedback_rich = (
                f"<evaluation_scores>\n"
                f"coverage={eval_result.coverage_score:.2f}, "
                f"specificity={eval_result.specificity_score:.2f}, "
                f"cross_cutting={eval_result.cross_cutting_score:.2f}, "
                f"structural={eval_result.structural_score:.2f}, "
                f"clarity={eval_result.clarity_score:.2f}\n"
                f"</evaluation_scores>\n\n"
                f"{feedback}"
            )

            logger.info(
                "ReviewRefiner: feedback generated, %d uncovered gaps, "
                "%d missing dimensions, structural=%.2f, clarity=%.2f",
                len(eval_result.uncovered_gaps),
                len(eval_result.missing_dimensions),
                eval_result.structural_score,
                eval_result.clarity_score,
            )

            # Step 4: Improve with agent
            improved = self._improve(
                current_text=current_text,
                feedback=feedback_rich,
                test_cases_text=test_cases_text,
                product_knowledge=product_knowledge,
            )

            if improved and improved.strip():
                pre_count = len(re.findall(r"###\s*发现", current_text))
                post_count = len(re.findall(r"###\s*发现", improved))
                if pre_count > 0 and post_count < pre_count * 0.5:
                    logger.warning(
                        "ReviewRefiner: finding count dropped %d->%d (>50%%), "
                        "rolling back to pre-improve text",
                        pre_count, post_count,
                    )
                else:
                    current_text = improved
                    logger.info(
                        "ReviewRefiner: improve step completed, text length %d->%d, "
                        "findings %d->%d",
                        len(review_text), len(current_text), pre_count, post_count,
                    )
            else:
                logger.warning(
                    "ReviewRefiner: improve step returned empty, keeping previous"
                )

        # Final evaluation after all iterations
        final_eval = self._evaluator.evaluate(
            review_text=current_text,
            traceability_text=traceability_text,
            experience_ctx=experience_ctx,
        )
        result.evaluations.append(final_eval)
        result.final_scores = final_eval.scores_dict
        result.iterations = self._max_iterations
        result.improved_text = current_text

        logger.info(
            "ReviewRefiner: completed %d iterations, initial=%.2f -> final=%.2f",
            result.iterations,
            result.initial_scores.get("overall", 0),
            result.final_scores.get("overall", 0),
        )
        return result

    def _improve(
        self,
        current_text: str,
        feedback: str,
        test_cases_text: str,
        product_knowledge: str,
    ) -> str:
        """Run one improve step with structured feedback."""
        # Clip reference texts to avoid token overflow
        tc_excerpt = test_cases_text[:12000] if test_cases_text else "(unavailable)"
        pk_excerpt = product_knowledge[:8000] if product_knowledge else "(unavailable)"

        prompt = _IMPROVE_PROMPT.format(
            review_text=current_text,
            feedback=feedback,
            test_cases_excerpt=tc_excerpt,
            product_knowledge_excerpt=pk_excerpt,
        )

        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="ReviewImprover",
                content=(
                    "你是评审报告改进专家。根据评估反馈改进评审报告。\n"
                    "评估反馈中已包含需要补充的具体缺口和维度信息。\n"
                    "重点关注反馈中标记为不足的维度（如结构性建议、预期明确性）。\n"
                    "直接根据反馈改进报告，不需要额外查询。\n"
                    "输出改进后的完整报告，保持 '### 发现 N:' 格式。"
                ),
            ),
            model=self._model,
            tools=[],  # No tools — feedback already contains all needed info
        )

        response = agent.step(
            BaseMessage.make_user_message(
                role_name="Pipeline",
                content=prompt,
            )
        )
        return (response.msg.content or "").strip()
