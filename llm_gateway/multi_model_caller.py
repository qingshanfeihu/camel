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

"""Multi-model concurrent caller implementation."""

import asyncio
import httpx
import logging
import os
import time
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class MultiModelCaller:
    """Multi-model caller with race/balance/hybrid modes."""
    
    def __init__(self, mode: str = "balance", rate_limiter: Any = None, default_timeout: float = 90.0):
        """Initialize multi-model caller.
        
        Args:
            mode: Calling mode - "balance" (round-robin, one request per call, recommended),
                  "race" (concurrent, first wins, wastes RPM/TPM), or "hybrid" (same as balance).
            rate_limiter: Optional rate limiter instance.
            default_timeout: Default timeout for API calls in seconds (default: 90.0).
                            Note: OpenAI SDK built-in retries are DISABLED (max_retries=0)
                            to prevent 3x timeout delays. Gateway handles failover.
                            Increased from 60s to 90s to handle SiliconFlow p99 latency (~60.15s).
        """
        self.mode = mode
        self.models: List[Dict[str, Any]] = []
        self.clients: Dict[str, AsyncOpenAI] = {}
        self.current_index = 0  # For round-robin in balance mode
        self._index_lock = asyncio.Lock()  # Concurrency-safe round-robin
        self.rate_limiter = rate_limiter
        self.default_timeout = default_timeout
        
        # Check SSL verification setting
        ssl_verify_str = os.getenv("SILICONFLOW_SSL_VERIFY", "true").lower()
        self.ssl_verify = ssl_verify_str not in ("false", "0", "no")
        
        if not self.ssl_verify:
            logger.warning("[MultiModelCaller] SSL verification is disabled")
        
        logger.info(
            f"[MultiModelCaller] Initialized with mode={mode}, timeout={default_timeout}s, "
            f"max_retries=0 (SDK retries disabled). "
            f"⏱️ Timeout buffer from SiliconFlow p99 latency (~60.15s): {default_timeout - 60:.1f}s"
        )
    
    def add_model(self, model_config: Dict[str, Any]):
        """Add a model to the caller pool.
        
        Args:
            model_config: Model configuration dict with keys:
                - id: Model identifier
                - provider: Provider name
                - api_key: API key
                - base_url: Base URL
                - model: Model name
                - rpm: Requests per minute
                - tpm: Tokens per minute
        """
        model_id = model_config["id"]
        self.models.append(model_config)
        
        # Create httpx client with SSL verification setting
        # Use per-request timeout from config, default 60s for long responses
        client_timeout = model_config.get("timeout", self.default_timeout)
        if not self.ssl_verify:
            http_client = httpx.AsyncClient(verify=False, timeout=client_timeout)
        else:
            http_client = httpx.AsyncClient(timeout=client_timeout)
        
        # Create async client with custom http client
        # CRITICAL: max_retries=0 to disable OpenAI SDK built-in retries
        # This prevents 3x30s = 90s delay per model; Gateway handles failover
        self.clients[model_id] = AsyncOpenAI(
            api_key=model_config["api_key"],
            base_url=model_config["base_url"],
            http_client=http_client,
            timeout=client_timeout,
            max_retries=0  # Disable SDK retries; Gateway handles failover
        )
        
        logger.info(
            f"[MultiModelCaller] Added model: {model_id} "
            f"({model_config['model']}) from {model_config['provider']}"
        )

    @staticmethod
    def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
        text_len = sum(len(str(msg.get("content", ""))) for msg in messages)
        return max(1, text_len // 4 + 100)
    
    async def _call_single_model(
        self, 
        model_config: Dict[str, Any],
        messages: List[Dict],
        **kwargs
    ) -> Dict[str, Any]:
        """Call a single model.
        
        Args:
            model_config: Model configuration
            messages: Chat messages
            **kwargs: Additional parameters
            
        Returns:
            Response dict with model info
        """
        model_id = model_config["id"]
        start_time = time.time()
        
        try:
            if self.rate_limiter is not None:
                estimated_tokens = self._estimate_tokens(messages)
                self.rate_limiter.acquire(
                    model_config["provider"],
                    model_id,
                    estimated_tokens=estimated_tokens,
                )
            
            # Log detailed model call information
            logger.info(
                f"[MultiModelCaller] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            logger.info(
                f"[MultiModelCaller] 🚀 CALLING MODEL: {model_id} ({model_config['model']})"
            )
            logger.info(
                f"[MultiModelCaller] 📍 Provider: {model_config['provider']} | "
                f"Messages: {len(messages)} | Stream: {kwargs.get('stream', False)}"
            )
            
            client = self.clients[model_id]
            response = await client.chat.completions.create(
                model=model_config["model"],
                messages=messages,
                **kwargs
            )
            
            elapsed = time.time() - start_time
            
            # Check if this is a streaming response (AsyncStream has no usage attribute)
            is_streaming = kwargs.get("stream", False)
            if is_streaming:
                logger.info(
                    f"[MultiModelCaller] ✓ Stream started in {elapsed:.3f}s"
                )
            else:
                # Log detailed response information for non-streaming
                if hasattr(response, 'usage') and response.usage:
                    logger.info(
                        f"[MultiModelCaller] 📊 TOKENS: "
                        f"prompt={response.usage.prompt_tokens}, "
                        f"completion={response.usage.completion_tokens}, "
                        f"total={response.usage.total_tokens}"
                    )
                
                # Log response content preview
                if hasattr(response, 'choices') and response.choices:
                    content = response.choices[0].message.content
                    if len(content) > 200:
                        content_preview = content[:200] + f"... ({len(content)} chars)"
                    else:
                        content_preview = content
                    logger.info(f"[MultiModelCaller] 📤 RESPONSE: {content_preview}")
                
                logger.info(
                    f"[MultiModelCaller] ⏱️  MODEL LATENCY: {elapsed:.3f}s"
                )
            
            logger.info(
                f"[MultiModelCaller] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            return {
                "success": True,
                "model_id": model_id,
                "model_name": model_config["model"],
                "provider": model_config["provider"],
                "response": response,
                "elapsed_time": elapsed
            }
            
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[MultiModelCaller] ✗ Model {model_id} failed "
                f"after {elapsed:.2f}s: {e}"
            )
            return {
                "success": False,
                "model_id": model_id,
                "model_name": model_config["model"],
                "provider": model_config["provider"],
                "error": str(e),
                "elapsed_time": elapsed
            }
    
    async def call_race_mode(
        self,
        messages: List[Dict],
        **kwargs
    ) -> Dict[str, Any]:
        """Race mode: Call all models concurrently, return fastest response.
        
        Args:
            messages: Chat messages
            **kwargs: Additional parameters
            
        Returns:
            Response from the fastest model
        """
        logger.info(
            f"[MultiModelCaller] 🏁 Race mode: Starting {len(self.models)} models"
        )
        
        # Create tasks for all models
        tasks = [
            asyncio.create_task(
                self._call_single_model(model_config, messages, **kwargs)
            )
            for model_config in self.models
        ]
        
        # Wait for first completion
        done, pending = await asyncio.wait(
            tasks, 
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Get the first successful result
        for task in done:
            result = await task
            if result["success"]:
                for pending_task in pending:
                    pending_task.cancel()
                logger.debug("[MultiModelCaller] Cancelled pending tasks")
                logger.info(
                    f"[MultiModelCaller] 🏆 Winner: {result['model_id']} "
                    f"({result['elapsed_time']:.2f}s)"
                )
                return result

        # No success yet, await remaining tasks for possible success
        for pending_task in asyncio.as_completed(pending):
            try:
                result = await pending_task
                if result["success"]:
                    return result
            except asyncio.CancelledError:
                continue
        
        # All failed
        logger.error("[MultiModelCaller] All models failed in race mode")
        raise Exception("All models failed to respond")
    
    async def call_balance_mode(
        self,
        messages: List[Dict],
        **kwargs
    ) -> Dict[str, Any]:
        """Balance mode: Round-robin selection, one model per request (no wasted RPM/TPM).
        
        Args:
            messages: Chat messages
            **kwargs: Additional parameters (e.g. response_format)
            
        Returns:
            Response from selected model
        """
        if not self.models:
            raise ValueError("No models available")

        attempts = 0
        last_error = None
        start_time = time.time()
        
        while attempts < len(self.models):
            async with self._index_lock:
                idx = self.current_index
                self.current_index = (self.current_index + 1) % len(self.models)
            model_config = self.models[idx]
            attempts += 1

            logger.info(
                f"[MultiModelCaller] ⚖️ Balance mode: Selected {model_config['id']} "
                f"(attempt {attempts}/{len(self.models)})"
            )

            result = await self._call_single_model(
                model_config, messages, **kwargs
            )

            if result["success"]:
                total_time = time.time() - start_time
                if attempts > 1:
                    logger.info(
                        f"[MultiModelCaller] ✓ Success after {attempts} attempts, "
                        f"total time: {total_time:.2f}s"
                    )
                return result

            last_error = result.get("error")
            logger.warning(
                f"[MultiModelCaller] Model {model_config['id']} failed, "
                "trying next model"
            )

        raise Exception(f"Model call failed: {last_error}")
    
    async def call_hybrid_mode(
        self,
        messages: List[Dict],
        **kwargs
    ) -> Dict[str, Any]:
        """Hybrid mode: Single-model selection (same as balance to avoid wasting RPM/TPM).
        
        Args:
            messages: Chat messages
            **kwargs: Additional parameters
            
        Returns:
            Response from selected model
        """
        logger.info("[MultiModelCaller] 🔀 Hybrid mode: Using balance (single model per request)")
        return await self.call_balance_mode(messages, **kwargs)
    
    async def call(
        self,
        messages: List[Dict],
        **kwargs
    ) -> Dict[str, Any]:
        """Call models based on configured mode.
        
        Args:
            messages: Chat messages
            **kwargs: Additional parameters
            
        Returns:
            Model response
        """
        if self.mode == "race":
            return await self.call_race_mode(messages, **kwargs)
        elif self.mode == "balance":
            return await self.call_balance_mode(messages, **kwargs)
        elif self.mode == "hybrid":
            return await self.call_hybrid_mode(messages, **kwargs)
        else:
            raise ValueError(f"Unknown mode: {self.mode}")
