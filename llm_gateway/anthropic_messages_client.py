# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Call Anthropic-compatible /v1/messages (OpenCode Go MiniMax). Map response to OpenAI chat shape."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


def _openai_messages_to_anthropic(
    messages: List[Dict[str, Any]],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    system_parts: List[str] = []
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        if role == "system":
            system_parts.append(text)
            continue
        if role not in ("user", "assistant"):
            role = "user"
        out.append({"role": role, "content": text})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


def _anthropic_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "thinking":
                    thinking_parts.append(str(block.get("thinking", "")))
                elif "text" in block:
                    text_parts.append(str(block["text"]))
            else:
                text_parts.append(str(block))
        out = "".join(text_parts).strip()
        if out:
            return out
        # MiniMax may return only thinking blocks if output was truncated before text.
        return "".join(thinking_parts).strip()
    return str(content) if content is not None else ""


async def anthropic_messages_chat_completion(
    *,
    http_client: httpx.AsyncClient,
    messages_url: str,
    api_key: str,
    model: str,
    openai_messages: List[Dict[str, Any]],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop: Optional[List[str]] = None,
    client_model_id: str = "",
    timeout: float = 600.0,
) -> ChatCompletion:
    """Non-streaming completion; returns OpenAI ChatCompletion."""
    system, anth_msgs = _openai_messages_to_anthropic(openai_messages)
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens if max_tokens is not None else 4096,
        "messages": anth_msgs,
    }
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if stop:
        body["stop_sequences"] = stop

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    url = messages_url.rstrip("/")
    if not url.endswith("/messages"):
        url = f"{url}/messages"

    t0 = time.time()
    resp = await http_client.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()
    elapsed = time.time() - t0

    text = _anthropic_text_content(raw.get("content"))
    usage = raw.get("usage") or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    total = in_tok + out_tok if (in_tok or out_tok) else 0

    stop_reason = raw.get("stop_reason") or "stop"
    oai_finish = "stop"
    if stop_reason in ("max_tokens", "length"):
        oai_finish = "length"

    msg = ChatCompletionMessage(role="assistant", content=text)
    choice = Choice(
        index=0,
        message=msg,
        finish_reason=oai_finish,
    )
    created = int(time.time())
    cid = raw.get("id") or f"chatcmpl-{created}"
    cc = ChatCompletion(
        id=cid,
        choices=[choice],
        created=created,
        model=client_model_id or model,
        object="chat.completion",
        usage=CompletionUsage(
            prompt_tokens=in_tok,
            completion_tokens=out_tok,
            total_tokens=total,
        ),
    )
    logger.info(
        "[AnthropicMessages] model=%s elapsed=%.3fs usage in=%s out=%s",
        model,
        elapsed,
        in_tok,
        out_tok,
    )
    return cc
