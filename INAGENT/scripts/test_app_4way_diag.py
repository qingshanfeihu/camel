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
APP 4 路一致性诊断 — app.pdf 原始内容 × KB文档层 × 向量检索 × GraphRAG

以 auto_convert 产出的 app.json 为权威源，用手册实际原文（page_content + description）
作为查询，检验 KB / 向量 / GraphRAG 各层是否能命中 app.pdf 的 ch.9 HA 和 ch.11 SLB 知识。

用法:
    python -m INAGENT.scripts.test_app_4way_diag
    python -m INAGENT.scripts.test_app_4way_diag --chapter 9
    python -m INAGENT.scripts.test_app_4way_diag --chapter 11
    python -m INAGENT.scripts.test_app_4way_diag --top-k 20
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app_4way")

_INAGENT = Path(__file__).resolve().parent.parent
_APP_JSON = _INAGENT / "backup" / "_rag_backup" / "doc_local_reference" / "app.json"
_KB_PATH = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"
_GRAPHRAG_ENTITIES = _INAGENT / "graphrag_index" / "output" / "entities.parquet"
_GRAPHRAG_RELS = _INAGENT / "graphrag_index" / "output" / "relationships.parquet"

# ──────────────────────────────────────────────────────────────────────────────
# Fixed test corpus: 5 entries from ch.9 HA + 5 from ch.11 SLB
# Each entry holds the raw page_content from app.json as the query source.
# ──────────────────────────────────────────────────────────────────────────────
_FIXED_CASES = [
    # ch.9 HA
    {"block_id": 9,  "chapter": 9,  "product_module": "高可用",
     "section_title": "高可用性（HA）",
     "query_hint": "高可用性 HA 系统故障自动切换 主备"},
    {"block_id": 11, "chapter": 9,  "product_module": "高可用",
     "section_title": "基本概念",
     "query_hint": "HA 基本概念 心跳 主备切换"},
    {"block_id": 13, "chapter": 9,  "product_module": "高可用",
     "section_title": "连接同步",
     "query_hint": "HA 连接同步 状态同步 会话保持"},
    {"block_id": 14, "chapter": 9,  "product_module": "高可用",
     "section_title": "场景1：Active/Standby",
     "query_hint": "Active Standby HA 主备配置示例 故障切换"},
    {"block_id": 15, "chapter": 9,  "product_module": "高可用",
     "section_title": "场景2：Active/Active",
     "query_hint": "Active Active HA 主主配置示例"},
    # ch.11 SLB
    {"block_id": 20, "chapter": 11, "product_module": "SLB",
     "section_title": "服务器负载均衡的功能原理和工作机制",
     "query_hint": "SLB 服务器负载均衡 功能原理 工作机制 反向代理"},
    {"block_id": 21, "chapter": 11, "product_module": "SLB",
     "section_title": "虚拟服务",
     "query_hint": "SLB 虚拟服务 VIP 端口 协议"},
    {"block_id": 22, "chapter": 11, "product_module": "SLB",
     "section_title": "后台服务组",
     "query_hint": "SLB 后台服务组 real server 权重 轮询"},
    {"block_id": 23, "chapter": 11, "product_module": "SLB",
     "section_title": "服务器负载均衡健康检查",
     "query_hint": "SLB 健康检查 TCP ping HTTP 探测"},
    {"block_id": 33, "chapter": 11, "product_module": "SLB",
     "section_title": "HTTP/TCP/FTP/UDP/HTTPS/TCPS/DNS协议的负载均衡配置",
     "query_hint": "SLB TCP HTTP 负载均衡协议配置示例"},
]


@dataclass
class AppFourWayResult:
    block_id: int
    chapter: int
    product_module: str
    section_title: str
    query_text: str          # the raw page_content used as ground truth

    # Source 1: app.json direct lookup
    app_found: bool = False
    app_content_len: int = 0
    app_description: str = ""

    # Source 2: KB doc layer (knowledge_base.json — non-CLI category)
    kb_has_doc: bool = False
    kb_doc_count: int = 0
    kb_doc_categories: List[str] = field(default_factory=list)
    kb_cli_count: int = 0       # CLI entries that accidentally match (informational)

    # Source 3: Vector retrieval
    vec_hit: bool = False
    vec_hit_is_doc: bool = False  # True if the hit is a doc (not CLI) entry
    vec_rank: Optional[int] = None
    vec_top1_category: str = ""
    vec_top1_module: str = ""
    vec_top3_snippets: List[str] = field(default_factory=list)

    # Source 4: GraphRAG entity lookup
    gr_module_found: bool = False
    gr_module_type: str = ""
    gr_module_neighbors: int = 0
    gr_section_found: bool = False   # exact or fuzzy section entity
    gr_section_entity: str = ""

    issues: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Source helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_app_json() -> List[Dict]:
    if not _APP_JSON.exists():
        logger.error("app.json 不存在: %s", _APP_JSON)
        return []
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


