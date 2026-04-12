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
采购员 Agent (Knowledge Procurement Agent)

对 **采购管线**（``procurement_ingest`` / ``auto_convert.run_procurement_document_pipeline``）
落盘产出的 chunk 做筛查，输出四类 action 与 target_kb。

机械层（L0+L1，实现见 ``procurement_pre_clean`` + ``chunk_text_quality``）
  L0 预清理：与全库一致的垃圾/版式启发式（纯标点行、版权声明单行等）→ reject，不进 LLM。
  L1 长度：page_content/text strip 后长度 < 50 → reject，否则进 L2。
  （二者在 ``_layer1_mechanical`` 中顺序执行，统称「机械层」。）

三层（对外说明仍可按 L2/L3 计）
  L2 LLM：按 metadata.source_file 分组；同一文件 chunk 数超过 _LLM_BATCH_SIZE（默认 50）时拆子批，
          每子批一次 ChatAgent.step。prompt 内每段仅展示正文前 500 字
          （用于判断，不修改入库正文）。JSON 字段 suggested_category → ProcurementDecision.suggested_value。
          confidence < 0.6 且 action 为 accept/reject → 改为 pending_review；非法 action → pending_review。
  L3 元数据：reject/pending_review 直接返回；否则用 suggested_value 或 metadata.document_category
          校验 DOCUMENT_CATEGORIES，不在白名单 → staging (new_category)；
          再校验 product_module 是否在 product_modules_registry.json 的 modules 中 → 否且表非空 → staging (new_module)。

公开入口
  KnowledgeProcurementAgent.evaluate_batch, enrich_decisions_for_farmer, filter_accepted, write_logs
  enrich_chunk_decision_for_farmer(cd)  # 模块函数，accept 上同步 metadata 供农民

日志（默认 knowledge_base/logs/，accept 不写 jsonl）
  reject → reject_log.jsonl；pending_review → pending_review.jsonl；staging → schema_gaps.jsonl

农民交接：enrich_chunk_decision_for_farmer / enrich_decisions_for_farmer / filter_accepted
  浅拷贝 chunk；非空 suggested_value → metadata.suggested_value；在 DOCUMENT_CATEGORIES 内则写 document_category；
  source_file 与 ChunkDecision 对齐。不截断正文、不删入库元数据（含 MinerU/规则阶段字段）。

SchemaGapType 含 new_entity / new_entity_attribute：当前 L3 仅产生 new_category、new_module。

职责或实现变更时同步更新：
  INAGENT/docs/agents/sessions/03-procurement.md（含「实现摘要」）
  .cursor/rules/kb-session-procurement.mdc
  procurement_pre_clean.py、chunk_text_quality.py、.cursor/skills/procurement-pre-clean/SKILL.md（机械预清理相关）
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend
from INAGENT.rag.knowledge_config import DOCUMENT_CATEGORIES
from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger(__name__)

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
_KB_LOGS_DIR = _INAGENT_ROOT / "knowledge_base" / "logs"
_PROCUREMENT_STEP_TIMEOUT = 180
_L2_CACHE_SCHEMA_VERSION = 1

ActionType = Literal["accept", "reject", "pending_review", "staging"]
TargetKB = Literal["product", "test", "unknown"]
SchemaGapType = Literal["new_category", "new_module", "new_entity", "new_entity_attribute"]


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ProcurementDecision:
    action: ActionType
    target_kb: TargetKB
    confidence: float
    reason: str
    schema_gap: Optional[SchemaGapType] = None
    suggested_value: Optional[str] = None
    reason_code: Optional[str] = None
    rule_layer: Optional[str] = None
    rule_confidence: Optional[float] = None


@dataclass
class ChunkDecision:
    chunk: Dict
    decision: ProcurementDecision
    source_file: str = ""
    chunk_index: int = 0


