"""
全流程 4-way 一致性诊断 — CLI 10 随机 + APP 5 随机

CLI 4-way:
  S1: cli PDF 原文 (cli_1-82.json MinerU output)
  S2: CommandTree 原文 (commandtree_base.json)
  S3: 向量检索 (Qdrant)
  S4: GraphRAG 检索

APP 4-way:
  S1: app PDF 原文 (app_1-40.json MinerU output)
  S2: CLI/CommandTree 叶子关联度
  S3: 向量检索 (Qdrant)
  S4: GraphRAG 检索

用法:
    python -m INAGENT.scripts.test_4way_full_diag
    python -m INAGENT.scripts.test_4way_full_diag --seed 123
    python -m INAGENT.scripts.test_4way_full_diag --cli-count 5 --app-count 3
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("4way_diag")

_INAGENT = Path(__file__).resolve().parent.parent
_REFERENCE = _INAGENT / "knowledge_base" / "reference"
_DOC_REF = _INAGENT / "knowledge_base" / "doc_local_reference"
_CT_PATH = _REFERENCE / "commandtree_base.json"
_KB_PATH = _REFERENCE / "knowledge_base.json"


def _find_docs(prefix: str) -> Path:
    matches = sorted(_DOC_REF.glob(f"{prefix}_*.json"))
    if not matches:
        raise FileNotFoundError(f"No {prefix}_*.json found in {_DOC_REF}")
    return matches[0]


def _find_all_docs(prefix: str) -> List[Path]:
    """找出 doc_local_reference 下所有匹配 prefix 的 JSON 文件。"""
    matches = sorted(_DOC_REF.glob(f"{prefix}_*.json"))
    if not matches:
        raise FileNotFoundError(f"No {prefix}_*.json found in {_DOC_REF}")
    return matches


def _load_merged_docs(prefix: str) -> list:
    """加载并合并同一 prefix 下所有 doc_local_reference 文件。"""
    paths = _find_all_docs(prefix)
    merged = []
    for p in paths:
        merged.extend(_load_json(p))
    return merged


_GRAPHRAG_ENTITIES = _INAGENT / "graphrag_index" / "output" / "entities.parquet"
_GRAPHRAG_RELS = _INAGENT / "graphrag_index" / "output" / "relationships.parquet"
_QDRANT_DIR = Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"

PASS = "OK"
FAIL = "XX"
WARN = "△"


def _load_json(p: Path) -> list:
    if not p.exists():
        logger.error("文件不存在: %s", p)
        return []
    return json.loads(p.read_text(encoding="utf-8"))


# ── GraphRAG helpers ──────────────────────────────────────────────────────────

_gr_entities = None
_gr_rels = None

def _load_graphrag():
    global _gr_entities, _gr_rels
    if _gr_entities is not None:
        return
    try:
        import pandas as pd
        _gr_entities = pd.read_parquet(_GRAPHRAG_ENTITIES)
        _gr_rels = pd.read_parquet(_GRAPHRAG_RELS)
        logger.info("GraphRAG: %d entities, %d rels", len(_gr_entities), len(_gr_rels))
    except Exception as exc:
        logger.warning("GraphRAG load failed: %s", exc)
        _gr_entities = None
        _gr_rels = None


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
    descs = _gr_entities["description"].str.lower().tolist() if "description" in _gr_entities.columns else [""] * len(titles)
    entity_ids = _gr_entities["id"].tolist() if "id" in _gr_entities.columns else list(range(len(titles)))

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


# ── Vector search helper ─────────────────────────────────────────────────────

_hybrid_retriever = None

def _init_retriever():
    global _hybrid_retriever
    if _hybrid_retriever is not None:
        return
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()
    from INAGENT.workflow_config_generator import initialize_rag_system
    hybrid_retriever, _reranker, _graphrag = initialize_rag_system()
    _hybrid_retriever = hybrid_retriever
    logger.info("HybridRetriever initialized")


_META_PREFIX = "INAGENT_META_JSON:"

def _extract_text_meta(text: str) -> tuple:
    """Extract (clean_text, meta_dict) from text that may start with INAGENT_META_JSON:{...}"""
    import json as _json
    if text.startswith(_META_PREFIX):
        nl = text.find("\n")
        if nl > 0:
            try:
                meta = _json.loads(text[len(_META_PREFIX):nl])
                return text[nl + 1:], meta
            except Exception:
                pass
    return text, {}

# Minimum RRF score to count as a real hit (not just BM25 noise fallback).
# Pure BM25-only top scores: 0.2/(60+1) ≈ 0.0033; 
# real dual-signal hits: ≥0.01.
_VEC_HIT_THRESHOLD = 0.008

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


# ══════════════════════════════════════════════════════════════════════════════
# CLI 4-way
# ══════════════════════════════════════════════════════════════════════════════

def _cli_pdf_lookup(cmd_prefix: str, cli_pdf: list) -> Dict[str, Any]:
    """Match command in PDF output, handling parameterized syntax like {on|off}."""
    cmd_lower = cmd_prefix.lower()
    cmd_words = cmd_lower.split()
    best = None
    best_score = 0
    for entry in cli_pdf:
        pc = entry.get("page_content", "")
        pc_lower = pc.lower()
        if cmd_lower in pc_lower:
            score = 3
        else:
            matched = sum(1 for w in cmd_words if w in pc_lower)
            if matched < len(cmd_words) - 1:
                continue
            if _cmd_matches_parameterized(cmd_words, pc_lower):
                score = 2
            elif matched == len(cmd_words):
                score = 1
            else:
                continue
        if score > best_score or (score == best_score and (best is None or len(pc) > len(best.get("page_content", "")))):
            best = entry
            best_score = score
    if best:
        return {"found": True, "content_len": len(best.get("page_content", "")),
                "snippet": best.get("page_content", "")[:120]}
    return {"found": False, "content_len": 0, "snippet": ""}


def _cmd_matches_parameterized(cmd_words: List[str], pdf_text: str) -> bool:
    """Check if cmd matches a PDF line with {opt1|opt2|...} parameterized syntax."""
    import re
    for line in pdf_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        brace_expanded = re.sub(
            r"\{([^}]+)\}",
            lambda m: "(?:" + "|".join(re.escape(o.strip()) for o in m.group(1).split("|")) + ")",
            line,
        )
        if brace_expanded == line:
            continue
        try:
            pattern = r"\b" + brace_expanded + r"\b"
            if re.search(pattern, " ".join(cmd_words)):
                return True
        except re.error:
            continue
    return False


def _ct_lookup(cmd_prefix: str, ct_data: list) -> Dict[str, Any]:
    cmd_lower = cmd_prefix.lower()
    for entry in ct_data:
        m = entry.get("metadata", {})
        if m.get("command_prefix", "").lower() == cmd_lower:
            pc = entry.get("page_content", "")
            return {"found": True, "content_len": len(pc),
                    "module": m.get("product_module", ""),
                    "snippet": pc[:120]}
    return {"found": False, "content_len": 0, "module": "", "snippet": ""}


def _ct_lookup_fuzzy(section_title: str, ct_data: list) -> Dict[str, Any]:
    """Fuzzy match a CLI PDF section_title against CommandTree command_prefix."""
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
                    "module": m.get("product_module", ""),
                    "snippet": pc[:120]}
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
                "module": m.get("product_module", ""),
                "snippet": pc[:120]}
    for entry in ct_data:
        pc = entry.get("page_content", "").lower()
        if title_lower in pc:
            m = entry.get("metadata", {})
            return {"found": True, "content_len": len(entry.get("page_content", "")),
                    "module": m.get("product_module", ""),
                    "snippet": entry.get("page_content", "")[:120]}
    return {"found": False, "content_len": 0, "module": "", "snippet": ""}


def run_cli_4way(ct_data: list, cli_pdf: list, count: int, seed: int):
    import re
    seen_titles: Set[str] = set()
    cmd_like = []
    for e in cli_pdf:
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
            cmd_like.append(e)
        elif re.match(r'^[a-z]', title) and len(title.split()) <= 6:
            cmd_like.append(e)
    candidates = cmd_like
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
            s2 = _ct_lookup(cmd_prefix, ct_data)
            if not s2["found"]:
                s2 = _ct_lookup_fuzzy(section, ct_data)
        else:
            s2 = _ct_lookup_fuzzy(section, ct_data)
        s3 = _vector_search(query, top_k=10)
        kws = section.split()[:3] + ([module] if module else [])
        s4 = _graphrag_search(query, kws)

        results.append({
            "cmd": section, "module": module,
            "s1_pdf": s1, "s2_ct": s2, "s3_vec": s3, "s4_gr": s4,
        })

    return results


def print_cli_report(results: list):
    print()
    print("=" * 90)
    print("  CLI 4-way 一致性报告 (%d commands)" % len(results))
    print("=" * 90)

    totals = {"s1": 0, "s2": 0, "s3": 0, "s3r": 0, "s4": 0}
    for i, r in enumerate(results):
        s1 = PASS if r["s1_pdf"]["found"] else FAIL
        s2 = PASS if r["s2_ct"]["found"] else FAIL
        s3_hit = r["s3_vec"]["hit"]
        s3_rel = r["s3_vec"].get("relevant", False)
        s3 = PASS if s3_rel else (WARN if s3_hit else FAIL)
        s4 = PASS if r["s4_gr"]["hit"] else FAIL
        if r["s1_pdf"]["found"]: totals["s1"] += 1
        if r["s2_ct"]["found"]: totals["s2"] += 1
        if s3_hit: totals["s3"] += 1
        if s3_rel: totals["s3r"] += 1
        if r["s4_gr"]["hit"]: totals["s4"] += 1

        vec_note = ""
        if s3_hit:
            cat = r["s3_vec"]["top1_cat"]
            mod = r["s3_vec"]["top1_module"]
            score_str = ",".join(str(s) for s in r["s3_vec"]["scores"][:3])
            vec_note = " cat=%s mod=%s scores=[%s]" % (cat or "?", mod or "?", score_str)

        gr_note = ""
        if r["s4_gr"]["hit"]:
            gr_note = " entities=%s rels=%d" % (
                r["s4_gr"]["entity_matches"][:2], r["s4_gr"]["rel_count"])

        print("  %2d. [%s] %-30s mod=%-8s PDF:%s CT:%s Vec:%s%s GR:%s%s" % (
            i + 1, r["cmd"][:28], r["cmd"][:30], r["module"][:8],
            s1, s2, s3, vec_note[:50], s4, gr_note[:50]))

        if not r["s2_ct"]["found"]:
            print("      [!] CommandTree 未找到匹配")
        if s3_hit and not s3_rel:
            snippet = r["s3_vec"]["top3_snippets"][0][:60] if r["s3_vec"]["top3_snippets"] else ""
            print("      [△] 向量命中但关键词未出现在结果中: %r" % snippet)

    n = len(results)
    print()
    print("  ─── 汇总 ───")
    print("  S1 cli.pdf 原文:      %d/%d (%d%%)" % (totals["s1"], n, 100*totals["s1"]//n if n else 0))
    print("  S2 CommandTree:       %d/%d (%d%%)" % (totals["s2"], n, 100*totals["s2"]//n if n else 0))
    print("  S3 向量(命中):        %d/%d (%d%%)" % (totals["s3"], n, 100*totals["s3"]//n if n else 0))
    print("  S3 向量(相关命中):    %d/%d (%d%%)" % (totals["s3r"], n, 100*totals["s3r"]//n if n else 0))
    print("  S4 GraphRAG:          %d/%d (%d%%)" % (totals["s4"], n, 100*totals["s4"]//n if n else 0))
    print("=" * 90)
    return totals


# ══════════════════════════════════════════════════════════════════════════════
# APP 4-way
# ══════════════════════════════════════════════════════════════════════════════

def _app_pdf_lookup(section_title: str, app_data: list) -> Dict[str, Any]:
    for entry in app_data:
        m = entry.get("metadata", {})
        if m.get("section_title", "") == section_title:
            pc = entry.get("page_content", "")
            return {"found": True, "content_len": len(pc),
                    "module": m.get("product_module", ""),
                    "section_path": m.get("section_path", ""),
                    "snippet": pc[:150]}
    title_lower = section_title.lower()
    for entry in app_data:
        m = entry.get("metadata", {})
        if title_lower in m.get("section_title", "").lower():
            pc = entry.get("page_content", "")
            return {"found": True, "content_len": len(pc),
                    "module": m.get("product_module", ""),
                    "section_path": m.get("section_path", ""),
                    "snippet": pc[:150]}
    return {"found": False, "content_len": 0, "module": "", "section_path": "", "snippet": ""}


def _cli_leaf_association(section_title: str, product_module: str,
                         ct_data: list, page_content: str = "") -> Dict[str, Any]:
    kws = set(section_title.lower().replace("（", " ").replace("）", " ").split())
    kws.discard("")
    if product_module:
        kws.add(product_module.lower())
    if page_content and len(kws) <= 1:
        import re
        extra = re.findall(r"[a-zA-Z]{2,}", page_content[:300])
        for term in extra[:5]:
            kws.add(term.lower())

    kws -= {"的", "和", "在", "是", "了", "等", "或", "概述", "注意", "注意：", "说明"}

    matched_cmds = []
    mod_lower = product_module.lower() if product_module else ""
    for entry in ct_data:
        m = entry.get("metadata", {})
        cmd = m.get("command_prefix", "")
        entry_mod = m.get("product_module", "").lower()

        if mod_lower and entry_mod and mod_lower != entry_mod:
            continue

        cmd_lower = cmd.lower()
        cmd_tokens = cmd_lower.split()
        alpha_kws = {kw for kw in kws if kw.isascii() and kw.isalpha()}
        if alpha_kws:
            prefix_hits = sum(1 for t in cmd_tokens[:2] if t in alpha_kws)
            if prefix_hits == 0:
                continue
        else:
            pc = entry.get("page_content", "").lower()
            hits = sum(1 for kw in kws if kw in pc or kw in entry_mod)
            if hits < max(1, len(kws) // 2):
                continue

        matched_cmds.append(cmd)
        if len(matched_cmds) >= 10:
            break

    return {
        "associated": len(matched_cmds) > 0,
        "count": len(matched_cmds),
        "commands": matched_cmds[:5],
    }


_GENERIC_TITLES = frozenset({
    "概述", "简介", "说明", "注意事项", "配置说明", "功能介绍", "使用说明",
    "overview", "introduction", "description", "summary", "about",
    "getting started", "prerequisites", "notes",
})


def _enrich_generic_query(section: str, module: str, path: str) -> tuple:
    if section.strip().lower() not in _GENERIC_TITLES:
        return section + " " + (module or "") + " " + (path or ""), section.split()[:3] + ([module] if module else [])
    parts = [p.strip() for p in path.split(">") if p.strip()] if path else []
    parent = ""
    for p in reversed(parts):
        if p.strip().lower() not in _GENERIC_TITLES:
            parent = p.strip()
            break
    if not parent and module:
        parent = module
    if parent:
        query = f"{parent} {section}"
        kws = parent.split()[:3] + [section]
    else:
        query = section + " " + (path or "")
        kws = section.split()[:3]
    if module:
        kws.append(module)
    return query, kws


def run_app_4way(app_data: list, ct_data: list, count: int, seed: int):
    candidates = []
    for e in app_data:
        if len(e.get("page_content", "")) <= 80:
            continue
        m = e.get("metadata", {})
        title = m.get("section_title", "").strip()
        if not title or title == "⽬录":
            continue
        if title.lower() in _GENERIC_TITLES:
            path = m.get("section_path", "")
            parts = [p.strip() for p in path.split(">") if p.strip()] if path else []
            has_parent = any(p.lower() not in _GENERIC_TITLES for p in parts)
            mod = m.get("product_module", "")
            if not has_parent and not mod:
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
        query, kws = _enrich_generic_query(section, module, path)

        pc = entry.get("page_content", "")
        s1 = _app_pdf_lookup(section, app_data)
        s2 = _cli_leaf_association(section, module, ct_data, page_content=pc)
        s3 = _vector_search(query.strip(), top_k=10)
        s4 = _graphrag_search(query, kws)

        results.append({
            "section": section, "module": module, "path": path,
            "s1_app": s1, "s2_cli_leaf": s2, "s3_vec": s3, "s4_gr": s4,
        })

    return results


def print_app_report(results: list):
    print()
    print("=" * 90)
    print("  APP 4-way 一致性报告 (%d entries)" % len(results))
    print("=" * 90)

    totals = {"s1": 0, "s2": 0, "s3": 0, "s3r": 0, "s4": 0}
    for i, r in enumerate(results):
        s1 = PASS if r["s1_app"]["found"] else FAIL
        s2 = PASS if r["s2_cli_leaf"]["associated"] else FAIL
        s3_hit = r["s3_vec"]["hit"]
        s3_rel = r["s3_vec"].get("relevant", False)
        s3 = PASS if s3_rel else (WARN if s3_hit else FAIL)
        s4 = PASS if r["s4_gr"]["hit"] else FAIL
        if r["s1_app"]["found"]: totals["s1"] += 1
        if r["s2_cli_leaf"]["associated"]: totals["s2"] += 1
        if s3_hit: totals["s3"] += 1
        if s3_rel: totals["s3r"] += 1
        if r["s4_gr"]["hit"]: totals["s4"] += 1

        cli_note = ""
        if r["s2_cli_leaf"]["associated"]:
            cli_note = " cmds=%s" % r["s2_cli_leaf"]["commands"][:3]

        vec_note = ""
        if s3_hit:
            cat = r["s3_vec"]["top1_cat"]
            mod = r["s3_vec"]["top1_module"]
            score_str = ",".join(str(s) for s in r["s3_vec"]["scores"][:3])
            vec_note = " cat=%s mod=%s scores=[%s]" % (cat or "?", mod or "?", score_str)

        gr_note = ""
        if r["s4_gr"]["hit"]:
            gr_note = " entities=%s rels=%d" % (
                r["s4_gr"]["entity_matches"][:2], r["s4_gr"]["rel_count"])

        print("  %d. [%-25s] mod=%-10s" % (i + 1, r["section"][:25].encode("gbk", errors="replace").decode("gbk"), (r["module"] or "?")[:10]))
        print("     APP:%s  CLI叶子:%s(%d)%s  Vec:%s%s  GR:%s%s" % (
            s1, s2, r["s2_cli_leaf"]["count"], cli_note[:40],
            s3, vec_note[:50], s4, gr_note[:50]))
        if s3_hit and not s3_rel:
            snippet = r["s3_vec"]["top3_snippets"][0][:60] if r["s3_vec"]["top3_snippets"] else ""
            print("      [△] 向量命中但关键词未出现在结果中: %r" % snippet)

    n = len(results)
    print()
    print("  ─── 汇总 ───")
    print("  S1 app.pdf 原文:         %d/%d (%d%%)" % (totals["s1"], n, 100*totals["s1"]//n if n else 0))
    print("  S2 CLI叶子关联:          %d/%d (%d%%)" % (totals["s2"], n, 100*totals["s2"]//n if n else 0))
    print("  S3 向量(命中):           %d/%d (%d%%)" % (totals["s3"], n, 100*totals["s3"]//n if n else 0))
    print("  S3 向量(相关命中):       %d/%d (%d%%)" % (totals["s3r"], n, 100*totals["s3r"]//n if n else 0))
    print("  S4 GraphRAG:             %d/%d (%d%%)" % (totals["s4"], n, 100*totals["s4"]//n if n else 0))
    print("=" * 90)
    return totals


# ══════════════════════════════════════════════════════════════════════════════
# ARCH 4-way
# ══════════════════════════════════════════════════════════════════════════════

def _arch_tree_level_check(section: str, module: str, blocks: list) -> Dict[str, Any]:
    """S2: 验证架构文档块的 tree_level 是否为 trunk/root。"""
    result = {"valid": False, "tree_level": "", "doc_category": ""}
    for blk in blocks:
        m = blk.get("metadata", {})
        title = m.get("section_title", "").strip()
        if title == section:
            tp = m.get("tree_position", {})
            tl = tp.get("tree_level", "")
            result["tree_level"] = tl
            result["doc_category"] = m.get("document_category", "")
            result["valid"] = tl in ("trunk", "root")
            return result
    return result


def run_arch_4way(arch_data: list, count: int, seed: int):
    candidates = []
    for e in arch_data:
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
        query, kws = _enrich_generic_query(section, module, path)

        s1 = _app_pdf_lookup(section, arch_data)
        s2 = _arch_tree_level_check(section, module, arch_data)
        s3 = _vector_search(query.strip(), top_k=10)
        s4 = _graphrag_search(query, kws)

        results.append({
            "section": section, "module": module, "path": path,
            "s1_arch": s1, "s2_tree_level": s2, "s3_vec": s3, "s4_gr": s4,
        })
    return results


def print_arch_report(results: list):
    print()
    print("=" * 90)
    print("  ARCH 4-way 一致性报告 (%d entries)" % len(results))
    print("=" * 90)

    totals = {"s1": 0, "s2": 0, "s3": 0, "s3r": 0, "s4": 0}
    for i, r in enumerate(results):
        s1 = PASS if r["s1_arch"]["found"] else FAIL
        s2 = PASS if r["s2_tree_level"]["valid"] else FAIL
        s3_hit = r["s3_vec"]["hit"]
        s3_rel = r["s3_vec"].get("relevant", False)
        s3 = PASS if s3_rel else (WARN if s3_hit else FAIL)
        s4 = PASS if r["s4_gr"]["hit"] else FAIL
        if r["s1_arch"]["found"]: totals["s1"] += 1
        if r["s2_tree_level"]["valid"]: totals["s2"] += 1
        if s3_hit: totals["s3"] += 1
        if s3_rel: totals["s3r"] += 1
        if r["s4_gr"]["hit"]: totals["s4"] += 1

        tl_note = " tree=%s cat=%s" % (
            r["s2_tree_level"]["tree_level"] or "?",
            r["s2_tree_level"]["doc_category"] or "?")

        vec_note = ""
        if s3_hit:
            score_str = ",".join(str(s) for s in r["s3_vec"]["scores"][:3])
            vec_note = " scores=[%s]" % score_str

        gr_note = ""
        if r["s4_gr"]["hit"]:
            gr_note = " entities=%s rels=%d" % (
                r["s4_gr"]["entity_matches"][:2], r["s4_gr"]["rel_count"])

        print("  %d. [%-25s] mod=%-10s" % (
            i + 1,
            r["section"][:25].encode("gbk", errors="replace").decode("gbk"),
            (r["module"] or "?")[:10]))
        print("     ARCH:%s  TreeLevel:%s%s  Vec:%s%s  GR:%s%s" % (
            s1, s2, tl_note[:40], s3, vec_note[:50], s4, gr_note[:50]))

    n = len(results)
    print()
    print("  ─── 汇总 ───")
    print("  S1 ARCH原文:             %d/%d (%d%%)" % (totals["s1"], n, 100*totals["s1"]//n if n else 0))
    print("  S2 树层级(trunk/root):   %d/%d (%d%%)" % (totals["s2"], n, 100*totals["s2"]//n if n else 0))
    print("  S3 向量(命中):           %d/%d (%d%%)" % (totals["s3"], n, 100*totals["s3"]//n if n else 0))
    print("  S3 向量(相关命中):       %d/%d (%d%%)" % (totals["s3r"], n, 100*totals["s3r"]//n if n else 0))
    print("  S4 GraphRAG:             %d/%d (%d%%)" % (totals["s4"], n, 100*totals["s4"]//n if n else 0))
    print("=" * 90)
    return totals


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="4-way full diagnostic")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cli-count", type=int, default=10)
    ap.add_argument("--app-count", type=int, default=5)
    ap.add_argument("--arch-count", type=int, default=3)
    ap.add_argument("--skip-linker-quality", action="store_true",
                    help="跳过 S5 Linker Quality 检测（快速模式）")
    args = ap.parse_args()

    ct_data = _load_json(_CT_PATH)
    cli_pdf = _load_json(_find_docs("cli"))
    app_data = _load_merged_docs("app")
    kb = _load_json(_KB_PATH)

    has_arch = bool(sorted(_DOC_REF.glob("ustack*.json")))
    arch_data = []
    if has_arch:
        arch_data = _load_json(sorted(_DOC_REF.glob("ustack*.json"))[0])

    logger.info("CommandTree: %d, CLI PDF: %d, APP PDF: %d, ARCH: %d, KB: %d",
                len(ct_data), len(cli_pdf), len(app_data), len(arch_data), len(kb))
    app_sources = _find_all_docs("app")
    logger.info("APP sources: %s", [p.name for p in app_sources])

    print("\n>>> CLI 4-way test: %d random commands (seed=%d)" % (args.cli_count, args.seed))
    cli_results = run_cli_4way(ct_data, cli_pdf, args.cli_count, args.seed)
    cli_totals = print_cli_report(cli_results)

    print("\n>>> APP 4-way test: %d random entries (seed=%d)" % (args.app_count, args.seed))
    app_results = run_app_4way(app_data, ct_data, args.app_count, args.seed)
    app_totals = print_app_report(app_results)

    arch_totals = {}
    if arch_data and args.arch_count > 0:
        print("\n>>> ARCH 4-way test: %d random entries (seed=%d)" % (args.arch_count, args.seed))
        arch_results = run_arch_4way(arch_data, args.arch_count, args.seed)
        arch_totals = print_arch_report(arch_results)

    print()
    print("=" * 90)
    print("  综合评估")
    print("=" * 90)
    all_ok = True
    tracks = [("CLI", cli_totals, args.cli_count),
              ("APP", app_totals, args.app_count)]
    if arch_totals:
        tracks.append(("ARCH", arch_totals, args.arch_count))
    for name, tots, n in tracks:
        for src, label in [("s1", "PDF原文"), ("s2", "参照源"),
                           ("s3", "向量(命中)"), ("s3r", "向量(相关)"), ("s4", "GraphRAG")]:
            cnt = tots.get(src, 0)
            pct = 100 * cnt // n if n else 0
            is_gate = src not in ("s3",)
            status = PASS if pct >= 80 else (WARN if pct >= 50 else FAIL)
            if pct < 80 and is_gate:
                all_ok = False
            marker = "" if pct >= 80 else (" ← 需关注" if is_gate else " (参考)")
            print("  %s %s %-12s: %d/%d (%d%%)%s" % (status, name, label, cnt, n, pct, marker))

    if all_ok:
        print("\n  全部关键项 ≥80%，流程验证通过")
    else:
        print("\n  存在 <80% 的检查项，需排查")
    print("=" * 90)

    # S5 Linker Quality
    if not args.skip_linker_quality:
        print("\n>>> S5 Linker Quality: 农民/农场主处理质量评估")
        try:
            from INAGENT.scripts.test_linker_quality import run_linker_quality
            lq_results, lq_totals = run_linker_quality(ref_dir=_REFERENCE)
            a_pass = lq_totals.get("A_pass", 0)
            a_total = lq_totals.get("A_total", 0)
            c_pass = lq_totals.get("C_pass", 0)
            c_total = lq_totals.get("C_total", 0)
            lq_ok = (a_pass >= a_total) and (c_pass >= c_total)
            if not lq_ok:
                all_ok = False
        except Exception as exc:
            logger.warning("S5 Linker Quality 运行失败: %s", exc)


if __name__ == "__main__":
    main()
