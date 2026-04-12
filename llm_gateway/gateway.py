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

"""FastAPI Gateway Service for Unified LLM Access."""

import asyncio
import httpx
import json
import logging
import os
import re
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# Load .env before any config that depends on environment variables
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "INAGENT", ".env",
    )
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
except ImportError:
    pass

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_gateway.rate_limiter import UnifiedRateLimiter
from llm_gateway.multi_model_caller import MultiModelCaller
from llm_gateway.chat_router import normalize_model_name, resolve_chat_route
from llm_gateway.anthropic_messages_client import anthropic_messages_chat_completion


def _force_utf8_console() -> None:
    """Force UTF-8 stdout/stderr on Windows terminals."""
    if os.name != "nt":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_console()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Check SSL verification setting
SSL_VERIFY_STR = os.getenv("SILICONFLOW_SSL_VERIFY", "true").lower()
SSL_VERIFY = SSL_VERIFY_STR not in ("false", "0", "no")

if not SSL_VERIFY:
    logger.warning("[Gateway] SSL verification is disabled")


class GatewayConfig:
    """Gateway configuration manager."""
    
    def __init__(self, config_path: str):
        """Load configuration from YAML file."""
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # Replace environment variables
        self.config = self._replace_env_vars(self.config)
        
        logger.info(f"[GatewayConfig] Loaded configuration from {config_path}")
    
    def _replace_env_vars(self, obj: Any) -> Any:
        """Recursively replace ${VAR} with environment variables."""
        if isinstance(obj, dict):
            return {k: self._replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._replace_env_vars(item) for item in obj]
        elif isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            var_name = obj[2:-1]
            value = os.getenv(var_name, "")
            if not value:
                logger.warning(f"[GatewayConfig] Environment variable {var_name} not set")
            return value
        return obj
    
    def get(self, *keys, default=None):
        """Get nested configuration value."""
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value


def _text_for_json_keyword_scan(msg: Dict[str, Any]) -> str:
    """Flatten message content to plain text for 'json' substring check."""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: List[str] = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return " ".join(parts)
    return str(c or "")


def _ensure_json_keyword_for_json_object_enabled() -> bool:
    """DashScope json_object: messages must contain 'json' (case-insensitive)."""
    env = (os.getenv("LLM_GATEWAY_ENSURE_JSON_KEYWORD") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    if config is not None:
        return bool(
            config.get("routing", "ensure_json_keyword_for_json_object", default=True)
        )
    return True


def normalize_messages_for_response_format(
    messages: List[Dict[str, Any]],
    response_format: Optional[Dict[str, Any]],
    *,
    ensure_json_keyword: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Apply gateway-side rules before forwarding to OpenAI-compatible upstream.

    - Passes through full ``response_format`` (``json_object`` or nested
      ``json_schema`` / ``strict``) unchanged to the SDK; callers must use a
      dict shape compatible with the upstream (e.g. DashScope, OpenAI).
    - For ``type: json_object`` only: when *ensure_json_keyword* is True and no
      message text contains ``json``, append a short hint so Alibaba Cloud
      Model Studio's requirement is satisfied (see json-mode docs).
    """
    if ensure_json_keyword is None:
        ensure_json_keyword = _ensure_json_keyword_for_json_object_enabled()

    if not messages or not response_format or not ensure_json_keyword:
        return messages

    rf_type = str(response_format.get("type", "") or "").lower()
    if rf_type != "json_object":
        return messages

    combined = " ".join(
        _text_for_json_keyword_scan(m)
        for m in messages
        if m.get("role") in ("system", "user", "assistant", "tool")
    )
    if "json" in combined.lower():
        return messages

    hint = "(Respond using JSON.)"
    out: List[Dict[str, Any]] = [dict(m) for m in messages]

    def augment_at(idx: int) -> None:
        msg = out[idx]
        role = msg.get("role")
        c = msg.get("content")
        if role == "system" and isinstance(c, str):
            out[idx] = {
                **msg,
                "content": f"{c}\n{hint}" if c.strip() else hint,
            }
        elif role == "user" and isinstance(c, str):
            out[idx] = {
                **msg,
                "content": f"{c}\n{hint}" if c.strip() else hint,
            }

    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "system" and isinstance(out[i].get("content"), str):
            augment_at(i)
            logger.info(
                "[Gateway] ensure_json_keyword: appended hint to system message "
                "(DashScope json_object)"
            )
            return out
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user" and isinstance(out[i].get("content"), str):
            augment_at(i)
            logger.info(
                "[Gateway] ensure_json_keyword: appended hint to user message "
                "(DashScope json_object)"
            )
            return out

    logger.warning(
        "[Gateway] ensure_json_keyword: no string system/user message to patch; "
        "upstream may reject json_object (messages must mention 'json')"
    )
    return messages


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[List[str]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0
    frequency_penalty: Optional[float] = 0
    logit_bias: Optional[Dict[str, float]] = None
    # json_object | json_schema (nested name/schema/strict per OpenAI / upstream)
    response_format: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    user: Optional[str] = None


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request."""
    model: str
    input: str | List[str]
    encoding_format: Optional[str] = "float"
    user: Optional[str] = None


class RerankRequest(BaseModel):
    """SiliconFlow-compatible rerank request."""
    model: str
    query: str
    documents: List[str]
    top_n: Optional[int] = None


# Global instances
config: Optional[GatewayConfig] = None
rate_limiter: Optional[UnifiedRateLimiter] = None
chat_caller: Optional[MultiModelCaller] = None
embedding_clients: Dict[str, AsyncOpenAI] = {}
embedding_configs: Dict[str, Dict[str, Any]] = {}
reranker_clients: Dict[str, AsyncOpenAI] = {}
reranker_http_clients: Dict[str, httpx.AsyncClient] = {}
reranker_configs: Dict[str, Dict[str, Any]] = {}
anthropic_chat_configs: Dict[str, Dict[str, Any]] = {}
anthropic_http_client: Optional[httpx.AsyncClient] = None
KNOWN_CHAT_MODEL_NAMES: set = set()

from datetime import datetime, date, timedelta
from collections import defaultdict

request_stats = {
    "chat": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
    "embeddings": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
    "rerank": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
}

_usage_by_day: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(lambda: {
    "count": 0, "errors": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
}))

_usage_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {
    "count": 0, "errors": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
})

