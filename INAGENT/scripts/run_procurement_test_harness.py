# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""真实采购测试程序：原始文档 -> 采购员 -> test_data 输出。

输入目录默认使用 INAGENT/test_data/input。
输出目录默认写入 INAGENT/test_data/runs/<run_id>/。
L2 LLM 缓存默认写入 INAGENT/test_data/cache/procurement_l2/。

**与正式采购入口的关系**：正式入库编排为 ``procurement_ingest.main`` /
``auto_convert.run_procurement_document_pipeline``（见 ``03-procurement.md``）。
本脚本为 **无副作用模拟器**：自建解析分支（PDF/Office/TXT）+ 采购员筛查，
不覆盖正式管线中的全部缓存与保护；用于回归筛查逻辑与 MinerU/Office 行为，
不等价于完整复现 ``procurement_ingest``。

本脚本只模拟到“交给农民前”为止：
1. 原始文档解析为 procurement chunks
2. 采购员三层筛查
3. 写出决策明细、日志副本、给农民的 accept 数据、统计摘要

不会修改 reference/knowledge_base.json，不会进入 farmer cultivate。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from camel.loaders.local_mineru_reader import LocalMinerUReader

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from INAGENT.agents.knowledge_procurement_agent import ChunkDecision, KnowledgeProcurementAgent  # noqa: E402
from INAGENT.data_tools import auto_convert as ac  # noqa: E402
from INAGENT.data_tools import mineru_procurement as mp  # noqa: E402
from INAGENT.data_tools.spec_parser import parse_spec_document  # noqa: E402
from INAGENT.data_tools.testlist_parser import parse_test_list  # noqa: E402
from INAGENT.utils.env_utils import get_product_name, load_inagent_env  # noqa: E402
from INAGENT.web.deps import get_llm_model  # noqa: E402

logger = logging.getLogger("procurement_test_harness")

TEST_DATA_ROOT = REPO_ROOT / "INAGENT" / "test_data"
DEFAULT_INPUT_DIR = TEST_DATA_ROOT / "input"
DEFAULT_RUNS_DIR = TEST_DATA_ROOT / "runs"
DEFAULT_CACHE_DIR = TEST_DATA_ROOT / "cache" / "procurement_l2"
# .doc / .xls：MarkItDown 路径不可靠，与「名义支持」脱钩；请改用 .docx / .xlsx。
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".txt"}
LEGACY_OFFICE_SUFFIXES = {".doc", ".xls"}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _collect_input_files(
    input_dir: Path,
) -> tuple[List[Path], List[Dict[str, Any]]]:
    """返回 (待处理文件, 因格式被拒绝的条目)。.doc/.xls 单独列出并说明原因。"""
    files: List[Path] = []
    skipped: List[Dict[str, Any]] = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        suf = path.suffix.lower()
        if suf in LEGACY_OFFICE_SUFFIXES:
            skipped.append({
                "path": str(path),
                "suffix": suf,
                "reason": "legacy_office_not_supported_use_docx_or_xlsx",
            })
            continue
        if suf in SUPPORTED_SUFFIXES:
            files.append(path)
    return files, skipped


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _get_mineru_command() -> str:
    mineru_cmd = ac.cfg_str("auto_convert.mineru.cli", "", env="MINERU_CLI")
    if mineru_cmd:
        return mineru_cmd
    candidates = [
        ac.BASE_DIR.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
        ac.BASE_DIR.parent.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
    ]
    picked = next((candidate for candidate in candidates if candidate.exists()), None)
    return str(picked) if picked else "mineru"


