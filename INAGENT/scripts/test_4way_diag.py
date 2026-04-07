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
4 路一致性诊断 — CLI PDF × Command Tree × 向量检索 × GraphRAG

对随机 N 条命令同时检查 4 个数据源，输出差异报告。
以 cli_keyword_graph.json 为权威源，诊断各层数据是否一致。

用法:
    python -m INAGENT.scripts.test_4way_diag
    python -m INAGENT.scripts.test_4way_diag --n 10 --seed 42
    python -m INAGENT.scripts.test_4way_diag --cmd "enable"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("4way_diag")

_INAGENT = Path(__file__).resolve().parent.parent
_KB_PATH = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"
_GRAPH_PATH = _INAGENT / "knowledge_base" / "cli_keyword_graph.json"
_MINERU_CLI = (
    _INAGENT / "knowledge_base" / "mineru_output"
    / "cli" / "hybrid_auto" / "cli_content_list.json"
)


@dataclass
class FourWayResult:
    command_prefix: str
    node_id: str

    # Source 1: Command Tree (cli_keyword_graph.json)
    tree_found: bool = False
    tree_type: str = ""
    tree_func: str = ""
    tree_help: str = ""
    tree_params: int = 0
    tree_scope: str = ""
    tree_id_collision: bool = False

    # Source 2: KB (knowledge_base.json)
    kb_found: bool = False
    kb_node_id: str = ""
    kb_content_len: int = 0
    kb_has_func: bool = False
    kb_has_params: bool = False
    kb_tree_node_id: str = ""

    # Source 3: Vector retrieval (honest query)
    vec_hit: bool = False
    vec_rank: Optional[int] = None
    vec_candidates: int = 0
    vec_top1_cp: str = ""
    vec_top3_cps: List[str] = field(default_factory=list)

    # Source 4: GraphRAG
    gr_found: bool = False
    gr_entity_type: str = ""
    gr_neighbors: int = 0
    gr_relationships: int = 0
    gr_base_entity: str = ""
    gr_base_found: bool = False

    # Issues found
    issues: List[str] = field(default_factory=list)


def _load_graph_nodes() -> Tuple[Dict[str, List[Dict]], Dict[str, List[Dict]]]:
    """Load cli_keyword_graph.json, return (nodes_by_id, edges_by_source)."""
    graph = json.loads(_GRAPH_PATH.read_text(encoding="utf-8"))
    from collections import defaultdict
    nodes_by_id: Dict[str, List[Dict]] = defaultdict(list)
    for n in graph.get("nodes", []):
        nid = n.get("id", "")
        if nid:
            nodes_by_id[nid].append(n)
    edges_by_source: Dict[str, List[Dict]] = defaultdict(list)
    for e in graph.get("edges", []):
        edges_by_source[e.get("source", "")].append(e)
    return dict(nodes_by_id), dict(edges_by_source)


def _load_kb() -> List[Dict]:
    if not _KB_PATH.exists():
        return []
    return json.loads(_KB_PATH.read_text(encoding="utf-8"))


def _build_candidate_pool(kb: List[Dict], nodes_by_id: Dict) -> List[Dict[str, str]]:
    """KB × CLIGraphStore 交集作为候选池。"""
    candidates = []
    for item in kb:
        meta = item.get("metadata", {})
        nid = str(meta.get("node_id") or "").strip()
        cp = str(meta.get("command_prefix") or "").strip()
        if not nid or not cp:
            continue
        if nid not in nodes_by_id:
            continue
        candidates.append({"command_prefix": cp, "node_id": nid})
    return candidates