def _load_kb() -> List[Dict]:
    if not _KB_PATH.exists():
        return []
    return json.loads(_KB_PATH.read_text(encoding="utf-8"))


def _check_app_json(block_id: int, section_title: str,
                    app_data: List[Dict]) -> Dict[str, Any]:
    result = {"found": False, "content_len": 0, "description": "",
              "page_content": "", "clean_text": ""}
    for entry in app_data:
        m = entry.get("metadata", {})
        if m.get("block_id") == block_id:
            result["found"] = True
            pc = entry.get("page_content", "")
            ct = m.get("clean_text", "")
            desc = m.get("description", "")
            result["content_len"] = len(pc) + len(ct) + len(desc)
            result["description"] = desc
            result["page_content"] = pc
            result["clean_text"] = ct
            break
    return result


def _check_kb_doc(product_module: str, section_title: str,
                  kb: List[Dict]) -> Dict[str, Any]:
    """S2: Check knowledge_base.json for app/doc content (non-CLI)."""
    doc_entries = []
    cli_entries = []
    for item in kb:
        m = item.get("metadata", {})
        rm = m.get("regex_metadata") or {}
        cat = (m.get("document_category") or rm.get("document_category") or "")
        mod = (m.get("product_module") or rm.get("product_module") or "").lower()
        src = m.get("filename") or m.get("source_file") or ""

        mod_match = (product_module.lower() in mod or
                     mod in product_module.lower())
        if not mod_match:
            continue

        if cat and cat != "cli/reference":
            doc_entries.append(cat)
        elif "app" in src.lower() or "doc" in cat.lower():
            doc_entries.append(cat or "doc/unknown")
        else:
            cli_entries.append(cat or "cli/reference")

    return {
        "has_doc": len(doc_entries) > 0,
        "doc_count": len(doc_entries),
        "doc_categories": list(set(doc_entries)),
        "cli_count": len(cli_entries),
    }


