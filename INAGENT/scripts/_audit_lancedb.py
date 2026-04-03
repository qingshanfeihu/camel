"""
Audit LanceDB entity table: check for quoted titles and sync with entities.parquet.
"""
from pathlib import Path
import pyarrow.parquet as pq
import lancedb

ROOT = Path(__file__).parent.parent.parent
LANCEDB_DIR = ROOT / "INAGENT/graphrag_index/output/lancedb"
ENTITIES_PARQUET = ROOT / "INAGENT/graphrag_index/output/entities.parquet"

def main():
    # 1. Check entities.parquet
    ep = pq.read_table(str(ENTITIES_PARQUET))
    ep_df = ep.to_pandas()
    print("=== entities.parquet ===")
    print(f"Rows: {len(ep_df)}")
    quoted_parquet = ep_df[ep_df['title'].str.startswith('"', na=False)]
    print(f"Quoted titles in parquet: {len(quoted_parquet)}")
    print(f"Type distribution: {ep_df['type'].value_counts().to_dict()}")

    # 2. Check LanceDB via lancedb library
    print(f"\n=== LanceDB ({LANCEDB_DIR}) ===")
    db = lancedb.connect(str(LANCEDB_DIR))
    tables = db.table_names()
    print(f"Tables: {tables}")

    entity_tbl = db.open_table("default-entity-description")
    ldf = entity_tbl.to_pandas()
    print(f"\nEntity table rows: {len(ldf)}")
    print(f"Columns: {list(ldf.columns)}")

    if 'title' in ldf.columns:
        quoted_ldb = ldf[ldf['title'].str.startswith('"', na=False)]
        print(f"Quoted titles in LanceDB: {len(quoted_ldb)}")
        if len(quoted_ldb) > 0:
            print("Sample quoted:", quoted_ldb['title'].head(10).tolist())
        else:
            print("No quoted titles in LanceDB.")

    if 'text' in ldf.columns:
        print(f"Sample text[0]: {str(ldf['text'].iloc[0])[:300]}")

    # 3. text_unit table
    if "default-text_unit-text" in tables:
        tu_tbl = db.open_table("default-text_unit-text")
        tu_df = tu_tbl.to_pandas()
        print(f"\nText_unit table rows: {len(tu_df)}, Cols: {list(tu_df.columns)}")
        if 'text' in tu_df.columns:
            print(f"Sample text[0]: {str(tu_df['text'].iloc[0])[:300]}")

    # 4. Sync check
    print(f"\n=== Sync check ===")
    parquet_ids = set(ep_df['id'].astype(str))
    ldb_ids = set(ldf['id'].astype(str)) if 'id' in ldf.columns else set()
    print(f"Parquet ids: {len(parquet_ids)}, LanceDB ids: {len(ldb_ids)}")
    if ldb_ids:
        print(f"In parquet only: {len(parquet_ids - ldb_ids)}")
        print(f"In LanceDB only: {len(ldb_ids - parquet_ids)}")
        print(f"Common: {len(parquet_ids & ldb_ids)}")
    print("Done.")

if __name__ == "__main__":
    main()
