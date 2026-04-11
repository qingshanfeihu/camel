"""
综合 4-way 端到端流水线 — 恢复快照 + 采购员→农民→农场主 + 4way诊断

覆盖文档:
  - cli_1-82.pdf   (leaf)
  - app_1-40.pdf   (branch)
  - ustack设计架构V3.pdf (root)
  - app_65-72.pdf   (branch, HA/SLB)

流程:
  Phase 0: 预检 doc_local_reference，缺少的跑 auto_convert
  Phase 1: 恢复小树苗快照
  Phase 2: 采购员→农民→合并→农场主→回填→合并
  Phase 3: 重建向量索引
  Phase 4: 4-way 诊断 (CLI 10条 / APP 5条 / ARCH / HA-SLB)

用法:
    python -m INAGENT.scripts.test_4way_pipeline
    python -m INAGENT.scripts.test_4way_pipeline --skip-autoconvert
    python -m INAGENT.scripts.test_4way_pipeline --seed 123
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("4way_pipeline")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = INAGENT_ROOT / "knowledge_base"
REFERENCE_DIR = KB_DIR / "reference"
DOC_LOCAL_REF = KB_DIR / "doc_local_reference"
INPUT_DIR = KB_DIR / "input"
LOGS_DIR = KB_DIR / "logs"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
CT_PATH = REFERENCE_DIR / "commandtree_base.json"
GAPS_FILE = LOGS_DIR / "sapling_schema_gaps.jsonl"
GRAPHRAG_OUTPUT = INAGENT_ROOT / "graphrag_index" / "output"
GRAPHRAG_ENTITIES = GRAPHRAG_OUTPUT / "entities.parquet"
GRAPHRAG_RELS = GRAPHRAG_OUTPUT / "relationships.parquet"

_report_file = None


def _init_report_log():
    global _report_file
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _report_file = open(
        LOGS_DIR / "4way_report.txt", "w", encoding="utf-8",
    )


def tprint(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    print(text, **kwargs)
    if _report_file:
        _report_file.write(text + "\n")
        _report_file.flush()


TARGET_DOCS = [
    {"pdf": "cli_1-82.pdf", "stem": "cli_1-82", "ref_prefix": "cli", "track": "CLI"},
    {"pdf": "app_1-40.pdf", "stem": "app_1-40", "ref_prefix": "app", "track": "APP"},
    {"pdf": "ustack设计架构V3.pdf", "stem": "ustack设计架构V3", "ref_prefix": "ustack", "track": "ARCH"},
    {"pdf": "app_65-72.pdf", "stem": "app_65-72", "ref_prefix": "app_65", "track": "HASLB"},
]

# 快照查找：最新的 pre_ircookie 3way 快照
SNAPSHOT_BASE = KB_DIR / "snapshots"

PASS = "OK"
FAIL = "XX"
WARN = "△"


def _find_latest_snapshot() -> Path:
    candidates = sorted(SNAPSHOT_BASE.glob("pre_ircookie_*"), reverse=True)
    for s in candidates:
        ref = s / "reference" / "knowledge_base.json"
        if ref.exists():
            return s
    raise FileNotFoundError(f"No valid snapshot found in {SNAPSHOT_BASE}")


def _load_json(p: Path) -> list:
    if not p.exists():
        logger.error("文件不存在: %s", p)
        return []
    return json.loads(p.read_text(encoding="utf-8"))


_kb_tree_level_index: Dict[str, str] = {}
_kb_doc_cat_index: Dict[str, str] = {}

# cjk-scan 黑名单：这些单 token 前缀语义过于宽泛，匹配到任意开头条目没有意义
_CJK_SCAN_BLACKLIST = frozenset({
    "no", "ip", "ipv6", "set", "show", "clear", "get", "do", "of",
})


def _build_kb_tree_level_index():
    global _kb_tree_level_index, _kb_doc_cat_index
    if _kb_tree_level_index and _kb_doc_cat_index:
        return
    sources = []
    for ref_file in sorted(REFERENCE_DIR.glob("*.json")):
        if ref_file.name == "commandtree_base.json":
            continue
        if "_bak" in ref_file.stem:
            continue
        try:
            data = json.loads(ref_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, list):
            sources.extend(data)
    for chunk in sources:
        m = chunk.get("metadata", {})
        tp = m.get("tree_position", {})
        tl = tp.get("tree_level", "") if isinstance(tp, dict) else ""
        dc = m.get("document_category", "")
        bid = m.get("block_id") or m.get("chunk_id") or m.get("node_id") or ""
        title = m.get("section_title", "").strip()
        src = m.get("source_file", "")
        if tl:
            if bid:
                _kb_tree_level_index[bid] = tl
            if title and src:
                _kb_tree_level_index[f"{src}::{title}"] = tl
            if title:
                _kb_tree_level_index.setdefault(f"title::{title}", tl)
        if dc:
            if bid:
                _kb_doc_cat_index[bid] = dc
            if title and src:
                _kb_doc_cat_index[f"{src}::{title}"] = dc
            if title:
                _kb_doc_cat_index.setdefault(f"title::{title}", dc)
    logger.info("KB tree_level index: %d entries, doc_cat index: %d entries",
                len(_kb_tree_level_index), len(_kb_doc_cat_index))


def _lookup_tree_level(block_id: str = "", title: str = "",
                        source_file: str = "") -> str:
    if not _kb_tree_level_index:
        _build_kb_tree_level_index()
    if block_id and block_id in _kb_tree_level_index:
        return _kb_tree_level_index[block_id]
    if title and source_file:
        key = f"{source_file}::{title}"
        if key in _kb_tree_level_index:
            return _kb_tree_level_index[key]
    if title:
        key = f"title::{title}"
        if key in _kb_tree_level_index:
            return _kb_tree_level_index[key]
    return "?"


def _lookup_doc_cat(block_id: str = "", title: str = "",
                    source_file: str = "") -> str:
    if not _kb_doc_cat_index:
        _build_kb_tree_level_index()
    if block_id and block_id in _kb_doc_cat_index:
        return _kb_doc_cat_index[block_id]
    if title and source_file:
        key = f"{source_file}::{title}"
        if key in _kb_doc_cat_index:
            return _kb_doc_cat_index[key]
    if title:
        key = f"title::{title}"
        if key in _kb_doc_cat_index:
            return _kb_doc_cat_index[key]
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Phase 0: 预检 + auto_convert
# ══════════════════════════════════════════════════════════════════════════════

def check_doc_local_refs() -> Dict[str, bool]:
    status = {}
    for doc in TARGET_DOCS:
        ref_path = DOC_LOCAL_REF / f"{doc['stem']}.json"
        status[doc["stem"]] = ref_path.exists()
        logger.info("doc_local_reference: %s -> %s", doc["stem"],
                     "EXISTS" if ref_path.exists() else "MISSING")
    return status


async def run_autoconvert_for_missing(missing_stems: List[str]):
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    from INAGENT.data_tools.auto_convert import (
        DOC_LOCAL_DIR, MINERU_OUTPUT_DIR, REFERENCE_DIR as AC_REF_DIR,
        convert_one, _setup_mineru_config, _refresh_mineru_vllm_settings,
    )
    from camel.loaders import LocalMinerUReader

    _refresh_mineru_vllm_settings()
    _setup_mineru_config()
    MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    mineru_cmd = "mineru"
    candidates = [
        INAGENT_ROOT.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
    ]
    picked = next((c for c in candidates if c.exists()), None)
    if picked:
        mineru_cmd = str(picked)

    reader = LocalMinerUReader(
        output_dir=str(MINERU_OUTPUT_DIR),
        mineru_command=mineru_cmd,
    )

    for stem in missing_stems:
        pdf_path = INPUT_DIR / f"{stem}.pdf"
        if not pdf_path.exists():
            for doc in TARGET_DOCS:
                if doc["stem"] == stem:
                    pdf_path = INPUT_DIR / doc["pdf"]
                    break
        if not pdf_path.exists():
            logger.error("PDF不存在: %s", pdf_path)
            continue
        logger.info("=" * 60)
        logger.info("auto_convert: %s", pdf_path.name)
        logger.info("=" * 60)
        try:
            await convert_one(reader, pdf_path)
            ref_json = DOC_LOCAL_REF / f"{stem}.json"
            if ref_json.exists():
                logger.info("doc_local_reference 生成成功: %s (%d blocks)",
                             ref_json.name, len(_load_json(ref_json)))
            else:
                logger.warning("doc_local_reference 未生成: %s", ref_json.name)
        except Exception as exc:
            logger.error("auto_convert 失败 %s: %s", pdf_path.name, exc)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1: 恢复快照
# ══════════════════════════════════════════════════════════════════════════════

def restore_snapshot() -> int:
    global _kb_tree_level_index, _kb_doc_cat_index
    snapshot = _find_latest_snapshot()
    logger.info("使用快照: %s", snapshot.name)

    snap_ref = snapshot / "reference" / "knowledge_base.json"
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    for f in REFERENCE_DIR.glob("*.json"):
        if f.name in ("commandtree_base.json", "tree_level_overlay.json"):
            continue
        f.unlink()
    for f in REFERENCE_DIR.glob("*.jsonl"):
        f.unlink()

    # 清理 logs 中的残留缓存（farmer_cache、reject_log、pending_review）
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.farmer_cache.json", "reject_log.jsonl", "pending_review.jsonl"):
        for f in LOGS_DIR.glob(pattern):
            f.unlink()
            logger.debug("清理 log 缓存: %s", f.name)

    # 重置模块级全局索引缓存
    _kb_tree_level_index.clear()
    _kb_doc_cat_index.clear()

    shutil.copy2(snap_ref, KB_PATH)
    kb_data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    logger.info("小树苗恢复: %d chunks", len(kb_data))

    snap_graphrag = snapshot / "graphrag_output"
    if snap_graphrag.exists():
        GRAPHRAG_OUTPUT.mkdir(parents=True, exist_ok=True)
        for f in snap_graphrag.glob("*.parquet"):
            shutil.copy2(f, GRAPHRAG_OUTPUT / f.name)
        logger.info("GraphRAG parquets 恢复: %d files",
                     len(list(snap_graphrag.glob("*.parquet"))))
    return len(kb_data)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2: 采购员→农民→农场主
# ══════════════════════════════════════════════════════════════════════════════

def load_doc_blocks(stem: str, limit: int = 0) -> List[Dict]:
    ref_path = DOC_LOCAL_REF / f"{stem}.json"
    if not ref_path.exists():
        logger.warning("doc_local_reference 不存在: %s", ref_path)
        return []
    blocks = json.loads(ref_path.read_text(encoding="utf-8"))
    if limit > 0:
        blocks = blocks[:limit]
    for b in blocks:
        meta = b.get("metadata", {})
        meta.pop("tree_position", None)
        meta.pop("owner_excluded", None)
        meta.pop("tree_node_id", None)
        meta.pop("command_refs", None)
    logger.info("加载 %s: %d blocks%s", ref_path.name, len(blocks),
                f" (limit={limit})" if limit > 0 else "")
    return blocks


def run_procurement(blocks: List[Dict], model, source_file: str) -> Tuple[List, List]:
    from INAGENT.agents.knowledge_procurement_agent import KnowledgeProcurementAgent

    for b in blocks:
        b.setdefault("metadata", {})["source_file"] = source_file

    procurement = KnowledgeProcurementAgent(model=model)
    decisions = procurement.evaluate_batch(blocks)
    accepted = procurement.filter_accepted(decisions)
    log_counts = procurement.write_logs(decisions, log_dir=LOGS_DIR)

    logger.info(
        "采购员 [%s]: total=%d, accept=%d, reject=%d, pending=%d",
        source_file, len(decisions),
        log_counts.get("accept", 0), log_counts.get("reject", 0),
        log_counts.get("pending_review", 0),
    )
    return accepted, decisions


def run_farmer(accepted_blocks, decisions_raw, model, source_file: str,
               skip_procurement: bool = False):
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision, ProcurementDecision, enrich_chunk_decision_for_farmer,
    )
    from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent

    if skip_procurement:
        decisions = []
        for idx, chunk in enumerate(accepted_blocks):
            decisions.append(ChunkDecision(
                chunk=chunk,
                decision=ProcurementDecision(
                    action="accept", target_kb="product",
                    confidence=0.9, reason="skip-procurement",
                ),
                source_file=source_file, chunk_index=idx,
            ))
    else:
        decisions = [d for d in decisions_raw if d.decision.action == "accept"]

    decisions = [enrich_chunk_decision_for_farmer(d) for d in decisions]

    farmer = KnowledgeFarmerAgent(model=model)
    logger.info("农民开始 cultivate: %d chunks [%s]", len(decisions), source_file)
    results = farmer.cultivate_batch(decisions)

    matched = sum(1 for r in results if r.matched_node_id)
    gaps = sum(len(r.schema_gaps) for r in results)
    logger.info("农民完成: %d results, matched=%d, gaps=%d", len(results), matched, gaps)

    counts = farmer.write_to_reference(results, ref_dir=REFERENCE_DIR, log_dir=LOGS_DIR)
    logger.info("写入 reference/: %s", counts)

    gap_count = farmer.emit_schema_gaps(results, GAPS_FILE)
    logger.info("schema gaps: %d → %s", gap_count, GAPS_FILE.name)

    return farmer, results, gap_count


def merge_to_sapling() -> int:
    from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base
    merge_knowledge_base(REFERENCE_DIR, KB_PATH)
    kb_data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    logger.info("合并完成: knowledge_base.json = %d chunks", len(kb_data))
    return len(kb_data)


def _rebuild_graphrag_index():
    graph_path = INAGENT_ROOT / "knowledge_base" / "cli_keyword_graph.json"
    if not graph_path.exists():
        logger.warning("cli_keyword_graph.json 不存在，跳过 GraphRAG 重建")
        return
    logger.info("重建 GraphRAG 索引 (from cli_keyword_graph.json) ...")
    from INAGENT.scripts.build_graphrag_from_graph import build
    build()
    logger.info("GraphRAG 索引重建完成")


def run_farm_owner(gap_count: int):
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    if gap_count == 0:
        logger.info("无 schema gaps，跳过农场主")
        return None, None

    workspace = INAGENT_ROOT / "graphrag_index"
    graphrag = GraphRAGRetriever(workspace_dir=workspace)
    if not graphrag.is_available():
        logger.warning("GraphRAG 不可用，跳过农场主")
        return None, None

    owner = KnowledgeFarmOwnerAgent(graphrag)
    logger.info("农场主处理 schema gaps: %d 条", gap_count)
    report = owner.process_gaps(GAPS_FILE)

    logger.info(
        "农场主报告: entities=%d, fill_requests=%d, deferred=%d, errors=%d",
        report.entities_added, len(report.fill_requests),
        len(report.deferred), len(report.errors),
    )
    return owner, report


def run_fill_cycle(farmer, report) -> int:
    if not report or not report.fill_requests:
        return 0
    filled = farmer.apply_fill_request(report.fill_requests, ref_dir=REFERENCE_DIR)
    logger.info("回填完成: %d 条", filled)
    return filled


def run_full_pipeline(doc_statuses: Dict[str, bool]):
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()
    from INAGENT.workflow_config_generator import initialize_llm_model
    model = initialize_llm_model()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if GAPS_FILE.exists():
        GAPS_FILE.unlink()

    all_farmers = []
    all_farm_stats = {}
    total_gap_count = 0

    for doc in TARGET_DOCS:
        stem = doc["stem"]
        pdf_name = doc["pdf"]
        track = doc["track"]

        if not doc_statuses.get(stem, False):
            logger.warning("跳过 %s: doc_local_reference 不存在", stem)
            continue

        logger.info("=" * 60)
        logger.info("处理文档: %s [%s]", pdf_name, track)
        logger.info("=" * 60)

        blocks = load_doc_blocks(stem)
        if not blocks:
            continue

        accepted, decisions = run_procurement(blocks, model, pdf_name)
        if not accepted:
            logger.warning("采购员全部 reject: %s", pdf_name)
            all_farm_stats[track] = {"blocks": len(blocks), "accepted": 0,
                                      "matched": 0, "gaps": 0}
            continue

        farmer, results, gap_count = run_farmer(
            accepted, decisions, model, pdf_name,
        )
        total_gap_count += gap_count
        all_farmers.append(farmer)
        all_farm_stats[track] = {
            "blocks": len(blocks),
            "accepted": len(accepted),
            "matched": sum(1 for r in results if r.matched_node_id),
            "gaps": gap_count,
            "results": results,
        }

    final_kb_size = merge_to_sapling()
    _rebuild_graphrag_index()
    owner, report = run_farm_owner(total_gap_count)

    if owner and report:
        for track, stats in all_farm_stats.items():
            results = stats.get("results", [])
            unclassified = []
            for r in results:
                meta = (r.chunk or {}).get("metadata") or {}
                tp = meta.get("tree_position")
                if isinstance(tp, dict) and tp.get("tree_level"):
                    continue
                unclassified.append(r.chunk)
            if unclassified:
                classify_reqs = owner.classify_uncovered_chunks(unclassified, report)
                logger.info("农场主分类 [%s]: %d 未分类 → %d fill_requests",
                             track, len(unclassified), len(classify_reqs))

    fill_count = 0
    for farmer in all_farmers:
        fill_count += run_fill_cycle(farmer, report)

    enrich_count = _enrich_cli_from_app(REFERENCE_DIR)
    logger.info("CLI chunk 内容注入完成: %d 块", enrich_count)

    if fill_count > 0 or enrich_count > 0:
        final_kb_size = merge_to_sapling()

    return all_farm_stats, final_kb_size


def _enrich_cli_from_app(ref_dir: Path) -> int:
    """将 app 类文档的描述文本注入到对应 cli/reference chunk 的 page_content。

    匹配规则：cli 块的 command_prefix == app 块的 command_prefix（normalized）。
    只注入未达到 500 字符、且尚无大量 CJK 内容的 cli 块。

    Returns: 注入的块数。
    """
    _HAS_DENSE_CJK = re.compile(r'[\u4e00-\u9fff]{10,}')
    _INJECTED_MARKER = "<!-- app-enriched -->"

    # 1. 从所有 reference/*.json 收集 app 块，按 normalized command_prefix 建索引
    # value = 最长的 app 块 page_content
    app_index: Dict[str, str] = {}
    for ref_file in sorted(ref_dir.glob("*.json")):
        if ref_file.name == "commandtree_base.json" or "_bak" in ref_file.stem:
            continue
        try:
            data = json.loads(ref_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for chunk in data:
            m = chunk.get("metadata", {})
            if m.get("document_category", "").split("/")[0] != "app":
                continue
            cp = m.get("command_prefix", "").lower().replace("_", " ").strip()
            if not cp:
                continue
            pc = chunk.get("page_content", "")
            # 只收集有实质内容的描述块（有中文，长度足够）
            if len(pc) < 20 or not _HAS_DENSE_CJK.search(pc):
                continue
            if cp not in app_index or len(pc) > len(app_index[cp]):
                app_index[cp] = pc

    if not app_index:
        logger.info("_enrich_cli_from_app: no app blocks with valid command_prefix found")
        return 0

    # 2. 遍历 cli/reference 块，注入
    total_enriched = 0
    for ref_file in sorted(ref_dir.glob("*.json")):
        if ref_file.name == "commandtree_base.json" or "_bak" in ref_file.stem:
            continue
        try:
            data = json.loads(ref_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue

        file_changed = False
        for chunk in data:
            m = chunk.get("metadata", {})
            if m.get("document_category", "").split("/")[0] != "cli":
                continue
            pc = chunk.get("page_content", "")
            # 跳过已注入、已足够长、或已有大量 CJK 的块
            if _INJECTED_MARKER in pc:
                continue
            if len(pc) >= 500:
                continue
            if _HAS_DENSE_CJK.search(pc):
                continue
            cp = m.get("command_prefix", "").lower().replace("_", " ").strip()
            if not cp:
                continue
            app_desc = app_index.get(cp, "")
            if not app_desc:
                # 尝试前缀扫描（app 可能有更精确的子命令前缀）
                for ak, av in app_index.items():
                    if ak.startswith(cp + " ") or cp.startswith(ak + " "):
                        if not app_desc or len(av) > len(app_desc):
                            app_desc = av
            if not app_desc:
                continue
            # 截取前 300 字
            inject_text = app_desc[:300].rstrip()
            chunk["page_content"] = f"{pc}\n\n{inject_text}\n{_INJECTED_MARKER}"
            file_changed = True
            total_enriched += 1

        if file_changed:
            ref_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return total_enriched


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2.5: 树节点 MD 导出
# ══════════════════════════════════════════════════════════════════════════════

def export_tree_nodes_to_md(out_dir: Path):
    kb_data = _load_json(KB_PATH)
    if not kb_data:
        logger.warning("knowledge_base.json 为空，跳过树导出")
        return

    by_level: Dict[str, list] = {}
    for chunk in kb_data:
        m = chunk.get("metadata", {})
        tp = m.get("tree_position", {})
        tl = tp.get("tree_level", "unknown") if isinstance(tp, dict) else "unknown"
        by_level.setdefault(tl, []).append(chunk)

    out_dir.mkdir(parents=True, exist_ok=True)

    for level, chunks in sorted(by_level.items()):
        chunks.sort(key=lambda c: (
            c.get("metadata", {}).get("source_file", ""),
            c.get("metadata", {}).get("product_module", ""),
            c.get("metadata", {}).get("command_prefix", ""),
        ))
        lines = [
            f"# tree_level = {level}  ({len(chunks)} nodes)",
            "",
            f"| # | source_file | product_module | document_category | command_prefix | section_title | content_len | linked_nodes |",
            f"|---|-------------|----------------|-------------------|----------------|---------------|-------------|--------------|",
        ]
        for i, c in enumerate(chunks, 1):
            m = c.get("metadata", {})
            tp = m.get("tree_position", {})
            linked = tp.get("linked_nodes", []) if isinstance(tp, dict) else []
            linked_str = ", ".join(str(n) for n in linked[:3])
            if len(linked) > 3:
                linked_str += f" +{len(linked)-3}"
            pc_len = len(c.get("page_content", ""))
            src = m.get("source_file", "")
            mod = m.get("product_module", "")
            cat = m.get("document_category", "")
            cp = m.get("command_prefix", "")
            title = m.get("section_title", "")[:40]
            lines.append(
                f"| {i} | {src} | {mod} | {cat} | {cp} | {title} | {pc_len} | {linked_str} |"
            )
        md_path = out_dir / f"{level}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("树导出: %s = %d nodes → %s", level, len(chunks), md_path.name)

    summary_lines = [
        "# 知识树节点汇总",
        "",
        f"总节点数: {len(kb_data)}",
        "",
        "| tree_level | count |",
        "|------------|-------|",
    ]
    for level in sorted(by_level.keys()):
        summary_lines.append(f"| {level} | {len(by_level[level])} |")
    summary_path = out_dir / "summary.md"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
    logger.info("树汇总: %s", summary_path)
    tprint()
    tprint("  树节点导出:")
    for level in sorted(by_level.keys()):
        tprint(f"    {level}: {len(by_level[level])} nodes")
    tprint(f"  总计: {len(kb_data)} nodes → {out_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: 重建向量
# ══════════════════════════════════════════════════════════════════════════════

def rebuild_vectors():
    from INAGENT.workflow_config_generator import refresh_hybrid_vector_index
    logger.info("重建 Qdrant 向量索引...")
    hybrid, reranker, graphrag = refresh_hybrid_vector_index(force=True)
    logger.info("向量索引重建完成")
    return hybrid, reranker, graphrag


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4: 4-way 诊断
# ══════════════════════════════════════════════════════════════════════════════

_gr_entities = None
_gr_rels = None

def _load_graphrag():
    global _gr_entities, _gr_rels
    if _gr_entities is not None:
        return
    try:
        import pandas as pd
        _gr_entities = pd.read_parquet(GRAPHRAG_ENTITIES)
        _gr_rels = pd.read_parquet(GRAPHRAG_RELS)
        logger.info("GraphRAG: %d entities, %d rels", len(_gr_entities), len(_gr_rels))
    except Exception as exc:
        logger.warning("GraphRAG load failed: %s", exc)


def _cjk_overlap(kw: str, text: str, strict: bool = False) -> bool:
    if kw == text:
        return True
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in kw)
    if not has_cjk:
        return False
    if kw in text:
        return True
    if strict:
        return False
    if len(kw) >= 3:
        for j in range(len(kw) - 1):
            bg = kw[j:j+2]
            if any('\u4e00' <= c <= '\u9fff' for c in bg) and bg in text:
                return True
    return False


_MODULE_HUB_THRESHOLD = 200
_CJK_RELEVANCE_RATIO = 0.3


def _graphrag_search(query: str, keywords: List[str]) -> Dict[str, Any]:
    _load_graphrag()
    result = {"hit": False, "entity_matches": [], "rel_count": 0}
    if _gr_entities is None:
        return result

    title_col = "title" if "title" in _gr_entities.columns else "name"
    titles = _gr_entities[title_col].str.lower().tolist()
    descs = (_gr_entities["description"].str.lower().tolist()
             if "description" in _gr_entities.columns else [""] * len(titles))

    types = (_gr_entities["type"].str.upper().tolist()
             if "type" in _gr_entities.columns else [""] * len(titles))

    matched_entities = []
    for kw in keywords:
        kw_lower = kw.lower()
        # Pass 1: exact title match (works for SECTION entities whose title = section_title)
        exact = None
        for i, t in enumerate(titles):
            if str(t) == kw_lower:
                exact = str(t)
                break
        if exact:
            matched_entities.append(exact)
            continue
        # Pass 1.5: substring match for SECTION entities (length ratio guard)
        if len(kw_lower) >= 4:
            sub_match = None
            sub_len = 0
            for i, t in enumerate(titles):
                if types[i] != "SECTION":
                    continue
                t_str = str(t)
                shorter = min(len(kw_lower), len(t_str))
                longer = max(len(kw_lower), len(t_str))
                if shorter < 4 or shorter / longer < 0.3:
                    continue
                if kw_lower in t_str or t_str in kw_lower:
                    if sub_match is None or len(t_str) < sub_len:
                        sub_match = t_str
                        sub_len = len(t_str)
            if sub_match:
                matched_entities.append(sub_match)
                continue
        # Pass 2: CJK overlap on COMMAND/MODULE entities only (skip SECTION/DOCUMENT)
        use_strict = len(kw_lower) <= 3
        has_cjk_kw = any('\u4e00' <= c <= '\u9fff' for c in kw_lower)
        if not has_cjk_kw and len(kw_lower) <= 5:
            continue
        for i, t in enumerate(titles):
            if types[i] in ("SECTION", "DOCUMENT"):
                continue
            if _cjk_overlap(kw_lower, str(t), strict=use_strict) or (
                not use_strict and _cjk_overlap(kw_lower, str(descs[i]))
            ):
                matched_entities.append(str(t))
                break

    if matched_entities:
        result["entity_matches"] = matched_entities[:5]

    has_specific = False
    if _gr_rels is not None and matched_entities:
        src_col = "source" if "source" in _gr_rels.columns else "source_id"
        tgt_col = "target" if "target" in _gr_rels.columns else "target_id"
        rel_count = 0
        for ent in matched_entities[:3]:
            mask = (_gr_rels[src_col].str.lower() == ent) | (_gr_rels[tgt_col].str.lower() == ent)
            cnt = int(mask.sum())
            rel_count += cnt
            ent_type = ""
            ent_mask = _gr_entities[title_col].str.lower() == ent
            if ent_mask.any():
                ent_type = str(_gr_entities.loc[ent_mask, "type"].iloc[0]).upper() if "type" in _gr_entities.columns else ""
            if cnt > 0:
                if ent_type == "DOCUMENT":
                    pass
                elif ent_type == "MODULE" and cnt > _MODULE_HUB_THRESHOLD:
                    pass
                else:
                    has_specific = True
        result["rel_count"] = rel_count

    result["hit"] = bool(result["entity_matches"]) and result["rel_count"] > 0 and has_specific
    return result


_hybrid_retriever = None

_VEC_HIT_THRESHOLD = 0.008

def _init_retriever():
    global _hybrid_retriever
    if _hybrid_retriever is not None:
        return
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()
    from INAGENT.workflow_config_generator import initialize_rag_system
    hybrid, _reranker, _graphrag = initialize_rag_system()
    _hybrid_retriever = hybrid
    logger.info("HybridRetriever initialized")


_META_PREFIX = "INAGENT_META_JSON:"

def _extract_text_meta(text: str) -> tuple:
    if text.startswith(_META_PREFIX):
        nl = text.find("\n")
        if nl > 0:
            try:
                meta = json.loads(text[len(_META_PREFIX):nl])
                return text[nl + 1:], meta
            except Exception:
                pass
    return text, {}


def _vector_search(query: str, top_k: int = 10) -> Dict[str, Any]:
    _init_retriever()
    result = {"hit": False, "rank": None, "top1_cat": "", "top1_module": "",
              "top1_command_prefix": "", "top1_section_title": "",
              "top3_snippets": [], "scores": [], "relevant": False}
    try:
        raw = _hybrid_retriever.query(query, top_k=top_k, return_detailed_info=True)
        items = raw.get("Retrieved Context", []) if isinstance(raw, dict) else raw
        if not items:
            return result
        top_score = 0.0
        all_text = ""
        for i, h in enumerate(items[:3]):
            if not isinstance(h, dict):
                continue
            text = str(h.get("text", ""))
            s = float(h.get("rrf_score", 0) or h.get("score", 0))
            if i == 0:
                top_score = s
            clean, meta = _extract_text_meta(text)
            result["top3_snippets"].append(clean[:80])
            all_text += " " + clean.lower()
            for v in meta.values():
                if isinstance(v, str):
                    all_text += " " + v.lower()
            if i == 0:
                result["top1_cat"] = meta.get("document_category", "")
                result["top1_module"] = meta.get("product_module", "")
                result["top1_command_prefix"] = meta.get("command_prefix", "")
                result["top1_section_title"] = meta.get("section_title", "")
        for h in items[:5]:
            if isinstance(h, dict):
                s = h.get("rrf_score", 0) or h.get("score", 0)
            else:
                s = 0
            result["scores"].append(round(float(s), 4) if s else 0)
        result["hit"] = top_score >= _VEC_HIT_THRESHOLD
        q_words = [w.lower() for w in query.split() if len(w) > 1]
        has_cjk_q = any('\u4e00' <= c <= '\u9fff' for c in query)
        if has_cjk_q:
            q_cjk = [c for c in query.lower() if '\u4e00' <= c <= '\u9fff']
            if q_cjk:
                matched_c = sum(1 for c in q_cjk if c in all_text)
                title_match = matched_c >= max(2, len(q_cjk) * _CJK_RELEVANCE_RATIO)
            else:
                title_match = True
        else:
            title_match = any(w in all_text for w in q_words if len(w) >= 2)
        result["relevant"] = result["hit"] and title_match
    except Exception as exc:
        logger.warning("Vector search error: %s", exc)
    return result


# ── CommandTree 诊断 ──────────────────────────────────────────────────────

def diagnose_commandtree(ct_data: list) -> Dict[str, Any]:
    logger.info("=" * 60)
    logger.info("CommandTree 诊断")
    logger.info("=" * 60)

    total = len(ct_data)
    no_prefix = sum(1 for e in ct_data if not e.get("metadata", {}).get("command_prefix"))
    no_module = sum(1 for e in ct_data if not e.get("metadata", {}).get("product_module"))
    empty_content = sum(1 for e in ct_data if len(e.get("page_content", "")) < 10)

    modules = {}
    for e in ct_data:
        mod = e.get("metadata", {}).get("product_module", "unknown")
        modules[mod] = modules.get(mod, 0) + 1

    tree_levels = {}
    for e in ct_data:
        tl = e.get("metadata", {}).get("tree_position", {}).get("tree_level", "none")
        tree_levels[tl] = tree_levels.get(tl, 0) + 1

    result = {
        "total": total,
        "no_prefix": no_prefix,
        "no_module": no_module,
        "empty_content": empty_content,
        "modules": modules,
        "tree_levels": tree_levels,
    }

    tprint()
    tprint("  CommandTree 诊断结果:")
    tprint(f"    总节点数: {total}")
    tprint(f"    缺少 command_prefix: {no_prefix}")
    tprint(f"    缺少 product_module: {no_module}")
    tprint(f"    空白内容(< 10字符): {empty_content}")
    tprint(f"    模块分布: {dict(sorted(modules.items(), key=lambda x: -x[1])[:10])}")
    tprint(f"    树层级分布: {tree_levels}")

    issues = []
    if no_prefix > total * 0.1:
        issues.append(f"  [!] {no_prefix}/{total} 节点缺少 command_prefix")
    if no_module > total * 0.1:
        issues.append(f"  [!] {no_module}/{total} 节点缺少 product_module")
    if issues:
        for iss in issues:
            tprint(iss)
    else:
        tprint("    CommandTree 结构正常")

    return result


# ── CLI 4-way ─────────────────────────────────────────────────────────────

def _extract_cmd_from_content(content: str) -> str:
    """从 page_content 第一行提取 CLI 命令前缀（仅 token，不含参数）。
    找到以小写 ASCII 开头、含 < [ { 的行，提取参数前的 tokens。"""
    import re
    _HAS_CJK = re.compile(r'[\u4e00-\u9fff]')
    _PARAM_RE = re.compile(r'[<\[{]')
    for line in content.splitlines():
        line = line.strip()
        if not line or not line[0].islower() or not line[0].isascii():
            continue
        if _HAS_CJK.search(line):
            continue
        if not _PARAM_RE.search(line):
            continue
        tokens = line.split()
        cmd_tokens = []
        for tok in tokens:
            if _PARAM_RE.match(tok):
                break
            if not tok.replace('-', '').replace('_', '').isalnum():
                break
            cmd_tokens.append(tok)
        if len(cmd_tokens) >= 2:
            return " ".join(cmd_tokens)
    return ""


def _ct_parent_exists(prefix: str, ct_data: list) -> bool:
    """Return True if CT has any entry whose command_prefix exactly matches prefix."""
    prefix_norm = prefix.lower().replace("_", " ").strip()
    if not prefix_norm:
        return False
    for entry in ct_data:
        cp = entry.get("metadata", {}).get("command_prefix", "").lower().replace("_", " ")
        if cp == prefix_norm:
            return True
    return False


def _ct_lookup_fuzzy(section_title: str, ct_data: list) -> Dict[str, Any]:
    import re
    cleaned = re.sub(r"\{[^}]*\}", "", section_title).strip()
    title_lower = cleaned.lower()
    title_norm = title_lower.replace("_", " ")
    title_words = title_norm.split()
    if not title_words:
        return {"found": False, "content_len": 0, "module": "", "snippet": "", "match_type": "none"}
    for entry in ct_data:
        m = entry.get("metadata", {})
        prefix = m.get("command_prefix", "").lower()
        prefix_norm = prefix.replace("_", " ")
        if prefix_norm == title_norm or prefix == title_lower:
            pc = entry.get("page_content", "")
            return {"found": True, "content_len": len(pc),
                    "module": m.get("product_module", ""), "snippet": pc[:120],
                    "match_type": "exact"}
    if len(title_words) < 2:
        return {"found": False, "content_len": 0, "module": "", "snippet": "", "match_type": "none"}
    best = None
    best_score = 0
    best_is_prefix_fallback = False
    for entry in ct_data:
        m = entry.get("metadata", {})
        prefix = m.get("command_prefix", "").lower().replace("_", " ")
        prefix_words = prefix.split()
        if not prefix_words:
            continue
        # title 比 CT 更长（title 以 CT prefix 开头）→ 子命令，parent-match
        if title_norm.startswith(prefix + " "):
            score = len(prefix_words) / len(title_words)
            if score > best_score:
                best = entry
                best_score = score
                best_is_prefix_fallback = True
            continue
        # CT 比 title 更长（CT 以 title 开头）→ title 是前缀，也是 prefix-fallback
        if prefix.startswith(title_norm + " "):
            score = len(title_words) / len(prefix_words)
            if score > best_score:
                best = entry
                best_score = score
                best_is_prefix_fallback = True
            continue
        common = sum(1 for w in title_words if w in prefix_words)
        score = common / max(len(title_words), len(prefix_words))
        if score > best_score and score >= 0.7:
            best = entry
            best_score = score
            best_is_prefix_fallback = False
    if best:
        m = best.get("metadata", {})
        pc = best.get("page_content", "")
        match_type = "prefix-fallback" if best_is_prefix_fallback else "token-match"
        ct_cmd = m.get("command_prefix", "")
        snippet = f"[parent-match] {ct_cmd}" if best_is_prefix_fallback else pc[:120]
        content_len = 0 if best_is_prefix_fallback else len(pc)
        return {"found": True, "content_len": content_len,
                "module": m.get("product_module", ""), "snippet": snippet,
                "match_type": match_type}
    return {"found": False, "content_len": 0, "module": "", "snippet": "", "match_type": "none"}


def run_cli_4way(ct_data: list, cli_blocks: list, count: int, seed: int):
    import re
    seen_titles: Set[str] = set()
    candidates = []
    for e in cli_blocks:
        if len(e.get("page_content", "")) < 80:
            continue
        m = e.get("metadata", {})
        if m.get("product_module", "unknown") == "unknown":
            continue
        title = m.get("section_title", "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        cp = m.get("command_prefix", "").strip()
        if cp:
            candidates.append(e)
        elif re.match(r'^[a-z]', title) and len(title.split()) <= 6:
            candidates.append(e)

    random.seed(seed)
    random.shuffle(candidates)
    sample = candidates[:min(count, len(candidates))]

    results = []
    for entry in sample:
        m = entry.get("metadata", {})
        section = m.get("section_title", "")
        module = m.get("product_module", "")
        query = section + " " + module

        _pc = entry.get("page_content", "")
        s1 = {"found": len(_pc) >= 80,
              "content_len": len(_pc), "snippet": _pc[:120]}
        cmd_prefix = m.get("command_prefix", "")
        # Resolve tree_level early (needed for new_leaf S2 check below)
        bid = m.get("block_id") or m.get("chunk_id") or ""
        src = m.get("source_file", "")
        tree_level = _lookup_tree_level(block_id=bid, title=section, source_file=src)
        if tree_level == "?":
            tp = m.get("tree_position", {})
            tree_level = tp.get("tree_level", "?") if isinstance(tp, dict) else "?"
        s2 = _ct_lookup_fuzzy(section, ct_data)
        if not s2["found"] and cmd_prefix and cmd_prefix.lower() != section.lower():
            s2 = _ct_lookup_fuzzy(cmd_prefix, ct_data)
        # CJK title fallback: extract CLI command from page_content
        if not s2["found"] and any('\u4e00' <= c <= '\u9fff' for c in section):
            _extracted = _extract_cmd_from_content(_pc)
            if _extracted:
                s2 = _ct_lookup_fuzzy(_extracted, ct_data)
                if not s2["found"]:
                    # try parent prefixes of extracted command
                    _ext_tokens = _extracted.split()
                    for _end in range(len(_ext_tokens) - 1, 0, -1):
                        _parent = " ".join(_ext_tokens[:_end])
                        if _ct_parent_exists(_parent, ct_data):
                            s2 = {"found": True, "content_len": 0, "module": "",
                                  "snippet": f"[cjk-fallback] parent in CT: {_parent}"}
                            break
            # single-token ASCII cmd_prefix: scan CT for any entry starting with it
            # 只允许有语义的前缀（3+ chars），并排除高频无意义前缀
            if not s2["found"] and cmd_prefix and cmd_prefix.replace(' ', '').isascii():
                _cp_lower = cmd_prefix.lower().strip()
                if (_cp_lower and ' ' not in _cp_lower
                        and len(_cp_lower) >= 3
                        and _cp_lower not in _CJK_SCAN_BLACKLIST):
                    for _ct_entry in ct_data:
                        _ct_cp = _ct_entry.get("metadata", {}).get("command_prefix", "").lower()
                        if _ct_cp == _cp_lower or _ct_cp.startswith(_cp_lower + " "):
                            s2 = {"found": True, "content_len": 0,
                                  "module": _ct_entry.get("metadata", {}).get("product_module", ""),
                                  "snippet": f"[cjk-scan] prefix match: {_ct_cp}",
                                  "match_type": "cjk-scan"}
                            break
        # new_leaf S2: command not in CT but parent is — verify parent exists
        if not s2["found"] and tree_level == "new_leaf":
            _cp_tokens = cmd_prefix.strip().split() if cmd_prefix else []
            for _end in range(len(_cp_tokens) - 1, 0, -1):
                _parent = " ".join(_cp_tokens[:_end])
                if _ct_parent_exists(_parent, ct_data):
                    s2 = {"found": True, "content_len": 0, "module": "",
                          "snippet": f"[new_leaf] parent in CT: {_parent}"}
                    break
        s3 = _vector_search(query, top_k=10)
        _top1_cp = s3.get("top1_command_prefix", "").lower().replace("_", " ")
        _top1_title = s3.get("top1_section_title", "").lower()
        _target_cp = section.lower().replace("_", " ")
        _target_words = {w for w in _target_cp.split() if len(w) >= 2}
        _has_cjk = any('\u4e00' <= c <= '\u9fff' for c in _target_cp)
        if s3["hit"]:
            top_text = (_top1_cp + " " + _top1_title + " " +
                        " ".join(s3.get("top3_snippets", []))).lower()
            if _has_cjk:
                target_cjk = [c for c in _target_cp if '\u4e00' <= c <= '\u9fff']
                if target_cjk:
                    matched = sum(1 for c in target_cjk if c in top_text)
                    s3["relevant"] = matched >= max(2, len(target_cjk) * _CJK_RELEVANCE_RATIO)
                else:
                    s3["relevant"] = True
            elif _target_words:
                matched_words = sum(1 for w in _target_words if w in top_text)
                s3["relevant"] = matched_words >= max(1, len(_target_words) * 0.5)
        kws = [section] + section.split()[:3] + ([module] if module else [])
        s4 = _graphrag_search(query, kws)

        results.append({
            "cmd": section, "module": module, "tree_level": tree_level,
            "query": query, "kws": kws,
            "s1_pdf": s1, "s2_ct": s2, "s3_vec": s3, "s4_gr": s4,
        })
    return results


# ── APP 4-way ─────────────────────────────────────────────────────────────

_GENERIC_TITLES = frozenset({
    "概述", "简介", "说明", "注意事项", "配置说明", "功能介绍", "使用说明",
    "overview", "introduction", "description", "summary",
})


def _enrich_generic_query(section: str, module: str, path: str,
                          content: str = "") -> tuple:
    if section.strip().lower() not in _GENERIC_TITLES:
        return (section + " " + (module or "") + " " + (path or ""),
                section.split()[:3] + ([module] if module else []))
    parts = [p.strip() for p in path.split(">") if p.strip()] if path else []
    parent = ""
    for p in reversed(parts):
        if p.strip().lower() not in _GENERIC_TITLES:
            parent = p.strip()
            break
    if not parent and module:
        parent = module
    content_snippet = ""
    if content:
        clean = content.strip().replace("\n", " ")[:200]
        tokens = [w for w in clean.split() if len(w) >= 2][:5]
        content_snippet = " ".join(tokens)
    if parent:
        query = f"{parent} {section}"
        if content_snippet:
            query = f"{parent} {content_snippet}"
        kws = parent.split()[:3] + [section]
    else:
        query = section + " " + (content_snippet or path or "")
        kws = section.split()[:3]
    if content_snippet:
        kws.extend([w for w in content_snippet.split()[:3] if w not in kws])
    if module:
        kws.append(module)
    return query, kws


def run_app_4way(app_blocks: list, ct_data: list, count: int, seed: int,
                 label: str = "APP"):
    candidates = []
    for e in app_blocks:
        if len(e.get("page_content", "")) <= 80:
            continue
        m = e.get("metadata", {})
        title = m.get("section_title", "").strip()
        if not title or title == "⽬录":
            continue
        candidates.append(e)

    random.seed(seed)
    sample = random.sample(candidates, min(count, len(candidates)))

    results = []
    for entry in sample:
        m = entry.get("metadata", {})
        section = m.get("section_title", "")
        module = m.get("product_module", "")
        path = m.get("section_path", "")
        pc = entry.get("page_content", "")
        query, kws = _enrich_generic_query(section, module, path, content=pc)

        s1 = {"found": len(pc) >= 80,
              "content_len": len(pc), "snippet": pc[:150]}

        bid = m.get("block_id") or m.get("chunk_id") or ""
        src = m.get("source_file", "")
        tree_level = _lookup_tree_level(block_id=bid, title=section, source_file=src)
        if tree_level == "?":
            tp = m.get("tree_position", {})
            tree_level = tp.get("tree_level", "?") if isinstance(tp, dict) else "?"
        doc_cat = (m.get("document_category")
                   or _lookup_doc_cat(block_id=bid, title=section, source_file=src)
                   or "?")

        s2_tree = {"valid": tree_level != "?", "tree_level": tree_level,
                    "doc_category": doc_cat}

        s3 = _vector_search(query.strip(), top_k=10)
        s4 = _graphrag_search(query, kws)

        results.append({
            "section": section, "module": module, "path": path,
            "tree_level": tree_level, "label": label,
            "query": query.strip(), "kws": kws,
            "s1_src": s1, "s2_tree": s2_tree, "s3_vec": s3, "s4_gr": s4,
        })
    return results


# ── ARCH 4-way ────────────────────────────────────────────────────────────

def run_arch_4way(arch_blocks: list, count: int, seed: int):
    candidates = []
    for e in arch_blocks:
        if len(e.get("page_content", "")) <= 80:
            continue
        m = e.get("metadata", {})
        title = m.get("section_title", "").strip()
        if not title:
            continue
        candidates.append(e)

    random.seed(seed)
    sample = random.sample(candidates, min(count, len(candidates)))

    results = []
    for entry in sample:
        m = entry.get("metadata", {})
        section = m.get("section_title", "")
        module = m.get("product_module", "")
        path = m.get("section_path", "")
        pc = entry.get("page_content", "")
        query, kws = _enrich_generic_query(section, module, path, content=pc)

        s1 = {"found": len(pc) >= 80,
              "content_len": len(pc), "snippet": pc[:150]}

        bid = m.get("block_id") or m.get("chunk_id") or ""
        src = m.get("source_file", "")
        tree_level = _lookup_tree_level(block_id=bid, title=section, source_file=src)
        if tree_level == "?":
            tp = m.get("tree_position", {})
            tree_level = tp.get("tree_level", "?") if isinstance(tp, dict) else "?"
        s2_tree = {"valid": tree_level != "?",
                    "tree_level": tree_level,
                    "doc_category": m.get("document_category", "?")}

        s3 = _vector_search(query.strip(), top_k=10)
        s4 = _graphrag_search(query, kws)

        results.append({
            "section": section, "module": module, "path": path,
            "tree_level": tree_level,
            "query": query.strip(), "kws": kws,
            "s1_src": s1, "s2_tree": s2_tree, "s3_vec": s3, "s4_gr": s4,
        })
    return results


# ── 报告输出 ──────────────────────────────────────────────────────────────

def print_track_report(track_name: str, results: list, s2_label: str = "参照源"):
    tprint()
    tprint("=" * 90)
    tprint(f"  {track_name} 4-way 一致性报告 ({len(results)} entries)")
    tprint("=" * 90)

    totals = {"s1": 0, "s2": 0, "s3": 0, "s3r": 0, "s4": 0}
    for i, r in enumerate(results):
        s1_ok = r.get("s1_pdf", r.get("s1_src", {})).get("found", False)
        if "s2_ct" in r:
            s2_ok = r["s2_ct"]["found"]
            s2_parent_match = (r["s2_ct"].get("match_type") == "prefix-fallback")
        elif "s2_tree" in r:
            s2_ok = r["s2_tree"]["valid"]
            s2_parent_match = False
        else:
            s2_ok = False
            s2_parent_match = False

        s3_hit = r["s3_vec"]["hit"]
        s3_rel = r["s3_vec"].get("relevant", False)
        s4_hit = r["s4_gr"]["hit"]

        s1 = PASS if s1_ok else FAIL
        # parent-match: 父节点在 CT，是正确的 new_leaf 状态，计入总分但标注 △
        s2 = WARN if (s2_ok and s2_parent_match) else (PASS if s2_ok else FAIL)
        s3 = PASS if s3_rel else (WARN if s3_hit else FAIL)
        s4 = PASS if s4_hit else FAIL

        if s1_ok: totals["s1"] += 1
        if s2_ok: totals["s2"] += 1
        if s3_hit: totals["s3"] += 1
        if s3_rel: totals["s3r"] += 1
        if s4_hit: totals["s4"] += 1

        label = r.get("cmd", r.get("section", "?"))[:28]
        module = r.get("module", "?")[:10]
        tl = r.get("tree_level", "?")

        vec_note = ""
        if s3_hit:
            score_str = ",".join(str(s) for s in r["s3_vec"]["scores"][:3])
            vec_note = f" [{score_str}]"

        gr_note = ""
        if s4_hit:
            ents = r["s4_gr"]["entity_matches"][:2]
            gr_note = f" ents={ents} rels={r['s4_gr']['rel_count']}"

        tprint(f"  {i+1:2d}. [{label:28s}] mod={module:10s} tl={tl:8s}")
        tprint(f"      S1:{s1} S2({s2_label}):{s2} S3:{s3}{vec_note} S4:{s4}{gr_note}")

        if s2_ok and s2_parent_match:
            tprint(f"      [△] S2 parent-match: 子命令是 new_leaf，父节点在 CT")

        if s3_hit and not s3_rel:
            snippet = r["s3_vec"]["top3_snippets"][0][:60] if r["s3_vec"]["top3_snippets"] else ""
            tprint(f"      [△] 向量命中但关键词不在结果中: {snippet!r}")

    n = len(results)
    tprint()
    tprint("  ─── 汇总 ───")
    tprint(f"  S1 原文:        {totals['s1']}/{n} ({100*totals['s1']//n if n else 0}%)")
    tprint(f"  S2 {s2_label}:  {totals['s2']}/{n} ({100*totals['s2']//n if n else 0}%)")
    tprint(f"  S3 向量(命中):  {totals['s3']}/{n} ({100*totals['s3']//n if n else 0}%)")
    tprint(f"  S3 向量(相关):  {totals['s3r']}/{n} ({100*totals['s3r']//n if n else 0}%)")
    tprint(f"  S4 GraphRAG:    {totals['s4']}/{n} ({100*totals['s4']//n if n else 0}%)")
    tprint("=" * 90)
    return totals


def print_pipeline_summary(farm_stats: Dict, kb_size: int):
    tprint()
    tprint("=" * 70)
    tprint("  Pipeline 处理统计")
    tprint("=" * 70)
    for track, stats in farm_stats.items():
        if isinstance(stats, dict):
            tprint(f"  {track}: blocks={stats.get('blocks',0)}, "
                   f"accepted={stats.get('accepted',0)}, "
                   f"matched={stats.get('matched',0)}, "
                   f"gaps={stats.get('gaps',0)}")
    tprint(f"  最终 knowledge_base.json: {kb_size} chunks")
    tprint("=" * 70)


def export_detail_md(all_results: Dict[str, list], md_path: Path):
    PASS_ICON, WARN_ICON, FAIL_ICON = "✅", "⚠️", "❌"
    lines: list[str] = []
    w = lines.append

    w("# 4-way 检索详情报告")
    w("")
    w(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    w("")

    for track_name, results in all_results.items():
        w(f"## {track_name} ({len(results)} entries)")
        w("")

        for i, r in enumerate(results):
            label = r.get("cmd", r.get("section", "?"))
            module = r.get("module", "?")
            tl = r.get("tree_level", "?")
            query = r.get("query", "")
            kws = r.get("kws", [])

            s1 = r.get("s1_pdf", r.get("s1_src", {}))
            s3 = r["s3_vec"]
            s4 = r["s4_gr"]

            s1_ok = s1.get("found", False)
            s3_hit = s3.get("hit", False)
            s3_rel = s3.get("relevant", False)
            s4_hit = s4.get("hit", False)

            if "s2_ct" in r:
                s2 = r["s2_ct"]
                s2_ok = s2.get("found", False)
                s2_parent_match = (s2.get("match_type") == "prefix-fallback")
                s2_label = "CommandTree"
            elif "s2_tree" in r:
                s2 = r["s2_tree"]
                s2_ok = s2.get("valid", False)
                s2_parent_match = False
                s2_label = "TreeLevel"
            else:
                s2 = {}
                s2_ok = False
                s2_parent_match = False
                s2_label = "?"

            s2_icon = (WARN_ICON if (s2_ok and s2_parent_match)
                       else (PASS_ICON if s2_ok else FAIL_ICON))
            verdict = f"S1:{PASS_ICON if s1_ok else FAIL_ICON} " \
                       f"S2:{s2_icon} " \
                       f"S3:{PASS_ICON if s3_rel else (WARN_ICON if s3_hit else FAIL_ICON)} " \
                       f"S4:{PASS_ICON if s4_hit else FAIL_ICON}"

            w(f"### {i+1}. {label}")
            w("")
            w(f"| 属性 | 值 |")
            w(f"|------|------|")
            w(f"| 模块 | `{module}` |")
            w(f"| 树层级 | `{tl}` |")
            w(f"| 判定 | {verdict} |")
            w(f"| 检索 query | `{query}` |")
            w(f"| 检索 keywords | `{kws}` |")
            w("")

            w(f"#### S1 原文内容 ({s2_label})")
            w("")
            w(f"- 长度: {s1.get('content_len', 0)} 字符")
            snippet = s1.get("snippet", "")
            if snippet:
                w(f"- 片段:")
                w(f"  ```")
                w(f"  {snippet}")
                w(f"  ```")
            w("")

            w(f"#### S2 {s2_label} 查找")
            w("")
            if s2_label == "CommandTree":
                w(f"- 命中: {s2_icon}")
                if s2_parent_match:
                    w(f"- 类型: `parent-match`（子命令不在 CT，是 new_leaf）")
                w(f"- 内容长度: {s2.get('content_len', 0)}")
                ct_snippet = s2.get("snippet", "")
                if ct_snippet:
                    w(f"- 片段: `{ct_snippet[:80]}`")
            else:
                w(f"- 有效: {PASS_ICON if s2_ok else FAIL_ICON}")
                w(f"- tree_level: `{s2.get('tree_level', '?')}`")
                w(f"- document_category: `{s2.get('doc_category', '?')}`")
            w("")

            w(f"#### S3 向量检索")
            w("")
            scores = s3.get("scores", [])
            w(f"- 命中: {PASS_ICON if s3_hit else FAIL_ICON}  相关: {PASS_ICON if s3_rel else (WARN_ICON if s3_hit else FAIL_ICON)}")
            w(f"- Top scores: `{scores[:5]}`")
            w(f"- Top1 module: `{s3.get('top1_module', '')}`")
            w(f"- Top1 command_prefix: `{s3.get('top1_command_prefix', '')}`")
            w(f"- Top1 section_title: `{s3.get('top1_section_title', '')}`")
            w(f"- Top1 category: `{s3.get('top1_cat', '')}`")
            snippets = s3.get("top3_snippets", [])
            if snippets:
                w(f"- Top3 片段:")
                for si, snip in enumerate(snippets):
                    w(f"  {si+1}. `{snip}`")
            w("")

            w(f"#### S4 GraphRAG")
            w("")
            w(f"- 命中: {PASS_ICON if s4_hit else FAIL_ICON}")
            w(f"- 匹配实体: `{s4.get('entity_matches', [])}`")
            w(f"- 关系数: {s4.get('rel_count', 0)}")
            w("")
            w("---")
            w("")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("详情 MD 已写入: %s", md_path)


def print_final_summary(all_totals: Dict[str, Dict]):
    tprint()
    tprint("=" * 90)
    tprint("  ███ 综合评估 ███")
    tprint("=" * 90)

    all_ok = True
    for track_name, tots in all_totals.items():
        n = tots.get("n", 1)
        for src, label in [("s1", "原文"), ("s2", "参照"),
                           ("s3", "向量命中"), ("s3r", "向量相关"), ("s4", "GraphRAG")]:
            cnt = tots.get(src, 0)
            pct = 100 * cnt // n if n else 0
            is_gate = src not in ("s3",)
            status = PASS if pct >= 80 else (WARN if pct >= 50 else FAIL)
            if pct < 80 and is_gate:
                all_ok = False
            marker = "" if pct >= 80 else (" ← 需关注" if is_gate else " (参考)")
            tprint(f"  {status} {track_name:8s} {label:10s}: {cnt}/{n} ({pct}%){marker}")

    if all_ok:
        tprint("\n  ✓ 全部关键项 ≥80%，流程验证通过")
    else:
        tprint("\n  ✗ 存在 <80% 的检查项，需排查")
    tprint("=" * 90)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="综合 4-way 端到端流水线")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cli-count", type=int, default=10)
    ap.add_argument("--app-count", type=int, default=5)
    ap.add_argument("--arch-count", type=int, default=5)
    ap.add_argument("--haslb-count", type=int, default=5)
    ap.add_argument("--skip-autoconvert", action="store_true",
                    help="跳过 auto_convert（假设 doc_local_reference 已就绪）")
    ap.add_argument("--clear-autoconvert", action="store_true",
                    help="删除 doc_local_reference 缓存，强制全部重跑 auto_convert")
    ap.add_argument("--skip-pipeline", action="store_true",
                    help="跳过采购员→农民→农场主（只跑4way诊断）")
    ap.add_argument("--skip-vectors", action="store_true",
                    help="跳过向量重建")
    args = ap.parse_args()

    _init_report_log()

    try:
        _main_inner(args)
    finally:
        if _report_file:
            _report_file.close()
            logger.info("报告已写入: %s", LOGS_DIR / "4way_report.txt")
        _cleanup_qdrant()


def _main_inner(args):
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    start_time = time.perf_counter()
    logger.info("█" * 60)
    logger.info("综合 4-way 端到端流水线")
    logger.info("█" * 60)

    # Phase 0: 预检 doc_local_reference
    logger.info("\n>>> Phase 0: 预检 doc_local_reference")

    if args.clear_autoconvert:
        logger.info("清除 auto_convert 缓存...")
        if DOC_LOCAL_REF.exists():
            for f in DOC_LOCAL_REF.glob("*.json"):
                f.unlink()
                logger.debug("删除 doc_local_ref: %s", f.name)
        for f in LOGS_DIR.glob("*.cache.json"):
            f.unlink()
            logger.debug("删除 cache: %s", f.name)
        logger.info("auto_convert 缓存已清除")

    # Phase 1: 恢复快照（必须在 auto_convert 前，使 reference 缓存失效）
    if not args.skip_pipeline:
        logger.info("\n>>> Phase 1: 恢复小树苗快照")
        sapling_size = restore_snapshot()

    doc_statuses = check_doc_local_refs()

    missing = [stem for stem, exists in doc_statuses.items() if not exists]
    if missing and not args.skip_autoconvert:
        logger.info("需要 auto_convert: %s", missing)
        import INAGENT.data_tools.auto_convert as _ac_mod
        _ac_mod._project_config_cache = None
        _saved_cache = _ac_mod._project_config_cache
        _ac_mod._project_config_cache = {}
        logger.info("已禁用 auto_convert LLM batch（4-way 不需要 LLM metadata）")
        try:
            asyncio.run(run_autoconvert_for_missing(missing))
        finally:
            _ac_mod._project_config_cache = None
        doc_statuses = check_doc_local_refs()

    missing_after = [stem for stem, exists in doc_statuses.items() if not exists]
    if missing_after:
        logger.warning("仍缺少 doc_local_reference: %s", missing_after)

    # Phase 2: 采购员→农民→农场主
    if not args.skip_pipeline:
        logger.info("\n>>> Phase 2: 采购员→农民→合并→农场主")
        farm_stats, final_kb_size = run_full_pipeline(doc_statuses)
        print_pipeline_summary(farm_stats, final_kb_size)

    # Phase 2.5: 导出树节点到 MD
    logger.info("\n>>> Phase 2.5: 导出树节点到 MD")
    tree_md_dir = LOGS_DIR / "tree_nodes"
    export_tree_nodes_to_md(tree_md_dir)

    # Phase 3: 重建向量
    if not args.skip_vectors:
        logger.info("\n>>> Phase 3: 重建向量索引")
        rebuild_vectors()

    # Phase 4: 4-way 诊断
    logger.info("\n>>> Phase 4: 4-way 诊断")

    ct_data = _load_json(CT_PATH)
    ct_diag = diagnose_commandtree(ct_data)

    all_totals = {}

    # CLI track
    cli_ref = DOC_LOCAL_REF / "cli_1-82.json"
    if cli_ref.exists():
        cli_blocks = _load_json(cli_ref)
        logger.info("CLI 4-way: %d source blocks, %d random (seed=%d)",
                     len(cli_blocks), args.cli_count, args.seed)
        cli_results = run_cli_4way(ct_data, cli_blocks, args.cli_count, args.seed)
        cli_totals = print_track_report("CLI", cli_results, s2_label="CommandTree")
        cli_totals["n"] = len(cli_results)
        all_totals["CLI"] = cli_totals
    else:
        logger.warning("跳过 CLI 4-way: %s 不存在", cli_ref)

    # APP track (app_1-40)
    app_ref = DOC_LOCAL_REF / "app_1-40.json"
    if app_ref.exists():
        app_blocks = _load_json(app_ref)
        logger.info("APP 4-way: %d source blocks, %d random (seed=%d)",
                     len(app_blocks), args.app_count, args.seed)
        app_results = run_app_4way(app_blocks, ct_data, args.app_count, args.seed,
                                    label="APP")
        app_totals = print_track_report("APP", app_results, s2_label="TreeLevel")
        app_totals["n"] = len(app_results)
        all_totals["APP"] = app_totals
    else:
        logger.warning("跳过 APP 4-way: %s 不存在", app_ref)

    # ARCH track (ustack)
    arch_ref = DOC_LOCAL_REF / "ustack设计架构V3.json"
    if arch_ref.exists():
        arch_blocks = _load_json(arch_ref)
        logger.info("ARCH 4-way: %d source blocks, %d random (seed=%d)",
                     len(arch_blocks), args.arch_count, args.seed)
        arch_results = run_arch_4way(arch_blocks, args.arch_count, args.seed)
        arch_totals = print_track_report("ARCH", arch_results, s2_label="TreeLevel(assigned)")
        arch_totals["n"] = len(arch_results)
        all_totals["ARCH"] = arch_totals
    else:
        logger.warning("跳过 ARCH 4-way: %s 不存在", arch_ref)

    # HA/SLB track (app_65-72)
    haslb_ref = DOC_LOCAL_REF / "app_65-72.json"
    if haslb_ref.exists():
        haslb_blocks = _load_json(haslb_ref)
        logger.info("HA/SLB 4-way: %d source blocks, %d random (seed=%d)",
                     len(haslb_blocks), args.haslb_count, args.seed)
        haslb_results = run_app_4way(haslb_blocks, ct_data, args.haslb_count,
                                      args.seed + 1, label="HA-SLB")
        haslb_totals = print_track_report("HA-SLB", haslb_results, s2_label="TreeLevel")
        haslb_totals["n"] = len(haslb_results)
        all_totals["HA-SLB"] = haslb_totals
    else:
        logger.warning("跳过 HA/SLB 4-way: %s 不存在", haslb_ref)

    # 综合评估
    print_final_summary(all_totals)

    # 详情 MD 导出
    all_detail_results = {}
    if "CLI" in all_totals and cli_ref.exists():
        all_detail_results["CLI"] = cli_results
    if "APP" in all_totals and app_ref.exists():
        all_detail_results["APP"] = app_results
    if "ARCH" in all_totals and arch_ref.exists():
        all_detail_results["ARCH"] = arch_results
    if "HA-SLB" in all_totals and haslb_ref.exists():
        all_detail_results["HA-SLB"] = haslb_results
    if all_detail_results:
        export_detail_md(all_detail_results, LOGS_DIR / "4way_detail.md")

    elapsed = time.perf_counter() - start_time
    logger.info("总耗时: %.1f 秒 (%.1f 分钟)", elapsed, elapsed / 60)


def _cleanup_qdrant():
    global _hybrid_retriever
    if _hybrid_retriever is None:
        return
    try:
        vr = getattr(_hybrid_retriever, "vr", None)
        if vr is not None:
            storage = getattr(vr, "storage", None)
            if storage is not None:
                client = getattr(storage, "_client", None)
                if client is not None:
                    client.close()
    except Exception:
        pass
    _hybrid_retriever = None


if __name__ == "__main__":
    main()
