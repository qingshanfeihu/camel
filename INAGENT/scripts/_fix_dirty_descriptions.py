"""
Fix malformed description artifacts in parquet outputs.

Affected pattern (LLM wrapping output in quotes):
  - '"description text">'  -> 'description text'
  - '"description text"'   -> 'description text'

Descriptive prose like '"TYPE" is a parameter...' is left unchanged
(does not end with '">' or match fully-quoted pattern).

Steps:
  1. Fix entities.parquet 'description' column  (+ backup)
  2. Fix relationships.parquet 'description'/'short_description' if they exist

Note:
    This script intentionally does NOT rewrite LanceDB. If parquet and LanceDB need
    to be resynchronized, use _rebuild_lancedb.py after verifying table health.
"""
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = ROOT / "INAGENT/graphrag_index/output"
LANCEDB_DIR = OUTPUT_DIR / "lancedb"


# ---------------------------------------------------------------------------
# Core helper (mirrors _strip_lm_quotes in graph_extractor.py)
# ---------------------------------------------------------------------------

def _strip_lm_quotes(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('">') and len(s) > 3:
        return s[1:-2].strip()
    if s.startswith('"') and s.endswith('"') and len(s) > 2:
        return s[1:-1].strip()
    return s


def _fix_parquet_column(parquet_path: Path, col: str) -> int:
    """Fix a string column in a parquet file in-place.  Returns number of rows changed."""
    tbl = pq.read_table(str(parquet_path))
    df = tbl.to_pandas()
    if col not in df.columns:
        print(f"  Column '{col}' not found in {parquet_path.name}, skipping.")
        return 0

    orig = df[col].copy()
    df[col] = df[col].apply(lambda v: _strip_lm_quotes(str(v)) if isinstance(v, str) else v)
    changed = (df[col] != orig).sum()

    if changed > 0:
        bak = parquet_path.with_suffix(".desc_bak.parquet")
        shutil.copy2(str(parquet_path), str(bak))
        print(f"  Backup: {bak.name}")
        pq.write_table(pa.Table.from_pandas(df, schema=tbl.schema), str(parquet_path))
        print(f"  Fixed {changed} rows in {parquet_path.name}[{col}]")
    else:
        print(f"  No changes needed in {parquet_path.name}[{col}]")
    return changed


# ---------------------------------------------------------------------------
# 1. Fix entities.parquet
# ---------------------------------------------------------------------------
print("=== 1. entities.parquet ===")
entities_path = OUTPUT_DIR / "entities.parquet"
n_entities = _fix_parquet_column(entities_path, "description")


# ---------------------------------------------------------------------------
# 2. Fix relationships.parquet (if column exists)
# ---------------------------------------------------------------------------
print("\n=== 2. relationships.parquet ===")
rel_path = OUTPUT_DIR / "relationships.parquet"
if rel_path.exists():
    _fix_parquet_column(rel_path, "description")
    _fix_parquet_column(rel_path, "short_description")
else:
    print("  Not found, skipping.")


print("\n=== 3. LanceDB ===")
print("  Skipped by design. Run _rebuild_lancedb.py if you need to resync embeddings.")

print("\nDone.")