def _check_tree(nid: str, nodes_by_id: Dict) -> Dict[str, Any]:
    """Source 1: Command Tree check."""
    result = {"found": False, "type": "", "func": "", "help": "", "params": 0,
              "scope": "", "id_collision": False}
    nodes = nodes_by_id.get(nid, [])
    if not nodes:
        return result
    result["found"] = True
    result["id_collision"] = len(nodes) > 1

    cmd_node = None
    for n in nodes:
        if n.get("type") in ("command", "operation_command"):
            cmd_node = n
            break
    if not cmd_node:
        cmd_node = nodes[0]

    result["type"] = cmd_node.get("type", "")
    result["func"] = cmd_node.get("func", "")
    result["help"] = cmd_node.get("help_string", "")
    result["params"] = len(cmd_node.get("parameters", []))
    result["scope"] = ",".join(cmd_node.get("scope", []))
    return result


def _check_kb(nid: str, cp: str, kb: List[Dict]) -> Dict[str, Any]:
    """Source 2: knowledge_base.json check."""
    result = {"found": False, "node_id": "", "content_len": 0,
              "has_func": False, "has_params": False, "tree_node_id": ""}
    for item in kb:
        meta = item.get("metadata", {})
        if str(meta.get("node_id", "")).strip() == nid:
            result["found"] = True
            result["node_id"] = meta.get("node_id", "")
            pc = item.get("page_content") or item.get("text") or ""
            result["content_len"] = len(pc)
            result["has_func"] = bool(meta.get("func"))
            result["has_params"] = "参数:" in pc
            result["tree_node_id"] = str(meta.get("tree_node_id") or "")
            break
    return result


