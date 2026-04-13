# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""采购文档入库管线 — 权威入口（MinerU / Office / TXT → reference / merge）。

架构约定：

- **本模块** 表示「原始文档 → 结构化块 → 落盘与合并」的 **采购** 侧编排；含 MinerU
  调用、前置页标记、``mineru.json`` 规则、可选批量 LLM 元数据与
  ``merge_knowledge_base`` 等（PDF/MinerU 见 ``mineru_procurement``，总编排见
  ``auto_convert.run_procurement_document_pipeline``）。
- 农民、农场主、质检员已拆为独立流程，不再由采购入口串行触发。

- **auto_convert** 模块同时提供 **农民工具函数**（如 ``_extract_chunk_metadata``、
  ``_extract_text_from_block``），供 ``KnowledgeFarmerAgent.cultivate_batch`` 等对
  **已存在 chunk** 做结构化；**不**通过本文件调用。

对外请使用::

    asyncio.run(main())

或显式::

    from INAGENT.data_tools.procurement_ingest import run_procurement_document_pipeline
    asyncio.run(run_procurement_document_pipeline())
"""
from __future__ import annotations

from INAGENT.data_tools.auto_convert import run_procurement_document_pipeline

__all__ = ["main", "run_procurement_document_pipeline"]


async def main() -> None:
    """采购管线主入口（与 ``auto_convert.main`` 历史行为一致）。"""
    await run_procurement_document_pipeline()
