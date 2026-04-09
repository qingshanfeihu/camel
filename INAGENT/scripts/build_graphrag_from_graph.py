"""
从 cli_keyword_graph.json + knowledge_base.json 构建全产品知识图谱。

零 LLM 调用：
  - CLI graph nodes → COMMAND / MODULE 实体 + 命令间关系
  - KB 元数据 → SECTION / DOCUMENT 实体 + 语义关系
    (BELONGS_TO, FROM_DOCUMENT, PARENT_OF, REFERENCES_COMMAND)
  - 每个 KB chunk → 独立 text_unit（细粒度）
  - MODULE 级社区 + DOCUMENT 级社区

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

    kb = json.loads(_KB_PATH.read_text("utf-8"))
    print(f"  KB entries: {len(kb)}")

    # ═══════════════════════════════════════════════════════════════════
    # Phase A: Entities
    # ═══════════════════════════════════════════════════════════════════

    seen_ids = set()
    entity_rows = []
    node_to_entity_id = {}  # key (lowercase) → entity id (sha256)

    # A1: CLI graph nodes → COMMAND / MODULE entities
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
    cli_entity_count = len(entity_rows)
    print(f"  CLI entities: {cli_entity_count}")

    entity_title_to_idx = {r["title"].lower(): i for i, r in enumerate(entity_rows)}

    # A2: Enrich MODULE entities with KB overview snippets
    enriched_entities = set()
    for entry in kb:
        em = entry.get("metadata", {})
        etitle = em.get("enhanced_title", "")
        if not etitle:
            continue
        mod = em.get("product_module", "")
        nid = em.get("node_id", "")
        for cand in ([nid, mod] if nid else [mod]):
            if not cand:
                continue
            idx = entity_title_to_idx.get(cand.lower())
            if idx is not None and idx not in enriched_entities:
                snippet = entry.get("page_content", "")[:300].strip()
                if snippet:
                    cur = entity_rows[idx]["description"]
                    entity_rows[idx]["description"] = f"{cur}. 概述: {snippet}"
                    enriched_entities.add(idx)
                break

    # A3: DOCUMENT entities (from source_file)
    doc_source_files = sorted({
        e.get("metadata", {}).get("source_file", "")
        for e in kb if e.get("metadata", {}).get("source_file", "")
    })
    doc_entity_ids = {}  # source_file → entity_id
    for sf in doc_source_files:
        eid = _sha256(f"doc::{sf}")
        doc_entity_ids[sf] = eid
        idx = len(entity_rows)
        entity_rows.append({
            "id": eid,
            "human_readable_id": idx,
            "title": sf,
            "type": "DOCUMENT",
            "description": f"产品文档: {sf}",
            "text_unit_ids": [],
            "frequency": 1,
            "degree": 0,
            "x": 0,
            "y": 0,
        })
        entity_title_to_idx[sf.lower()] = idx
    print(f"  DOCUMENT entities: {len(doc_source_files)}")

    # A4: SECTION entities (from KB section_title, deduped by (source_file, section_title))
    # Pre-scan: collect longest page_content per section_title for description
    _best_section_desc = {}  # title_lower → longest page_content[:500]
    for entry in kb:
        meta = entry.get("metadata", {})
        title = meta.get("section_title", "").strip()
        if not title:
            continue
        content = entry.get("page_content", "").strip()
        title_lower = title.lower()
        prev = _best_section_desc.get(title_lower, "")
        if len(content) > len(prev):
            _best_section_desc[title_lower] = content[:500]

    section_entity_ids = {}  # (source_file, section_title) → entity_id
    section_count = 0
    for entry in kb:
        meta = entry.get("metadata", {})
        title = meta.get("section_title", "").strip()
        sf = meta.get("source_file", "")
        if not title:
            continue
        sec_key = (sf, title)
        if sec_key in section_entity_ids:
            continue
        title_lower = title.lower()
        if title_lower in entity_title_to_idx:
            section_entity_ids[sec_key] = entity_rows[entity_title_to_idx[title_lower]]["id"]
            continue
        eid = _sha256(f"sec::{sf}::{title}")
        idx = len(entity_rows)
        desc = _best_section_desc.get(title_lower, "")[:300].strip()
        entity_rows.append({
            "id": eid,
            "human_readable_id": idx,
            "title": title,
            "type": "SECTION",
            "description": desc,
            "text_unit_ids": [],
            "frequency": 1,
            "degree": 0,
            "x": 0,
            "y": 0,
        })
        entity_title_to_idx[title_lower] = idx
        section_entity_ids[sec_key] = eid
        section_count += 1
    print(f"  SECTION entities: {section_count}")

    # A5: MODULE entities from KB that don't exist in CLI graph
    kb_modules = sorted({
        e.get("metadata", {}).get("product_module", "")
        for e in kb if e.get("metadata", {}).get("product_module", "")
    })
    new_mod_count = 0
    for mod in kb_modules:
        if mod.lower() in entity_title_to_idx:
            continue
        eid = _sha256(f"kbmod::{mod}")
        idx = len(entity_rows)
        entity_rows.append({
            "id": eid,
            "human_readable_id": idx,
            "title": mod,
            "type": "MODULE",
            "description": f"产品模块: {mod}",
            "text_unit_ids": [],
            "frequency": 1,
            "degree": 0,
            "x": 0,
            "y": 0,
        })
        entity_title_to_idx[mod.lower()] = idx
        node_to_entity_id[mod.lower()] = eid
        new_mod_count += 1
    if new_mod_count:
        print(f"  KB-only MODULE entities: {new_mod_count}")

    print(f"  Total entities: {len(entity_rows)}")

    # ═══════════════════════════════════════════════════════════════════
    # Phase B: Relationships
    # ═══════════════════════════════════════════════════════════════════

    degree_counter = defaultdict(int)
    rel_rows = []
    seen_edges = set()

    def _add_rel(src_title: str, tgt_title: str, rel_type: str, desc: str):
        if src_title.lower() == tgt_title.lower():
            return
        edge_key = (src_title.lower(), tgt_title.lower(), rel_type)
        if edge_key in seen_edges:
            return
        seen_edges.add(edge_key)
        degree_counter[src_title.lower()] += 1
        degree_counter[tgt_title.lower()] += 1
        rel_rows.append({
            "id": _sha256(f"{src_title}->{tgt_title}::{rel_type}"),
            "human_readable_id": len(rel_rows),
            "source": src_title,
            "target": tgt_title,
            "description": desc,
            "weight": 1.0,
            "combined_degree": 0,
            "text_unit_ids": [],
        })

    # B1: CLI graph edges (COMMAND → COMMAND)
    for edge in edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if not src or not tgt:
            continue
        if src not in node_to_entity_id or tgt not in node_to_entity_id:
            continue
        _add_rel(src.upper(), tgt.upper(), "CLI_EDGE",
                 _edge_description(edge, nodes_by_id))
    cli_rel_count = len(rel_rows)

    # B2: SECTION → MODULE (BELONGS_TO)
    for entry in kb:
        meta = entry.get("metadata", {})
        title = meta.get("section_title", "").strip()
        mod = meta.get("product_module", "")
        sf = meta.get("source_file", "")
        if not title or not mod:
            continue
        sec_key = (sf, title)
        sec_eid = section_entity_ids.get(sec_key)
        if not sec_eid:
            continue
        sec_title = entity_rows[entity_title_to_idx[title.lower()]]["title"]
        mod_idx = entity_title_to_idx.get(mod.lower())
        if mod_idx is not None:
            mod_title = entity_rows[mod_idx]["title"]
            _add_rel(sec_title, mod_title, "BELONGS_TO",
                     f"{sec_title} 属于模块 {mod_title}")

    # B3: SECTION → DOCUMENT (FROM_DOCUMENT)
    for entry in kb:
        meta = entry.get("metadata", {})
        title = meta.get("section_title", "").strip()
        sf = meta.get("source_file", "")
        if not title or not sf:
            continue
        sec_idx = entity_title_to_idx.get(title.lower())
        if sec_idx is None:
            continue
        sec_title = entity_rows[sec_idx]["title"]
        if sf in doc_entity_ids:
            _add_rel(sec_title, sf, "FROM_DOCUMENT",
                     f"{sec_title} 来自文档 {sf}")

    # B4: SECTION → SECTION (PARENT_OF, from section_path hierarchy)
    seen_parent_rels = set()
    for entry in kb:
        meta = entry.get("metadata", {})
        path = meta.get("section_path", "")
        if not path:
            continue
        parts = [p.strip() for p in path.replace(">", "/").split("/") if p.strip()]
        for i in range(len(parts) - 1):
            parent = parts[i]
            child = parts[i + 1]
            pkey = (parent.lower(), child.lower())
            if pkey in seen_parent_rels:
                continue
            seen_parent_rels.add(pkey)
            p_idx = entity_title_to_idx.get(parent.lower())
            c_idx = entity_title_to_idx.get(child.lower())
            if p_idx is not None and c_idx is not None:
                p_title = entity_rows[p_idx]["title"]
                c_title = entity_rows[c_idx]["title"]
                _add_rel(p_title, c_title, "PARENT_OF",
                         f"{p_title} 包含子章节 {c_title}")

    # B5: SECTION → COMMAND (REFERENCES_COMMAND, from command_prefix)
    for entry in kb:
        meta = entry.get("metadata", {})
        title = meta.get("section_title", "").strip()
        cp = meta.get("command_prefix", "").strip()
        if not title or not cp:
            continue
        sec_idx = entity_title_to_idx.get(title.lower())
        cmd_idx = entity_title_to_idx.get(cp.lower())
        if sec_idx is not None and cmd_idx is not None and sec_idx != cmd_idx:
            sec_title = entity_rows[sec_idx]["title"]
            cmd_title = entity_rows[cmd_idx]["title"]
            _add_rel(sec_title, cmd_title, "REFERENCES_COMMAND",
                     f"{sec_title} 引用命令 {cmd_title}")

    kb_rel_count = len(rel_rows) - cli_rel_count

    # Update degree on entities
    entity_id_to_idx = {r["id"]: i for i, r in enumerate(entity_rows)}
    for nid_lower, deg in degree_counter.items():
        idx = entity_title_to_idx.get(nid_lower)
        if idx is not None:
            entity_rows[idx]["degree"] += deg

    for rel in rel_rows:
        src_lower = rel["source"].lower()
        tgt_lower = rel["target"].lower()
        rel["combined_degree"] = degree_counter.get(src_lower, 0) + degree_counter.get(tgt_lower, 0)

    print(f"  Relationships: {len(rel_rows)} (CLI: {cli_rel_count}, KB: {kb_rel_count})")

    # ═══════════════════════════════════════════════════════════════════
    # Phase C: Text Units (fine-grained, one per KB chunk)
    # ═══════════════════════════════════════════════════════════════════

    tu_rows = []
    doc_rows = []
    tu_id_by_kb_idx = {}  # kb list index → text_unit id

    source_file_entries = defaultdict(list)
    for i, entry in enumerate(kb):
        sf = entry.get("metadata", {}).get("source_file", "unknown")
        source_file_entries[sf].append((i, entry))

    doc_id_counter = 0
    for sf, entries_with_idx in sorted(source_file_entries.items()):
        doc_id = f"doc_{doc_id_counter:04d}"
        doc_id_counter += 1
        tu_ids_in_doc = []

        for kb_idx, entry in entries_with_idx:
            meta = entry.get("metadata", {})
            text = entry.get("page_content", "")
            if not text.strip():
                continue

            tu_id = _sha256(f"tu::{kb_idx}::{text[:100]}")
            tu_id_by_kb_idx[kb_idx] = tu_id

            linked_eids = []
            title = meta.get("section_title", "").strip()
            if title:
                idx = entity_title_to_idx.get(title.lower())
                if idx is not None:
                    linked_eids.append(entity_rows[idx]["id"])
            nid = meta.get("node_id", "")
            if nid:
                eid = node_to_entity_id.get(nid)
                if eid and eid not in linked_eids:
                    linked_eids.append(eid)
            cp = meta.get("command_prefix", "").strip()
            if cp:
                cp_idx = entity_title_to_idx.get(cp.upper().lower())
                if cp_idx is not None:
                    cp_eid = entity_rows[cp_idx]["id"]
                    if cp_eid not in linked_eids:
                        linked_eids.append(cp_eid)
            mod = meta.get("product_module", "")
            if mod:
                mod_idx = entity_title_to_idx.get(mod.lower())
                if mod_idx is not None:
                    mod_eid = entity_rows[mod_idx]["id"]
                    if mod_eid not in linked_eids:
                        linked_eids.append(mod_eid)

            tu_rows.append({
                "id": tu_id,
                "text": text,
                "document_ids": [doc_id],
                "n_tokens": len(text) // 3,
                "entity_ids": linked_eids,
                "relationship_ids": [],
            })
            tu_ids_in_doc.append(tu_id)

            for eid in linked_eids:
                eidx = entity_id_to_idx.get(eid)
                if eidx is not None:
                    entity_rows[eidx]["text_unit_ids"].append(tu_id)

        doc_rows.append({
            "id": doc_id,
            "human_readable_id": doc_id_counter - 1,
            "title": sf,
            "text": "",
            "text_unit_ids": tu_ids_in_doc,
            "creation_date": "2026-04-08",
        })

    # Link relationships to text units
    entity_to_tus = {}
    for er in entity_rows:
        entity_to_tus[er["id"]] = set(er["text_unit_ids"])

    for rel in rel_rows:
        src_idx = entity_title_to_idx.get(rel["source"].lower())
        tgt_idx = entity_title_to_idx.get(rel["target"].lower())
        if src_idx is None or tgt_idx is None:
            continue
        src_eid = entity_rows[src_idx]["id"]
        tgt_eid = entity_rows[tgt_idx]["id"]
        common = entity_to_tus.get(src_eid, set()) & entity_to_tus.get(tgt_eid, set())
        if common:
            rel["text_unit_ids"] = list(common)[:3]
        else:
            union = entity_to_tus.get(src_eid, set()) | entity_to_tus.get(tgt_eid, set())
            rel["text_unit_ids"] = list(union)[:1]

    print(f"  Text Units: {len(tu_rows)}")
    print(f"  Documents: {len(doc_rows)}")

    # ═══════════════════════════════════════════════════════════════════
    # Phase D: Communities (MODULE-level + DOCUMENT-level)
    # ═══════════════════════════════════════════════════════════════════

    comm_rows = []
    comm_report_rows = []
    comm_id = 0

    # D1: MODULE communities (level=0)
    modules = defaultdict(list)
    for entry in kb:
        mod = entry.get("metadata", {}).get("product_module", "unknown")
        modules[mod].append(entry)

    for mod, entries in sorted(modules.items()):
        eids = set()
        for entry in entries:
            nid = entry.get("metadata", {}).get("node_id", "")
            if nid:
                eid = node_to_entity_id.get(nid)
                if eid:
                    eids.add(eid)
            title = entry.get("metadata", {}).get("section_title", "").strip()
            if title:
                idx = entity_title_to_idx.get(title.lower())
                if idx is not None:
                    eids.add(entity_rows[idx]["id"])
        if not eids:
            continue
        top_titles = []
        seen = set()
        for e in entries[:10]:
            t = e.get("metadata", {}).get("section_title", "")
            if t and t not in seen:
                top_titles.append(t)
                seen.add(t)
                if len(top_titles) >= 5:
                    break
        cid = _sha256(f"community_mod_{comm_id}")
        eids_list = list(eids)
        comm_rows.append({
            "id": cid, "community": comm_id, "level": 0,
            "title": f"Module: {mod}",
            "entity_ids": eids_list, "relationship_ids": [],
            "text_unit_ids": [], "period": "", "size": len(eids_list),
        })
        comm_report_rows.append({
            "id": cid, "community": comm_id, "level": 0,
            "title": f"Module: {mod}",
            "summary": f"模块 {mod} 包含 {len(eids_list)} 个实体。主要章节: {', '.join(top_titles)}",
            "full_content": f"模块 {mod} 的知识集合。",
            "rank": len(eids_list),
            "rank_explanation": f"实体数量: {len(eids_list)}",
            "findings": [], "full_content_json": "",
            "period": "", "size": len(eids_list),
        })
        comm_id += 1

    # D2: DOCUMENT communities (level=1)
    for sf in doc_source_files:
        eids = set()
        sf_entries = source_file_entries.get(sf, [])
        for _, entry in sf_entries:
            title = entry.get("metadata", {}).get("section_title", "").strip()
            if title:
                idx = entity_title_to_idx.get(title.lower())
                if idx is not None:
                    eids.add(entity_rows[idx]["id"])
        if sf in doc_entity_ids:
            eids.add(doc_entity_ids[sf])
        if not eids:
            continue
        cid = _sha256(f"community_doc_{comm_id}")
        eids_list = list(eids)
        comm_rows.append({
            "id": cid, "community": comm_id, "level": 1,
            "title": f"Document: {sf}",
            "entity_ids": eids_list, "relationship_ids": [],
            "text_unit_ids": [], "period": "", "size": len(eids_list),
        })
        comm_report_rows.append({
            "id": cid, "community": comm_id, "level": 1,
            "title": f"Document: {sf}",
            "summary": f"文档 {sf} 包含 {len(sf_entries)} 个知识块, {len(eids_list)} 个实体。",
            "full_content": f"文档 {sf} 的知识集合。",
            "rank": len(eids_list),
            "rank_explanation": f"实体数量: {len(eids_list)}",
            "findings": [], "full_content_json": "",
            "period": "", "size": len(eids_list),
        })
        comm_id += 1

    print(f"  Communities: {len(comm_rows)}")

    # ═══════════════════════════════════════════════════════════════════
    # Write Parquet Files
    # ═══════════════════════════════════════════════════════════════════

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

    # ═══════════════════════════════════════════════════════════════════
    # Coverage report
    # ═══════════════════════════════════════════════════════════════════

    ent_titles_lower = set(ent_df["title"].str.lower())
    kb_nids = {e.get("metadata", {}).get("node_id", "") for e in kb
               if e.get("metadata", {}).get("node_id", "")}
    covered_nid = sum(1 for nid in kb_nids if nid.lower() in ent_titles_lower)

    kb_sections = {e.get("metadata", {}).get("section_title", "").strip().lower()
                   for e in kb if e.get("metadata", {}).get("section_title", "").strip()}
    covered_sec = sum(1 for s in kb_sections if s in ent_titles_lower)

    print(f"\nCoverage:")
    print(f"  node_id: {covered_nid}/{len(kb_nids)} ({covered_nid/max(len(kb_nids),1):.1%})")
    print(f"  section_title: {covered_sec}/{len(kb_sections)} ({covered_sec/max(len(kb_sections),1):.1%})")
    print(f"  Entities by type: {dict(ent_df['type'].value_counts())}")


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
