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


def _build_kb_tree_level_index():
    global _kb_tree_level_index
    if _kb_tree_level_index:
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
        if not tl:
            continue
        bid = m.get("block_id") or m.get("chunk_id") or m.get("node_id") or ""
        if bid:
            _kb_tree_level_index[bid] = tl
        title = m.get("section_title", "").strip()
        src = m.get("source_file", "")
        if title and src:
            _kb_tree_level_index[f"{src}::{title}"] = tl
        if title:
            _kb_tree_level_index.setdefault(f"title::{title}", tl)
    logger.info("KB tree_level index: %d entries", len(_kb_tree_level_index))


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
    if all_farmers:
        fill_count = run_fill_cycle(all_farmers[0], report)

    if fill_count > 0:
        final_kb_size = merge_to_sapling()

    return all_farm_stats, final_kb_size


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
    if kw in text:
        return True
    if strict:
        return False
    if len(kw) >= 3 and any('\u4e00' <= c <= '\u9fff' for c in kw):
        for j in range(len(kw) - 1):
            if kw[j:j+2] in text:
                return True
    return False


def _graphrag_search(query: str, keywords: List[str]) -> Dict[str, Any]:
    _load_graphrag()
    result = {"hit": False, "entity_matches": [], "rel_count": 0}
    if _gr_entities is None:
        return result

    title_col = "title" if "title" in _gr_entities.columns else "name"
    titles = _gr_entities[title_col].str.lower().tolist()
    descs = (_gr_entities["description"].str.lower().tolist()
             if "description" in _gr_entities.columns else [""] * len(titles))

    matched_entities = []
    for kw in keywords:
        kw_lower = kw.lower()
        use_strict = len(kw_lower) <= 3
        for i, t in enumerate(titles):
            if _cjk_overlap(kw_lower, str(t), strict=use_strict) or (
                not use_strict and _cjk_overlap(kw_lower, str(descs[i]))
            ):
                matched_entities.append(str(t))
                break

    if matched_entities:
        result["hit"] = True
        result["entity_matches"] = matched_entities[:5]

    if _gr_rels is not None and matched_entities:
        src_col = "source" if "source" in _gr_rels.columns else "source_id"
        tgt_col = "target" if "target" in _gr_rels.columns else "target_id"
        rel_count = 0
        for ent in matched_entities[:3]:
            mask = (_gr_rels[src_col].str.lower() == ent) | (_gr_rels[tgt_col].str.lower() == ent)
            rel_count += mask.sum()
        result["rel_count"] = int(rel_count)

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
        for h in items[:5]:
            if isinstance(h, dict):
                s = h.get("rrf_score", 0) or h.get("score", 0)
            else:
                s = 0
            result["scores"].append(round(float(s), 4) if s else 0)
        result["hit"] = top_score >= _VEC_HIT_THRESHOLD
        top_module = result["top1_module"].lower()
        q_words = [w.lower() for w in query.split() if len(w) > 1]
        module_match = top_module and any(
            _cjk_overlap(w, top_module) or _cjk_overlap(top_module, w)
            for w in q_words if len(w) >= 2
        )
        title_match = any(
            _cjk_overlap(w, all_text, strict=(len(w) <= 2)) for w in q_words
        )
        result["relevant"] = result["hit"] and (module_match or title_match)
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

