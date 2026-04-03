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
  1. 调 auto_convert 对采购员 accept 的裸 chunk 做结构化（规则+LLM 元数据提取）
  2. 匹配骨架（knowledge_base.json）节点，逐字段 diff
  3. 对齐的部分直接更新骨架；冲突/溢出/无匹配 → SchemaGapEntry → 农场主裁决
  4. 收到 FillRequest 后回填

auto_convert 是农民的通用工具（PDF/DOCX/XLSX/TXT → 统一 metadata），农民不关心原始格式。

持久化：
  reference/{stem}.json         —— 新 chunk 按 block_id 去重追加
  knowledge_base.json           —— 骨架节点原地更新
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
from INAGENT.rag.knowledge_schema import FillRequest, SchemaGapEntry
from INAGENT.utils.env_utils import load_inagent_env

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
            if tree_node_id:
                meta["tree_node_id"] = tree_node_id
                enriched.append("tree_node_id")

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
                if cmd_prefix:
                    gaps.append(SchemaGapEntry(
                        gap_type="overflow",
                        entity_title=str(cmd_prefix),
                        field_name="unmatched_chunk",
                        entity_description=str(ac_meta.get("description", "")),
                        evidence=content[:200],
                        source_file=cd.source_file,
                        timestamp=datetime.now().isoformat(),
                        chunk_content=content[:500],
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
            if out_path.exists():
                try:
                    existing = json.loads(out_path.read_text(encoding="utf-8"))
                    existing_ids = {
                        item.get("metadata", {}).get("block_id", "")
                        for item in existing
                    }
                except Exception:
                    existing, existing_ids = [], set()

            cache: Dict[str, str] = {}
            if cache_path.exists():
                try:
                    cache = json.loads(cache_path.read_text(encoding="utf-8"))
                except Exception:
                    cache = {}

            new_chunks: List[Dict] = []
            for r in stem_results:
                if r.block_id in existing_ids or r.block_id in cache:
                    continue
                new_chunks.append(r.chunk)
                cache[r.block_id] = datetime.now().isoformat()

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
                }, ensure_ascii=False, default=str) + "\n")
        logger.info("[农民] emit_schema_gaps: %d 条写入 %s", len(all_gaps), gaps_file)
        return len(all_gaps)

    def apply_fill_request(
        self,
        fill_requests: List[FillRequest],
        ref_dir: Optional[Path] = None,
    ) -> int:
        ref_dir = ref_dir or _REFERENCE_DIR
        if not fill_requests or not ref_dir.exists():
            return 0

        by_entity: Dict[str, Dict] = {}
        for req in fill_requests:
            if not req.entity_title or req.action == "discard":
                continue
            by_entity.setdefault(req.entity_title, {}).update(req.fill_fields)

        if not by_entity:
            return 0

        filled = 0
        for json_file in sorted(ref_dir.glob("*.json")):
            if "_bak" in json_file.stem or "_bak_" in json_file.stem:
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            modified = False
            for item in data:
                meta = item.get("metadata", {})
                tid = meta.get("tree_node_id") or meta.get("node_id", "")
                if tid in by_entity:
                    for k, v in by_entity[tid].items():
                        meta[k] = v
                    item["metadata"] = meta
                    modified = True
                    filled += 1
            if modified:
                json_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        logger.info("[农民] apply_fill_request: %d 条已填充", filled)
        return filled

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

        rules = _get_metadata_rules()
        for prefix, patterns in rules.get("command_prefixes", {}).items():
            if any(content.lstrip().startswith(p) for p in patterns):
                fields["command_prefix"] = prefix
                break

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
        """Resolve skeleton node_id: authoritative ids first, then kb_index + prefix."""
        ac_meta = ac_meta or {}
        skeleton = self._load_skeleton()
        index = self._load_kb_index()
        alias = self._load_tree_alias_map()

        def _tid_in_skeleton(tid: str) -> Optional[str]:
            t = str(tid or "").strip()
            return t if t and t in skeleton else None

        for src in (meta, ac_meta):
            hit = _tid_in_skeleton(src.get("tree_node_id") or src.get("node_id"))
            if hit:
                return hit

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

        first_line = ""
        if content.strip():
            first_line = content.strip().split("\n")[0].strip()
        if (
            first_line
            and first_line[0].isascii()
            and first_line[0].isalpha()
        ):
            slug = _normalize_heading_to_node_slug(first_line)
            if slug:
                if slug in skeleton:
                    return slug
                mapped = alias.get(slug)
                if mapped and mapped in skeleton:
                    return mapped

        if not index:
            return None

        cp = str(meta.get("command_prefix") or "").strip()
        if not cp:
            cp = str(ac_meta.get("command_prefix") or "").strip()
        if cp and cp in index:
            return index[cp]

        if ac_meta.get("chunk_type") and ac_meta["chunk_type"] != "single_command":
            return None
        for cand in _extract_cli_prefix_strings(content):
            nid = _longest_prefix_match_node_id(index, cand.lower())
            if nid:
                return nid
        return None

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

        Phase 1: rule-based extraction via _extract_chunk_metadata
        Phase 2: batch LLM for chunks needing more metadata (if configured)

        After this, :meth:`_refine_ac_meta_command_prefix` maps coarse prefixes to skeleton labels.
        """
        from INAGENT.data_tools.auto_convert import (
            _apply_llm_metadata_extraction_batch,
            _extract_chunk_metadata,
            _load_project_config,
        )

        config = _load_project_config()
        llm_config = config.get("llm-aided-config", {}).get(
            "metadata_extraction", {}
        )

        results: List[Dict] = []
        for chunk in chunks:
            text = (chunk.get("page_content") or chunk.get("text") or "")
            base_meta = chunk.get("metadata", {})
            meta = _extract_chunk_metadata(text, base_meta)
            results.append(meta)

        if llm_config.get("enable", False):
            _KEY_FIELDS = {"product_module", "protocol_type", "intent", "config_mode"}
            needs_llm: list = []
            for i, meta in enumerate(results):
                filled = sum(1 for f in _KEY_FIELDS if meta.get(f))
                if filled < 3 or not meta.get("chunk_type"):
                    text = (chunks[i].get("page_content") or "")
                    needs_llm.append((text, meta))
            if needs_llm:
                _apply_llm_metadata_extraction_batch(needs_llm, llm_config)

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

    from INAGENT.utils.env_utils import load_inagent_env
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
    )

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
