# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Chat model resolution: map OpenAI-style request.model to gateway routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_PREFIX_STRIP = re.compile(r"^opencode-go/", re.IGNORECASE)
_KEY_SUFFIX = re.compile(r"_key\d+$", re.IGNORECASE)


def normalize_model_name(name: str) -> str:
    s = (name or "").strip()
    s = _PREFIX_STRIP.sub("", s)
    return s.strip()


def base_chat_id(model_id: str) -> str:
    return _KEY_SUFFIX.sub("", model_id or "")


@dataclass
class ChatResolution:
    """Result of resolving request.model to a concrete backend."""

    kind: str  # "openai_subset" | "anthropic"
    openai_subset: Optional[List[Dict[str, Any]]] = None
    anthropic_config: Optional[Dict[str, Any]] = None
    resolved_name: str = ""


def _index_openai_models(
    models: List[Dict[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    """Map normalized id / upstream model name -> list of MultiModelCaller entries (multi-key)."""
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    by_upstream: Dict[str, List[Dict[str, Any]]] = {}
    for m in models:
        mid = normalize_model_name(m.get("id", ""))
        if not mid:
            continue
        by_id.setdefault(mid, []).append(m)
        um = normalize_model_name(m.get("model", ""))
        if um:
            by_upstream.setdefault(um, []).append(m)
    return by_id, by_upstream


def _index_anthropic(
    configs: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_upstream: Dict[str, Dict[str, Any]] = {}
    for cid, cfg in configs.items():
        nid = normalize_model_name(cid)
        by_id[nid] = cfg
        um = normalize_model_name(cfg.get("model", ""))
        if um:
            by_upstream[um] = cfg
    return by_id, by_upstream


def _collect_openai_matches(
    norm: str,
    by_id: Dict[str, List[Dict[str, Any]]],
    by_upstream: Dict[str, List[Dict[str, Any]]],
    openai_models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """All OpenAI-route entries matching norm (id, upstream name, or base id for _key variants)."""
    seen = set()
    out: List[Dict[str, Any]] = []

    def add_list(entries: List[Dict[str, Any]]) -> None:
        for e in entries:
            k = id(e)
            if k not in seen:
                seen.add(k)
                out.append(e)

    if norm in by_id:
        add_list(by_id[norm])
    if norm in by_upstream:
        add_list(by_upstream[norm])

    # Match logical id against suffixed entries: qwen-plus -> qwen-plus_key1
    for m in openai_models:
        if base_chat_id(m.get("id", "")) == norm:
            add_list([m])

    return out


def resolve_chat_route(
    request_model: str,
    *,
    openai_models: List[Dict[str, Any]],
    anthropic_configs: Dict[str, Dict[str, Any]],
    default_chat_model: str,
) -> ChatResolution:
    """
    Resolve client model string to one backend.

    When multiple OpenAI entries share the same logical id (multi-key), returns all for failover.
    """
    norm = normalize_model_name(request_model)
    if not norm:
        norm = normalize_model_name(default_chat_model)

    aid, aup = _index_anthropic(anthropic_configs)
    if norm in aid:
        return ChatResolution(
            kind="anthropic",
            anthropic_config=aid[norm],
            resolved_name=norm,
        )
    if norm in aup:
        cfg = aup[norm]
        return ChatResolution(
            kind="anthropic",
            anthropic_config=cfg,
            resolved_name=norm,
        )

    oid, oup = _index_openai_models(openai_models)
    matches = _collect_openai_matches(norm, oid, oup, openai_models)
    if matches:
        return ChatResolution(
            kind="openai_subset",
            openai_subset=matches,
            resolved_name=norm,
        )

    # Fallback to default_chat_model
    dnorm = normalize_model_name(default_chat_model)
    if dnorm in aid:
        return ChatResolution(
            kind="anthropic",
            anthropic_config=aid[dnorm],
            resolved_name=dnorm,
        )
    if dnorm in aup:
        return ChatResolution(
            kind="anthropic",
            anthropic_config=aup[dnorm],
            resolved_name=dnorm,
        )

    matches = _collect_openai_matches(dnorm, oid, oup, openai_models)
    if matches:
        return ChatResolution(
            kind="openai_subset",
            openai_subset=matches,
            resolved_name=dnorm,
        )

    return ChatResolution(kind="unresolved", resolved_name=norm)