def _check_vector(cp: str, nid: str, hybrid_retriever, top_k: int = 20) -> Dict[str, Any]:
    """Source 3: honest vector retrieval."""
    result = {"hit": False, "rank": None, "candidates": 0,
              "top1_cp": "", "top3_cps": [], "match_method": ""}
    query = cp.strip() if cp.strip() else nid.replace("_", " ")
    try:
        res = hybrid_retriever.query(query, top_k=top_k, return_detailed_info=True)
        retrieved = res.get("Retrieved Context", [])
        result["candidates"] = len(retrieved)

        for i, doc in enumerate(retrieved[:3]):
            if not isinstance(doc, dict):
                continue
            dm = doc.get("metadata") or {}
            doc_cp = dm.get("command_prefix", "")
            if not doc_cp:
                doc_cp = (dm.get("regex_metadata") or {}).get("command_prefix", "")
            if i == 0:
                result["top1_cp"] = doc_cp
            result["top3_cps"].append(doc_cp)

        for rank, doc in enumerate(retrieved, start=1):
            if not isinstance(doc, dict):
                continue
            dm = doc.get("metadata") or {}
            rm = dm.get("regex_metadata") or {}

            doc_nid = dm.get("node_id", "") or rm.get("node_id", "")
            doc_tid = dm.get("tree_node_id", "") or rm.get("tree_node_id", "")
            doc_cp = dm.get("command_prefix", "") or rm.get("command_prefix", "")

            if doc_nid == nid:
                result["hit"] = True
                result["rank"] = rank
                result["match_method"] = "node_id"
                break
            if doc_tid == nid:
                result["hit"] = True
                result["rank"] = rank
                result["match_method"] = "tree_node_id"
                break
            if doc_cp and doc_cp.lower() == cp.lower():
                result["hit"] = True
                result["rank"] = rank
                result["match_method"] = "command_prefix"
                break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _check_graphrag(nid: str, cp: str) -> Dict[str, Any]:
    """Source 4: GraphRAG entity check (exact + base-command fallback)."""
    result = {"found": False, "entity_type": "", "neighbors": 0,
              "relationships": 0, "base_entity": "", "base_found": False}
    try:
        import pandas as pd
        entities_path = _INAGENT / "graphrag_index" / "output" / "entities.parquet"
        rels_path = _INAGENT / "graphrag_index" / "output" / "relationships.parquet"
        if not entities_path.exists():
            return result
        ent_df = pd.read_parquet(entities_path)
        label_upper = nid.upper()
        cp_upper = cp.upper().replace(" ", "_")

        search_set = {label_upper, cp_upper, nid.upper()}
        matches = ent_df[ent_df["title"].str.upper().isin(search_set)]

        if not matches.empty:
            row = matches.iloc[0]
            result["found"] = True
            result["entity_type"] = str(row.get("type", ""))

        rel_df = None
        if rels_path.exists():
            rel_df = pd.read_parquet(rels_path)
            as_src = rel_df[rel_df["source"].str.upper().isin(search_set)]
            as_tgt = rel_df[rel_df["target"].str.upper().isin(search_set)]
            result["relationships"] = len(as_src) + len(as_tgt)
            neighbor_set = set(as_src["target"].tolist()) | set(as_tgt["source"].tolist())
            result["neighbors"] = len(neighbor_set)

        if not result["found"]:
            candidates = []
            for prefix in ("SHOW_STATISTICS_", "CLEAR_STATISTICS_",
                           "SHOW_", "CLEAR_", "NO_", "DISPLAY_"):
                if label_upper.startswith(prefix):
                    candidates.append(label_upper[len(prefix):])
            for suffix in ("_ON", "_OFF"):
                if label_upper.endswith(suffix):
                    candidates.append(label_upper[:-len(suffix)])
            for prefix in ("SHOW_", "CLEAR_", "NO_"):
                for suffix in ("_ON", "_OFF"):
                    if label_upper.startswith(prefix) and label_upper.endswith(suffix):
                        candidates.append(label_upper[len(prefix):-len(suffix)])
            seen = set()
            for base in candidates:
                if base in seen or not base:
                    continue
                seen.add(base)
                base_matches = ent_df[ent_df["title"].str.upper() == base]
                if not base_matches.empty:
                    result["base_entity"] = base
                    result["base_found"] = True
                    break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _analyze_issues(r: FourWayResult) -> List[str]:
    """Analyze discrepancies across the 4 sources."""
    issues = []

    if r.tree_id_collision:
        issues.append(f"TREE: id={r.node_id} 存在 module+command 碰撞")

    if r.tree_found and not r.kb_found:
        issues.append("KB: Command Tree有此节点但KB中不存在")
    if not r.tree_found and r.kb_found:
        issues.append("TREE: KB有此节点但Command Tree中找不到")

    if r.tree_found and r.kb_found:
        if r.tree_func and not r.kb_has_func:
            issues.append(f"KB: Tree有func={r.tree_func}但KB无func字段")
        if r.tree_params > 0 and not r.kb_has_params:
            issues.append(f"KB: Tree有{r.tree_params}个参数但KB内容无参数段")

    if r.kb_found and r.kb_tree_node_id and r.kb_tree_node_id != r.node_id:
        issues.append(f"KB: tree_node_id={r.kb_tree_node_id} != node_id={r.node_id} (历史污染?)")

    if not r.vec_hit:
        issues.append(f"VEC: 诚实查询'{r.command_prefix}'未命中top-20 (top1={r.vec_top1_cp})")
    elif r.vec_rank and r.vec_rank > 5:
        issues.append(f"VEC: 命中但排名偏低@{r.vec_rank}")

    if not r.gr_found:
        if r.gr_base_found:
            issues.append(f"GRAPHRAG: 实体'{r.node_id}'不存在, 但基命令'{r.gr_base_entity}'存在(部分可达)")
        else:
            issues.append(f"GRAPHRAG: 实体'{r.node_id}'不存在")
    elif r.gr_entity_type and r.gr_entity_type not in ("COMMAND", "CLI_COMMAND", "MODULE"):
        issues.append(f"GRAPHRAG: 实体类型={r.gr_entity_type} (期望COMMAND/MODULE)")
    if r.gr_found and r.gr_relationships == 0:
        issues.append("GRAPHRAG: 实体存在但无任何关系边")

    return issues