_gateway_start_time: float = time.time()


def _usage_bucket(model_id: str, provider: str = "", metric_key: str = "") -> str:
    """Usage aggregation key; optional provider prefix when routing.metrics_usage_bucket_include_provider."""
    use_p = False
    if config is not None:
        use_p = bool(
            config.get("routing", "metrics_usage_bucket_include_provider", default=False)
        )
    mid = (model_id or "").strip() or metric_key
    if use_p and provider:
        return f"{provider}:{mid}"
    return mid


def _record_request(
    metric_key: str,
    elapsed: float,
    is_error: bool = False,
    model_id: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    provider: str = "",
) -> None:
    stats = request_stats.get(metric_key)
    if stats is None:
        return
    stats["count"] += 1
    if is_error:
        stats["errors"] += 1
    stats["latencies"].append(elapsed)

    bucket = _usage_bucket(model_id, provider, metric_key)
    today = date.today().isoformat()

    day_entry = _usage_by_day[today][bucket]
    day_entry["count"] += 1
    if is_error:
        day_entry["errors"] += 1
    day_entry["prompt_tokens"] += prompt_tokens
    day_entry["completion_tokens"] += completion_tokens
    day_entry["total_tokens"] += total_tokens

    totals = _usage_totals[bucket]
    totals["count"] += 1
    if is_error:
        totals["errors"] += 1
    totals["prompt_tokens"] += prompt_tokens
    totals["completion_tokens"] += completion_tokens
    totals["total_tokens"] += total_tokens


