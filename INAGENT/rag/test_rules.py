# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
测试规则引擎

提供测试用例编写规范（模版、命名、分类、优先级）和评审标准。
这些 "规则" 来自：
- Test_List_HTTP2_new_cli.json 中定义的 Option Definition（测试类型、优先级、Case ID 格式）
- 各 Test_list 文件的实际测试项结构

不依赖向量检索，是确定性的规则查询。
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_REFERENCE_DIR = Path(__file__).parent.parent / "knowledge_base" / "reference"

# ── 测试类型定义 ─────────────────────────────────────────────────────
TEST_TYPES = {
    "Configuration": "配置测试：验证功能的基本配置是否正确，包括启用/禁用/修改配置项",
    "Boundary": "边界值测试：验证参数在边界值（最大值、最小值、超出范围值）下的行为",
    "Negative": "负面测试：验证非法输入、错误配置、异常场景下系统的容错能力",
    "Compatibility": "兼容性测试：验证功能与其他模块/协议/版本的兼容性",
    "Functional": "功能测试：验证功能在正常使用场景下的完整性和正确性",
    "Load": "负载测试：验证功能在高并发、大流量场景下的性能和稳定性",
    "Integration": "集成测试：验证功能与其他系统组件集成后的端到端行为",
}

# ── 优先级定义 ─────────────────────────────────────────────────────
PRIORITY_LEVELS = {
    "High": "高优先级：核心功能、阻塞性问题、安全相关。必须在每轮回归测试中执行。",
    "Medium": "中优先级：常用功能路径、重要配置场景。应在主要版本测试中覆盖。",
    "Low": "低优先级：边缘场景、罕见配置组合。可在完整回归测试中执行。",
}

# ── Case ID 格式规范 ───────────────────────────────────────────────
CASE_ID_FORMAT = {
    "pattern": "MMSSIICCNN",
    "description": "10 位编号: MM(模块2位) + SS(Sheet2位) + II(大项2位) + CC(小项2位) + NN(序号2位)",
    "example": "0101010101",
    "fields": {
        "MM": "模块编号 (01=SLB, 02=LLB, 03=GSLB, ...)",
        "SS": "Sheet 编号 (01, 02, ...)",
        "II": "大项编号 (测试功能组)",
        "CC": "小项编号 (功能细项)",
        "NN": "测试序号 (同一小项内的序列号)",
    },
}

# ── 测试用例模板 ───────────────────────────────────────────────────
TEST_CASE_TEMPLATE = {
    "fields": [
        {"name": "Case ID", "required": True, "description": "遵循 MMSSIICCNN 格式的唯一编号"},
        {"name": "Status", "required": True, "description": "测试项状态 (Active/Deprecated)"},
        {"name": "Test Type", "required": True, "description": "测试类型 (Configuration/Boundary/Negative/Compatibility/Functional/Load/Integration)"},
        {"name": "Priority", "required": True, "description": "优先级 (High/Medium/Low)"},
        {"name": "Description", "required": True, "description": "测试描述 — 格式: '<功能点>: <测试动作和预期>'"},
        {"name": "Precondition", "required": False, "description": "前置条件（配置预设状态）"},
        {"name": "Steps", "required": True, "description": "测试步骤（编号列表）"},
        {"name": "Expected Result", "required": True, "description": "预期结果（与步骤对应）"},
        {"name": "Module", "required": True, "description": "所属产品模块"},
        {"name": "Feature", "required": True, "description": "所属功能特性"},
    ],
    "description_format": "<功能点>: <具体测试行为>, <预期结果描述>",
}

