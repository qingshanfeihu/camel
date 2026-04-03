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

import re
import pandas as pd


# ── emoji 清理与摘要提取 ────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\u2705\u274c\u274e\u2757\u2753\u2754\u2755"  # ✅❌❎❗❓❔❕
    "\u26a0\ufe0f?"  # ⚠️
    "\u2b50"  # ⭐
    "\U0001F534\U0001F7E0\U0001F7E1\U0001F7E2\U0001F7E3\U0001F7E4\U0001F535"  # 🔴🟠🟡🟢🟣🟤🔵
    "\U0001F6A8\U0001F4A1\U0001F4CC\U0001F4CB\U0001F4DD"  # 🚨💡📌📋📝
    "\U0001F44D\U0001F44E\U0001F44F"  # 👍👎👏
    "\u2714\ufe0f?\u2716\ufe0f?\u2611\ufe0f?\u2612\ufe0f?"  # ✔✖☑☒ with optional VS16
    "\u2139\ufe0f?"  # ℹ️
    "\U0001F525\U0001F4AF"  # 🔥💯
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Remove emoji characters from text."""
    return _EMOJI_RE.sub("", text)


def _case_range_overlaps(text: str, mod_start: int, mod_end: int) -> bool | None:
    """Check if any case-number range in *text* overlaps [mod_start, mod_end].

    Returns ``True``/``False`` when case numbers are found, ``None`` otherwise.
    """
    found = False
    for m in re.finditer(r'#(\d+)(?:\s*[-–—]\s*#?(\d+))?', text):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        found = True
        if start <= mod_end and end >= mod_start:
            return True
    return False if found else None


def _redistribute_batch_summary(
    result: dict[int, str],
    batch_indices: list[int],
    modules: list,
    global_offsets: list[int],
) -> None:
    """当合批综合评审只在最后一个模块留下 [问题摘要] 时，
    按用例编号范围将条目重新分配到各模块（原地修改 *result*）。
    """
    summary_pat = re.compile(r"\[问题摘要\]\s*\n((?:- .+\n?)+)")

    # 检查哪些模块已经有 [问题摘要]
    with_summary = [idx for idx in batch_indices if summary_pat.search(result.get(idx, ""))]

    # 全部 / 0 个模块有 → 无需处理；非最后一个模块独有 → 也跳过
    if len(with_summary) != 1 or with_summary[0] != batch_indices[-1]:
        return

    last_idx = batch_indices[-1]
    m = summary_pat.search(result[last_idx])
    if not m:
        return
    batch_summary = m.group(1).strip()

    # 判断该 [问题摘要] 是否仅覆盖最后模块自身的范围
    last_off = global_offsets[last_idx]
    last_cnt = len(modules[last_idx][2])
    all_nums = [int(n) for n in re.findall(r'#(\d+)', batch_summary)]
    if all_nums and all(last_off + 1 <= n <= last_off + last_cnt for n in all_nums):
        return  # 已是单模块摘要，无需重分配

    # 逐模块过滤并追加
    for idx in batch_indices:
        if idx == last_idx:
            continue
        off = global_offsets[idx]
        cnt = len(modules[idx][2])
        mod_start, mod_end = off + 1, off + cnt

        filtered = []
        for line in batch_summary.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            overlap = _case_range_overlaps(line, mod_start, mod_end)
            if overlap is True or overlap is None:
                # 有交集 或 无用例编号（通用条目）→ 纳入
                filtered.append(line)
        if filtered:
            result[idx] += "\n\n[问题摘要]\n" + "\n".join(filtered)


def _split_batch_answer(full_answer: str, batch_indices: list[int],
                        modules: list,
                        global_offsets: list[int] | None = None) -> dict[int, str]:
    """将合并的多模块评审结果按 '## 模块 X:' 标题拆分回各模块。"""
    # Locate Step 2 section where per-module results live
    step2_marker = "### Step 2 评审结果"
    header_pos = full_answer.find(step2_marker)
    if header_pos < 0:
        # No Step 2 found — assign full answer to each module as fallback
        return {idx: full_answer for idx in batch_indices}

    prefix = full_answer[:header_pos + len(step2_marker)]
    review_body = full_answer[header_pos + len(step2_marker):]

    # Build per-module segments by splitting on module headers in the review body
    # LLM output format varies: "## 模块 16: ..." or "--- Task ... Result ---"
    # We split on any '## 模块 <num>' pattern
    segment_pattern = re.compile(r'(?=\n##\s+模块\s+\d+)')
    segments = segment_pattern.split(review_body)

    result: dict[int, str] = {}
    for idx in batch_indices:
        module_num = idx + 1
        # Find segment matching this module number
        target = None
        for seg in segments:
            if re.search(rf'##\s+模块\s+{module_num}\b', seg):
                if target is None:
                    target = seg.strip()
                else:
                    # Append additional sections for same module
                    target += "\n\n" + seg.strip()
        if target:
            result[idx] = f"{prefix}\n\n{target}"
        else:
            # Fallback: assign full answer if this module not found
            result[idx] = full_answer

    # Redistribute batch-level [问题摘要] to individual modules
    if global_offsets and len(batch_indices) > 1:
        _redistribute_batch_summary(result, batch_indices, modules, global_offsets)

    return result


def _extract_issue_summary(review_text: str) -> str | None:
    """Extract the [问题摘要] block from review text."""
    m = re.search(r"\[\u95ee\u9898\u6458\u8981\]\s*\n((?:- .+\n?)+)", review_text)
    if m:
        return m.group(1).strip()
    # fallback: try to find the section by header line
    m = re.search(r"\*{0,2}\u95ee\u9898\u6458\u8981\*{0,2}[\s:]*\n((?:[\-\*] .+\n?)+)", review_text)
    if m:
        return m.group(1).strip()
    return None

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

# 多模块合并批次: 将相邻小模块合并为一次 LLM 调用，上限为此条数
# qwen-plus 131K 上下文可容纳约 200 条用例的表格 + 知识上下文
MAX_BATCH_CASES = 150


# ════════════════════════════════════════════════════════════
#  数据加载与格式化
# ════════════════════════════════════════════════════════════

def load_test_cases(excel_path: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    if "Description" not in df.columns:
        logger.warning("Sheet '%s' 缺少 Description 列，跳过", sheet_name)
        return pd.DataFrame()
    # Clean Excel carriage-return artifacts
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].str.replace('_x000d_', ' ', regex=False)
            df[col] = df[col].str.replace('\r\n', ' ', regex=False)
            df[col] = df[col].str.replace('\r', ' ', regex=False)
    df = df.dropna(subset=["Description"]).reset_index(drop=True)
    if "Item" in df.columns:
        df["Item"] = df["Item"].ffill()
    if "Sub Item" in df.columns:
        df["Sub Item"] = df["Sub Item"].ffill()
        # 过滤掉 Sub Item 仍为 NaN 的分组标题行（Excel 中 Item 行无 Sub Item）
        df = df.dropna(subset=["Sub Item"]).reset_index(drop=True)
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

def _create_pipeline():
    """Create a ReviewPipeline instance (reusable across modules in same sheet)."""
    from INAGENT.review import ReviewPipeline
    from INAGENT.web.deps import get_llm_model, get_knowledge_router

    product_name = env_utils.get_product_name()
    router = get_knowledge_router()
    model = get_llm_model()
    return ReviewPipeline(router=router, model=model, product_name=product_name)


def call_agent(test_cases_text: str, label: str, bug_profile: dict | None = None,
               pipeline=None):
    """调用自主评审 Pipeline。"""
    if pipeline is None:
        pipeline = _create_pipeline()

    logger.info("[%s] review pipeline start...", label)
    result = pipeline.run(test_cases_text=test_cases_text, bug_profile=bug_profile)
    logger.info(
        "[%s] pipeline done (%.1fs, review chars=%d)",
        label,
        result.elapsed_seconds,
        len(result.review),
    )
    return result


def _append_debug_jsonl(debug_file: Path, payload: dict) -> None:
    debug_file.parent.mkdir(parents=True, exist_ok=True)
    with open(debug_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ════════════════════════════════════════════════════════════
#  多模块合并批次
# ════════════════════════════════════════════════════════════

def _group_modules_into_batches(modules: list, max_cases: int = MAX_BATCH_CASES) -> list[list[int]]:
    """将相邻小模块合并为批次，每批的总用例数不超过 max_cases。

    超过 max_cases 的单个大模块独占一个批次。
    返回: [[module_index, ...], ...] 每个子列表是一个批次。
    """
    batches: list[list[int]] = []
    current_batch: list[int] = []
    current_count = 0

    for idx, (_item, _sub, sub_df) in enumerate(modules):
        n = len(sub_df)
        if current_count + n > max_cases and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_count = 0
        current_batch.append(idx)
        current_count += n

    if current_batch:
        batches.append(current_batch)

    return batches


def review_module_batch(
    modules: list,
    batch_indices: list[int],
    sheet_name: str,
    global_offsets: list[int],
    total_modules: int,
    bug_profile: dict | None = None,
    pipeline=None,
    debug_file: Path | None = None,
) -> dict[int, str]:
    """评审一个批次中的多个模块（一次 LLM 调用）。

    Returns: {module_index: review_answer_text}
    """
    if len(batch_indices) == 1:
        # 单模块批次，直接用原始 review_module
        idx = batch_indices[0]
        item, sub_item, sub_df = modules[idx]
        answer = review_module(
            item, sub_item, sub_df,
            sheet_name, idx + 1, total_modules, global_offsets[idx],
            bug_profile=bug_profile, pipeline=pipeline, debug_file=debug_file,
        )
        return {idx: answer}

    # 多模块合并为一次调用
    parts: list[str] = []
    total_cases = 0
    for idx in batch_indices:
        item, sub_item, sub_df = modules[idx]
        n = len(sub_df)
        total_cases += n
        offset = global_offsets[idx]
        table = format_module_table(sub_df, offset)
        stats = build_module_stats(sub_df)
        parts.append(
            f"### 模块 {idx+1}: {item} > {sub_item}\n"
            f"**统计**: {stats}\n\n"
            f"{table}"
        )

    module_range = f"{batch_indices[0]+1}-{batch_indices[-1]+1}"
    label = f"{sheet_name} [模块{module_range}/{total_modules}] ({total_cases}条)"

    test_cases_text = (
        f"[多模块批量评审 — {sheet_name}]\n\n"
        f"**Sheet**: {sheet_name}\n"
        f"**模块范围**: 模块 {module_range} (共 {len(batch_indices)} 个模块, {total_cases} 条用例)\n\n"
        + "\n\n---\n\n".join(parts) + "\n\n"
        f"请对以上 {len(batch_indices)} 个模块分别进行评审。\n"
        f"对每个模块，请用 '## 模块 X: <名称>' 标题分隔。\n"
        f"下方表格中 # 列即为该 Sheet 内的用例序号，"
        f"发现问题时请明确指出 '用例 #XX' 和所属模块名称。\n"
        f"对每个模块分别输出 [问题摘要] 区块。"
    )

    try:
        result = call_agent(test_cases_text, label, bug_profile=bug_profile, pipeline=pipeline)
        review_text = _strip_emoji(result.review)
        full_answer = (
            "### 知识上下文\n\n"
            f"{result.knowledge or '(无额外知识上下文)'}\n\n"
            "### 评审结果\n\n"
            f"{review_text}"
        )
        if debug_file:
            _append_debug_jsonl(
                debug_file,
                {
                    "label": label,
                    "sheet": sheet_name,
                    "batch_indices": batch_indices,
                    "type": "batch_review",
                    "pipeline_result": result.to_dict(),
                },
            )
    except Exception as e:
        logger.error("[%s] 批量评审失败: %s", label, e)
        full_answer = f"**评审失败**: {e}"

    # Split the combined answer back to per-module using headers
    return _split_batch_answer(full_answer, batch_indices, modules, global_offsets)


# ════════════════════════════════════════════════════════════
#  按模块评审
# ════════════════════════════════════════════════════════════

def review_module(item: str, sub_item: str, sub_df: pd.DataFrame,
                  sheet_name: str, module_idx: int, total_modules: int,
                  global_offset: int, bug_profile: dict | None = None,
                  pipeline=None, debug_file: Path | None = None) -> str:
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
            f"**Sheet**: {sheet_name}\n"
            f"**模块**: {item} > {sub_item}\n"
            f"**统计**: {stats}\n\n"
            f"{table}\n\n"
            f"请对该模块的全部 {n} 条测试用例进行整体评审。"
            f"下方表格中 # 列即为该 Sheet 内的用例序号，"
            f"发现问题时请明确指出 '用例 #XX' 和所属 Sheet 名称。"
            f"重点评估该模块的覆盖度是否完整，然后逐条检查质量问题。"
        )
        try:
            result = call_agent(test_cases_text, label, bug_profile=bug_profile, pipeline=pipeline)
            review_text = _strip_emoji(result.review)
            if debug_file:
                _append_debug_jsonl(
                    debug_file,
                    {
                        "label": label,
                        "sheet": sheet_name,
                        "module_idx": module_idx,
                        "module_name": f"{item} > {sub_item}",
                        "type": "module_review",
                        "pipeline_result": result.to_dict(),
                    },
                )
            return (
                "### 知识上下文\n\n"
                f"{result.knowledge or '(无额外知识上下文)'}\n\n"
                "### 评审结果\n\n"
                f"{review_text}"
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
                f"**Sheet**: {sheet_name}\n"
                f"**模块**: {item} > {sub_item} (共{n}条, 本批第{start+1}-{end}条)\n"
                f"**模块整体统计**: {stats}\n\n"
                f"{table}\n\n"
                f"请评审该模块的第 {start+1}-{end} 条用例。"
                f"下方表格中 # 列即为该 Sheet 内的用例序号，"
                f"发现问题时请明确指出 '用例 #XX' 和所属 Sheet 名称。"
                f"从模块整体覆盖度角度评估，并逐条检查质量。"
            )
            try:
                result = call_agent(test_cases_text, sub_label, bug_profile=bug_profile, pipeline=pipeline)
                review_text = _strip_emoji(result.review)
                if debug_file:
                    _append_debug_jsonl(
                        debug_file,
                        {
                            "label": sub_label,
                            "sheet": sheet_name,
                            "module_idx": module_idx,
                            "module_name": f"{item} > {sub_item}",
                            "range": [start + 1, end],
                            "type": "module_split_review",
                            "pipeline_result": result.to_dict(),
                        },
                    )
                answer = (
                    "#### 知识上下文\n\n"
                    f"{result.knowledge or '(无额外知识上下文)'}\n\n"
                    "#### 评审结果\n\n"
                    f"{review_text}"
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
    """自动检测 Excel 文件中的有效数据 Sheet（排除非数据 Sheet）。"""
    xl = pd.ExcelFile(excel_path)
    skip = {"list sample", "sample", "template", "说明", "readme",
            "history", "option definition"}
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
    all_summaries: dict[str, list[dict]] = {}
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

        # Create a single pipeline per sheet (knowledge cache reused across modules)
        pipeline = _create_pipeline()

        sheet_file = output_dir / f"review_{sheet_name.replace(' ', '_')}.md"
        debug_file = output_dir / f"review_debug_{sheet_name.replace(' ', '_')}.jsonl"
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

        # Compute global offsets for each module
        global_offsets: list[int] = []
        offset = 0
        for _item, _sub, sub_df in modules:
            global_offsets.append(offset)
            offset += len(sub_df)

        # Group modules into batches for efficient LLM usage
        batches = _group_modules_into_batches(modules, MAX_BATCH_CASES)
        logger.info("[%s] %d 个模块分为 %d 个批次 (上限 %d 条/批)",
                    sheet_name, num_modules, len(batches), MAX_BATCH_CASES)

        # 收集每个模块的问题摘要
        module_summaries: list[dict] = []
        module_answers: dict[int, str] = {}

        for batch_num, batch_indices in enumerate(batches, 1):
            batch_modules = [f"{modules[i][0]}/{modules[i][1]}" for i in batch_indices]
            batch_cases = sum(len(modules[i][2]) for i in batch_indices)
            logger.info("-" * 40)
            logger.info("[%s] 批次 %d/%d: %s (%d 条)",
                        sheet_name, batch_num, len(batches),
                        ", ".join(batch_modules), batch_cases)

            batch_results = review_module_batch(
                modules, batch_indices, sheet_name,
                global_offsets, num_modules,
                bug_profile=bug_profile, pipeline=pipeline, debug_file=debug_file,
            )
            module_answers.update(batch_results)

        # Write results per module (in original order)
        for mi, (item, sub_item, sub_df) in enumerate(modules):
            n = len(sub_df)
            answer = module_answers.get(mi, "**未评审**")

            # 提取问题摘要
            issue_summary = _extract_issue_summary(answer)
            if issue_summary:
                module_summaries.append({
                    "module": f"{item} > {sub_item}",
                    "count": n,
                    "offset": global_offsets[mi],
                    "issues": issue_summary,
                })

            with open(sheet_file, "a", encoding="utf-8") as f:
                f.write(f"---\n\n")
                f.write(f"## 模块 {mi+1}/{num_modules}: {item} > {sub_item} ({n} 条)\n\n")
                table = format_module_table(sub_df, global_offsets[mi])
                f.write(f"### 用例列表\n\n{table}\n\n")
                f.write(f"### 评审结果\n\n{answer}\n\n")

        # 在评审文件尾部追加汇总摘要
        if module_summaries:
            with open(sheet_file, "a", encoding="utf-8") as f:
                f.write("---\n\n")
                f.write("## 问题汇总\n\n")
                for ms in module_summaries:
                    f.write(f"### {ms['module']} (用例 #{ms['offset']+1}-#{ms['offset']+ms['count']})\n\n")
                    f.write(f"{ms['issues']}\n\n")

        all_summaries[sheet_name] = module_summaries
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

        # 写入各 Sheet 的问题汇总
        has_issues = any(summaries for summaries in all_summaries.values())
        if has_issues:
            f.write("\n---\n\n## 问题清单\n\n")
            for sn in data_sheets:
                summaries = all_summaries.get(sn, [])
                if not summaries:
                    continue
                f.write(f"### Sheet: {sn}\n\n")
                for ms in summaries:
                    f.write(f"**{ms['module']}** (用例 #{ms['offset']+1}-#{ms['offset']+ms['count']})\n\n")
                    f.write(f"{ms['issues']}\n\n")
        else:
            f.write("\n> 未提取到结构化问题摘要，请参阅各 Sheet 的详细评审文件。\n")

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
