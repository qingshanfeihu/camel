# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Tests for response_format passthrough and DashScope json_object keyword rule."""

import pytest

from llm_gateway.gateway import normalize_messages_for_response_format


def test_json_schema_response_format_passes_nested_dict():
    rf = {
        "type": "json_schema",
        "json_schema": {
            "name": "out",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        },
    }
    msgs = [{"role": "user", "content": "hi"}]
    out = normalize_messages_for_response_format(
        msgs, rf, ensure_json_keyword=False
    )
    assert out is msgs
    assert rf["json_schema"]["strict"] is True


def test_json_object_appends_hint_when_no_json_word():
    msgs = [{"role": "user", "content": "Say hello"}]
    out = normalize_messages_for_response_format(
        msgs,
        {"type": "json_object"},
        ensure_json_keyword=True,
    )
    assert len(out) == 1
    assert "json" in out[0]["content"].lower()
    assert msgs[0]["content"] == "Say hello"


def test_json_object_no_change_when_json_already_present():
    msgs = [{"role": "user", "content": "Return JSON for {}"}]
    out = normalize_messages_for_response_format(
        msgs,
        {"type": "json_object"},
        ensure_json_keyword=True,
    )
    assert out is msgs


def test_json_object_skipped_when_ensure_false():
    msgs = [{"role": "user", "content": "plain"}]
    out = normalize_messages_for_response_format(
        msgs,
        {"type": "json_object"},
        ensure_json_keyword=False,
    )
    assert out is msgs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
