# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
知识链接器：农民-农场主二层架构

农民（farmer_link）：规则匹配，将文档块挂到已有命令树节点
农场主（owner_decide）：LLM 决策，处理农民无法匹配的块，可修改树结构

树位置模型（替代 document_category）：
  leaf     — 补充已有命令的属性槽
  new_leaf — 新发现的命令
  branch   — 多命令组合功能
  trunk    — 跨功能知识（协议栈、转发面）
  root     — 底层架构（硬件、平台）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
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
    *,
    manifest_hint: Optional[str] = None,
) -> Optional[TreePosition]:
    """规则匹配：尝试将一个知识块挂到已有命令树节点。

    匹配策略（按优先级）：
      1. manifest tree_level_hint（用户显式声明）
      2. command_prefix → command_exists() → leaf
      3. function_hierarchy → branch 匹配
      4. 关键词 → keywords_index 匹配

    Returns:
        TreePosition if matched, None if needs escalation.
    """
    if manifest_hint and manifest_hint in TREE_LEVELS:
        nodes = _match_by_hierarchy(block_meta, cli_graph_store)
        return TreePosition(
            tree_level=manifest_hint,
            linked_nodes=nodes or [],
            confidence=0.9,
            knowledge_role="manifest_declared",
        )

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

    kw_pos = _match_keywords(block_text, block_meta, cli_graph_store)
    if kw_pos:
        return kw_pos

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


def _match_keywords(
    block_text: str, block_meta: Dict[str, Any], cli_graph_store
) -> Optional[TreePosition]:
    """用关键词匹配 keywords_index。"""
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
        return None

    ki = getattr(cli_graph_store, "_keywords_index", {})
    if not ki:
        return None

    matched_nodes: list[str] = []
    for kw in candidates:
        node_ids = ki.get(kw, [])
        matched_nodes.extend(node_ids)

    if not matched_nodes:
        return None

    from collections import Counter
    counts = Counter(matched_nodes)
    top_nodes = [n for n, _ in counts.most_common(5)]

    module_hits = sum(1 for n in top_nodes if n in cli_graph_store._module_ids)
    module_ratio = module_hits / len(top_nodes)

    if module_ratio >= 0.6:
        level = "trunk" if module_ratio >= 0.8 else "branch"
    else:
        level = "leaf"

    return TreePosition(
        tree_level=level,
        linked_nodes=top_nodes,
        confidence=0.5,
        knowledge_role="keyword_match",
    )


# ── 农场主决策 ───────────────────────────────────────────────────────────

_OWNER_SYSTEM_PROMPT = """\
你是网络设备知识库的农场主。农民（auto_convert）在处理文档时发现了一些无法自动挂载到命令树上的知识块。

命令树结构说明：
- 树根（root）：产品底层架构、硬件架构
- 躯干（trunk）：跨功能知识，如协议栈、转发面、管理面
- 枝丫（branch）：多个命令组合实现的功能
- 叶子（leaf）：单个命令的属性补充
- 新叶子（new_leaf）：文档中发现的新命令

当前树的模块列表：
{module_list}

你的任务：为每个知识块判断它属于树的哪个层级，以及应该挂载到哪些节点。

对每个块，返回 JSON：
{{
  "block_id": <int>,
  "tree_level": "leaf|new_leaf|branch|trunk|root",
  "linked_nodes": ["node_id1", "node_id2"],
  "confidence": 0.0-1.0,
  "knowledge_role": "简要描述这块知识的角色"
}}

如果这个块确实无法挂载到任何位置（比如完全无关的内容），返回：
{{"block_id": <int>, "tree_level": "", "linked_nodes": [], "confidence": 0, "knowledge_role": "irrelevant"}}

只输出 JSON 数组，不要解释。"""

_OWNER_BLOCK_TEMPLATE = """\
--- Block {block_id} ---
标题: {title}
内容预览（前300字）: {preview}
已有元数据: product_module={product_module}, protocol={protocol}, function_hierarchy={fh}
"""


