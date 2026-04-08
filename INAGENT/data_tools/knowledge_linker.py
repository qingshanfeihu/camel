# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
知识链接器：农民结构匹配层

农民（farmer_link）：结构规则匹配，将文档块挂到已有命令树节点
无法匹配的块产出 SchemaGapEntry，由调用方传递给农场主
(KnowledgeFarmOwnerAgent.process_gap_entries) 做结构裁决。

宪章对齐：
  - 农民只做结构验证（command_exists, module 解析），不做语义决策
  - 纯关键词匹配不构成结构证据，仅作为 gap 线索提供给农场主
  - 农场主通过 TreeInformed Decision Engine 裁决，禁止硬编码阈值

树位置模型：
  leaf     — 补充已有命令的属性槽
  new_leaf — 新发现的命令
  branch   — 多命令组合功能
  trunk    — 跨功能知识（协议栈、转发面）
  root     — 底层架构（硬件、平台）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TREE_LEVELS = ("leaf", "new_leaf", "branch", "trunk", "root")


@dataclass
class TreePosition:
    tree_level: str  # leaf / new_leaf / branch / trunk / root
    linked_nodes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    knowledge_role: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TreePosition":
        return cls(
            tree_level=d.get("tree_level", ""),
            linked_nodes=d.get("linked_nodes", []),
            confidence=d.get("confidence", 0.0),
            knowledge_role=d.get("knowledge_role", ""),
        )

    def is_valid(self) -> bool:
        return self.tree_level in TREE_LEVELS and self.confidence > 0


@dataclass
class TreeModification:
    """农场主对树结构的变更指令。"""
    action: str  # add_leaf / add_branch / add_module
    node_id: str = ""
    parent_id: str = ""
    label: str = ""
    metadata: dict = field(default_factory=dict)


# ── 农民匹配 ────────────────────────────────────────────────────────────

def farmer_link(
    block_text: str,
    block_meta: Dict[str, Any],
    cli_graph_store,
) -> Optional[TreePosition]:
    """结构规则匹配：尝试将一个知识块挂到已有命令树节点。

    匹配策略（按优先级，仅结构验证）：
      1. command_prefix → command_exists() → leaf
      2. function_hierarchy → branch/leaf 匹配
      3. section_title → command_exists() 回退（CLI 文档常以命令名作标题）

    纯关键词匹配不构成结构证据，不在此函数范围内。
    无法匹配 → 返回 None → 由 link_blocks 产出 SchemaGapEntry。

    Returns:
        TreePosition if structural match found, None if needs escalation.
    """
    cmd_prefix = block_meta.get("command_prefix", "").strip()
    if cmd_prefix:
        pos = _match_command_leaf(cmd_prefix, block_text, cli_graph_store)
        if pos:
            return pos

    fh = block_meta.get("function_hierarchy", "").strip()
    if fh:
        pos = _match_hierarchy_branch(fh, cli_graph_store)
        if pos:
            return pos

    title = block_meta.get("section_title", "").strip()
    if title:
        pos = _match_title_as_command(title, cli_graph_store)
        if pos:
            return pos

    return None


def _match_command_leaf(
    cmd_prefix: str, block_text: str, cli_graph_store
) -> Optional[TreePosition]:
    """用 command_prefix 精确/模糊匹配命令树叶子节点。"""
    try:
        exists, similar = cli_graph_store.command_exists(cmd_prefix)
    except Exception:
        return None

    if exists:
        return TreePosition(
            tree_level="leaf",
            linked_nodes=similar[:3],
            confidence=0.95,
            knowledge_role="command_attribute",
        )

    if similar:
        return TreePosition(
            tree_level="leaf",
            linked_nodes=similar[:3],
            confidence=0.7,
            knowledge_role="command_attribute_fuzzy",
        )

    return None