def run_4way(
    candidates: List[Dict[str, str]],
    n: int,
    seed: Optional[int],
    top_k: int = 20,
) -> List[FourWayResult]:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    nodes_by_id, edges_by_source = _load_graph_nodes()
    kb = _load_kb()

    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_retriever, _reranker, _gr = initialize_rag_system()
        has_vec = True
    except Exception as exc:
        logger.error("RAG初始化失败: %s", exc)
        has_vec = False
        hybrid_retriever = None

    if len(candidates) <= n:
        sample = candidates
    else:
        rng = random.Random(seed) if seed else random.Random()
        sample = rng.sample(candidates, n)

    results: List[FourWayResult] = []
    for i, item in enumerate(sample, 1):
        cp = item["command_prefix"]
        nid = item["node_id"]
        logger.info("─── [%d/%d] cmd='%s' node='%s' ───", i, len(sample), cp, nid)

        r = FourWayResult(command_prefix=cp, node_id=nid)

        # S1: Command Tree
        tree = _check_tree(nid, nodes_by_id)
        r.tree_found = tree["found"]
        r.tree_type = tree["type"]
        r.tree_func = tree["func"]
        r.tree_help = tree["help"]
        r.tree_params = tree["params"]
        r.tree_scope = tree["scope"]
        r.tree_id_collision = tree["id_collision"]

        # S2: KB
        kb_res = _check_kb(nid, cp, kb)
        r.kb_found = kb_res["found"]
        r.kb_node_id = kb_res["node_id"]
        r.kb_content_len = kb_res["content_len"]
        r.kb_has_func = kb_res["has_func"]
        r.kb_has_params = kb_res["has_params"]
        r.kb_tree_node_id = kb_res["tree_node_id"]

        # S3: Vector
        if has_vec:
            vec = _check_vector(cp, nid, hybrid_retriever, top_k)
            r.vec_hit = vec["hit"]
            r.vec_rank = vec.get("rank")
            r.vec_candidates = vec["candidates"]
            r.vec_top1_cp = vec["top1_cp"]
            r.vec_top3_cps = vec.get("top3_cps", [])

        # S4: GraphRAG
        gr = _check_graphrag(nid, cp)
        r.gr_found = gr["found"]
        r.gr_entity_type = gr["entity_type"]
        r.gr_neighbors = gr["neighbors"]
        r.gr_relationships = gr["relationships"]
        r.gr_base_entity = gr.get("base_entity", "")
        r.gr_base_found = gr.get("base_found", False)

        r.issues = _analyze_issues(r)

        status_parts = []
        status_parts.append("TREE:" + ("OK" if r.tree_found else "MISS"))
        status_parts.append("KB:" + ("OK" if r.kb_found else "MISS"))
        if has_vec:
            if r.vec_hit:
                status_parts.append(f"VEC:HIT@{r.vec_rank}")
            else:
                status_parts.append("VEC:MISS")
        gr_status = "OK" if r.gr_found else ("BASE" if r.gr_base_found else "MISS")
        status_parts.append("GR:" + gr_status)
        logger.info("  %s", " | ".join(status_parts))
        if r.issues:
            for iss in r.issues:
                logger.warning("  !! %s", iss)

        results.append(r)

    return results


