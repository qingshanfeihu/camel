"""SpecDecomposer — 规格分解器 + 可追溯矩阵 (Phase 2).

将产品知识 (product_knowledge) 分解为可枚举的需求清单，
然后对照测试用例构建覆盖矩阵。

流程:
  1. LLM 从 product_knowledge + bug_context 提取结构化需求列表
  2. 逐条在 test_cases_text 中搜索覆盖证据
  3. 输出: 需求覆盖矩阵 + 未覆盖缺口列表
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
class SpecRequirement:
    """单条规格需求。"""
    req_id: str = ""
    description: str = ""
    source: str = ""           # 来源: spec/bug_context/cli_reference
    constraints: str = ""      # 约束条件
    test_criteria: str = ""    # 验证标准
    priority: str = "medium"   # 与变更相关性: high/medium/low


@dataclass
class CoverageEntry:
    """单条覆盖检查结果。"""
    req_id: str = ""
    covered: bool = False
    covered_by_cases: List[str] = field(default_factory=list)
    gap_description: str = ""
    confidence: float = 0.0


@dataclass
class TraceabilityMatrix:
    """需求→用例 可追溯矩阵。"""
    requirements: List[SpecRequirement] = field(default_factory=list)
    coverage: List[CoverageEntry] = field(default_factory=list)
    total_requirements: int = 0
    covered_count: int = 0
    gap_count: int = 0

    def to_prompt_text(self, max_chars: int = 6000) -> str:
        """格式化为可注入 prompt 的文本。"""
        lines: list[str] = [
            "<traceability_matrix>",
            f"【需求覆盖矩阵】共 {self.total_requirements} 条需求，"
            f"已覆盖 {self.covered_count}，缺口 {self.gap_count}",
            "",
        ]
        # 先输出缺口（最重要）
        gaps = [c for c in self.coverage if not c.covered]
        if gaps:
            lines.append("## 未覆盖的需求缺口（评审重点）")
            for g in gaps:
                req = next((r for r in self.requirements if r.req_id == g.req_id), None)
                desc = req.description if req else g.req_id
                src = req.source if req else ""
                lines.append(f"- [{g.req_id}] {desc}")
                if src:
                    lines.append(f"  来源: {src}")
                if g.gap_description:
                    lines.append(f"  缺口: {g.gap_description}")
                lines.append("")

        # 再输出已覆盖（供参考）
        covered = [c for c in self.coverage if c.covered]
        if covered:
            lines.append("## 已覆盖的需求（已确认）")
            for c in covered[:20]:
                cases_str = ", ".join(c.covered_by_cases[:5])
                lines.append(f"- [{c.req_id}] 覆盖: {cases_str}")

        lines.append("</traceability_matrix>")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[矩阵截断]...\n</traceability_matrix>"
        return text


_DECOMPOSE_PROMPT = """\
你是一位产品需求分析专家。请将以下产品知识和变更描述分解为**可枚举的测试需求清单**。

<product_knowledge>
{product_knowledge}
</product_knowledge>

<bug_context>
{bug_context}
</bug_context>

<change_impact>
{change_impact}
</change_impact>

<tech_context>
{tech_context}
</tech_context>

请输出 JSON 数组，每个元素包含:
- req_id: 需求编号 (REQ_001, REQ_002, ...)
- description: 需求描述（一句话，可验证）
- source: 来源 (product_knowledge / bug_context / cli_reference / inferred)
- source_evidence: 来源原文引用（从 product_knowledge 或 bug_context 中摘录的原句）
- constraints: 约束条件（如有）
- test_criteria: 验证标准（如何判断测试通过）
- priority: 与本次变更的相关性 (high=新功能核心需求 / medium=相关功能 / low=已有功能基线)

要求:
1. 每条需求必须能在 <product_knowledge> 或 <bug_context> 中找到原文证据（在 source_evidence 中引用原句）
2. 优先提取 bug_context 中描述的新增/修改功能点
3. 包含负向需求（如非法参数应报错、未配置时操作应失败等）
4. 禁止从"通用安全常识"或"通用测试经验"推导需求（如"应审计日志"、"应压力测试"、"应零明文存储"）
5. 禁止臆造具体技术实现细节（如算法名、字段名、具体阈值数字），除非原文明确提到
6. 如果 product_knowledge 中仅提及概念但无具体规格，标注 source="inferred" 且 priority="low"
7. 数量控制在 10-25 条，宁缺毋滥
8. <tech_context> 列出了涉及模块的技术特征（协议栈、OSI层级、功能标签等），从产品CLI图谱自动提取。派生 inferred 类型需求时，必须确认关联的技术概念属于同一OSI层级（如L7特性不得作为L4需求的依据）。如果两个功能仅因共享泛化术语（如"加密""安全""转发"）而关联，但 tech_context 显示它们属于不同层级，标记 source="inferred", priority="low"
9. 仅输出 JSON 数组，不输出其他内容
"""

_COVERAGE_CHECK_PROMPT = """\
你是一位测试覆盖度分析专家。请逐条检查以下需求在测试用例中是否被覆盖。

<requirements>
{requirements_json}
</requirements>

<test_cases>
{test_cases_text}
</test_cases>