def _ct_lookup_fuzzy(section_title: str, ct_data: list) -> Dict[str, Any]:
    import re
    cleaned = re.sub(r"\{[^}]*\}", "", section_title).strip()
    title_lower = cleaned.lower()
    title_words = title_lower.split()
    if not title_words:
        return {"found": False, "content_len": 0, "module": "", "snippet": ""}
    for entry in ct_data:
        m = entry.get("metadata", {})
        prefix = m.get("command_prefix", "").lower()
        if prefix == title_lower:
            pc = entry.get("page_content", "")
            return {"found": True, "content_len": len(pc),
                    "module": m.get("product_module", ""), "snippet": pc[:120]}
    best = None
    best_score = 0
    for entry in ct_data:
        m = entry.get("metadata", {})
        prefix = m.get("command_prefix", "").lower()
        prefix_words = prefix.split()
        if not prefix_words:
            continue
        common = sum(1 for w in title_words if w in prefix_words)
        score = common / max(len(title_words), len(prefix_words))
        if score > best_score and score >= 0.5:
            best = entry
            best_score = score
    if best:
        m = best.get("metadata", {})
        pc = best.get("page_content", "")
        return {"found": True, "content_len": len(pc),
                "module": m.get("product_module", ""), "snippet": pc[:120]}
    return {"found": False, "content_len": 0, "module": "", "snippet": ""}


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

        s1 = {"found": True, "content_len": len(entry.get("page_content", "")),
              "snippet": entry.get("page_content", "")[:120]}
        cmd_prefix = m.get("command_prefix", "")
        if cmd_prefix:
            s2 = _ct_lookup_fuzzy(cmd_prefix, ct_data)
        else:
            s2 = _ct_lookup_fuzzy(section, ct_data)
        s3 = _vector_search(query, top_k=10)
        kws = section.split()[:3] + ([module] if module else [])
        s4 = _graphrag_search(query, kws)

        bid = m.get("block_id") or m.get("chunk_id") or ""
        src = m.get("source_file", "")
        tree_level = _lookup_tree_level(block_id=bid, title=section, source_file=src)
        if tree_level == "?":
            tp = m.get("tree_position", {})
            tree_level = tp.get("tree_level", "?") if isinstance(tp, dict) else "?"

        results.append({
            "cmd": section, "module": module, "tree_level": tree_level,
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

        s1 = {"found": True, "content_len": len(pc),
              "snippet": pc[:150]}

        bid = m.get("block_id") or m.get("chunk_id") or ""
        src = m.get("source_file", "")
        tree_level = _lookup_tree_level(block_id=bid, title=section, source_file=src)
        if tree_level == "?":
            tp = m.get("tree_position", {})
            tree_level = tp.get("tree_level", "?") if isinstance(tp, dict) else "?"
        doc_cat = m.get("document_category", "?")

        s2_tree = {"valid": tree_level != "?", "tree_level": tree_level,
                    "doc_category": doc_cat}

        s3 = _vector_search(query.strip(), top_k=10)
        s4 = _graphrag_search(query, kws)

        results.append({
            "section": section, "module": module, "path": path,
            "tree_level": tree_level, "label": label,
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

        s1 = {"found": True, "content_len": len(pc),
              "snippet": pc[:150]}

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
        elif "s2_tree" in r:
            s2_ok = r["s2_tree"]["valid"]
        else:
            s2_ok = False

        s3_hit = r["s3_vec"]["hit"]
        s3_rel = r["s3_vec"].get("relevant", False)
        s4_hit = r["s4_gr"]["hit"]

        s1 = PASS if s1_ok else FAIL
        s2 = PASS if s2_ok else FAIL
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
    ap.add_argument("--skip-pipeline", action="store_true",
                    help="跳过采购员→农民→农场主（只跑4way诊断）")
    ap.add_argument("--skip-vectors", action="store_true",
                    help="跳过向量重建")
    args = ap.parse_args()

    _init_report_log()

    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    start_time = time.perf_counter()
    logger.info("█" * 60)
    logger.info("综合 4-way 端到端流水线")
    logger.info("█" * 60)

    # Phase 0: 预检 doc_local_reference
    logger.info("\n>>> Phase 0: 预检 doc_local_reference")
    doc_statuses = check_doc_local_refs()

    missing = [stem for stem, exists in doc_statuses.items() if not exists]
    if missing and not args.skip_autoconvert:
        logger.info("需要 auto_convert: %s", missing)
        asyncio.run(run_autoconvert_for_missing(missing))
        doc_statuses = check_doc_local_refs()

    missing_after = [stem for stem, exists in doc_statuses.items() if not exists]
    if missing_after:
        logger.warning("仍缺少 doc_local_reference: %s", missing_after)

    # Phase 1: 恢复快照
    if not args.skip_pipeline:
        logger.info("\n>>> Phase 1: 恢复小树苗快照")
        sapling_size = restore_snapshot()

        # Phase 2: 采购员→农民→农场主
        logger.info("\n>>> Phase 2: 采购员→农民→合并→农场主")
        farm_stats, final_kb_size = run_full_pipeline(doc_statuses)
        print_pipeline_summary(farm_stats, final_kb_size)

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

    elapsed = time.perf_counter() - start_time
    logger.info("总耗时: %.1f 秒 (%.1f 分钟)", elapsed, elapsed / 60)

    if _report_file:
        _report_file.close()
        logger.info("报告已写入: %s", LOGS_DIR / "4way_report.txt")


if __name__ == "__main__":
    main()
