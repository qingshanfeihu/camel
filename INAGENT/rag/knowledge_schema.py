from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


@dataclass
class FillRequest:
    entity_title: str
    fill_fields: Dict[str, Any] = field(default_factory=dict)
    source_evidence: str = ""
    action: str = "update"
    resolved_value: Dict[str, Any] = field(default_factory=dict)
    target_node_id: str = ""
    new_node_template: Optional[Dict[str, Any]] = None


class KnowledgeNodeType(str, Enum):
    RULE = "rule"
    TEST = "test"
    DESIGN = "design"
    CLI = "cli"
    DOC = "doc"


@dataclass
class KnowledgeEntity:
    entity_id: str
    node_type: KnowledgeNodeType
    title: str
    content: str
    document_category: str
    source_ref: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "node_type": self.node_type.value,
            "title": self.title,
            "content": self.content,
            "document_category": self.document_category,
            "source_ref": self.source_ref,
            "metadata": self.metadata or {},
        }


def infer_node_type(document_category: str) -> KnowledgeNodeType:
    cat = (document_category or "").strip().lower()
    if cat.startswith("review/"):
        return KnowledgeNodeType.RULE
    if cat.startswith("test/"):
        return KnowledgeNodeType.TEST
    if cat.startswith("spec/"):
        return KnowledgeNodeType.DESIGN
    if cat.startswith("cli/"):
        return KnowledgeNodeType.CLI
    return KnowledgeNodeType.DOC


def build_entity_id(
    *,
    document_category: str,
    title: str = "",
    content: str = "",
    source_ref: str = "",
) -> str:
    seed = "|".join(
        [
            (document_category or "").strip().lower(),
            (title or "").strip().lower(),
            (source_ref or "").strip().lower(),
            " ".join((content or "").strip().split())[:500].lower(),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    prefix = (document_category or "doc").replace("/", "_")
    return f"{prefix}:{digest}"


# ── Farm-Owner data models ────────────────────────────────────────────────────

SchemaGapKind = Literal[
    "new_entity",
    "new_entity_attribute",
    "conflict",
    "overflow",
]


@dataclass
class SchemaGapEntry:
    gap_type: SchemaGapKind
    entity_title: str = ""
    entity_description: str = ""
    entity_type: str = ""
    field_name: str = ""
    skeleton_value: Any = None
    new_value: Any = None
    column_name: str = ""
    column_dtype: str = "str"
    default_value: Any = None
    evidence: str = ""
    source_file: str = ""
    timestamp: str = ""
    nearest_matches: List[Dict[str, Any]] = field(default_factory=list)
    chunk_content: str = ""


@dataclass
class EntityPatch:
    title: str
    description: str
    entity_type: str = "CONFIGURATION"
    source_id: str = ""


@dataclass
class ColumnSpec:
    name: str
    dtype: str = "str"
    default: Any = None


@dataclass
class FarmOwnerReport:
    snapshot_dir: Optional[Path] = None
    entities_added: int = 0
    columns_added: List[str] = field(default_factory=list)
    entities_reembedded: int = 0
    conflicts_resolved: int = 0
    overflows_handled: int = 0
    fill_requests: List[FillRequest] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