# ── 评审检查清单 ───────────────────────────────────────────────────
REVIEW_CHECKLIST = [
    {"id": "R01", "category": "完整性", "check": "所有必填字段均已填写", "severity": "High"},
    {"id": "R02", "category": "完整性", "check": "Case ID 遵循 MMSSIICCNN 格式", "severity": "High"},
    {"id": "R03", "category": "完整性", "check": "测试类型属于 7 种标准类型之一", "severity": "High"},
    {"id": "R04", "category": "完整性", "check": "优先级为 High/Medium/Low 之一", "severity": "High"},
    {"id": "R05", "category": "覆盖度", "check": "Configuration 类型覆盖了所有可配置项", "severity": "Medium"},
    {"id": "R06", "category": "覆盖度", "check": "Boundary 类型覆盖了关键参数的边界值", "severity": "Medium"},
    {"id": "R07", "category": "覆盖度", "check": "Negative 类型覆盖了主要错误场景", "severity": "Medium"},
    {"id": "R08", "category": "覆盖度", "check": "测试用例覆盖了功能规格书中的所有需求", "severity": "High"},
    {"id": "R09", "category": "质量", "check": "Description 格式为 '<功能点>: <行为描述>'", "severity": "Low"},
    {"id": "R10", "category": "质量", "check": "Steps 中的 CLI 命令语法正确（与 CLI 参考一致）", "severity": "High"},
    {"id": "R11", "category": "质量", "check": "Expected Result 可量化/可验证，非模糊表述", "severity": "Medium"},
    {"id": "R12", "category": "质量", "check": "测试步骤与预期结果一一对应", "severity": "Medium"},
    {"id": "R13", "category": "一致性", "check": "同一功能的测试用例优先级分布合理（不全为 High）", "severity": "Low"},
    {"id": "R14", "category": "一致性", "check": "无重复/高度相似的测试用例", "severity": "Medium"},
    {"id": "R15", "category": "覆盖度", "check": "Load 类型用例覆盖并发连接、大流量和稳定性等关键维度", "severity": "Medium"},
    {"id": "R16", "category": "覆盖度", "check": "高优先级功能包含 Stress/负载回归场景", "severity": "Medium"},
    {"id": "R17", "category": "Bug定向", "check": "Bug-to-Case 用例覆盖 Root Cause 场景", "severity": "High"},
    {"id": "R18", "category": "Bug定向", "check": "Bug-to-Case 用例覆盖 Testing Suggestions 的关键验证点", "severity": "High"},
    {"id": "R19", "category": "回归", "check": "已有回归用例覆盖了修复影响范围内的基础功能路径", "severity": "Medium"},
    {"id": "R20", "category": "回归", "check": "out_of_scope 判定有功能树或数据流的结构化依据", "severity": "Medium"},
]
RULE_INDEX = {item["id"]: item.copy() for item in REVIEW_CHECKLIST}


