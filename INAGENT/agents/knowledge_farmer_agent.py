# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
农民 Agent (Knowledge Farmer Agent)

职责：
  1. cultivate_batch：对采购员 accept 的裸 chunk 做结构化；元数据经 `_extract_chunk_metadata(..., skip_llm=True)`（批处理不调 LLM）；MinerU table_body 经 `auto_convert._extract_text_from_block` 与采购管线一致进正文
  2. _match_tree_node 匹配骨架（knowledge_base.json）叶节点；歧义多候选 → ambiguous_match gap；_diff_with_skeleton 逐字段 diff
  3. 冲突/溢出/无匹配 → SchemaGapEntry → 农场主裁决；可选 update_skeleton 写回骨架 page_content/metadata（说明/参数/语法/相关操作）
  4. 执行农场主已产出的 FillRequest：apply_fill_request 批量合并到 reference/*.json（及 kb_path 存在时的骨架 node_id 命中项），不自动 merge、不刷向量
  5. enrich_scenario_nodes：读农场主产出的 scenarios_scaffold，LLM 写 scenarios_synthesized（与 cultivate_batch 批处理路径分离）

结构边界：仅给 **已有** 树/图挂载点补信息与 reference；**新建** 叶/枝/干/根节点、**挖槽/新列** 由农场主裁决（gap / FillRequest），农民不擅自扩张 GraphRAG 结构。见 docs/agents/sessions/02-farmer.md § 与农场主的结构边界。

`auto_convert` 模块中的 **提取函数**（如 `_extract_chunk_metadata`）是农民结构化工具；**原始文档批处理（MinerU 等）** 属采购，入口为 `procurement_ingest.main`。

持久化：
  reference/{stem}.json         —— 新 chunk 按 block_id 去重追加；FillRequest 主要落点
  reference/scenarios_scaffold.json   —— 农场主 cultivate_scenarios（农民只读输入）
  reference/scenarios_synthesized.json —— enrich_scenario_nodes 输出
  knowledge_base.json           —— 骨架；update_skeleton / apply_fill_request（可选）原地更新
  farmer_tree_alias.json       —— 可选：树会话提供的 heading_slug → node_id 映射
  logs/{stem}.farmer_cache.json —— 已处理 block_id 记录
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend
from INAGENT.agents.knowledge_procurement_agent import ChunkDecision
from INAGENT.rag.knowledge_schema import (
    FillRequest,
    SchemaGapEntry,
    build_tree_position,
    infer_knowledge_role,
)
from INAGENT.utils.env_utils import get_product_name, load_inagent_env

logger = logging.getLogger(__name__)

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
_KB_LOGS_DIR = _INAGENT_ROOT / "knowledge_base" / "logs"
_FUNCTION_INDEX_PATH = _INAGENT_ROOT / "knowledge_base" / "function_structure_index.json"
_MINERU_CONFIG_PATH = _INAGENT_ROOT.parent / "mineru.json"

# Detects CLI-style command lines: starts with a word, then a space, then a CLI token
_CLI_COMMAND_LINE_RE = re.compile(
    r"^[a-zA-Z][\w-]+(?: [\w\-<\[\{\"'\.\/])", re.MULTILINE
)

_KB_PATH = _REFERENCE_DIR / "knowledge_base.json"
# Optional slug → node_id map maintained by tree session (see docs/agents/sessions/01-tree.md).
_FARMER_TREE_ALIAS_PATH = _REFERENCE_DIR / "farmer_tree_alias.json"

_CROSS_REF_CMD_RE = re.compile(
    r"(?:命令|command)\s*((?:no\s+)?[a-z][a-z0-9_-]+(?:\s+[a-z][a-z0-9_-]+){1,5})",
    re.IGNORECASE,
)

_SCHEMA_GAP_OVERRIDE_RE = re.compile(
    r"支持覆盖|command\s+override|can\s+be\s+overridden|系统命令覆盖",
    re.IGNORECASE,
)

_SKELETON_META_FIELDS = frozenset({
    "command_prefix", "document_category", "func", "is_variant",
    "node_id", "product_module", "scope", "source_file",
})

_SKIP_DIFF_FIELDS = frozenset({
    "clean_text", "section_title", "parent_section", "section_path",
    "block_id", "word_count", "has_code_block", "tree_node_id",
    "command_refs", "source_file", "chunk_type", "override_commands",
})

_SKELETON_SECTION_RE = re.compile(
    r"^(\[命令\]|\[说明\]|语法:|参数:|适用范围:|相关操作:|内部函数:)",
    re.MULTILINE,
)

_GENERIC_SECTION_TITLES = frozenset({
    "概述", "简介", "说明", "注意事项", "配置说明", "功能介绍", "使用说明",
    "overview", "introduction", "description", "summary", "about",
    "getting started", "prerequisites", "notes",
})


def _extract_labeled_section(text: str, label: str) -> str:
    """Extract body under `label:` line in raw text until the next section label."""
    pat = re.compile(r"^" + re.escape(label) + r"[ \t]*\n", re.MULTILINE)
    m = pat.search(text)
    if not m:
        return ""
    start = m.end()
    nm = _SKELETON_SECTION_RE.search(text, start)
    return text[start : (nm.start() if nm else len(text))].strip()


def _write_skeleton_section_if_richer(pc: str, label: str, body: str) -> str:
    """Replace section `label` in skeleton page_content if `body` is richer.

    'Richer' = more non-whitespace characters. If the section doesn't exist,
    append it. If existing content is already longer, leave it alone.
    """
    lp = re.compile(r"^" + re.escape(label) + r"[ \t]*$", re.MULTILINE)
    m = lp.search(pc)
    new_body = "  " + "\n  ".join(body.splitlines())
    if not m:
        return pc.rstrip() + f"\n{label}\n{new_body}"
    after = pc[m.end():]
    nm = _SKELETON_SECTION_RE.search(after)
    existing = after[: nm.start()].strip() if nm else after.strip()
    if len(existing.replace(" ", "")) >= len(body.replace(" ", "")):
        return pc
    end = m.end() + (nm.start() if nm else len(after))
    return pc[: m.end()] + "\n" + new_body + "\n" + pc[end:]


def _append_skeleton_section_entries(pc: str, label: str, entries: List[str]) -> str:
    """Append entries to section `label` in page_content, creating if absent."""
    formatted = "\n  ".join(str(e) for e in entries)
    lp = re.compile(r"^" + re.escape(label) + r"[ \t]*$", re.MULTILINE)
    m = lp.search(pc)
    if not m:
        return pc.rstrip() + f"\n{label}\n  {formatted}"
    after = pc[m.end():]
    nm = _SKELETON_SECTION_RE.search(after)
    body_end = m.end() + (nm.start() if nm else len(after))
    return pc[:body_end].rstrip() + f"\n  {formatted}\n" + pc[body_end:]


def _normalize_heading_to_node_slug(text: str) -> str:
    """Map a human heading / CLI line toward knowledge_base node_id shape."""
    s = " ".join((text or "").strip().lower().split())
    if not s:
        return ""
    out: List[str] = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "_", "-"):
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


def _extract_cli_command_from_content(content: str) -> str:
    """Extracts a full CLI command verb from content (tokens before first parameter marker).

    Looks for the first line that:
      - starts with a lowercase ASCII letter
      - contains no CJK characters
      - contains at least one parameter marker: '<', '[', or '{'

    Returns the whitespace-stripped tokens before the first marker, joined by spaces.
    Requires at least 2 tokens. Returns empty string if no match.

    Example: "ipv6 ospf advertise <interface_name> <filter_prefix>" -> "ipv6 ospf advertise"
    """
    _CJK_RANGE = range(0x4E00, 0xA000)
    _PARAM_MARKS = frozenset("<[{")
    for line in (content or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if not (s[0].isalpha() and s[0].islower() and s[0].isascii()):
            continue
        if any(ord(c) in _CJK_RANGE for c in s):
            continue
        mark_pos = min(
            (s.find(m) for m in _PARAM_MARKS if m in s),
            default=-1,
        )
        if mark_pos < 0:
            continue
        verb = s[:mark_pos].strip()
        tokens = verb.split()
        if len(tokens) >= 2:
            return " ".join(tok.lower() for tok in tokens)
    return ""


def _extract_cli_prefix_strings(content: str) -> List[str]:
    """Prefer CLI-looking lines for kb_index prefix matching (avoid Chinese lead-in)."""
    ordered: List[str] = []
    seen: set = set()

    def add(s: str) -> None:
        t = s.strip()
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)

    for line in (content or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "<" in s or "[" in s:
            add(s)
    for m in _CLI_COMMAND_LINE_RE.finditer(content or ""):
        line_start = (content or "").rfind("\n", 0, m.start()) + 1
        line_end = (content or "").find("\n", m.end())
        if line_end == -1:
            line_end = len(content or "")
        add((content or "")[line_start:line_end])
    add((content or "").strip())
    return ordered


def _longest_prefix_match_node_id(
    index: Dict[str, str], text_lower: str
) -> Optional[str]:
    """Return node_id for unique longest matching command_prefix key; else None if ambiguous."""
    if not index or not text_lower:
        return None
    tl = text_lower.strip().lower()
    best: List[Tuple[int, str, str]] = []
    for key, nid in index.items():
        kl = key.lower()
        if tl.startswith(kl):
            best.append((len(key), key, nid))
    if not best:
        return None
    max_len = max(b[0] for b in best)
    top = [b for b in best if b[0] == max_len]
    node_ids = {b[2] for b in top}
    if len(node_ids) > 1:
        return None
    return top[0][2]


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FarmResult:
    chunk: Dict
    source_file: str
    block_id: str
    enriched_fields: List[str] = field(default_factory=list)
    schema_gaps: List[SchemaGapEntry] = field(default_factory=list)
    matched_node_id: str = ""


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_mineru_config() -> Dict:
    if _MINERU_CONFIG_PATH.exists():
        try:
            return json.loads(_MINERU_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _get_metadata_rules() -> Dict:
    return _load_mineru_config().get("metadata_rules", {})


# ── Main class ────────────────────────────────────────────────────────────────

class KnowledgeFarmerAgent:
    """
    农民：调 auto_convert 结构化 → 匹配骨架 → diff → 更新/上报 gap。

    用法::

        farmer = KnowledgeFarmerAgent(model=model)
        results = farmer.cultivate_batch(decisions)
        farmer.write_to_reference(results)
    """

    def __init__(
        self,
        model: Optional[BaseModelBackend] = None,
        product_name: str = "NSAE (InfosecOS) 负载均衡器",
        _chat_agent=None,
    ):
        load_inagent_env()
        self._chat_agent = _chat_agent
        if self._chat_agent is None and model is not None:
            self._chat_agent = ChatAgent(
                system_message=BaseMessage.make_assistant_message(
                    role_name="Metadata Extractor",
                    content=(
                        f"You are a precise metadata extractor for {product_name} "
                        "technical documentation. Always return valid JSON arrays."
                    ),
                ),
                model=model,
            )
        self._function_index: Optional[Dict] = self._load_function_index()
        self._kb_index: Optional[Dict[str, str]] = None
        self._skeleton: Optional[Dict[str, Dict]] = None
        self._tree_alias_map: Optional[Dict[str, str]] = None

    # ── public ────────────────────────────────────────────────────────────────

    def cultivate_batch(self, decisions: List[ChunkDecision]) -> List[FarmResult]:
        """Align procurement chunks against skeleton + detect gaps.

        Flow:
          1. auto_convert 结构化裸 chunk metadata
          2. 匹配骨架节点 (knowledge_base.json)
          3. diff metadata → updates / conflict gaps / overflow gaps
          4. 无匹配 → overflow gap with nearest matches
        """
        accepted = [d for d in decisions if d.decision.action == "accept"]
        if not accepted:
            return []

        raw_chunks = [cd.chunk for cd in accepted]
        ac_metas = self._structure_via_auto_convert(raw_chunks)
        skeleton = self._load_skeleton()

        results: List[FarmResult] = []
        for i, cd in enumerate(accepted):
            chunk = {**cd.chunk}
            meta = {**chunk.get("metadata", {})}
            ac_meta = ac_metas[i]

            # Step 1: rule-based fields
            step1 = self._step1_rules(chunk, cd.chunk_index)
            meta.update(step1)
            enriched = list(step1.keys())

            content = (chunk.get("page_content") or chunk.get("text") or "")

            from INAGENT.utils.chunk_text_quality import detect_quality_flags
            _qflags = detect_quality_flags(content, str(meta.get("section_title", "")))
            if _qflags:
                meta["_quality_flags"] = _qflags

            self._refine_ac_meta_command_prefix(content, ac_meta)

            if not meta.get("command_prefix") and ac_meta.get("command_prefix"):
                meta["command_prefix"] = str(ac_meta["command_prefix"])
                if "command_prefix" not in enriched:
                    enriched.append("command_prefix")
            elif meta.get("command_prefix") and ac_meta.get("command_prefix"):
                mcp = str(meta["command_prefix"])
                acp = str(ac_meta["command_prefix"])
                if len(acp) > len(mcp) and acp.lower().startswith(mcp.lower()):
                    meta["command_prefix"] = acp
                    if "command_prefix" not in enriched:
                        enriched.append("command_prefix")

            # Tree node matching (ac_meta carries section_title / LLM tree hints)
            tree_node_id = self._match_tree_node(content, meta, ac_meta)
            ambiguous_candidates = list(getattr(self, "_last_ambiguous_candidates", []))
            if tree_node_id:
                meta["tree_node_id"] = tree_node_id
                enriched.append("tree_node_id")
                # Bug-A fix: 匹配成功时立即构建 tree_position，
                # 从骨架节点推导 tree_level，保留 linked_nodes 关联
                skel_node = skeleton.get(tree_node_id, {})
                skel_meta = skel_node.get("metadata", {})
                skel_tp = skel_meta.get("tree_position", {})
                skel_level = (
                    skel_tp.get("tree_level", "")
                    if isinstance(skel_tp, dict) else ""
                ) or skel_meta.get("tree_level", "leaf")
                meta["tree_position"] = build_tree_position(
                    tree_level=skel_level,
                    linked_nodes=[tree_node_id],
                    confidence=0.9,
                    knowledge_role=infer_knowledge_role(skel_level),
                )
                if "tree_position" not in enriched:
                    enriched.append("tree_position")
                index = self._load_kb_index()
                if index:
                    keys_for_node = [k for k, n in index.items() if n == tree_node_id]
                    if keys_for_node:
                        best_cp = max(keys_for_node, key=len)
                        cur_cp = str(meta.get("command_prefix") or "")
                        if cur_cp.lower() not in {k.lower() for k in keys_for_node}:
                            meta["command_prefix"] = best_cp
                            if "command_prefix" not in enriched:
                                enriched.append("command_prefix")

            # Cross-reference detection
            cross_refs = self._detect_cross_refs(content, meta.get("command_prefix"))
            if cross_refs:
                meta["command_refs"] = cross_refs
                enriched.append("command_refs")

            # Skeleton diff
            gaps: List[SchemaGapEntry] = []
            matched_node_id = ""
            skeleton_node = skeleton.get(tree_node_id) if tree_node_id else None

            if skeleton_node:
                matched_node_id = tree_node_id or ""
                updates, diff_gaps = self._diff_with_skeleton(
                    ac_meta, content, skeleton_node,
                )
                for k, v in updates.items():
                    meta[k] = v
                    if k not in enriched:
                        enriched.append(k)
                gaps.extend(diff_gaps)

                # Shadow new_entity: command_prefix 比匹配到的 CT 节点更具体（子命令），
                # 且正文含参数形态 → 上报 new_entity，由农场主裁决是否新建叶/关系。
                # 宪章：农民不预写 tree_level / tree_position（扩树归农场主）。
                _sne_cp = str(meta.get("command_prefix") or "").strip()
                _sne_tokens = _sne_cp.split()
                if (
                    matched_node_id
                    and len(_sne_tokens) >= 2
                    and all(t.isascii() for t in _sne_tokens)
                    and not any(g.gap_type == "new_entity" for g in gaps)
                ):
                    _sne_idx = self._load_kb_index()
                    _node_keys = {
                        k.lower() for k, nid in _sne_idx.items()
                        if nid == matched_node_id
                    }
                    _sne_cp_lower = _sne_cp.lower()
                    if (
                        _sne_cp_lower not in _node_keys
                        and any(
                            _sne_cp_lower.startswith(k + " ") for k in _node_keys
                        )
                        and re.search(r'[<\[{]', content)
                    ):
                        gaps.append(SchemaGapEntry(
                            gap_type="new_entity",
                            entity_title=_sne_cp,
                            entity_type="COMMAND",
                            entity_description=str(
                                ac_meta.get("description") or meta.get("description", "")
                            ),
                            evidence=content[:200],
                            source_file=cd.source_file,
                            timestamp=datetime.now().isoformat(),
                            chunk_content=content[:500],
                            chunk_block_id=str(meta.get("block_id", "")),
                        ))
            else:
                for fname in ("product_module", "protocol_type", "intent",
                              "config_mode", "description"):
                    val = ac_meta.get(fname)
                    if val and val not in ("unknown", "") and not meta.get(fname):
                        meta[fname] = val
                        if fname not in enriched:
                            enriched.append(fname)

                cmd_prefix = (
                    ac_meta.get("command_prefix") or meta.get("command_prefix", "")
                )
                if ambiguous_candidates:
                    # 歧义匹配：多个候选节点，交给农场主裁决
                    gaps.append(SchemaGapEntry(
                        gap_type="ambiguous_match",
                        entity_title=str(cmd_prefix or "?"),
                        field_name="tree_node_id",
                        entity_description=str(ac_meta.get("description", "")),
                        evidence=content[:200],
                        source_file=cd.source_file,
                        timestamp=datetime.now().isoformat(),
                        chunk_content=content[:500],
                        ambiguous_candidates=ambiguous_candidates,
                        chunk_block_id=str(meta.get("block_id", "")),
                    ))
                elif cmd_prefix:
                    _block_id = meta.get("block_id", "")
                    _full_cmd = _extract_cli_command_from_content(content)
                    if (
                        _full_cmd
                        and len(_full_cmd) > len(str(cmd_prefix))
                        and _full_cmd.startswith(str(cmd_prefix).lower())
                    ):
                        cmd_prefix = _full_cmd
                        meta["command_prefix"] = _full_cmd
                        if "command_prefix" not in enriched:
                            enriched.append("command_prefix")
                    _cmd_tokens = str(cmd_prefix).strip().split()
                    _all_ascii = all(t.isascii() for t in _cmd_tokens)
                    if len(_cmd_tokens) >= 2 and _all_ascii:
                        gaps.append(SchemaGapEntry(
                            gap_type="new_entity",
                            entity_title=str(cmd_prefix),
                            entity_type="COMMAND",
                            entity_description=str(ac_meta.get("description", "")),
                            evidence=content[:200],
                            source_file=cd.source_file,
                            timestamp=datetime.now().isoformat(),
                            chunk_content=content[:500],
                            chunk_block_id=_block_id,
                        ))
                    else:
                        gaps.append(SchemaGapEntry(
                            gap_type="overflow",
                            entity_title=str(cmd_prefix),
                            field_name="unmatched_chunk",
                            entity_description=str(ac_meta.get("description", "")),
                            evidence=content[:200],
                            source_file=cd.source_file,
                            timestamp=datetime.now().isoformat(),
                            chunk_content=content[:500],
                            chunk_block_id=str(meta.get("block_id", "")),
                            nearest_matches=self._find_nearest_skeleton_matches(
                                content, skeleton,
                            ),
                        ))
                else:
                    # 非 CLI 文档且无 command_prefix — 用 section_title 生成 overflow gap
                    _title = str(meta.get("section_title") or meta.get("product_module") or "?")
                    if _title and _title != "?" and content.strip():
                        gaps.append(SchemaGapEntry(
                            gap_type="overflow",
                            entity_title=_title,
                            field_name="unmatched_non_cli",
                            entity_description=str(ac_meta.get("description", "")),
                            evidence=content[:200],
                            source_file=cd.source_file,
                            timestamp=datetime.now().isoformat(),
                            chunk_content=content[:500],
                            chunk_block_id=str(meta.get("block_id", "")),
                            nearest_matches=self._find_nearest_skeleton_matches(
                                content, skeleton,
                            ),
                        ))

            # Schema gap detection (supports_override etc.)
            gaps.extend(self._detect_schema_gaps(content, meta, ac_meta))

            # Step 3: function-index fields
            step3 = self._step3_index(meta)
            meta.update(step3)
            for k in step3:
                if k not in enriched:
                    enriched.append(k)

            section_title = meta.get("section_title", "").strip()
            if section_title.lower() in _GENERIC_SECTION_TITLES:
                spath = meta.get("section_path", "")
                parts = [p.strip() for p in spath.split(">") if p.strip()] if spath else []
                parent = ""
                for p in reversed(parts):
                    if p.lower() not in _GENERIC_SECTION_TITLES:
                        parent = p
                        break
                if not parent:
                    parent = meta.get("product_module", "")
                if parent:
                    meta["enhanced_title"] = f"{parent} - {section_title}"
                    content = f"[{parent}] {section_title}\n{content}"
                    chunk["page_content"] = content
                    if "enhanced_title" not in enriched:
                        enriched.append("enhanced_title")

            chunk["metadata"] = meta
            results.append(FarmResult(
                chunk=chunk,
                source_file=cd.source_file,
                block_id=meta["block_id"],
                enriched_fields=enriched,
                schema_gaps=gaps,
                matched_node_id=matched_node_id,
            ))

        return results

    def enrich_scenario_nodes(
        self,
        *,
        force_reenrich: bool = False,
        refresh_hybrid_vectors: bool = False,
        hybrid_vectors_force: bool = True,
    ) -> Dict[str, Any]:
        """为农场主创建的枝干骨架节点填充内容（农民加肉）。

        读取 scenarios_scaffold.json（由农场主 cultivate_scenarios 生成），
        对每个 _scaffold=True 的骨架节点，用 LLM 生成完整的场景描述，
        写入 scenarios_synthesized.json（document_category='scenario/guide'）。

        Args:
            force_reenrich: 是否重新处理已有 synthesized 条目（默认跳过）。
            refresh_hybrid_vectors: 写入后是否刷新 Qdrant/BM25。
            hybrid_vectors_force: 刷新时是否强制重嵌。

        Returns:
            {"total": int, "enriched": int, "skipped": int, "errors": List[str]}
        """
        _ref_dir = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
        _scaffold_path = _ref_dir / "scenarios_scaffold.json"
        _out_path = _ref_dir / "scenarios_synthesized.json"

        summary: Dict[str, Any] = {
            "total": 0,
            "enriched": 0,
            "skipped": 0,
            "errors": [],
        }

        if not _scaffold_path.exists():
            summary["errors"].append(
                f"骨架文件不存在，请先运行农场主 cultivate_scenarios(): {_scaffold_path}"
            )
            return summary

        scaffolds: List[Dict] = json.loads(_scaffold_path.read_text(encoding="utf-8"))
        scaffolds = [s for s in scaffolds if isinstance(s, dict)]
        summary["total"] = len(scaffolds)

        existing_synth: List[Dict] = []
        if _out_path.exists():
            try:
                existing_synth = json.loads(_out_path.read_text(encoding="utf-8"))
                if not isinstance(existing_synth, list):
                    existing_synth = []
            except Exception:
                existing_synth = []

        existing_fids: set = {
            e.get("metadata", {}).get("tree_node_id", "") for e in existing_synth
        }
        existing_fids.discard("")

        new_docs: List[Dict] = []

        for scaffold in scaffolds:
            m = scaffold.get("metadata", {})
            fid = m.get("tree_node_id", "")
            if not fid:
                continue
            if not force_reenrich and fid in existing_fids:
                summary["skipped"] += 1
                continue

            logger.info("enrich_scenario_nodes: 填充场景 %s", fid)
            try:
                enriched_doc = self._enrich_one_scaffold(scaffold, fid)
                new_docs.append(enriched_doc)
                summary["enriched"] += 1
            except Exception as exc:
                msg = f"填充失败 {fid}: {exc}"
                logger.warning(msg)
                summary["errors"].append(msg)

        if new_docs:
            if force_reenrich:
                new_fids = {d["metadata"]["tree_node_id"] for d in new_docs}
                merged = [e for e in existing_synth if e.get("metadata", {}).get("tree_node_id", "") not in new_fids]
            else:
                merged = list(existing_synth)
            merged.extend(new_docs)
            _out_path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(
                "enrich_scenario_nodes: 写入 %s (%d 条，新增 %d)",
                _out_path.name, len(merged), len(new_docs),
            )

        if refresh_hybrid_vectors and (new_docs or summary["skipped"] > 0):
            try:
                from INAGENT.data_tools.merge_knowledge_base import (
                    merge_knowledge_base,
                )
                merge_knowledge_base(_ref_dir, _ref_dir / "knowledge_base.json")
            except Exception as exc:
                summary["errors"].append(f"merge_knowledge_base 失败: {exc}")
            try:
                from INAGENT.workflow_config_generator import (
                    refresh_hybrid_vector_index,
                )
                refresh_hybrid_vector_index(force=hybrid_vectors_force)
            except Exception as exc:
                summary["errors"].append(f"向量索引刷新失败: {exc}")

        logger.info(
            "enrich_scenario_nodes 完成: total=%d enriched=%d skipped=%d errors=%d",
            summary["total"], summary["enriched"], summary["skipped"], len(summary["errors"]),
        )
        return summary

    def _enrich_one_scaffold(self, scaffold: Dict, feature_id: str) -> Dict:
        """对单个骨架节点用 LLM 加肉，返回 scenario/guide 或 module/guide 条目。"""
        m = scaffold.get("metadata", {})
        fh = m.get("function_hierarchy", "")
        module = m.get("product_module", "")
        node_level = m.get("_node_level", "branch")
        section_title = m.get("section_title", feature_id.replace("_", " ") + " 功能配置指南")
        cmd_prefixes = [c.strip() for c in m.get("scenario_command_prefixes", "").split("|") if c.strip()]
        node_ids = m.get("scenario_commands", "")
        product = get_product_name()

        # 农场主写的 HyDE 规格作为农民的写作说明
        hyde_query = m.get("_hyde_query", "")
        hyde_hints = m.get("_hyde_coverage_hints", "")
        required_fields = m.get("_required_fields", "")

        page_content = self._llm_enrich_scenario(
            feature_id=feature_id,
            cmd_prefixes=cmd_prefixes,
            function_hierarchy=fh,
            product=product,
            hyde_query=hyde_query,
            hyde_hints=hyde_hints,
            required_fields=required_fields,
            node_level=node_level,
        )

        out_category = {
            "root": "product/guide",
            "trunk": "module/guide",
        }.get(node_level, "scenario/guide")

        return {
            "page_content": page_content,
            "metadata": {
                "document_category": out_category,
                "tree_node_id": feature_id,
                "_tree_feature_id": feature_id,
                "_node_level": node_level,
                "section_title": section_title,
                "function_hierarchy": fh,
                "product_module": module,
                "source_file": "synthesized",
                "word_count": str(len(page_content)),
                "scenario_commands": node_ids,
                "_synthesized": "True",
            },
        }

    def _llm_enrich_scenario(
        self,
        feature_id: str,
        cmd_prefixes: List[str],
        function_hierarchy: str,
        product: str,
        *,
        hyde_query: str = "",
        hyde_hints: str = "",
        required_fields: str = "",
        node_level: str = "branch",
    ) -> str:
        """调用农民自己的 ChatAgent 填充场景文档内容。

        如果农场主提供了 HyDE 规格（hyde_query + hyde_hints），
        则以此作为写作方向约束，确保内容可被目标查询命中。
        """
        if node_level == "root":
            return self._llm_enrich_root(feature_id=feature_id, product=product,
                                           hyde_query=hyde_query, hyde_hints=hyde_hints,
                                           required_fields=required_fields)
        if node_level == "trunk":
            return self._llm_enrich_trunk(feature_id=feature_id, scaffold_meta=m,
                                            product=product, hyde_query=hyde_query,
                                            hyde_hints=hyde_hints, required_fields=required_fields)

        if not cmd_prefixes:
            return f"[{feature_id}] 功能场景，命令信息不完整。"

        fid_label = feature_id.replace("_", " ")

        kb_path = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference" / "knowledge_base.json"
        cmd_descs: List[str] = []
        if kb_path.exists():
            try:
                kb = json.loads(kb_path.read_text(encoding="utf-8"))
                cp_to_desc: Dict[str, str] = {}
                for item in kb:
                    meta = item.get("metadata", {})
                    cp = str(meta.get("command_prefix", "")).lower()
                    desc = meta.get("_enriched_description", "") or item.get("page_content", "")[:80]
                    if cp and desc and cp not in cp_to_desc:
                        cp_to_desc[cp] = desc
                for cp in cmd_prefixes:
                    desc = cp_to_desc.get(cp.lower(), "")
                    cmd_descs.append(f"  - `{cp}`: {desc}" if desc else f"  - `{cp}`")
            except Exception:
                cmd_descs = [f"  - `{cp}`" for cp in cmd_prefixes]
        else:
            cmd_descs = [f"  - `{cp}`" for cp in cmd_prefixes]

        cmds_text = "\n".join(cmd_descs)

        # 农场主 HyDE 规格插入（如有）
        hyde_section = ""
        if hyde_query:
            hyde_section += f"\n农场主规格（必须覆盖）:\n  目标查询: {hyde_query}"
        if hyde_hints:
            hyde_section += f"\n  内容要点: {hyde_hints}"
        if required_fields:
            hyde_section += f"\n  必须包含的字段: {required_fields}"

        prompt = (
            f"你是 {product} 网络设备知识库的文档撰写专家。\n"
            f"请为以下功能组生成一份完整的场景配置指南，供 RAG 检索系统使用。\n\n"
            f"功能 ID: {feature_id}\n"
            f"功能层级: {function_hierarchy}\n"
            f"包含的 CLI 命令（共 {len(cmd_prefixes)} 条）:\n{cmds_text}"
            f"{hyde_section}\n\n"
            f"要求：\n"
            f"1. 用中文撰写，面向网络工程师\n"
            f"2. 包含：功能简介、适用场景、配置步骤（按命令使用顺序）、注意事项\n"
            f"3. 在配置步骤中自然地引用每条命令的作用\n"
            f"4. 若有「目标查询」，确保文档内容能回答该查询\n"
            f"5. 直接输出纯文本，不要代码块标记或 JSON\n"
            f"6. 长度 200-500 字"
        )

        if self._chat_agent is not None:
            try:
                user_msg = BaseMessage.make_user_message(role_name="user", content=prompt)
                resp = self._chat_agent.step(user_msg)
                content = resp.msgs[0].content if resp.msgs else ""
                if content.strip():
                    return content.strip()
            except Exception as exc:
                logger.warning("LLM 填充失败 (%s): %s，回退内联模板", feature_id, exc)

        fh_label = function_hierarchy or fid_label
        lines = [
            f"【{fid_label} 功能配置指南】",
            "",
            f"产品：{product}  功能层级：{fh_label}",
            "",
            f"本功能由以下 {len(cmd_prefixes)} 条命令组成：",
        ]
        for i, cp in enumerate(cmd_prefixes, 1):
            lines.append(f"{i}. {cp}")
        lines += [
            "",
            "配置时请按上述顺序执行，配置完成后使用 show 命令验证。",
        ]
        return "\n".join(lines)

    def _llm_enrich_trunk(
        self,
        feature_id: str,
        scaffold_meta: Dict,
        product: str,
        *,
        hyde_query: str = "",
        hyde_hints: str = "",
        required_fields: str = "",
    ) -> str:
        """为 trunk(模块) 层骨架生成概述文档。"""
        trunk_label = scaffold_meta.get("trunk_label", feature_id)
        child_fids = [f.strip() for f in scaffold_meta.get("child_feature_ids", "").split(",") if f.strip()]

        hyde_section = ""
        if hyde_query:
            hyde_section += f"\n目标查询: {hyde_query}"
        if hyde_hints:
            hyde_section += f"\n内容要点: {hyde_hints}"

        prompt = (
            f"你是 {product} 知识库文档专家。\n"
            f"请为以下功能模块生成概述文档，供 RAG 检索系统使用。\n\n"
            f"模块名: {trunk_label}\n"
            f"子功能: {', '.join(child_fids[:10])}\n"
            f"{hyde_section}\n\n"
            f"要求：\n"
            f"1. 用中文，面向网络工程师\n"
            f"2. 包含：模块简介、子功能目录及适用场景、典型使用顺序\n"
            f"3. 若有「目标查询」，确保文档能回答它\n"
            f"4. 直接输出纯文本，150-350 字"
        )

        if self._chat_agent is not None:
            try:
                user_msg = BaseMessage.make_user_message(role_name="user", content=prompt)
                resp = self._chat_agent.step(user_msg)
                content = resp.msgs[0].content if resp.msgs else ""
                if content.strip():
                    return content.strip()
            except Exception as exc:
                logger.warning("LLM trunk 填充失败 (%s): %s，回退模板", feature_id, exc)

        lines = [f"【{trunk_label} 模块概述】", "", f"产品：{product}", ""]
        if child_fids:
            lines += ["子功能列表："] + [f"- {f}" for f in child_fids] + [""]
        lines.append("请参阅各子功能文档获取详细配置步骤。")
        return "\n".join(lines)

    def _llm_enrich_root(
        self,
        feature_id: str,
        product: str,
        *,
        hyde_query: str = "",
        hyde_hints: str = "",
        required_fields: str = "",
    ) -> str:
        """为 root(产品) 层骨架生成产品全览文档。"""
        hyde_section = ""
        if hyde_query:
            hyde_section += f"\n目标查询: {hyde_query}"
        if hyde_hints:
            hyde_section += f"\n内容要点: {hyde_hints}"

        prompt = (
            f"你是 {product} 产品文档专家。\n"
            f"请生成一份产品功能全览介绍，供 RAG 检索系统使用。\n"
            f"{hyde_section}\n\n"
            f"要求：\n"
            f"1. 面向网络工程师，中文\n"
            f"2. 包含：产品简介、主要功能模块、典型部署场景\n"
            f"3. 若有「目标查询」，确保文档能回答它\n"
            f"4. 直接输出纯文本，150-300 字"
        )

        if self._chat_agent is not None:
            try:
                user_msg = BaseMessage.make_user_message(role_name="user", content=prompt)
                resp = self._chat_agent.step(user_msg)
                content = resp.msgs[0].content if resp.msgs else ""
                if content.strip():
                    return content.strip()
            except Exception as exc:
                logger.warning("LLM root 填充失败 (%s): %s，回退模板", feature_id, exc)

        return f"【{product} 产品简介】\n\n{product} 是一款企业级负载均衡设备，支持多种负载均衡策略和安全防护功能。\n请参阅各功能模块文档获取详细信息。"

    def write_to_reference(
        self,
        results: List[FarmResult],
        ref_dir: Optional[Path] = None,
        log_dir: Optional[Path] = None,
    ) -> Dict[str, int]:
        """Write enriched chunks to reference/{stem}.json with block_id dedup.

        Returns new chunk counts per stem: {"cli": 12, ...}
        """
        ref_dir = ref_dir or _REFERENCE_DIR
        ref_dir.mkdir(parents=True, exist_ok=True)
        log_dir = log_dir or _KB_LOGS_DIR
        log_dir.mkdir(parents=True, exist_ok=True)

        by_stem: Dict[str, List[FarmResult]] = defaultdict(list)
        for r in results:
            stem = Path(r.source_file).stem
            by_stem[stem].append(r)

        counts: Dict[str, int] = {}
        for stem, stem_results in by_stem.items():
            out_path = ref_dir / f"{stem}.json"
            cache_path = log_dir / f"{stem}.farmer_cache.json"

            existing: List[Dict] = []
            existing_ids: set = set()
            existing_hashes: set = set()
            if out_path.exists():
                try:
                    existing = json.loads(out_path.read_text(encoding="utf-8"))
                    existing_ids = {
                        item.get("metadata", {}).get("block_id", "")
                        for item in existing
                    }
                    existing_hashes = {
                        hashlib.md5(
                            (item.get("page_content") or item.get("text") or "").strip().encode()
                        ).hexdigest()
                        for item in existing
                    }
                except Exception:
                    existing, existing_ids, existing_hashes = [], set(), set()

            cache: Dict[str, str] = {}
            if cache_path.exists():
                if not out_path.exists():
                    logger.info("[农民] 清除残留 cache: %s (reference 文件不存在)", cache_path.name)
                    cache_path.unlink()
                else:
                    try:
                        cache = json.loads(cache_path.read_text(encoding="utf-8"))
                    except Exception:
                        cache = {}

            new_chunks: List[Dict] = []
            dup_hash_count = 0
            for r in stem_results:
                if r.block_id in existing_ids or r.block_id in cache:
                    continue
                _pc = (r.chunk.get("page_content") or r.chunk.get("text") or "").strip()
                _ch = hashlib.md5(_pc.encode()).hexdigest()
                if _ch in existing_hashes:
                    dup_hash_count += 1
                    continue
                existing_hashes.add(_ch)
                new_chunks.append(r.chunk)
                cache[r.block_id] = datetime.now().isoformat()

            if dup_hash_count:
                logger.info("[农民] %s: 跳过 %d 条内容重复块", stem, dup_hash_count)

            if new_chunks:
                merged = existing + new_chunks
                out_path.write_text(
                    json.dumps(merged, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    "[农民] %s: +%d 块 (共 %d)", out_path.name, len(new_chunks), len(merged)
                )

            counts[stem] = len(new_chunks)

        return counts

    def emit_schema_gaps(
        self, results: List[FarmResult], gaps_file: Path
    ) -> int:
        all_gaps = [g for r in results for g in r.schema_gaps]
        if not all_gaps:
            return 0
        gaps_file.parent.mkdir(parents=True, exist_ok=True)
        with open(gaps_file, "w", encoding="utf-8") as f:
            for gap in all_gaps:
                f.write(json.dumps({
                    "gap_type": gap.gap_type,
                    "entity_title": gap.entity_title,
                    "entity_description": gap.entity_description,
                    "entity_type": gap.entity_type,
                    "field_name": gap.field_name,
                    "skeleton_value": gap.skeleton_value,
                    "new_value": gap.new_value,
                    "column_name": gap.column_name,
                    "column_dtype": gap.column_dtype,
                    "default_value": gap.default_value,
                    "evidence": gap.evidence,
                    "source_file": gap.source_file,
                    "timestamp": gap.timestamp,
                    "nearest_matches": gap.nearest_matches,
                    "chunk_content": gap.chunk_content,
                    "ambiguous_candidates": gap.ambiguous_candidates,
                }, ensure_ascii=False, default=str) + "\n")
        logger.info("[农民] emit_schema_gaps: %d 条写入 %s", len(all_gaps), gaps_file)
        return len(all_gaps)

    def apply_fill_request(
        self,
        fill_requests: List[FillRequest],
        ref_dir: Optional[Path] = None,
        kb_path: Optional[Path] = None,
    ) -> int:
        """Apply farm-owner ``FillRequest`` rows to reference chunks (and optionally skeleton).

        Batched I/O: reads each JSON file once, applies all matching patches in memory,
        writes once. O(files × items + requests) instead of O(requests × files × items).

        ``create_slot`` requests must be produced by :class:`KnowledgeFarmOwnerAgent`
        (or equivalent orchestration); this method only performs reference I/O.
        """
        ref_dir = ref_dir or _REFERENCE_DIR
        kb_path = kb_path or _KB_PATH
        if not fill_requests:
            return 0

        filled = 0

        by_block_id: Dict[str, Dict[str, Any]] = {}
        by_node_id: Dict[str, Dict[str, Any]] = {}
        kb_by_node_id: Dict[str, Dict[str, Any]] = {}
        create_slots: List[FillRequest] = []

        for req in fill_requests:
            if req.action == "needs_tree_session":
                continue

            has_patch = bool(getattr(req, "chunk_meta_patch", None))
            block_id_target = getattr(req, "target_block_id", "").strip()

            if req.action == "discard":
                if not has_patch:
                    continue
                node_target = (req.target_node_id or req.entity_title or "").strip()
                if block_id_target:
                    by_block_id.setdefault(block_id_target, {}).update(req.chunk_meta_patch)
                elif node_target:
                    by_node_id.setdefault(node_target, {}).update(req.chunk_meta_patch)
                continue

            target = (req.target_node_id or req.entity_title or "").strip()
            if not target and not block_id_target:
                continue

            if req.action == "create_slot" and req.new_node_template:
                create_slots.append(req)
                continue

            merge: Dict = {}
            if req.fill_fields:
                merge.update(req.fill_fields)
            if getattr(req, "enrich_fields", None):
                merge.update(req.enrich_fields)
            if has_patch:
                merge.update(req.chunk_meta_patch)
            if not merge and not block_id_target:
                continue

            if block_id_target:
                by_block_id.setdefault(block_id_target, {}).update(merge)
            if target:
                by_node_id.setdefault(target, {}).update(merge)
                kb_by_node_id.setdefault(target, {}).update(merge)

        for req in create_slots:
            filled += self._apply_create_slot(req, ref_dir)

        if (by_block_id or by_node_id) and ref_dir.exists():
            filled += self._apply_patches_batch(ref_dir, by_block_id, by_node_id)

        kb_file = Path(kb_path) if kb_path else None
        if kb_by_node_id and kb_file and kb_file.exists():
            filled += self._apply_kb_patches_batch(kb_file, kb_by_node_id)

        logger.info(
            "[农民] apply_fill_request: 共更新 %d 条 (block_id_keys=%d, node_id_keys=%d, create_slots=%d)",
            filled, len(by_block_id), len(by_node_id), len(create_slots),
        )
        return filled

    def _apply_patches_batch(
        self,
        ref_dir: Path,
        by_block_id: Dict[str, Dict[str, Any]],
        by_node_id: Dict[str, Dict[str, Any]],
    ) -> int:
        count = 0
        updated_nodes: set = set()
        for json_file in sorted(ref_dir.glob("*.json")):
            if "_bak" in json_file.stem:
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            file_changed = False
            for item in data:
                if not isinstance(item, dict):
                    continue
                meta = item.get("metadata")
                if not isinstance(meta, dict):
                    continue
                merged: Dict[str, Any] = {}
                bid = str(meta.get("block_id", "")).strip()
                if bid and bid in by_block_id:
                    merged.update(by_block_id[bid])
                tid = str(meta.get("tree_node_id") or meta.get("node_id") or "").strip()
                if tid and tid in by_node_id:
                    merged.update(by_node_id[tid])
                    if tid not in updated_nodes:
                        updated_nodes.add(tid)
                if not merged:
                    continue
                for k, v in merged.items():
                    meta[k] = v
                item["metadata"] = meta
                file_changed = True
                count += 1
            if file_changed:
                json_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
                )
        if updated_nodes:
            logger.info("[农民] apply_fill_request: 批量更新 %d 节点", len(updated_nodes))
        logger.info("[农民] apply_fill_request: 批量 patch %d 条记录", count)
        return count

    def _apply_kb_patches_batch(
        self,
        kb_file: Path,
        kb_by_node_id: Dict[str, Dict[str, Any]],
    ) -> int:
        try:
            kb_data = json.loads(kb_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[农民] apply_fill_request: 无法读取骨架 %s (%s)", kb_file, exc)
            return 0
        if not isinstance(kb_data, list):
            return 0
        count = 0
        modified = False
        for item in kb_data:
            if not isinstance(item, dict):
                continue
            m = item.get("metadata")
            if not isinstance(m, dict):
                continue
            nid = str(m.get("node_id") or "").strip()
            if nid not in kb_by_node_id:
                continue
            patch = kb_by_node_id[nid]
            for k, v in patch.items():
                m[k] = v
            item["metadata"] = m
            modified = True
            count += 1
        if modified:
            kb_file.write_text(
                json.dumps(kb_data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            logger.info("[农民] apply_fill_request: 骨架批量更新 %d 节点", count)
        return count

    def _apply_create_slot(self, req: FillRequest, ref_dir: Path) -> int:
        """Append one reference chunk for an existing ``tree_node_id`` (FO decision only)."""
        template = req.new_node_template
        if not isinstance(template, dict):
            return 0
        target = (req.target_node_id or req.entity_title or "").strip()
        if not target:
            return 0

        source_file = template.get("source_file", "overflow_created")
        stem = Path(source_file).stem if source_file else "overflow_created"
        ref_file = ref_dir / f"{stem}.json"

        data: list = []
        if ref_file.exists():
            try:
                data = json.loads(ref_file.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []

        for item in data:
            m = item.get("metadata", {})
            if str(m.get("tree_node_id", "")) == target or str(m.get("node_id", "")) == target:
                logger.info("[农民] create_slot: 节点已存在 %s, skip", target)
                return 0

        content = template.get("page_content", "")
        prefix_bytes = content[:50].encode("utf-8", errors="replace")
        short_hash = hashlib.md5(prefix_bytes).hexdigest()[:8]
        block_id = f"{stem}_slot_{short_hash}"

        meta = {
                "tree_node_id": target,
                "node_id": target,
                "block_id": block_id,
                "source_file": source_file,
                **{k: v for k, v in template.items()
                   if k not in ("page_content", "source_file")},
            }
        if req.chunk_meta_patch:
            meta.update(req.chunk_meta_patch)

        new_chunk = {
            "page_content": content,
            "metadata": meta,
        }
        data.append(new_chunk)
        ref_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info("[农民] create_slot: 新节点 %s 写入 %s", target, ref_file.name)
        return 1

    # ── Step 1: rules ─────────────────────────────────────────────────────────

    def _step1_rules(self, chunk: Dict, chunk_index: int) -> Dict:
        """Zero-LLM rule-based fields."""
        meta = chunk.get("metadata", {})
        source_file = meta.get("source_file", "unknown")
        content = (chunk.get("page_content") or chunk.get("text") or "").strip()

        stem = Path(source_file).stem
        prefix_bytes = content[:50].encode("utf-8", errors="replace")
        short_hash = hashlib.md5(prefix_bytes).hexdigest()[:8]
        block_id = f"{stem}_{chunk_index}_{short_hash}"

        fields: Dict = {
            "block_id": block_id,
            "word_count": len(content.split()),
        }

        if _CLI_COMMAND_LINE_RE.search(content):
            fields["has_code_block"] = True

        # command_prefix 由 _match_tree_node 通过 CLI 树权威确定，不在此猜测

        # Promote suggested_category from procurement decision if category missing
        if not meta.get("document_category"):
            suggested = (chunk.get("metadata") or {}).get("suggested_value", "")
            if suggested:
                fields["document_category"] = suggested

        return fields

    def _load_kb_index(self) -> Dict[str, str]:
        if self._kb_index is not None:
            return self._kb_index
        index: Dict[str, str] = {}
        if _KB_PATH.exists():
            try:
                data = json.loads(_KB_PATH.read_text(encoding="utf-8"))
                for item in data:
                    m = item.get("metadata", {})
                    nid = m.get("node_id", "")
                    cp = m.get("command_prefix", "")
                    if nid and cp:
                        index[cp] = nid
            except Exception as exc:
                logger.warning("[农民] 无法加载 knowledge_base.json 索引: %s", exc)
        self._kb_index = index
        return index

    def _load_tree_alias_map(self) -> Dict[str, str]:
        if self._tree_alias_map is not None:
            return self._tree_alias_map
        m: Dict[str, str] = {}
        if _FARMER_TREE_ALIAS_PATH.exists():
            try:
                raw = json.loads(_FARMER_TREE_ALIAS_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k is None or v is None:
                            continue
                        ks, vs = str(k).strip(), str(v).strip()
                        if ks and vs:
                            m[ks] = vs
            except Exception as exc:
                logger.warning(
                    "[农民] 无法加载 %s: %s",
                    _FARMER_TREE_ALIAS_PATH.name,
                    exc,
                )
        self._tree_alias_map = m
        return m

    def _match_tree_node(
        self,
        content: str,
        meta: Dict,
        ac_meta: Optional[Dict] = None,
    ) -> Optional[str]:
        """Resolve skeleton node_id: authoritative ids first, then kb_index + prefix.

        Priority order (high → low):
          1. Explicit tree_node_id / node_id in meta or ac_meta
          2. section_title slug match in skeleton / alias
          2b. section_title stripped of parameter syntax → kb_index lookup
          3. command_prefix exact match in kb_index (with content-lead validation)
          4. first_line slug (only when consistent with command_prefix or cp absent)
             If first_line slug conflicts with command_prefix → treat as ambiguous,
             record in self._last_ambiguous_candidates and return None.
          5. _extract_cli_prefix_strings longest-prefix fallback
        """
        ac_meta = ac_meta or {}
        skeleton = self._load_skeleton()
        index = self._load_kb_index()
        alias = self._load_tree_alias_map()
        self._last_ambiguous_candidates: List[Dict] = []

        def _tid_in_skeleton(tid: str) -> Optional[str]:
            t = str(tid or "").strip()
            return t if t and t in skeleton else None

        # Priority 1: explicit node_id
        for src in (meta, ac_meta):
            hit = _tid_in_skeleton(src.get("tree_node_id") or src.get("node_id"))
            if hit:
                return hit

        # Priority 2: section_title slug
        for src in (meta, ac_meta):
            st = str(src.get("section_title") or "").strip()
            if st:
                slug = _normalize_heading_to_node_slug(st)
                if slug:
                    if slug in skeleton:
                        return slug
                    mapped = alias.get(slug)
                    if mapped and mapped in skeleton:
                        return mapped

        # Priority 2b: section_title stripped of parameter syntax → kb_index
        # Handles titles like "crontab {enable|disable}", "ip route <prefix>",
        # "vlan add [vlan-id]" — strip trailing parameter syntax, then look up
        # the base-command portion in the kb_index (exact → shortest-key family).
        if index:
            for src in (meta, ac_meta):
                st = str(src.get("section_title") or "").strip()
                if not st:
                    continue
                st_base = re.sub(r"\s*[{<\[\(].*", "", st).strip()
                if not st_base or len(st_base) <= 2:
                    continue
                exact = index.get(st_base)
                if exact:
                    return exact
                st_lower = st_base.lower()
                family = [
                    (k, n) for k, n in index.items()
                    if k.lower() == st_lower
                    or k.lower().startswith(st_lower + " ")
                ]
                if family:
                    family.sort(key=lambda x: len(x[0]))
                    return family[0][1]

        # Priority 2c: product_module → trunk/branch node (非 CLI 文档的核心匹配路径)
        # APP/ARCH/HASLB 文档没有 command_prefix，但有中文 product_module（如 "高可用"、"安全"）。
        # 通过 CLI 图 module 节点的 cn_aliases 反查，将中文模块映射到骨架 trunk 节点。
        # section_path 层级可进一步匹配 branch 节点。
        pm = str(meta.get("product_module") or ac_meta.get("product_module") or "").strip()
        has_cp = bool(meta.get("command_prefix") or ac_meta.get("command_prefix"))
        if pm and pm != "unknown" and not has_cp:
            module_node_id = self._match_module_by_name(pm, skeleton)
            if module_node_id:
                # 尝试用 section_path 匹配更具体的 branch 节点
                sp = str(meta.get("section_path") or ac_meta.get("section_path") or "")
                branch_hit = self._match_branch_under_module(
                    module_node_id, sp, skeleton, index,
                )
                return branch_hit or module_node_id

        # Priority 3: command_prefix exact match in kb_index
        # Validate: cp must appear in the leading portion of content to guard
        # against mixed-section chunks where auto_convert tagged a later section.
        # Exception: if content is empty, the meta is authoritative — skip validation.
        cp = str(meta.get("command_prefix") or "").strip()
        if not cp:
            cp = str(ac_meta.get("command_prefix") or "").strip()
        cp_node: Optional[str] = None
        if index and cp:
            cp_node = index.get(cp)
            if cp_node:
                if not content.strip():
                    return cp_node
                content_lead = (content or "")[:300].lower()
                if cp.lower() in content_lead:
                    return cp_node

        # Priority 4: first_line slug — but validate against command_prefix
        first_line = ""
        if content.strip():
            first_line = content.strip().split("\n")[0].strip()
        if (
            first_line
            and first_line[0].isascii()
            and first_line[0].isalpha()
        ):
            slug = _normalize_heading_to_node_slug(first_line)
            fl_node: Optional[str] = None
            if slug:
                if slug in skeleton:
                    fl_node = slug
                else:
                    mapped = alias.get(slug)
                    if mapped and mapped in skeleton:
                        fl_node = mapped
            if fl_node:
                # Check consistency: if cp points to a different node → ambiguous
                cp_idx_node = _longest_prefix_match_node_id(index, cp.lower()) if (index and cp) else None
                if cp_idx_node and cp_idx_node != fl_node:
                    # Conflicting signals — record ambiguity and escalate to farm owner
                    self._last_ambiguous_candidates = [
                        {"source": "first_line_slug", "node_id": fl_node,
                         "evidence": first_line[:120]},
                        {"source": "command_prefix", "node_id": cp_idx_node,
                         "evidence": cp},
                    ]
                    return None
                return fl_node

        if not index:
            return None

        # Priority 5: _extract_cli_prefix_strings longest-prefix fallback
        if ac_meta.get("chunk_type") and ac_meta["chunk_type"] != "single_command":
            return None
        for cand in _extract_cli_prefix_strings(content):
            nid = _longest_prefix_match_node_id(index, cand.lower())
            if nid:
                return nid
        return None

    # ── 模块级匹配辅助（Priority 2c） ──────────────────────────────────────

    _module_cn_map: Optional[Dict[str, List[str]]] = None

    def _build_module_cn_map(self) -> Dict[str, List[str]]:
        """构建 中文模块名 → [trunk node_id, ...] 的反向映射。

        数据源：CLI 图 module 节点的 cn_aliases 字段。
        """
        if self._module_cn_map is not None:
            return self._module_cn_map
        cn_map: Dict[str, List[str]] = {}
        try:
            graph_path = _INAGENT_ROOT / "knowledge_base" / "cli_keyword_graph.json"
            if graph_path.exists():
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                for node in graph.get("nodes", []):
                    if node.get("type") != "module":
                        continue
                    nid = node.get("id", "")
                    if not nid:
                        continue
                    for alias in node.get("cn_aliases", []):
                        alias_key = alias.strip()
                        if alias_key:
                            cn_map.setdefault(alias_key, []).append(nid)
                    # 也加入英文 label（大写）作为候选
                    label = node.get("label", "").strip()
                    if label:
                        cn_map.setdefault(label, []).append(nid)
                        cn_map.setdefault(label.lower(), []).append(nid)
        except Exception as exc:
            logger.debug("[农民] 构建 cn_map 失败: %s", exc)
        self._module_cn_map = cn_map
        return cn_map

    def _match_module_by_name(
        self, product_module: str, skeleton: Dict[str, Dict],
    ) -> Optional[str]:
        """通过中文/英文模块名查找骨架 trunk 节点。"""
        cn_map = self._build_module_cn_map()
        candidates = cn_map.get(product_module, [])
        if not candidates:
            # 尝试不区分大小写
            pm_lower = product_module.lower()
            for key, nids in cn_map.items():
                if key.lower() == pm_lower:
                    candidates = nids
                    break
        # 优先返回骨架中存在的节点
        for nid in candidates:
            if nid in skeleton:
                return nid
        return candidates[0] if candidates else None

    def _match_branch_under_module(
        self,
        module_node_id: str,
        section_path: str,
        skeleton: Dict[str, Dict],
        index: Optional[Dict[str, str]],
    ) -> Optional[str]:
        """尝试用 section_path 在模块下匹配更具体的 branch 节点。

        策略：提取 section_path 中的各段，查找骨架中属于该模块的 branch 节点，
        通过关键词重叠判断最佳匹配。
        """
        if not section_path:
            return None
        path_parts = [p.strip() for p in section_path.split(">") if p.strip()]
        if len(path_parts) <= 1:
            return None

        # 收集该模块下的 branch 节点
        module_branches: List[str] = []
        for nid, node in skeleton.items():
            m = node.get("metadata", {})
            tp = m.get("tree_position", {})
            tl = tp.get("tree_level", "") if isinstance(tp, dict) else ""
            if tl == "branch" and m.get("product_module") == module_node_id:
                module_branches.append(nid)

        if not module_branches:
            return None

        # 对 section_path 各段做关键词匹配
        best_nid = None
        best_score = 0
        for nid in module_branches:
            node_m = skeleton[nid].get("metadata", {})
            node_cp = (node_m.get("command_prefix") or nid).lower()
            node_tokens = set(node_cp.replace("_", " ").split())
            for part in path_parts:
                part_lower = part.lower()
                # 检查 node_cp 是否出现在 section_path 段中
                overlap = sum(1 for t in node_tokens if t in part_lower)
                if overlap > best_score:
                    best_score = overlap
                    best_nid = nid
        # 至少需要 1 个 token 重叠才算匹配
        return best_nid if best_score >= 1 else None

    def _detect_cross_refs(
        self, content: str, own_prefix: Optional[str] = None
    ) -> List[str]:
        refs: List[str] = []
        for m in _CROSS_REF_CMD_RE.finditer(content):
            cmd = m.group(1).strip()
            normalized = cmd.replace(" ", " ")
            if own_prefix and normalized == own_prefix:
                continue
            if normalized not in refs:
                refs.append(normalized)
        return refs

    def _detect_schema_gaps(
        self, content: str, meta: Dict, ac_meta: Optional[Dict] = None
    ) -> List[SchemaGapEntry]:
        gaps: List[SchemaGapEntry] = []
        ac_meta = ac_meta or {}
        match = _SCHEMA_GAP_OVERRIDE_RE.search(content)
        if not match:
            return gaps
        override_cmds: List[str] = ac_meta.get("override_commands") or []
        entities = (
            [c.strip() for c in override_cmds if c.strip()]
            if override_cmds
            else [meta.get("tree_node_id") or meta.get("command_prefix") or ""]
        )
        start = max(0, match.start() - 40)
        end = min(len(content), match.end() + 40)
        evidence = content[start:end]
        for entity_title in entities:
            if not entity_title:
                continue
            gaps.append(SchemaGapEntry(
                gap_type="new_entity_attribute",
                entity_title=entity_title,
                column_name="supports_override",
                column_dtype="bool",
                default_value=False,
                evidence=evidence,
                source_file=meta.get("source_file", ""),
                timestamp=datetime.now().isoformat(),
            ))
        return gaps

    # ── auto_convert 工具 + 骨架 diff ─────────────────────────────────────────

    def _load_skeleton(self) -> Dict[str, Dict]:
        if self._skeleton is not None:
            return self._skeleton
        skeleton: Dict[str, Dict] = {}
        if _KB_PATH.exists():
            try:
                data = json.loads(_KB_PATH.read_text(encoding="utf-8"))
                for item in data:
                    m = item.get("metadata", {})
                    nid = m.get("node_id", "")
                    if nid:
                        skeleton[nid] = item
            except Exception as exc:
                logger.warning("[农民] 无法加载骨架: %s", exc)
        self._skeleton = skeleton
        return skeleton

    def _refine_ac_meta_command_prefix(self, content: str, ac_meta: Dict) -> None:
        """Align auto_convert output to skeleton CLI labels using kb_index.

        auto_convert is a shared extractor; tree-shaped command_prefix policy lives in the farmer.
        """
        index = self._load_kb_index()
        text = (content or "").strip()
        if not index or not text:
            return
        tl = text.lower()
        nid = _longest_prefix_match_node_id(index, tl)
        if not nid:
            return
        keys = [k for k, n in index.items() if n == nid]
        if not keys:
            return
        best_key = max(keys, key=len)
        bl = best_key.lower()
        if not tl.startswith(bl):
            return
        cur = str(ac_meta.get("command_prefix") or "").strip()
        if not cur:
            ac_meta["command_prefix"] = best_key
            return
        cl = cur.lower()
        if len(best_key) > len(cur) and bl.startswith(cl):
            ac_meta["command_prefix"] = best_key

    def _structure_via_auto_convert(self, chunks: List[Dict]) -> List[Dict]:
        """Call auto_convert to structure raw chunk metadata.

        Phase 1 only: rule-based extraction via _extract_chunk_metadata.
        Farmer never calls LLM — missing fields become overflow gaps
        for the farm owner to decide.
        """
        from INAGENT.data_tools.auto_convert import (
            _extract_chunk_metadata,
        )

        results: List[Dict] = []
        for chunk in chunks:
            text = (chunk.get("page_content") or chunk.get("text") or "")
            base_meta = chunk.get("metadata", {})
            meta = _extract_chunk_metadata(text, base_meta, skip_llm=True)
            results.append(meta)

        return results

    def _diff_with_skeleton(
        self,
        ac_meta: Dict,
        content: str,
        skeleton_node: Dict,
    ) -> Tuple[Dict, List[SchemaGapEntry]]:
        """Compare auto_convert metadata against a skeleton node.

        Returns (updates_to_apply, gaps_detected).
        """
        sk_meta = skeleton_node.get("metadata", {})
        node_id = sk_meta.get("node_id", "")
        source_file = str(
            ac_meta.get("source_file") or sk_meta.get("source_file", "")
        )

        updates: Dict = {}
        gaps: List[SchemaGapEntry] = []

        for fld in _SKELETON_META_FIELDS:
            new_val = ac_meta.get(fld)
            old_val = sk_meta.get(fld)
            if not new_val or new_val == old_val:
                continue
            if fld == "product_module":
                if not new_val:
                    continue
                os = str(old_val) if old_val is not None else ""
                ns = str(new_val) if new_val is not None else ""
                if old_val:
                    if os.lower() == ns.lower():
                        if ns != os:
                            updates[fld] = old_val
                    else:
                        gaps.append(SchemaGapEntry(
                            gap_type="conflict",
                            entity_title=node_id,
                            field_name=fld,
                            skeleton_value=old_val,
                            new_value=new_val,
                            evidence=content[:200],
                            source_file=source_file,
                            timestamp=datetime.now().isoformat(),
                        ))
                else:
                    updates[fld] = ns.lower()
                continue
            if old_val and new_val != old_val:
                gaps.append(SchemaGapEntry(
                    gap_type="conflict",
                    entity_title=node_id,
                    field_name=fld,
                    skeleton_value=old_val,
                    new_value=new_val,
                    evidence=content[:200],
                    source_file=source_file,
                    timestamp=datetime.now().isoformat(),
                ))
            else:
                updates[fld] = new_val

        for fld, val in ac_meta.items():
            if fld in _SKELETON_META_FIELDS or fld in _SKIP_DIFF_FIELDS:
                continue
            if not val or val in ("unknown", ""):
                continue
            if fld in sk_meta:
                continue
            gaps.append(SchemaGapEntry(
                gap_type="overflow",
                entity_title=node_id,
                field_name=fld,
                new_value=val,
                evidence=content[:200],
                source_file=source_file,
                timestamp=datetime.now().isoformat(),
                chunk_content=content[:500],
            ))

        ac_desc = str(ac_meta.get("description", ""))
        if ac_desc and ac_desc not in ("unknown", ""):
            updates["_enriched_description"] = ac_desc

        return updates, gaps

    def _find_nearest_skeleton_matches(
        self,
        content: str,
        skeleton: Dict[str, Dict],
        top_k: int = 3,
    ) -> List[Dict]:
        content_words = set(content.lower().split())
        scores: list = []
        for nid, node in skeleton.items():
            sk_content = (node.get("page_content") or "").lower()
            sk_words = set(sk_content.split())
            overlap = len(content_words & sk_words)
            if overlap > 0:
                scores.append((nid, overlap))
        scores.sort(key=lambda x: -x[1])
        return [
            {"node_id": nid, "similarity": score}
            for nid, score in scores[:top_k]
        ]

    @staticmethod
    def _parse_skeleton_sections(page_content: str) -> Dict[str, str]:
        sections: Dict[str, str] = {}
        current_header = ""
        current_lines: List[str] = []
        for line in page_content.split("\n"):
            m = _SKELETON_SECTION_RE.match(line)
            if m:
                if current_header:
                    sections[current_header] = "\n".join(current_lines).strip()
                current_header = m.group(1)
                rest = line[m.end():].strip()
                current_lines = [rest] if rest else []
            else:
                current_lines.append(line)
        if current_header:
            sections[current_header] = "\n".join(current_lines).strip()
        return sections

    def update_skeleton(
        self,
        results: List[FarmResult],
        kb_path: Optional[Path] = None,
    ) -> int:
        """Update matched skeleton nodes in knowledge_base.json."""
        kb_path = kb_path or _KB_PATH
        matched = [r for r in results if r.matched_node_id]
        if not matched or not kb_path.exists():
            return 0

        try:
            data = json.loads(kb_path.read_text(encoding="utf-8"))
        except Exception:
            return 0

        by_node: Dict[str, Dict] = {}
        by_node_content: Dict[str, str] = {}
        for r in matched:
            meta = r.chunk.get("metadata", {})
            by_node[r.matched_node_id] = meta
            by_node_content[r.matched_node_id] = (
                r.chunk.get("page_content") or r.chunk.get("text") or ""
            )

        updated = 0
        for item in data:
            m = item.get("metadata", {})
            nid = m.get("node_id", "")
            if nid not in by_node:
                continue
            node_meta = by_node[nid]
            changed = False
            for k, v in node_meta.items():
                if k.startswith("_enriched_") or k in (
                    "tree_node_id", "block_id", "word_count",
                    "has_code_block", "command_refs",
                ):
                    continue
                if k in _SKELETON_META_FIELDS:
                    if k == "product_module":
                        continue
                    if not m.get(k) and v:
                        m[k] = v
                        changed = True

            desc = node_meta.get("_enriched_description", "")
            if desc:
                pc = item.get("page_content", "")
                if desc not in pc:
                    if "[说明]" in pc and "\n语法:" in pc:
                        item["page_content"] = pc.replace(
                            "\n语法:", f"\n{desc}\n语法:",
                        )
                    else:
                        item["page_content"] = f"{pc}\n[说明] {desc}"
                    changed = True

            # P2: fill empty 参数: / 语法: from chunk content
            chunk_pc = by_node_content.get(nid, "")
            if chunk_pc:
                sk_secs = self._parse_skeleton_sections(item.get("page_content", ""))
                chunk_params = _extract_labeled_section(chunk_pc, "参数:")
                if chunk_params:
                    item["page_content"] = _write_skeleton_section_if_richer(
                        item["page_content"], "参数:", chunk_params
                    )
                    changed = True
                if not sk_secs.get("语法:", "").strip():
                    first_line = chunk_pc.strip().split("\n")[0].strip()
                    if first_line and first_line[0].isascii() and first_line[0].isalpha():
                        item["page_content"] = _write_skeleton_section_if_richer(
                            item["page_content"], "语法:", first_line
                        )
                        changed = True

            # P3: append command_refs to 相关操作:
            command_refs = node_meta.get("command_refs") or []
            if command_refs and isinstance(command_refs, list):
                sk_secs_ref = self._parse_skeleton_sections(item.get("page_content", ""))
                existing_refs = sk_secs_ref.get("相关操作:", "")
                new_refs = [r for r in command_refs if r not in existing_refs]
                if new_refs:
                    item["page_content"] = _append_skeleton_section_entries(
                        item["page_content"], "相关操作:", new_refs
                    )
                    changed = True

            if changed:
                item["metadata"] = m
                updated += 1

        if updated:
            kb_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("[农民] update_skeleton: %d 节点已更新", updated)

        return updated

    # ── Step 3: index ────────────────────────────────────────────────────────

    def _step3_index(self, meta: Dict) -> Dict:
        """Append scenario_id / step_type from function_structure_index."""
        if not self._function_index:
            return {}

        fields: Dict = {}
        scenarios = self._function_index.get("scenarios", {})
        product_module = meta.get("product_module", "")
        protocol_types = meta.get("protocol_type", [])
        if isinstance(protocol_types, str):
            protocol_types = [protocol_types]
        elif not isinstance(protocol_types, list):
            protocol_types = []

        # Build a lightweight search text from already-known metadata fields
        search_text = " ".join(filter(None, [
            meta.get("section_title", ""),
            meta.get("description", ""),
            product_module,
        ])).lower()

        if not meta.get("scenario_id"):
            for sid, cfg in scenarios.items():
                pm_match = any(
                    pm.lower() in search_text or pm == product_module
                    for pm in cfg.get("product_modules", [])
                )
                pt_match = (
                    not cfg.get("protocol_types")
                    or any(pt in protocol_types for pt in cfg.get("protocol_types", []))
                )
                if pm_match and pt_match:
                    fields["scenario_id"] = sid
                    break

        if not meta.get("step_type"):
            try:
                from INAGENT.utils.index_utils import get_step_type_keywords
                for stype, keywords in get_step_type_keywords(self._function_index).items():
                    if any(kw.lower() in search_text for kw in keywords):
                        fields["step_type"] = stype
                        break
            except Exception:
                pass

        return fields

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_llm_json(self, raw: str, expected: int) -> Dict[int, Dict]:
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        if fence:
            raw = fence.group(1).strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            logger.warning("[农民] LLM 响应中未找到 JSON 数组")
            return {}
        try:
            data = json.loads(raw[start: end + 1])
        except json.JSONDecodeError:
            try:
                import json_repair  # type: ignore
                data = json_repair.loads(raw[start: end + 1])
            except Exception:
                logger.warning("[农民] JSON 解析失败: %s", raw[:200])
                return {}
        if not isinstance(data, list):
            return {}
        return {i: item for i, item in enumerate(data) if isinstance(item, dict)}

    def _load_function_index(self) -> Optional[Dict]:
        if not _FUNCTION_INDEX_PATH.exists():
            return None
        try:
            return json.loads(_FUNCTION_INDEX_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[农民] 无法加载 function_structure_index.json: %s", exc)
            return None


# ── CLI smoke test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
    )
    from INAGENT.utils.env_utils import load_inagent_env

    load_inagent_env()

    sample_dir = _REFERENCE_DIR
    sample_decisions: list = []
    for json_file in sorted(sample_dir.glob("*.json"))[:1]:
        if "_bak" in json_file.stem:
            continue
        try:
            items = json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for idx, item in enumerate(items[:3]):
            sample_decisions.append(ChunkDecision(
                chunk=item,
                decision=ProcurementDecision(
                    action="accept",
                    target_kb="product",
                    confidence=0.9,
                    reason="smoke test",
                ),
                source_file=item.get("metadata", {}).get(
                    "source_file", json_file.name,
                ),
                chunk_index=idx,
            ))

    if not sample_decisions:
        print("无样本数据，请先确保 reference/ 目录下有 JSON 文件。")
        sys.exit(1)

    farmer = KnowledgeFarmerAgent()
    results = farmer.cultivate_batch(sample_decisions)

    print(f"\n共 {len(results)} 个 FarmResult\n")
    for r in results:
        print(f"[{r.block_id}] matched={r.matched_node_id or '(none)'}")
        print(f"  enriched: {r.enriched_fields}")
        if r.schema_gaps:
            for g in r.schema_gaps:
                print(f"  gap: {g.gap_type} field={g.field_name} entity={g.entity_title}")
        print()