def enrich_chunk_decision_for_farmer(cd: ChunkDecision) -> ChunkDecision:
    """Sync procurement outputs onto chunk metadata before cultivate_batch.

    Farmers read metadata.suggested_value when document_category is empty
    (_step1_rules). They expect source_file aligned with ChunkDecision.source_file.
    Does not drop keys from chunk or metadata; shallow-copies chunk and metadata.
    """
    if cd.decision.action != "accept":
        return cd
    chunk = {**cd.chunk}
    meta = {**chunk.get("metadata", {})}
    sug = (cd.decision.suggested_value or "").strip()
    if sug:
        meta["suggested_value"] = sug
        if sug in DOCUMENT_CATEGORIES:
            meta["document_category"] = sug
    if cd.source_file and cd.source_file != "unknown":
        meta["source_file"] = cd.source_file
    chunk["metadata"] = meta
    return ChunkDecision(
        chunk=chunk,
        decision=cd.decision,
        source_file=cd.source_file,
        chunk_index=cd.chunk_index,
    )


# ── System prompt ─────────────────────────────────────────────────────────────

_KB_REFERENCE_PATH = _INAGENT_ROOT / "knowledge_base" / "reference" / "knowledge_base.json"
_MODULE_COUNT_THRESHOLD = 10


def _load_known_modules(min_count: int = _MODULE_COUNT_THRESHOLD) -> List[str]:
    """Read product_module distribution from knowledge_base.json.

    Returns sorted list of modules appearing >= min_count times.
    Falls back to empty list if file is missing or unreadable.
    """
    if not _KB_REFERENCE_PATH.exists():
        return []
    try:
        data = json.loads(_KB_REFERENCE_PATH.read_text(encoding="utf-8"))
        counts: Dict[str, int] = {}
        for item in data:
            m = item.get("metadata", {}).get("product_module", "")
            if m and m not in ("unknown", ""):
                counts[m] = counts.get(m, 0) + 1
        return sorted(k for k, v in counts.items() if v >= min_count)
    except Exception:
        return []


def _build_system_prompt(
    product_name: str,
    known_modules: Optional[List[str]] = None,
) -> str:
    categories_str = "\n".join(f"  - {c}" for c in DOCUMENT_CATEGORIES)
    if known_modules:
        modules_str = "、".join(known_modules)
        modules_section = (
            "\n【当前知识库已有功能模块（自动同步）】\n"
            f"  {modules_str}\n"
            "  — 若内容涉及以上模块，优先标记为 product。\n"
            "  — 若内容属于某模块但该模块不在列表中，照常 accept，reason 中注明模块名。\n"
        )
    else:
        modules_section = ""
    return f"""\
你是 {product_name} 知识库的内容质量过滤器（采购员）。

你的职责是判断每段内容是否值得进入知识库，以及属于哪类知识。
你不需要了解具体的命令树结构，只需判断内容本身的质量和归属。

【判断标准 — 只有一个核心问题】
  这段内容，对理解/配置/测试该产品有实质价值吗？
  即：它包含功能说明、命令语法、配置示例、测试步骤、或架构原理吗？

【接受的内容（标注 target_kb）】
  product（产品知识）：功能说明、配置示例、架构原理、CLI 语法参考、设计规格、接口描述
  test（测试知识）：测试用例、测试策略、Bug 修复说明、评审规则、缺陷分析

【必须拒绝的内容】
  - 纯目录页（只有标题和页码，无实质内容）
  - 版权声明、商标声明、法律合规声明
  - 修订历史表（仅有版本号和日期）
  - 空白页、图表列表、纯缩略词/术语表（无解释）
  - 占位模板内容（如 "XXX子功能"、"YYY子功能"）

【粒度（与下游 CLI 树匹配相关）】
  - 一大张附录表、同一块内多条独立命令时，最长前缀匹配易绑错叶子。
  - 若上游能把此类内容拆成多块或按命令行分块（chunk_type 与内容一致，如 single_command），
    优先标 accept；若无法拆分且风险高，可用 pending_review 并说明「建议拆块」。
  - 不要编造或删除正文；表格（MinerU 转写）须随原文完整保留在评估依据中。

【注意】：标题看似"附录"或"说明"，但内容含具体命令语法、配置参数或行为描述 → 应接受。
{modules_section}
【已知文档分类体系】
{categories_str}

【输出格式】
对每个 chunk 输出一个 JSON 对象：
{{
  "idx": <chunk索引>,
  "action": "accept" | "reject" | "pending_review" | "staging",
  "target_kb": "product" | "test" | "unknown",
  "confidence": 0.0~1.0,
  "reason": "一句话说明原因",
  "suggested_category": "建议的分类（来自已知分类体系；若为 staging 则填建议的新分类名）"
}}

规则：
- confidence < 0.6 时使用 "pending_review"，不武断判定
- "staging" 仅用于内容有价值但分类不在已知体系中的情况
- 返回 JSON 数组，按 idx 顺序排列，不输出其他文字
"""


