# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Unit tests for chat route resolution (no live API)."""

import pytest

from llm_gateway.chat_router import normalize_model_name, resolve_chat_route


def test_normalize_strips_opencode_prefix():
    assert normalize_model_name("opencode-go/kimi-k2.5") == "kimi-k2.5"


def test_resolve_single_openai_fallback_default():
    openai = [
        {
            "id": "qwen-plus",
            "provider": "dashscope",
            "model": "qwen-plus",
            "api_key": "k",
            "base_url": "https://x",
        }
    ]
    r = resolve_chat_route(
        "unknown-model",
        openai_models=openai,
        anthropic_configs={},
        default_chat_model="qwen-plus",
    )
    assert r.kind == "openai_subset"
    assert r.openai_subset == openai


def test_resolve_anthropic_by_id():
    ant = {
        "minimax-m2.5": {
            "id": "minimax-m2.5",
            "provider": "opencode_go",
            "model": "minimax-m2.5",
            "api_key": "k",
            "messages_url": "https://opencode.ai/zen/go/v1",
        }
    }
    r = resolve_chat_route(
        "minimax-m2.5",
        openai_models=[],
        anthropic_configs=ant,
        default_chat_model="minimax-m2.5",
    )
    assert r.kind == "anthropic"
    assert r.anthropic_config["model"] == "minimax-m2.5"


def test_resolve_multi_key_base_id():
    openai = [
        {"id": "qwen-plus_key1", "provider": "d", "model": "qwen-plus", "api_key": "a", "base_url": "u"},
        {"id": "qwen-plus_key2", "provider": "d", "model": "qwen-plus", "api_key": "b", "base_url": "u"},
    ]
    r = resolve_chat_route(
        "qwen-plus",
        openai_models=openai,
        anthropic_configs={},
        default_chat_model="qwen-plus",
    )
    assert r.kind == "openai_subset"
    assert len(r.openai_subset) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