def _apply_rule_metadata(clean_text: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    config = ac._load_project_config()
    meta_rules = config.get("metadata_rules", {})
    product_modules_map = ac._merge_product_modules_map(meta_rules.get("product_modules", {}))
    lower_text = clean_text.lower()
    lower_section = (
        f"{meta.get('section_title', '')} {meta.get('parent_section', '')}".lower()
    )

    for intent, keywords in meta_rules.get("intents", {}).items():
        if any(keyword.lower() in lower_text for keyword in keywords):
            meta["intent"] = intent
            break
    for module, keywords in product_modules_map.items():
        if any(keyword.lower() in lower_text for keyword in keywords) or any(
            keyword.lower() in lower_section for keyword in keywords
        ):
            meta["product_module"] = module
            break
    protocols = ac._match_protocols_word_boundary(
        meta_rules.get("protocol_types", {}),
        clean_text,
    )
    if protocols:
        meta["protocol_type"] = protocols
    for prefix, keywords in meta_rules.get("command_prefixes", {}).items():
        if any(keyword.lower() in lower_text for keyword in keywords):
            meta["command_prefix"] = prefix
            break
    for mode, keywords in meta_rules.get("config_modes", {}).items():
        if any(keyword.lower() in lower_text for keyword in keywords):
            meta["config_mode"] = mode
            break
    return meta


async def _build_pdf_chunks(file_path: Path, max_pages: Optional[int]) -> Dict[str, Any]:
    """解析 PDF；MinerU 失败或缺少 content_list 时不抛异常，与 mineru_procurement.convert_one 策略对齐。"""
    load_inagent_env()
    mp.refresh_mineru_vllm_settings()
    reader = LocalMinerUReader(
        output_dir=str(mp.MINERU_OUTPUT_DIR),
        mineru_command=_get_mineru_command(),
    )

    task_id = file_path.stem
    existing_output = mp._find_existing_mineru_output(task_id)
    raw_json: Optional[str] = None
    mineru_error: Optional[str] = None

    try:
        if existing_output and mp._can_reuse_mineru_output(file_path, existing_output):
            raw_json = existing_output.read_text(encoding="utf-8")
            logger.info("[pdf] 复用现有 MinerU 输出: %s", existing_output)
        else:
            logger.info("[pdf] 提交 MinerU 任务: %s", file_path.name)
            task_id = await reader.submit_task(
                str(file_path),
                cwd=str(mp.MINERU_OUTPUT_DIR),
            )
            raw_json = await reader.get_task_output(task_id)
    except RuntimeError as e:
        mineru_error = f"mineru_runtime:{e}"
        logger.warning("[pdf] MinerU 执行失败 %s: %s", file_path.name, e)
    except FileNotFoundError as e:
        mineru_error = f"missing_output:{e}"
        logger.warning(
            "[pdf] MinerU 输出缺失（与正式管线告警后继续的空块策略对齐）%s: %s",
            file_path.name,
            e,
        )
    except OSError as e:
        mineru_error = f"io_error:{e}"
        logger.warning("[pdf] MinerU I/O 错误 %s: %s", file_path.name, e)

    blocks: List[Any] = []
    if raw_json is not None:
        try:
            loaded = json.loads(raw_json)
            if isinstance(loaded, list):
                blocks = loaded
            else:
                mineru_error = (mineru_error + "; " if mineru_error else "") + "mineru_json_not_list"
                logger.warning("[pdf] MinerU 顶层 JSON 非 list: %s", file_path.name)
        except json.JSONDecodeError as e:
            mineru_error = (mineru_error + "; " if mineru_error else "") + f"json_decode:{e}"
            logger.warning("[pdf] MinerU JSON 解析失败 %s: %s", file_path.name, e)

    blocks, frontmatter_pages = await asyncio.to_thread(ac._filter_frontmatter_blocks, blocks)
    section_context_map = ac._build_section_context_map(blocks)
    frontmatter_page_set = set(frontmatter_pages)
    source_label = ac._source_file_label(file_path)
    document_category = mp._infer_pdf_document_category(file_path)

    chunks: List[Dict[str, Any]] = []
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        page_idx = block.get("page_idx")
        if max_pages is not None and isinstance(page_idx, int) and page_idx >= max_pages:
            continue
        text = ac._extract_text_from_block(block)
        if not text.strip():
            continue
        section_context = section_context_map.get(idx, {})
        meta: Dict[str, Any] = {
            "source_pdf": str(file_path),
            "source_file": source_label,
            "page_idx": page_idx,
            "block_type": block.get("type"),
            "block_id": idx,
            "mineru_task_id": task_id,
            "section_title": section_context.get("section_title", ""),
            "parent_section": section_context.get("parent_section", ""),
            "section_path": section_context.get("section_path", ""),
            "document_category": document_category,
            "is_frontmatter": page_idx in frontmatter_page_set,
            "frontmatter_confidence": 0.95 if page_idx in frontmatter_page_set else 0.0,
        }
        if block.get("img_path"):
            meta["img_path"] = block["img_path"]
        clean_text = ac._clean_chunk_text(text)
        meta["clean_text"] = clean_text
        _apply_rule_metadata(clean_text, meta)
        chunks.append({
            "page_content": text,
            "metadata": meta,
        })

    return {
        "source_type": "pdf",
        "source_file": source_label,
        "task_id": task_id,
        "frontmatter_pages": sorted(frontmatter_pages),
        "raw_block_count": len(blocks),
        "chunk_count": len(chunks),
        "document_category": document_category,
        "mineru_error": mineru_error,
        "chunks": chunks,
    }


def _build_office_chunks(file_path: Path) -> Dict[str, Any]:
    source_label = ac._source_file_label(file_path)
    suffix = file_path.suffix.lower()
    if suffix in {".xlsx"}:
        document_category = "test/test_list"
        chunks = parse_test_list(
            file_path,
            product_module="",
            document_category=document_category,
        )
    else:
        document_category = "spec/design"
        chunks = parse_spec_document(
            file_path,
            document_category=document_category,
            product_module="",
        )
    for chunk in chunks:
        meta = chunk.setdefault("metadata", {})
        meta["source_file"] = source_label
        meta.setdefault("source_pdf", str(file_path))
        meta.setdefault("document_category", document_category)
    office_warn = None
    if not chunks:
        office_warn = (
            "markitdown_returned_no_chunks_check_format_or_conversion; "
            "xlsx_requires_parseable_markdown_table"
        )
    return {
        "source_type": "office",
        "source_file": source_label,
        "raw_block_count": len(chunks),
        "chunk_count": len(chunks),
        "document_category": document_category,
        "office_parse_warning": office_warn,
        "chunks": chunks,
    }


def _build_text_chunks(file_path: Path) -> Dict[str, Any]:
    source_label = ac._source_file_label(file_path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    if ac.re.search(r"Bug\s+\d+", text[:2000], ac.re.IGNORECASE):
        chunks = ac._parse_bug_fix_text(text, source_label)
        document_category = "review/bug_fix"
    else:
        chunks = ac._parse_generic_text(text, source_label, "text")
        document_category = "text"
    for chunk in chunks:
        meta = chunk.setdefault("metadata", {})
        meta["source_file"] = source_label
        meta.setdefault("source_pdf", str(file_path))
        meta.setdefault("document_category", document_category)
    return {
        "source_type": "text",
        "source_file": source_label,
        "raw_block_count": len(chunks),
        "chunk_count": len(chunks),
        "document_category": document_category,
        "chunks": chunks,
    }


async def _build_chunks_for_file(file_path: Path, max_pages: Optional[int]) -> Dict[str, Any]:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return await _build_pdf_chunks(file_path, max_pages)
    if suffix in {".docx", ".xlsx"}:
        return _build_office_chunks(file_path)
    if suffix == ".txt":
        return _build_text_chunks(file_path)
    raise ValueError(f"不支持的文件类型: {file_path}")


def _decision_to_dict(decision: ChunkDecision) -> Dict[str, Any]:
    return {
        "chunk_index": decision.chunk_index,
        "source_file": decision.source_file,
        "chunk": decision.chunk,
        "decision": {
            "action": decision.decision.action,
            "target_kb": decision.decision.target_kb,
            "confidence": decision.decision.confidence,
            "reason": decision.decision.reason,
            "schema_gap": decision.decision.schema_gap,
            "suggested_value": decision.decision.suggested_value,
            "reason_code": decision.decision.reason_code,
            "rule_layer": decision.decision.rule_layer,
            "rule_confidence": decision.decision.rule_confidence,
        },
    }


def _summarize_decisions(decisions: List[ChunkDecision]) -> Dict[str, Any]:
    action_counts: Counter[str] = Counter()
    target_counts: Counter[str] = Counter()
    schema_gap_counts: Counter[str] = Counter()
    low_confidence = 0
    for item in decisions:
        action_counts[item.decision.action] += 1
        target_counts[item.decision.target_kb] += 1
        if item.decision.schema_gap:
            schema_gap_counts[item.decision.schema_gap] += 1
        if item.decision.confidence < 0.6:
            low_confidence += 1
    return {
        "total": len(decisions),
        "actions": dict(action_counts),
        "targets": dict(target_counts),
        "schema_gaps": dict(schema_gap_counts),
        "low_confidence": low_confidence,
    }


async def _run(args: argparse.Namespace) -> int:
    load_inagent_env()
    input_dir = args.input_dir.resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"输入目录不存在: {input_dir}")

    files, skipped_legacy = _collect_input_files(input_dir)
    if not files and not skipped_legacy:
        raise FileNotFoundError(
            f"输入目录下未发现可处理文档（支持后缀 {sorted(SUPPORTED_SUFFIXES)}）: {input_dir}",
        )

    run_id = args.run_id or _build_run_id()
    run_dir = args.output_root.resolve() / run_id
    files_dir = run_dir / "files"
    cache_dir = args.cache_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    if skipped_legacy:
        _write_json(
            run_dir / "skipped_unsupported_format.json",
            {"skipped": skipped_legacy, "note": ".doc/.xls 需先转为 .docx/.xlsx"},
        )
    if not args.disable_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)

    model = get_llm_model()
    product_name = get_product_name()
    agent = KnowledgeProcurementAgent(
        model=model,
        product_name=product_name,
        model_signature=args.model_signature,
        llm_cache_dir=None if args.disable_cache else cache_dir,
        llm_cache_force_refresh=args.refresh_cache,
    )

    logger.info("待处理文件: %d，跳过 .doc/.xls: %d", len(files), len(skipped_legacy))
    logger.info("输出目录: %s", run_dir)
    logger.info("L2 cache: %s", "disabled" if args.disable_cache else cache_dir)

    manifest_files: List[Dict[str, Any]] = []
    all_decisions: List[ChunkDecision] = []
    file_errors: List[Dict[str, Any]] = []
    empty_parse_files: List[str] = []

    for file_path in files:
        logger.info("[run] 处理文件: %s", file_path)
        stem = ac._output_stem_for_file(file_path)
        file_dir = files_dir / stem
        file_dir.mkdir(parents=True, exist_ok=True)

        try:
            parsed = await _build_chunks_for_file(file_path, args.max_pages)
            chunks = parsed.pop("chunks")
            _write_json(file_dir / "parsed_chunks.json", chunks)
            _write_json(file_dir / "parsed_summary.json", parsed)

            if not chunks:
                reason = "no_chunks_after_parse"
                if parsed.get("source_type") == "pdf" and parsed.get("mineru_error"):
                    reason = "mineru_failed_or_empty_blocks"
                elif parsed.get("office_parse_warning"):
                    reason = "office_markitdown_empty"
                logger.warning("[run] 跳过筛查（0 chunk）: %s (%s)", file_path, reason)
                empty_parse_files.append(str(file_path))
                manifest_files.append({
                    "input_file": str(file_path),
                    "status": "empty_content",
                    "empty_reason": reason,
                    "source_type": parsed.get("source_type"),
                    "source_file": parsed.get("source_file"),
                    "output_dir": str(file_dir),
                    "parsed_chunks": str(file_dir / "parsed_chunks.json"),
                    "parsed_summary": str(file_dir / "parsed_summary.json"),
                    "mineru_error": parsed.get("mineru_error"),
                    "office_parse_warning": parsed.get("office_parse_warning"),
                    "decision_total": 0,
                    "accepted_total": 0,
                })
                continue

            decisions = agent.evaluate_batch(chunks)
            accepted_chunks = agent.filter_accepted(decisions)
            logs_dir = file_dir / "logs"
            log_counts = agent.write_logs(decisions, log_dir=logs_dir)

            decision_payload = [_decision_to_dict(item) for item in decisions]
            summary_payload = _summarize_decisions(decisions)
            summary_payload["accepted_for_farmer"] = len(accepted_chunks)
            summary_payload["log_counts"] = log_counts

            _write_json(file_dir / "decisions.json", decision_payload)
            _write_json(file_dir / "accepted_for_farmer.json", accepted_chunks)
            _write_json(file_dir / "summary.json", summary_payload)

            manifest_files.append({
                "input_file": str(file_path),
                "status": "ok",
                "source_type": parsed.get("source_type"),
                "source_file": parsed.get("source_file"),
                "output_dir": str(file_dir),
                "parsed_chunks": str(file_dir / "parsed_chunks.json"),
                "parsed_summary": str(file_dir / "parsed_summary.json"),
                "decisions": str(file_dir / "decisions.json"),
                "accepted_for_farmer": str(file_dir / "accepted_for_farmer.json"),
                "summary": str(file_dir / "summary.json"),
                "logs_dir": str(logs_dir),
                "decision_total": len(decisions),
                "accepted_total": len(accepted_chunks),
            })
            all_decisions.extend(decisions)
        except Exception as exc:
            err_text = f"{type(exc).__name__}: {exc}"
            logger.exception("[run] 文件处理失败: %s", file_path)
            tb = traceback.format_exc()
            _write_json(
                file_dir / "error.json",
                {"error": err_text, "traceback": tb},
            )
            file_errors.append({
                "input_file": str(file_path),
                "error": err_text,
                "traceback": tb,
            })
            manifest_files.append({
                "input_file": str(file_path),
                "status": "error",
                "error": err_text,
                "output_dir": str(file_dir),
                "error_file": str(file_dir / "error.json"),
            })

    cache_stats = agent.get_l2_cache_stats()
    global_summary = _summarize_decisions(all_decisions)
    global_summary["run_id"] = run_id
    global_summary["input_dir"] = str(input_dir)
    global_summary["files_total"] = len(files)
    global_summary["files_skipped_legacy_office"] = len(skipped_legacy)
    global_summary["files_with_errors"] = len(file_errors)
    global_summary["files_empty_parse"] = len(empty_parse_files)
    global_summary["empty_parse_inputs"] = empty_parse_files
    global_summary["file_errors"] = file_errors
    has_ok = any(m.get("status") == "ok" for m in manifest_files)
    if file_errors and not has_ok:
        rs = "failed"
    elif file_errors or empty_parse_files or skipped_legacy or (
        not files and skipped_legacy
    ):
        rs = "partial"
    else:
        rs = "ok"
    global_summary["run_status"] = rs
    global_summary["cache"] = {
        "enabled": not args.disable_cache,
        "dir": None if args.disable_cache else str(cache_dir),
        **cache_stats,
    }

    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(),
        "input_dir": str(input_dir),
        "output_dir": str(run_dir),
        "cache_dir": None if args.disable_cache else str(cache_dir),
        "skipped_unsupported_format_file": (
            str(run_dir / "skipped_unsupported_format.json")
            if skipped_legacy else None
        ),
        "files": manifest_files,
        "summary_file": str(run_dir / "summary.json"),
        "run_status": rs,
        "errors": file_errors,
    }

    _write_json(run_dir / "summary.json", global_summary)
    _write_json(run_dir / "manifest.json", manifest)
    logger.info(
        "[done] run_id=%s files=%d ok_decisions=%d errors=%d empty=%d skipped_legacy=%d",
        run_id,
        len(files),
        len(all_decisions),
        len(file_errors),
        len(empty_parse_files),
        len(skipped_legacy),
    )
    return 1 if rs == "failed" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--disable-cache", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--model-signature", type=str, default="")
    args = parser.parse_args()

    _setup_logging()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())