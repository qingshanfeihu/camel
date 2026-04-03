"""AdversarialVerifier — 对抗验证器 (Phase 1B).

对 Synthesis Worker 生成的每条发现进行工具辅助的事实核查。
核心逻辑: 对每条发现, 主动在完整测试用例中搜索反证。

输入: findings JSON + 完整 test_cases_text + product_knowledge
输出: 每条发现的 verdict (confirmed/refuted/uncertain) + counter_evidence
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


@dataclass
class VerificationResult:
    """单条发现的验证结果。"""
    finding_id: str = ""
    finding_title: str = ""
    verdict: str = "uncertain"  # confirmed / refuted / uncertain
    counter_evidence: str = ""
    reasoning: str = ""
    confidence: float = 0.5


@dataclass
class VerificationReport:
    """完整的验证报告。"""
    results: List[VerificationResult] = field(default_factory=list)
    confirmed_count: int = 0
    refuted_count: int = 0
    uncertain_count: int = 0

    def get_confirmed_findings(self) -> List[str]:
        return [r.finding_id for r in self.results if r.verdict == "confirmed"]

    def get_refuted_findings(self) -> List[str]:
        return [r.finding_id for r in self.results if r.verdict == "refuted"]

    def to_prompt_text(self) -> str:
        lines = [
            "<adversarial_verification>",
            f"【对抗验证结果】确认 {self.confirmed_count}, "
            f"驳回 {self.refuted_count}, 待定 {self.uncertain_count}",
            "",
        ]
        for r in self.results:
            icon = {"confirmed": "[确认]", "refuted": "[驳回]", "uncertain": "[待定]"}
            lines.append(f"- {r.finding_id} {icon.get(r.verdict, '[?]')} {r.finding_title}")
            if r.verdict == "refuted" and r.counter_evidence:
                lines.append(f"  反证: {r.counter_evidence}")
            if r.verdict == "uncertain" and r.reasoning:
                lines.append(f"  说明: {r.reasoning}")
        lines.append("</adversarial_verification>")
        return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + "\n...[truncated]..."


_VERIFY_PROMPT = """\
你是一位公正的事实核查专家。你的任务是验证以下评审发现的准确性。

重要原则：
- 你的目标是"验证"而非"反驳"。不要为了反驳而反驳
- "间接覆盖"或"隐含验证"不等于"明确覆盖"。如果测试用例没有显式测试某个场景，不应仅凭推理认为它已被覆盖
- 只有当测试用例的 Test Step 或 Expected Result 中**直接、明确**地描述了该发现声称缺失的验证点时，才能判定为 refuted
- 功能 A 的测试不能替代功能 B 的测试，即使 A 和 B 有关联

对于每条发现，你需要:
1. 在 <test_cases> 中逐条搜索，寻找**直接且明确**覆盖该发现声称缺失内容的用例
2. 检查发现中引用的 Case ID 是否真实存在，且描述是否准确
3. 检查发现的逻辑推理是否正确（如 CLI 参数语义是否被正确理解）
4. 利用 <review_experience> 中的经验判断是否违反已知规则

<findings>
{findings_json}
</findings>

<test_cases>
{test_cases_text}
</test_cases>

<product_knowledge>
{product_knowledge}
</product_knowledge>

<review_experience>
{review_experience}
</review_experience>

对每条发现输出 JSON 数组，每个元素包含:
- finding_id: 发现编号
- finding_title: 发现标题
- verdict: "confirmed"（确认有效）/ "refuted"（已被明确反驳）/ "uncertain"（无法判断）
- counter_evidence: 反证描述（仅 refuted 时填写：列出**具体用例编号**和**精确引用其 Expected Result 原文**）
- reasoning: 你的判断逻辑（2-3 句话）
- confidence: 置信度 (0-1)

判定规则（从严执行）:
- refuted：**仅当** <test_cases> 中某条用例的 Test Step 或 Expected Result **直接且明确**覆盖了该发现声称缺失的验证点。间接推理、隐含关联、相似但不同的场景均**不构成反驳**
- refuted：发现引用的 Case ID 不存在，或发现描述与用例实际内容严重不符
- confirmed：发现有 <product_knowledge> 支撑，且 <test_cases> 中确实没有对应的直接覆盖
- uncertain：存在部分间接覆盖但不够直接，或信息不足以明确判断

当你不确定时，倾向于 uncertain 而非 refuted。宁可保留一条可能正确的发现，也不要错误驳回。

