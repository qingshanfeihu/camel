"""Quick audit of all current parquet files to understand the new build state."""
import pyarrow.parquet as pq
from pathlib import Path

OUTPUT = Path(__file__).parent.parent.parent / "INAGENT/graphrag_index/output"

files = {
    "entities.parquet": ["id", "title", "type"],
    "text_units.parquet": ["id"],
    "relationships.parquet": ["id"],
    "communities.parquet": ["id"],
    "community_reports.parquet": ["id"],
    "documents.parquet": ["id"],
}
for fname, cols in files.items():
    p = OUTPUT / fname
    if p.exists():
        t = pq.read_table(str(p))
        info = f"rows={len(t)}, cols={t.column_names[:6]}"
        print(f"{fname}: {info}")
    else:
        print(f"{fname}: NOT FOUND")

# Check entity type distribution
ep = pq.read_table(str(OUTPUT / "entities.parquet")).to_pandas()
print("\nType distribution:", ep['type'].value_counts().to_dict())
print("Empty type count:", (ep['type'] == '').sum() + ep['type'].isna().sum())
print("Quoted titles:", ep['title'].str.startswith('"', na=False).sum())
print("Quoted descriptions:", ep['description'].str.startswith('"', na=False).sum())
