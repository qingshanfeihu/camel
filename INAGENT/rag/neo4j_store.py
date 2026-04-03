from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Neo4jStore:
    """Neo4j 轻量封装，提供统一节点/关系写入和检索。"""

    def __init__(
        self,
        *,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.database = database
        self._driver = None

        if not self.enabled:
            return

        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(uri, auth=(username, password))
            with self._driver.session(database=self.database) as session:
                session.run("RETURN 1")
            self._init_constraints()
            logger.info("Neo4jStore 已连接: %s/%s", uri, database)
        except Exception as e:
            logger.warning("Neo4jStore 初始化失败，已降级禁用: %s", e)
            self.enabled = False
            self._driver = None

    def _init_constraints(self) -> None:
        if not self._driver:
            return
        with self._driver.session(database=self.database) as session:
            session.run(
                "CREATE CONSTRAINT knowledge_entity_id IF NOT EXISTS "
                "FOR (n:KnowledgeEntity) REQUIRE n.entity_id IS UNIQUE"
            )
            session.run(
                "CREATE INDEX knowledge_category IF NOT EXISTS "
                "FOR (n:KnowledgeEntity) ON (n.document_category)"
            )

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None

    def upsert_entity(self, entity: Dict[str, Any]) -> Optional[str]:
        if not self.enabled or not self._driver:
            return None
        node_type = (entity.get("node_type") or "doc").upper()
        q = (
            "MERGE (n:KnowledgeEntity {entity_id: $entity_id}) "
            "SET n += $props "
            f"SET n:{node_type} "
            "RETURN elementId(n) AS node_id"
        )
        with self._driver.session(database=self.database) as session:
            rec = session.run(
                q,
                entity_id=entity["entity_id"],
                props={
                    "title": entity.get("title", ""),
                    "content": entity.get("content", ""),
                    "document_category": entity.get("document_category", ""),
                    "source_ref": entity.get("source_ref", ""),
                    "node_type": entity.get("node_type", "doc"),
                    "metadata_json": str(entity.get("metadata", {})),
                },
            ).single()
        return rec["node_id"] if rec else None

    def upsert_relation(
        self,
        *,
        source_entity_id: str,
        target_entity_id: str,
        rel_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not self._driver:
            return
        rel = (rel_type or "RELATED_TO").upper()
        q = (
            "MATCH (a:KnowledgeEntity {entity_id: $src}), "
            "(b:KnowledgeEntity {entity_id: $dst}) "
            f"MERGE (a)-[r:{rel}]->(b) "
            "SET r += $props"
        )
        with self._driver.session(database=self.database) as session:
            session.run(
                q,
                src=source_entity_id,
                dst=target_entity_id,
                props=properties or {},
            )

    def search_related(
        self,
        *,
        query_text: str,
        categories: Optional[List[str]] = None,
        top_k: int = 8,
    ) -> List[Dict[str, Any]]:
        if not self.enabled or not self._driver:
            return []
        categories = categories or []
        cypher = (
            "MATCH (n:KnowledgeEntity) "
            "WHERE ($categories = [] OR n.document_category IN $categories) "
            "AND (toLower(n.title) CONTAINS toLower($q) "
            "OR toLower(n.content) CONTAINS toLower($q)) "
            "OPTIONAL MATCH (n)-[r]-(m:KnowledgeEntity) "
            "RETURN n.entity_id AS entity_id, n.title AS title, "
            "n.content AS content, n.document_category AS document_category, "
            "collect(DISTINCT m.entity_id)[0..5] AS neighbors, "
            "count(r) AS degree "
            "ORDER BY degree DESC "
            "LIMIT $top_k"
        )
        with self._driver.session(database=self.database) as session:
            rows = session.run(
                cypher,
                q=query_text,
                categories=categories,
                top_k=top_k,
            )
            return [dict(r) for r in rows]
