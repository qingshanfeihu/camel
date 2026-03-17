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
    response_format: Optional[Dict[str, str]] = None  # e.g. {"type": "json_object"}
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

request_stats = {
    "chat": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
    "embeddings": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
    "rerank": {"count": 0, "errors": 0, "latencies": deque(maxlen=2000)},
}


def _record_request(metric_key: str, elapsed: float, is_error: bool = False) -> None:
    stats = request_stats.get(metric_key)
    if stats is None:
        return
    stats["count"] += 1
    if is_error:
        stats["errors"] += 1
    stats["latencies"].append(elapsed)


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
    
    for provider_name, provider_config in providers.items():
        if not provider_config.get("enabled", True):
            logger.info(f"[Gateway] Provider {provider_name} is disabled, skipping")
            continue
        
        base_url = provider_config.get("base_url")
        
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
        if provider_limits:
            rate_limiter.set_provider_limits(
                provider_name,
                provider_limits.get("rpm", 1000),
                provider_limits.get("tpm", 50000)
            )
        
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
            
            # Create a model instance for each API key
            for key_index, current_api_key in enumerate(api_keys):
                # Generate unique model ID for each API key
                if len(api_keys) > 1:
                    model_id = f"{model_id_base}_key{key_index+1}"
                    logger.info(f"[Gateway] Multi-key model: {model_id} using account {key_index+1}/{len(api_keys)}")
                else:
                    model_id = model_id_base
                
                # Set per-key rate limit
                rate_limiter.set_model_limits(
                    model_id,
                    model_limits.get("rpm", 1000),
                    model_limits.get("tpm", 50000)
                )
                
                # Add to multi-model caller
                chat_caller.add_model({
                    "id": model_id,
                    "provider": provider_name,
                    "api_key": current_api_key,
                    "base_url": base_url,
                    "model": model_name,
                    "rpm": model_limits.get("rpm", 1000),
                    "tpm": model_limits.get("tpm", 50000)
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
            rate_limiter.set_model_limits(
                model_id,
                model_limits.get("rpm", 2000),
                model_limits.get("tpm", 500000)
            )
            
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
            rate_limiter.set_model_limits(
                model_id,
                model_limits.get("rpm", 2000),
                model_limits.get("tpm", 500000)
            )
            
            logger.info(f"[Gateway] Registered reranker model: {model_id} ({model_config['model']})")
    
    logger.info("=" * 80)
    logger.info(f"[Gateway] ✓ Initialization complete")
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
        "embedding_models": len(embedding_clients),
        "reranker_models": len(reranker_clients)
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
    return {
        "chat_models": [model["id"] for model in chat_caller.models],
        "embedding_models": list(embedding_clients.keys()),
        "reranker_models": list(reranker_configs.keys()),
    }


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
            f"[Gateway] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        logger.info(
            f"[Gateway] 📥 INCOMING REQUEST: {len(request.messages)} messages, "
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
        logger.info(f"[Gateway] 📊 Estimated input tokens: {estimated_tokens}")
        
        # Check rate limits for all models (they'll be checked individually too)
        # This is just a pre-check
        logger.debug(f"[Gateway] Estimated tokens: {estimated_tokens}")
        
        # Call multi-model caller (single model selected by mode; response_format for JSON mode)
        call_kwargs = dict(
            messages=request.messages,
            temperature=request.temperature,
            top_p=request.top_p,
            max_tokens=request.max_tokens,
            stop=request.stop,
            stream=request.stream,
        )
        if request.response_format is not None:
            call_kwargs["response_format"] = request.response_format
        result = await chat_caller.call(**call_kwargs)
        
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
                try:
                    async for chunk in response:
                        # Convert chunk to SSE format
                        chunk_data = chunk.model_dump_json()
                        yield f"data: {chunk_data}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    logger.error(f"[Gateway] Stream error: {e}")
                    error_chunk = {"error": str(e)}
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                finally:
                    _record_request("chat", time.time() - start_time)
            
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
            logger.info(f"[Gateway] 📤 RESPONSE CONTENT: {content_preview}")
        
        # Log token usage if available
        if hasattr(response, 'usage') and response.usage:
            logger.info(
                f"[Gateway] 📊 TOKEN USAGE: "
                f"prompt={response.usage.prompt_tokens}, "
                f"completion={response.usage.completion_tokens}, "
                f"total={response.usage.total_tokens}"
            )
        
        logger.info(
            f"[Gateway] ⏱️  TOTAL TIME: {elapsed_time:.3f}s | "
            f"Model: {result['model_id']} ({result['model_name']}) | "
            f"Provider: {result['provider']}"
        )
        logger.info(
            f"[Gateway] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
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

        response_payload = JSONResponse(
            content=response_data,
            headers={
                "X-Model-ID": result["model_id"],
                "X-Model-Name": result["model_name"],
                "X-Provider": result["provider"],
                "X-Elapsed-Time": str(result["elapsed_time"])
            }
        )
        _record_request("chat", time.time() - start_time)
        return response_payload
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Gateway] Error in chat_completions: {e}", exc_info=True)
        _record_request("chat", time.time() - start_time, is_error=True)
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
                raise HTTPException(status_code=404, detail=f"Embedding model not found: {request.model}")
        else:
            model_id = request.model
        
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
        # Force float encoding to avoid base64 serialization issues
        response = await client.embeddings.create(
            model=upstream_model,
            input=request.input,
            encoding_format="float"  # Always request float arrays, not base64
        )
        
        logger.info(f"[Gateway] ✓ Embedding generated: {len(response.data)} vectors")
        
        response_payload = JSONResponse(content=response.model_dump())
        _record_request("embeddings", time.time() - start_time)
        return response_payload
        
    except Exception as e:
        logger.error(f"[Gateway] Error in embeddings: {e}", exc_info=True)
        _record_request("embeddings", time.time() - start_time, is_error=True)
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
                raise HTTPException(
                    status_code=404,
                    detail=f"Reranker model not found: {request.model}",
                )
        else:
            model_id = request.model

        model_config = reranker_configs[model_id]
        provider_name = model_config.get("provider", "siliconflow")

        estimated_tokens = (
            len(request.query) + sum(len(doc) for doc in request.documents)
        ) // 4 + 50
        rate_limiter.acquire(provider_name, model_id, estimated_tokens)

        # Use upstream model name from config (never send id to API)
        upstream_model = model_config.get("model", model_id)
        client = reranker_http_clients[model_id]
        payload = {
            "model": upstream_model,
            "query": request.query,
            "documents": request.documents,
        }
        if request.top_n is not None:
            payload["top_n"] = request.top_n

        headers = {
            "Authorization": f"Bearer {model_config.get('api_key', '')}",
            "Content-Type": "application/json",
        }

        response = await client.post(
            f"{model_config['base_url']}/rerank",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()

        response_payload = JSONResponse(content=response.json())
        _record_request("rerank", time.time() - start_time)
        return response_payload

    except Exception as e:
        logger.error(f"[Gateway] Error in rerank: {e}", exc_info=True)
        _record_request("rerank", time.time() - start_time, is_error=True)
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