def _mask_api_key(api_key: str) -> str:
    if not api_key:
        return "(empty)"
    trimmed = api_key.strip()
    if len(trimmed) <= 8:
        return f"{trimmed[:2]}***{trimmed[-2:]}(len={len(trimmed)})"
    return f"{trimmed[:4]}***{trimmed[-4:]}(len={len(trimmed)})"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for FastAPI app."""
    global config, rate_limiter, chat_caller, embedding_clients, reranker_clients
    global anthropic_chat_configs, anthropic_http_client, KNOWN_CHAT_MODEL_NAMES
    
    logger.info("=" * 80)
    logger.info("[Gateway] Starting INFOAGEN LLM Gateway Service")
    logger.info("=" * 80)
    
    # Load configuration
    config_path = os.getenv("GATEWAY_CONFIG", "llm_gateway/config.yaml")
    config = GatewayConfig(config_path)
    
    # Initialize rate limiter
    rate_limiter = UnifiedRateLimiter()
    
    # Set global limits if enabled
    if config.get("global_limits", "enabled", default=False):
        global_rpm = config.get("global_limits", "rpm", default=3000)
        global_tpm = config.get("global_limits", "tpm", default=150000)
        rate_limiter.set_global_limits(global_rpm, global_tpm)
    
    # Get timeout configuration (with OpenAI SDK retries disabled, these are single-attempt timeouts)
    chat_timeout = config.get("request_timeout", "chat", default=60)
    embedding_timeout = config.get("request_timeout", "embedding", default=60)
    reranker_timeout = config.get("request_timeout", "reranker", default=60)
    
    logger.info(f"[Gateway] Timeouts: chat={chat_timeout}s, embedding={embedding_timeout}s, reranker={reranker_timeout}s (SDK retries disabled)")
    
    # Initialize multi-model caller for chat (default balance = one model per request, no wasted RPM/TPM)
    calling_mode = config.get("calling_mode", default="balance")
    chat_caller = MultiModelCaller(mode=calling_mode, rate_limiter=rate_limiter, default_timeout=chat_timeout)
    
    # Load providers
    providers = config.get("providers", default={})
    anthropic_chat_configs.clear()
    if anthropic_http_client is not None:
        try:
            await anthropic_http_client.aclose()
        except Exception:
            pass
        anthropic_http_client = None

    for provider_name, provider_config in providers.items():
        if not provider_config.get("enabled", True):
            logger.info(f"[Gateway] Provider {provider_name} is disabled, skipping")
            continue
        
        base_url = provider_config.get("base_url") or provider_config.get(
            "messages_base_url"
        )
        if not base_url:
            logger.warning(
                "[Gateway] Provider %s has no base_url/messages_base_url, skipping",
                provider_name,
            )
            continue
        
        # Support both single api_key and multiple api_keys for load balancing
        # 支持单个 api_key 或多个 api_keys 用于负载均衡
        api_key = provider_config.get("api_key")  # Legacy single key
        api_keys = provider_config.get("api_keys", [])  # New multi-key support
        
        # If api_keys array exists, use it; otherwise fall back to single api_key
        if api_keys:
            logger.info(f"[Gateway] Provider {provider_name} has {len(api_keys)} API keys for load balancing")
        elif api_key:
            api_keys = [api_key]  # Convert single key to array for uniform processing
        else:
            logger.warning(f"[Gateway] Provider {provider_name} has no API key, skipping")
            continue

        masked_keys = ", ".join(_mask_api_key(key) for key in api_keys)
        logger.info(
            "[Gateway] Provider %s API key(s): %s",
            provider_name,
            masked_keys,
        )
        
        # Add provider-level rate limit
        provider_limits = provider_config.get("limits", {})
        prpm = provider_limits.get("rpm", 1000)
        ptpm = provider_limits.get("tpm", 50000)
        if provider_limits and (prpm > 0 or ptpm > 0):
            rate_limiter.set_provider_limits(provider_name, prpm, ptpm)
        
        # Add chat models
        # For multi-key load balancing: each key gets its own model instance
        # 多key负载均衡：每个key创建独立的模型实例，实现账号级负载
        for model_config in provider_config.get("chat_models", []):
            if not model_config.get("enabled", True):
                continue
            
            model_id_base = model_config["id"]
            model_name = model_config["model"]
            
            # Add model-level rate limit (per account if multi-key)
            model_limits = model_config.get("limits", {})
            api_style = (model_config.get("api_style") or "openai_chat").lower()

            if api_style == "anthropic_messages":
                msgs_base = provider_config.get("messages_base_url") or base_url
                if not msgs_base:
                    logger.warning(
                        "[Gateway] Skip anthropic chat %s: no messages_base_url",
                        model_id_base,
                    )
                    continue
                mrpm = model_limits.get("rpm", 0)
                mtpm = model_limits.get("tpm", 0)
                if mrpm > 0 or mtpm > 0:
                    rate_limiter.set_model_limits(model_id_base, mrpm, mtpm)
                anthropic_chat_configs[model_id_base] = {
                    "id": model_id_base,
                    "provider": provider_name,
                    "model": model_name,
                    "api_key": api_keys[0],
                    "messages_url": str(msgs_base).rstrip("/"),
                }
                logger.info(
                    "[Gateway] Registered anthropic_messages chat: %s (%s)",
                    model_id_base,
                    model_name,
                )
                continue

            # Create a model instance for each API key
            for key_index, current_api_key in enumerate(api_keys):
                # Generate unique model ID for each API key
                if len(api_keys) > 1:
                    model_id = f"{model_id_base}_key{key_index+1}"
                    logger.info(f"[Gateway] Multi-key model: {model_id} using account {key_index+1}/{len(api_keys)}")
                else:
                    model_id = model_id_base
                
                # Set per-key rate limit
                mrpm = model_limits.get("rpm", 1000)
                mtpm = model_limits.get("tpm", 50000)
                if mrpm > 0 or mtpm > 0:
                    rate_limiter.set_model_limits(model_id, mrpm, mtpm)
                
                # Add to multi-model caller
                chat_caller.add_model({
                    "id": model_id,
                    "provider": provider_name,
                    "api_key": current_api_key,
                    "base_url": base_url,
                    "model": model_name,
                    "rpm": mrpm,
                    "tpm": mtpm
                })
        
        # Initialize embedding clients (use first API key for embeddings)
        # Embeddings typically don't need multi-account load balancing
        primary_api_key = api_keys[0]
        for model_config in provider_config.get("embedding_models", []):
            if not model_config.get("enabled", True):
                continue
            
            model_id = model_config["id"]
            
            # Create httpx client with SSL verification setting
            if not SSL_VERIFY:
                http_client = httpx.AsyncClient(verify=False, timeout=embedding_timeout)
            else:
                http_client = httpx.AsyncClient(timeout=embedding_timeout)
            
            # CRITICAL: max_retries=0 to disable OpenAI SDK built-in retries
            embedding_clients[model_id] = AsyncOpenAI(
                api_key=primary_api_key,
                base_url=base_url,
                http_client=http_client,
                timeout=embedding_timeout,
                max_retries=0  # Disable SDK retries
            )
            embedding_configs[model_id] = {
                "provider": provider_name,
                "model": model_config.get("model", model_id),
            }
            
            # Add model-level rate limit
            model_limits = model_config.get("limits", {})
            erpm = model_limits.get("rpm", 2000)
            etpm = model_limits.get("tpm", 500000)
            if erpm > 0 or etpm > 0:
                rate_limiter.set_model_limits(model_id, erpm, etpm)
            
            logger.info(f"[Gateway] Registered embedding model: {model_id} ({model_config['model']})")
        
        # Initialize reranker clients
        for model_config in provider_config.get("reranker_models", []):
            if not model_config.get("enabled", True):
                continue
            
            model_id = model_config["id"]
            
            # Create httpx client with SSL verification setting
            if not SSL_VERIFY:
                http_client = httpx.AsyncClient(verify=False, timeout=reranker_timeout)
            else:
                http_client = httpx.AsyncClient(timeout=reranker_timeout)
            
            # CRITICAL: max_retries=0 to disable OpenAI SDK built-in retries
            reranker_clients[model_id] = AsyncOpenAI(
                api_key=primary_api_key,
                base_url=base_url,
                http_client=http_client,
                timeout=reranker_timeout,
                max_retries=0  # Disable SDK retries
            )
            reranker_http_clients[model_id] = http_client
            reranker_configs[model_id] = {
                "provider": provider_name,
                "model": model_config.get("model", model_id),
                "api_key": primary_api_key,
                "base_url": base_url.rstrip("/"),
            }
            
            # Add model-level rate limit
            model_limits = model_config.get("limits", {})
            rrpm = model_limits.get("rpm", 2000)
            rtpm = model_limits.get("tpm", 500000)
            if rrpm > 0 or rtpm > 0:
                rate_limiter.set_model_limits(model_id, rrpm, rtpm)
            
            logger.info(f"[Gateway] Registered reranker model: {model_id} ({model_config['model']})")
    
    if anthropic_chat_configs:
        if not SSL_VERIFY:
            anthropic_http_client = httpx.AsyncClient(verify=False, timeout=chat_timeout)
        else:
            anthropic_http_client = httpx.AsyncClient(timeout=chat_timeout)

    KNOWN_CHAT_MODEL_NAMES.clear()
    for m in chat_caller.models:
        KNOWN_CHAT_MODEL_NAMES.add(normalize_model_name(m["id"]))
        um = normalize_model_name(m.get("model", ""))
        if um:
            KNOWN_CHAT_MODEL_NAMES.add(um)
    for cid, acfg in anthropic_chat_configs.items():
        KNOWN_CHAT_MODEL_NAMES.add(normalize_model_name(cid))
        um = normalize_model_name(acfg.get("model", ""))
        if um:
            KNOWN_CHAT_MODEL_NAMES.add(um)

    _n_routes = len(chat_caller.models) + len(anthropic_chat_configs)
    logger.info(
        "[Gateway] Chat routes: openai=%s anthropic=%s resolve_when_multi=%s",
        len(chat_caller.models),
        len(anthropic_chat_configs),
        _n_routes > 1,
    )

    logger.info("=" * 80)
    logger.info("[Gateway] Initialization complete")
    logger.info(f"[Gateway] Mode: {calling_mode}")
    logger.info(f"[Gateway] Chat models: {len(chat_caller.models)}")
    logger.info(f"[Gateway] Embedding models: {len(embedding_clients)}")
    logger.info(f"[Gateway] Reranker models: {len(reranker_clients)}")
    logger.info("=" * 80)
    
    yield

    for client in embedding_clients.values():
        await client.close()
    for client in reranker_clients.values():
        await client.close()
    for client in reranker_http_clients.values():
        await client.aclose()
    if anthropic_http_client is not None:
        await anthropic_http_client.aclose()
        anthropic_http_client = None

    logger.info("[Gateway] Shutting down...")


# Create FastAPI app
app = FastAPI(
    title="INFOAGEN LLM Gateway",
    description="Unified LLM Gateway with Multi-Model Support",
    version="0.1.0",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "INFOAGEN LLM Gateway",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "mode": config.get("calling_mode", default="balance"),
        "chat_models": len(chat_caller.models),
        "chat_models_anthropic": len(anthropic_chat_configs),
        "embedding_models": len(embedding_clients),
        "reranker_models": len(reranker_clients),
        "chat_multi_route": (
            len(chat_caller.models) + len(anthropic_chat_configs)
        ) > 1,
    }


@app.get("/metrics")
async def metrics():
    """Get rate limiter metrics."""
    stats = rate_limiter.get_all_stats()
    return JSONResponse(content=stats)


@app.get("/metrics/requests")
async def request_metrics():
    """Get request-level metrics."""
    payload: Dict[str, Any] = {}
    for key, stats in request_stats.items():
        latencies = list(stats["latencies"])
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            p95_index = max(0, int(len(latencies) * 0.95) - 1)
            p95_latency = sorted(latencies)[p95_index]
        else:
            avg_latency = 0.0
            p95_latency = 0.0
        payload[key] = {
            "count": stats["count"],
            "errors": stats["errors"],
            "avg_latency": round(avg_latency, 4),
            "p95_latency": round(p95_latency, 4),
        }
    return JSONResponse(content=payload)


@app.get("/metrics/usage")
async def usage_metrics(days: int = 30):
    """Token usage breakdown by model and day.

    Query params:
        days: number of past days to include (default 30)
    """
    today = date.today()
    start_date = today - timedelta(days=days - 1)

    daily: Dict[str, Dict[str, Any]] = {}
    weekly: Dict[str, Dict[str, int]] = defaultdict(lambda: {
        "count": 0, "errors": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    })

    for day_str, models in sorted(_usage_by_day.items()):
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            continue
        if d < start_date:
            continue
        daily[day_str] = dict(models)

        week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        for _model, stats in models.items():
            w = weekly[week_key]
            for k in ("count", "errors", "prompt_tokens", "completion_tokens", "total_tokens"):
                w[k] += stats.get(k, 0)

    uptime_s = time.time() - _gateway_start_time
    uptime_h = round(uptime_s / 3600, 2)

    routing_meta = {}
    if config is not None:
        routing_meta = {
            "default_chat_model": config.get(
                "routing", "default_chat_model", default="qwen-plus"
            ),
            "strict_model_match": bool(
                config.get("routing", "strict_model_match", default=False)
            ),
            "metrics_usage_bucket_include_provider": bool(
                config.get(
                    "routing",
                    "metrics_usage_bucket_include_provider",
                    default=False,
                )
            ),
        }

    return JSONResponse(content={
        "uptime_hours": uptime_h,
        "totals_by_model": dict(_usage_totals),
        "daily": daily,
        "weekly": dict(weekly),
        "routing": routing_meta,
    })


class ModeUpdateRequest(BaseModel):
    """Update calling mode for chat models."""
    mode: str


@app.get("/admin/mode")
async def get_mode():
    """Get current calling mode."""
    return {"mode": chat_caller.mode}


@app.post("/admin/mode")
async def set_mode(request: ModeUpdateRequest):
    """Set calling mode for chat models."""
    if request.mode not in {"race", "balance", "hybrid"}:
        raise HTTPException(status_code=400, detail="Unsupported mode")
    chat_caller.mode = request.mode
    return {"mode": chat_caller.mode}


@app.get("/admin/models")
async def list_models():
    """List registered models."""
    ids = [model["id"] for model in chat_caller.models]
    ids.extend(sorted(anthropic_chat_configs.keys()))
    return {
        "chat_models": ids,
        "embedding_models": list(embedding_clients.keys()),
        "reranker_models": list(reranker_configs.keys()),
    }


async def _dispatch_chat_completion(request: ChatCompletionRequest) -> Dict[str, Any]:
    """Route chat to OpenAI-compatible pool, subset, or Anthropic /messages."""
    messages = normalize_messages_for_response_format(
        request.messages,
        request.response_format,
    )
    call_kwargs: Dict[str, Any] = dict(
        messages=messages,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
        stop=request.stop,
        stream=request.stream,
    )
    if request.response_format is not None:
        call_kwargs["response_format"] = request.response_format
    if request.tools is not None:
        call_kwargs["tools"] = request.tools
    if request.tool_choice is not None:
        call_kwargs["tool_choice"] = request.tool_choice

    n_openai = len(chat_caller.models)
    n_ant = len(anthropic_chat_configs)
    need_resolve = (n_openai + n_ant) > 1

    strict = bool(config.get("routing", "strict_model_match", default=False))
    rq = normalize_model_name(request.model)
    if strict and rq and rq not in KNOWN_CHAT_MODEL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown chat model: {request.model}",
        )

    if not need_resolve:
        return await chat_caller.call(**call_kwargs)

    resolution = resolve_chat_route(
        request.model,
        openai_models=chat_caller.models,
        anthropic_configs=anthropic_chat_configs,
        default_chat_model=config.get("routing", "default_chat_model", default="qwen-plus"),
    )

    if resolution.kind == "unresolved":
        raise HTTPException(
            status_code=500,
            detail="No chat route resolved; check routing.default_chat_model and providers",
        )

    if resolution.kind == "anthropic":
        # CAMEL 使用 beta.chat.completions.parse → 同一 /v1/chat/completions 且带 response_format；
        # Anthropic /messages 适配层不转发结构化参数，避免静默丢字段导致难排查。
        if request.response_format is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "response_format is not supported on the anthropic_messages route; "
                    "use an OpenAI-compatible chat model (see GET /admin/models chat_models). "
                    "Procurement L2 structured output requires this."
                ),
            )
        if anthropic_http_client is None:
            raise HTTPException(status_code=503, detail="Anthropic route not initialized")
        if request.stream:
            raise HTTPException(
                status_code=400,
                detail="stream=true is not supported for anthropic_messages models",
            )
        if request.tools:
            raise HTTPException(
                status_code=400,
                detail="tools are not supported for anthropic_messages models in this gateway",
            )
        acfg = resolution.anthropic_config or {}
        est = (
            sum(len(str(m.get("content", ""))) for m in messages) // 4 + 100
        )
        rate_limiter.acquire(acfg["provider"], acfg["id"], est)
        chat_timeout = float(config.get("request_timeout", "chat", default=600))
        t0 = time.time()
        cc = await anthropic_messages_chat_completion(
            http_client=anthropic_http_client,
            messages_url=acfg["messages_url"],
            api_key=acfg["api_key"],
            model=acfg["model"],
            openai_messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stop=request.stop,
            client_model_id=acfg["id"],
            timeout=chat_timeout,
        )
        elapsed = time.time() - t0
        return {
            "success": True,
            "model_id": acfg["id"],
            "model_name": acfg["model"],
            "provider": acfg["provider"],
            "response": cc,
            "elapsed_time": elapsed,
        }

    subset = resolution.openai_subset or []
    if not subset:
        raise HTTPException(status_code=500, detail="Empty OpenAI chat subset")
    kw = {k: v for k, v in call_kwargs.items() if k != "messages"}
    return await chat_caller.call_on_subset(subset, messages, **kw)


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint.
    
    This endpoint accepts chat completion requests and routes them through
    the multi-model caller based on the configured mode.
    
    Supports both streaming and non-streaming modes:
    - stream=false: Returns complete response as JSON
    - stream=true: Returns Server-Sent Events (SSE) stream
    """
    start_time = time.time()
    try:
        # Log detailed request information
        logger.info(
            "[Gateway] ------------------------------------------------------------------"
        )
        logger.info(
            f"[Gateway] INCOMING REQUEST: {len(request.messages)} messages, "
            f"model={request.model}, stream={request.stream}"
        )
        
        # Log each message with role and content preview
        for idx, msg in enumerate(request.messages):
            role = msg.get("role", "unknown")
            content = str(msg.get("content", ""))
            # Truncate long content for readability
            if len(content) > 500:
                content_preview = content[:500] + f"... (total {len(content)} chars)"
            else:
                content_preview = content
            logger.info(f"[Gateway]   Message {idx+1} [{role}]: {content_preview}")
        
        # Estimate tokens (rough estimate: ~4 chars per token)
        estimated_tokens = sum(
            len(str(msg.get("content", ""))) for msg in request.messages
        ) // 4 + 100
        logger.info(f"[Gateway] Estimated input tokens: {estimated_tokens}")
        
        # Check rate limits for all models (they'll be checked individually too)
        # This is just a pre-check
        logger.debug(f"[Gateway] Estimated tokens: {estimated_tokens}")
        
        result = await _dispatch_chat_completion(request)
        
        if not result["success"]:
            error_msg = result.get("error", "Model call failed")
            # Return 504 Gateway Timeout when upstream timed out, so clients can distinguish and retry
            if "timed out" in str(error_msg).lower() or "timeout" in str(error_msg).lower():
                raise HTTPException(status_code=504, detail=f"Upstream timeout: {error_msg}")
            raise HTTPException(status_code=500, detail=f"Model call failed: {error_msg}")
        
        response = result["response"]
        
        # Handle streaming response
        if request.stream:
            async def stream_generator():
                """Generate SSE stream from OpenAI AsyncStream."""
                usage_prompt = 0
                usage_compl = 0
                usage_total = 0
                try:
                    async for chunk in response:
                        chunk_data = chunk.model_dump_json()
                        yield f"data: {chunk_data}\n\n"
                        try:
                            d = chunk.model_dump()
                            u = d.get("usage")
                            if u:
                                usage_prompt = int(u.get("prompt_tokens") or 0) or usage_prompt
                                usage_compl = int(u.get("completion_tokens") or 0) or usage_compl
                                usage_total = int(u.get("total_tokens") or 0) or usage_total
                        except Exception:
                            pass
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.error(f"[Gateway] Stream error: {e}")
                    error_chunk = {"error": str(e)}
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                finally:
                    pt, ct, tt = usage_prompt, usage_compl, usage_total
                    if tt <= 0:
                        pt = MultiModelCaller._estimate_tokens(request.messages)
                        ct = max(1, pt // 10)
                        tt = pt + ct
                        logger.debug(
                            "[Gateway] Stream usage fallback (estimated): pt=%s ct=%s",
                            pt,
                            ct,
                        )
                    _record_request(
                        "chat",
                        time.time() - start_time,
                        model_id=result.get("model_id", ""),
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt,
                        provider=result.get("provider", ""),
                    )
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Model-ID": result["model_id"],
                    "X-Model-Name": result["model_name"],
                    "X-Provider": result["provider"],
                }
            )
        
        # Handle non-streaming response
        # Log detailed response information
        elapsed_time = time.time() - start_time
        
        # Extract response content and usage
        if hasattr(response, 'choices') and response.choices:
            response_content = response.choices[0].message.content
            # Truncate long response for readability
            if len(response_content) > 500:
                content_preview = response_content[:500] + f"... (total {len(response_content)} chars)"
            else:
                content_preview = response_content
            logger.info(f"[Gateway] RESPONSE CONTENT: {content_preview}")
        
        # Log token usage if available
        if hasattr(response, 'usage') and response.usage:
            logger.info(
                        f"[Gateway] TOKEN USAGE: "
                f"prompt={response.usage.prompt_tokens}, "
                f"completion={response.usage.completion_tokens}, "
                f"total={response.usage.total_tokens}"
            )
        
        logger.info(
            f"[Gateway] TOTAL TIME: {elapsed_time:.3f}s | "
            f"Model: {result['model_id']} ({result['model_name']}) | "
            f"Provider: {result['provider']}"
        )
        logger.info(
            "[Gateway] ------------------------------------------------------------------"
        )
        
        response_data = response.model_dump()
        # Normalize finish_reason: DashScope returns string "null" instead
        # of a valid value when output is very long. Map it to "stop" (if
        # content exists) or "length" to satisfy downstream validators.
        for choice in response_data.get("choices") or []:
            fr = choice.get("finish_reason")
            if fr is None or fr == "null":
                has_content = bool(
                    choice.get("message", {}).get("content")
                )
                choice["finish_reason"] = "stop" if has_content else "length"

        _p_tok = response.usage.prompt_tokens if hasattr(response, 'usage') and response.usage else 0
        _c_tok = response.usage.completion_tokens if hasattr(response, 'usage') and response.usage else 0
        _t_tok = response.usage.total_tokens if hasattr(response, 'usage') and response.usage else 0
        response_payload = JSONResponse(
            content=response_data,
            headers={
                "X-Model-ID": result["model_id"],
                "X-Model-Name": result["model_name"],
                "X-Provider": result["provider"],
                "X-Elapsed-Time": str(result["elapsed_time"])
            }
        )
        _record_request(
            "chat",
            time.time() - start_time,
            model_id=result.get("model_id", ""),
            prompt_tokens=_p_tok,
            completion_tokens=_c_tok,
            total_tokens=_t_tok,
            provider=result.get("provider", ""),
        )
        return response_payload

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Gateway] Error in chat_completions: {e}", exc_info=True)
        _record_request(
            "chat",
            time.time() - start_time,
            is_error=True,
            model_id=request.model,
            provider="",
        )
        err_str = str(e)
        # Return 504 when upstream timed out (e.g. "Model call failed: Request timed out.")
        if "timed out" in err_str.lower() or "timeout" in err_str.lower():
            raise HTTPException(status_code=504, detail=f"Upstream timeout: {err_str}")
        raise HTTPException(status_code=500, detail=err_str)