def _print_report(results: List[FourWayResult]) -> None:
    print()
    print("=" * 78)
    print("  4 路一致性诊断报告")
    print("=" * 78)

    total = len(results)
    tree_ok = sum(1 for r in results if r.tree_found)
    kb_ok = sum(1 for r in results if r.kb_found)
    vec_ok = sum(1 for r in results if r.vec_hit)
    gr_ok = sum(1 for r in results if r.gr_found)
    gr_base = sum(1 for r in results if not r.gr_found and r.gr_base_found)
    all_ok = sum(1 for r in results if not r.issues)
    collisions = sum(1 for r in results if r.tree_id_collision)

    print(f"\n  样本: {total} 条命令")
    print(f"  ┌─────────────┬─────────┬──────────┐")
    print(f"  │ 数据源       │ 命中    │ 命中率    │")
    print(f"  ├─────────────┼─────────┼──────────┤")
    print(f"  │ Command Tree │ {tree_ok:3d}/{total:<3d} │ {tree_ok/total:.0%}      │")
    print(f"  │ KB           │ {kb_ok:3d}/{total:<3d} │ {kb_ok/total:.0%}      │")
    print(f"  │ Vector       │ {vec_ok:3d}/{total:<3d} │ {vec_ok/total:.0%}      │")
    gr_detail = f"{gr_ok/total:.0%}" + (f"+{gr_base}base" if gr_base else "")
    print(f"  │ GraphRAG     │ {gr_ok:3d}/{total:<3d} │ {gr_detail:<8s} │")
    print(f"  └─────────────┴─────────┴──────────┘")
    print(f"  4路全OK: {all_ok}/{total}  |  ID碰撞: {collisions}/{total}")

    # Issue summary
    from collections import Counter
    issue_counter = Counter()
    for r in results:
        for iss in r.issues:
            tag = iss.split(":")[0]
            issue_counter[tag] += 1

    if issue_counter:
        print(f"\n  问题分布:")
        for tag, cnt in issue_counter.most_common():
            print(f"    {tag}: {cnt} 条")

    # Per-command detail
    print(f"\n  ── 逐条详情 ──")
    for r in results:
        star = " *" if r.issues else ""
        vec_str = f"@{r.vec_rank}" if r.vec_hit else "MISS"
        gr_str = r.gr_entity_type if r.gr_found else ("BASE:" + r.gr_base_entity if r.gr_base_found else "MISS")
        collision_flag = " [ID冲突]" if r.tree_id_collision else ""
        print(f"\n  cmd={r.command_prefix}")
        print(f"    TREE: {r.tree_type} func={r.tree_func or '(无)'} params={r.tree_params}{collision_flag}")
        print(f"    KB:   {'有' if r.kb_found else '无'} len={r.kb_content_len} func={'有' if r.kb_has_func else '无'}")
        print(f"    VEC:  {vec_str} top3={r.vec_top3_cps}")
        print(f"    GR:   {gr_str} rels={r.gr_relationships} neighbors={r.gr_neighbors}")
        if r.issues:
            for iss in r.issues:
                print(f"    !! {iss}")

    # Commands with problems
    problem_cmds = [r for r in results if r.issues]
    if problem_cmds:
        print(f"\n  ── 问题命令汇总 ({len(problem_cmds)}/{total}) ──")
        for r in problem_cmds:
            print(f"    {r.command_prefix}: {'; '.join(r.issues)}")
    else:
        print(f"\n  所有 {total} 条命令 4 路完全一致!")

    print("=" * 78)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="4路一致性诊断（Command Tree × KB × Vector × GraphRAG）")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-rebuild", action="store_true",
                    help="跳过 TB0 重建 knowledge_base.json")
    ap.add_argument("--cmd", type=str, default="",
                    help="指定单条命令前缀测试")
    return ap.parse_args()


def main() -> None:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    args = parse_args()
    seed = args.seed if args.seed != 0 else None

    if not args.no_rebuild:
        from INAGENT.scripts.rebuild_cli_docs import build_cli_chunks
        chunks = build_cli_chunks(_GRAPH_PATH)
        _KB_PATH.write_text(
            json.dumps(chunks, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        logger.info("TB0: KB重建完成 (%d条)", len(chunks))

    nodes_by_id, _ = _load_graph_nodes()
    kb = _load_kb()
    candidates = _build_candidate_pool(kb, nodes_by_id)
    logger.info("候选池: %d条", len(candidates))

    if args.cmd:
        cmd_lower = args.cmd.strip().lower()
        matched = [c for c in candidates if c["command_prefix"].lower() == cmd_lower]
        if not matched:
            logger.error("--cmd %r 在候选池中找不到", args.cmd)
            sys.exit(1)
        candidates = matched
        args.n = len(candidates)

    results = run_4way(candidates, args.n, seed, args.top_k)
    _print_report(results)


if __name__ == "__main__":
    main()
