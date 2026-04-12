# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""L2 采购员结构化输出：与 ChatAgent.step(..., response_format=...) 对齐。"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from INAGENT.rag.knowledge_config import DOCUMENT_CATEGORIES

_DOC_CAT_SET = frozenset(DOCUMENT_CATEGORIES)


class ProcurementL2Row(BaseModel):
    """单片段评估结果。document_category 仅允许为已知文档类型路径，与功能模块解耦。"""

    idx: int = Field(..., ge=0, description="与 prompt 中 [idx] 一致，从 0 递增")
    action: Literal["accept", "reject", "pending_review", "staging"]
    target_kb: Literal["product", "test", "unknown"] = "unknown"
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    reason: str = ""
    document_category: Optional[str] = Field(
        None,
        description=(
            "accept 时必须为【已知文档分类体系】中的确切一项（如 cli/reference）；"
            "禁止填功能模块名（nat/slb 等）。reject/pending_review 可为空。"
        ),
    )
    proposed_new_document_category: Optional[str] = Field(
        None,
        description="action=staging 时：建议新增文档类型名称（尚未在白名单内）。",
    )
    product_module_hint: Optional[str] = Field(
        None,
        description="功能模块提示（如 nat、slb），勿写入 document_category。",
    )

    @field_validator("document_category", "proposed_new_document_category", mode="before")
    @classmethod
    def _strip_opt(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s or None
        return v

    @model_validator(mode="after")
    def _accept_needs_whitelist_category(self) -> "ProcurementL2Row":
        if self.action == "accept":
            if not self.document_category or self.document_category not in _DOC_CAT_SET:
                raise ValueError(
                    "accept 要求 document_category 为 DOCUMENT_CATEGORIES 中的确切一项"
                )
        return self


class ProcurementL2Batch(BaseModel):
    """整批响应：顶层 JSON 对象，含 items 数组。"""

    items: List[ProcurementL2Row] = Field(
        ...,
        description="与本次 prompt 片段一一对应，按 idx 覆盖 0..N-1",
    )