@app.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest):
    """OpenAI-compatible embeddings endpoint."""
    start_time = time.time()
    try:
        logger.info(f"[Gateway] Received embedding request: model={request.model}")
        
        # Find embedding model
        if request.model not in embedding_clients:
            # Try to match by provider model name
            model_id = None
            for provider_config in config.get("providers", default={}).values():
                for model_config in provider_config.get("embedding_models", []):
                    if model_config["model"] == request.model:
                        model_id = model_config["id"]
                        break
                if model_id:
                    break
            
            if not model_id:
                if embedding_clients:
                    model_id = next(iter(embedding_clients.keys()))
                    logger.warning(
                        "[Gateway] Embedding model '%s' not found, fallback to '%s'",
                        request.model,
                        model_id,
                    )
                else:
                    raise HTTPException(status_code=404, detail=f"Embedding model not found: {request.model}")
        else:
            model_id = request.model
        if model_id not in embedding_clients:
            if embedding_clients:
                fallback_model_id = next(iter(embedding_clients.keys()))
                logger.warning(
                    "[Gateway] Embedding model id '%s' unavailable, fallback to '%s'",
                    model_id,
                    fallback_model_id,
                )
                model_id = fallback_model_id
            else:
                raise HTTPException(status_code=404, detail=f"Embedding model not found: {request.model}")
        
        client = embedding_clients[model_id]
        
        # Check rate limit
        provider_name = embedding_configs.get(model_id, {}).get(
            "provider", "siliconflow"
        )
        estimated_tokens = (
            len(str(request.input)) // 4 + 50 if request.input else 50
        )
        rate_limiter.acquire(provider_name, model_id, estimated_tokens)
        
        # Call embedding API with upstream model name (id like "bge-m3" -> "BAAI/bge-m3")
        upstream_model = embedding_configs[model_id].get("model", request.model)
        # DashScope text-embedding-v4 limits batch size to 10 per request.
        # Split large batches and merge results transparently.
        MAX_EMBED_BATCH = 10
        inputs = request.input if isinstance(request.input, list) else [request.input]
        if len(inputs) <= MAX_EMBED_BATCH:
            response = await client.embeddings.create(
                model=upstream_model,
                input=inputs,
                encoding_format="float",
            )
        else:
            MAX_EMBED_CONCURRENCY = 5
            batches = [inputs[i : i + MAX_EMBED_BATCH] for i in range(0, len(inputs), MAX_EMBED_BATCH)]
            sem = asyncio.Semaphore(MAX_EMBED_CONCURRENCY)

            async def _embed_batch(batch):
                async with sem:
                    return await client.embeddings.create(
                        model=upstream_model,
                        input=batch,
                        encoding_format="float",
                    )

            resps = await asyncio.gather(*[_embed_batch(b) for b in batches])
            all_data = []
            total_tokens = 0
            for resp in resps:
                for item in resp.data:
                    item.index = len(all_data)
                    all_data.append(item)
                if resp.usage:
                    total_tokens += resp.usage.total_tokens
            last_resp = resps[-1]
            last_resp.data = all_data
            if last_resp.usage:
                last_resp.usage.total_tokens = total_tokens
                last_resp.usage.prompt_tokens = total_tokens
            response = last_resp

        logger.info(f"[Gateway] Embedding generated: {len(response.data)} vectors")

        _e_tok = response.usage.total_tokens if hasattr(response, 'usage') and response.usage else 0
        response_payload = JSONResponse(content=response.model_dump())
        _emb_prov = embedding_configs.get(model_id, {}).get("provider", "")
        _record_request(
            "embeddings",
            time.time() - start_time,
            model_id=model_id,
            total_tokens=_e_tok,
            prompt_tokens=_e_tok,
            provider=_emb_prov,
        )
        return response_payload

    except Exception as e:
        logger.error(f"[Gateway] Error in embeddings: {e}", exc_info=True)
        _record_request(
            "embeddings",
            time.time() - start_time,
            is_error=True,
            model_id=request.model,
            provider="",
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/rerank")
@app.post("/rerank")
async def rerank(request: RerankRequest):
    """SiliconFlow-compatible rerank endpoint."""
    start_time = time.time()
    try:
        logger.info(f"[Gateway] Received rerank request: model={request.model}")

        if request.model not in reranker_configs:
            model_id = None
            for provider_config in config.get("providers", default={}).values():
                for model_config in provider_config.get("reranker_models", []):
                    if model_config["model"] == request.model:
                        model_id = model_config["id"]
                        break
                if model_id:
                    break
            if not model_id:
                if reranker_configs:
                    model_id = next(iter(reranker_configs.keys()))
                    logger.warning(
                        "[Gateway] Reranker model '%s' not found, fallback to '%s'",
                        request.model,
                        model_id,
                    )
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Reranker model not found: {request.model}",
                    )
        else:
            model_id = request.model
        if model_id not in reranker_configs:
            if reranker_configs:
                fallback_model_id = next(iter(reranker_configs.keys()))
                logger.warning(
                    "[Gateway] Reranker model id '%s' unavailable, fallback to '%s'",
                    model_id,
                    fallback_model_id,
                )
                model_id = fallback_model_id
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Reranker model not found: {request.model}",
                )

        model_config = reranker_configs[model_id]
        provider_name = model_config.get("provider", "siliconflow")

        estimated_tokens = (
            len(request.query) + sum(len(doc) for doc in request.documents)
        ) // 4 + 50
        rate_limiter.acquire(provider_name, model_id, estimated_tokens)

        # Use upstream model name from config (never send id to API)
        upstream_model = model_config.get("model", model_id)
        client = reranker_http_clients[model_id]
        headers = {
            "Authorization": f"Bearer {model_config.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        if provider_name == "dashscope":
            # DashScope 原生重排接口（非 OpenAI 兼容路径）
            base = model_config["base_url"].rstrip("/")
            base = re.sub(r"/compatible-mode/v1/?$", "", base)
            endpoint = f"{base}/api/v1/services/rerank/text-rerank/text-rerank"
            payload = {
                "model": upstream_model,
                "input": {
                    "query": request.query,
                    "documents": request.documents,
                },
                "parameters": {
                    "top_n": request.top_n or len(request.documents),
                },
            }
            response = await client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            raw = response.json()
            # 统一成当前 retriever 可识别的结果结构
            response_payload = JSONResponse(
                content={
                    "results": (raw.get("output") or {}).get("results", []),
                    "request_id": raw.get("request_id", ""),
                    "usage": raw.get("usage", {}),
                }
            )
        else:
            payload = {
                "model": upstream_model,
                "query": request.query,
                "documents": request.documents,
            }
            if request.top_n is not None:
                payload["top_n"] = request.top_n
            response = await client.post(
                f"{model_config['base_url']}/rerank",
                json=payload,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            response_payload = JSONResponse(content=response.json())
        _r_tok = estimated_tokens
        _record_request(
            "rerank",
            time.time() - start_time,
            model_id=model_id,
            total_tokens=_r_tok,
            prompt_tokens=_r_tok,
            provider=model_config.get("provider", ""),
        )
        return response_payload

    except Exception as e:
        logger.error(f"[Gateway] Error in rerank: {e}", exc_info=True)
        _record_request(
            "rerank",
            time.time() - start_time,
            is_error=True,
            model_id=request.model,
            provider="",
        )
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("GATEWAY_PORT", "9000"))
    
    uvicorn.run(
        "gateway:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False
    )