def owner_decide(
    escalated_blocks: List[Dict[str, Any]],
    cli_graph_store,
    *,
    batch_size: int = 20,
) -> List[Tuple[int, Optional[TreePosition]]]:
    """农场主 LLM 决策：为 escalate 的块确定树位置。

    Args:
        escalated_blocks: block dicts, 必须含 "block_id" 和 "text"/"page_content"
        cli_graph_store: CLI 图实例
        batch_size: 每批发给 LLM 的块数

    Returns:
        (block_id, TreePosition | None) 列表
    """
    if not escalated_blocks:
        return []

    module_list = ", ".join(sorted(cli_graph_store._module_ids))
    system_prompt = _OWNER_SYSTEM_PROMPT.format(module_list=module_list)

    results: List[Tuple[int, Optional[TreePosition]]] = []

    for i in range(0, len(escalated_blocks), batch_size):
        batch = escalated_blocks[i : i + batch_size]
        user_parts = []
        for blk in batch:
            bid = blk.get("block_id", i)
            meta = blk.get("metadata", {})
            text = str(blk.get("page_content") or blk.get("text") or "")
            title = meta.get("section_title", "") or meta.get("title", "")
            user_parts.append(_OWNER_BLOCK_TEMPLATE.format(
                block_id=bid,
                title=title,
                preview=text[:300],
                product_module=meta.get("product_module", ""),
                protocol=meta.get("protocol_type", ""),
                fh=meta.get("function_hierarchy", ""),
            ))

        user_msg = "\n".join(user_parts)
        batch_results = _call_owner_llm(system_prompt, user_msg)

        bid_set = {blk.get("block_id", i + j) for j, blk in enumerate(batch)}
        for item in batch_results:
            bid = item.get("block_id")
            if bid not in bid_set:
                continue
            level = item.get("tree_level", "")
            if level and level in TREE_LEVELS:
                pos = TreePosition(
                    tree_level=level,
                    linked_nodes=item.get("linked_nodes", []),
                    confidence=item.get("confidence", 0.6),
                    knowledge_role=item.get("knowledge_role", ""),
                )
                results.append((bid, pos))
            else:
                results.append((bid, None))

        decided_ids = {r[0] for r in results}
        for blk in batch:
            bid = blk.get("block_id", 0)
            if bid not in decided_ids:
                results.append((bid, None))

    return results


def _call_owner_llm(system_prompt: str, user_msg: str) -> List[dict]:
    """调用 LLM 网关获取农场主决策。"""
    try:
        from INAGENT.utils.llm_config import get_gateway_config
        from openai import OpenAI

        cfg = get_gateway_config()
        api_key = cfg.get("api_key", "")
        base_url = cfg.get("base_url", "")
        model = cfg.get("chat_model", "")
        if not all([api_key, base_url, model]):
            logger.warning("[owner] LLM 网关配置不完整，跳过农场主决策")
            return []

        client = OpenAI(api_key=api_key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=4096,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()

        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return data

    except json.JSONDecodeError as e:
        logger.warning("[owner] LLM 返回非法 JSON: %s", e)
        return []
    except Exception as e:
        logger.warning("[owner] LLM 调用失败: %s", e)
        return []


# ── 批量处理入口 ─────────────────────────────────────────────────────────

def link_blocks(
    blocks: List[Dict[str, Any]],
    cli_graph_store,
    *,
    manifest_hints: Optional[Dict[str, str]] = None,
    enable_owner: bool = True,
) -> Tuple[List[Dict[str, Any]], dict]:
    """批量链接知识块到命令树。

    两阶段：farmer_link → owner_decide (对 escalate 的块)

    Args:
        blocks: 知识块列表（会被原地修改，添加 tree_position）
        cli_graph_store: CLI 图实例
        manifest_hints: {source_file_stem: tree_level_hint}
        enable_owner: 是否启用农场主 LLM 决策

    Returns:
        (blocks, stats) — stats 含 farmer_matched, owner_decided, escalated, total
    """
    manifest_hints = manifest_hints or {}
    stats = {"total": len(blocks), "farmer_matched": 0, "owner_decided": 0, "escalated": 0}
    escalate_list: List[Dict[str, Any]] = []

    for idx, block in enumerate(blocks):
        meta = block.get("metadata", {})
        text = str(block.get("page_content") or block.get("text") or "")

        source = meta.get("source_file", "")
        stem = Path(source).stem.lower() if source else ""
        hint = manifest_hints.get(stem)

        pos = farmer_link(text, meta, cli_graph_store, manifest_hint=hint)
        if pos and pos.is_valid():
            meta["tree_position"] = pos.to_dict()
            stats["farmer_matched"] += 1
        else:
            block["block_id"] = idx
            escalate_list.append(block)

    if escalate_list and enable_owner:
        decisions = owner_decide(escalate_list, cli_graph_store)
        decided_map = {bid: pos for bid, pos in decisions}

        for block in escalate_list:
            bid = block.get("block_id", -1)
            pos = decided_map.get(bid)
            meta = block.get("metadata", {})
            if pos and pos.is_valid():
                meta["tree_position"] = pos.to_dict()
                stats["owner_decided"] += 1
            else:
                stats["escalated"] += 1
    else:
        stats["escalated"] = len(escalate_list)

    logger.info(
        "[linker] 链接完成: total=%d, farmer=%d, owner=%d, escalated=%d",
        stats["total"], stats["farmer_matched"],
        stats["owner_decided"], stats["escalated"],
    )
    return blocks, stats
