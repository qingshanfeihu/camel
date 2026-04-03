#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from INAGENT.config.project_config import cfg_str
from INAGENT.rag.knowledge_schema import (
    KnowledgeEntity,
    KnowledgeNodeType,
)
from INAGENT.rag.neo4j_store import Neo4jStore


def _env(name: str, default: str = "") -> str:
    mapping = {
        "NEO4J_URI": "storage.neo4j.uri",
        "NEO4J_USERNAME": "storage.neo4j.username",
        "NEO4J_PASSWORD": "storage.neo4j.password",
        "NEO4J_DATABASE": "storage.neo4j.database",
    }
    cfg_key = mapping.get(name, "")
    if not cfg_key:
        return default.strip()
    return cfg_str(cfg_key, default, env=name).strip()


def load_graph(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 CLI 图谱到 Neo4j")
    parser.add_argument(
        "--graph",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "knowledge_base"
        / "cli_keyword_graph.json",
    )
    parser.add_argument("--uri", default=_env("NEO4J_URI", "bolt://127.0.0.1:7687"))
    parser.add_argument("--user", default=_env("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--password", default=_env("NEO4J_PASSWORD", "neo4j"))
    parser.add_argument("--database", default=_env("NEO4J_DATABASE", "neo4j"))
    args = parser.parse_args()

    graph = load_graph(args.graph)
    store = Neo4jStore(
        uri=args.uri,
        username=args.user,
        password=args.password,
        database=args.database,
        enabled=True,
    )
    if not store.enabled:
        raise RuntimeError("Neo4j 不可用，导入中止。")

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    node_id_map = {}
    for node in nodes:
        ntype = (node.get("type") or "").lower()
        if ntype == "module":
            ktype = KnowledgeNodeType.DESIGN
            cat = "spec/design"
        elif ntype == "command":
            ktype = KnowledgeNodeType.CLI
            cat = "cli/reference"
        else:
            ktype = KnowledgeNodeType.DOC
            cat = "doc/unknown"
        entity = KnowledgeEntity(
            entity_id=f"cli_graph:{node.get('id')}",
            node_type=ktype,
            title=node.get("label") or node.get("id") or "cli_node",
            content=node.get("help_string") or "",
            document_category=cat,
            source_ref=str(args.graph),
            metadata=node,
        )
        node_id = store.upsert_entity(entity.to_dict())
        node_id_map[entity.entity_id] = node_id

    for edge in edges:
        src = f"cli_graph:{edge.get('source')}"
        dst = f"cli_graph:{edge.get('target')}"
        store.upsert_relation(
            source_entity_id=src,
            target_entity_id=dst,
            rel_type=edge.get("type") or "RELATED_TO",
            properties={"weight": edge.get("weight", 1.0)},
        )

    print(
        f"Imported CLI graph to Neo4j: nodes={len(nodes)} edges={len(edges)} "
        f"database={args.database}"
    )


if __name__ == "__main__":
    main()