def _check_vector(query: str, product_module: str, top_k: int,
                  hybrid_retriever) -> Dict[str, Any]:
    """S3: Vector search with raw content query."""
    result = {"hit": False, "hit_is_doc": False, "rank": None,
              "top1_category": "", "top1_module": "", "top3_snippets": []}
    try:
        res = hybrid_retriever.query(query, top_k=top_k, return_detailed_info=True)
        retrieved = res.get("Retrieved Context", [])

        for i, doc in enumerate(retrieved[:3], 1):
            if not isinstance(doc, dict):
                continue
            m = doc.get("metadata") or {}
            rm = m.get("regex_metadata") or {}
            text = doc.get("text", "") or doc.get("page_content", "")
            snippet = text[:60].replace("\n", " ")
            result["top3_snippets"].append(snippet)

        for rank, doc in enumerate(retrieved, 1):
            if not isinstance(doc, dict):
                continue
            m = doc.get("metadata") or {}
            rm = m.get("regex_metadata") or {}
            cat = (m.get("document_category") or rm.get("document_category") or "")
            mod = (m.get("product_module") or rm.get("product_module") or "").lower()
            src = m.get("filename") or ""
            section = (m.get("section_title") or rm.get("section_title") or "")

            if rank == 1:
                result["top1_category"] = cat
                result["top1_module"] = mod

            # Hit = non-CLI app doc content for this module/section
            is_doc = cat not in ("", "cli/reference") or "app" in src.lower()
            mod_match = (product_module.lower() in mod or
                         mod in product_module.lower())
            if is_doc and mod_match:
                result["hit"] = True
                result["hit_is_doc"] = True
                result["rank"] = rank
                break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _check_graphrag(product_module: str, section_title: str) -> Dict[str, Any]:
    """S4: GraphRAG entity lookup for module + section."""
    result = {"module_found": False, "module_type": "", "module_neighbors": 0,
              "section_found": False, "section_entity": ""}
    try:
        import pandas as pd
        if not _GRAPHRAG_ENTITIES.exists():
            return result
        ent_df = pd.read_parquet(_GRAPHRAG_ENTITIES)

        # Module-level entity lookup
        mod_upper = product_module.upper()
        # Also try English aliases
        aliases = {mod_upper}
        if "高可用" in product_module:
            aliases |= {"HA", "HIGH_AVAILABILITY"}
        if "SLB" in product_module.upper():
            aliases |= {"SLB", "SERVER_LOAD_BALANCE"}
        if "基础网络" in product_module:
            aliases |= {"NETWORK", "基础网络"}

        mod_matches = ent_df[ent_df["title"].str.upper().isin(aliases)]
        if not mod_matches.empty:
            row = mod_matches.iloc[0]
            result["module_found"] = True
            result["module_type"] = str(row.get("type", ""))
            if _GRAPHRAG_RELS.exists():
                rel_df = pd.read_parquet(_GRAPHRAG_RELS)
                found_titles = set(mod_matches["title"].str.upper())
                as_src = rel_df[rel_df["source"].str.upper().isin(found_titles)]
                as_tgt = rel_df[rel_df["target"].str.upper().isin(found_titles)]
                nbrs = set(as_src["target"].tolist()) | set(as_tgt["source"].tolist())
                result["module_neighbors"] = len(nbrs)

        # Section-level entity fuzzy lookup (keywords from section_title)
        keywords = [w for w in section_title.replace("（", " ").replace("）", " ")
                    .replace(":", " ").replace("：", " ").split()
                    if len(w) >= 2 and w not in ("配置", "示例", "概述")]
        for kw in keywords[:4]:
            kw_up = kw.upper()
            sec_matches = ent_df[ent_df["title"].str.upper().str.contains(
                kw_up, regex=False, na=False)]
            if not sec_matches.empty:
                result["section_found"] = True
                result["section_entity"] = sec_matches.iloc[0]["title"]
                break

    except Exception as exc:
        result["error"] = str(exc)
    return result


def _analyze_issues(r: AppFourWayResult) -> List[str]:
    issues = []
    if not r.app_found:
        issues.append("APP_JSON: block_id 在 app.json 中找不到 — 数据源丢失")
    if not r.kb_has_doc:
        issues.append(
            f"KB_DOC: 模块'{r.product_module}'在 KB 中无 app/doc 类内容 "
            f"(仅 {r.kb_cli_count} 条 CLI)"
        )
    if not r.vec_hit:
        issues.append(
            f"VECTOR: 查询'{r.query_text[:30]}...'未命中任何 app/doc 文档 "
            f"(top1 category='{r.vec_top1_category}' mod='{r.vec_top1_module}')"
        )
    elif not r.vec_hit_is_doc:
        issues.append(f"VECTOR: 命中的是 CLI 文档而非 app/doc (rank={r.vec_rank})")
    if not r.gr_module_found:
        issues.append(f"GRAPHRAG: 模块实体'{r.product_module}'不存在")
    return issues


# ──────────────────────────────────────────────────────────────────────────────
# Main runner
# ──────────────────────────────────────────────────────────────────────────────

