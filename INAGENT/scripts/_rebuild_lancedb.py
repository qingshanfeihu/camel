"""
Rebuild LanceDB embedding tables from the current parquet outputs.

Why: the authoritative GraphRAG outputs live in entities.parquet and
text_units.parquet. If LanceDB drifts from those files, retrieval quality drops.

Strategy:
    1. Validate whether each LanceDB table already matches the current parquet.
    2. Rebuild only the unhealthy tables.
    3. Truncate embed inputs with the GraphRAG tokenizer so they stay within the
         embedding gateway limit.

This avoids re-running the slow extract_graph LLM step.
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import lancedb

ROOT = Path(__file__).parent.parent.parent
WORKSPACE = ROOT / "INAGENT/graphrag_index"
OUTPUT = WORKSPACE / "output"
LANCEDB_DIR = OUTPUT / "lancedb"

ENTITIES_PARQUET = OUTPUT / "entities.parquet"
TEXT_UNITS_PARQUET = OUTPUT / "text_units.parquet"

BATCH_SIZE = 32   # embed 32 texts per API call
DEFAULT_EMBED_INPUT_TOKEN_LIMIT = int(
    os.environ.get("INAGENT_EMBEDDING_INPUT_TOKEN_LIMIT", "8000")
)


def load_graphrag_config():
    """Load graphrag GraphRagConfig from workspace settings.yaml."""
    import sys
    sys.path.insert(0, str(ROOT / "INAGENT/graphrag"))
    from graphrag.config.load_config import load_config
    return load_config(WORKSPACE)


def get_embedding_fn(config):
    """Return an async batch embedding function using graphrag ModelManager."""
    from graphrag.language_model.manager import ModelManager
    embedding_settings = config.get_language_model_config(
        config.local_search.embedding_model_id
    )
    model = ModelManager().get_or_create_embedding_model(
        name="rebuild_embedding",
        model_type=embedding_settings.type,
        config=embedding_settings,
    )
    return model


def get_embedding_tokenizer(config):
    """Return the tokenizer aligned with the configured embedding model."""
    from graphrag.tokenizer.get_tokenizer import get_tokenizer

    embedding_settings = config.get_language_model_config(
        config.local_search.embedding_model_id
    )
    return get_tokenizer(model_config=embedding_settings)


async def embed_texts(model, texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, returns list of float vectors."""
    results = await model.aembed_batch(texts)
    return [list(r) for r in results]


def _table_names(db) -> list[str]:
    if hasattr(db, "list_tables"):
        return list(db.list_tables())
    return list(db.table_names())


def _truncate_for_embedding(text: str, tokenizer, max_tokens: int) -> tuple[str, bool]:
    """Trim text to the embedding model limit using the model tokenizer."""
    tokens = tokenizer.encode(text)
    if len(tokens) <= max_tokens:
        return text, False
    trimmed = tokenizer.decode(tokens[:max_tokens])
    return trimmed, True


def _table_matches_parquet(
    db,
    table_name: str,
    parquet_df: pd.DataFrame,
) -> tuple[bool, str]:
    """Check whether a LanceDB table is aligned with the corresponding parquet."""
    tables = _table_names(db)
    if table_name not in tables:
        return False, "missing table"

    table_df = db.open_table(table_name).to_pandas()
    if len(table_df) != len(parquet_df):
        return False, f"row mismatch: lancedb={len(table_df)} parquet={len(parquet_df)}"

    table_ids = set(table_df["id"].astype(str))
    parquet_ids = set(parquet_df["id"].astype(str))
    if table_ids != parquet_ids:
        return False, (
            f"id mismatch: overlap={len(table_ids & parquet_ids)} "
            f"lancedb_only={len(table_ids - parquet_ids)} parquet_only={len(parquet_ids - table_ids)}"
        )

    empty_text = int((table_df["text"].astype(str).str.len() == 0).sum())
    if empty_text:
        return False, f"{empty_text} empty text rows"

    return True, "aligned"


