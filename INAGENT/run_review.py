"""
Manager 统一评审工作流

由评审委员会会长 (Manager) 统一组织的 Bug 定向测试评审。
流程:
1. 自动执行 prepare_bug_to_case.py 将包含 Excel 和 Bug 详情文本的目录转换为 bug_to_case.json
2. Manager 全局审计 → 模块分流 (skip/light/full) → Workforce Pipeline 整体评审
3. 结果按模块拆分输出到 reports/ 目录
"""
import argparse
import logging
import os
import re
import shutil
import sys
import subprocess
import time
from pathlib import Path
import json
from dataclasses import dataclass, field
from statistics import mean
from datetime import datetime
import uuid
import traceback
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent 
sys.path.insert(0, str(ROOT))

from INAGENT.utils import env_utils
from INAGENT.config.project_config import cfg_bool, cfg_float, cfg_int, cfg_str
env_utils.load_inagent_env()

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - tqdm 为可选依赖
    tqdm = None

def _force_utf8_console() -> None:
    """Force UTF-8 stdout/stderr on Windows PowerShell terminals."""
    if os.name != "nt":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_console()

_META_JSON_INLINE_RE = re.compile(r"INAGENT_META_JSON:\{.*?\}\s*", re.IGNORECASE)
# 内部结构化 JSON 标记行 —— 不应出现在最终报告中
_INTERNAL_JSON_MARKER_RE = re.compile(
    r"^\s*(?:WORKER_RESULT_JSON|MANAGER_JUDGEMENT_JSON|GLOBAL_AUDIT_JSON)\s*:\s*\{.*",
    re.IGNORECASE,
)
# 内部配置参数关键词 —— 报告中不应引用这些实现细节
_INTERNAL_PARAM_TOKENS = (
    "min_confidence",
    "max_unresolved",
    "pack_sig",
    "cache_key",
    "worker_result_json",
    "manager_judgement_json",
    "global_audit_json",
    "inagent_meta_json",
    "quality_flags",
    "evidence_refs",
    "supplement_tasks",
    "consistency_alert",
)
_EVIDENCE_DISCLAIMER_PATTERNS = (
    "the provided data does not contain",
    "if you don't know the answer, just say so",
    "points supported by data should list their data references",
    "does not contain explicit information",
    "not found in the provided data",
    "insufficient information",
    "the available documentation",
    "cannot be determined from the data",
)


def _sanitize_knowledge_evidence(
    text: str,
    *,
    domain_keywords: Optional[List[str]] = None,
) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    keyword_tokens = [
        t for t in (_normalize_token(k) for k in (domain_keywords or [])) if len(t) >= 3
    ]
    seen = set()
    sanitized_lines = []
    for line in raw.splitlines():
        line = _META_JSON_INLINE_RE.sub("", line).strip()
        if not line:
            continue
        # 过滤内部 JSON 标记行
        if _INTERNAL_JSON_MARKER_RE.match(line):
            continue
        low = line.lower()
        if any(p in low for p in _EVIDENCE_DISCLAIMER_PATTERNS):
            continue
        # 过滤包含内部参数 token 的行
        if any(tok in low for tok in _INTERNAL_PARAM_TOKENS):
            continue
        if line in seen:
            continue
        if EVIDENCE_STRICT_DOMAIN_FILTER and keyword_tokens:
            norm_line = _normalize_token(line)
            if _line_relevance_score(norm_line, keyword_tokens) <= 0:
                continue
        seen.add(line)
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines).strip()


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _compact_knowledge(
    text: str,
    limit: int = 1200,
    *,
    domain_keywords: Optional[List[str]] = None,
) -> str:
    raw = _sanitize_knowledge_evidence(text, domain_keywords=domain_keywords)
    if not raw:
        return "(无额外知识上下文)"
    compact = " ".join(raw.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + " ...[已截断，详见 debug 日志]"


_RXX_PATTERN = re.compile(r"\bR(?:0[1-9]|1[0-8])\b")
_CASE_REF_PATTERN = re.compile(r"用例\s*#(\d+)")


def _dedup_review_issues(text: str) -> str:
    """对评审报告中按用例编号分组的重复问题进行去重。"""
    if not text:
        return text
    lines = text.split("\n")
    seen_fingerprints: set = set()
    result_lines: list = []
    current_block: list = []
    current_case: str = ""

    def _flush_block():
        nonlocal current_block, current_case
        if not current_block:
            return
        block_text = "\n".join(current_block)
        _normalized = re.sub(r'\s+', '', block_text[:80])
        fp = f"{current_case}:{_normalized}"
        if fp not in seen_fingerprints:
            seen_fingerprints.add(fp)
            result_lines.extend(current_block)
        current_block = []

    for line in lines:
        case_match = _CASE_REF_PATTERN.search(line)
        if case_match:
            _flush_block()
            current_block = [line]
            current_case = case_match.group(1)
        elif current_block:
            current_block.append(line)
        else:
            result_lines.append(line)
    _flush_block()
    return "\n".join(result_lines)


def _strip_rxx_codes(text: str) -> str:
    """从报告文本中移除 Rxx 内部编号。"""
    if not text:
        return text
    result = _RXX_PATTERN.sub("", text)
    # 清理 " - " 残留（原来 "R01 - 规则原文" → " - 规则原文" → "规则原文"）
    result = re.sub(r"(?<=\s)-\s+(?=\S)", "", result)
    result = re.sub(r"  +", " ", result)
    return result


def _strip_internal_markers(text: str) -> str:
    """从报告文本中移除内部标记、标签引用和内部段落，使报告面向被评审人。"""
    if not text:
        return text
    # ── Phase 1: 移除整段内部块 ──────────────────────────────
    # 移除 <adversarial_verification> ... </adversarial_verification> 块
    text = re.sub(
        r"---\s*\n\s*<adversarial_verification>.*?</adversarial_verification>\s*",
        "", text, flags=re.DOTALL,
    )
    text = re.sub(
        r"<adversarial_verification>.*?</adversarial_verification>\s*",
        "", text, flags=re.DOTALL,
    )
    # 移除 [溯源校验摘要] 到下一个 ### 或文末的段落
    text = re.sub(
        r"\[溯源校验摘要\].*?(?=\n###|\n---|\Z)",
        "", text, flags=re.DOTALL,
    )

    # ── Phase 2: 逐行过滤 ────────────────────────────────────
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if _INTERNAL_JSON_MARKER_RE.match(stripped):
            continue
        line = _META_JSON_INLINE_RE.sub("", line)
        stripped_lower = stripped.lower()
        if any(tok in stripped_lower for tok in _INTERNAL_PARAM_TOKENS):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned)

    # ── Phase 3: 将内部标签引用替换为自然语言 ─────────────────
    _TAG_REPLACEMENTS = {
        "<product_knowledge>": "产品知识文档",
        "<traceability_matrix>": "需求追踪矩阵",
        "<change_impact>": "变更影响分析",
        "<bug_context>": "Bug描述",
        "<review_experience_memory>": "评审经验库",
        "<review_experience>": "评审经验库",
        "<target_test_cases>": "待评审用例",
        "<rule_map>": "评审规范",
        "<cli_reference>": "CLI参考手册",
        "<similar_tests>": "相似测试用例",
        "`<product_knowledge>`": "产品知识文档",
        "`<traceability_matrix>`": "需求追踪矩阵",
        "`<change_impact>`": "变更影响分析",
        "`<bug_context>`": "Bug描述",
        "`<review_experience_memory>`": "评审经验库",
        "`<review_experience>`": "评审经验库",
        "`<target_test_cases>`": "待评审用例",
        "`<rule_map>`": "评审规范",
        "`<cli_reference>`": "CLI参考手册",
        "<product_knowledge_excerpt>": "产品知识文档",
        "<current_review>": "当前评审报告",
        "<test_cases_excerpt>": "待评审用例",
        "<evaluation_scores>": "评估分数",
        "`<product_knowledge_excerpt>`": "产品知识文档",
        "`<current_review>`": "当前评审报告",
        "`<test_cases_excerpt>`": "待评审用例",
        "`<evaluation_scores>`": "评估分数",
    }
    for tag, replacement in _TAG_REPLACEMENTS.items():
        text = text.replace(tag, replacement)
    text = re.sub(r"<(?:product_knowledge(?:_excerpt)?|traceability_matrix|change_impact|"
                  r"bug_context|review_experience(?:_memory)?|target_test_cases|"
                  r"rule_map|cli_reference|similar_tests|current_review|"
                  r"test_cases_excerpt|evaluation_scores)>", "相关参考文档", text)

    # ── Phase 3.5: 替换裸文本形式的内部引用名（无尖括号）──────────
    # LLM 可能输出 "product_knowledge 明确指出" 或 "bug_context 中"
    _BARE_REF_REPLACEMENTS = {
        "product_knowledge": "产品知识文档",
        "traceability_matrix": "需求追踪矩阵",
        "change_impact": "变更影响分析",
        "bug_context": "Bug描述",
        "review_experience_memory": "评审经验库",
        "review_experience": "评审经验库",
        "target_test_cases": "待评审用例",
        "rule_map": "评审规范",
        "cli_reference": "CLI参考手册",
        "product_knowledge_excerpt": "产品知识文档",
        "current_review": "当前评审报告",
        "test_cases_excerpt": "待评审用例",
        "evaluation_scores": "评估分数",
    }
    for bare, replacement in _BARE_REF_REPLACEMENTS.items():
        # 匹配独立出现的裸引用名（前后非字母数字下划线），避免替换正常英文单词
        text = re.sub(
            r"(?<![a-zA-Z0-9_])" + re.escape(bare) + r"(?![a-zA-Z0-9_])",
            replacement, text,
        )

    # ── Phase 4: 移除 REQ_xxx 编号（被评审人不需要看到内部需求编号）────
    # 替换 "[REQ_006]" 或 "REQ_006" 为空，但保留其周围的描述文字
    text = re.sub(r"\[REQ_\d+\]\s*", "", text)
    text = re.sub(r"(?<![A-Z])REQ_\d+\s*", "", text)

    # ── Phase 4.5: 清理 REQ 移除后的孤立标点 ──────────────────
    # 移除后可能残留 "、、" "；、" 等空顿号序列
    text = re.sub(r"[、，]{2,}", "、", text)  # "、、、" → "、"
    # 移除行首或分号后紧跟的孤立顿号
    text = re.sub(r"；\s*、", "；", text)

    # ── Phase 5: 清理多余空行 ────────────────────────────────
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ── Phase 6a: 剥离 emoji 字符 ──────────────────────────────
    text = re.sub(
        r'[\u2705\u274C\u2714\u2716\u26A0\u2B50\u2611\u2612\u23F0'
        r'\U0001F4A1\U0001F6A8\U0001F527]',
        '', text,
    )

    # ── Phase 6b: 剥离 Refiner 元叙述开头段 ─────────────────────
    text = re.sub(
        r'^.*?(?=---\s*\n|### 发现)',
        '',
        text,
        count=1,
        flags=re.DOTALL,
    )

    return text


