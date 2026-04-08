"""
从 cli_keyword_graph.json 直接生成 GraphRAG parquet 文件。

零 LLM 调用：利用已有的图结构（5505 nodes, 25750 edges）直接转换为
GraphRAG 格式，替代昂贵的 LLM 实体提取流程。

用法:
    python -m INAGENT.scripts.build_graphrag_from_graph
"""
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

_INAGENT = Path(__file__).resolve().parent.parent
_GRAPH_PATH = _INAGENT / "knowledge_base" / "cli_keyword_graph.json"
_KB_PATH = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"
_OUTPUT = _INAGENT / "graphrag_index" / "output"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _node_type(node: dict) -> str:
    ntype = node.get("type", "")
    if ntype == "module":
        return "MODULE"
    if ntype in ("command", "operation_command"):
        return "COMMAND"
    if ntype == "parameter":
        return "PARAMETER"
    return "COMMAND"


def _node_description(node: dict) -> str:
    """动态合成实体描述 — 上游 enrich 增删字段后自动跟进。

    优先使用 MODULE 节点预合成的 description（由 enrich_cli_graph 生成），
    若为空则从所有非系统字段拼接。COMMAND 节点走原有逻辑。
    """
    pre = node.get("description", "")
    if pre and node.get("type") == "module":
        return pre

    _SKIP = {"id", "type", "size", "commands_count", "human_readable_id",
             "frequency", "degree", "x", "y", "parent"}
    parts = []
    cp = node.get("command_prefix", "")
    if cp:
        parts.append(f"CLI命令: {cp}")
    hs = node.get("help_string", "")
    if hs:
        parts.append(hs)

    cn = node.get("cn_aliases", [])
    if cn:
        parts.append("中文名称: " + ", ".join(cn))

    func = node.get("func", "")
    if func:
        parts.append(f"内部函数: {func}")
    scope = node.get("scope", "")
    if scope:
        parts.append(f"作用域: {scope}")

    for key in ("protocol_stack", "feature_tags", "related_modules",
                "keywords", "layer", "address_family", "interface_types"):
        val = node.get(key)
        if val and isinstance(val, list):
            parts.append(f"{key}: {', '.join(str(v) for v in val)}")
        elif val and isinstance(val, str):
            parts.append(f"{key}: {val}")

    return ". ".join(parts) if parts else node.get("id", "")


def _edge_description(edge: dict, nodes_by_id: dict) -> str:
    etype = edge.get("type", "child_of")
    src = edge.get("source", "")
    tgt = edge.get("target", "")
    src_cp = ""
    tgt_cp = ""
    src_nodes = nodes_by_id.get(src, [])
    tgt_nodes = nodes_by_id.get(tgt, [])
    if src_nodes:
        src_cp = src_nodes[0].get("command_prefix", src)
    if tgt_nodes:
        tgt_cp = tgt_nodes[0].get("command_prefix", tgt)
    if etype == "child_of":
        return f"{src_cp} 是 {tgt_cp} 的子命令"
    if etype == "param_of":
        return f"{src_cp} 是 {tgt_cp} 的参数"
    return f"{src_cp} {etype} {tgt_cp}"


