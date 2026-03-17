"""检查单文档构建的实体/关系/社区报告质量"""
import pandas as pd
from pathlib import Path

out = Path("INAGENT/graphrag_index/output")

# --- Entities ---
ent = pd.read_parquet(out / "entities.parquet")
print(f"=== ENTITIES ({len(ent)}) ===")
print(f"Columns: {list(ent.columns)}\n")
for _, row in ent.iterrows():
    t = row.get("type", "?")
    title = row.get("title", "?")
    desc = str(row.get("description", ""))[:100]
    print(f"  [{t}] {title}")
    print(f"        {desc}")

# --- Relationships ---
rel = pd.read_parquet(out / "relationships.parquet")
print(f"\n=== RELATIONSHIPS ({len(rel)}) ===")
print(f"Columns: {list(rel.columns)}\n")
for _, row in rel.iterrows():
    src = row.get("source", "?")
    tgt = row.get("target", "?")
    desc = str(row.get("description", ""))[:100]
    print(f"  {src} -> {tgt}")
    print(f"        {desc}")

# --- Community Reports ---
cr = pd.read_parquet(out / "community_reports.parquet")
print(f"\n=== COMMUNITY REPORTS ({len(cr)}) ===")
print(f"Columns: {list(cr.columns)}\n")
for _, row in cr.iterrows():
    cid = row.get("community", "?")
    title = row.get("title", "?")
    summary = str(row.get("summary", ""))[:200]
    full = str(row.get("full_content", ""))[:300]
    print(f"  Community {cid}: {title}")
    print(f"    Summary: {summary}")
    print(f"    Content: {full}")
    print()
