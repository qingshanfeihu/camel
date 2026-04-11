"""
Linker Quality Diagnostic — 农民/农场主处理质量评估

评估维度：
  D1 link_rate          — 有 tree_position 的块比例（链接率）
  D2 exclusion_rate     — 被农场主标记为 owner_excluded 的块比例（排除率）
  D3 node_validity_rate — linked_nodes 中实际存在于 CommandTree 的节点比例
  D4 contamination_count — C类文档专用：有 tree_position 且正文含友商特征词的块数（零容忍）
  D5 semantic_match_rate — linked_nodes 关键词出现在 page_content 中的比例（抽样验证）

数据来源：自动扫描 reference/*.json，从块 metadata 推断 doc_class。

用法：
    python -m INAGENT.scripts.test_linker_quality
    python -m INAGENT.scripts.test_linker_quality --ref-dir INAGENT/knowledge_base/reference
    python -m INAGENT.scripts.test_linker_quality --sample 50
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("linker_quality")

_INAGENT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT / "knowledge_base" / "reference"
_CT_PATH = _REFERENCE_DIR / "commandtree_base.json"

_SKIP_FILENAMES = frozenset((
    "knowledge_base.json", "_ingest_report.json", "commandtree_base.json",
))

PASS = "OK"
FAIL = "XX"
WARN = "△"
SKIP = "--"

_DEFAULT_QUALITY_A = {
    "min_link_rate": 0.60,
    "max_exclusion_rate": 0.15,
    "min_node_validity_rate": 0.85,
    "min_semantic_match_rate": 0.40,
}

_DEFAULT_QUALITY_B = {}

_DEFAULT_QUALITY_C = {
    "min_exclusion_rate": 0.50,
    "max_link_rate": 0.30,
    "contamination_check": True,
}

_CATEGORY_TO_DOC_CLASS = {
    "cli/reference": "A",
    "app/reference": "A",
    "architecture/design": "A",
    "spec/func_spec": "A",
    "spec/prd": "A",
    "spec/design": "A",
    "test/test_list": "B",
    "test/test_strategy": "B",
    "test/test_template": "B",
    "review/bug_fix": "B",
    "review/rules": "B",
}


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)



def _infer_doc_class(blocks: List[Dict]) -> str:
    from collections import Counter
    cats = Counter()
    for b in blocks[:100]:
        cat = (b.get("metadata") or {}).get("document_category", "")
        if cat:
            cats[cat] += 1
    if not cats:
        return "B"
    top_cat = cats.most_common(1)[0][0]
    return _CATEGORY_TO_DOC_CLASS.get(top_cat, "B")


def _build_contract(doc_class: str) -> Dict[str, Any]:
    if doc_class == "A":
        return {"doc_class": "A", "quality": dict(_DEFAULT_QUALITY_A)}
    elif doc_class == "C":
        return {"doc_class": "C", "quality": dict(_DEFAULT_QUALITY_C)}
    return {"doc_class": "B", "quality": dict(_DEFAULT_QUALITY_B)}


def _discover_documents(ref_dir: Path) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}

    for jf in sorted(ref_dir.glob("*.json")):
        if jf.name in _SKIP_FILENAMES or jf.name.startswith("_"):
            continue

        blocks = _load_json(jf)
        if not isinstance(blocks, list) or not blocks:
            continue

        doc_class = _infer_doc_class(blocks)

        contract = _build_contract(doc_class)
        contract.setdefault("doc_class", doc_class)
        docs[jf.name] = contract

    return docs


def _build_ct_node_set(ct_data: Optional[List[Dict]]) -> set:
    if not ct_data:
        return set()
    nodes = set()
    for entry in ct_data:
        m = entry.get("metadata", {})
        cp = m.get("command_prefix", "")
        if cp:
            nodes.add(cp.lower().strip())
        pc = entry.get("page_content", "")
        first_line = pc.split("\n")[0].strip()
        if first_line:
            nodes.add(first_line.lower())
    return nodes


# ── 维度计算 ──────────────────────────────────────────────────────────────────

def _d1_link_rate(blocks: List[Dict]) -> Tuple[float, int, int]:
    """D1: 有 tree_position 的块比例。返回 (rate, linked_count, total)。"""
    total = len(blocks)
    if total == 0:
        return 0.0, 0, 0
    linked = sum(
        1 for b in blocks
        if (b.get("metadata") or {}).get("tree_position")
    )
    return linked / total, linked, total


def _d2_exclusion_rate(blocks: List[Dict]) -> Tuple[float, int, int]:
    """D2: owner_excluded 块比例。返回 (rate, excluded_count, total)。"""
    total = len(blocks)
    if total == 0:
        return 0.0, 0, 0
    excluded = sum(
        1 for b in blocks
        if (b.get("metadata") or {}).get("owner_excluded")
    )
    return excluded / total, excluded, total


def _d3_node_validity_rate(blocks: List[Dict], ct_nodes: set) -> Tuple[float, int, int]:
    """D3: linked_nodes 中存在于 CommandTree 的节点占所有 linked_nodes 的比例。"""
    if not ct_nodes:
        return 1.0, 0, 0  # 无法验证，跳过
    total_nodes = 0
    valid_nodes = 0
    for b in blocks:
        tp = (b.get("metadata") or {}).get("tree_position") or {}
        for node in tp.get("linked_nodes") or []:
            total_nodes += 1
            node_lower = node.lower().strip()
            # 精确匹配 or 作为前缀包含
            if node_lower in ct_nodes or any(n.startswith(node_lower) for n in ct_nodes):
                valid_nodes += 1
    if total_nodes == 0:
        return 1.0, 0, 0
    return valid_nodes / total_nodes, valid_nodes, total_nodes


def _d4_contamination(blocks: List[Dict], contamination_keywords: List[str]) -> Tuple[int, List[str]]:
    """
    D4: C类污染检测。
    有 tree_position 且 page_content 含友商特征词的块数。
    返回 (count, sample_snippets)。
    """
    if not contamination_keywords:
        return 0, []
    kws_lower = [kw.lower() for kw in contamination_keywords]
    contaminated = []
    for b in blocks:
        tp = (b.get("metadata") or {}).get("tree_position")
        if not tp:
            continue  # 没有链接，不算污染
        content = (b.get("page_content") or "").lower()
        for kw in kws_lower:
            if kw in content:
                snippet = b.get("page_content", "")[:80].replace("\n", " ")
                contaminated.append(snippet)
                break
    return len(contaminated), contaminated[:3]


def _d5_semantic_match_rate(
    blocks: List[Dict], sample_size: int = 50, seed: int = 42
) -> Tuple[float, int, int]:
    """
    D5: linked_nodes 关键词在 page_content 中的出现比例（抽样）。
    取有 tree_position 且 linked_nodes 非空的块，随机抽样后验证。
    """
    linked_blocks = [
        b for b in blocks
        if (b.get("metadata") or {}).get("tree_position", {}).get("linked_nodes")
    ]
    if not linked_blocks:
        return 0.0, 0, 0

    rng = random.Random(seed)
    sample = rng.sample(linked_blocks, min(sample_size, len(linked_blocks)))

    matched = 0
    for b in sample:
        tp = (b.get("metadata") or {}).get("tree_position", {})
        nodes = tp.get("linked_nodes") or []
        content_lower = (b.get("page_content") or "").lower()
        # 取节点名中的每个单词（token）检查是否出现在正文
        for node in nodes[:3]:
            tokens = re.split(r"[\s_\-]+", node.lower())
            tokens = [t for t in tokens if len(t) >= 3]
            if any(t in content_lower for t in tokens):
                matched += 1
                break

    total = len(sample)
    return matched / total if total else 0.0, matched, total


# ── 单文档评估 ────────────────────────────────────────────────────────────────

def evaluate_document(
    ref_filename: str,
    contract: Dict[str, Any],
    ref_dir: Path,
    ct_nodes: set,
    sample_size: int = 50,
) -> Dict[str, Any]:
    doc_class = contract.get("doc_class", "B")
    quality = contract.get("quality", {})

    if doc_class == "skip":
        return {"input_name": ref_filename, "doc_class": "skip", "status": SKIP, "skipped": True}

    ref_path = ref_dir / ref_filename
    blocks = _load_json(ref_path)
    if not isinstance(blocks, list):
        return {
            "input_name": ref_filename,
            "doc_class": doc_class,
            "status": FAIL,
            "reason": f"{ref_filename} is not a list",
        }

    # 计算各维度
    d1_rate, d1_linked, d1_total = _d1_link_rate(blocks)
    d2_rate, d2_excluded, d2_total = _d2_exclusion_rate(blocks)
    d3_rate, d3_valid, d3_total = _d3_node_validity_rate(blocks, ct_nodes)
    d4_count, d4_samples = _d4_contamination(
        blocks, quality.get("contamination_keywords", [])
        if quality.get("contamination_check") else []
    )
    d5_rate, d5_matched, d5_total = _d5_semantic_match_rate(blocks, sample_size)

    # 判定每个维度是否通过
    checks: List[Tuple[str, bool, str]] = []  # (label, passed, detail)

    if doc_class == "A":
        if "min_link_rate" in quality:
            thr = quality["min_link_rate"]
            passed = d1_rate >= thr
            checks.append(("D1 链接率", passed,
                f"{d1_rate:.0%} (要求≥{thr:.0%}, linked={d1_linked}/{d1_total})"))
        if "max_exclusion_rate" in quality:
            thr = quality["max_exclusion_rate"]
            passed = d2_rate <= thr
            checks.append(("D2 排除率", passed,
                f"{d2_rate:.0%} (要求≤{thr:.0%}, excluded={d2_excluded}/{d2_total})"))
        if d3_total > 0:
            thr = quality.get("min_node_validity_rate", 0.90)
            passed = d3_rate >= thr
            checks.append(("D3 节点合法率", passed,
                f"{d3_rate:.0%} (要求≥{thr:.0%}, valid={d3_valid}/{d3_total})"))
        if d5_total > 0:
            thr = quality.get("min_semantic_match_rate", 0.65)
            passed = d5_rate >= thr
            checks.append(("D5 语义匹配率", passed,
                f"{d5_rate:.0%} (要求≥{thr:.0%}, matched={d5_matched}/{d5_total})"))

    elif doc_class == "C":
        if "min_exclusion_rate" in quality:
            thr = quality["min_exclusion_rate"]
            passed = d2_rate >= thr
            checks.append(("D2 排除率", passed,
                f"{d2_rate:.0%} (要求≥{thr:.0%}, excluded={d2_excluded}/{d2_total})"))
        if "max_link_rate" in quality:
            thr = quality["max_link_rate"]
            passed = d1_rate <= thr
            checks.append(("D1 链接率", passed,
                f"{d1_rate:.0%} (要求≤{thr:.0%}, linked={d1_linked}/{d1_total})"))
        if quality.get("contamination_check"):
            passed = d4_count == 0
            checks.append(("D4 污染检测", passed,
                f"污染块={d4_count} (要求=0)" + (
                    f" 样例: {d4_samples[0]!r}" if d4_samples else ""
                )))
        if d3_total > 0:
            thr = quality.get("min_node_validity_rate", 0.90)
            passed = d3_rate >= thr
            checks.append(("D3 节点合法率", passed,
                f"{d3_rate:.0%} (valid={d3_valid}/{d3_total})"))

    elif doc_class == "B":
        # B 类：只收集指标，不做通过判定
        checks.append(("D1 链接率", None,
            f"{d1_rate:.0%} (linked={d1_linked}/{d1_total})"))
        checks.append(("D2 排除率", None,
            f"{d2_rate:.0%} (excluded={d2_excluded}/{d2_total})"))
        if d3_total > 0:
            checks.append(("D3 节点合法率", None, f"{d3_rate:.0%} (valid={d3_valid}/{d3_total})"))
        if d5_total > 0:
            checks.append(("D5 语义匹配率", None, f"{d5_rate:.0%} (matched={d5_matched}/{d5_total})"))

    # A/C 类：全部通过才算 OK
    if doc_class in ("A", "C"):
        all_passed = all(passed for _, passed, _ in checks if passed is not None)
        status = PASS if all_passed else FAIL
    else:
        status = WARN  # B类用 △ 表示"观测中"

    return {
        "input_name": ref_filename,
        "doc_class": doc_class,
        "ref_file": ref_filename,
        "total_blocks": d1_total,
        "status": status,
        "checks": checks,
        "metrics": {
            "d1_link_rate": round(d1_rate, 3),
            "d2_exclusion_rate": round(d2_rate, 3),
            "d3_node_validity_rate": round(d3_rate, 3),
            "d4_contamination_count": d4_count,
            "d5_semantic_match_rate": round(d5_rate, 3),
        },
    }


# ── 报告打印 ──────────────────────────────────────────────────────────────────

def print_linker_report(results: List[Dict]) -> Dict[str, int]:
    """打印农民/农场主质量报告，返回汇总计数。"""
    WIDTH = 90
    print()
    print("=" * WIDTH)
    print("  Linker Quality 报告 — 农民/农场主处理质量 (%d 个文档)" % len(results))
    print("=" * WIDTH)

    totals = {"A_pass": 0, "A_total": 0, "C_pass": 0, "C_total": 0, "B_total": 0}

    for r in results:
        doc_class = r.get("doc_class", "?")
        status = r.get("status", SKIP)
        name = r.get("input_name", "?")
        ref = r.get("ref_file", "—")

        if r.get("skipped"):
            reason = r.get("reason", "已跳过")
            print("  %s [%s] %s" % (SKIP, doc_class, name))
            print("       %s" % reason)
            continue

        print()
        print("  %s [%s] %s  →  %s  (%d块)" % (
            status, doc_class, name, ref, r.get("total_blocks", 0)))

        for label, passed, detail in r.get("checks", []):
            if passed is None:
                marker = "   "
            elif passed:
                marker = PASS
            else:
                marker = FAIL
            print("       %s %-18s %s" % (marker, label, detail))

        if doc_class == "A":
            totals["A_total"] += 1
            if status == PASS:
                totals["A_pass"] += 1
        elif doc_class == "C":
            totals["C_total"] += 1
            if status == PASS:
                totals["C_pass"] += 1
        elif doc_class == "B":
            totals["B_total"] += 1

    print()
    print("  ─── 汇总 ───")
    if totals["A_total"]:
        pct = 100 * totals["A_pass"] // totals["A_total"]
        marker = PASS if pct == 100 else (WARN if pct >= 50 else FAIL)
        print("  %s A类(产品文档)通过: %d/%d (%d%%)" % (
            marker, totals["A_pass"], totals["A_total"], pct))
    if totals["C_total"]:
        pct = 100 * totals["C_pass"] // totals["C_total"]
        marker = PASS if pct == 100 else (WARN if pct >= 50 else FAIL)
        print("  %s C类(外部文档)通过: %d/%d (%d%%)" % (
            marker, totals["C_pass"], totals["C_total"], pct))
    if totals["B_total"]:
        print("  %s B类(待观测): %d 个" % (WARN, totals["B_total"]))

    print("=" * WIDTH)
    return totals


# ── 入口 ──────────────────────────────────────────────────────────────────────

def run_linker_quality(
    ref_dir: Optional[Path] = None,
    sample_size: int = 50,
) -> Tuple[List[Dict], Dict[str, int]]:
    if ref_dir is None:
        ref_dir = _REFERENCE_DIR

    docs = _discover_documents(ref_dir)
    ct_data = _load_json(_CT_PATH)
    ct_nodes = _build_ct_node_set(ct_data)
    logger.info("CommandTree nodes loaded: %d, documents discovered: %d", len(ct_nodes), len(docs))

    results = []
    for ref_filename, contract in docs.items():
        result = evaluate_document(ref_filename, contract, ref_dir, ct_nodes, sample_size)
        results.append(result)

    return results, print_linker_report(results)


def main():
    ap = argparse.ArgumentParser(description="Linker Quality Diagnostic")
    ap.add_argument("--ref-dir", type=Path, default=_REFERENCE_DIR,
                    help="reference JSON 目录")
    ap.add_argument("--sample", type=int, default=50,
                    help="D5 语义匹配抽样数量")
    ap.add_argument("--doc", type=str, default=None,
                    help="只评估指定文档名（可部分匹配）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    docs = _discover_documents(args.ref_dir)
    ct_data = _load_json(_CT_PATH)
    ct_nodes = _build_ct_node_set(ct_data)
    logger.info("CommandTree nodes: %d, documents: %d", len(ct_nodes), len(docs))

    results = []
    for ref_filename, contract in docs.items():
        if args.doc and args.doc.lower() not in ref_filename.lower():
            continue
        result = evaluate_document(ref_filename, contract, args.ref_dir, ct_nodes, args.sample)
        results.append(result)

    totals = print_linker_report(results)

    if totals.get("A_total", 0) > 0 and totals["A_pass"] < totals["A_total"]:
        sys.exit(1)
    if totals.get("C_total", 0) > 0 and totals["C_pass"] < totals["C_total"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