def build():
    print("Loading cli_keyword_graph.json ...")
    graph = json.loads(_GRAPH_PATH.read_text("utf-8"))
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    print(f"  {len(nodes)} nodes, {len(edges)} edges")

    nodes_by_id = defaultdict(list)
    for n in nodes:
        nid = n.get("id", "")
        if nid:
            nodes_by_id[nid].append(n)

    # Load KB for text_units
    kb = json.loads(_KB_PATH.read_text("utf-8"))
    kb_by_nid = {}
    for entry in kb:
        nid = entry.get("metadata", {}).get("node_id", "")
        if nid:
            kb_by_nid[nid] = entry

    # --- Entities ---
    seen_ids = set()
    entity_rows = []
    node_to_entity_id = {}

    for node in nodes:
        nid = node.get("id", "")
        if not nid or nid in seen_ids:
            continue
        seen_ids.add(nid)

        eid = _sha256(nid)
        node_to_entity_id[nid] = eid
        entity_rows.append({
            "id": eid,
            "human_readable_id": len(entity_rows),
            "title": nid.upper(),
            "type": _node_type(node),
            "description": _node_description(node),
            "text_unit_ids": [],
            "frequency": 1,
            "degree": 0,
            "x": 0,
            "y": 0,
        })

    print(f"  Entities: {len(entity_rows)}")

    # Enrich module entities with overview snippets from KB
    entity_title_to_idx = {r["title"].lower(): i for i, r in enumerate(entity_rows)}
    overview_count = 0
    enriched_entities = set()
    for entry in kb:
        em = entry.get("metadata", {})
        etitle = em.get("enhanced_title", "")
        if not etitle:
            continue
        mod = em.get("product_module", "")
        nid = em.get("node_id", "")
        candidates = [nid, mod] if nid else [mod]
        for cand in candidates:
            if not cand:
                continue
            idx = entity_title_to_idx.get(cand.lower())
            if idx is not None and idx not in enriched_entities:
                snippet = entry.get("page_content", "")[:300].strip()
                if snippet:
                    cur = entity_rows[idx]["description"]
                    entity_rows[idx]["description"] = f"{cur}. 概述: {snippet}"
                    overview_count += 1
                    enriched_entities.add(idx)
                break
    if overview_count:
        print(f"  Overview-enriched entities: {overview_count}")

    # --- Relationships ---
    degree_counter = defaultdict(int)
    rel_rows = []
    seen_edges = set()

    for edge in edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if not src or not tgt:
            continue
        edge_key = (src, tgt)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        if src not in node_to_entity_id or tgt not in node_to_entity_id:
            continue

        degree_counter[src] += 1
        degree_counter[tgt] += 1

        rel_rows.append({
            "id": _sha256(f"{src}->{tgt}"),
            "human_readable_id": len(rel_rows),
            "source": src.upper(),
            "target": tgt.upper(),
            "description": _edge_description(edge, nodes_by_id),
            "weight": 1.0,
            "combined_degree": 0,
            "text_unit_ids": [],
        })

    # Update degree
    entity_id_to_idx = {r["id"]: i for i, r in enumerate(entity_rows)}
    for nid, deg in degree_counter.items():
        eid = node_to_entity_id.get(nid)
        if eid and eid in entity_id_to_idx:
            entity_rows[entity_id_to_idx[eid]]["degree"] = deg

    # Update combined_degree for relationships
    for rel in rel_rows:
        src_nid = rel["source"].lower()
        tgt_nid = rel["target"].lower()
        rel["combined_degree"] = degree_counter.get(src_nid, 0) + degree_counter.get(tgt_nid, 0)

    print(f"  Relationships: {len(rel_rows)}")

    # --- Text Units (from KB) ---
    tu_rows = []
    doc_rows = []
    modules = defaultdict(list)
    for entry in kb:
        m = entry.get("metadata", {})
        mod = m.get("product_module", "unknown")
        modules[mod].append(entry)

    doc_id_counter = 0
    for mod, entries in sorted(modules.items()):
        doc_id = f"doc_{doc_id_counter:04d}"
        doc_id_counter += 1

        texts = []
        entity_ids_in_doc = []
        rel_ids_in_doc = []

        for entry in entries:
            nid = entry.get("metadata", {}).get("node_id", "")
            text = entry.get("page_content", "")
            texts.append(text)

            eid = node_to_entity_id.get(nid)
            if eid:
                entity_ids_in_doc.append(eid)

        full_text = "\n\n".join(texts)
        tu_id = _sha256(full_text)

        tu_rows.append({
            "id": tu_id,
            "text": full_text,
            "document_ids": [doc_id],
            "n_tokens": len(full_text) // 3,
            "entity_ids": entity_ids_in_doc,
            "relationship_ids": rel_ids_in_doc,
        })

        doc_rows.append({
            "id": doc_id,
            "human_readable_id": doc_id_counter - 1,
            "title": f"module_{mod}",
            "text": full_text,
            "text_unit_ids": [tu_id],
            "creation_date": "2026-04-04",
        })

        # Link entities to text units
        for eid in entity_ids_in_doc:
            idx = entity_id_to_idx.get(eid)
            if idx is not None:
                entity_rows[idx]["text_unit_ids"].append(tu_id)

    # Link relationships to text units
    # For each rel, find text units that contain both source and target entities
    entity_to_tu = {}
    for i, er in enumerate(entity_rows):
        entity_to_tu[er["id"]] = set(er["text_unit_ids"])

    for rel in rel_rows:
        src_eid = node_to_entity_id.get(rel["source"].lower())
        tgt_eid = node_to_entity_id.get(rel["target"].lower())
        if src_eid and tgt_eid:
            common_tus = entity_to_tu.get(src_eid, set()) & entity_to_tu.get(tgt_eid, set())
            if common_tus:
                rel["text_unit_ids"] = list(common_tus)
            else:
                src_tus = entity_to_tu.get(src_eid, set())
                tgt_tus = entity_to_tu.get(tgt_eid, set())
                rel["text_unit_ids"] = list(src_tus | tgt_tus)[:1]

    print(f"  Text Units: {len(tu_rows)}")
    print(f"  Documents: {len(doc_rows)}")

    # --- Communities (simple module-based) ---
    comm_rows = []
    comm_report_rows = []
    for comm_id, (mod, entries) in enumerate(sorted(modules.items())):
        eids = []
        for entry in entries:
            nid = entry.get("metadata", {}).get("node_id", "")
            eid = node_to_entity_id.get(nid)
            if eid:
                eids.append(eid)

        if not eids:
            continue

        rel_ids = []
        top_cmds = [e.get("metadata", {}).get("command_prefix", "") for e in entries[:5]]

        cid = _sha256(f"community_{comm_id}")
        comm_rows.append({
            "id": cid,
            "community": comm_id,
            "level": 0,
            "title": f"Module: {mod}",
            "entity_ids": eids,
            "relationship_ids": rel_ids,
            "text_unit_ids": [],
            "period": "",
            "size": len(eids),
        })

        comm_report_rows.append({
            "id": cid,
            "community": comm_id,
            "level": 0,
            "title": f"Module: {mod}",
            "summary": f"模块 {mod} 包含 {len(eids)} 个CLI命令实体。主要命令: {', '.join(top_cmds)}",
            "full_content": f"模块 {mod} 的命令集合, 包含 {len(eids)} 个命令。",
            "rank": len(eids),
            "rank_explanation": f"命令数量: {len(eids)}",
            "findings": [],
            "full_content_json": "",
            "period": "",
            "size": len(eids),
        })

    print(f"  Communities: {len(comm_rows)}")

    # --- Write Parquet Files ---
    _OUTPUT.mkdir(parents=True, exist_ok=True)

    ent_df = pd.DataFrame(entity_rows)
    ent_df.to_parquet(_OUTPUT / "entities.parquet", index=False)

    rel_df = pd.DataFrame(rel_rows)
    rel_df.to_parquet(_OUTPUT / "relationships.parquet", index=False)

    tu_df = pd.DataFrame(tu_rows)
    tu_df.to_parquet(_OUTPUT / "text_units.parquet", index=False)

    doc_df = pd.DataFrame(doc_rows)
    doc_df.to_parquet(_OUTPUT / "documents.parquet", index=False)

    comm_df = pd.DataFrame(comm_rows)
    comm_df.to_parquet(_OUTPUT / "communities.parquet", index=False)

    report_df = pd.DataFrame(comm_report_rows)
    report_df.to_parquet(_OUTPUT / "community_reports.parquet", index=False)

    print(f"\nAll parquet files written to {_OUTPUT}")

    # Verify coverage
    kb_nids = {e.get("metadata", {}).get("node_id", "") for e in kb if e.get("metadata", {}).get("node_id", "")}
    ent_titles = set(ent_df["title"].str.lower())
    covered = sum(1 for nid in kb_nids if nid.lower() in ent_titles)
    print(f"\nCoverage: {covered}/{len(kb_nids)} KB node_ids in entities ({covered/len(kb_nids):.1%})")