def run_app_4way(chapter_filter: Optional[int] = None,
                 top_k: int = 20) -> List[AppFourWayResult]:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    app_data = _load_app_json()
    kb = _load_kb()
    logger.info("APP JSON: %d entries | KB: %d entries", len(app_data), len(kb))

    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_retriever, _reranker, _gr = initialize_rag_system()
        has_vec = True
        logger.info("RAG 初始化成功")
    except Exception as exc:
        logger.error("RAG 初始化失败: %s", exc)
        has_vec = False
        hybrid_retriever = None

    cases = _FIXED_CASES
    if chapter_filter:
        cases = [c for c in _FIXED_CASES if c["chapter"] == chapter_filter]
    logger.info("测试用例: %d 条 (chapter=%s)", len(cases), chapter_filter or "全部")

    results: List[AppFourWayResult] = []
    for i, case in enumerate(cases, 1):
        block_id = case["block_id"]
        module = case["product_module"]
        section = case["section_title"]
        query_hint = case["query_hint"]

        logger.info("─── [%d/%d] ch%d block=%d '%s' ───",
                    i, len(cases), case["chapter"], block_id, section[:40])

        # Resolve actual query text from app.json page_content + description
        app_res = _check_app_json(block_id, section, app_data)
        raw_pc = app_res.get("page_content", "").strip()
        raw_desc = app_res.get("description", "").strip()
        # Build query: prefer description (richer), fallback to page_content + hint
        if raw_desc and raw_desc.lower() not in ("no description generated.", ""):
            query_text = raw_desc[:200]
        elif raw_pc:
            query_text = (raw_pc + " " + query_hint)[:200]
        else:
            query_text = query_hint

        r = AppFourWayResult(
            block_id=block_id,
            chapter=case["chapter"],
            product_module=module,
            section_title=section,
            query_text=query_text,
        )

        # S1: app.json
        r.app_found = app_res["found"]
        r.app_content_len = app_res["content_len"]
        r.app_description = app_res["description"][:80]

        # S2: KB doc layer
        kb_res = _check_kb_doc(module, section, kb)
        r.kb_has_doc = kb_res["has_doc"]
        r.kb_doc_count = kb_res["doc_count"]
        r.kb_doc_categories = kb_res["doc_categories"]
        r.kb_cli_count = kb_res["cli_count"]

        # S3: Vector
        if has_vec:
            vec = _check_vector(query_text, module, top_k, hybrid_retriever)
            r.vec_hit = vec["hit"]
            r.vec_hit_is_doc = vec.get("hit_is_doc", False)
            r.vec_rank = vec.get("rank")
            r.vec_top1_category = vec.get("top1_category", "")
            r.vec_top1_module = vec.get("top1_module", "")
            r.vec_top3_snippets = vec.get("top3_snippets", [])

        # S4: GraphRAG
        gr = _check_graphrag(module, section)
        r.gr_module_found = gr["module_found"]
        r.gr_module_type = gr.get("module_type", "")
        r.gr_module_neighbors = gr.get("module_neighbors", 0)
        r.gr_section_found = gr.get("section_found", False)
        r.gr_section_entity = gr.get("section_entity", "")

        r.issues = _analyze_issues(r)

        # Inline status
        parts = [
            "APP:" + ("OK" if r.app_found else "MISS"),
            "KB_DOC:" + ("OK" if r.kb_has_doc else "MISS"),
            ("VEC:HIT_DOC" if r.vec_hit and r.vec_hit_is_doc
             else f"VEC:HIT_CLI@{r.vec_rank}" if r.vec_hit
             else "VEC:MISS"),
            "GR_MOD:" + ("OK" if r.gr_module_found else "MISS"),
        ]
        logger.info("  %s", " | ".join(parts))
        for iss in r.issues:
            logger.warning("  !! %s", iss)

        results.append(r)

    return results