仅输出 JSON 数组，不输出其他内容。
"""


def _extract_json_array(text: str) -> list:
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()
    start = text.find("[")
    if start < 0:
        return []
    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return []


class AdversarialVerifier:
    """对抗验证器：对 Synthesis 输出的发现逐条进行事实核查。"""

    def __init__(self, model, cli_graph=None):
        self._model = model
        self._cli_graph = cli_graph

    def verify(
        self,
        findings: List[Dict[str, Any]],
        test_cases_text: str,
        product_knowledge: str,
        review_experience: str = "",
    ) -> VerificationReport:
        """执行对抗验证。"""
        if not findings:
            return VerificationReport()

        findings_json = json.dumps(
            [
                {
                    "finding_id": f.get("id", ""),
                    "title": f.get("title", ""),
                    "scope": f.get("scope", ""),
                    "issue_or_suggestion": f.get("issue_or_suggestion", ""),
                    "evidence": f.get("evidence", ""),
                    "priority": f.get("priority", ""),
                }
                for f in findings
            ],
            ensure_ascii=False,
            indent=2,
        )

        prompt = _VERIFY_PROMPT.format(
            findings_json=_clip(findings_json, 10000),
            test_cases_text=_clip(test_cases_text, 45000),
            product_knowledge=_clip(product_knowledge, 6000),
            review_experience=review_experience or "(无历史经验)",
        )

        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="AdversarialVerifier",
                content=(
                    "你是公正的事实核查专家。\n"
                    "你的目标是验证评审发现的准确性，而非一味反驳。\n"
                    "你必须在测试用例中逐条搜索，基于直接证据判断。\n"
                    "只有当某条用例的 Test Step 或 Expected Result 直接、明确覆盖了"
                    "发现声称缺失的验证点时，才能判定 refuted。\n"
                    "间接推理、隐含关联、相似场景不构成反驳。\n"
                    "不确定时请判定 uncertain。\n"
                    "仅输出 JSON 数组。"
                ),
            ),
            model=self._model,
        )

        response = agent.step(prompt)
        raw = response.msgs[0].content if response.msgs else ""
        items = _extract_json_array(raw)

        results: List[VerificationResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append(VerificationResult(
                finding_id=str(item.get("finding_id", "")),
                finding_title=str(item.get("finding_title", "")),
                verdict=str(item.get("verdict", "uncertain")),
                counter_evidence=str(item.get("counter_evidence", "")),
                reasoning=str(item.get("reasoning", "")),
                confidence=float(item.get("confidence", 0.5)),
            ))

        # 处理未被验证的发现
        verified_ids = {r.finding_id for r in results}
        for f in findings:
            fid = f.get("id", "")
            if fid and fid not in verified_ids:
                results.append(VerificationResult(
                    finding_id=fid,
                    finding_title=f.get("title", ""),
                    verdict="uncertain",
                    reasoning="验证器未返回该发现的验证结果",
                ))

        confirmed = sum(1 for r in results if r.verdict == "confirmed")
        refuted = sum(1 for r in results if r.verdict == "refuted")
        uncertain = sum(1 for r in results if r.verdict == "uncertain")

        if self._cli_graph:
            self._validate_cli_commands(findings, results)

        report = VerificationReport(
            results=results,
            confirmed_count=confirmed,
            refuted_count=refuted,
            uncertain_count=uncertain,
        )
        logger.info(
            "AdversarialVerifier: %d confirmed, %d refuted, %d uncertain",
            confirmed, refuted, uncertain,
        )
        return report

    def _validate_cli_commands(
        self,
        findings: List[Dict[str, Any]],
        results: List[VerificationResult],
    ) -> None:
        """对 findings 中 backtick 内的 CLI 命令做存在性校验。"""
        result_map = {r.finding_id: r for r in results}
        _cmd_re = re.compile(r"`([^`]{4,80})`")
        _cli_prefixes = ("slb ", "no ", "show ", "clear ", "system ", "ip ", "http ")
        for f in findings:
            fid = f.get("id", "")
            text = f"{f.get('issue_or_suggestion', '')} {f.get('evidence', '')}"
            cmds = _cmd_re.findall(text)
            invalid_cmds = []
            for cmd in cmds:
                cmd_lower = cmd.strip().lower()
                if not any(cmd_lower.startswith(p) for p in _cli_prefixes):
                    continue
                exists, _similar = self._cli_graph.command_exists(cmd.strip())
                if not exists:
                    invalid_cmds.append(cmd.strip())
            if invalid_cmds and fid in result_map:
                r = result_map[fid]
                annotation = "CLI命令未在参考中找到: " + ", ".join(
                    f"`{c}`" for c in invalid_cmds
                )
                if r.reasoning:
                    r.reasoning += f"; {annotation}"
                else:
                    r.reasoning = annotation
                logger.warning(
                    "AdversarialVerifier: finding %s has unverified CLI: %s",
                    fid, invalid_cmds,
                )
