"""Knowledge base ingestion validator — 4-rule quality gate.

Rules:
1. MinLength — reject chunks with <20 chars of content
2. TreePositionCheck — quarantine chunks without tree_position
3. SimHashDedup — cross-source near-duplicate detection (64-bit SimHash)
4. HierarchyEnrich — backfill function_hierarchy + product_module from CLI graph
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from INAGENT.rag.knowledge_schema import CAT_TO_LEVEL

logger = logging.getLogger(__name__)

_MIN_CONTENT_LENGTH = 20
_SIMHASH_BITS = 64
_SIMHASH_THRESHOLD = 3


def _simhash(text: str, bits: int = _SIMHASH_BITS) -> int:
    tokens = text.lower().split()
    if not tokens:
        return 0
    v = [0] * bits
    for token in tokens:
        h = hash(token) & ((1 << bits) - 1)
        for i in range(bits):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1
    return sum(1 << i for i in range(bits) if v[i] > 0)


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class IngestReport:
    input_count: int = 0
    accepted: int = 0
    rejected_short: int = 0
    rejected_category: int = 0
    rejected_excluded: int = 0
    quarantined: int = 0
    duplicates: int = 0
    tree_linked: int = 0
    hierarchy_backfilled: int = 0
    module_inferred: int = 0
    tree_attached: int = 0
    category_rescued: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_count": self.input_count,
            "accepted": self.accepted,
            "rejected_short": self.rejected_short,
            "rejected_category": self.rejected_category,
            "rejected_excluded": self.rejected_excluded,
            "quarantined": self.quarantined,
            "duplicates": self.duplicates,
            "tree_linked": self.tree_linked,
            "hierarchy_backfilled": self.hierarchy_backfilled,
            "module_inferred": self.module_inferred,
            "tree_attached": self.tree_attached,
            "category_rescued": self.category_rescued,
        }


class IngestValidator:
    """Quality gate for knowledge base chunks during ingestion."""

    def __init__(
        self,
        cli_graph_store=None,
        existing_hashes: Optional[Set[int]] = None,
    ):
        self._cli_graph = cli_graph_store
        self._simhash_set: Set[int] = existing_hashes or set()
        self._quarantine: List[Dict[str, Any]] = []
        self._report = IngestReport()
        self._module_keywords: Optional[Dict[str, List[str]]] = None

    def _get_module_keywords(self) -> Dict[str, List[str]]:
        if self._module_keywords is not None:
            return self._module_keywords
        self._module_keywords = {}
        if not self._cli_graph:
            return self._module_keywords
        self._cli_graph._ensure_loaded()
        for mid, node in self._cli_graph._module_nodes.items():
            label = node.get("label", mid).lower()
            keywords = [label, mid.lower()]
            proto = node.get("protocol_stack", [])
            if proto:
                keywords.extend(p.lower() for p in proto)
            tags = node.get("feature_tags", [])
            if tags:
                keywords.extend(t.lower() for t in tags)
            self._module_keywords[mid] = keywords
        return self._module_keywords

    def validate_chunk(self, chunk: dict) -> Tuple[str, str]:
        """Validate a single chunk.

        Returns:
            (verdict, reason) where verdict is 'accept' | 'reject' | 'quarantine' | 'duplicate'
        """
        self._report.input_count += 1
        meta = chunk.get("metadata") or {}
        if "metadata" not in chunk:
            chunk["metadata"] = meta

        content = str(chunk.get("page_content") or chunk.get("text") or "")

        # Rule 0: OwnerExcluded — 农场主明确排除的块
        if meta.get("owner_excluded"):
            self._report.rejected_excluded += 1
            return "reject", "owner_excluded"

        # Rule 1: MinLength
        if len(content.strip()) < _MIN_CONTENT_LENGTH:
            self._report.rejected_short += 1
            return "reject", "short_content"

        # Rule 2: TreePositionCheck — tree_position 必须存在且有效
        tp = meta.get("tree_position")
        if isinstance(tp, dict) and tp.get("tree_level"):
            self._report.tree_linked += 1
        else:
            # 无 tree_position — 尝试用旧字段兼容（document_category → tree_level 映射）
            old_cat = meta.get("document_category", "")
            if old_cat and old_cat != "unknown":
                prefix = old_cat.split("/")[0] if "/" in old_cat else old_cat
                level = CAT_TO_LEVEL.get(prefix, "branch")
                meta["tree_position"] = {
                    "tree_level": level,
                    "linked_nodes": [],
                    "confidence": 0.5,
                    "knowledge_role": f"migrated_from_{old_cat}",
                }
                self._report.tree_linked += 1
            else:
                self._report.quarantined += 1
                self._quarantine.append(chunk)
                return "quarantine", "no_tree_position"

        # Rule 3: SimHash dedup
        h = _simhash(content[:500])
        if h != 0:
            for existing_h in self._simhash_set:
                if _hamming_distance(h, existing_h) <= _SIMHASH_THRESHOLD:
                    self._report.duplicates += 1
                    return "duplicate", "simhash_near_duplicate"
            self._simhash_set.add(h)

        # Rule 4a: infer product_module if missing (before hierarchy)
        if not meta.get("product_module") and self._cli_graph:
            inferred = self._infer_module(content)
            if inferred:
                meta["product_module"] = inferred
                meta["_module_inferred"] = True
                self._report.module_inferred += 1

        # Rule 4b: HierarchyEnrich (needs product_module from 4a)
        if not meta.get("function_hierarchy"):
            module = meta.get("product_module", "")
            if module and self._cli_graph:
                prefix = self._cli_graph.get_hierarchy_prefix(module)
                if prefix:
                    meta["function_hierarchy"] = prefix
                    meta["_hierarchy_backfilled"] = True
                    self._report.hierarchy_backfilled += 1

        # Rule 5: AttachToTree — update feature knowledge_sources
        self._attach_to_tree(meta)

        self._report.accepted += 1
        return "accept", ""

    def _infer_module(self, content: str) -> Optional[str]:
        content_lower = content[:1000].lower()
        mkw = self._get_module_keywords()
        best_mid = ""
        best_score = 0
        for mid, keywords in mkw.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > best_score:
                best_score = score
                best_mid = mid
        if best_score >= 2:
            return best_mid
        return None

    _TREE_LEVEL_TO_SOURCE_KEY = {
        "leaf": "cli",
        "new_leaf": "cli",
        "branch": "app",
        "trunk": "design",
        "root": "design",
    }

    def _attach_to_tree(self, meta: dict) -> None:
        """Rule 5: 将 chunk 挂载到功能树节点，更新 knowledge_sources 计数。"""
        if not self._cli_graph:
            return

        tp = meta.get("tree_position", {})
        tree_level = tp.get("tree_level", "") if isinstance(tp, dict) else ""
        linked_nodes = tp.get("linked_nodes", []) if isinstance(tp, dict) else []

        fh = meta.get("function_hierarchy", "")
        module = meta.get("product_module", "")
        if not fh and not module and not linked_nodes:
            return

        source_key = self._TREE_LEVEL_TO_SOURCE_KEY.get(tree_level, tree_level or "app")

        feature_id = ""

        if linked_nodes:
            for nid in linked_nodes:
                if self._cli_graph._nodes_by_id.get(nid):
                    feature_id = nid
                    break

        if not feature_id and fh:
            fh_lower = fh.lower().replace(" ", "")
            self._cli_graph._ensure_loaded()
            self._cli_graph._build_l2_index()
            for mid in self._cli_graph._module_ids:
                for bid in self._cli_graph.get_branch_ids(mid):
                    bid_tokens = set(bid.lower().split("_"))
                    for t in bid_tokens:
                        if len(t) >= 3 and t in fh_lower:
                            feature_id = bid
                            break
                    if feature_id:
                        break
                if feature_id:
                    break

        if not feature_id and module:
            module_lower = module.lower()
            branches = self._cli_graph.get_branch_ids(module_lower)
            if not branches:
                for mid in self._cli_graph._module_ids:
                    if mid.lower() == module_lower:
                        branches = self._cli_graph.get_branch_ids(mid)
                        break
            if len(branches) == 1:
                feature_id = branches[0]

        if feature_id and source_key:
            try:
                from INAGENT.rag.skeleton_index import get_skeleton_index
                si = get_skeleton_index()
                si.update_knowledge_sources(feature_id, source_key)
                meta["_tree_feature_id"] = feature_id
                self._report.tree_attached += 1
            except Exception:
                pass

    def validate_batch(self, chunks: List[dict]) -> List[dict]:
        """Validate a batch of chunks, returning only accepted ones."""
        accepted = []
        for chunk in chunks:
            verdict, reason = self.validate_chunk(chunk)
            if verdict == "accept":
                accepted.append(chunk)
        return accepted

    @property
    def report(self) -> IngestReport:
        return self._report

    @property
    def quarantine(self) -> List[dict]:
        return self._quarantine

    def save_report(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)

        report_path = output_dir / "_ingest_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(self._report.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info("Ingest report saved: %s", report_path)

        if self._quarantine:
            q_path = output_dir / "_quarantine.jsonl"
            with open(q_path, "w", encoding="utf-8") as f:
                for chunk in self._quarantine:
                    f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            logger.info("Quarantine file saved: %d chunks -> %s", len(self._quarantine), q_path)
