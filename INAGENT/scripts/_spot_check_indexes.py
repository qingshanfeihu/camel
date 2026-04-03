#!/usr/bin/env python3
"""
抽检向量库和 GraphRAG 索引数据质量
- 覆盖不同权限级别、语法类型（必选/可选/枚举）、no/show/clear 命令
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

KB = Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json"
GRAPHRAG_OUT = Path(__file__).parent.parent / "graphrag_index" / "output"
QDRANT_PATH = Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"

# ── 1. 从 KB 挑选代表性命令 ─────────────────────────────────────────────────

def pick_commands():
    kb = json.load(open(KB, encoding="utf-8"))
    chunks = kb if isinstance(kb, list) else kb.get("chunks", [])

    results = {"enum": [], "mixed": [], "neg": [], "cfg": []}
    for c in chunks:
        text = c.get("text", "") or c.get("page_content", "")
        meta = c.get("metadata", {})
        mod  = meta.get("product_module", "")

        cmd_m = re.search(r"\[命令\]\s*(.+)", text)
        syn_m = re.search(r"语法:\s*(.+)", text)
        priv_m = re.search(r"权限[级别]*[：:]\s*(.+)", text)
        mode_m = re.search(r"模式[：:]\s*(.+)", text)
        if not cmd_m:
            continue
        cmd  = cmd_m.group(1).strip()
        syn  = syn_m.group(1).strip() if syn_m else ""
        priv = priv_m.group(1).strip() if priv_m else ""
        mode = mode_m.group(1).strip() if mode_m else ""

        entry = dict(cmd=cmd, mod=mod, syn=syn, priv=priv, mode=mode, text=text)
        has_req  = "<" in syn and ">" in syn
        has_opt  = "[" in syn and "]" in syn
        has_enum = "{" in syn
        is_neg   = mod.startswith(("no_", "show_", "clear_"))

        if has_enum and len(results["enum"]) < 3:
            results["enum"].append(entry)
        if has_req and has_opt and not is_neg and len(results["mixed"]) < 3:
            results["mixed"].append(entry)
        if is_neg and len(results["neg"]) < 5:
            results["neg"].append(entry)
        if has_req and not is_neg and not has_enum and len(results["cfg"]) < 3:
            results["cfg"].append(entry)

        if all(len(v) >= 3 for v in results.values()):
            break

    return results


# ── 2. Qdrant 向量检索 ────────────────────────────────────────────────────────

def check_qdrant(queries: list[str]) -> dict:
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from INAGENT.utils import env_utils
    env_utils.load_inagent_env()

    # 用简单的 scroll+关键词过滤验证（不走 embedding，避免依赖 LLM gateway）
    client = QdrantClient(path=str(QDRANT_PATH))
    hits = {}
    pts, _ = client.scroll("workflow_rag", limit=3570, with_payload=True, with_vectors=False)
    # 构建简单倒排
    for q in queries:
        keyword = q.split()[0].lower()  # 用命令的第一个词搜索
        matched = []
        for pt in pts:
            payload = pt.payload or {}
            text = payload.get("text", "") or str(payload.get("extra_info", ""))
            if keyword in text.lower():
                matched.append({
                    "id": str(pt.id),
                    "category": (payload.get("metadata") or {}).get("regex_metadata", {}).get("document_category"),
                    "module":   (payload.get("metadata") or {}).get("regex_metadata", {}).get("product_module"),
                    "preview":  text[:120].replace("\n", " "),
                })
        hits[q] = matched[:2]
    client.close()
    return hits


# ── 3. GraphRAG 实体检索 ─────────────────────────────────────────────────────

def check_graphrag(keywords: list[str]) -> dict:
    if not GRAPHRAG_OUT.exists():
        return {"error": "output dir not found"}
    entities_path = GRAPHRAG_OUT / "entities.parquet"
    if not entities_path.exists():
        return {"error": "entities.parquet not found (build still running?)"}
    import pandas as pd
    df = pd.read_parquet(entities_path)
    results = {}
    for kw in keywords:
        mask = df["name"].str.lower().str.contains(kw.lower(), na=False) | \
               df["description"].str.lower().str.contains(kw.lower(), na=False)
        rows = df[mask][["name", "type", "description"]].head(3)
        results[kw] = rows.to_dict("records")
    return results


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("SPOT CHECK: 向量库 + GraphRAG 索引数据质量验证")
    print("=" * 70)

    cmds = pick_commands()

    # 打印挑选的命令
    labels = [
        ("枚举参数 {x|y|…}", "enum"),
        ("必选+可选混合 <> []", "mixed"),
        ("no / show / clear", "neg"),
        ("配置命令 <必选参数>", "cfg"),
    ]
    all_entries = []
    for label, key in labels:
        print(f"\n【{label}】")
        for e in cmds[key]:
            print(f"  ✦ {e['cmd']}")
            print(f"    mod={e['mod']}  priv={e['priv'] or '—'}  mode={e['mode'] or '—'}")
            print(f"    语法: {e['syn'][:90]}")
            all_entries.append(e)

    # ── Qdrant ──
    print("\n" + "─" * 70)
    print("【Qdrant 向量库 payload 抽检】")
    sample_queries = [e["cmd"].split()[0] for e in all_entries[:8]]
    qdrant_hits = check_qdrant(sample_queries)
    for q, hits in qdrant_hits.items():
        marker = "✅" if hits else "❌"
        print(f"  {marker} keyword={q!r}  命中={len(hits)}")
        for h in hits:
            print(f"      category={h['category']}  module={h['module']}")
            print(f"      {h['preview'][:80]}")
    # 污染检查
    from qdrant_client import QdrantClient
    client = QdrantClient(path=str(QDRANT_PATH))
    pts, _ = client.scroll("workflow_rag", limit=3570, with_payload=True, with_vectors=False)
    cats = {}
    for pt in pts:
        cat = (pt.payload.get("metadata") or {}).get("regex_metadata", {}).get("document_category", "?")
        cats[cat] = cats.get(cat, 0) + 1
    client.close()
    print(f"\n  全库分类分布: {cats}")
    if list(cats.keys()) == ["cli/reference"] or set(cats.keys()) == {"cli/reference"}:
        print("  ✅ Qdrant 数据纯净！仅含 cli/reference")
    else:
        print("  ⚠️  Qdrant 含非 cli/reference 数据！")

    # ── GraphRAG ──
    print("\n" + "─" * 70)
    print("【GraphRAG 实体抽检】")
    kws = [e["cmd"].split()[0] for e in all_entries[:6]]
    grag = check_graphrag(kws)
    if "error" in grag:
        print(f"  ⏳ {grag['error']} （构建可能仍在进行中）")
    else:
        # 检查旧实体类型污染
        import pandas as pd
        df = pd.read_parquet(GRAPHRAG_OUT / "entities.parquet")
        bad_types = {"STEP_TYPE", "TEST_CASE", "BUSINESS_STATE", "STATE_TRANSITION",
                     "SCENARIO", "REQUIREMENT", "DESIGN_KNOWLEDGE", "CONFIG_EXAMPLE",
                     "TEST_STANDARD", "ERROR_CODE_TRIGGER"}
        all_types = set(df["type"].str.strip('"').str.upper().unique())
        dirty = bad_types & all_types
        clean_types = all_types - bad_types
        if dirty:
            print(f"  ⚠️  发现旧类型污染: {dirty}")
        else:
            print(f"  ✅ 实体类型干净！类型集合: {sorted(clean_types)}")

        print(f"  总实体数: {len(df)}")
        for kw, rows in grag.items():
            marker = "✅" if rows else "❌"
            print(f"  {marker} keyword={kw!r}  命中实体数={len(rows)}")
            for r in rows:
                print(f"      [{r['type']}] {r['name']}: {str(r['description'])[:60]}")


if __name__ == "__main__":
    main()
