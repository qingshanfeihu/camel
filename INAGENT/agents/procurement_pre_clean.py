# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Procurement mechanical pre-clean (L0) — unified rule screen before LLM (L2).

Pipeline placement (conceptual):
  auto_convert / ingest → **采购 L0** → L1 长度（与此模块合并）→ L2 LLM → L3 元数据

L0 能力：与全库一致的「垃圾/版式片段」启发式（见 ``chunk_text_quality``），避免无价值块进入
LLM 批处理。与 auto_convert 的目录/前置页过滤、农场主 discard 规则在**职责上衔接**：
  - 解析阶段：auto_convert 丢页/空块；
  - 准入阶段：采购 L0 丢块；
  - 结构后处理：农场主对 uncovered 垃圾发 discard。

此模块不调用 LLM；仅返回是否继续进入 L2。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal

from INAGENT.utils.chunk_text_quality import (
    detect_quality_flags,
    is_garbage_page_content,
)

# 与 ``KnowledgeProcurementAgent`` 历史行为一致：strip 后长度门槛
MIN_CHUNK_CHARS = 50
FRONTMATTER_REJECT_CONFIDENCE = 0.95
FRONTMATTER_PENDING_CONFIDENCE = 0.75


@dataclass(frozen=True)
class MechanicalPreCleanResult:
    """Result of :func:`mechanical_pre_clean_chunk`."""

    decision_hint: Literal["pass", "reject", "pending_review"]
    reason: str = ""
    reason_code: str = ""
    confidence: float = 0.0
    rule_layer: str = "L0"
    quality_flags: List[str] | None = None

    @property
    def continue_to_layer2(self) -> bool:
        return self.decision_hint == "pass"


def mechanical_pre_clean_chunk(chunk: Dict[str, Any]) -> MechanicalPreCleanResult:
    """Apply rule-based pre-clean. If not ``continue_to_layer2``, caller should reject.

    Order: (1) minimum length (2) garbage / boilerplate heuristics for remaining text.
    """
    content = (chunk.get("page_content") or chunk.get("text") or "").strip()
    n = len(content)
    meta = chunk.get("metadata", {}) if isinstance(chunk.get("metadata", {}), dict) else {}
    section_title = str(meta.get("section_title", ""))

    if n < MIN_CHUNK_CHARS:
        return MechanicalPreCleanResult(
            decision_hint="reject",
            reason=f"内容长度 {n} 字符，低于 {MIN_CHUNK_CHARS} 字符最低门槛",
            reason_code="too_short",
            confidence=1.0,
            rule_layer="L1",
            quality_flags=["extremely_short"] if n <= 25 else [],
        )

    quality_flags = detect_quality_flags(content, section_title)

    if is_garbage_page_content(content):
        return MechanicalPreCleanResult(
            decision_hint="reject",
            reason=(
                "预清理（机械）：无检索价值的版式/声明性片段（与全库垃圾启发式一致）"
            ),
            reason_code="garbage_boilerplate",
            confidence=1.0,
            rule_layer="L0",
            quality_flags=quality_flags,
        )

    is_frontmatter = bool(meta.get("is_frontmatter"))
    frontmatter_confidence = float(meta.get("frontmatter_confidence", 0.0) or 0.0)
    if is_frontmatter:
        if frontmatter_confidence >= FRONTMATTER_REJECT_CONFIDENCE:
            return MechanicalPreCleanResult(
                decision_hint="reject",
                reason="预清理（frontmatter）：高置信命中前置页，直接拒绝",
                reason_code="frontmatter_high_confidence",
                confidence=FRONTMATTER_REJECT_CONFIDENCE,
                rule_layer="L0",
                quality_flags=quality_flags,
            )
        return MechanicalPreCleanResult(
            decision_hint="pending_review",
            reason="预清理（frontmatter）：前置页命中但置信度不足，转人工待审",
            reason_code="frontmatter_medium_confidence",
            confidence=max(frontmatter_confidence, FRONTMATTER_PENDING_CONFIDENCE),
            rule_layer="L0",
            quality_flags=quality_flags,
        )

    if "title_content_mismatch" in quality_flags:
        return MechanicalPreCleanResult(
            decision_hint="pending_review",
            reason="预清理（质量）：标题与正文关联弱，转人工待审",
            reason_code="title_content_mismatch",
            confidence=0.7,
            rule_layer="L0",
            quality_flags=quality_flags,
        )

    return MechanicalPreCleanResult(
        decision_hint="pass",
        reason="",
        reason_code="pass",
        confidence=0.0,
        rule_layer="L0",
        quality_flags=quality_flags,
    )


def peek_content_for_debug(chunk: Dict[str, Any]) -> str:
    """Strip and return primary body text (for logging/tests)."""
    return (chunk.get("page_content") or chunk.get("text") or "").strip()