def _print_report(results: List[AppFourWayResult]) -> None:
    print()
    print("=" * 78)
    print("  APP 4 路一致性诊断报告  (app.pdf ch.9 HA + ch.11 SLB)")
    print("=" * 78)

    total = len(results)
    app_ok = sum(1 for r in results if r.app_found)
    kb_ok = sum(1 for r in results if r.kb_has_doc)
    vec_ok = sum(1 for r in results if r.vec_hit)
    vec_doc = sum(1 for r in results if r.vec_hit and r.vec_hit_is_doc)
    gr_ok = sum(1 for r in results if r.gr_module_found)
    gr_sec = sum(1 for r in results if r.gr_section_found)
    all_ok = sum(1 for r in results if not r.issues)

    print(f"\n  样本: {total} 条 (auto_convert 原始 PDF 内容)")
    print(f"  {'─'*60}")
    print(f"  {'数据源':<20} {'命中':>8}  {'命中率':>8}  {'说明'}")
    print(f"  {'─'*60}")
    print(f"  {'① app.json(raw PDF)':<20} {app_ok:>3}/{total:<3}  {app_ok/total:>7.0%}  auto_convert输出")
    kb_note = "含doc类" if kb_ok else "仅CLI,无app/doc"
    print(f"  {'② KB doc层':<20} {kb_ok:>3}/{total:<3}  {kb_ok/total:>7.0%}  {kb_note}")
    vec_note = f"doc命中{vec_doc}条" if vec_doc > 0 else "全为CLI命中"
    print(f"  {'③ Vector(Qdrant)':<20} {vec_ok:>3}/{total:<3}  {vec_ok/total:>7.0%}  {vec_note}")
    gr_note = f"section实体{gr_sec}条" if gr_sec else "仅模块级"
    print(f"  {'④ GraphRAG(实体)':<20} {gr_ok:>3}/{total:<3}  {gr_ok/total:>7.0%}  {gr_note}")
    print(f"  {'─'*60}")
    print(f"  4路全OK: {all_ok}/{total}")

    from collections import Counter
    issue_counter: Counter = Counter()
    for r in results:
        for iss in r.issues:
            tag = iss.split(":")[0]
            issue_counter[tag] += 1
    if issue_counter:
        print(f"\n  问题分布:")
        for tag, cnt in issue_counter.most_common():
            gap_note = ""
            if tag == "KB_DOC":
                gap_note = " ← app.pdf 尚未写入 KB doc层"
            elif tag == "VECTOR":
                gap_note = " ← app.pdf 尚未向量索引"
            print(f"    {tag}: {cnt} 条{gap_note}")

    print(f"\n  ── 逐条详情 ──")
    ch_prev = 0
    for r in results:
        if r.chapter != ch_prev:
            print(f"\n  [ 第{r.chapter}章 {'高可用性 HA' if r.chapter==9 else '服务器负载均衡 SLB'} ]")
            ch_prev = r.chapter
        status = "OK" if not r.issues else "!!"
        print(f"\n  [{status}] block={r.block_id} [{r.product_module}] {r.section_title}")
        print(f"    query: {r.query_text[:80]}")
        print(f"    APP_JSON: {'有' if r.app_found else '无'} len={r.app_content_len}")
        print(f"    KB_DOC:   {'有' if r.kb_has_doc else '无'} "
              f"doc={r.kb_doc_count} cli={r.kb_cli_count}")
        if r.vec_hit:
            vec_str = f"HIT({'doc' if r.vec_hit_is_doc else 'CLI'})@{r.vec_rank}"
        else:
            vec_str = f"MISS top1=[{r.vec_top1_category}|{r.vec_top1_module}]"
        print(f"    VECTOR:   {vec_str}")
        if r.vec_top3_snippets:
            print(f"    VEC top3: {r.vec_top3_snippets}")
        gr_str = (f"模块OK({r.gr_module_type} nbrs={r.gr_module_neighbors})"
                  if r.gr_module_found else "模块MISS")
        if r.gr_section_found:
            gr_str += f" + 节点'{r.gr_section_entity}'"
        print(f"    GRAPHRAG: {gr_str}")
        for iss in r.issues:
            print(f"    !! {iss}")

    print()
    # Gap summary
    has_gaps = (kb_ok < total or vec_doc < total)
    if has_gaps:
        print("  ── 数据缺口诊断 ──")
        if kb_ok == 0:
            print("  [KB_DOC] app.pdf 内容未写入 knowledge_base.json 的 doc 类别。")
            print("           解决: 运行 auto_convert 并将 app 类别写入 KB reference。")
        if vec_doc == 0:
            print("  [VECTOR] Qdrant 中无 app/doc 文档，仅含 CLI 数据。")
            print("           解决: 将 app.json 向量化并 upsert 到 Qdrant 对应集合。")
        if gr_ok == total and gr_sec == 0:
            print("  [GRAPHRAG] 模块级实体存在，但无章节级实体。")
            print("             现有 GraphRAG 仅覆盖 CLI 命令图，非手册章节。")
    else:
        print("  所有 4 路全部命中 — app.pdf 已完整流通!")

    print("=" * 78)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="APP 4路诊断 (app.pdf ch.9 HA + ch.11 SLB × KB × Vector × GraphRAG)")
    ap.add_argument("--chapter", type=int, choices=[9, 11], default=None,
                    help="限定章节 9=HA 11=SLB，默认全部")
    ap.add_argument("--top-k", type=int, default=20)
    return ap.parse_args()


def main() -> None:
    import io
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding=enc, errors="replace")
    args = parse_args()
    results = run_app_4way(chapter_filter=args.chapter, top_k=args.top_k)
    _print_report(results)


if __name__ == "__main__":
    main()
