"""
按模块评审测试用例 — 使用 INAGENT 的 test_review 工作流

按 Item / Sub Item 分组，每个模块的全部用例作为整体一起评审，
使 LLM 能从模块角度评估覆盖度、结构完整性和质量。
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repo root
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from INAGENT.utils import env_utils
env_utils.load_inagent_env()

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)
        ),
        logging.FileHandler(ROOT / "INAGENT" / "knowledge_base" / "logs" / "test_review.log", mode="w", encoding="utf-8"),
    ],
)
logger = logging.getLogger("test_review")

# ── 默认值（可通过 CLI 参数覆盖）────────────────────────────
DEFAULT_INPUT_DIR = ROOT / "INAGENT" / "jobs" / "test_review"
DEFAULT_OUTPUT_DIR = ROOT / "INAGENT" / "review_results"

# 单个模块超过此条数时，分成子批（避免 token 溢出）
MAX_MODULE_BATCH = 50


# ════════════════════════════════════════════════════════════
#  数据加载与格式化
# ════════════════════════════════════════════════════════════

def load_test_cases(excel_path: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    df = df.dropna(subset=["Description"]).reset_index(drop=True)
    df["Item"] = df["Item"].ffill()
    df["Sub Item"] = df["Sub Item"].ffill()
    return df


def get_module_groups(df: pd.DataFrame) -> list:
    """按 Item / Sub Item 分组，返回 [(item, sub_item, sub_df), ...]，保持原始顺序。"""
    groups = []
    seen = set()
    for _, row in df.iterrows():
        key = (row["Item"], row["Sub Item"])
        if key not in seen:
            seen.add(key)
            mask = (df["Item"] == key[0]) & (df["Sub Item"] == key[1])
            groups.append((key[0], key[1], df[mask].copy()))
    return groups


def format_module_table(sub_df: pd.DataFrame, global_offset: int = 0) -> str:
    """将模块内的测试用例格式化为完整 Markdown 表格。"""
    lines = [
        "| # | ID | Test Type | Priority | Description | Expected Result | Note |",
        "|---|-----|-----------|----------|-------------|-----------------|------|",
    ]
    for i, (_, row) in enumerate(sub_df.iterrows()):
        def _c(v):
            return str(v).replace("|", "/").replace("\n", " ").strip() if pd.notna(v) else ""
        lines.append(
            f"| {global_offset + i + 1} | {_c(row.get('ID',''))} "
            f"| {_c(row.get('Test Types',''))} | {_c(row.get('Priority',''))} "
            f"| {_c(row.get('Description',''))} | {_c(row.get('Expected Result',''))} "
            f"| {_c(row.get('Note',''))} |"
        )
    return "\n".join(lines)


def build_module_stats(sub_df: pd.DataFrame) -> str:
    """生成模块内部的统计摘要。"""
    n = len(sub_df)
    types = sub_df["Test Types"].value_counts().to_dict()
    prios = sub_df["Priority"].value_counts().to_dict()
    type_str = ", ".join(f"{t}: {c}" for t, c in types.items())
    prio_str = ", ".join(f"{p}: {c}" for p, c in prios.items())
    bugs = sub_df[sub_df["Note"].astype(str).str.contains("Bug|bug", na=False)]
    bug_str = f"，含 {len(bugs)} 条 Bug 关联" if len(bugs) > 0 else ""
    return f"共 {n} 条用例{bug_str}\n- 测试类型: {type_str}\n- 优先级: {prio_str}"


# ════════════════════════════════════════════════════════════
#  Agent 调用
# ════════════════════════════════════════════════════════════

def call_agent(test_cases_text: str, label: str, bug_profile: dict | None = None):
    """调用自主评审 Pipeline。"""
    from INAGENT.review import ReviewPipeline
    from INAGENT.web.deps import get_llm_model, get_knowledge_router

    product_name = env_utils.get_product_name()
    router = get_knowledge_router()
    model = get_llm_model()
    pipeline = ReviewPipeline(router=router, model=model, product_name=product_name)

    logger.info("[%s] review pipeline start...", label)
    result = pipeline.run(test_cases_text=test_cases_text, bug_profile=bug_profile)
    logger.info(
        "[%s] pipeline done (%.1fs, review chars=%d)",
        label,
        result.elapsed_seconds,
        len(result.review),
    )
    return result


# ════════════════════════════════════════════════════════════
#  按模块评审
# ════════════════════════════════════════════════════════════

def review_module(item: str, sub_item: str, sub_df: pd.DataFrame,
                  sheet_name: str, module_idx: int, total_modules: int,
                  global_offset: int, bug_profile: dict | None = None) -> str:
    """评审一个模块（Item/SubItem）的全部用例。
    如果模块过大（>MAX_MODULE_BATCH），拆分为子批但仍在同一模块上下文中评审。
    """
    n = len(sub_df)
    label = f"{sheet_name} [{module_idx}/{total_modules}] {item}/{sub_item}"
    stats = build_module_stats(sub_df)

    if n <= MAX_MODULE_BATCH:
        table = format_module_table(sub_df, global_offset)
        test_cases_text = (
            f"[模块评审 — {sheet_name} / {item} / {sub_item}]\n\n"
            f"**模块**: {item} > {sub_item}\n"
            f"**统计**: {stats}\n\n"
            f"{table}\n\n"
            f"请对该模块的全部 {n} 条测试用例进行整体评审。"
            f"重点评估该模块的覆盖度是否完整，然后逐条检查质量问题。"
        )
        try:
            result = call_agent(test_cases_text, label, bug_profile=bug_profile)
            review_plan_json = json.dumps(result.plan.to_dict(), ensure_ascii=False, indent=2)
            return (
                "### Step 1 ReviewPlan JSON\n\n"
                "```json\n"
                f"{review_plan_json}\n"
                "```\n\n"
                "### Step 1.5 知识上下文\n\n"
                f"{result.knowledge or '(无额外知识上下文)'}\n\n"
                "### Step 2 评审结果\n\n"
                f"{result.review}"
            )
        except Exception as e:
            logger.error("[%s] 评审失败: %s", label, e)
            return f"**评审失败**: {e}"
    else:
        results = []
        num_sub = (n + MAX_MODULE_BATCH - 1) // MAX_MODULE_BATCH
        for si in range(num_sub):
            start = si * MAX_MODULE_BATCH
            end = min(start + MAX_MODULE_BATCH, n)
            batch_df = sub_df.iloc[start:end]
            table = format_module_table(batch_df, global_offset + start)
            sub_label = f"{label} part{si+1}/{num_sub}"
            test_cases_text = (
                f"[模块评审 — {sheet_name} / {item} / {sub_item} (第{si+1}/{num_sub}部分)]\n\n"
                f"**模块**: {item} > {sub_item} (共{n}条, 本批第{start+1}-{end}条)\n"
                f"**模块整体统计**: {stats}\n\n"
                f"{table}\n\n"
                f"请评审该模块的第 {start+1}-{end} 条用例。"
                f"从模块整体覆盖度角度评估，并逐条检查质量。"
            )
            try:
                result = call_agent(test_cases_text, sub_label, bug_profile=bug_profile)
                review_plan_json = json.dumps(result.plan.to_dict(), ensure_ascii=False, indent=2)
                answer = (
                    "#### Step 1 ReviewPlan JSON\n\n"
                    "```json\n"
                    f"{review_plan_json}\n"
                    "```\n\n"
                    "#### Step 1.5 知识上下文\n\n"
                    f"{result.knowledge or '(无额外知识上下文)'}\n\n"
                    "#### Step 2 评审结果\n\n"
                    f"{result.review}"
                )
            except Exception as e:
                logger.error("[%s] 评审失败: %s", sub_label, e)
                answer = f"**评审失败**: {e}"
            results.append(f"### 第 {start+1}-{end} 条\n\n{answer}")
        return "\n\n---\n\n".join(results)


# ════════════════════════════════════════════════════════════
#  主流程
# ════════════════════════════════════════════════════════════

def _bug_profile_header(bp: dict) -> str:
    """Generate a Markdown header block describing the bug under review."""
    lines = ["## Bug 定向评审\n"]
    for k, v in bp.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    return "\n".join(lines)


def _discover_excel_files(input_dir: Path) -> list[Path]:
    """在指定目录中查找所有 Excel 文件。"""
    patterns = ["*.xlsx", "*.xls"]
    files = []
    for p in patterns:
        files.extend(input_dir.glob(p))
    return sorted(set(files))


def _detect_data_sheets(excel_path: Path) -> list[str]:
    """自动检测 Excel 文件中的有效数据 Sheet（排除 List Sample 等模板 Sheet）。"""
    xl = pd.ExcelFile(excel_path)
    skip = {"list sample", "sample", "template", "说明", "readme"}
    sheets = [s for s in xl.sheet_names if s.strip().lower() not in skip]
    return sheets


def review_single_file(
    excel_path: Path,
    output_dir: Path,
    data_sheets: list[str] | None = None,
    bug_profile: dict | None = None,
):
    """评审单个 Excel 文件中的全部 Sheet。"""
    logger.info("=" * 60)
    logger.info("按模块评审测试用例 — 文件: %s", excel_path.name)

    if data_sheets is None:
        data_sheets = _detect_data_sheets(excel_path)
    logger.info("Sheet: %s", data_sheets)
    if bug_profile:
        logger.info("Bug 定向评审: Bug#%s", bug_profile.get("Bug ID", "?"))
    logger.info("=" * 60)

    all_stats = {}
    grand_total = 0
    for sheet_name in data_sheets:
        df = load_test_cases(excel_path, sheet_name)
        modules = get_module_groups(df)
        n = len(df)
        grand_total += n
        all_stats[sheet_name] = {"total": n, "modules": len(modules)}
        logger.info("  %s: %d 条用例, %d 个模块", sheet_name, n, len(modules))
    logger.info("合计: %d 条用例", grand_total)

    for sheet_name in data_sheets:
        df = load_test_cases(excel_path, sheet_name)
        modules = get_module_groups(df)
        total = len(df)
        num_modules = len(modules)

        logger.info("\n" + "=" * 60)
        logger.info("[%s] 开始评审: %d 条用例, %d 个模块", sheet_name, total, num_modules)
        logger.info("=" * 60)

        sheet_file = output_dir / f"review_{sheet_name.replace(' ', '_')}.md"
        with open(sheet_file, "w", encoding="utf-8") as f:
            f.write(f"# 测试用例评审: {sheet_name}\n\n")
            f.write(f"- 文件: {excel_path.name}\n")
            f.write(f"- 用例总数: {total}\n")
            f.write(f"- 模块数: {num_modules}\n")
            f.write(f"- 评审模式: 按模块（Item/Sub Item）整体评审\n")
            if bug_profile:
                f.write(f"- 关联 Bug: #{bug_profile.get('Bug ID', '?')}\n")
            f.write("\n")
            if bug_profile:
                f.write(_bug_profile_header(bug_profile))
                f.write("\n")
            f.write("## 模块索引\n\n")
            f.write("| # | Item | Sub Item | 用例数 |\n")
            f.write("|---|------|----------|--------|\n")
            for mi, (item, sub, sdf) in enumerate(modules):
                f.write(f"| {mi+1} | {item} | {sub} | {len(sdf)} |\n")
            f.write("\n")

        global_offset = 0
        for mi, (item, sub_item, sub_df) in enumerate(modules):
            n = len(sub_df)
            logger.info("-" * 40)
            logger.info("[%s] 模块 %d/%d: %s / %s (%d 条)",
                        sheet_name, mi + 1, num_modules, item, sub_item, n)

            answer = review_module(
                item, sub_item, sub_df,
                sheet_name, mi + 1, num_modules, global_offset,
                bug_profile=bug_profile,
            )

            with open(sheet_file, "a", encoding="utf-8") as f:
                f.write(f"---\n\n")
                f.write(f"## 模块 {mi+1}/{num_modules}: {item} > {sub_item} ({n} 条)\n\n")
                table = format_module_table(sub_df, global_offset)
                f.write(f"### 用例列表\n\n{table}\n\n")
                f.write(f"### 评审结果\n\n{answer}\n\n")

            global_offset += n

        logger.info("[%s] 全部 %d 个模块评审完成: %s", sheet_name, num_modules, sheet_file)

    summary_file = output_dir / "_summary.md"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write("# 测试用例评审汇总\n\n")
        f.write(f"- 文件: {excel_path.name}\n")
        f.write(f"- 评审时间: {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"- 评审模式: 按模块（Item/Sub Item）整体评审\n")
        if bug_profile:
            f.write(f"- 关联 Bug: #{bug_profile.get('Bug ID', '?')}\n")
        f.write("\n")
        f.write("| Sheet | 用例数 | 模块数 | 结果文件 |\n")
        f.write("|-------|--------|--------|----------|\n")
        for sn in data_sheets:
            st = all_stats[sn]
            fname = f"review_{sn.replace(' ', '_')}.md"
            f.write(f"| {sn} | {st['total']} | {st['modules']} | {fname} |\n")
        f.write(f"\n**合计: {grand_total} 条用例**\n")

    logger.info("=" * 60)
    logger.info("全部评审完成! 汇总: %s", summary_file)
    logger.info("  共评审 %d 条用例", grand_total)
    logger.info("  结果目录: %s", output_dir)
    logger.info("=" * 60)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="测试用例评审 — 按模块整体评审")
    parser.add_argument(
        "--input-dir", type=Path, default=DEFAULT_INPUT_DIR,
        help="输入目录，扫描其中的 .xlsx/.xls 文件 (默认: INAGENT/jobs/test_review/)",
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help="直接指定单个 Excel 文件路径（优先于 --input-dir）",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="评审结果输出目录 (默认: INAGENT/review_results/)",
    )
    parser.add_argument(
        "--sheets", nargs="*", default=None,
        help="指定要评审的 Sheet 名称（默认自动检测）",
    )
    parser.add_argument(
        "--bug-profile", type=Path, default=None,
        help="Bug profile JSON 文件路径（可选，提供时做定向评审）",
    )
    args = parser.parse_args()

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载 bug profile
    bug_profile = None
    if args.bug_profile:
        with open(args.bug_profile, "r", encoding="utf-8") as f:
            bug_profile = json.load(f)
        logger.info("已加载 Bug Profile: %s", args.bug_profile.name)

    # 确定要评审的 Excel 文件列表
    if args.file:
        excel_files = [args.file]
    else:
        input_dir = args.input_dir
        if not input_dir.exists():
            logger.error("输入目录不存在: %s", input_dir)
            sys.exit(1)
        excel_files = _discover_excel_files(input_dir)
        if not excel_files:
            logger.warning("输入目录下没有找到 Excel 文件: %s", input_dir)
            logger.info("请将 .xlsx/.xls 文件放入: %s", input_dir)
            sys.exit(0)

    logger.info("找到 %d 个 Excel 文件待评审", len(excel_files))

    for excel_path in excel_files:
        if not excel_path.exists():
            logger.error("文件不存在: %s", excel_path)
            continue
        # 每个文件独立输出子目录（多文件时区分）
        if len(excel_files) > 1:
            file_output_dir = output_dir / excel_path.stem
            file_output_dir.mkdir(parents=True, exist_ok=True)
        else:
            file_output_dir = output_dir

        review_single_file(
            excel_path=excel_path,
            output_dir=file_output_dir,
            data_sheets=args.sheets,
            bug_profile=bug_profile,
        )


if __name__ == "__main__":
    main()