# ── Agent builder ─────────────────────────────────────────────────────────────

def build_procurement_agent(model: BaseModelBackend, product_name: str) -> ChatAgent:
    """Build the procurement agent (采购员).

    Dynamically loads known product modules from knowledge_base.json so the
    prompt reflects the current knowledge base state without manual maintenance.
    """
    load_inagent_env()
    known_modules = _load_known_modules()
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Knowledge Procurement Agent",
            content=_build_system_prompt(product_name, known_modules),
        ),
        model=model,
        step_timeout=_PROCUREMENT_STEP_TIMEOUT,
        # L2 按批独立评估，无需跨批对话；默认 summarize_threshold=50 会在累积上下文后
        # 触发 CAMEL「Summarize the conversation」额外 LLM 调用，极慢且与采购任务无关。
        summarize_threshold=None,
        # 采购无工具，每步一次模型调用即可；避免异常路径下多轮迭代拉长单次 step。
        max_iteration=1,
    )


def build_procurement_prompt(source_file: str, chunks: List[Dict]) -> str:
    """Build per-source-file evaluation prompt for Layer 2."""
    lines = [
        f"来源文档：{source_file}",
        f"共 {len(chunks)} 个片段，请逐一评估。",
        "",
        "以下是各片段内容（格式：[IDX] section_title / 内容前500字）：",
        "",
    ]
    for i, chunk in enumerate(chunks):
        meta = chunk.get("metadata", {})
        sec = meta.get("section_title", "")
        content = (chunk.get("page_content") or chunk.get("text") or "")[:500]
        header = f"[{i}] {sec}" if sec else f"[{i}]"
        lines.append(header)
        lines.append(content)
        lines.append("---")
    lines += [
        "",
        "请返回 JSON 数组，每个元素对应一个片段（按 idx 顺序）。",
        '格式：[{"idx":0,"action":"...","target_kb":"...","confidence":0.9,"reason":"...","suggested_category":"..."},...]',
        "只返回 JSON，不要输出其他文字。",
    ]
    return "\n".join(lines)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_callable_signature(func) -> str:
    try:
        return inspect.getsource(func)
    except Exception:
        return getattr(func, "__qualname__", repr(func))


_PROCUREMENT_PROMPT_SOURCE_SIGNATURE = _hash_text(
    _safe_callable_signature(_build_system_prompt)
    + "\n"
    + _safe_callable_signature(build_procurement_prompt)
)


def _infer_model_signature(model: BaseModelBackend) -> str:
    parts = [f"class={model.__class__.__module__}.{model.__class__.__name__}"]
    for attr in (
        "model_type",
        "model_name",
        "model",
        "base_url",
        "api_base",
        "model_platform",
    ):
        try:
            value = getattr(model, attr, None)
        except Exception:
            value = None
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            parts.append(f"{attr}={value}")
    return "|".join(parts)