对每条需求，输出 JSON 数组，每个元素包含:
- req_id: 需求编号
- covered: true/false
- covered_by_cases: 覆盖该需求的用例编号列表（如 ["#20", "#23"]）
- gap_description: 如未覆盖，说明缺什么（一句话）
- confidence: 你的判断置信度 (0-1)

要求:
1. 必须在 <test_cases> 中找到**明确的用例编号和对应的 Expected Result** 才算覆盖
2. 仅凭 Description 不算覆盖，必须同时有 Expected Result 验证该需求
3. 如果需求涉及多个用例，列出所有相关用例编号
4. 对于横切面需求（如 IPv6、HTTP 版本），检查是否有专门的测试模块
5. 仅输出 JSON 数组，不输出其他内容
"""


def _clip(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + "\n...[truncated]..."


def _extract_json_array(text: str) -> list:
    """从 LLM 输出中提取 JSON 数组。"""
    text = (text or "").strip()
    # 去掉 markdown 代码块
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()
    # 找 [ ... ] 范围
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


class SpecDecomposer:
    """规格分解器：将产品知识分解为需求清单并构建覆盖矩阵。"""

    def __init__(self, model):
        self._model = model

    def decompose(
        self,
        product_knowledge: str,
        bug_context: str,
        change_impact: str = "",
        test_cases_text: str = "",
        tech_context: str = "",
    ) -> TraceabilityMatrix:
        """执行完整的规格分解 + 覆盖检查流程。"""
        # Step 1: 分解需求
        requirements = self._decompose_requirements(
            product_knowledge, bug_context, change_impact, tech_context,
        )
        if not requirements:
            logger.warning("SpecDecomposer: 需求分解返回空列表")
            return TraceabilityMatrix()

        # Step 2: 覆盖检查
        coverage = self._check_coverage(requirements, test_cases_text)

        # Step 3: 构建矩阵
        covered_count = sum(1 for c in coverage if c.covered)
        matrix = TraceabilityMatrix(
            requirements=requirements,
            coverage=coverage,
            total_requirements=len(requirements),
            covered_count=covered_count,
            gap_count=len(requirements) - covered_count,
        )
        logger.info(
            "SpecDecomposer: %d requirements, %d covered, %d gaps",
            matrix.total_requirements,
            matrix.covered_count,
            matrix.gap_count,
        )
        return matrix

    def _decompose_requirements(
        self,
        product_knowledge: str,
        bug_context: str,
        change_impact: str,
        tech_context: str = "",
    ) -> List[SpecRequirement]:
        prompt = _DECOMPOSE_PROMPT.format(
            product_knowledge=_clip(product_knowledge, 8000),
            bug_context=_clip(bug_context, 3000),
            change_impact=_clip(change_impact, 2000),
            tech_context=_clip(tech_context, 2000),
        )
        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="SpecDecomposer",
                content="你是产品需求分析专家，擅长将产品文档分解为可测试的需求清单。仅输出JSON。",
            ),
            model=self._model,
        )
        response = agent.step(prompt)
        raw = response.msgs[0].content if response.msgs else ""
        items = _extract_json_array(raw)
        requirements: List[SpecRequirement] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            requirements.append(SpecRequirement(
                req_id=str(item.get("req_id", f"REQ_{len(requirements)+1:03d}")),
                description=str(item.get("description", "")),
                source=str(item.get("source", "")),
                constraints=str(item.get("constraints", "")),
                test_criteria=str(item.get("test_criteria", "")),
                priority=str(item.get("priority", "medium")),
            ))
        return requirements

    def _check_coverage(
        self,
        requirements: List[SpecRequirement],
        test_cases_text: str,
    ) -> List[CoverageEntry]:
        if not test_cases_text or not requirements:
            return [
                CoverageEntry(req_id=r.req_id, covered=False, gap_description="无测试用例")
                for r in requirements
            ]

        req_json = json.dumps(
            [
                {
                    "req_id": r.req_id,
                    "description": r.description,
                    "constraints": r.constraints,
                    "test_criteria": r.test_criteria,
                    "priority": r.priority,
                }
                for r in requirements
            ],
            ensure_ascii=False,
            indent=2,
        )

        prompt = _COVERAGE_CHECK_PROMPT.format(
            requirements_json=_clip(req_json, 6000),
            test_cases_text=_clip(test_cases_text, 40000),
        )
        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="CoverageChecker",
                content="你是测试覆盖度分析专家，逐条检查需求是否被测试用例覆盖。仅输出JSON。",
            ),
            model=self._model,
        )
        response = agent.step(prompt)
        raw = response.msgs[0].content if response.msgs else ""
        items = _extract_json_array(raw)
        coverage: List[CoverageEntry] = []
        # 用 dict 快速查找
        coverage_map: Dict[str, CoverageEntry] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = CoverageEntry(
                req_id=str(item.get("req_id", "")),
                covered=bool(item.get("covered", False)),
                covered_by_cases=[str(c) for c in (item.get("covered_by_cases") or [])],
                gap_description=str(item.get("gap_description", "")),
                confidence=float(item.get("confidence", 0.5)),
            )
            coverage_map[entry.req_id] = entry

        # 确保每个需求都有覆盖结果
        for r in requirements:
            if r.req_id in coverage_map:
                coverage.append(coverage_map[r.req_id])
            else:
                coverage.append(CoverageEntry(
                    req_id=r.req_id,
                    covered=False,
                    gap_description="覆盖检查未返回结果",
                ))
        return coverage
