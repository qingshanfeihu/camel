from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

# ── Action taxonomy ───────────────────────────────────────────────────────────
# 农场主产出的 FillRequest.action 合法值
OwnerAction = Literal[
    "discard",              # 丢弃，无任何写入
    "tree_create_leaf",     # 新建叶节点（command/artifact）
    "tree_create_branch",   # 新建枝节点（feature/sub-module）
    "merge_into_existing",  # 挂载/合并到已有节点（GraphRAG+Skeleton）
    "attr_tag_update",      # 更新已有节点的 enrich 字段/标签
    "graphrag_col_update",  # 更新 GraphRAG 实体自定义列值
    "skeleton_register",    # 仅在 SkeletonIndex 注册 artifact
    "needs_tree_session",   # 结构变更超出农场主权限，挂起等待树会话 → report.deferred
    "update",               # 兼容旧值（等价 graphrag_col_update）
    "create_slot",          # 兼容旧值（等价 tree_create_leaf）
]

# 各树层级对应的合法 enrich 键（规则驱动 attr_tag_update 时保守验证）
TREE_ENRICH_KEYS: Dict[str, List[str]] = {
    "root": [
        "protocol_stack", "address_family", "layer", "interface_types",
        "related_modules", "feature_tags", "func", "scope",
    ],
    "trunk": [
        "protocol_stack", "address_family", "layer", "interface_types",
        "related_modules", "feature_tags", "func", "scope",
        "trunk_layers", "trunk_planes", "knowledge_sources",
    ],
    "branch": [
        "protocol_stack", "address_family", "layer", "interface_types",
        "related_modules", "feature_tags", "func", "scope",
        "trunk_layers", "trunk_planes", "knowledge_sources",
    ],
    "leaf": [
        "keywords", "help_string", "full_syntax", "parameters",
        "operations", "actual_module",
    ],
}


@dataclass
class FillRequest:
    entity_title: str
    fill_fields: Dict[str, Any] = field(default_factory=dict)
    source_evidence: str = ""
    action: str = "update"
    resolved_value: Dict[str, Any] = field(default_factory=dict)
    target_node_id: str = ""
    new_node_template: Optional[Dict[str, Any]] = None
    # TreeInformed 扩展字段（向后兼容，有默认值）
    tree_level: str = ""                            # "root"|"trunk"|"branch"|"leaf"|"none"
    enrich_fields: Dict[str, Any] = field(default_factory=dict)  # 各层 enrich key
    artifact_id: str = ""                           # skeleton_register 时用
    artifact_link: Optional[Dict[str, Any]] = None  # add_link 参数包


# ── TreeContext ───────────────────────────────────────────────────────────────

@dataclass
class TreeContext:
    """农场主通过 _query_tree_context() 从树侧查询得到的只读上下文。"""
    entity_title: str
    exists_in_tree: bool = False            # 来自 command_exists 第一项；该方法契约为 (bool, list)
    tree_level: str = "unknown"             # "root"|"trunk"|"branch"|"leaf"|"unknown"
    matched_node_id: str = ""              # 精确命中的 node_id
    hierarchy_prefix: str = ""             # "SLB > Load Balancing Group > ..."
    trunk_info: Dict[str, List[str]] = field(default_factory=dict)  # layers/planes
    branch_ids: List[str] = field(default_factory=list)             # depth=1 branches
    parent_candidate_id: str = ""          # 最佳父节点（新建时用）
    similar_commands: List[str] = field(default_factory=list)       # 模糊命中列表
    skeleton_artifact_exists: bool = False # artifact_count(module_id) > 0
    skeleton_module_id: str = ""           # resolve_module() 结果
    enrich_snapshot: Dict[str, Any] = field(default_factory=dict)   # 树上当前 enrich 快照


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
    "ambiguous_match",  # 农民无法区分多个候选节点，交给农场主裁决
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
    ambiguous_candidates: List[Dict[str, Any]] = field(default_factory=list)  # ambiguous_match 时的候选节点列表


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
    deferred: List[FillRequest] = field(default_factory=list)  # needs_tree_session 条目
    errors: List[str] = field(default_factory=list)