def populate_lancedb(batch_size: int = 64):
    import openai

    print("\n--- Populating LanceDB with entity description embeddings ---")

    ent_df = pd.read_parquet(_OUTPUT / "entities.parquet")

    if "description_embedding" in ent_df.columns and ent_df["description_embedding"].notna().any():
        print("  description_embedding 已存在于 entities.parquet，跳过 API 调用")
        all_embeddings = ent_df["description_embedding"].tolist()
    else:
        api_base = os.environ.get("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:9000")
        if not api_base.endswith("/v1"):
            api_base = api_base.rstrip("/") + "/v1"
        api_key = os.environ.get("LLM_GATEWAY_API_KEY", "test")
        model = "text-embedding-v4"

        client = openai.OpenAI(base_url=api_base, api_key=api_key)

        texts = ent_df["description"].fillna("").tolist()

        import time as _time
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch = [t if t else " " for t in batch]
            for attempt in range(5):
                try:
                    resp = client.embeddings.create(input=batch, model=model)
                    break
                except Exception as e:
                    wait = 10 * (attempt + 1)
                    print(f"  API error (attempt {attempt+1}): {e} — retrying in {wait}s")
                    _time.sleep(wait)
            else:
                raise RuntimeError(f"Failed after 5 retries at offset {i}")
            for d in resp.data:
                all_embeddings.append(d.embedding)
            done = min(i + batch_size, len(texts))
            if done % 500 < batch_size or done == len(texts):
                print(f"  Embedded {done}/{len(texts)}")

        print(f"  Total embeddings: {len(all_embeddings)}, dim: {len(all_embeddings[0])}")

        ent_df["description_embedding"] = all_embeddings
        ent_df.to_parquet(_OUTPUT / "entities.parquet", index=False)
        print("  Updated entities.parquet with description_embedding column")

    from graphrag.vector_stores.lancedb import LanceDBVectorStore
    from graphrag.vector_stores.base import VectorStoreDocument
    from graphrag.config.models.vector_store_schema_config import VectorStoreSchemaConfig

    ldb_uri = str(_OUTPUT / "lancedb")
    index_name = "default-entity-description"
    vec_dim = len(all_embeddings[0])

    schema_config = VectorStoreSchemaConfig(
        index_name=index_name,
        vector_size=vec_dim,
    )
    store = LanceDBVectorStore(vector_store_schema_config=schema_config)
    store.connect(
        db_uri=ldb_uri,
        type="lancedb",
        container_name="default",
    )

    ids = ent_df["id"].tolist()
    texts = ent_df["description"].fillna("").tolist()

    docs = []
    for eid, text, emb in zip(ids, texts, all_embeddings):
        vec = list(emb) if not isinstance(emb, list) else emb
        docs.append(VectorStoreDocument(
            id=eid,
            text=text,
            vector=vec,
        ))

    store.load_documents(docs, overwrite=True)
    print(f"  LanceDB populated: {len(docs)} documents in '{index_name}'")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(_INAGENT / ".env")

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--embed", action="store_true", help="生成 embedding 并写入 LanceDB")
    args = parser.parse_args()

    build()
    if args.embed:
        populate_lancedb()
