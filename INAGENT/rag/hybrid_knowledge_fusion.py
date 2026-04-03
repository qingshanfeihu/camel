from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from INAGENT.rag.knowledge_schema import build_entity_id, infer_node_type

logger = logging.getLogger(__name__)


class HybridKnowledgeFusion:
    """
    跨存储融合检索：
    - UnifiedRAG 作为主召回
    - Neo4j 作为关系补充
    - EntityLinkStore 维护对齐映射
    """

    def __init__(
        self,
        *,
        unified_rag,
        neo4j_store=None,
        entity_link_store=None,
    ):
        self.unified_rag = unified_rag
        self.neo4j_store = neo4j_store
        self.entity_link_store = entity_link_store

    def retrieve(
        self,
        *,
        query: str,
        category_whitelist: List[str],
        top_k_final: int = 8,
    ) -> Tuple[str, Dict[str, Any]]:
        ctx, constraints, _ = self.unified_rag.retrieve(
            query=query,
            top_k_final=top_k_final,
            category_whitelist=category_whitelist,
        )

        if not ctx:
            return "", constraints

        # 将主召回结果注册到 entity_link，便于后续跨存储关联。
        self._upsert_links_from_context(ctx)

        # Neo4j 关系补充（可选）
        graph_ctx = self._retrieve_from_neo4j(
            query=query,
            category_whitelist=category_whitelist,
        )
        if graph_ctx:
            merged = f"{ctx}\n\n[图谱关联补充]\n{graph_ctx}"
            return merged, constraints
        return ctx, constraints

    def _upsert_links_from_context(self, context: str) -> None:
        if not self.entity_link_store:
            return
        for chunk in [c.strip() for c in context.split("\n\n") if c.strip()]:
            if not chunk:
                continue
            category = self._extract_category_from_chunk(chunk)
            entity_id = build_entity_id(
                document_category=category,
                content=chunk,
            )
            node_type = infer_node_type(category).value
            try:
                self.entity_link_store.upsert_link(
                    entity_id=entity_id,
                    node_type=node_type,
                    document_category=category,
                    title=chunk[:80],
                )
            except Exception as e:
                logger.debug("entity_link upsert failed: %s", e)

    @staticmethod
    def _extract_category_from_chunk(chunk: str) -> str:
        """Try to extract document_category from INAGENT_META_JSON header or
        bracketed prefix like ``[cli/reference]``.  Falls back to
        ``doc/unknown``."""
        # Method 1: INAGENT_META_JSON header (same format as unified_rag)
        meta_match = re.match(r"^INAGENT_META_JSON:(\{.*?\})\n", chunk, re.DOTALL)
        if meta_match:
            try:
                meta = json.loads(meta_match.group(1))
                cat = meta.get("document_category", "")
                if cat:
                    return cat
            except (json.JSONDecodeError, TypeError):
                pass
        # Method 2: bracketed category prefix like "[cli/reference]"
        bracket_match = re.match(r"^\[([a-z]+/[a-z_]+)\]", chunk)
        if bracket_match:
            return bracket_match.group(1)
        return "doc/unknown"

    def _retrieve_from_neo4j(
        self,
        *,
        query: str,
        category_whitelist: Optional[List[str]] = None,
    ) -> str:
        if not self.neo4j_store:
            return ""
        try:
            rows = self.neo4j_store.search_related(
                query_text=query,
                categories=category_whitelist or [],
                top_k=6,
            )
        except Exception as e:
            logger.warning("Neo4j 检索失败: %s", e)
            return ""

        if not rows:
            return ""
        parts: List[str] = []
        for row in rows:
            title = row.get("title") or "(untitled)"
            cat = row.get("document_category") or ""
            content = (row.get("content") or "")[:600]
            neighbors = row.get("neighbors") or []
            parts.append(
                f"[{cat}] {title}\n{content}\n关联节点: {', '.join(neighbors[:5])}"
            )
        return "\n\n".join(parts)
