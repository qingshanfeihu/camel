from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent / "vector_store" / "skeleton_index.db"
)


class SkeletonIndex:
    """Module -> Feature -> Artifact cross-storage registry.

    SQLite-based backbone connecting CLI graph modules to knowledge chunks,
    Qdrant points, and GraphRAG entities.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS modules (
                module_id TEXT PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                protocol_stack TEXT NOT NULL DEFAULT '[]',
                layer TEXT NOT NULL DEFAULT '',
                address_family TEXT NOT NULL DEFAULT '[]',
                feature_tags TEXT NOT NULL DEFAULT '[]',
                commands_count INTEGER NOT NULL DEFAULT 0,
                aliases TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'cli_graph'
            );

            CREATE TABLE IF NOT EXISTS features (
                feature_id TEXT PRIMARY KEY,
                module_id TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                depth INTEGER NOT NULL DEFAULT 1,
                kb_fh_mappings TEXT NOT NULL DEFAULT '[]',
                knowledge_sources TEXT NOT NULL DEFAULT '{}',
                trunk_layers TEXT NOT NULL DEFAULT '[]',
                trunk_planes TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_features_module
            ON features(module_id);

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                artifact_type TEXT NOT NULL DEFAULT 'doc',
                module_id TEXT NOT NULL DEFAULT '',
                document_category TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                qdrant_point_id TEXT NOT NULL DEFAULT '',
                graphrag_entity_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                quality_score REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_module
            ON artifacts(module_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_type
            ON artifacts(artifact_type);
            CREATE INDEX IF NOT EXISTS idx_artifacts_category
            ON artifacts(document_category);

            CREATE TABLE IF NOT EXISTS artifact_links (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                link_type TEXT NOT NULL DEFAULT 'references',
                confidence REAL NOT NULL DEFAULT 1.0,
                face_type TEXT NOT NULL DEFAULT 'general',
                PRIMARY KEY (source_id, target_id, link_type)
            );

            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                artifact_id TEXT NOT NULL DEFAULT '',
                was_used INTEGER NOT NULL DEFAULT 0,
                run_id TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_log_run
            ON retrieval_log(run_id);
            """
        )
        self._try_add_column("modules", "aliases", "TEXT NOT NULL DEFAULT '[]'")
        self._try_add_column(
            "artifact_links", "face_type", "TEXT NOT NULL DEFAULT 'general'"
        )
        self._try_add_column(
            "artifacts", "tree_level", "TEXT NOT NULL DEFAULT ''"
        )
        self._conn.commit()

    def _try_add_column(self, table: str, column: str, col_def: str) -> None:
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        except sqlite3.OperationalError:
            pass

    def seed_from_cli_graph(self, cli_graph_store) -> int:
        """Bulk-load module nodes from CLIGraphStore.

        Returns the number of modules inserted/updated.
        """
        cli_graph_store._ensure_loaded()
        count = 0
        for mid, node in cli_graph_store._module_nodes.items():
            label = node.get("label", mid)
            aliases = [mid.lower(), label.lower()]
            related = node.get("related_modules", [])
            if related:
                aliases.extend(r.lower() for r in related[:3])
            aliases = list(dict.fromkeys(aliases))

            self._conn.execute(
                """
                INSERT INTO modules(
                    module_id, label, protocol_stack, layer,
                    address_family, feature_tags, commands_count, aliases, source
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'cli_graph')
                ON CONFLICT(module_id) DO UPDATE SET
                    label=excluded.label,
                    protocol_stack=excluded.protocol_stack,
                    layer=excluded.layer,
                    address_family=excluded.address_family,
                    feature_tags=excluded.feature_tags,
                    commands_count=excluded.commands_count,
                    aliases=excluded.aliases
                """,
                (
                    mid,
                    label,
                    json.dumps(node.get("protocol_stack", []), ensure_ascii=False),
                    node.get("layer", ""),
                    json.dumps(node.get("address_family", []), ensure_ascii=False),
                    json.dumps(node.get("feature_tags", []), ensure_ascii=False),
                    node.get("commands_count", 0),
                    json.dumps(aliases, ensure_ascii=False),
                ),
            )
            count += 1
        self._conn.commit()
        logger.info("SkeletonIndex: seeded %d modules from CLI graph", count)
        return count

    def resolve_module(self, hint: str) -> Optional[str]:
        """从 alias/label/id 任意入口查找 canonical module_id。"""
        if not hint:
            return None
        hint_lower = hint.strip().lower()

        row = self._conn.execute(
            "SELECT module_id FROM modules WHERE lower(module_id) = ?",
            (hint_lower,),
        ).fetchone()
        if row:
            return row[0]

        row = self._conn.execute(
            "SELECT module_id FROM modules WHERE lower(label) = ?",
            (hint_lower,),
        ).fetchone()
        if row:
            return row[0]

        rows = self._conn.execute(
            "SELECT module_id, aliases FROM modules"
        ).fetchall()
        for r in rows:
            try:
                alias_list = json.loads(r["aliases"])
            except (json.JSONDecodeError, TypeError):
                alias_list = []
            if hint_lower in alias_list:
                return r["module_id"]

        return None

    def seed_features_from_cli_tree(self, cli_graph_store) -> int:
        """从 CLI graph 的 parent_of 结构 seed features 表。

        提取每个 module 下 depth=1 的 branch 节点，
        并统计 KB 中各 document_category 的 chunk 计数。
        """
        cli_graph_store._ensure_loaded()
        count = 0

        kb_fh_stats = self._compute_kb_fh_stats(cli_graph_store)

        for mid in cli_graph_store._module_ids:
            branch_ids = cli_graph_store.get_branch_ids(mid)
            trunk = cli_graph_store.derive_trunk(mid)

            for bid in branch_ids:
                node = cli_graph_store._nodes_by_id.get(bid, {})
                label = node.get("label", bid)

                ks = kb_fh_stats.get(bid, {})

                self._conn.execute(
                    """
                    INSERT INTO features(
                        feature_id, module_id, label, depth,
                        kb_fh_mappings, knowledge_sources,
                        trunk_layers, trunk_planes
                    )
                    VALUES(?, ?, ?, 1, '[]', ?, ?, ?)
                    ON CONFLICT(feature_id) DO UPDATE SET
                        module_id=excluded.module_id,
                        label=excluded.label,
                        knowledge_sources=excluded.knowledge_sources,
                        trunk_layers=excluded.trunk_layers,
                        trunk_planes=excluded.trunk_planes
                    """,
                    (
                        bid,
                        mid,
                        label,
                        json.dumps(ks, ensure_ascii=False),
                        json.dumps(trunk.get("layers", []), ensure_ascii=False),
                        json.dumps(trunk.get("planes", []), ensure_ascii=False),
                    ),
                )
                count += 1

        self._conn.commit()
        logger.info("SkeletonIndex: seeded %d features from CLI tree", count)
        return count

    def _compute_kb_fh_stats(self, cli_graph_store) -> Dict[str, Dict[str, int]]:
        """统计 KB chunks 中各 branch 的 tree_level 分布。"""
        _KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"
        kb_path = _KB_DIR / "knowledge_base.json"
        if not kb_path.exists():
            return {}

        try:
            with open(kb_path, "r", encoding="utf-8") as f:
                chunks = json.load(f)
        except Exception:
            return {}

        from INAGENT.rag.knowledge_config import CATEGORY_TO_TREE_LEVEL

        stats: Dict[str, Dict[str, int]] = {}
        for chunk in chunks:
            meta = chunk.get("metadata") or {}
            fh = meta.get("function_hierarchy", "")
            if not fh:
                rm = meta.get("regex_metadata") or {}
                if isinstance(rm, dict):
                    fh = rm.get("function_hierarchy", "")
            if not fh:
                continue

            tree_pos = meta.get("tree_position") or {}
            cat_key = tree_pos.get("tree_level", "") if isinstance(tree_pos, dict) else ""
            if not cat_key:
                old_cat = meta.get("document_category", "")
                cat_key = CATEGORY_TO_TREE_LEVEL.get(old_cat, "unknown")

            content = str(chunk.get("page_content") or chunk.get("text") or "")
            content_lower = content[:200].lower()

            for mid in cli_graph_store._module_ids:
                branch_ids = cli_graph_store.get_branch_ids(mid)
                for bid in branch_ids:
                    bid_tokens = set(bid.lower().split("_"))
                    fh_lower = fh.lower().replace(" ", "")
                    matched = False
                    for t in bid_tokens:
                        if len(t) >= 3 and t in fh_lower:
                            matched = True
                            break
                    if not matched:
                        for t in bid_tokens:
                            if len(t) >= 3 and t in content_lower:
                                matched = True
                                break
                    if matched:
                        if bid not in stats:
                            stats[bid] = {}
                        stats[bid][cat_key] = stats[bid].get(cat_key, 0) + 1

        return stats

    def get_feature_tree(self, module_id: str) -> List[Dict[str, Any]]:
        """返回模块下完整的 feature branch 树。"""
        rows = self._conn.execute(
            "SELECT * FROM features WHERE module_id = ? ORDER BY label",
            (module_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            for field in ("kb_fh_mappings", "knowledge_sources", "trunk_layers", "trunk_planes"):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = [] if field != "knowledge_sources" else {}
            results.append(d)
        return results

    def get_feature(self, feature_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM features WHERE feature_id = ?", (feature_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for field in ("kb_fh_mappings", "knowledge_sources", "trunk_layers", "trunk_planes"):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = [] if field != "knowledge_sources" else {}
        return d

    def feature_count(self, module_id: Optional[str] = None) -> int:
        if module_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM features WHERE module_id = ?",
                (module_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM features").fetchone()
        return row[0] if row else 0

    def update_knowledge_sources(
        self, feature_id: str, category_key: str, delta: int = 1
    ) -> None:
        """增量更新 feature 的 knowledge_sources 中某个 category 的计数。"""
        row = self._conn.execute(
            "SELECT knowledge_sources FROM features WHERE feature_id = ?",
            (feature_id,),
        ).fetchone()
        if not row:
            return
        try:
            ks = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            ks = {}
        ks[category_key] = ks.get(category_key, 0) + delta
        self._conn.execute(
            "UPDATE features SET knowledge_sources = ? WHERE feature_id = ?",
            (json.dumps(ks, ensure_ascii=False), feature_id),
        )
        self._conn.commit()

    _SOURCE_KEYS = ("leaf", "new_leaf", "branch", "trunk", "root")

    def get_knowledge_completeness(self, feature_id: str) -> Dict[str, Any]:
        """返回功能节点的知识完整度。"""
        feat = self.get_feature(feature_id)
        if not feat:
            return {
                "feature_id": feature_id,
                "source_coverage": {},
                "source_coverage_ratio": 0.0,
                "trunk_completeness": {},
                "growth_stage": "unknown",
            }

        ks = feat.get("knowledge_sources", {})
        covered = sum(1 for k in self._SOURCE_KEYS if ks.get(k, 0) > 0)
        ratio = covered / len(self._SOURCE_KEYS) if self._SOURCE_KEYS else 0.0

        trunk_layers = feat.get("trunk_layers", [])
        trunk_planes = feat.get("trunk_planes", [])

        if ks.get("root", 0) > 0:
            stage = "root"
        elif ks.get("trunk", 0) > 0 or trunk_layers:
            stage = "trunk"
        elif ks.get("branch", 0) > 0:
            stage = "branch"
        elif ks.get("leaf", 0) > 0 or ks.get("new_leaf", 0) > 0:
            stage = "leaf"
        else:
            stage = "unknown"

        return {
            "feature_id": feature_id,
            "source_coverage": {k: ks.get(k, 0) for k in self._SOURCE_KEYS},
            "source_coverage_ratio": round(ratio, 2),
            "trunk_completeness": {
                "layers_known": trunk_layers,
                "planes_known": trunk_planes,
            },
            "growth_stage": stage,
        }

    def module_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM modules").fetchone()
        return row[0] if row else 0

    def get_module(self, module_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM modules WHERE module_id = ?", (module_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for field in ("protocol_stack", "address_family", "feature_tags"):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        return d

    def list_modules(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT module_id, label, commands_count FROM modules ORDER BY label"
        ).fetchall()
        return [dict(r) for r in rows]

    def register_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str = "doc",
        module_id: str = "",
        document_category: str = "",
        tree_level: str = "",
        source_file: str = "",
        qdrant_point_id: str = "",
        graphrag_entity_id: str = "",
        title: str = "",
        quality_score: float = 1.0,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO artifacts(
                artifact_id, artifact_type, module_id, document_category,
                tree_level, source_file, qdrant_point_id, graphrag_entity_id,
                title, quality_score, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(artifact_id) DO UPDATE SET
                artifact_type=excluded.artifact_type,
                module_id=CASE
                    WHEN excluded.module_id != '' THEN excluded.module_id
                    ELSE artifacts.module_id
                END,
                document_category=excluded.document_category,
                tree_level=CASE
                    WHEN excluded.tree_level != '' THEN excluded.tree_level
                    ELSE artifacts.tree_level
                END,
                source_file=excluded.source_file,
                qdrant_point_id=CASE
                    WHEN excluded.qdrant_point_id != '' THEN excluded.qdrant_point_id
                    ELSE artifacts.qdrant_point_id
                END,
                graphrag_entity_id=CASE
                    WHEN excluded.graphrag_entity_id != '' THEN excluded.graphrag_entity_id
                    ELSE artifacts.graphrag_entity_id
                END,
                title=excluded.title,
                quality_score=excluded.quality_score
            """,
            (
                artifact_id,
                artifact_type,
                module_id,
                document_category,
                tree_level,
                source_file,
                qdrant_point_id,
                graphrag_entity_id,
                title,
                quality_score,
            ),
        )
        self._conn.commit()

    def get_module_artifacts(
        self, module_id: str, artifact_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if artifact_type:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE module_id = ? AND artifact_type = ?",
                (module_id, artifact_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE module_id = ?",
                (module_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def artifact_count(self, module_id: Optional[str] = None) -> int:
        if module_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE module_id = ?",
                (module_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        return row[0] if row else 0

    def add_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str = "references",
        confidence: float = 1.0,
        face_type: str = "general",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO artifact_links(source_id, target_id, link_type, confidence, face_type)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET
                confidence=excluded.confidence,
                face_type=excluded.face_type
            """,
            (source_id, target_id, link_type, confidence, face_type),
        )
        self._conn.commit()

    def log_retrieval(
        self,
        query: str,
        artifact_id: str,
        was_used: bool,
        run_id: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO retrieval_log(query, artifact_id, was_used, run_id, timestamp)
            VALUES(?, ?, ?, ?, datetime('now'))
            """,
            (query, artifact_id, int(was_used), run_id),
        )
        self._conn.commit()

    def get_retrieval_stats(self, run_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(was_used) as used,
                COUNT(*) - SUM(was_used) as wasted
            FROM retrieval_log WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if not row or row[0] == 0:
            return {"total": 0, "used": 0, "wasted": 0, "waste_ratio": 0.0}
        total = row[0]
        used = row[1] or 0
        return {
            "total": total,
            "used": used,
            "wasted": total - used,
            "waste_ratio": round((total - used) / total, 3) if total > 0 else 0.0,
        }

    def get_coverage_summary(self, module_id: str) -> Dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT artifact_type, COUNT(*) as cnt
            FROM artifacts WHERE module_id = ?
            GROUP BY artifact_type
            """,
            (module_id,),
        ).fetchall()
        breakdown = {r["artifact_type"]: r["cnt"] for r in rows}
        return {
            "module_id": module_id,
            "total_artifacts": sum(breakdown.values()),
            "by_type": breakdown,
        }

    def close(self) -> None:
        self._conn.close()


_instance: Optional[SkeletonIndex] = None


def get_skeleton_index(db_path: Optional[Path] = None) -> SkeletonIndex:
    global _instance
    if _instance is None:
        _instance = SkeletonIndex(db_path)
    return _instance
