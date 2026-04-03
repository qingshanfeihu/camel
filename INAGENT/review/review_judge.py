# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""ReviewJudge — LLM-as-Judge 自动评估评审报告质量。

评审完成后自动运行，按多个维度对评审报告打分，帮助持续追踪评审质量改进。

Usage:
    from INAGENT.review.review_judge import ReviewJudge
    judge = ReviewJudge(model=model)
    scores = judge.evaluate(review_text, test_cases_text, product_knowledge)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """\
你是测试评审质量评估专家。你的任务是客观评估一份AI生成的测试用例评审报告的质量。

评估维度（每项1-5分）：

1. **证据充分性 (evidence)**: 每个发现是否有具体文本引用支撑？
   - 5分: 所有发现都引用了产品知识/CLI参考/用例原文中的具体段落
   - 3分: 大部分发现有证据，少量缺乏具体引用
   - 1分: 大部分发现缺乏证据，靠通用测试经验

2. **覆盖面完整性 (coverage)**: 是否涵盖了功能、协议、接口、地址族等横切面？
   - 5分: 覆盖了功能点、协议版本、地址族、管理接口、配置层级等多个维度
   - 3分: 覆盖了主要功能点，但遗漏了部分横切面维度
   - 1分: 仅关注单一维度（如仅格式检查）

3. **规格一致性 (spec_alignment)**: 指出的预期结果是否与产品规格一致？
   - 5分: 所有预期结果判断都与产品知识一致，对不确定项标注了待确认
   - 3分: 大部分预期结果正确，少量与产品知识不一致
   - 1分: 多处预期结果判断错误或臆测

4. **可操作性 (actionability)**: 建议是否具体可执行？
   - 5分: 每个建议都具体到case编号、修改内容、补充方向
   - 3分: 大部分建议可操作，少量过于笼统
   - 1分: 建议多为空泛的"应加强XX"类型

5. **溯源可靠性 (traceability)**: 是否避免了幻觉（引用不存在的case、虚构的spec内容）？
   - 5分: 无幻觉，所有引用都可验证
   - 3分: 少量引用存疑但不影响整体
   - 1分: 存在明显的幻觉引用

请严格输出以下JSON格式（不要输出其他内容）：
{
  "scores": {
    "evidence": <1-5>,
    "coverage": <1-5>,
    "spec_alignment": <1-5>,
    "actionability": <1-5>,
    "traceability": <1-5>
  },
  "overall": <1-5 加权平均>,
  "missing_dimensions": ["<遗漏的评审维度1>", "..."],
  "strengths": ["<优点1>", "..."],
  "weaknesses": ["<不足1>", "..."]
}
"""


class ReviewJudge:
    """LLM-as-Judge for automated review quality evaluation."""

    def __init__(self, model=None):
        self._model = model
        self._agent = None

    def _get_agent(self) -> ChatAgent:
        if self._agent is None:
            self._agent = ChatAgent(
                system_message=BaseMessage.make_assistant_message(
                    role_name="ReviewQualityJudge",
                    content=_JUDGE_SYSTEM_PROMPT,
                ),
                model=self._model,
                tools=[],
            )
        return self._agent

    def evaluate(
        self,
        review_text: str,
        test_cases_text: str = "",
        product_knowledge: str = "",
    ) -> Dict[str, Any]:
        """Evaluate a review report and return quality scores.

        Args:
            review_text: The review report to evaluate.
            test_cases_text: The test cases that were reviewed (for cross-reference).
            product_knowledge: The product knowledge used during review.

        Returns:
            Dict with scores, overall rating, missing dimensions, strengths,
            and weaknesses. Returns an error dict on failure.
        """
        context_parts = []
        if product_knowledge:
            context_parts.append(
                f"<product_knowledge>\n{product_knowledge[:3000]}\n</product_knowledge>"
            )
        if test_cases_text:
            context_parts.append(
                f"<test_cases>\n{test_cases_text[:3000]}\n</test_cases>"
            )
        context_parts.append(
            f"<review_report>\n{review_text}\n</review_report>"
        )

        user_msg = BaseMessage.make_user_message(
            role_name="Evaluator",
            content=(
                "请评估以下评审报告的质量。\n\n"
                + "\n\n".join(context_parts)
            ),
        )

        try:
            agent = self._get_agent()
            agent.reset()
            response = agent.step(user_msg)
            raw = response.msg.content.strip()

            # Extract JSON from response
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start >= 0 and end > start:
                    result = json.loads(raw[start:end])
                else:
                    raise
            logger.info(
                "Review judge scores: overall=%.1f, %s",
                result.get("overall", 0),
                result.get("scores", {}),
            )
            return result
        except json.JSONDecodeError as e:
            logger.warning("Judge response not valid JSON: %s", e)
            return {"error": f"Invalid JSON: {e}", "raw_response": raw}
        except Exception as e:
            logger.warning("Review judge failed: %s", e)
            return {"error": str(e)}
