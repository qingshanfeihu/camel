#!/usr/bin/env python3
"""知识覆盖门禁检查。

检查关键词在以下层级的命中情况：
1) knowledge_base chunks
2) GraphRAG entities
3) GraphRAG relationships
4) GraphRAG text_units

可选：
5) UnifiedRAG 最终上下文（需要 --with-unified-rag）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def _load_kb_chunks(kb_path: Path) -> List[dict]:
    if not kb_path.exists():
        return []
    data = json.loads(kb_path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("chunks", [])


def _contains_term(text: str, term: str) -> bool:
    return term.lower() in (text or "").lower()


def _count_term_hits_in_chunks(chunks: Iterable[dict], term: str) -> int:
    hits = 0
    for c in chunks:
        text = str(c.get("text") or c.get("page_content") or "")
        if _contains_term(text, term):
            hits += 1
            continue
        meta = c.get("metadata") or {}
        if _contains_term(json.dumps(meta, ensure_ascii=False), term):
            hits += 1
    return hits


def _count_term_hits_in_df(df, term: str) -> int:
    if df is None or len(df) == 0:
        return 0
    mask = df.astype(str).apply(
        lambda col: col.str.contains(re.escape(term), case=False, na=False)
    )
    return int(mask.any(axis=1).sum())


def _load_parquet_or_none(path: Path):
    if not path.exists():
        return None
    import pandas as pd

    return pd.read_parquet(path)


def _check_unified_rag_context(terms: List[str], top_k_final: int = 8) -> Dict[str, int]:
    """可选：检查 UnifiedRAG 返回上下文是否包含关键词。"""
    from INAGENT.web.deps import get_unified_rag

    unified = get_unified_rag()
    out: Dict[str, int] = {}
    for term in terms:
        try:
            ctx, _constraints, _ = unified.retrieve(
                query=term,
                top_k_retrieval=30,
                top_k_final=top_k_final,
                use_graphrag=True,
                decomposition_result={"rag_queries": [{"query": term, "priority": 0}]},
                document_category_filter=None,
                category_whitelist=[],
            )
            out[term] = 1 if _contains_term(ctx or "", term) else 0
        except Exception:
            out[term] = 0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="检查知识覆盖门禁")
    parser.add_argument(
        "--terms",
        type=str,
        required=True,
        help="关键词，逗号/分号分隔，例如: directfwd,faststack",
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=Path("INAGENT/knowledge_base/reference/knowledge_base.json"),
        help="knowledge_base.json 路径",
    )
    parser.add_argument(
        "--graphrag-output",
        type=Path,
        default=Path("INAGENT/graphrag_index/output"),
        help="GraphRAG output 目录",
    )
    parser.add_argument(
        "--with-unified-rag",
        action="store_true",
        help="额外检查 UnifiedRAG 最终上下文命中（需要依赖可用）",
    )
    args = parser.parse_args()

    terms = [t.strip() for t in re.split(r"[;,，]+", args.terms) if t.strip()]
    if not terms:
        print("未提供有效关键词")
        return 2

    chunks = _load_kb_chunks(args.kb)
    entities = _load_parquet_or_none(args.graphrag_output / "entities.parquet")
    relationships = _load_parquet_or_none(args.graphrag_output / "relationships.parquet")
    text_units = _load_parquet_or_none(args.graphrag_output / "text_units.parquet")

    print("=== 知识覆盖检查 ===")
    print(f"关键词: {terms}")
    print(f"KB chunks: {len(chunks)}")
    print(f"GraphRAG output: {args.graphrag_output}")

    all_pass = True
    summary: List[Tuple[str, int, int, int, int, int]] = []
    unified_ctx_hits: Dict[str, int] = {}

    if args.with_unified_rag:
        unified_ctx_hits = _check_unified_rag_context(terms)

    for term in terms:
        chunk_hits = _count_term_hits_in_chunks(chunks, term)
        entity_hits = _count_term_hits_in_df(entities, term)
        rel_hits = _count_term_hits_in_df(relationships, term)
        text_hits = _count_term_hits_in_df(text_units, term)
        ctx_hits = unified_ctx_hits.get(term, -1)  # -1 表示未检查

        # 门禁：chunk/entity/relationship/text_units 四层都必须 > 0
        passed = (
            chunk_hits > 0
            and entity_hits > 0
            and rel_hits > 0
            and text_hits > 0
            and (ctx_hits > 0 if args.with_unified_rag else True)
        )
        if not passed:
            all_pass = False

        summary.append((term, chunk_hits, entity_hits, rel_hits, text_hits, ctx_hits))

    header = "term | chunk | entity | relationship | text_unit | unified_ctx"
    print(header)
    print("-" * len(header))
    for row in summary:
        term, c, e, r, t, u = row
        u_str = str(u) if u >= 0 else "N/A"
        print(f"{term} | {c} | {e} | {r} | {t} | {u_str}")

    if all_pass:
        print("PASS: 知识覆盖门禁通过")
        return 0
    print("FAIL: 知识覆盖门禁未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())