# ── Main class ────────────────────────────────────────────────────────────────

class KnowledgeProcurementAgent:
    """
    采购员：对 auto_convert 输出的 chunk 进行三层筛查。

    用法::

        agent = KnowledgeProcurementAgent(model=model)
        decisions = agent.evaluate_batch(chunks)
        agent.write_logs(decisions)
        decisions = agent.enrich_decisions_for_farmer(decisions)
        # 农民：cultivate_batch([d for d in decisions if d.decision.action == "accept"])
        # 若只要已同步 metadata 的裸 chunk 列表：filter_accepted(decisions)
    """

    def __init__(
        self,
        model: BaseModelBackend,
        product_name: str = "NSAE (InfosecOS) 负载均衡器",
        registry_path: Optional[Path] = None,
        model_signature: Optional[str] = None,
        llm_cache_dir: Optional[Path] = None,
        llm_cache_read_only: bool = False,
        llm_cache_force_refresh: bool = False,
        _chat_agent=None,
    ):
        self._chat_agent = (
            _chat_agent if _chat_agent is not None
            else build_procurement_agent(model, product_name)
        )
        self._product_name = product_name
        self._model_signature = model_signature or _infer_model_signature(model)
        self._llm_cache_dir = Path(llm_cache_dir) if llm_cache_dir else None
        self._llm_cache_read_only = llm_cache_read_only
        self._llm_cache_force_refresh = llm_cache_force_refresh
        self._l2_cache_hits = 0
        self._l2_cache_misses = 0
        if self._llm_cache_dir and not self._llm_cache_read_only:
            self._llm_cache_dir.mkdir(parents=True, exist_ok=True)
        self._registry = self._load_registry(registry_path)

    # ── public ────────────────────────────────────────────────────────────────

    def evaluate_batch(self, chunks: List[Dict]) -> List[ChunkDecision]:
        """
        Evaluate all chunks through three layers.

        Chunks are grouped by source_file so Layer 2 LLM calls are
        batched per document (not per chunk), reducing token usage.
        """
        decisions: List[ChunkDecision] = []
        pending_for_llm: Dict[str, List[Tuple[int, Dict]]] = defaultdict(list)

        for global_idx, chunk in enumerate(chunks):
            meta = chunk.get("metadata", {})
            source_file = meta.get("source_file", "unknown")
            result = self._layer1_mechanical(chunk)
            if result is not None:
                decisions.append(ChunkDecision(
                    chunk=chunk,
                    decision=result,
                    source_file=source_file,
                    chunk_index=global_idx,
                ))
            else:
                pending_for_llm[source_file].append((global_idx, chunk))

        layer2_map: Dict[int, ProcurementDecision] = {}
        for source_file, indexed in pending_for_llm.items():
            batch = self._layer2_llm_batch(source_file, indexed)
            layer2_map.update(batch)

        for sublist in pending_for_llm.values():
            for global_idx, chunk in sublist:
                l2 = layer2_map.get(global_idx) or ProcurementDecision(
                    action="pending_review",
                    target_kb="unknown",
                    confidence=0.0,
                    reason="Layer 2 未返回结果，默认待审",
                )
                final = self._layer3_schema(chunk, l2)
                meta = chunk.get("metadata", {})
                decisions.append(ChunkDecision(
                    chunk=chunk,
                    decision=final,
                    source_file=meta.get("source_file", "unknown"),
                    chunk_index=global_idx,
                ))

        decisions.sort(key=lambda d: d.chunk_index)
        return decisions

    def enrich_decisions_for_farmer(self, decisions: List[ChunkDecision]) -> List[ChunkDecision]:
        """Return decisions with accept chunks' metadata synced for cultivate_batch.

        Call this (or filter_accepted) before passing accepts to the farmer so
        suggested_category → metadata.suggested_value / document_category and
        source_file stay aligned with ChunkDecision.
        """
        return [enrich_chunk_decision_for_farmer(cd) for cd in decisions]

    def filter_accepted(self, decisions: List[ChunkDecision]) -> List[Dict]:
        """Return accepted chunks with farmer-handoff metadata applied."""
        return [
            enrich_chunk_decision_for_farmer(cd).chunk
            for cd in decisions
            if cd.decision.action == "accept"
        ]

    def write_logs(
        self,
        decisions: List[ChunkDecision],
        log_dir: Optional[Path] = None,
    ) -> Dict[str, int]:
        """Write per-action JSONL logs. Returns counts per action."""
        log_dir = log_dir or _KB_LOGS_DIR
        log_dir.mkdir(parents=True, exist_ok=True)

        buckets: Dict[str, List[Dict]] = defaultdict(list)
        for cd in decisions:
            entry = {
                "chunk_index": cd.chunk_index,
                "source_file": cd.source_file,
                "section_title": cd.chunk.get("metadata", {}).get("section_title", ""),
                "content_preview": (
                    cd.chunk.get("page_content") or cd.chunk.get("text") or ""
                )[:200],
                "action": cd.decision.action,
                "target_kb": cd.decision.target_kb,
                "confidence": cd.decision.confidence,
                "reason": cd.decision.reason,
                "reason_code": cd.decision.reason_code,
                "rule_layer": cd.decision.rule_layer,
                "rule_confidence": cd.decision.rule_confidence,
                "schema_gap": cd.decision.schema_gap,
                "suggested_value": cd.decision.suggested_value,
                "timestamp": datetime.now().isoformat(),
            }
            buckets[cd.decision.action].append(entry)

        file_map = {
            "reject": "reject_log.jsonl",
            "pending_review": "pending_review.jsonl",
            "staging": "schema_gaps.jsonl",
        }
        counts: Dict[str, int] = {}
        for action, entries in buckets.items():
            counts[action] = len(entries)
            if action == "accept":
                continue
            fname = file_map.get(action, f"{action}.jsonl")
            out_path = log_dir / fname
            with out_path.open("a", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.info("[采购员] 写入日志 %s: %d 条", fname, len(entries))

        counts.setdefault("accept", len(buckets.get("accept", [])))
        return counts

    def get_l2_cache_stats(self) -> Dict[str, int]:
        return {
            "hits": self._l2_cache_hits,
            "misses": self._l2_cache_misses,
        }

    # ── layers ────────────────────────────────────────────────────────────────

    def _layer1_mechanical(self, chunk: Dict) -> Optional[ProcurementDecision]:
        """L0 预清理 + L1 长度；通过则返回 None 进入 L2。"""
        from INAGENT.agents.procurement_pre_clean import (
            mechanical_pre_clean_chunk,
        )

        screen = mechanical_pre_clean_chunk(chunk)
        if screen.quality_flags:
            meta = chunk.setdefault("metadata", {})
            if isinstance(meta, dict):
                merged = set(meta.get("_quality_flags", []))
                merged.update(screen.quality_flags)
                meta["_quality_flags"] = sorted(merged)
        if screen.continue_to_layer2:
            return None
        if screen.decision_hint == "pending_review":
            return ProcurementDecision(
                action="pending_review",
                target_kb="unknown",
                confidence=screen.confidence,
                reason=screen.reason,
                reason_code=screen.reason_code,
                rule_layer=screen.rule_layer,
                rule_confidence=screen.confidence,
            )
        return ProcurementDecision(
            action="reject",
            target_kb="unknown",
            confidence=screen.confidence or 1.0,
            reason=screen.reason,
            reason_code=screen.reason_code,
            rule_layer=screen.rule_layer,
            rule_confidence=screen.confidence,
        )

    # 单批过大会导致单次 completion 极长（每段一条 JSON），网关/模型耗时陡增，表现为某批（如 cli_1-82 后半批）「卡住」。
    _LLM_BATCH_SIZE = 50

    def _layer2_llm_batch(
        self,
        source_file: str,
        indexed_chunks: List[Tuple[int, Dict]],
    ) -> Dict[int, ProcurementDecision]:
        """LLM calls per source_file, split into sub-batches. Returns {global_idx: decision}."""
        results: Dict[int, ProcurementDecision] = {}
        if not indexed_chunks:
            return results

        for batch_start in range(0, len(indexed_chunks), self._LLM_BATCH_SIZE):
            sub_batch = indexed_chunks[batch_start:batch_start + self._LLM_BATCH_SIZE]
            batch_num = batch_start // self._LLM_BATCH_SIZE + 1
            total_batches = (len(indexed_chunks) + self._LLM_BATCH_SIZE - 1) // self._LLM_BATCH_SIZE
            logger.info(
                "[采购员] LLM batch %d/%d (%s): %d chunks",
                batch_num, total_batches, source_file, len(sub_batch),
            )
            sub_results = self._layer2_llm_single_batch(source_file, sub_batch)
            results.update(sub_results)

        return results

    def _layer2_llm_single_batch(
        self,
        source_file: str,
        indexed_chunks: List[Tuple[int, Dict]],
    ) -> Dict[int, ProcurementDecision]:
        """Single LLM call for a sub-batch. Returns {global_idx: decision}."""
        results: Dict[int, ProcurementDecision] = {}
        local_chunks = [chunk for _, chunk in indexed_chunks]
        prompt = build_procurement_prompt(source_file, local_chunks)
        cache_key = self._build_l2_cache_key(source_file, prompt)

        cached_raw = self._load_l2_cache_raw(cache_key)
        if cached_raw is not None:
            self._l2_cache_hits += 1
            logger.info(
                "[采购员] L2 cache hit (%s): %d chunks",
                source_file,
                len(local_chunks),
            )
            parsed = self._parse_llm_json(cached_raw, len(local_chunks))
        else:
            self._l2_cache_misses += 1
            try:
                # 每子批仅保留 system，避免多批 step 堆叠触发上下文压缩 / 模型超长
                self._chat_agent.clear_memory()
                msg = BaseMessage.make_user_message(role_name="Operator", content=prompt)
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(self._chat_agent.step, msg)
                try:
                    response = future.result(timeout=_PROCUREMENT_STEP_TIMEOUT)
                except FutureTimeout as exc:
                    future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise TimeoutError(
                        f"采购 L2 调用超时（>{_PROCUREMENT_STEP_TIMEOUT}s）"
                    ) from exc
                executor.shutdown(wait=False, cancel_futures=True)
                raw = response.msgs[0].content if response.msgs else ""
                parsed = self._parse_llm_json(raw, len(local_chunks))
                self._store_l2_cache_raw(
                    cache_key=cache_key,
                    source_file=source_file,
                    prompt=prompt,
                    raw_response=raw,
                    expected_count=len(local_chunks),
                )
            except Exception as exc:
                logger.warning("[采购员] Layer 2 LLM 失败 (%s): %s", source_file, exc)
                for global_idx, _ in indexed_chunks:
                    results[global_idx] = ProcurementDecision(
                        action="pending_review",
                        target_kb="unknown",
                        confidence=0.0,
                        reason=f"LLM 调用异常: {exc}",
                    )
                return results

        for local_idx, (global_idx, _) in enumerate(indexed_chunks):
            item = parsed.get(local_idx)
            if item is None:
                results[global_idx] = ProcurementDecision(
                    action="pending_review",
                    target_kb="unknown",
                    confidence=0.0,
                    reason="LLM 未返回该片段评估结果",
                )
                continue
            confidence = float(item.get("confidence", 0.5))
            action: ActionType = item.get("action", "pending_review")
            if action not in ("accept", "reject", "pending_review", "staging"):
                action = "pending_review"
            if confidence < 0.6 and action in ("accept", "reject"):
                action = "pending_review"
            results[global_idx] = ProcurementDecision(
                action=action,
                target_kb=item.get("target_kb", "unknown"),
                confidence=confidence,
                reason=item.get("reason", ""),
                suggested_value=item.get("suggested_category"),
            )
        return results

    def _build_l2_cache_key(self, source_file: str, prompt: str) -> str:
        payload = {
            "schema_version": _L2_CACHE_SCHEMA_VERSION,
            "source_file": source_file,
            "product_name": self._product_name,
            "model_signature": self._model_signature,
            "prompt_source_signature": _PROCUREMENT_PROMPT_SOURCE_SIGNATURE,
            "prompt_hash": _hash_text(prompt),
        }
        return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _load_l2_cache_raw(self, cache_key: str) -> Optional[str]:
        if self._llm_cache_dir is None or self._llm_cache_force_refresh:
            return None
        cache_path = self._llm_cache_dir / f"{cache_key}.json"
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[采购员] L2 cache 读取失败 %s: %s", cache_path.name, exc)
            return None
        if payload.get("schema_version") != _L2_CACHE_SCHEMA_VERSION:
            return None
        raw_response = payload.get("raw_response")
        if not isinstance(raw_response, str) or not raw_response.strip():
            return None
        return raw_response

    def _store_l2_cache_raw(
        self,
        *,
        cache_key: str,
        source_file: str,
        prompt: str,
        raw_response: str,
        expected_count: int,
    ) -> None:
        if self._llm_cache_dir is None or self._llm_cache_read_only:
            return
        payload = {
            "schema_version": _L2_CACHE_SCHEMA_VERSION,
            "cache_key": cache_key,
            "source_file": source_file,
            "product_name": self._product_name,
            "model_signature": self._model_signature,
            "prompt_source_signature": _PROCUREMENT_PROMPT_SOURCE_SIGNATURE,
            "prompt_hash": _hash_text(prompt),
            "expected_count": expected_count,
            "raw_response": raw_response,
            "created_at": datetime.now().isoformat(),
        }
        cache_path = self._llm_cache_dir / f"{cache_key}.json"
        try:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[采购员] L2 cache 写入失败 %s: %s", cache_path.name, exc)

    def _layer3_schema(
        self, chunk: Dict, decision: ProcurementDecision
    ) -> ProcurementDecision:
        """Schema compatibility check. May promote action to 'staging'."""
        if decision.action in ("reject", "pending_review"):
            return decision

        meta = chunk.get("metadata", {})
        category = decision.suggested_value or meta.get("document_category", "")
        product_module = meta.get("product_module", "")

        if category and category not in DOCUMENT_CATEGORIES:
            return ProcurementDecision(
                action="staging",
                target_kb=decision.target_kb,
                confidence=decision.confidence,
                reason=f"document_category '{category}' 不在已知分类体系，等待 schema patch",
                schema_gap="new_category",
                suggested_value=category,
            )

        known_modules = set(self._registry.get("modules", {}).keys())
        if (
            product_module
            and product_module not in ("unknown", "")
            and known_modules
            and product_module not in known_modules
        ):
            return ProcurementDecision(
                action="staging",
                target_kb=decision.target_kb,
                confidence=decision.confidence,
                reason=f"product_module '{product_module}' 不在产品模块注册表，等待 schema patch",
                schema_gap="new_module",
                suggested_value=product_module,
            )

        return decision

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_llm_json(self, raw: str, expected: int) -> Dict[int, Dict]:
        """Parse LLM JSON array → {local_idx: item_dict}."""
        raw = raw.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
        if fence:
            raw = fence.group(1).strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            logger.warning("[采购员] LLM 响应中未找到 JSON 数组")
            return {}
        try:
            data = json.loads(raw[start: end + 1])
        except json.JSONDecodeError:
            try:
                import json_repair  # type: ignore
                data = json_repair.loads(raw[start: end + 1])
            except Exception:
                logger.warning("[采购员] LLM JSON 解析失败")
                return {}
        if not isinstance(data, list):
            return {}
        return {
            int(item["idx"]): item
            for item in data
            if isinstance(item, dict) and "idx" in item
        }

    def _load_registry(self, path: Optional[Path]) -> Dict:
        candidates = [
            path,
            _INAGENT_ROOT / "knowledge_base" / "product_modules_registry.json",
        ]
        for p in candidates:
            if p and p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}


