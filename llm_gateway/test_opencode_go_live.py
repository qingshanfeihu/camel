#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Live probe of OpenCode Go upstream (no local gateway config change).

Loads OPENCODE_GO_API_KEY from environment (e.g. INAGENT/.env via dotenv).
Does not print the API key.

Usage (from repo root):
  python llm_gateway/test_opencode_go_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / "INAGENT" / ".env"

OPENAI_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"

OPENAI_MODELS = ["glm-5", "kimi-k2.5", "mimo-v2-pro", "mimo-v2-omni"]
ANTHROPIC_MODELS = ["minimax-m2.5", "minimax-m2.7"]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH, override=False)


def _key() -> str:
    k = (os.getenv("OPENCODE_GO_API_KEY") or "").strip()
    if not k:
        print("ERROR: OPENCODE_GO_API_KEY is not set (add to INAGENT/.env)", file=sys.stderr)
        sys.exit(2)
    return k


async def _probe_openai(client: httpx.AsyncClient, api_key: str, model: str) -> Tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        # Some models fill `reasoning` first; small max_tokens can yield empty `content`.
        "max_tokens": 256,
    }
    try:
        r = await client.post(OPENAI_URL, headers=headers, json=body, timeout=120.0)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:500]}"
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return False, f"no choices: {json.dumps(data)[:300]}"
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or ""
        preview = (content or reasoning or "")[:120]
        if not preview.strip():
            return False, "empty message.content and message.reasoning"
        return True, f"preview={preview!r}"
    except Exception as e:
        return False, str(e)


async def _probe_anthropic(client: httpx.AsyncClient, api_key: str, model: str) -> Tuple[bool, str]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "model": model,
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    try:
        r = await client.post(MESSAGES_URL, headers=headers, json=body, timeout=120.0)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:500]}"
        data = r.json()
        content = data.get("content")
        text = ""
        thinking = ""
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text += str(block.get("text", ""))
                elif block.get("type") == "thinking":
                    thinking += str(block.get("thinking", ""))
        elif isinstance(content, str):
            text = content
        preview = (text or thinking).strip()
        if not preview:
            usage = data.get("usage") or {}
            ot = int(usage.get("output_tokens", 0) or 0)
            if ot > 0:
                return True, f"no_text_but_output_tokens={ot}"
            return False, f"empty assistant text: keys={list(data.keys())}"
        return True, f"content_preview={preview[:80]!r}"
    except Exception as e:
        return False, str(e)


async def _main_async() -> int:
    _load_env()
    api_key = _key()
    results: List[Tuple[str, bool, str]] = []

    async with httpx.AsyncClient() as client:
        for m in OPENAI_MODELS:
            ok, detail = await _probe_openai(client, api_key, m)
            results.append((f"openai/{m}", ok, detail))
        for m in ANTHROPIC_MODELS:
            ok, detail = await _probe_anthropic(client, api_key, m)
            results.append((f"messages/{m}", ok, detail))

    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{status}\t{name}\t{detail}")

    print("---")
    print(f"total={len(results)} failed={failed}")
    return 1 if failed else 0


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