def _extract_synthesis_review(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    full_text = raw  # keep original for fallback
    marker = "--- Task review_synthesis Result ---"
    idx = raw.rfind(marker)
    if idx >= 0:
        raw = raw[idx + len(marker):].strip()
    # Fallback: if synthesis section has no structured findings (e.g. max_iteration hit),
    # search the full pipeline output (fork worker results) for findings.
    _finding_re = re.compile(r"^###\s*发现\s*\d*\s*[:：]", re.MULTILINE)
    if not _finding_re.search(raw) and _finding_re.search(full_text):
        logger.warning(
            "Synthesis section has no findings; falling back to full pipeline output"
        )
        # Extract all fork worker sections that contain findings
        parts = []
        for task_marker in re.finditer(r"--- Task \w+ Result ---", full_text):
            section_start = task_marker.end()
            next_marker = re.search(r"--- Task \w+ Result ---", full_text[section_start:])
            section_end = section_start + next_marker.start() if next_marker else len(full_text)
            section = full_text[section_start:section_end]
            if _finding_re.search(section):
                parts.append(section.strip())
        if parts:
            raw = "\n\n".join(parts)
    raw = _strip_internal_markers(raw)
    raw = _strip_rxx_codes(raw)
    raw = _dedup_review_issues(raw)
    raw = _sanitize_cli_commands(raw)
    raw = _annotate_unverified_cli(raw)
    return raw


def _extract_structured_findings(review_text: str) -> List[Dict[str, Any]]:
    """从 Markdown 评审文本中提取结构化 findings。"""
    text = (review_text or "").strip()
    if not text:
        return []
    title_re = re.compile(r"^###\s*发现\s*(\d+)?\s*[:：]\s*(.+)$", re.MULTILINE)
    matches = list(title_re.finditer(text))
    if not matches:
        return []

    field_aliases = {
        "scope": ["涉及范围", "涉及用例", "范围", "Scope"],
        "issue_or_suggestion": ["问题或建议", "问题描述", "问题", "建议", "Issue", "Suggestion"],
        "modification": ["修改建议"],
        "evidence": ["依据", "证据", "Evidence"],
        "priority": ["优先级", "Priority"],
    }
    bold_patterns = {
        key: re.compile(
            r"^\s*(?:-\s*)?\*\*(?:" + "|".join(re.escape(x) for x in aliases) + r")\*\*\s*[:：]\s*(.*)$",
            re.IGNORECASE,
        )
        for key, aliases in field_aliases.items()
    }
    table_patterns = {
        key: re.compile(
            r"^\s*\|\s*(?:" + "|".join(re.escape(x) for x in aliases) + r")\s*\|\s*(.*?)\s*\|?\s*$",
            re.IGNORECASE,
        )
        for key, aliases in field_aliases.items()
    }
    stop_prefixes = (
        "[溯源校验摘要]",
        "[补充说明]",
        "MANAGER_JUDGEMENT_JSON:",
        "WORKER_RESULT_JSON:",
        "GLOBAL_AUDIT_JSON:",
    )

    def _trim_value(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip())

    findings: List[Dict[str, Any]] = []
    for idx, match in enumerate(matches, start=1):
        start = match.end()
        end = matches[idx].start() if idx < len(matches) else len(text)
        body = text[start:end].strip()
        title = match.group(2).strip()
        parsed = {
            "id": f"F{idx:03d}",
            "title": title,
            "scope": "",
            "issue_or_suggestion": "",
            "modification": "",
            "evidence": "",
            "priority": "",
        }

        current_field: Optional[str] = None
        for line in body.splitlines():
            raw_line = line.strip()
            if not raw_line:
                continue

            if raw_line.startswith(stop_prefixes):
                break

            # 跳过表格分隔行 |------|------|
            if re.match(r"^\s*\|[-\s|]+\|\s*$", raw_line):
                continue
            # 跳过表头行 | 项目 | 内容 |
            if re.match(r"^\s*\|\s*项目\s*\|\s*内容\s*\|", raw_line):
                continue

            matched_field = None
            matched_value = ""
            for key in field_aliases:
                m = bold_patterns[key].match(raw_line)
                if m:
                    matched_field = key
                    matched_value = m.group(1).strip()
                    break
                m = table_patterns[key].match(raw_line)
                if m:
                    matched_field = key
                    matched_value = m.group(1).strip()
                    break

            if matched_field:
                current_field = matched_field
                if matched_value:
                    parsed[matched_field] = _trim_value(matched_value)
                continue

            # 连续行归并到当前字段，支持长段 evidence / issue
            if current_field and not raw_line.startswith("###"):
                existing = parsed.get(current_field, "")
                merged = f"{existing} {raw_line}".strip() if existing else raw_line
                parsed[current_field] = _trim_value(merged)

        if parsed.get("modification"):
            if parsed.get("issue_or_suggestion"):
                parsed["issue_or_suggestion"] += f" 修改建议: {parsed['modification']}"
            else:
                parsed["issue_or_suggestion"] = parsed["modification"]
        findings.append(parsed)
    return findings


def _filter_hallucinated_findings(
    findings: List[Dict[str, Any]],
    review_text: str,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """依据 MANAGER_JUDGEMENT_JSON.hallucination_removed 过滤结构化 findings。"""
    if not findings:
        return [], []
    judgement = _extract_marked_json_object(review_text, "MANAGER_JUDGEMENT_JSON") or {}
    removed_hints = [
        str(x).strip()
        for x in (judgement.get("hallucination_removed") or [])
        if str(x).strip()
    ]
    if not removed_hints:
        return findings, []

    def _normalize_phrase(s: str) -> str:
        return re.sub(r"\s+", "", (s or "").lower())

    hint_norms = [_normalize_phrase(x) for x in removed_hints]
    filtered: List[Dict[str, Any]] = []
    dropped_titles: List[str] = []
    for finding in findings:
        text_blob = " ".join(
            [
                str(finding.get("title") or ""),
                str(finding.get("scope") or ""),
                str(finding.get("issue_or_suggestion") or ""),
                str(finding.get("evidence") or ""),
            ]
        )
        blob_norm = _normalize_phrase(text_blob)
        matched = False
        for hint_norm in hint_norms:
            if hint_norm and hint_norm in blob_norm:
                matched = True
                break
        if matched:
            dropped_titles.append(str(finding.get("title") or "(untitled)"))
        else:
            filtered.append(finding)

    return filtered, dropped_titles


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _append_debug_event(
    *,
    debug_file: Path,
    event: str,
    payload: Dict[str, Any],
    run_id: str,
    sheet_name: str,
) -> None:
    """统一写入 debug 事件，补齐最小可观测字段。"""
    event_obj = {
        "schema_version": "debug-event-v1",
        "event": event,
        "event_id": uuid.uuid4().hex[:12],
        "timestamp_utc": _utc_now_iso(),
        "run_id": run_id,
        # 对齐 OTel 风格字段（后续可接入真正 trace/span）
        "trace_id": run_id,
        "sheet_name": sheet_name,
    }
    event_obj.update(payload or {})
    try:
        with open(debug_file, "a", encoding="utf-8") as df:
            df.write(json.dumps(event_obj, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.debug("debug event write failed (non-fatal): %s", e)


# ── CLI 命令后处理验证 ──────────────────────────────────────────────
_BACKTICK_CMD_RE = re.compile(r"`([^`]{4,120})`")
# 已知不存在的命令模式（正则）
_KNOWN_FAKE_CLI_PATTERNS = [
    re.compile(r"packet\s+capture\s+start.*filter", re.IGNORECASE),
    re.compile(r"show\s+connection\s+detail\s*\|?\s*include", re.IGNORECASE),
    re.compile(r"system\s+tune\s+faststack\s+enable", re.IGNORECASE),
]


def _sanitize_cli_commands(text: str, cli_reference: str = "") -> str:
    """扫描报告中反引号包裹的 CLI 命令，标注无法验证的命令。"""
    if not text:
        return text
    cli_ref_lower = (cli_reference or "").lower()

    def _check_cmd(match: re.Match) -> str:
        cmd = match.group(1)
        cmd_stripped = cmd.strip()
        # 跳过非 CLI 的反引号内容（纯数字、短词、常见代码片段）
        if len(cmd_stripped) < 6 or cmd_stripped.startswith("#") or cmd_stripped.startswith("0x"):
            return match.group(0)
        # 检查已知假命令模式
        for fake_pat in _KNOWN_FAKE_CLI_PATTERNS:
            if fake_pat.search(cmd_stripped):
                return f"`{cmd}` ⚠ 此命令不存在，需修正"
        # 如果有 cli_reference，检查命令第一个词是否出现
        if cli_ref_lower:
            first_word = cmd_stripped.split()[0].lower() if cmd_stripped.split() else ""
            # CLI 命令通常以 show/system/no/ip/slb/config/debug/clear/enable/disable 等开头
            cli_verb_like = first_word in (
                "show", "system", "no", "ip", "slb", "config", "debug",
                "clear", "enable", "disable", "set", "unset", "display",
                "tcpdump", "scapy", "hping3", "tcpreplay", "ping",
            )
            if cli_verb_like and first_word not in cli_ref_lower:
                return f"`{cmd}` ⚠ 此命令需人工确认是否存在"
        return match.group(0)

    return _BACKTICK_CMD_RE.sub(_check_cmd, text)


def _annotate_unverified_cli(text: str) -> str:
    """使用 CLI graph 的 command_exists() 对报告中的 CLI 命令做最终校验。"""
    if not text:
        return text
    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        cli_graph = get_cli_graph_store()
    except Exception:
        return text

    _cli_prefixes = ("slb ", "no ", "show ", "clear ", "system ", "ip ", "http ")

    def _check(match: re.Match) -> str:
        cmd = match.group(1).strip()
        if len(cmd) < 6:
            return match.group(0)
        cmd_lower = cmd.lower()
        if not any(cmd_lower.startswith(p) for p in _cli_prefixes):
            return match.group(0)
        exists, _similar = cli_graph.command_exists(cmd)
        if not exists:
            return f"`{cmd}`（待确认）"
        return match.group(0)

    return _BACKTICK_CMD_RE.sub(_check, text)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
)
logger = logging.getLogger("run_review")
REDUCE_CAMEL_LOGS = cfg_bool(
    "bug_to_case.runtime.reduce_camel_logs",
    True,
    env="BUG_TO_CASE_REDUCE_CAMEL_LOGS",
)
if REDUCE_CAMEL_LOGS:
    # 这些 logger 会打印完整 prompt/response，I/O 成本高且影响评审吞吐。
    logging.getLogger("camel.base_model").setLevel(logging.WARNING)
    logging.getLogger("camel.camel.agents.chat_agent").setLevel(logging.WARNING)
    logging.getLogger("camel.camel.societies.workforce.single_agent_worker").setLevel(
        logging.WARNING
    )
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)

MODULE_REVIEW_TIMEOUT_SECONDS = cfg_int(
    "bug_to_case.runtime.module_timeout_seconds",
    480,
    env="BUG_TO_CASE_MODULE_TIMEOUT_SECONDS",
)
BUG_TO_CASE_CLEAN_OUTPUT_DIR = cfg_bool(
    "bug_to_case.runtime.clean_output_dir",
    True,
    env="BUG_TO_CASE_CLEAN_OUTPUT_DIR",
)
MODULE_RETRY_TIMES = max(
    0,
    cfg_int(
        "bug_to_case.retry.module_retry_times",
        1,
        env="BUG_TO_CASE_MODULE_RETRY_TIMES",
    ),
)
MODULE_RETRY_BACKOFF_SECONDS = max(
    0.0,
    cfg_float(
        "bug_to_case.retry.module_retry_backoff_seconds",
        2.0,
        env="BUG_TO_CASE_MODULE_RETRY_BACKOFF_SECONDS",
    ),
)
ENABLE_PROGRESS_BAR = cfg_bool(
    "bug_to_case.progress.enable_progress_bar",
    True,
    env="BUG_TO_CASE_ENABLE_PROGRESS_BAR",
)
FAIL_ON_SOFT_FAILED = cfg_bool(
    "bug_to_case.runtime.fail_on_soft_failed",
    False,
    env="BUG_TO_CASE_FAIL_ON_SOFT_FAILED",
)
SUPPRESS_COMMUNITY_WARNING = cfg_bool(
    "bug_to_case.rag.suppress_community_warning",
    True,
    env="BUG_TO_CASE_SUPPRESS_COMMUNITY_WARNING",
)
GLOBAL_AUDIT_ENABLED = cfg_bool(
    "bug_to_case.global_audit.enabled",
    True,
    env="BUG_TO_CASE_GLOBAL_AUDIT_ENABLED",
)
GLOBAL_AUDIT_MAX_MODULES_IN_PROMPT = max(
    1,
    cfg_int(
        "bug_to_case.global_audit.max_modules_in_prompt",
        64,
        env="BUG_TO_CASE_GLOBAL_AUDIT_MAX_MODULES_IN_PROMPT",
    ),
)
GLOBAL_AUDIT_MAX_CONTEXT_CHARS = max(
    600,
    cfg_int(
        "bug_to_case.global_audit.max_context_chars",
        3600,
        env="BUG_TO_CASE_GLOBAL_AUDIT_MAX_CONTEXT_CHARS",
    ),
)
GLOBAL_AUDIT_LIGHT_CHECK_CASE_THRESHOLD = max(
    0,
    cfg_int(
        "bug_to_case.global_audit.light_check_case_threshold",
        1,
        env="BUG_TO_CASE_GLOBAL_AUDIT_LIGHT_CHECK_CASE_THRESHOLD",
    ),
)
GLOBAL_AUDIT_STRICT_PARSE = cfg_bool(
    "bug_to_case.global_audit.strict_parse",
    True,
    env="BUG_TO_CASE_GLOBAL_AUDIT_STRICT_PARSE",
)
GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL = max(
    0,
    cfg_int(
        "bug_to_case.global_audit.retry_on_parse_fail",
        1,
        env="BUG_TO_CASE_GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL",
    ),
)
GLOBAL_AUDIT_MIN_SKIP_CONFIDENCE = max(
    0.0,
    min(
        1.0,
        cfg_float(
            "bug_to_case.global_audit.scope_classifier.min_out_scope_confidence",
            0.9,
            env="BUG_TO_CASE_SCOPE_MIN_OUT_SCOPE_CONFIDENCE",
        ),
    ),
)
GLOBAL_AUDIT_MAX_SKIP_RATIO = max(
    0.0,
    min(
        1.0,
        cfg_float(
            "bug_to_case.global_audit.scope_classifier.max_out_scope_ratio",
            0.35,
            env="BUG_TO_CASE_SCOPE_MAX_OUT_SCOPE_RATIO",
        ),
    ),
)
GLOBAL_AUDIT_MIN_ACTIVE_RATIO = max(
    0.0,
    min(
        1.0,
        cfg_float(
            "bug_to_case.global_audit.scope_classifier.min_active_module_ratio",
            0.6,
            env="BUG_TO_CASE_SCOPE_MIN_ACTIVE_RATIO",
        ),
    ),
)
GLOBAL_AUDIT_UNCERTAIN_POLICY = (
    cfg_str(
        "bug_to_case.global_audit.scope_classifier.uncertain_policy",
        "light",
        env="BUG_TO_CASE_SCOPE_UNCERTAIN_POLICY",
    )
    .strip()
    .lower()
)
EVIDENCE_STRICT_DOMAIN_FILTER = cfg_bool(
    "bug_to_case.report.evidence.strict_domain_filter",
    False,
    env="BUG_TO_CASE_EVIDENCE_STRICT_DOMAIN_FILTER",
)
MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE = cfg_bool(
    "bug_to_case.manager_first.require_strict_plan_parse",
    True,
    env="BUG_TO_CASE_MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE",
)
MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS = max(
    0,
    cfg_int(
        "bug_to_case.manager_first.max_supplement_rounds",
        1,
        env="BUG_TO_CASE_MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS",
    ),
)
MANAGER_FIRST_MIN_CONFIDENCE = max(
    0.0,
    min(
        1.0,
        cfg_float(
            "bug_to_case.manager_first.acceptance.min_confidence",
            0.7,
            env="BUG_TO_CASE_MANAGER_FIRST_MIN_CONFIDENCE",
        ),
    ),
)
MANAGER_FIRST_MAX_UNRESOLVED = max(
    0,
    cfg_int(
        "bug_to_case.manager_first.acceptance.max_unresolved",
        0,
        env="BUG_TO_CASE_MANAGER_FIRST_MAX_UNRESOLVED",
    ),
)
MANAGER_FIRST_MAX_TASKPACK_CHARS = max(
    300,
    cfg_int(
        "bug_to_case.manager_first.context.max_taskpack_chars",
        2400,
        env="BUG_TO_CASE_MANAGER_FIRST_MAX_TASKPACK_CHARS",
    ),
)
if SUPPRESS_COMMUNITY_WARNING:
    logging.getLogger("graphrag.query.context_builder.community_context").setLevel(
        logging.ERROR
    )
GATE_MAX_AVG_SEC = cfg_float(
    "bug_to_case.gates.max_avg_module_seconds",
    0.0,
    env="BUG_TO_CASE_GATE_MAX_AVG_SEC",
)
GATE_MAX_P95_SEC = cfg_float(
    "bug_to_case.gates.max_p95_module_seconds",
    0.0,
    env="BUG_TO_CASE_GATE_MAX_P95_SEC",
)
GATE_MAX_FAILED_MODULES = cfg_int(
    "bug_to_case.gates.max_failed_modules",
    0,
    env="BUG_TO_CASE_GATE_MAX_FAILED_MODULES",
)
GATE_MIN_MANAGER_FIRST_ACCEPT_RATE = max(
    0.0,
    min(
        1.0,
        cfg_float(
            "bug_to_case.gates.min_manager_first_accept_rate",
            0.0,
            env="BUG_TO_CASE_GATE_MIN_MANAGER_FIRST_ACCEPT_RATE",
        ),
    ),
)
REPORT_INCLUDE_EVIDENCE_SUMMARY = cfg_bool(
    "bug_to_case.report.include_evidence_summary",
    False,
    env="BUG_TO_CASE_REPORT_INCLUDE_EVIDENCE_SUMMARY",
)
REPORT_INCLUDE_RUN_STATUS_SECTION = cfg_bool(
    "bug_to_case.report.include_run_status_section",
    False,
    env="BUG_TO_CASE_REPORT_INCLUDE_RUN_STATUS_SECTION",
)
PERF_TARGET_SHEET_SECONDS = max(
    60.0,
    cfg_float(
        "bug_to_case.performance.target_sheet_seconds",
        480.0,
        env="BUG_TO_CASE_TARGET_SHEET_SECONDS",
    ),
)
PERF_MAX_SUPPLEMENT_SECONDS = max(
    20.0,
    cfg_float(
        "bug_to_case.performance.max_supplement_seconds",
        180.0,
        env="BUG_TO_CASE_MAX_SUPPLEMENT_SECONDS",
    ),
)


def _module_key(item: str, sub_item: str) -> str:
    return f"{(item or '').strip()} > {(sub_item or '').strip()}".lower()


def _module_display(module: dict) -> str:
    return f"{module.get('item', '').strip()} > {module.get('sub_item', '').strip()}"


def _truncate_text(raw: str, limit: int) -> str:
    text = (raw or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[已截断]"


def _normalize_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _line_relevance_score(line: str, keyword_tokens: List[str]) -> int:
    if not line:
        return 0
    low = line.lower()
    score = 0
    for token in keyword_tokens:
        if token and token in low:
            score += 1
    return score


def _extract_json_object(text: str) -> Optional[dict]:
    raw = (text or "").strip()
    if not raw:
        return None
    fence_match = re.search(r"```json\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    candidates: List[str] = []
    if fence_match:
        candidates.append(fence_match.group(1))
    marker_match = re.search(r"GLOBAL_AUDIT_JSON\s*:\s*(\{.*\})", raw, flags=re.IGNORECASE | re.DOTALL)
    if marker_match:
        candidates.append(marker_match.group(1))
    candidates.append(raw)
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        start = cand.find("{")
        end = cand.rfind("}")
        if start < 0 or end <= start:
            continue
        segment = cand[start : end + 1]
        try:
            payload = json.loads(segment)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return None


def _extract_marked_json_object(text: str, marker: str) -> Optional[dict]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(
        rf"{re.escape(marker)}\s*:\s*\{{",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    # Find matching closing brace using depth counting
    start = m.end() - 1  # position of the opening {
    depth = 0
    end = start
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    else:
        return None
    seg = raw[start:end].strip()
    try:
        obj = json.loads(seg)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass
class ManagerTaskPack:
    task_id: str
    worker_type: str
    objective: str
    focus_points: List[str] = field(default_factory=list)
    required_evidence: List[str] = field(default_factory=list)
    suggested_queries: List[str] = field(default_factory=list)
    max_output_chars: int = 1800

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "worker_type": self.worker_type,
            "objective": self.objective,
            "focus_points": list(self.focus_points),
            "required_evidence": list(self.required_evidence),
            "suggested_queries": list(self.suggested_queries),
            "max_output_chars": int(self.max_output_chars),
        }


@dataclass
class ManagerPlanContract:
    feature_outline: List[str] = field(default_factory=list)
    acceptance_gates: Dict[str, Any] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)
    confidence: float = 0.0
    worker_tasks: Dict[str, ManagerTaskPack] = field(default_factory=dict)
    product_context: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_outline": list(self.feature_outline),
            "acceptance_gates": dict(self.acceptance_gates),
            "unresolved": list(self.unresolved),
            "confidence": float(self.confidence),
            "worker_tasks": {k: v.to_dict() for k, v in self.worker_tasks.items()},
            "product_context": self.product_context,
        }


@dataclass
class WorkerResultContract:
    worker_type: str
    findings: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    confidence: float = 0.0
    unresolved: List[str] = field(default_factory=list)
    quality_flags: List[str] = field(default_factory=list)


@dataclass
class ManagerJudgementContract:
    passed: bool = False
    confidence: float = 0.0
    failed_gates: List[str] = field(default_factory=list)
    supplement_tasks: List[Dict[str, Any]] = field(default_factory=list)
    final_findings: List[str] = field(default_factory=list)
    consistency_alert: bool = False
    unresolved: List[str] = field(default_factory=list)
    parse_ok: bool = False


def _coerce_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, set):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return []
    text = str(value).strip()
    return [text] if text else []


def _coerce_dict_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _build_default_worker_taskpacks(
    *,
    module_name: str,
    bug_focus: str,
    key_questions: List[str],
    light_mode: bool,
    active_module_names: Optional[List[str]] = None,
) -> Dict[str, ManagerTaskPack]:
    common_focus = [module_name]
    if bug_focus:
        common_focus.append(bug_focus)
    # 追加活跃模块名到 focus_points，供 Worker 检索使用
    if active_module_names:
        for mn in active_module_names[:6]:
            mn = mn.strip()
            if mn and mn not in common_focus:
                common_focus.append(mn)
    common_focus.extend(key_questions[:4])
    common_focus = _dedupe_keep_order(common_focus)
    query_seed = [q for q in [module_name, bug_focus] if q]
    # 为 CLI worker 生成模块级查询种子
    cli_query_seed = list(query_seed)
    if active_module_names:
        for mn in active_module_names[:4]:
            mn = mn.strip()
            if mn and mn not in cli_query_seed:
                cli_query_seed.append(mn)
    budget = 1200 if light_mode else 1800
    return {
        "coverage": ManagerTaskPack(
            task_id="coverage",
            worker_type="coverage",
            objective="识别功能覆盖缺口与无关覆盖",
            focus_points=common_focus,
            required_evidence=["case_id", "功能点", "覆盖判断依据"],
            suggested_queries=[f"{x} 覆盖要求" for x in query_seed if x],
            max_output_chars=budget,
        ),
        "cli": ManagerTaskPack(
            task_id="cli",
            worker_type="cli",
            objective="检查 CLI 命令语法、可执行性与观测点",
            focus_points=common_focus,
            required_evidence=["case_id", "命令片段", "语法依据"],
            suggested_queries=[f"{x} CLI 语法 参考" for x in cli_query_seed if x],
            max_output_chars=budget,
        ),
        "spec": ManagerTaskPack(
            task_id="spec",
            worker_type="spec",
            objective="检查用例是否符合编写规范和评审标准",
            focus_points=common_focus,
            required_evidence=["case_id", "规则描述", "问题说明"],
            suggested_queries=[f"{x} 测试规范" for x in query_seed if x],
            max_output_chars=budget,
        ),
        "load": ManagerTaskPack(
            task_id="load",
            worker_type="load",
            objective="评估 Load/Stress 相关覆盖是否充分",
            focus_points=common_focus,
            required_evidence=["case_id", "负载场景", "风险说明"],
            suggested_queries=[f"{x} 压力测试 场景" for x in query_seed if x],
            max_output_chars=budget,
        ),
    }


def _retrieve_sheet_product_context(
    *,
    sheet_name: str,
    module_names: List[str],
    pipeline,
) -> str:
    """Manager 用来理解产品功能的 sheet 级产品知识检索。

    用 sheet_name + 各模块 item 名拼接查询，检索产品功能概要，
    让 Manager 在理解产品的基础上驱动评审。
    """
    if not pipeline:
        return ""
    try:
        router = getattr(pipeline, "router", None)
        if not router or not hasattr(router, "retrieve"):
            return ""
        max_query_modules = max(
            3,
            cfg_int(
                "bug_to_case.rag.manager_context_max_modules",
                8,
                env="BUG_TO_CASE_MANAGER_CONTEXT_MAX_MODULES",
            ),
        )
        max_context_chars = max(
            800,
            cfg_int(
                "bug_to_case.rag.manager_context_max_chars",
                2200,
                env="BUG_TO_CASE_MANAGER_CONTEXT_MAX_CHARS",
            ),
        )
        # 拼接查询：sheet名 + 各模块名
        query_parts = [sheet_name]
        query_parts.extend(module_names[:max_query_modules])
        query = " ".join(query_parts).strip()
        if not query:
            return ""
        result = router.retrieve(
            query=f"{query} 产品功能 设计要求",
            mode="explain",
            max_context_chars=max_context_chars,
        )
        return (result.get("context") or "").strip()
    except Exception as e:
        logger.warning("Manager 产品知识检索失败: %s", e)
        return ""


def _build_manager_plan_for_sheet(
    *,
    sheet_name: str,
    modules: List[dict],
    bug_context: dict,
    global_audit: dict,
    pipeline=None,
) -> ManagerPlanContract:
    key_questions = [
        str(x).strip()
        for x in (bug_context.get("key_questions") or [])
        if str(x).strip()
    ]
    bug_focus = str(bug_context.get("review_focus") or "").strip()
    priority_names: List[str] = []
    priority_keys = set(global_audit.get("priority_keys") or set())
    for module in modules:
        key = _module_key(module.get("item", ""), module.get("sub_item", ""))
        if key in priority_keys:
            priority_names.append(_module_display(module))
    outline = []
    if bug_focus:
        outline.append(bug_focus)
    if priority_names:
        outline.append("优先模块: " + "；".join(priority_names[:10]))
    outline.extend(global_audit.get("missing_features", [])[:8])
    # 提取活跃模块的 item/sub_item 名称，用于针对性检索
    active_module_names: List[str] = []
    for module in modules:
        for field_name in ("sub_item", "item"):
            name = str(module.get(field_name, "")).strip()
            if name and name not in active_module_names:
                active_module_names.append(name)
    # Manager 检索产品知识，理解模块功能后再驱动评审
    product_context = _retrieve_sheet_product_context(
        sheet_name=sheet_name,
        module_names=active_module_names,
        pipeline=pipeline,
    )
    if product_context:
        # 将产品知识要点追加到 feature_outline，影响 Manager 的决策
        outline.append("产品功能概要: " + product_context[:500])
    worker_tasks = _build_default_worker_taskpacks(
        module_name=f"{sheet_name}（整体验审）",
        bug_focus=bug_focus,
        key_questions=key_questions,
        light_mode=False,
        active_module_names=active_module_names,
    )
    gates = {
        "min_confidence": MANAGER_FIRST_MIN_CONFIDENCE,
        "max_unresolved": MANAGER_FIRST_MAX_UNRESOLVED,
        "require_all_workers": True,
        "supplement_once": True,
    }
    return ManagerPlanContract(
        feature_outline=outline,
        acceptance_gates=gates,
        unresolved=[],
        confidence=_safe_float(global_audit.get("confidence"), 0.0),
        worker_tasks=worker_tasks,
        product_context=product_context,
    )


def _parse_manager_judgement_from_text(text: str) -> ManagerJudgementContract:
    raw = _extract_marked_json_object(text, "MANAGER_JUDGEMENT_JSON")
    if not raw:
        return ManagerJudgementContract(parse_ok=False)
    failed_gates = _coerce_string_list(raw.get("failed_gates"))
    supplement_tasks = _coerce_dict_list(raw.get("supplement_tasks"))
    unresolved = _coerce_string_list(raw.get("unresolved"))
    return ManagerJudgementContract(
        passed=bool(raw.get("pass", False)),
        confidence=max(0.0, min(1.0, _safe_float(raw.get("confidence"), 0.0))),
        failed_gates=failed_gates,
        supplement_tasks=supplement_tasks,
        final_findings=_coerce_string_list(raw.get("final_findings")),
        consistency_alert=bool(raw.get("consistency_alert", False)),
        unresolved=unresolved,
        parse_ok=True,
    )


def _build_supplement_taskpack(
    base_pack: Dict[str, Any],
    supplement_tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    updated = dict(base_pack or {})
    worker_tasks = dict(updated.get("worker_tasks") or {})
    selected_workers: List[str] = []
    for item in supplement_tasks:
        worker_type = str(item.get("worker_type") or "").strip().lower()
        if not worker_type:
            continue
        if worker_type not in selected_workers:
            selected_workers.append(worker_type)
        task_obj = dict(worker_tasks.get(worker_type) or {})
        if item.get("objective"):
            task_obj["objective"] = str(item["objective"]).strip()
        extra_focus = [str(x).strip() for x in (item.get("focus_points") or []) if str(x).strip()]
        if extra_focus:
            task_obj["focus_points"] = list(dict.fromkeys((task_obj.get("focus_points") or []) + extra_focus))
        worker_tasks[worker_type] = task_obj
    updated["worker_tasks"] = worker_tasks
    if selected_workers:
        # 补充轮仅跑失败维度对应 worker，避免重跑整条链路。
        updated["selected_workers"] = selected_workers
    else:
        updated.pop("selected_workers", None)
    return updated


def _build_global_audit_prompt(
    *,
    sheet_name: str,
    modules: List[dict],
    bug_info: dict,
    bug_context: dict,
    retry_hint: str = "",
) -> str:
    listed_modules = modules[:GLOBAL_AUDIT_MAX_MODULES_IN_PROMPT]

    # ── Pre-load CLI graph for per-module tech features ──────────
    _cli_graph = None
    _module_node_map: Dict[str, dict] = {}
    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        _cli_graph = get_cli_graph_store()
        _cli_graph._ensure_loaded()
        # Build lookup: lowercase module id/label → node
        for _nid, _node in _cli_graph._module_nodes.items():
            _module_node_map[_nid.lower()] = _node
            _label = (_node.get("label") or "").lower()
            if _label and _label != _nid.lower():
                _module_node_map[_label] = _node
    except Exception:
        pass

    lines: List[str] = []
    for idx, module in enumerate(listed_modules, start=1):
        base_line = (
            f"{idx}. {_module_display(module)} | cases={int(module.get('case_count') or 0)} "
            f"| types={module.get('type_distribution') or {{}}} "
            f"| priorities={module.get('priority_distribution') or {{}}}"
        )
        # Inject tech features from enriched CLI graph
        sub_item = str(module.get("sub_item") or "").strip().lower()
        item = str(module.get("item") or "").strip().lower()
        mnode = _module_node_map.get(sub_item) or _module_node_map.get(item)
        # Fuzzy fallback: resolve sub_item/item as keyword in CLI graph
        if not mnode and _cli_graph:
            for candidate in [sub_item, item]:
                if not candidate:
                    continue
                try:
                    seeds = _cli_graph._resolve_hint_to_seeds(candidate)
                    if seeds:
                        for seed in seeds:
                            for mid, mn in _cli_graph._module_nodes.items():
                                if _cli_graph._graph.has_edge(mid, seed):
                                    mnode = mn
                                    break
                            if mnode:
                                break
                except Exception:
                    pass
                if mnode:
                    break
        if mnode:
            tech_parts = []
            proto = mnode.get("protocol_stack")
            if proto:
                tech_parts.append(f"proto={','.join(proto[:5])}")
            af = mnode.get("address_family")
            if af:
                tech_parts.append(f"addr={','.join(af)}")
            layer = mnode.get("layer")
            if layer:
                tech_parts.append(f"layer={layer}")
            iface = mnode.get("interface_types")
            if iface:
                tech_parts.append(f"iface={','.join(iface)}")
            if tech_parts:
                base_line += f" | tech=[{' | '.join(tech_parts)}]"
        lines.append(base_line)
    if len(modules) > len(listed_modules):
        lines.append(f"... 共 {len(modules)} 个模块，已截断展示前 {len(listed_modules)} 个")
    module_table = "\n".join(lines) if lines else "(无模块)"

    bug_title = str(bug_info.get("title") or "").strip()
    bug_id = str(bug_info.get("bug_id") or "").strip()
    review_focus = str(bug_context.get("review_focus") or "").strip()
    key_questions = [str(x).strip() for x in (bug_context.get("key_questions") or []) if str(x).strip()]
    key_q_text = "\n".join(f"- {q}" for q in key_questions) if key_questions else "- (无)"
    review_focus = _truncate_text(review_focus, GLOBAL_AUDIT_MAX_CONTEXT_CHARS)
    bug_title = _truncate_text(bug_title, GLOBAL_AUDIT_MAX_CONTEXT_CHARS)

    # ── CLI 功能树注入 ──────────────────────────────────────────
    cli_tree_section = ""
    try:
        cli_graph = _cli_graph  # reuse pre-loaded graph instance
        if cli_graph is not None:
            module_hints = []
            for m in modules:
                item = str(m.get("item") or "").strip()
                sub_item = str(m.get("sub_item") or "").strip()
                if sub_item:
                    module_hints.append(sub_item)
                elif item:
                    module_hints.append(item)
            if module_hints:
                subgraph = cli_graph.extract_subgraph(module_hints[:20])
                tree_text = cli_graph.format_for_prompt(subgraph, max_chars=1500)
                if tree_text:
                    cli_tree_section = (
                        f"\n## 产品 CLI 功能树（模块间层级/依赖关系）\n{tree_text}\n\n"
                        "注意：上面的功能树展示了模块间的父子/兄弟关系和共享关键字。\n"
                        "判断 out_of_scope 时，必须参考功能树确认该模块是否与修复路径存在结构关联。\n"
                    )
    except Exception as _e:
        logger.debug("CLI tree injection skipped: %s", _e)

    return (
        f"**Sheet**: {sheet_name}\n"
        f"**模块**: 全局覆盖审计\n\n"
        "你是测试评审架构师。请先基于 bug/new feature 语义，推导“应测模块/功能”，"
        "再对照当前模块列表判断覆盖是否完整。\n\n"
        "请正常输出你的分析结论（可用 Markdown），并在最后单独追加一行：\n"
        "GLOBAL_AUDIT_JSON: { ... }\n"
        "其中 JSON 字段必须完整，格式如下：\n"
        "{\n"
        "  \"in_scope_modules\": [\"模块A > 子模块X\"],\n"
        "  \"out_of_scope_modules\": [\"模块B > 子模块Y\"],\n"
        "  \"priority_modules\": [\"模块A > 子模块X\"],\n"
        "  \"missing_features\": [\"缺失功能1\"],\n"
        "  \"regression_scope\": [\"与修复路径存在功能树耦合但非直接根因的模块\"],\n"
        "  \"reason\": \"1-2句依据\",\n"
        "  \"confidence\": 0.0\n"
        "}\n\n"
        "要求：\n"
        "1) 只根据已知信息判断，不要虚构未知功能。\n"
        "2) out_of_scope_modules 仅列“明显与本次改动无关”的模块。\n"
        "3) priority_modules 列最应该优先深查的模块（最多 8 个）。\n"
        "4) confidence 取值 0.0-1.0。\n"
        "5) 若无项请输出空数组 []。\n"
        "6) GLOBAL_AUDIT_JSON 必须是单行合法 JSON。\n"
        "7) out_of_scope_modules 中的每个模块须同时满足：\n"
        "   a) 在功能树中与修复代码路径无父子/兄弟关系；\n"
        "   b) 其功能语义与 root_cause 的数据流/控制流无交集。\n"
        "   若不确定，应列入 in_scope 而非 out_of_scope。\n"
        "10) 横切面测试维度模块（如 HTTP版本、IPv6/双栈、语言/主题、集成turbo、HA、Stress等）\n"
        "   用于验证新功能在不同环境条件下的行为，不得标为 out_of_scope。\n"
        "   仅当模块的功能与本次改动完全无关（如独立的 FTP、DNS 模块）才可 out_of_scope。\n"
        "8) missing_features 仅列有明确产品知识支撑的缺失功能，禁止推测或编造。\n"
        "   regression_scope 列出与修复路径存在功能树耦合但非直接根因的模块。\n"
        "9) 模块列表中的 tech=[] 字段包含该模块的协议栈/地址族/层级/接口类型等技术特征，\n"
        "   请利用这些信息推断模块与 bug 修复路径的技术关联性。\n"
        f"{retry_hint}\n\n"
        f"## Bug 摘要\n- bug_id: {bug_id or 'unknown'}\n- title: {bug_title or '(无)'}\n"
        f"- review_focus: {review_focus or '(无)'}\n- key_questions:\n{key_q_text}\n\n"
        f"## 当前模块列表\n{module_table}\n"
        f"{cli_tree_section}"
    )


def _parse_pipe_list(value: str) -> List[str]:
    raw = (value or "").strip()
    if not raw or raw.upper() == "NONE":
        return []
    chunks = re.split(r"\|\||[;；]\s*", raw)
    return [c.strip() for c in chunks if c.strip()]


def _resolve_module_names(candidates: List[str], modules: List[dict]) -> Set[str]:
    if not candidates:
        return set()
    key_to_display: Dict[str, str] = {}
    item_to_keys: Dict[str, Set[str]] = {}
    sub_to_keys: Dict[str, Set[str]] = {}
    for module in modules:
        item = str(module.get("item") or "").strip()
        sub_item = str(module.get("sub_item") or "").strip()
        key = _module_key(item, sub_item)
        key_to_display[key] = f"{item} > {sub_item}"
        if item:
            item_to_keys.setdefault(item.lower(), set()).add(key)
        if sub_item:
            sub_to_keys.setdefault(sub_item.lower(), set()).add(key)

    resolved: Set[str] = set()
    for raw in candidates:
        cand = raw.strip().lower()
        if not cand:
            continue
        direct = None
        for key, display in key_to_display.items():
            disp = display.lower()
            if cand == disp or cand == key:
                direct = key
                break
            # Substring match only if the shorter side is long enough to be meaningful
            shorter_len = min(len(cand), len(disp))
            if shorter_len >= 4 and (cand in disp or disp in cand):
                direct = key
                break
        if direct:
            resolved.add(direct)
            continue
        if ">" in cand:
            left, right = [x.strip() for x in cand.split(">", 1)]
            for key, display in key_to_display.items():
                disp = display.lower()
                if left and right and left in disp and right in disp:
                    resolved.add(key)
            continue
        resolved.update(item_to_keys.get(cand, set()))
        resolved.update(sub_to_keys.get(cand, set()))
    return resolved


def _run_global_audit_direct(
    *,
    prompt: str,
    model,
) -> str:
    """直接使用 ChatAgent 执行全局审计，避免走完整 Workforce 流程。"""
    from camel.agents import ChatAgent
    from camel.messages import BaseMessage

    agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="GlobalCoverageAuditor",
            content=(
                "你是测试评审架构师。你的唯一任务是分析模块列表与 Bug 的关系，"
                "输出结构化的 GLOBAL_AUDIT_JSON。\n"
                "严禁输出 MANAGER_JUDGEMENT_JSON 或 WORKER_RESULT_JSON。\n"
                "你只需要在分析文本末尾追加一行 GLOBAL_AUDIT_JSON: {...}，"
                "且 JSON 必须包含以下字段：\n"
                "  in_scope_modules, out_of_scope_modules, priority_modules, "
                "missing_features, reason, confidence"
            ),
        ),
        model=model,
        tools=[],
    )
    response = agent.step(prompt)
    return (response.msgs[0].content if response.msgs else "")