async def rebuild_entity_table(
    config,
    ep: pd.DataFrame,
    tokenizer,
    max_tokens: int,
    force: bool = False,
):
    """Rebuild default-entity-description table."""
    db = lancedb.connect(str(LANCEDB_DIR))
    healthy, reason = _table_matches_parquet(db, "default-entity-description", ep)
    if healthy and not force:
        print("Entity table already aligned, skipping rebuild.")
        return
    print(f"Entity table rebuild required: {reason}")

    model = get_embedding_fn(config)
    
    # Build text representation for each entity (matches GraphRAG convention)
    texts = []
    truncated_count = 0
    for _, row in ep.iterrows():
        title = str(row.get('title', '') or '')
        desc = str(row.get('description', '') or '')
        text = f"{title}: {desc}" if desc else title
        text, was_truncated = _truncate_for_embedding(text, tokenizer, max_tokens)
        texts.append(text)
        truncated_count += int(was_truncated)
    if truncated_count:
        print(f"  Truncated {truncated_count} entity texts to <= {max_tokens} tokens")
    
    print(f"Embedding {len(texts)} entities in batches of {BATCH_SIZE}...")
    t0 = time.time()
    all_vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        vecs = await embed_texts(model, batch)
        all_vectors.extend(vecs)
        if (i // BATCH_SIZE + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + len(batch)) / elapsed
            remain = (len(texts) - i - len(batch)) / rate if rate > 0 else 0
            print(f"  {i+len(batch)}/{len(texts)} done, {elapsed:.0f}s elapsed, ~{remain:.0f}s remaining")
    
    print(f"  Embedding done in {time.time()-t0:.1f}s")
    
    # Build LanceDB records
    records = []
    for i, (_, row) in enumerate(ep.iterrows()):
        uid = str(row['id'])
        text = texts[i]
        attrs = json.dumps({"title": text}, ensure_ascii=False)
        records.append({
            "id": uid,
            "text": text,
            "vector": np.array(all_vectors[i], dtype=np.float32),
            "attributes": attrs,
        })
    
    existing = _table_names(db)
    if "default-entity-description" in existing:
        db.drop_table("default-entity-description")
    db.create_table("default-entity-description", data=pd.DataFrame(records), mode="create")
    print(f"  LanceDB default-entity-description rebuilt: {len(records)} rows")


async def rebuild_text_unit_table(
    config,
    tu: pd.DataFrame,
    tokenizer,
    max_tokens: int,
    force: bool = False,
):
    """Rebuild default-text_unit-text table."""
    db = lancedb.connect(str(LANCEDB_DIR))
    healthy, reason = _table_matches_parquet(db, "default-text_unit-text", tu)
    if healthy and not force:
        print("Text-unit table already aligned, skipping rebuild.")
        return
    print(f"Text-unit table rebuild required: {reason}")

    model = get_embedding_fn(config)
    
    texts = []
    truncated_count = 0
    for _, row in tu.iterrows():
        text = str(row.get('text', '') or '')
        text, was_truncated = _truncate_for_embedding(text, tokenizer, max_tokens)
        texts.append(text)
        truncated_count += int(was_truncated)
    if truncated_count:
        print(f"  Truncated {truncated_count} text_units to <= {max_tokens} tokens")

    print(f"Embedding {len(texts)} text_units in batches of {BATCH_SIZE}...")
    t0 = time.time()
    all_vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        vecs = await embed_texts(model, batch)
        all_vectors.extend(vecs)
        if (i // BATCH_SIZE + 1) % 10 == 0:
            elapsed = time.time() - t0
            rate = (i + len(batch)) / elapsed
            remain = (len(texts) - i - len(batch)) / rate if rate > 0 else 0
            print(f"  {i+len(batch)}/{len(texts)} done, {elapsed:.0f}s elapsed, ~{remain:.0f}s remaining")
    print(f"  Text_unit embedding done in {time.time()-t0:.1f}s")
    
    records = []
    for i, (_, row) in enumerate(tu.iterrows()):
        attrs = json.dumps({"title": str(row.get('id', ''))}, ensure_ascii=False)
        records.append({
            "id": str(row['id']),
            "text": texts[i],
            "vector": np.array(all_vectors[i], dtype=np.float32),
            "attributes": attrs,
        })
    
    if "default-text_unit-text" in _table_names(db):
        db.drop_table("default-text_unit-text")
    db.create_table("default-text_unit-text", data=pd.DataFrame(records), mode="create")
    print(f"  LanceDB default-text_unit-text rebuilt: {len(records)} rows")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild GraphRAG LanceDB tables from parquet")
    parser.add_argument(
        "--force-entity",
        action="store_true",
        help="Rebuild entity table even if it already matches parquet",
    )
    parser.add_argument(
        "--force-text-unit",
        action="store_true",
        help="Rebuild text-unit table even if it already matches parquet",
    )
    parser.add_argument(
        "--embed-input-token-limit",
        type=int,
        default=DEFAULT_EMBED_INPUT_TOKEN_LIMIT,
        help="Maximum tokenizer tokens per embedding input. Default comes from INAGENT_EMBEDDING_INPUT_TOKEN_LIMIT or 8000.",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    print("Loading graphrag config...")
    config = load_graphrag_config()
    tokenizer = get_embedding_tokenizer(config)
    
    print("Loading parquet files...")
    ep = pq.read_table(str(ENTITIES_PARQUET)).to_pandas()
    tu = pq.read_table(str(TEXT_UNITS_PARQUET)).to_pandas()
    print(f"  Entities: {len(ep)}, Text_units: {len(tu)}")
    
    await rebuild_entity_table(
        config,
        ep,
        tokenizer,
        args.embed_input_token_limit,
        force=args.force_entity,
    )
    await rebuild_text_unit_table(
        config,
        tu,
        tokenizer,
        args.embed_input_token_limit,
        force=args.force_text_unit,
    )
    
    print("\nVerification:")
    db = lancedb.connect(str(LANCEDB_DIR))
    for t in ["default-entity-description", "default-text_unit-text"]:
        df = db.open_table(t).to_pandas()
        print(f"  {t}: {len(df)} rows, vector dim={len(df['vector'].iloc[0])}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
