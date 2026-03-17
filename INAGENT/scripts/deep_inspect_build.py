"""深度检查 GraphRAG 构建产出，评估 RAG 检索适用性"""
import pandas as pd
from pathlib import Path
import json

out = Path("INAGENT/graphrag_index/output")

# ============================================================
# 1. ENTITIES - 完整 description
# ============================================================
ent = pd.read_parquet(out / "entities.parquet")
print("=" * 80)
print(f"ENTITIES ({len(ent)})")
print("=" * 80)

type_groups = {}
for _, row in ent.iterrows():
    t = row.get("type", "UNKNOWN")
    if t not in type_groups:
        type_groups[t] = []
    type_groups[t].append({
        "title": row.get("title", "?"),
        "description": str(row.get("description", "")),
        "degree": row.get("degree", 0),
        "frequency": row.get("frequency", 0),
    })

for t in sorted(type_groups.keys()):
    items = type_groups[t]
    print(f"\n--- [{t}] ({len(items)} entities) ---")
    for item in items:
        print(f"  {item['title']} (degree={item['degree']}, freq={item['frequency']})")
        print(f"    DESC: {item['description']}")

# ============================================================
# 2. RELATIONSHIPS - 完整 description
# ============================================================
rel = pd.read_parquet(out / "relationships.parquet")
print("\n" + "=" * 80)
print(f"RELATIONSHIPS ({len(rel)})")
print("=" * 80)
for _, row in rel.iterrows():
    src = row.get("source", "?")
    tgt = row.get("target", "?")
    desc = str(row.get("description", ""))
    w = row.get("weight", 0)
    print(f"  {src} --[{desc}]--> {tgt}  (weight={w})")

# ============================================================
# 3. COMMUNITY REPORTS - 完整内容
# ============================================================
cr = pd.read_parquet(out / "community_reports.parquet")
print("\n" + "=" * 80)
print(f"COMMUNITY REPORTS ({len(cr)})")
print("=" * 80)
for _, row in cr.iterrows():
    cid = row.get("community", "?")
    title = row.get("title", "?")
    level = row.get("level", "?")
    rank = row.get("rank", "?")
    full = str(row.get("full_content", ""))
    findings_raw = row.get("findings", None)
    findings_json = row.get("full_content_json", None)
    print(f"\n{'~' * 60}")
    print(f"Community {cid} | Level={level} | Rank={rank}")
    print(f"Title: {title}")
    print(f"Full Content ({len(full)} chars):")
    print(full)
    if findings_json:
        try:
            fj = json.loads(findings_json) if isinstance(findings_json, str) else findings_json
            print(f"\nFindings JSON keys: {list(fj.keys()) if isinstance(fj, dict) else type(fj)}")
        except:
            pass
    print()

# ============================================================
# 4. TEXT UNITS
# ============================================================
tu = pd.read_parquet(out / "text_units.parquet")
print("=" * 80)
print(f"TEXT UNITS ({len(tu)})")
print("=" * 80)
for _, row in tu.iterrows():
    tid = row.get("id", "?")
    text = str(row.get("text", ""))
    ent_ids = row.get("entity_ids", [])
    rel_ids = row.get("relationship_ids", [])
    print(f"  TextUnit {tid[:20]}... | {len(text)} chars | entities={len(ent_ids) if ent_ids else 0} | rels={len(rel_ids) if rel_ids else 0}")
    # Print first 500 chars of text
    print(f"    TEXT (first 500): {text[:500]}")
