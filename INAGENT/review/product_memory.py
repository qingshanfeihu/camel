# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""ProductArchitectureMemory — 产品架构长期记忆。

从 enriched CLI keyword graph 中提取各模块技术画像，
写入 CAMEL LongtermAgentMemory (VectorDBBlock + ChatHistoryBlock)，
使 Worker Agent 在评审过程中能通过语义检索调取产品架构知识。

Usage:
    memory = build_product_memory(
        token_counter=model_backend.token_counter,
        embedding=embedding_model,   # OpenAICompatibleEmbedding instance
    )
    agent = ChatAgent(..., memory=memory)
"""
from __future__ import annotations

import logging
from typing import Optional

from camel.memories import LongtermAgentMemory, MemoryRecord
from camel.memories.blocks import ChatHistoryBlock, VectorDBBlock
from camel.memories.context_creators import ScoreBasedContextCreator
from camel.messages import BaseMessage
from camel.types import OpenAIBackendRole

logger = logging.getLogger(__name__)


def _format_module_profile(node: dict) -> str:
    """Format a single enriched module node into a text profile."""
    label = node.get("label", node.get("id", "?"))
    parts = [f"[{label}模块技术画像]"]

    proto = node.get("protocol_stack", [])
    if proto:
        parts.append(f"协议栈: {', '.join(proto)}")

    af = node.get("address_family", [])
    if af:
        parts.append(f"地址族: {', '.join(af)}")

    layer = node.get("layer", "")
    if layer:
        parts.append(f"OSI层级: {layer}")

    iface = node.get("interface_types", [])
    if iface:
        parts.append(f"管理接口: {', '.join(iface)}")

    related = node.get("related_modules", [])
    if related:
        parts.append(f"关联模块: {', '.join(related[:15])}")

    tags = node.get("feature_tags", [])
    if tags:
        parts.append(f"功能标签: {', '.join(tags)}")

    cmd_count = node.get("commands_count", 0)
    parts.append(f"CLI命令数: {cmd_count}")

    return "\n".join(parts)


def build_product_memory(
    token_counter,
    token_limit: int = 4096,
    retrieve_limit: int = 5,
    agent_id: Optional[str] = None,
    embedding=None,
) -> LongtermAgentMemory:
    """Build a LongtermAgentMemory pre-populated with product architecture.

    Args:
        token_counter: A BaseTokenCounter instance (from model_backend).
        token_limit: Token limit for context creation.
        retrieve_limit: Max vector search results per query.
        agent_id: Optional agent identifier for memory records.
        embedding: BaseEmbedding instance (e.g. OpenAICompatibleEmbedding).

    Returns:
        A LongtermAgentMemory with all module tech profiles written to the
        VectorDB block for semantic retrieval during agent reasoning.

    Raises:
        ValueError: If embedding is None.
    """
    if embedding is None:
        raise ValueError("build_product_memory: embedding 参数必须提供，不允许退化为无向量检索模式")
    context_creator = ScoreBasedContextCreator(
        token_counter=token_counter,
        token_limit=token_limit,
    )

    vector_db_block = VectorDBBlock(embedding=embedding)

    memory = LongtermAgentMemory(
        context_creator=context_creator,
        vector_db_block=vector_db_block,
        retrieve_limit=retrieve_limit,
        agent_id=agent_id,
    )

    # Load enriched module nodes from CLI graph store
    try:
        from INAGENT.rag.cli_graph_store import get_cli_graph_store
        store = get_cli_graph_store()
        store._ensure_loaded()
    except Exception as e:
        raise RuntimeError("build_product_memory: CLI graph 加载失败") from e

    count = 0
    for _nid, node in store._module_nodes.items():
        profile_text = _format_module_profile(node)
        if not profile_text.strip():
            continue

        record = MemoryRecord(
            message=BaseMessage.make_user_message(
                role_name="ProductArchitect",
                content=profile_text,
            ),
            role_at_backend=OpenAIBackendRole.USER,
            agent_id=agent_id or "",
        )
        memory.write_records([record])
        count += 1

    logger.info("Product memory: wrote %d module profiles", count)
    return memory