class TestRulesEngine:
    """
    测试规则引擎

    提供确定性的规则查询，不需要 LLM 推理：
    - 获取测试类型定义
    - 获取优先级标准
    - 获取 Case ID 格式
    - 获取测试用例模板
    - 获取评审检查清单
    - 从已有测试列表中检索相似测试项
    """

    def __init__(self, reference_dir: Optional[Path] = None):
        self._ref_dir = reference_dir or _REFERENCE_DIR
        self._test_items: List[Dict[str, Any]] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """延迟加载所有测试列表文件中的测试项。"""
        if self._loaded:
            return
        test_files = [
            "Test List HTTP2.0_phaseII.json",
            "Test List HTTP_2_new_cli.json",
            "Test list_HC.json",
            "Test_list_Cache_HTTP2.json",
            "HTTP2 test in phase II_jiangyz.json",
        ]
        for fname in test_files:
            fpath = self._ref_dir / fname
            if not fpath.exists():
                continue
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    continue
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("page_content", "")
                    meta = item.get("metadata", {})
                    if text:
                        self._test_items.append({
                            "text": text,
                            "source_file": meta.get("source_file", fname),
                            "step_type": meta.get("step_type", ""),
                            "product_module": meta.get("product_module", ""),
                        })
            except Exception as e:
                logger.warning("加载测试列表 %s 失败: %s", fname, e)
        logger.info("测试规则引擎: 加载 %d 条已有测试项", len(self._test_items))
        self._loaded = True

    @property
    def test_item_count(self) -> int:
        self._ensure_loaded()
        return len(self._test_items)

    def get_test_types(self) -> Dict[str, str]:
        """获取标准测试类型及其定义。"""
        return TEST_TYPES.copy()

    def get_priority_levels(self) -> Dict[str, str]:
        """获取优先级定义。"""
        return PRIORITY_LEVELS.copy()

    def get_case_id_format(self) -> Dict[str, Any]:
        """获取 Case ID 格式规范。"""
        return CASE_ID_FORMAT.copy()

    def get_template(self) -> Dict[str, Any]:
        """获取测试用例模板。"""
        return TEST_CASE_TEMPLATE.copy()

    def get_review_checklist(self) -> List[Dict[str, str]]:
        """获取评审检查清单。"""
        return [item.copy() for item in REVIEW_CHECKLIST]

    def get_rule_map(self) -> Dict[str, Dict[str, str]]:
        """获取规则映射字典（rule_id -> 规则定义）。"""
        return {k: v.copy() for k, v in RULE_INDEX.items()}

    def get_rules_context(self, purpose: str = "write") -> str:
        """
        将所有规则整合为上下文文本，供 LLM prompt 使用。

        Args:
            purpose: "write" (编写测试用例) 或 "review" (评审测试用例)
        """
        parts = []

        parts.append("# 测试用例规范\n")

        # 测试类型
        parts.append("## 测试类型 (Test Type)")
        for tt, desc in TEST_TYPES.items():
            parts.append(f"- **{tt}**: {desc}")

        # 优先级
        parts.append("\n## 优先级 (Priority)")
        for pri, desc in PRIORITY_LEVELS.items():
            parts.append(f"- **{pri}**: {desc}")

        # Case ID 格式
        parts.append("\n## Case ID 格式")
        parts.append(f"格式: {CASE_ID_FORMAT['pattern']}")
        parts.append(f"说明: {CASE_ID_FORMAT['description']}")
        for field, desc in CASE_ID_FORMAT["fields"].items():
            parts.append(f"  - {field}: {desc}")

        # 模板
        parts.append("\n## 测试用例字段")
        for f in TEST_CASE_TEMPLATE["fields"]:
            req = "必填" if f["required"] else "选填"
            parts.append(f"- **{f['name']}** ({req}): {f['description']}")

        parts.append(f"\n## Description 格式")
        parts.append(f"标准格式: {TEST_CASE_TEMPLATE['description_format']}")

        if purpose == "review":
            parts.append("\n## 评审检查清单")
            for item in REVIEW_CHECKLIST:
                parts.append(f"- [{item['severity']}] {item['id']}: {item['check']}")
            parts.append("\n## 规则引用格式（强约束）")
            parts.append("- 引用规则时必须同时给出 rule_id 与规则原文 check，二者必须一一对应。")
            parts.append("- 若证据不足，写“待确认”，不得强行标注规则编号。")
            parts.append("- 每个问题按以下字段组织：")
            parts.append("  - 问题: <具体问题与影响>")
            parts.append("  - 建议: <可执行的修复建议>")
            parts.append("  - 证据: <用例编号/CLI片段/知识来源>")
            parts.append("  - 规则: <Rxx - 规则原文>")

        return "\n".join(parts)

    def search_similar_tests(
        self,
        query: str,
        product_module: str = "",
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        从已有测试列表中搜索与查询相关的测试项。

        用于：
        - 编写新测试时参考已有测试的格式
        - 评审时检查是否与已有测试重复
        """
        self._ensure_loaded()
        q_lower = query.lower()
        q_parts = q_lower.split()

        scored: List[tuple] = []
        for idx, item in enumerate(self._test_items):
            text_lower = item["text"].lower()
            # 产品模块过滤
            if product_module:
                item_module = item.get("product_module", "").lower()
                if item_module and product_module.lower() not in item_module:
                    continue
            match_count = sum(1 for kw in q_parts if kw in text_lower)
            if match_count == 0:
                continue
            score = match_count / max(len(q_parts), 1)
            scored.append((score, idx))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._test_items[idx] for _, idx in scored[:max_results]]

    def format_similar_tests(self, items: List[Dict[str, Any]], max_chars: int = 2000) -> str:
        """格式化相似测试项列表。"""
        parts = []
        total = 0
        for i, item in enumerate(items, 1):
            text = item["text"][:300]
            src = item.get("source_file", "")
            chunk = f"[参考测试项 {i}] ({src})\n{text}"
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n\n".join(parts)


# ── 全局单例 ─────────────────────────────────────────────────────
_rules_engine: Optional[TestRulesEngine] = None


def get_test_rules_engine(ref_dir: Optional[Path] = None) -> TestRulesEngine:
    """获取测试规则引擎单例。"""
    global _rules_engine
    if _rules_engine is None:
        _rules_engine = TestRulesEngine(ref_dir)
    return _rules_engine