def _run_global_coverage_audit(
    *,
    sheet_name: str,
    modules: List[dict],
    bug_info: dict,
    bug_context: dict,
    sheet_bug_profile: dict,
    product_name: str,
    pipeline,
    input_cache: dict,
) -> dict:
    if not GLOBAL_AUDIT_ENABLED or not modules:
        return {
            "enabled": False,
            "elapsed_seconds": 0.0,
            "review_text": "",
            "summary": "全局覆盖审计已关闭或无模块",
            "in_scope_keys": set(),
            "out_scope_keys": set(),
            "priority_keys": set(),
            "missing_features": [],
            "focus_context": "",
            "parse_ok": True,
            "parse_attempts": 0,
            "fallback_used": False,
            "confidence": 0.0,
            "raw_payload": {},
        }
    started = time.perf_counter()
    attempts = 1 + (GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL if GLOBAL_AUDIT_STRICT_PARSE else 0)
    used_attempts = 0
    parse_ok = False
    review_text = ""
    raw_payload: Dict[str, Any] = {}
    reason = ""
    confidence = 0.0
    in_scope_raw: List[str] = []
    out_scope_raw: List[str] = []
    priority_raw: List[str] = []
    missing_features: List[str] = []
    regression_scope_raw: List[str] = []

    # 获取 model 对象用于直接 ChatAgent 调用
    audit_model = getattr(pipeline, "model", None) if pipeline else None

    for attempt in range(1, attempts + 1):
        used_attempts = attempt
        retry_hint = ""
        if attempt > 1:
            retry_hint = (
                "上次返回未通过 JSON 校验。请只输出分析文本，"
                "并在末尾追加一行合法的 GLOBAL_AUDIT_JSON: {...}。"
                "禁止输出 MANAGER_JUDGEMENT_JSON。"
            )
        prompt = _build_global_audit_prompt(
            sheet_name=sheet_name,
            modules=modules,
            bug_info=bug_info,
            bug_context=bug_context,
            retry_hint=retry_hint,
        )
        if audit_model:
            # 直接 ChatAgent 调用，无需走完整 Workforce pipeline
            review_text = _run_global_audit_direct(
                prompt=prompt,
                model=audit_model,
            )
        else:
            result_data = _run_module_review_inline(
                pipeline=pipeline,
                full_text=prompt,
                sheet_bug_profile=sheet_bug_profile,
                input_cache=input_cache,
            )
            review_text = _extract_synthesis_review(str(result_data.get("review") or ""))
        payload = _extract_json_object(review_text)
        if not payload:
            logger.warning("[%s] 全局审计 JSON 解析失败 (attempt=%d/%d)", sheet_name, attempt, attempts)
            continue
        required_keys = {
            "in_scope_modules",
            "out_of_scope_modules",
            "priority_modules",
            "missing_features",
            "reason",
            "confidence",
        }
        if not required_keys.issubset(set(payload.keys())):
            logger.warning("[%s] 全局审计 JSON 字段不完整 (attempt=%d/%d)", sheet_name, attempt, attempts)
            continue
        if not isinstance(payload.get("in_scope_modules"), list):
            continue
        if not isinstance(payload.get("out_of_scope_modules"), list):
            continue
        if not isinstance(payload.get("priority_modules"), list):
            continue
        if not isinstance(payload.get("missing_features"), list):
            continue
        raw_payload = payload
        in_scope_raw = [str(x).strip() for x in payload.get("in_scope_modules", []) if str(x).strip()]
        out_scope_raw = [str(x).strip() for x in payload.get("out_of_scope_modules", []) if str(x).strip()]
        priority_raw = [str(x).strip() for x in payload.get("priority_modules", []) if str(x).strip()]
        missing_features = [str(x).strip() for x in payload.get("missing_features", []) if str(x).strip()]
        regression_scope_raw = [str(x).strip() for x in payload.get("regression_scope", []) if str(x).strip()]
        reason = str(payload.get("reason") or "").strip()
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
        except Exception:
            confidence = 0.0
        parse_ok = True
        break

    elapsed = time.perf_counter() - started
    in_scope_keys = _resolve_module_names(in_scope_raw, modules)
    out_scope_keys = _resolve_module_names(out_scope_raw, modules)
    priority_keys = _resolve_module_names(priority_raw, modules)
    regression_keys = _resolve_module_names(regression_scope_raw, modules)

    # Guard: cross-cutting test dimension modules must never be out_of_scope
    _CROSS_CUTTING_KEYWORDS = {
        "http版本", "http 版本", "http version", "ipv6", "ipv4", "双栈",
        "语言", "主题", "turbo", "ha", "stress", "upgrade",
        "兼容", "compat",
    }
    rescued = set()
    for okey in list(out_scope_keys):
        okey_lower = okey.lower()
        for kw in _CROSS_CUTTING_KEYWORDS:
            if kw in okey_lower:
                rescued.add(okey)
                break
    if rescued:
        out_scope_keys -= rescued
        in_scope_keys |= rescued
        logger.info(
            "Cross-cutting guard: rescued %d modules from out_of_scope: %s",
            len(rescued), rescued,
        )

    all_module_keys = {_module_key(m.get("item", ""), m.get("sub_item", "")) for m in modules}
    fallback_used = False
    if not in_scope_keys:
        in_scope_keys = set(all_module_keys)
        fallback_used = True
    if not priority_keys:
        priority_keys = set(sorted(in_scope_keys)[:min(len(in_scope_keys), 4)])
        fallback_used = True
    if not parse_ok and GLOBAL_AUDIT_STRICT_PARSE:
        fallback_used = True
        reason = reason or "全局审计结构化解析失败，已走保守降级策略（默认全量 in-scope）"

    focus_parts = []
    if reason:
        focus_parts.append(f"全局判断: {reason}")
    if missing_features:
        focus_parts.append("疑似缺失功能: " + "；".join(missing_features[:6]))
    if priority_keys:
        priority_names = []
        for module in modules:
            key = _module_key(module.get("item", ""), module.get("sub_item", ""))
            if key in priority_keys:
                priority_names.append(_module_display(module))
        if priority_names:
            focus_parts.append("优先深查模块: " + "；".join(priority_names[:8]))
    if regression_keys:
        regression_names = []
        for module in modules:
            key = _module_key(module.get("item", ""), module.get("sub_item", ""))
            if key in regression_keys:
                regression_names.append(_module_display(module))
        if regression_names:
            focus_parts.append("回归验证模块: " + "；".join(regression_names[:6]))
    focus_context = _truncate_text("\n".join(focus_parts), 900)
    logger.info(
        "[%s] 全局覆盖审计完成: parse_ok=%s attempts=%d in_scope=%d out_scope=%d priority=%d missing=%d elapsed=%.1fs",
        sheet_name,
        parse_ok,
        used_attempts,
        len(in_scope_keys),
        len(out_scope_keys),
        len(priority_keys),
        len(missing_features),
        elapsed,
    )
    return {
        "enabled": True,
        "elapsed_seconds": elapsed,
        "review_text": review_text,
        "summary": reason or "已完成全局覆盖审计",
        "in_scope_keys": in_scope_keys,
        "out_scope_keys": out_scope_keys,
        "priority_keys": priority_keys,
        "regression_keys": regression_keys,
        "missing_features": missing_features,
        "focus_context": focus_context,
        "parse_ok": parse_ok,
        "parse_attempts": used_attempts,
        "fallback_used": fallback_used,
        "confidence": confidence,
        "raw_payload": raw_payload,
    }