# ── CLI smoke test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    快速冒烟测试：使用真实 LLM 跑一批样本 chunk，观察采购员的判断。
    运行方式：
        cd c:\\SynologyDrive\\INFOAGEN
        python -m INAGENT.agents.knowledge_procurement_agent
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from INAGENT.utils.env_utils import load_inagent_env
    from INAGENT.web.deps import get_llm_model

    load_inagent_env()
    model = get_llm_model()

    SAMPLE_CHUNKS = [
        {
            "page_content": "slb virtual http <name> <vip> <port>\n配置 HTTP 类型的虚拟服务。参数 name 为服务名称，vip 为虚拟 IP 地址，port 为监听端口（1-65535）。",
            "metadata": {"source_file": "cli.pdf", "section_title": "SLB 虚拟服务配置", "document_category": "cli/reference", "product_module": "SLB"},
        },
        {
            "page_content": "测试用例 #101：配置 HTTP 虚拟服务后，验证流量正常转发。步骤：1. 配置 VIP 2. 绑定 real server 3. 发起 HTTP 请求。预期结果：返回 200 OK。",
            "metadata": {"source_file": "Test_List_HTTP2.xlsx", "section_title": "HTTP SLB 基础功能", "document_category": "test/test_list", "product_module": "SLB"},
        },
        {
            "page_content": "目录\n1. 概述 ........ 1\n2. 配置说明 ........ 5\n3. 故障排查 ........ 12",
            "metadata": {"source_file": "app.pdf", "section_title": "目录", "document_category": "spec/design"},
        },
        {
            "page_content": "Copyright © 2024 InfosecOS. All Rights Reserved. 本文档所含信息属于保密信息，未经授权不得复制或传播。",
            "metadata": {"source_file": "cli.pdf", "section_title": "版权声明"},
        },
        {
            "page_content": "XXX子功能 CLI 测试用例模板\n用例编号：XXX-001\n测试功能：XXX\n测试步骤：YYY",
            "metadata": {"source_file": "XXX子功能 CLI 测试用例.xlsx", "section_title": "模板"},
        },
        {
            "page_content": "见第3章",
            "metadata": {"source_file": "app.pdf"},
        },
        {
            "page_content": "NSAE 设备支持通过 SNMP v3 进行监控集成，可配置 trap 接收端点，支持 MIB-II 标准对象标识符查询。",
            "metadata": {"source_file": "monitoring_guide.pdf", "section_title": "SNMP 监控集成", "document_category": "monitoring/integration", "product_module": "SLB"},
        },
    ]

    agent = KnowledgeProcurementAgent(model=model)
    decisions = agent.evaluate_batch(SAMPLE_CHUNKS)

    print("\n========== 采购员判断结果 ==========")
    for cd in decisions:
        d = cd.decision
        content_preview = (cd.chunk.get("page_content") or "")[:60].replace("\n", " ")
        print(f"[{cd.chunk_index}] {d.action:15s} | {d.target_kb:8s} | conf={d.confidence:.2f} | {d.reason}")
        print(f"     内容: {content_preview}...")
        if d.schema_gap:
            print(f"     schema_gap={d.schema_gap}, suggested={d.suggested_value}")
        print()

    import tempfile
    counts = agent.write_logs(decisions, log_dir=Path(tempfile.mkdtemp()))
    print("日志统计:", counts)
    accepted = agent.filter_accepted(decisions)
    print(f"通过入库: {len(accepted)} 个 chunk")
