"""
将 Bug-to-Case 类型的测试评审输入（Excel + Bug 修复信息）转换为结构化 JSON。

用法:
    python prepare_bug_to_case.py <bug_dir>
    python prepare_bug_to_case.py INAGENT/jobs/test_review/Bug\ 139213

输出: <bug_dir>/bug_to_case.json
"""

import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = ROOT.parent  # c:\SynologyDrive\INFOAGEN
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("prepare_bug_to_case")

# 自动跳过的 Sheet 名称（不区分大小写）
_SKIP_SHEETS = {"option definition", "list sample", "sample", "template", "说明", "readme", "history"}

# Excel 中需要提取的标准列名
_CASE_COLUMNS = [
    "Item", "Sub Item", "ID", "Test Types", "Description",
    "Expected Result", "Priority", "Note",
]
# 可选的执行结果列
_RESULT_COLUMNS = ["Release", "Automated", "StressResult", "FuncResult"]


def _clean(value) -> str:
    """安全地转为字符串，去除首尾空白。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _parse_bug_fix_detail(txt_path: Path) -> dict:
    """解析 Bug fix detail.txt，提取结构化字段。"""
    text = txt_path.read_text(encoding="utf-8")

    fields = {}
    # 提取已知键值对
    # bug_id 优先匹配 Description 行中的 Bug 编号
    patterns = {
        "bug_id": r"Description:\s*Bug\s+(\d+)",
        "title": r"Description:\s*Bug\s+\d+\s*-\s*(.+?)(?:\n|$)",
        "root_cause": r"Root\s+Cause:\s*(.+?)(?:\n|$)",
        "condition": r"Condition\s+of\s+Occurrence:\s*(.+?)(?:\n|$)",
        "fixed_details": r"Fixed\s+Details:\s*(.+?)(?:\n|$)",
        "testing_suggestions": r"Testing\s+Suggestions:\s*(.+?)(?:\n|$)",
        "affected_release": r"Affected\s+Release:\s*(.+?)(?:\n|$)",
        "extra_impact": r"Extra\s+Impact:\s*(.+?)(?:\n|$)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            fields[key] = m.group(1).strip()

    def _extract_section(label: str, next_labels: list[str]) -> str:
        next_pat = "|".join(re.escape(x) for x in next_labels)
        pattern = rf"{re.escape(label)}\s*:\s*(.*?)(?=\n\s*(?:{next_pat})\s*:|\Z)"
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            return ""
        return m.group(1).strip()

    section_order = [
        "Description",
        "Root Cause",
        "Condition of Occurrence",
        "Fixed Details",
        "Testing Suggestions",
        "Affected Release",
        "Extra Impact",
    ]
    section_key_map = {
        "Description": "description",
        "Root Cause": "root_cause",
        "Condition of Occurrence": "condition",
        "Fixed Details": "fixed_details",
        "Testing Suggestions": "testing_suggestions",
        "Affected Release": "affected_release",
        "Extra Impact": "extra_impact",
    }
    section_payload = {}
    for idx, label in enumerate(section_order):
        section_text = _extract_section(label, section_order[idx + 1:])
        if section_text:
            section_payload[section_key_map[label]] = section_text
            # 分段提取优先，避免单行正则截断多行语义
            fields[section_key_map[label]] = section_text

    # 提取受影响的源文件
    src_files = set(re.findall(r"Modified:\s*\S+/([\w/]+\.c)", text))
    if src_files:
        fields["affected_files"] = sorted(src_files)

    # 提取 SVN revision 信息
    revisions = re.findall(r"New\s+Revision:\s*(\d+)", text)
    if revisions:
        fields["fix_revisions"] = revisions

    # 保留原始文本以备 LLM 参考
    fields["raw_text"] = text
    if section_payload:
        fields["raw_sections"] = section_payload

    return fields


def _load_sheet_cases(excel_book: pd.ExcelFile, sheet_name: str) -> list[dict]:
    """加载单个 Sheet 的测试用例，返回结构化列表。"""
    df = excel_book.parse(sheet_name)

    # 检查必需列
    missing = [c for c in ["Description"] if c not in df.columns]
    if missing:
        logger.warning("Sheet '%s' 缺少必需列 %s，跳过", sheet_name, missing)
        return []

    df = df.dropna(subset=["Description"]).reset_index(drop=True)

    # 向下填充 Item / Sub Item
    if "Item" in df.columns:
        df["Item"] = df["Item"].ffill()
    if "Sub Item" in df.columns:
        df["Sub Item"] = df["Sub Item"].ffill()
        # 过滤掉 Sub Item 仍为 NaN 的分组标题行
        df = df.dropna(subset=["Sub Item"]).reset_index(drop=True)

    cases = []
    for idx, row in df.iterrows():
        case = {"seq": idx + 1}
        for col in _CASE_COLUMNS:
            if col in df.columns:
                case[col.lower().replace(" ", "_")] = _clean(row.get(col))

        # 包含执行结果（如果有）
        has_results = False
        result_data = {}
        for col in _RESULT_COLUMNS:
            val = _clean(row.get(col)) if col in df.columns else ""
            if val:
                has_results = True
                result_data[col.lower().replace(" ", "_")] = val
        if has_results:
            case["execution_results"] = result_data

        cases.append(case)

    return cases


def _group_cases_by_module(cases: list[dict]) -> list[dict]:
    """按 item / sub_item 分组，返回模块列表。"""
    modules = []
    seen = {}
    for case in cases:
        key = (case.get("item", ""), case.get("sub_item", ""))
        if key not in seen:
            seen[key] = len(modules)
            modules.append({
                "item": key[0],
                "sub_item": key[1],
                "cases": [],
            })
        modules[seen[key]]["cases"].append(case)

    # 添加统计信息
    for mod in modules:
        mod_cases = mod["cases"]
        mod["case_count"] = len(mod_cases)
        # 统计测试类型分布
        types = {}
        prios = {}
        for c in mod_cases:
            t = c.get("test_types", "")
            p = c.get("priority", "")
            if t:
                types[t] = types.get(t, 0) + 1
            if p:
                prios[p] = prios.get(p, 0) + 1
        mod["type_distribution"] = types
        mod["priority_distribution"] = prios

    return modules


def _compute_sheet_summary(modules: list[dict], sheet_name: str) -> dict:
    """计算单个 Sheet 的统计摘要。"""
    total = sum(m["case_count"] for m in modules)
    all_types = {}
    all_prios = {}
    for m in modules:
        for t, c in m["type_distribution"].items():
            all_types[t] = all_types.get(t, 0) + c
        for p, c in m["priority_distribution"].items():
            all_prios[p] = all_prios.get(p, 0) + c

    has_results = any(
        "execution_results" in case
        for m in modules
        for case in m["cases"]
    )

    return {
        "sheet_name": sheet_name,
        "total_cases": total,
        "module_count": len(modules),
        "has_execution_results": has_results,
        "type_distribution": all_types,
        "priority_distribution": all_prios,
    }


def prepare_bug_to_case(bug_dir: Path) -> dict:
    """主函数：将 Bug 目录下的 Excel + Bug 修复信息转换为结构化 JSON。"""

    # 1. 查找 Excel 文件
    excel_files = list(bug_dir.glob("*.xlsx")) + list(bug_dir.glob("*.xls"))
    if not excel_files:
        raise FileNotFoundError(f"在 {bug_dir} 下未找到 Excel 文件")
    excel_path = excel_files[0]
    logger.info("Excel 文件: %s", excel_path.name)

    # 2. 查找并解析 Bug 修复信息
    bug_fix = {}
    txt_files = list(bug_dir.glob("*fix*detail*")) + list(bug_dir.glob("*bug*detail*"))
    # 不区分大小写匹配
    if not txt_files:
        txt_files = [f for f in bug_dir.glob("*.txt")
                     if "fix" in f.name.lower() or "bug" in f.name.lower()]
    if txt_files:
        bug_fix = _parse_bug_fix_detail(txt_files[0])
        logger.info("Bug 修复信息: %s", txt_files[0].name)

    # 也检查是否已有 bug_profile.json
    json_profile = bug_dir / "bug_profile.json"
    if json_profile.exists():
        with open(json_profile, "r", encoding="utf-8") as f:
            manual_profile = json.load(f)
        # 合并（手动 profile 优先覆盖自动解析的值）
        for k, v in manual_profile.items():
            normalized_key = k.lower().replace(" ", "_")
            if v:  # 手动值非空时始终覆盖
                bug_fix[normalized_key] = v
        logger.info("已合并手动 Bug Profile: bug_profile.json")

    # 3. 检测有效 Sheet
    xl = pd.ExcelFile(excel_path)
    data_sheets = [s for s in xl.sheet_names
                   if s.strip().lower() not in _SKIP_SHEETS]
    logger.info("数据 Sheet: %s", data_sheets)

    # 4. 加载各 Sheet 数据
    sheets_data = []
    for sheet_name in data_sheets:
        cases = _load_sheet_cases(xl, sheet_name)
        if not cases:
            continue
        modules = _group_cases_by_module(cases)
        summary = _compute_sheet_summary(modules, sheet_name)

        sheets_data.append({
            "summary": summary,
            "modules": modules,
        })
        logger.info("  %s: %d 条用例, %d 个模块",
                     sheet_name, summary["total_cases"], summary["module_count"])

    # 5. 构建 bug_to_case 分析上下文
    bug_context = _build_bug_context(bug_fix, sheets_data)

    # 6. 组装最终 JSON
    result = {
        "meta": {
            "type": "bug_to_case",
            "source_dir": str(bug_dir.name),
            "excel_file": excel_path.name,
            "description": "Bug 修复驱动的测试用例补充评审。"
                           "目标：验证 bug 修复用例的覆盖度和质量，"
                           "并评估是否需要补充到现有用例库。",
        },
        "bug_info": _build_bug_info_section(bug_fix),
        "bug_context": bug_context,
        "sheets": sheets_data,
    }

    return result


def _build_bug_info_section(bug_fix: dict) -> dict:
    """构建 bug 信息段落（不含 raw_text 以减小体积）。"""
    info = {}
    key_fields = [
        "bug_id", "title", "root_cause", "condition",
        "fixed_details", "testing_suggestions",
        "affected_release", "affected_files", "fix_revisions",
        "extra_impact",
    ]
    for k in key_fields:
        if k in bug_fix and bug_fix[k]:
            info[k] = bug_fix[k]
    return info


def _build_bug_context(bug_fix: dict, sheets_data: list[dict]) -> dict:
    """构建 bug-to-case 特有的分析上下文，帮助评审流程聚焦。"""
    context = {
        "review_focus": (
            "本次评审为 Bug-to-Case 类型：基于已知 Bug 的修复细节，"
            "评估新编写测试用例的覆盖度和质量。"
        ),
        "key_questions": _generate_key_questions_from_bug(bug_fix),
    }

    # 识别跨 sheet 差异
    sheet_names = [s["summary"]["sheet_name"] for s in sheets_data]
    sheets_with_results = [s["summary"]["sheet_name"] for s in sheets_data
                           if s["summary"]["has_execution_results"]]
    sheets_without_results = [s["summary"]["sheet_name"] for s in sheets_data
                              if not s["summary"]["has_execution_results"]]

    if sheets_with_results and sheets_without_results:
        context["sheet_roles"] = {
            "reference_sheets": {
                "names": sheets_with_results,
                "role": "已有用例库（含历史执行结果），可作为参考基线",
            },
            "new_case_sheets": {
                "names": sheets_without_results,
                "role": "新增 Bug-to-Case 用例，需重点评审覆盖度和质量",
            },
        }

    raw_bug_detail = str(bug_fix.get("raw_text") or "").strip()
    if raw_bug_detail:
        context["bug_detail_raw"] = raw_bug_detail
    raw_sections = bug_fix.get("raw_sections")
    if isinstance(raw_sections, dict) and raw_sections:
        context["bug_detail_sections"] = raw_sections

    return context


# ── 通用回退模板（去除了 ECN+CWR 等与特定 bug 无关的内容） ──
_FALLBACK_KEY_QUESTIONS = [
    "新增用例是否覆盖了 Bug 的根因场景（Root Cause）？",
    "新增用例是否覆盖了修复建议（Testing Suggestions）中提到的所有验证点？",
    "用例的 Expected Result 描述是否准确反映了修复后的正确行为？",
    "是否有遗漏的负面测试（修复前应该失败的场景）？",
]


def _generate_key_questions_from_bug(bug_fix: dict) -> list[str]:
    """基于 bug 的真实字段，用 LLM 生成 3-5 条有针对性的 key_questions。

    失败时回退到通用模板。
    """
    title = str(bug_fix.get("title") or "").strip()
    root_cause = str(bug_fix.get("root_cause") or "").strip()
    fixed_details = str(bug_fix.get("fixed_details") or "").strip()
    testing_suggestions = str(bug_fix.get("testing_suggestions") or "").strip()

    # 如果 bug 信息太少，直接回退
    if not (title and (root_cause or fixed_details)):
        logger.info("[key_questions] bug 信息不足，使用通用模板")
        return list(_FALLBACK_KEY_QUESTIONS)

    try:
        from INAGENT.utils.env_utils import load_inagent_env
        from INAGENT.config.project_config import cfg_str
        from camel.models import ModelFactory
        from camel.types import ModelPlatformType
        from camel.agents import ChatAgent
        from camel.messages import BaseMessage

        load_inagent_env()
        gateway_url = cfg_str("llm.gateway.base_url", "", env="LLM_GATEWAY_BASE_URL")
        if not gateway_url:
            logger.info("[key_questions] 无 LLM 网关配置，使用通用模板")
            return list(_FALLBACK_KEY_QUESTIONS)

        api_key = cfg_str("llm.gateway.api_key", "", env="LLM_GATEWAY_API_KEY") or "local-gateway"
        if not gateway_url.rstrip("/").endswith("/v1"):
            gateway_url = f"{gateway_url.rstrip('/')}/v1"
        model_name = cfg_str(
            "llm.siliconflow.model_type",
            "Qwen/Qwen2.5-72B-Instruct",
            env="SILICONFLOW_MODEL_TYPE",
        )
        model = ModelFactory.create(
            model_platform=ModelPlatformType.SILICONFLOW,
            model_type=model_name,
            api_key=api_key,
            url=gateway_url,
            model_config_dict={"temperature": 0.0},
        )

        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="KeyQuestionGenerator",
                content=(
                    "你是网络安全产品测试专家。根据 Bug 修复信息，生成 3-5 条"
                    "针对性强的测试评审问题。\n"
                    "要求：\n"
                    "1. 每条问题必须与该 Bug 的具体修复内容直接相关\n"
                    "2. 涵盖：根因验证、修复范围边界、配置可恢复性、"
                    "已有功能与新功能的隔离\n"
                    "3. 禁止输出与该 Bug 无关的通用测试建议\n"
                    "4. 仅输出 JSON 数组格式: [\"问题1\", \"问题2\", ...]"
                ),
            ),
            model=model,
        )

        prompt = (
            f"Bug Title: {title}\n"
            f"Root Cause: {root_cause}\n"
            f"Fixed Details: {fixed_details}\n"
            f"Testing Suggestions: {testing_suggestions}\n\n"
            f"请生成 3-5 条针对此 Bug 修复的测试评审问题（JSON 数组）。"
        )
        response = agent.step(prompt)
        raw = (response.msg.content or "").strip()

        # 解析 JSON 数组
        import json as _json
        # 尝试从响应中提取 JSON 数组
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            questions = _json.loads(match.group(0))
            if isinstance(questions, list) and len(questions) >= 2:
                result = [str(q).strip() for q in questions if str(q).strip()]
                if len(result) >= 2:
                    logger.info(
                        "[key_questions] LLM 生成 %d 条针对性问题", len(result)
                    )
                    return result

        logger.warning("[key_questions] LLM 输出解析失败，使用通用模板")
        return list(_FALLBACK_KEY_QUESTIONS)

    except Exception as e:
        logger.warning("[key_questions] LLM 调用失败: %s，使用通用模板", e)
        return list(_FALLBACK_KEY_QUESTIONS)


def main():
    if len(sys.argv) < 2:
        print("用法: python prepare_bug_to_case.py <bug_dir>")
        print("示例: python prepare_bug_to_case.py INAGENT/jobs/test_review/Bug\\ 139213")
        sys.exit(1)

    bug_dir = Path(sys.argv[1])
    if not bug_dir.is_absolute():
        bug_dir = Path.cwd() / bug_dir
    if not bug_dir.exists():
        logger.error("目录不存在: %s", bug_dir)
        sys.exit(1)

    result = prepare_bug_to_case(bug_dir)

    output_path = bug_dir / "bug_to_case.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("输出: %s", output_path)
    total_cases = sum(s["summary"]["total_cases"] for s in result["sheets"])
    logger.info("共 %d 个 Sheet, %d 条用例", len(result["sheets"]), total_cases)


if __name__ == "__main__":
    main()