def _prepare_bug_output_dir(bug_output_dir: Path) -> None:
    """为当前 bug 运行准备干净输出目录，防止历史文件污染。"""
    if bug_output_dir.exists() and BUG_TO_CASE_CLEAN_OUTPUT_DIR:
        shutil.rmtree(bug_output_dir, ignore_errors=True)
        for _ in range(20):
            if not bug_output_dir.exists():
                break
            time.sleep(0.1)
    bug_output_dir.mkdir(parents=True, exist_ok=True)


def _build_pipeline(product_name: str):
    from INAGENT.review import ReviewPipeline
    from INAGENT.web.deps import get_llm_model, get_knowledge_router, check_rag_health

    router = get_knowledge_router()
    model = get_llm_model()

    # ---- RAG 健康检查（fail-stop）----
    health = check_rag_health(require_graphrag=True)
    logger.info("RAG 健康检查通过: %s", health)

    return ReviewPipeline(
        router=router,
        model=model,
        product_name=product_name,
    )

def run_prepare_script(bug_dir: Path) -> Path:
    """调用 prepare_bug_to_case.py 预处理数据"""
    logger.info("开始预处理 bug 测试用例数据...")
    prepare_script = ROOT / "INAGENT" / "scripts" / "prepare_bug_to_case.py"
    
    if not prepare_script.exists():
        logger.error(f"找不到预处理脚本: {prepare_script}")
        sys.exit(1)
        
    result = subprocess.run(
        [sys.executable, str(prepare_script), str(bug_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    
    if result.returncode != 0:
        logger.error(f"预处理执行失败:\n{result.stderr}")
        sys.exit(1)
        
    logger.info("预处理完成.")
    
    json_path = bug_dir / "bug_to_case.json"
    if not json_path.exists():
        logger.error(f"未生成预期的 {json_path} 文件。")
        sys.exit(1)
        
    return json_path

def build_test_cases_text_for_sheet(sheet_data: dict, sheet_name: str) -> str:
    """为整张 Sheet 构建一次性评审文本（manager-first 主路径）。"""
    modules = sheet_data.get("modules", [])
    module_count = len(modules)
    total_cases = 0
    blocks: List[str] = []
    for idx, module in enumerate(modules, start=1):
        item = module.get("item", "")
        sub_item = module.get("sub_item", "")
        cases = module.get("cases", [])
        total_cases += len(cases)
        types = module.get("type_distribution", {})
        prios = module.get("priority_distribution", {})
        type_str = ", ".join(f"{t}: {c}" for t, c in types.items())
        prio_str = ", ".join(f"{p}: {c}" for p, c in prios.items())
        lines = [
            f"### 模块 {idx}/{module_count}: {item} > {sub_item}",
            f"- 用例数: {len(cases)}",
            f"- 测试类型: {type_str}",
            f"- 优先级: {prio_str}",
            "",
            "| # | ID | Test Type | Priority | Description | Expected Result | Note |",
            "|---|-----|-----------|----------|-------------|-----------------|------|",
        ]
        for case in cases:
            lines.append(
                f"| {case.get('seq', '')} | {case.get('id', '')} "
                f"| {case.get('test_types', '')} | {case.get('priority', '')} "
                f"| {case.get('description', '')} | {case.get('expected_result', '')} "
                f"| {case.get('note', '')} |"
            )
        blocks.append("\n".join(lines))

    block_text = "\n\n".join(blocks)
    return (
        f"**Sheet**: {sheet_name}\n"
        f"**整体统计**: 模块 {module_count} 个，测试用例 {total_cases} 条\n\n"
        f"{block_text}\n\n"
        "请先从整体覆盖角度评估模块范围与缺口，再在同一次评审中完成质量检查，"
        "发现问题时请明确指出用例序号（#）与所属模块。"
    )


def _build_active_modules_text(
    sheet_data: dict,
    sheet_name: str,
    active_plan: List[dict],
    global_focus_context: str,
) -> str:
    """为活跃（非 skip）模块构建一次性评审文本，跳过模块不在文本中出现。"""
    module_count = len(active_plan)
    total_cases = 0
    blocks: List[str] = []
    for idx, row in enumerate(active_plan, start=1):
        module = row["module"]
        item = row["item"]
        sub_item = row["sub_item"]
        cases = module.get("cases", [])
        total_cases += len(cases)
        types = module.get("type_distribution", {})
        prios = module.get("priority_distribution", {})
        type_str = ", ".join(f"{t}: {c}" for t, c in types.items())
        prio_str = ", ".join(f"{p}: {c}" for p, c in prios.items())
        light_tag = "（轻量检查）" if row.get("mode") == "light" else ""
        lines = [
            f"### 模块 {idx}/{module_count}: {item} > {sub_item}{light_tag}",
            f"- 用例数: {len(cases)}",
            f"- 测试类型: {type_str}",
            f"- 优先级: {prio_str}",
            "",
            "| # | ID | Test Type | Priority | Description | Expected Result | Note |",
            "|---|-----|-----------|----------|-------------|-----------------|------|",
        ]
        for case in cases:
            lines.append(
                f"| {case.get('seq', '')} | {case.get('id', '')} "
                f"| {case.get('test_types', '')} | {case.get('priority', '')} "
                f"| {case.get('description', '')} | {case.get('expected_result', '')} "
                f"| {case.get('note', '')} |"
            )
        blocks.append("\n".join(lines))

    block_text = "\n\n".join(blocks)
    focus_section = ""
    if global_focus_context:
        focus_section = (
            f"\n## 全局审计重点关注\n\n{global_focus_context}\n\n"
        )
    return (
        f"**Sheet**: {sheet_name}\n"
        f"**活跃模块统计**: {module_count} 个模块，测试用例 {total_cases} 条\n"
        f"{focus_section}\n"
        f"{block_text}\n\n"
        "请先从整体覆盖角度评估模块范围与缺口，再在同一次评审中完成质量检查，"
        "发现问题时请明确指出用例序号（#）与所属模块。\n"
        "输出时请按 '### 模块 N/M: 项目 > 子项' 格式分段输出各模块评审结果。"
    )


_MODULE_SPLIT_RE = re.compile(
    r"^#{2,4}\s*模块\s*\d+\s*/\s*\d+\s*[:：\-—\s]",
    re.MULTILINE,
)


def _split_answer_to_modules(
    answer_text: str,
    active_plan: List[dict],
) -> Dict[int, str]:
    """按 '### 模块 N/M:' 正则切分 LLM 输出到各活跃模块。

    返回 {mi: 该模块段文本}。无法切分时返回空 dict（调用方回退到整体复制）。
    支持柔性段数匹配：段数 >= active_plan 时截断，段数不足时尽量按名称匹配。
    """
    splits = list(_MODULE_SPLIT_RE.finditer(answer_text))
    if len(splits) < 2:
        return {}
    segments: List[str] = []
    for i, m in enumerate(splits):
        start = m.start()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(answer_text)
        segments.append(answer_text[start:end].strip())

    # 精确匹配
    if len(segments) == len(active_plan):
        return {row["mi"]: seg for row, seg in zip(active_plan, segments)}

    # 段数 >= active_plan：截断多余段
    if len(segments) >= len(active_plan):
        return {row["mi"]: seg for row, seg in zip(active_plan, segments[:len(active_plan)])}

    # 段数不足但 >= 60%：按模块名最佳匹配
    if len(segments) >= max(2, int(len(active_plan) * 0.6)):
        result: Dict[int, str] = {}
        used = set()
        for row in active_plan:
            item_l = str(row.get("item") or "").lower()
            sub_item_l = str(row.get("sub_item") or "").lower()
            best_idx = -1
            best_score = 0
            for j, seg in enumerate(segments):
                if j in used:
                    continue
                seg_l = seg[:200].lower()
                score = 0
                if item_l and item_l in seg_l:
                    score += 2
                if sub_item_l and sub_item_l in seg_l:
                    score += 3
                if score > best_score:
                    best_score = score
                    best_idx = j
            if best_idx >= 0 and best_score > 0:
                result[row["mi"]] = segments[best_idx]
                used.add(best_idx)
        if result:
            return result

    return {}


def _extract_module_specific_answer(
    answer_text: str,
    row: dict,
    fallback_max_chars: int = 2400,
) -> str:
    """当 LLM 未按模块分段时，按模块名与用例序号提取局部证据。

    目标：避免把整份总评重复复制到每个模块，降低“同质化”。
    """
    item = str(row.get("item") or "").strip()
    sub_item = str(row.get("sub_item") or "").strip()
    module = row.get("module") or {}
    case_nums = []
    for c in (module.get("cases") or []):
        seq = str(c.get("seq", "")).strip()
        if seq.isdigit():
            case_nums.append(seq)
    case_nums = case_nums[:24]

    paras = [p.strip() for p in re.split(r"\n\s*\n", answer_text or "") if p.strip()]
    if not paras:
        return (
            f"### 模块证据提取: {item} > {sub_item}\n"
            "未收到可解析的模块级评审文本，请重试该 Sheet。"
        )

    item_l = item.lower()
    sub_item_l = sub_item.lower()
    picked: List[str] = []
    for p in paras:
        pl = p.lower()
        name_hit = (item_l and item_l in pl) or (sub_item_l and sub_item_l in pl)
        # 如“#1-#321 / #1–#321 / 用例#1-321”等全量范围描述，视为跨模块段落。
        has_global_case_range = bool(
            re.search(r"(?:用例\s*)?#\s*\d+\s*[–-]\s*#?\s*\d+", p)
        )
        case_hit = False
        if case_nums:
            for n in case_nums:
                if re.search(
                    rf"(?:#|用例\s*#?)\s*{re.escape(n)}\b(?!\s*[–-]\s*\d)",
                    p,
                ):
                    case_hit = True
                    break
        if (name_hit or case_hit) and not (has_global_case_range and not name_hit):
            picked.append(p)

    if not picked:
        # 没有模块命中时，给出明确的非重复结论
        case_count = len(case_nums) if case_nums else 0
        return (
            f"### 模块证据提取: {item} > {sub_item}\n"
            f"该模块（{case_count} 条用例）在本轮评审中未发现模块专属问题。\n"
            "建议优先关注重点模块的评审结论。"
        )

    merged = "\n\n".join(picked)
    if len(merged) > fallback_max_chars:
        merged = merged[:fallback_max_chars].rstrip() + "\n\n...(已截断)"
    return merged

def _run_module_review_inline(
    pipeline,
    full_text: str,
    sheet_bug_profile: dict,
    input_cache: dict,
) -> dict:
    result = pipeline.run(
        test_cases_text=full_text,
        bug_profile=sheet_bug_profile,
        input_cache=input_cache,
    )
    return result.to_dict()


def _write_benchmark_report(
    bug_output_dir: Path,
    benchmark: dict,
) -> None:
    gate_max_avg = GATE_MAX_AVG_SEC
    gate_max_p95 = GATE_MAX_P95_SEC
    gate_max_failed = GATE_MAX_FAILED_MODULES
    gate_min_manager_accept = GATE_MIN_MANAGER_FIRST_ACCEPT_RATE
    manager_accept_rate = _safe_float(benchmark.get("manager_first_accept_rate"), 0.0)
    gate_results = {
        "avg_ok": True if gate_max_avg <= 0 else benchmark["avg_module_seconds"] <= gate_max_avg,
        "p95_ok": True if gate_max_p95 <= 0 else benchmark["p95_module_seconds"] <= gate_max_p95,
        "failed_ok": benchmark["failed_modules"] <= gate_max_failed,
        "manager_accept_ok": True if gate_min_manager_accept <= 0 else manager_accept_rate >= gate_min_manager_accept,
    }
    benchmark["gates"] = {
        "max_avg_module_seconds": gate_max_avg,
        "max_p95_module_seconds": gate_max_p95,
        "max_failed_modules": gate_max_failed,
        "min_manager_first_accept_rate": gate_min_manager_accept,
        "result": gate_results,
        "all_passed": all(gate_results.values()),
    }

    json_file = bug_output_dir / "_benchmark.json"
    md_file = bug_output_dir / "_benchmark.md"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, ensure_ascii=False, indent=2)

    with open(md_file, "w", encoding="utf-8") as f:
        f.write("# 评审性能基准报告\n\n")
        f.write(f"- 总模块数: {benchmark['total_modules']}\n")
        f.write(f"- 成功模块数: {benchmark['success_modules']}\n")
        f.write(f"- 失败模块数: {benchmark['failed_modules']}\n")
        f.write(f"- 软失败模块数: {benchmark.get('soft_failed_modules', 0)}\n")
        f.write(f"- 全局缺失功能数: {benchmark.get('global_missing_features', 0)}\n")
        f.write(f"- 全局判定离题模块数: {benchmark.get('global_out_of_scope_modules', 0)}\n")
        f.write(f"- 全局优先模块数: {benchmark.get('global_priority_modules', 0)}\n")
        f.write(f"- 轻量检查模块数: {benchmark.get('planned_light_modules', 0)}\n")
        f.write(f"- 跳过深查模块数: {benchmark.get('planned_skipped_modules', 0)}\n")
        f.write(f"- 全局审计解析失败Sheet数: {benchmark.get('global_audit_parse_failed_sheets', 0)}\n")
        f.write(f"- 全局审计降级Sheet数: {benchmark.get('global_audit_fallback_sheets', 0)}\n")
        f.write(f"- 全局/模块冲突告警Sheet数: {benchmark.get('audit_consistency_alert_sheets', 0)}\n")
        f.write(f"- 模块低覆盖信号数: {benchmark.get('low_coverage_signal_modules', 0)}\n")
        f.write(f"- manager_first模块数: {benchmark.get('manager_first_modules', 0)}\n")
        f.write(f"- manager_first首轮通过数: {benchmark.get('manager_first_accept_count', 0)}\n")
        f.write(f"- 补充轮次数: {benchmark.get('supplement_round_count', 0)}\n")
        f.write(f"- 平均单模块耗时(s): {benchmark['avg_module_seconds']:.2f}\n")
        f.write(f"- P95 单模块耗时(s): {benchmark['p95_module_seconds']:.2f}\n")
        f.write(f"- 总耗时(s): {benchmark['total_seconds']:.2f}\n")
        f.write("\n## 验收门槛\n")
        f.write(
            f"- avg gate: <= {gate_max_avg if gate_max_avg > 0 else 'N/A'} "
            f"-> {'PASS' if gate_results['avg_ok'] else 'FAIL'}\n"
        )
        f.write(
            f"- p95 gate: <= {gate_max_p95 if gate_max_p95 > 0 else 'N/A'} "
            f"-> {'PASS' if gate_results['p95_ok'] else 'FAIL'}\n"
        )
        f.write(
            f"- failed modules gate: <= {gate_max_failed} "
            f"-> {'PASS' if gate_results['failed_ok'] else 'FAIL'}\n"
        )
        f.write(
            f"- manager first accept gate: >= {gate_min_manager_accept if gate_min_manager_accept > 0 else 'N/A'} "
            f"-> {'PASS' if gate_results['manager_accept_ok'] else 'FAIL'}\n"
        )
        f.write(
            f"- 总体: {'PASS' if benchmark['gates']['all_passed'] else 'FAIL'}\n"
        )


class _NoopProgress:
    def update(self, _n: int = 1) -> None:
        return

    def set_postfix_str(self, _text: str) -> None:
        return

    def close(self) -> None:
        return


def _make_progress_bar(total: int, desc: str):
    if not ENABLE_PROGRESS_BAR:
        return _NoopProgress()
    if tqdm is None:
        logger.info("[%s] 进度条降级为日志模式（未安装 tqdm）", desc)
        return _NoopProgress()
    return tqdm(total=total, desc=desc, unit="module")


def _run_module_with_retry(
    *,
    label: str,
    sheet_name: str,
    item: str,
    sub_item: str,
    module: dict,
    full_text: str,
    sheet_bug_profile: dict,
    manager_taskpack: Optional[Dict[str, Any]],
    product_name: str,
    pipeline,
    input_cache: dict,
) -> dict:
    total_attempts = MODULE_RETRY_TIMES + 1
    last_error = ""
    last_traceback = ""
    for attempt in range(1, total_attempts + 1):
        started = time.perf_counter()
        try:
            runtime_bug_profile = dict(sheet_bug_profile or {})
            if manager_taskpack:
                runtime_bug_profile["__manager_taskpack__"] = manager_taskpack
            else:
                runtime_bug_profile.pop("__manager_taskpack__", None)
            result_data = _run_module_review_inline(
                pipeline=pipeline,
                full_text=full_text,
                sheet_bug_profile=runtime_bug_profile,
                input_cache=input_cache,
            )
            elapsed = time.perf_counter() - started
            elapsed_hint = result_data.get("elapsed_seconds")
            if elapsed_hint is not None:
                logger.info(
                    "[%s] pipeline_elapsed=%ss total_elapsed=%.1fs",
                    label,
                    elapsed_hint,
                    elapsed,
                )
            rag_status = result_data.get("rag_status") or {}
            wf_failed_tasks = int(rag_status.get("task_failed_count") or 0)
            failed_tasks = rag_status.get("failed_tasks") or []
            failed_task_brief = []
            failed_task_struct = []
            for task in failed_tasks[:3]:
                if isinstance(task, dict):
                    task_id = str(task.get("task_id") or "?")
                    err = str(task.get("error") or "").strip()
                    err_brief = re.sub(r"\s+", " ", err)[:120] if err else "unknown"
                    failed_task_brief.append(f"{task_id}: {err_brief}")
                    failed_task_struct.append(
                        {
                            "task_id": task_id,
                            "error": err_brief,
                            "retried": attempt > 1,
                            "impact": "soft_failed_module_output",
                        }
                    )
                else:
                    failed_task_brief.append(str(task))
                    failed_task_struct.append(
                        {
                            "task_id": "?",
                            "error": str(task),
                            "retried": attempt > 1,
                            "impact": "soft_failed_module_output",
                        }
                    )
            soft_fail_reason = "；".join(failed_task_brief) if failed_task_brief else ""
            if wf_failed_tasks > 0:
                logger.warning(
                    "[%s] Workforce 子任务失败次数=%d（模块产物已生成，标记为 soft-failed）%s",
                    label,
                    wf_failed_tasks,
                    f"；详情: {soft_fail_reason}" if soft_fail_reason else "",
                )
                if FAIL_ON_SOFT_FAILED:
                    debug_obj = {
                        "label": label,
                        "sheet": sheet_name,
                        "module": f"{item} > {sub_item}",
                        "bug_profile": sheet_bug_profile,
                        "pipeline_result": result_data,
                        "attempt": attempt,
                        "total_attempts": total_attempts,
                        "soft_fail_reason": soft_fail_reason,
                        "soft_fail_tasks": failed_task_struct,
                        "fail_on_soft_failed": True,
                    }
                    return {
                        "ok": False,
                        "answer": (
                            "**评审失败**: Workforce 子任务失败（严格模式）\n\n"
                            f"- 失败子任务数: {wf_failed_tasks}\n"
                            f"- 失败摘要: {soft_fail_reason or '见 debug 日志'}"
                        ),
                        "elapsed": elapsed,
                        "soft_failed": False,
                        "manager_judgement": ManagerJudgementContract(parse_ok=False),
                        "debug_obj": debug_obj,
                    }

            # Pipeline 子任务状态仅记日志，不注入报告
            if wf_failed_tasks > 0:
                logger.warning(
                    "Pipeline 失败子任务数: %d, 原因: %s",
                    wf_failed_tasks,
                    soft_fail_reason or "无",
                )
            answer = ""
            if REPORT_INCLUDE_EVIDENCE_SUMMARY:
                answer += (
                    "### 证据摘要（检索上下文）\n\n"
                    f"{_compact_knowledge(result_data.get('knowledge') or '', domain_keywords=[item, sub_item, sheet_name])}\n\n"
                )
            answer += (
                "### 评审结果\n\n"
                f"{_extract_synthesis_review(result_data.get('review', ''))}"
            )
            manager_judgement = _parse_manager_judgement_from_text(result_data.get("review", ""))
            debug_obj = {
                "label": label,
                "sheet": sheet_name,
                "module": f"{item} > {sub_item}",
                "bug_profile": runtime_bug_profile,
                "pipeline_result": result_data,
                "attempt": attempt,
                "total_attempts": total_attempts,
                "soft_fail_reason": soft_fail_reason,
                "soft_fail_tasks": failed_task_struct,
                "manager_judgement": {
                    "parse_ok": manager_judgement.parse_ok,
                    "pass": manager_judgement.passed,
                    "confidence": manager_judgement.confidence,
                    "failed_gates": manager_judgement.failed_gates,
                    "unresolved": manager_judgement.unresolved,
                    "supplement_tasks": manager_judgement.supplement_tasks,
                },
            }
            if attempt > 1:
                logger.info("[%s] 重试成功（第 %d/%d 次）", label, attempt, total_attempts)
            return {
                "ok": True,
                "answer": answer,
                "elapsed": elapsed,
                "soft_failed": wf_failed_tasks > 0,
                "soft_fail_tasks": failed_task_struct,
                "manager_judgement": manager_judgement,
                "debug_obj": debug_obj,
                "rag_status": rag_status,
            }
        except Exception as e:
            elapsed = time.perf_counter() - started
            last_error = str(e)
            last_traceback = traceback.format_exc()
            logger.error(
                "[%s] 评审失败（尝试 %d/%d，耗时 %.1fs）: %s\n%s",
                label,
                attempt,
                total_attempts,
                elapsed,
                e,
                last_traceback,
            )
            if attempt < total_attempts and MODULE_RETRY_BACKOFF_SECONDS > 0:
                sleep_s = MODULE_RETRY_BACKOFF_SECONDS * attempt
                logger.info("[%s] 将在 %.1fs 后重试", label, sleep_s)
                time.sleep(sleep_s)

    debug_obj = {
        "label": label,
        "sheet": sheet_name,
        "module": f"{item} > {sub_item}",
        "bug_profile": sheet_bug_profile,
        "error": last_error,
        "traceback": last_traceback,
        "attempt": total_attempts,
        "total_attempts": total_attempts,
    }
    return {
        "ok": False,
        "answer": f"**评审失败**: {last_error}",
        "elapsed": 0.0,
        "soft_failed": False,
        "soft_fail_tasks": [],
        "manager_judgement": ManagerJudgementContract(parse_ok=False),
        "debug_obj": debug_obj,
    }


def _append_module_result(
    *,
    sheet_file: Path,
    module_order: int,
    module_total: int,
    item: str,
    sub_item: str,
    case_count: int,
    answer: str,
    original_index: Optional[int] = None,
) -> None:
    with open(sheet_file, "a", encoding="utf-8") as f:
        f.write("---\n\n")
        origin_note = f" (原始模块 #{original_index})" if original_index is not None else ""
        f.write(
            f"## 模块 {module_order}/{module_total}: {item} > {sub_item} "
            f"({case_count} 条){origin_note}\n\n"
        )
        f.write(f"### 详细评审结果\n\n{answer}\n\n")


def _review_single_sheet(
    sheet_data: dict,
    bug_context: dict,
    bug_profile: dict,
    bug_info: dict,
    bug_output_dir: Path,
    product_name: str,
    run_id: str,
    run_started_at: str,
    shared_pipeline=None,
) -> dict:
    sheet_started_at = time.perf_counter()
    summary = sheet_data.get("summary", {})
    sheet_name = summary.get("sheet_name", "Unknown_Sheet")
    modules = sheet_data.get("modules", [])
    logger.info(
        f"\n[{sheet_name}] 开始评审: {summary.get('total_cases', 0)} 条用例, "
        f"{summary.get('module_count', 0)} 个模块"
    )

    current_sheet_role = ""
    sheet_roles = bug_context.get("sheet_roles", {})
    for role_info in sheet_roles.values():
        if sheet_name in role_info.get("names", []):
            current_sheet_role = role_info.get("role", "")

    sheet_bug_profile = bug_profile.copy()
    if current_sheet_role:
        sheet_bug_profile["Sheet_Context"] = (
            f"当前评审的 Sheet [{sheet_name}] 定位为: {current_sheet_role}"
        )

    sheet_file = bug_output_dir / f"review_{sheet_name.replace(' ', '_')}.md"
    debug_file = bug_output_dir / f"review_debug_{sheet_name.replace(' ', '_')}.jsonl"
    findings_file = bug_output_dir / f"review_findings_{sheet_name.replace(' ', '_')}.json"

    with open(sheet_file, "w", encoding="utf-8") as f:
        f.write(f"# 测试用例 Bug-to-Case 评审: {sheet_name}\n\n")
        f.write(f"- 运行ID: {run_id}\n")
        f.write(f"- 运行开始时间(UTC): {run_started_at}\n")
        f.write(f"- 关联 Bug: #{bug_info.get('bug_id', '?')}\n")
        f.write(f"- 用例总数: {summary.get('total_cases', 0)}\n")
        f.write(f"- 模块数: {summary.get('module_count', 0)}\n")
        if current_sheet_role:
            f.write(f"- Sheet 定位: {current_sheet_role}\n")
        f.write("\n## Bug 详情\n")
        for k, v in bug_info.items():
            if k != "raw_text":
                f.write(f"- **{k}**: {v}\n")
        f.write("\n")

    input_cache: dict = {}
    pipeline = shared_pipeline
    if pipeline is None:
        raise RuntimeError("评审要求提供 shared_pipeline")
    module_costs = []
    failed_modules = 0
    soft_failed_modules = 0
    soft_fail_details: List[dict] = []

    global_audit = _run_global_coverage_audit(
        sheet_name=sheet_name,
        modules=modules,
        bug_info=bug_info,
        bug_context=bug_context,
        sheet_bug_profile=sheet_bug_profile,
        product_name=product_name,
        pipeline=pipeline,
        input_cache=input_cache,
    )

    _append_debug_event(
        debug_file=debug_file,
        event="global_audit",
        run_id=run_id,
        sheet_name=sheet_name,
        payload={
            "elapsed_seconds": global_audit.get("elapsed_seconds", 0.0),
            "summary": global_audit.get("summary", ""),
            "missing_features": global_audit.get("missing_features", []),
            "in_scope_count": len(global_audit.get("in_scope_keys", set())),
            "out_scope_count": len(global_audit.get("out_scope_keys", set())),
            "priority_count": len(global_audit.get("priority_keys", set())),
            "review_text": global_audit.get("review_text", ""),
            "parse_ok": bool(global_audit.get("parse_ok", False)),
            "parse_attempts": int(global_audit.get("parse_attempts", 0)),
            "fallback_used": bool(global_audit.get("fallback_used", False)),
            "confidence": float(global_audit.get("confidence", 0.0)),
        },
    )

    out_scope_keys = set(global_audit.get("out_scope_keys", set()))
    priority_keys = set(global_audit.get("priority_keys", set()))
    review_plan: List[dict] = []
    uncertain_mode = "full" if GLOBAL_AUDIT_UNCERTAIN_POLICY == "full" else "light"
    for mi, module in enumerate(modules):
        item = module.get("item", "")
        sub_item = module.get("sub_item", "")
        key = _module_key(item, sub_item)
        case_count = int(module.get("case_count") or len(module.get("cases") or []))
        mode = "full"
        if key in out_scope_keys:
            mode = "skip"
        elif case_count <= GLOBAL_AUDIT_LIGHT_CHECK_CASE_THRESHOLD:
            mode = "light"
        review_plan.append(
            {
                "mi": mi,
                "module": module,
                "item": item,
                "sub_item": sub_item,
                "key": key,
                "case_count": case_count,
                "mode": mode,
                "module_name": f"{item} > {sub_item}",
            }
        )

    audit_confidence = _safe_float(global_audit.get("confidence"), 0.0)
    if audit_confidence < GLOBAL_AUDIT_MIN_SKIP_CONFIDENCE:
        for row in review_plan:
            if row.get("mode") == "skip":
                row["mode"] = uncertain_mode
    else:
        skip_rows = [row for row in review_plan if row.get("mode") == "skip"]
        max_skip_by_ratio = int(len(review_plan) * GLOBAL_AUDIT_MAX_SKIP_RATIO)
        max_skip_by_active = max(0, len(review_plan) - int(len(review_plan) * GLOBAL_AUDIT_MIN_ACTIVE_RATIO))
        allowed_skip = max(0, min(max_skip_by_ratio, max_skip_by_active))
        if len(skip_rows) > allowed_skip:
            for row in skip_rows[allowed_skip:]:
                row["mode"] = uncertain_mode

    review_plan.sort(key=lambda x: (x["key"] not in priority_keys, x["mi"]))

    # Safety guard: modules with ≥5 cases should never be skipped
    for row in review_plan:
        if row.get("mode") == "skip" and int(row.get("case_count", 0)) >= 5:
            row["mode"] = "light"
            logger.info(
                "Safety override: %s forced to 'light' (case_count=%s)",
                row["key"], row["case_count"],
            )

    for order, row in enumerate(review_plan, start=1):
        row["order"] = order
    planned_skipped = sum(1 for row in review_plan if row["mode"] == "skip")
    planned_light = sum(1 for row in review_plan if row["mode"] == "light")
    low_coverage_signal_modules = 0
    manager_first_modules = 0
    supplement_round_count = 0
    manager_first_accept_count = 0

    for row in review_plan:
        row["manager_plan"] = None
        row["manager_taskpack"] = None

    # 全局覆盖审计信息仅写入 debug log，不写入面向被评审人的报告
    _audit_summary = global_audit.get("summary", "")
    _audit_missing = global_audit.get("missing_features", [])
    if _audit_summary or _audit_missing or priority_keys:
        logger.info(
            "全局审计摘要: %s | 缺失特性: %s | 重点模块(%d): %s | 跳过: %d",
            _audit_summary[:200] if _audit_summary else "(无)",
            "；".join(str(m) for m in _audit_missing[:8]) if _audit_missing else "(无)",
            len(priority_keys),
            ", ".join(sorted(priority_keys)[:10]),
            planned_skipped,
        )

    progress = _make_progress_bar(len(review_plan), f"{sheet_name} 评审进度")

    skipped_modules = []
    try:
        # ---- 0. skip 模块: 收集到列表，报告末尾统一输出 ----
        active_plan = []
        for row in review_plan:
            if row["mode"] == "skip":
                skipped_modules.append(row)
                _append_debug_event(
                    debug_file=debug_file,
                    event="module_skipped",
                    run_id=run_id,
                    sheet_name=sheet_name,
                    payload={
                        "sheet": sheet_name,
                        "module": f"{row['item']} > {row['sub_item']}",
                        "mode": "skip",
                        "reason": "out_of_scope",
                    },
                )
                progress.update(1)
                progress.set_postfix_str(
                    f"skip | fail={failed_modules} soft={soft_failed_modules}"
                )
            else:
                active_plan.append(row)

        if not active_plan:
            logger.info("[%s] 所有模块均被跳过，无需 LLM 评审", sheet_name)
        else:
            # ---- 1. Manager 统一整体验审 ----
            manager_plan = _build_manager_plan_for_sheet(
                sheet_name=sheet_name,
                modules=modules,
                bug_context=bug_context,
                global_audit=global_audit,
                pipeline=pipeline,
            )
            manager_taskpack = {
                "manager_plan": manager_plan.to_dict(),
                "worker_tasks": {k: v.to_dict() for k, v in manager_plan.worker_tasks.items()},
            }
            module_total_cases = sum(r["case_count"] for r in active_plan)
            label = f"{sheet_name} [1/1] 全量整体验审"
            logger.info(
                "[%s] 开始整体验审（manager planner -> workers -> judge），活跃模块=%d",
                sheet_name, len(active_plan),
            )
            full_text = _build_active_modules_text(
                sheet_data=sheet_data,
                sheet_name=sheet_name,
                active_plan=active_plan,
                global_focus_context=global_audit.get("focus_context", ""),
            )
            sheet_module = {"case_count": module_total_cases}
            outcome = _run_module_with_retry(
                label=label,
                sheet_name=sheet_name,
                item="整体覆盖",
                sub_item="全部模块",
                module=sheet_module,
                full_text=full_text,
                sheet_bug_profile=sheet_bug_profile,
                manager_taskpack=manager_taskpack,
                product_name=product_name,
                pipeline=pipeline,
                input_cache=input_cache,
            )
            manager_judgement = outcome.get("manager_judgement")
            if isinstance(manager_judgement, ManagerJudgementContract):
                manager_first_modules = 1
                need_supplement = (
                    (not manager_judgement.parse_ok and MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE)
                    or (not manager_judgement.passed)
                    or (manager_judgement.confidence < MANAGER_FIRST_MIN_CONFIDENCE)
                    or (len(manager_judgement.unresolved) > MANAGER_FIRST_MAX_UNRESOLVED)
                )
                if not need_supplement:
                    manager_first_accept_count = 1
                if need_supplement and MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS > 0:
                    elapsed_sheet = time.perf_counter() - sheet_started_at
                    if elapsed_sheet >= PERF_TARGET_SHEET_SECONDS:
                        logger.info(
                            "[%s] 达到sheet时延预算(%.1fs/%.1fs)，跳过补充轮",
                            sheet_name,
                            elapsed_sheet,
                            PERF_TARGET_SHEET_SECONDS,
                        )
                        need_supplement = False
                if need_supplement and MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS > 0:
                    supplement_round_count = 1
                    supplement_started_at = time.perf_counter()
                    supplement_pack = _build_supplement_taskpack(
                        manager_taskpack,
                        manager_judgement.supplement_tasks,
                    )
                    supplement_text = (
                        full_text
                        + "\n\n## 补充评审要求\n"
                        + "请仅针对 manager 指定补充任务继续输出，避免重复整份报告。"
                    )
                    supplement_outcome = _run_module_with_retry(
                        label=f"{label} [补充轮]",
                        sheet_name=sheet_name,
                        item="整体覆盖",
                        sub_item="全部模块",
                        module=sheet_module,
                        full_text=supplement_text,
                        sheet_bug_profile=sheet_bug_profile,
                        manager_taskpack=supplement_pack,
                        product_name=product_name,
                        pipeline=pipeline,
                        input_cache=input_cache,
                    )
                    if supplement_outcome.get("ok"):
                        supplement_elapsed = time.perf_counter() - supplement_started_at
                        if supplement_elapsed > PERF_MAX_SUPPLEMENT_SECONDS:
                            logger.warning(
                                "[%s] 补充轮耗时 %.1fs 超过预算 %.1fs",
                                sheet_name,
                                supplement_elapsed,
                                PERF_MAX_SUPPLEMENT_SECONDS,
                            )
                        outcome = supplement_outcome
                        manager_judgement = outcome.get("manager_judgement")
                        if (
                            isinstance(manager_judgement, ManagerJudgementContract)
                            and manager_judgement.passed
                            and manager_judgement.confidence >= MANAGER_FIRST_MIN_CONFIDENCE
                        ):
                            manager_first_accept_count = 1
            module_costs.append(outcome["elapsed"])
            if outcome["soft_failed"]:
                soft_failed_modules += 1
                soft_fail_details.append(
                    {
                        "module": "整体覆盖 > 全部模块",
                        "order": 1,
                        "tasks": outcome.get("soft_fail_tasks") or [],
                    }
                )
            if not outcome["ok"]:
                failed_modules += 1
            debug_obj = dict(outcome.get("debug_obj") or {})
            event_name = str(debug_obj.pop("event", "module_review")).strip() or "module_review"
            _append_debug_event(
                debug_file=debug_file,
                event=event_name,
                run_id=run_id,
                sheet_name=sheet_name,
                payload=debug_obj,
            )
            # ---- 2. 直接输出整体评审意见（不再按模块拆分）----
            raw_answer_text = str(outcome.get("answer") or "")
            answer_text = _strip_internal_markers(raw_answer_text)
            with open(sheet_file, "a", encoding="utf-8") as f:
                f.write("\n## 评审意见\n\n")
                f.write(answer_text)
                f.write("\n\n")
            findings = _extract_structured_findings(raw_answer_text)
            findings, dropped_titles = _filter_hallucinated_findings(
                findings,
                raw_answer_text,
            )
            with open(findings_file, "w", encoding="utf-8") as ff:
                json.dump(
                    {
                        "run_id": run_id,
                        "sheet_name": sheet_name,
                        "bug_id": str(bug_info.get("bug_id") or "").strip(),
                        "finding_count": len(findings),
                        "findings": findings,
                    },
                    ff,
                    ensure_ascii=False,
                    indent=2,
                )
            _append_debug_event(
                debug_file=debug_file,
                event="structured_findings",
                run_id=run_id,
                sheet_name=sheet_name,
                payload={
                    "sheet": sheet_name,
                    "finding_count": len(findings),
                    "filtered_out_count": len(dropped_titles),
                    "filtered_out_titles": dropped_titles,
                    "path": str(findings_file),
                },
            )
            if ("零覆盖" in answer_text) or ("未覆盖" in answer_text) or ("无覆盖" in answer_text):
                low_coverage_signal_modules += 1
            for row in active_plan:
                progress.update(1)
                progress.set_postfix_str(
                    f"done | fail={failed_modules} soft={soft_failed_modules}"
                )
    finally:
        progress.close()

    effective_total_modules = len(review_plan)
    consistency_alert = (
        len(global_audit.get("missing_features", [])) == 0
        and low_coverage_signal_modules >= max(1, effective_total_modules // 4)
    )
    # ---- 跳过模块在报告末尾统一列出 ----
    if skipped_modules:
        with open(sheet_file, "a", encoding="utf-8") as f:
            f.write("---\n\n")
            f.write("## 低相关模块（已跳过深度评审）\n\n")
            for row in skipped_modules:
                f.write(f"- {row['item']} > {row['sub_item']} ({row['case_count']} 条)\n")
            f.write("\n")

    if REPORT_INCLUDE_RUN_STATUS_SECTION:
        with open(sheet_file, "a", encoding="utf-8") as f:
            f.write("---\n\n")
            f.write("## 运行状态\n\n")
            f.write(f"- run_id: {run_id}\n")
            f.write(f"- completed_modules: {effective_total_modules}\n")
            f.write(f"- failed_modules: {failed_modules}\n")
            f.write(f"- soft_failed_modules: {soft_failed_modules}\n")
            f.write(f"- global_missing_features: {len(global_audit.get('missing_features', []))}\n")
            f.write(f"- global_out_of_scope_modules: {len(out_scope_keys)}\n")
            f.write(f"- global_priority_modules: {len(priority_keys)}\n")
            f.write(f"- planned_light_modules: {planned_light}\n")
            f.write(f"- planned_skipped_modules: {planned_skipped}\n")
            f.write(f"- manager_first_modules: {manager_first_modules}\n")
            f.write(f"- manager_first_accept_count: {manager_first_accept_count}\n")
            f.write(f"- supplement_round_count: {supplement_round_count}\n")
            f.write(f"- low_coverage_signal_modules: {low_coverage_signal_modules}\n")
            f.write(f"- audit_consistency_alert: {bool(consistency_alert)}\n")
            if consistency_alert:
                f.write(
                    "- audit_consistency_reason: 全局审计显示缺失=0，但模块级出现密集低覆盖信号，请复核全局审计结果。\n"
                )
            status = "completed_with_failures" if (failed_modules or soft_failed_modules) else "completed"
            f.write(f"- status: {status}\n")
            f.write(f"- finished_at_utc: {_utc_now_iso()}\n\n")
            if soft_fail_details:
                f.write("### 软失败明细\n\n")
                for detail in sorted(soft_fail_details, key=lambda x: int(x.get("order") or 0)):
                    f.write(
                        f"- 模块[{detail.get('order', '?')}]: {detail.get('module', '?')} | "
                        f"失败子任务数: {len(detail.get('tasks') or [])}\n"
                    )
                    for task in (detail.get("tasks") or []):
                        f.write(
                            f"  - task={task.get('task_id', '?')} | retried={bool(task.get('retried', False))} | "
                            f"impact={task.get('impact', 'soft_failed_module_output')} | "
                            f"error={task.get('error', 'unknown')}\n"
                        )
                f.write("\n")

    return {
        "sheet_name": sheet_name,
        "summary": summary,
        "module_costs": module_costs,
        "failed_modules": failed_modules,
        "soft_failed_modules": soft_failed_modules,
        "total_modules": effective_total_modules,
        "global_missing_features": len(global_audit.get("missing_features", [])),
        "global_out_of_scope_modules": len(out_scope_keys),
        "global_priority_modules": len(priority_keys),
        "planned_light_modules": planned_light,
        "planned_skipped_modules": planned_skipped,
        "soft_fail_detail_count": len(soft_fail_details),
        "manager_first_modules": manager_first_modules,
        "manager_first_accept_count": manager_first_accept_count,
        "supplement_round_count": supplement_round_count,
        "global_audit_parse_ok": bool(global_audit.get("parse_ok", False)),
        "global_audit_fallback_used": bool(global_audit.get("fallback_used", False)),
        "audit_consistency_alert": bool(
            len(global_audit.get("missing_features", [])) == 0
            and low_coverage_signal_modules >= max(1, effective_total_modules // 4)
        ),
        "low_coverage_signal_modules": low_coverage_signal_modules,
    }


def _find_qdrant_lock_holder(lock_file: Path) -> Optional[int]:
    """查找持有 Qdrant 锁文件的进程 PID（仅 Windows）。"""
    try:
        import subprocess as _sp
        # 使用 handle.exe 或 powershell 查找文件句柄持有者
        # 优先用 powershell Get-Process 扫描 python 进程中打开该文件的
        result = _sp.run(
            ["powershell", "-Command",
             f"Get-Process python* -ErrorAction SilentlyContinue | "
             f"Where-Object {{$_.Id -ne {os.getpid()}}} | "
             f"Select-Object Id,ProcessName,StartTime | "
             f"Format-Table -AutoSize"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            logger.info("发现其他 Python 进程:\n%s", result.stdout.strip())
            # 解析第一个非 self PID
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    pid = int(parts[0])
                    if pid != os.getpid():
                        return pid
    except Exception:
        pass
    return None


def _cleanup_stale_qdrant_lock():
    """清理前次异常退出残留的 Qdrant 文件锁。

    Qdrant 本地模式使用 portalocker 文件锁，如果前次进程被 Ctrl+C 或异常中断，
    锁文件可能残留导致 AlreadyLocked 错误。本函数尝试非阻塞获取锁：
    - 能获取 → 孤立锁，删除
    - 不能获取 → 找到持锁进程，提示用户确认是否杀掉
    """
    qdrant_dir = Path(os.environ.get(
        "QDRANT_LOCAL_DIR",
        str(Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"),
    ))
    lock_file = qdrant_dir / ".lock"
    if not lock_file.exists():
        return
    try:
        import portalocker
        fh = open(lock_file, "r")
        try:
            portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
            portalocker.unlock(fh)
            fh.close()
            lock_file.unlink(missing_ok=True)
            logger.info("已清理孤立 Qdrant 锁: %s", lock_file)
        except portalocker.LockException:
            fh.close()
            holder_pid = _find_qdrant_lock_holder(lock_file)
            if holder_pid:
                logger.warning(
                    "Qdrant 锁被 PID %d 持有 (%s)",
                    holder_pid, lock_file,
                )
                try:
                    answer = input(
                        f"[Qdrant] 检测到旧进程 PID {holder_pid} 持有锁，"
                        f"是否强行终止该进程？(y/N): "
                    ).strip().lower()
                except EOFError:
                    answer = ""
                if answer in ("y", "yes"):
                    import signal
                    try:
                        os.kill(holder_pid, signal.SIGTERM)
                        logger.info("已发送 SIGTERM 到 PID %d", holder_pid)
                        time.sleep(1)
                        # 再次尝试清理锁
                        if lock_file.exists():
                            lock_file.unlink(missing_ok=True)
                            logger.info("锁文件已清理")
                    except OSError as e:
                        logger.error("无法终止 PID %d: %s", holder_pid, e)
                        sys.exit(1)
                else:
                    logger.error("Qdrant 锁冲突未解决，退出")
                    sys.exit(1)
            else:
                logger.warning(
                    "Qdrant 锁文件被未知进程持有: %s — 请手动检查后重试",
                    lock_file,
                )
                sys.exit(1)
    except ImportError:
        logger.debug("portalocker 未安装，跳过 Qdrant 锁清理")
    except OSError as e:
        logger.debug("Qdrant 锁清理异常（不影响运行）: %s", e)


def execute_review(json_path: Path, output_dir: Path, bug_dir: Optional[Path] = None):
    """读取预处理后的 JSON 并执行评审"""
    started_all = time.perf_counter()

    # ---- 0. Pre-run 清理 & 健康检查 ----
    _cleanup_stale_qdrant_lock()

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("无法读取 bug_to_case.json: %s", e)
        sys.exit(1)

    bug_info = data.get("bug_info", {})

    # 构造 bug profile, 包含 bug context 信息
    bug_profile = bug_info.copy()
    bug_context = dict(data.get("bug_context", {}) or {})
    if bug_info.get("title"):
        bug_profile["Summary"] = bug_info.get("title")
    if bug_info.get("bug_id"):
        bug_profile["Bug ID"] = bug_info.get("bug_id")
    
    if bug_context:
        bug_profile["Review_Focus"] = bug_context.get("review_focus", "")
        kq = bug_context.get("key_questions", [])
        bug_profile["Key_Questions"] = (" \n- " + "\n- ".join(kq)) if kq else ""
        if bug_context.get("bug_detail_raw"):
            bug_profile["Bug_Detail_Raw"] = str(bug_context.get("bug_detail_raw") or "")
    
    logger.info(f"关联 Bug ID: {bug_info.get('bug_id', 'Unknown')}")

    # 初始化组件
    product_name = env_utils.get_product_name()

    bug_dir_name = Path(json_path).parent.name
    bug_output_dir = output_dir / f"{bug_dir_name}_review"
    _prepare_bug_output_dir(bug_output_dir)
    run_id = uuid.uuid4().hex[:12]
    run_started_at = _utc_now_iso()
    run_meta_file = bug_output_dir / "_run_meta.json"
    with open(run_meta_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "run_id": run_id,
                "status": "running",
                "started_at_utc": run_started_at,
                "json_path": str(json_path),
                "clean_output_dir": BUG_TO_CASE_CLEAN_OUTPUT_DIR,
                "module_retry_times": MODULE_RETRY_TIMES,
                "module_retry_backoff_seconds": MODULE_RETRY_BACKOFF_SECONDS,
                "global_audit_enabled": GLOBAL_AUDIT_ENABLED,
                "global_audit_max_modules_in_prompt": GLOBAL_AUDIT_MAX_MODULES_IN_PROMPT,
                "global_audit_max_context_chars": GLOBAL_AUDIT_MAX_CONTEXT_CHARS,
                "global_audit_light_check_case_threshold": GLOBAL_AUDIT_LIGHT_CHECK_CASE_THRESHOLD,
                "global_audit_strict_parse": GLOBAL_AUDIT_STRICT_PARSE,
                "global_audit_retry_on_parse_fail": GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL,
                "evidence_strict_domain_filter": EVIDENCE_STRICT_DOMAIN_FILTER,
                "manager_first_require_strict_plan_parse": MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE,
                "manager_first_max_supplement_rounds": MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS,
                "manager_first_min_confidence": MANAGER_FIRST_MIN_CONFIDENCE,
                "manager_first_max_unresolved": MANAGER_FIRST_MAX_UNRESOLVED,
                "manager_first_max_taskpack_chars": MANAGER_FIRST_MAX_TASKPACK_CHARS,
                "scope_min_out_scope_confidence": GLOBAL_AUDIT_MIN_SKIP_CONFIDENCE,
                "scope_max_out_scope_ratio": GLOBAL_AUDIT_MAX_SKIP_RATIO,
                "scope_min_active_module_ratio": GLOBAL_AUDIT_MIN_ACTIVE_RATIO,
                "scope_uncertain_policy": GLOBAL_AUDIT_UNCERTAIN_POLICY,
                "report_include_evidence_summary": REPORT_INCLUDE_EVIDENCE_SUMMARY,
                "report_include_run_status_section": REPORT_INCLUDE_RUN_STATUS_SECTION,
                "target_sheet_seconds": PERF_TARGET_SHEET_SECONDS,
                "max_supplement_seconds": PERF_MAX_SUPPLEMENT_SECONDS,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    grand_total = 0
    all_stats = {}

    # 预热并全局复用 pipeline，避免重复初始化 RAG/GraphRAG。
    init_started = time.perf_counter()
    shared_pipeline = _build_pipeline(product_name)
    logger.info(
        f"pipeline 初始化耗时 {time.perf_counter() - init_started:.2f}s"
    )

    sheets = data.get("sheets", [])
    results = []

    for sheet_data in sheets:
        results.append(
            _review_single_sheet(
                sheet_data=sheet_data,
                bug_context=bug_context,
                bug_profile=bug_profile,
                bug_info=bug_info,
                bug_output_dir=bug_output_dir,
                product_name=product_name,
                run_id=run_id,
                run_started_at=run_started_at,
                shared_pipeline=shared_pipeline,
            )
        )

    module_costs = []
    failed_modules = 0
    soft_failed_modules = 0
    total_modules = 0
    global_missing_features = 0
    global_out_of_scope_modules = 0
    global_priority_modules = 0
    planned_light_modules = 0
    planned_skipped_modules = 0
    global_audit_parse_failed_sheets = 0
    global_audit_fallback_sheets = 0
    audit_consistency_alert_sheets = 0
    low_coverage_signal_modules = 0
    manager_first_modules = 0
    manager_first_accept_count = 0
    supplement_round_count = 0
    for item in results:
        sheet_name = item["sheet_name"]
        summary = item["summary"]
        grand_total += summary.get("total_cases", 0)
        all_stats[sheet_name] = summary
        module_costs.extend(item["module_costs"])
        failed_modules += item["failed_modules"]
        soft_failed_modules += item.get("soft_failed_modules", 0)
        total_modules += item["total_modules"]
        global_missing_features += int(item.get("global_missing_features", 0))
        global_out_of_scope_modules += int(item.get("global_out_of_scope_modules", 0))
        global_priority_modules += int(item.get("global_priority_modules", 0))
        planned_light_modules += int(item.get("planned_light_modules", 0))
        planned_skipped_modules += int(item.get("planned_skipped_modules", 0))
        global_audit_parse_failed_sheets += 0 if item.get("global_audit_parse_ok", True) else 1
        global_audit_fallback_sheets += 1 if item.get("global_audit_fallback_used", False) else 0
        audit_consistency_alert_sheets += 1 if item.get("audit_consistency_alert", False) else 0
        low_coverage_signal_modules += int(item.get("low_coverage_signal_modules", 0))
        manager_first_modules += int(item.get("manager_first_modules", 0))
        manager_first_accept_count += int(item.get("manager_first_accept_count", 0))
        supplement_round_count += int(item.get("supplement_round_count", 0))

    summary_file = bug_output_dir / "_summary.md"
    with open(summary_file, "w", encoding="utf-8") as f:
        f.write(f"# Bug #{bug_info.get('bug_id', '?')} 测试评审汇总\n\n")
        f.write(f"- run_id: {run_id}\n")
        f.write(f"- started_at_utc: {run_started_at}\n")
        f.write(f"- finished_at_utc: {_utc_now_iso()}\n\n")
        f.write("| Sheet | 用例数 | 模块数 | 结果文件 |\n")
        f.write("|-------|--------|--------|----------|\n")
        for sheet_name, stats in all_stats.items():
            fname = f"review_{sheet_name.replace(' ', '_')}.md"
            f.write(f"| {sheet_name} | {stats.get('total_cases')} | {stats.get('module_count')} | {fname} |\n")

    total_elapsed = time.perf_counter() - started_all
    sorted_costs = sorted(module_costs)
    p95_idx = max(0, min(len(sorted_costs) - 1, int(len(sorted_costs) * 0.95) - 1))

    # Collect judge result and traceability from the last outcome's rag_status
    _collected_judge = {}
    _collected_trace = {}
    if outcome and isinstance(outcome, dict):
        _rag = outcome.get("rag_status") or {}
        _collected_judge = _rag.get("judge_result") or {}
        _collected_trace = _rag.get("traceability_matrix") or {}

    benchmark = {
        "run_id": run_id,
        "started_at_utc": run_started_at,
        "finished_at_utc": _utc_now_iso(),
        "total_modules": total_modules,
        "success_modules": max(0, total_modules - failed_modules),
        "failed_modules": failed_modules,
        "soft_failed_modules": soft_failed_modules,
        "avg_module_seconds": mean(module_costs) if module_costs else 0.0,
        "p95_module_seconds": sorted_costs[p95_idx] if sorted_costs else 0.0,
        "total_seconds": total_elapsed,
        "module_retry_times": MODULE_RETRY_TIMES,
        "module_retry_backoff_seconds": MODULE_RETRY_BACKOFF_SECONDS,
        "global_audit_enabled": GLOBAL_AUDIT_ENABLED,
        "global_audit_strict_parse": GLOBAL_AUDIT_STRICT_PARSE,
        "global_audit_retry_on_parse_fail": GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL,
        "evidence_strict_domain_filter": EVIDENCE_STRICT_DOMAIN_FILTER,
        "manager_first_require_strict_plan_parse": MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE,
        "manager_first_max_supplement_rounds": MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS,
        "manager_first_min_confidence": MANAGER_FIRST_MIN_CONFIDENCE,
        "manager_first_max_unresolved": MANAGER_FIRST_MAX_UNRESOLVED,
        "manager_first_max_taskpack_chars": MANAGER_FIRST_MAX_TASKPACK_CHARS,
        "scope_min_out_scope_confidence": GLOBAL_AUDIT_MIN_SKIP_CONFIDENCE,
        "scope_max_out_scope_ratio": GLOBAL_AUDIT_MAX_SKIP_RATIO,
        "scope_min_active_module_ratio": GLOBAL_AUDIT_MIN_ACTIVE_RATIO,
        "scope_uncertain_policy": GLOBAL_AUDIT_UNCERTAIN_POLICY,
        "report_include_evidence_summary": REPORT_INCLUDE_EVIDENCE_SUMMARY,
        "report_include_run_status_section": REPORT_INCLUDE_RUN_STATUS_SECTION,
        "target_sheet_seconds": PERF_TARGET_SHEET_SECONDS,
        "max_supplement_seconds": PERF_MAX_SUPPLEMENT_SECONDS,
        "global_missing_features": global_missing_features,
        "global_out_of_scope_modules": global_out_of_scope_modules,
        "global_priority_modules": global_priority_modules,
        "planned_light_modules": planned_light_modules,
        "planned_skipped_modules": planned_skipped_modules,
        "global_audit_parse_failed_sheets": global_audit_parse_failed_sheets,
        "global_audit_fallback_sheets": global_audit_fallback_sheets,
        "audit_consistency_alert_sheets": audit_consistency_alert_sheets,
        "low_coverage_signal_modules": low_coverage_signal_modules,
        "manager_first_modules": manager_first_modules,
        "manager_first_accept_count": manager_first_accept_count,
        "manager_first_accept_rate": (
            (manager_first_accept_count / manager_first_modules)
            if manager_first_modules > 0
            else 0.0
        ),
        "supplement_round_count": supplement_round_count,
        "judge_result": _collected_judge,
        "traceability_matrix": _collected_trace,
    }
    _write_benchmark_report(bug_output_dir, benchmark)

    # ── RunLedger: structured quality record ──────────────────────
    try:
        from INAGENT.review.run_ledger import RunLedger

        _judge = benchmark.get("judge_result", {})
        _trace = benchmark.get("traceability_matrix", {})
        _usage = (_collected_judge and {}) or {}
        if outcome and isinstance(outcome, dict):
            _rag = outcome.get("rag_status") or {}
            _usage = _rag.get("retrieval_usage") or {}
        ledger = RunLedger(
            run_id=run_id,
            bug_id=str(bug_id),
            timestamp=_utc_now_iso(),
            total_seconds=benchmark.get("total_elapsed_sec", 0),
            module_hints=benchmark.get("global_priority_modules", []),
            spec_requirements=_trace.get("total_requirements", 0) if isinstance(_trace, dict) else 0,
            spec_covered=_trace.get("covered", 0) if isinstance(_trace, dict) else 0,
            spec_gaps=_trace.get("gaps", 0) if isinstance(_trace, dict) else 0,
            findings_count=benchmark.get("findings_count", 0),
            all_gates_passed=benchmark.get("all_passed", False),
            judge_scores=_judge.get("scores", {}) if isinstance(_judge, dict) else {},
            judge_overall=_judge.get("overall", 0) if isinstance(_judge, dict) else 0,
            retrieval_usage=_usage if isinstance(_usage, dict) else {},
        )
        ledger.save(bug_output_dir)
    except Exception as _ledger_err:
        logger.warning("RunLedger save failed: %s", _ledger_err)

    with open(run_meta_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "run_id": run_id,
                "status": "completed",
                "started_at_utc": run_started_at,
                "finished_at_utc": _utc_now_iso(),
                "json_path": str(json_path),
                "clean_output_dir": BUG_TO_CASE_CLEAN_OUTPUT_DIR,
                "module_retry_times": MODULE_RETRY_TIMES,
                "module_retry_backoff_seconds": MODULE_RETRY_BACKOFF_SECONDS,
                "global_audit_enabled": GLOBAL_AUDIT_ENABLED,
                "global_audit_max_modules_in_prompt": GLOBAL_AUDIT_MAX_MODULES_IN_PROMPT,
                "global_audit_max_context_chars": GLOBAL_AUDIT_MAX_CONTEXT_CHARS,
                "global_audit_light_check_case_threshold": GLOBAL_AUDIT_LIGHT_CHECK_CASE_THRESHOLD,
                "global_audit_strict_parse": GLOBAL_AUDIT_STRICT_PARSE,
                "global_audit_retry_on_parse_fail": GLOBAL_AUDIT_RETRY_ON_PARSE_FAIL,
                "evidence_strict_domain_filter": EVIDENCE_STRICT_DOMAIN_FILTER,
                "manager_first_require_strict_plan_parse": MANAGER_FIRST_REQUIRE_STRICT_PLAN_PARSE,
                "manager_first_max_supplement_rounds": MANAGER_FIRST_MAX_SUPPLEMENT_ROUNDS,
                "manager_first_min_confidence": MANAGER_FIRST_MIN_CONFIDENCE,
                "manager_first_max_unresolved": MANAGER_FIRST_MAX_UNRESOLVED,
                "manager_first_max_taskpack_chars": MANAGER_FIRST_MAX_TASKPACK_CHARS,
                "scope_min_out_scope_confidence": GLOBAL_AUDIT_MIN_SKIP_CONFIDENCE,
                "scope_max_out_scope_ratio": GLOBAL_AUDIT_MAX_SKIP_RATIO,
                "scope_min_active_module_ratio": GLOBAL_AUDIT_MIN_ACTIVE_RATIO,
                "scope_uncertain_policy": GLOBAL_AUDIT_UNCERTAIN_POLICY,
                "report_include_evidence_summary": REPORT_INCLUDE_EVIDENCE_SUMMARY,
                "report_include_run_status_section": REPORT_INCLUDE_RUN_STATUS_SECTION,
                "target_sheet_seconds": PERF_TARGET_SHEET_SECONDS,
                "max_supplement_seconds": PERF_MAX_SUPPLEMENT_SECONDS,
                "total_modules": total_modules,
                "failed_modules": failed_modules,
                "soft_failed_modules": soft_failed_modules,
                "global_missing_features": global_missing_features,
                "global_out_of_scope_modules": global_out_of_scope_modules,
                "global_priority_modules": global_priority_modules,
                "planned_light_modules": planned_light_modules,
                "planned_skipped_modules": planned_skipped_modules,
                "global_audit_parse_failed_sheets": global_audit_parse_failed_sheets,
                "global_audit_fallback_sheets": global_audit_fallback_sheets,
                "audit_consistency_alert_sheets": audit_consistency_alert_sheets,
                "low_coverage_signal_modules": low_coverage_signal_modules,
                "manager_first_modules": manager_first_modules,
                "manager_first_accept_count": manager_first_accept_count,
                "manager_first_accept_rate": (
                    (manager_first_accept_count / manager_first_modules)
                    if manager_first_modules > 0
                    else 0.0
                ),
                "supplement_round_count": supplement_round_count,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    logger.info(f"全部评审完成，输出目录: {bug_output_dir}")

def main():
    parser = argparse.ArgumentParser(description="Manager 统一 Bug 定向测试评审")
    parser.add_argument("bug_dir", type=Path, help="Bug 相关文件所在目录 (包含 xlsx 和 fix detail txt)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "INAGENT" / "reports", help="结果输出目录")
    
    args = parser.parse_args()
    
    if not args.bug_dir.exists() or not args.bug_dir.is_dir():
         logger.error(f"无效的 Bug 目录: {args.bug_dir}")
         sys.exit(1)
         
    # 步骤 1: 预处理（如已有 bug_to_case.json 则跳过）
    json_path = args.bug_dir / "bug_to_case.json"
    if json_path.exists():
        logger.info("已存在 %s，跳过预处理步骤", json_path)
    else:
        json_path = run_prepare_script(args.bug_dir)
    
    # 步骤 2: 执行评审
    execute_review(json_path, args.output_dir, args.bug_dir)
    
if __name__ == "__main__":
     main()
