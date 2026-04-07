from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional


class EntityLinkStore:
    """跨存储实体对齐索引: entity_id -> graphrag/qdrant/neo4j."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_links (
                entity_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL DEFAULT 'doc',
                document_category TEXT NOT NULL DEFAULT '',
                tree_level TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                graphrag_doc_id TEXT NOT NULL DEFAULT '',
                qdrant_point_id TEXT NOT NULL DEFAULT '',
                neo4j_node_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_entity_links_category
            ON entity_links(document_category);
            CREATE INDEX IF NOT EXISTS idx_entity_links_node_type
            ON entity_links(node_type);
            CREATE INDEX IF NOT EXISTS idx_entity_links_tree_level
            ON entity_links(tree_level);
            """
        )
        self._try_add_column("entity_links", "tree_level", "TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    def _try_add_column(self, table: str, column: str, col_def: str) -> None:
        try:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        except sqlite3.OperationalError:
            pass

    def upsert_link(
        self,
        *,
        entity_id: str,
        node_type: str,
        document_category: str,
        tree_level: str = "",
        source_ref: str = "",
        title: str = "",
        graphrag_doc_id: str = "",
        qdrant_point_id: str = "",
        neo4j_node_id: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO entity_links(
                entity_id, node_type, document_category, tree_level, source_ref, title,
                graphrag_doc_id, qdrant_point_id, neo4j_node_id, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(entity_id) DO UPDATE SET
                node_type=excluded.node_type,
                document_category=excluded.document_category,
                tree_level=CASE
                    WHEN excluded.tree_level != '' THEN excluded.tree_level
                    ELSE entity_links.tree_level
                END,
                source_ref=excluded.source_ref,
                title=excluded.title,
                graphrag_doc_id=CASE
                    WHEN excluded.graphrag_doc_id != '' THEN excluded.graphrag_doc_id
                    ELSE entity_links.graphrag_doc_id
                END,
                qdrant_point_id=CASE
                    WHEN excluded.qdrant_point_id != '' THEN excluded.qdrant_point_id
                    ELSE entity_links.qdrant_point_id
                END,
                neo4j_node_id=CASE
                    WHEN excluded.neo4j_node_id != '' THEN excluded.neo4j_node_id
                    ELSE entity_links.neo4j_node_id
                END,
                updated_at=datetime('now')
            """,
            (
                entity_id,
                node_type,
                document_category,
                tree_level,
                source_ref,
                title,
                graphrag_doc_id,
                qdrant_point_id,
                neo4j_node_id,
            ),
        )
        self.conn.commit()

    def get_link(self, entity_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM entity_links WHERE entity_id = ?",
            (entity_id,),
        ).fetchone()
        return dict(row) if row else None