def _match_title_as_command(
    section_title: str, cli_graph_store
) -> Optional[TreePosition]:
    """用 section_title 尝试精确命令匹配。

    去掉 CLI 语法标记 ({...}, [...], <...>) 后，用 command_exists 做精确匹配。
    仅精确命中才构成结构证据；模糊候选作为 gap 线索留给农场主 LLM 裁决。
    """
    import re
    cleaned = re.sub(r"[\{<\[][^\}\]>]*[\}\]>]", "", section_title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    try:
        exists, similar = cli_graph_store.command_exists(cleaned)
    except Exception:
        return None

    if exists:
        return TreePosition(
            tree_level="leaf",
            linked_nodes=similar[:3],
            confidence=0.85,
            knowledge_role="title_command_match",
        )

    return None


def _match_hierarchy_branch(
    function_hierarchy: str, cli_graph_store
) -> Optional[TreePosition]:
    """用 function_hierarchy 匹配 branch 级节点。"""
    parts = [p.strip().lower() for p in function_hierarchy.replace(">", "/").split("/") if p.strip()]
    if not parts:
        return None

    try:
        seeds = cli_graph_store._resolve_hint_to_seeds(" ".join(parts))
    except Exception:
        seeds = []

    if not seeds:
        hint = parts[0] if parts else ""
        if hint:
            try:
                seeds = cli_graph_store._resolve_hint_to_seeds(hint)
            except Exception:
                seeds = []

    if seeds:
        seeds = list(seeds)
        module_hits = sum(1 for s in seeds if s in cli_graph_store._module_ids)
        level = "branch" if module_hits > len(seeds) // 2 else "leaf"
        return TreePosition(
            tree_level=level,
            linked_nodes=seeds[:5],
            confidence=0.8,
            knowledge_role="hierarchy_match",
        )
    return None


def _match_by_hierarchy(
    block_meta: Dict[str, Any], cli_graph_store
) -> list[str]:
    """辅助：从 metadata 中提取层级信息去匹配节点（不判断 tree_level）。"""
    fh = block_meta.get("function_hierarchy", "")
    if fh:
        pos = _match_hierarchy_branch(fh, cli_graph_store)
        if pos:
            return pos.linked_nodes

    cmd = block_meta.get("command_prefix", "")
    if cmd:
        pos = _match_command_leaf(cmd, "", cli_graph_store)
        if pos:
            return pos.linked_nodes
    return []


# ── 关键词线索收集（不构成决策，仅为 gap 提供上下文） ───────────────────

def _gather_keyword_candidates(
    block_text: str, block_meta: Dict[str, Any], cli_graph_store
) -> List[str]:
    """从关键词索引中收集候选节点 ID，作为 SchemaGapEntry 的线索。

    不作为农民决策依据，仅供农场主 TreeInformed Engine 参考。
    """
    candidates: list[str] = []

    kws = block_meta.get("required_keywords", [])
    if isinstance(kws, list):
        candidates.extend(k.lower() for k in kws if isinstance(k, str) and len(k) >= 3)

    protocols = block_meta.get("protocol_type", [])
    if isinstance(protocols, list):
        candidates.extend(p.lower() for p in protocols if isinstance(p, str))

    if not candidates and block_text:
        words = block_text[:500].lower().split()
        candidates = [w for w in words if len(w) >= 4][:10]

    if not candidates:
        return []

    ki = getattr(cli_graph_store, "_keywords_index", {})
    if not ki:
        return []

    matched_nodes: list[str] = []
    for kw in candidates:
        node_ids = ki.get(kw, [])
        matched_nodes.extend(node_ids)

    if not matched_nodes:
        return []

    from collections import Counter
    counts = Counter(matched_nodes)
    return [n for n, _ in counts.most_common(5)]


# ── Gap 构建 ─────────────────────────────────────────────────────────────

def _build_gap_entry(
    block: Dict[str, Any],
    block_idx: int,
    cli_graph_store,
):
    """为农民无法结构匹配的块构建 SchemaGapEntry。

    收集两类线索供农场主 TreeInformed Engine 裁决：
      1. 模糊命令候选 — section_title 去掉语法标记后 command_exists 的 similar 列表
      2. 关键词候选 — keywords_index 中匹配的节点
    """
    import re
    from INAGENT.rag.knowledge_schema import SchemaGapEntry

    meta = block.get("metadata", {})
    text = str(block.get("page_content") or block.get("text") or "")

    nearest = []

    title = meta.get("section_title", "").strip()
    if title:
        cleaned = re.sub(r"[\{<\[][^\}\]>]*[\}\]>]", "", title).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            try:
                _, similar = cli_graph_store.command_exists(cleaned)
                nearest.extend({"node_id": s, "match_type": "fuzzy_command"} for s in similar[:3])
            except Exception:
                pass

    kw_candidates = _gather_keyword_candidates(text, meta, cli_graph_store)
    seen = {n["node_id"] for n in nearest}
    nearest.extend(
        {"node_id": n, "match_type": "keyword"} for n in kw_candidates if n not in seen
    )

    gap_type = "ambiguous_match" if nearest else "new_entity"

    return SchemaGapEntry(
        gap_type=gap_type,
        entity_title=title or meta.get("title", ""),
        entity_description=text[:300],
        entity_type=meta.get("document_category", ""),
        evidence=(
            f"farmer structural match failed; "
            f"nearest: {[n['node_id'] for n in nearest[:5]]}"
        ),
        source_file=meta.get("source_file", ""),
        nearest_matches=nearest,
        chunk_content=text[:500],
    )


# ── 批量处理入口 ─────────────────────────────────────────────────────────

def link_blocks(
    blocks: List[Dict[str, Any]],
    cli_graph_store,
) -> Tuple[List[Dict[str, Any]], List, dict]:
    """农民规则匹配：批量将知识块挂到命令树。

    仅做结构验证（command_exists, hierarchy 解析, section_title 命令匹配）。
    无法匹配的块产出 SchemaGapEntry，由调用方传递给农场主处理。

    Args:
        blocks: 知识块列表（会被原地修改，添加 tree_position）
        cli_graph_store: CLI 图实例

    Returns:
        (blocks, gap_entries, stats)
        - gap_entries: List[SchemaGapEntry] — 农民无法匹配的块
        - stats: {"total", "farmer_matched", "escalated"}
    """
    stats = {"total": len(blocks), "farmer_matched": 0, "escalated": 0}
    gap_entries = []

    for idx, block in enumerate(blocks):
        meta = block.get("metadata", {})
        text = str(block.get("page_content") or block.get("text") or "")

        pos = farmer_link(text, meta, cli_graph_store)
        if pos and pos.is_valid():
            meta["tree_position"] = pos.to_dict()
            stats["farmer_matched"] += 1
        else:
            gap = _build_gap_entry(block, idx, cli_graph_store)
            gap_entries.append(gap)
            stats["escalated"] += 1

    logger.info(
        "[linker] farmer match: total=%d, matched=%d, escalated=%d",
        stats["total"], stats["farmer_matched"], stats["escalated"],
    )
    return blocks, gap_entries, stats
