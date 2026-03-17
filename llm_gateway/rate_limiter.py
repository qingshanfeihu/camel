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

"""Sliding window rate limiter - based on INAGENT/auto_convert.py implementation."""

import logging
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class _SlidingWindowRateLimiter:
    r"""A lightweight sliding-window limiter for RPM and approximate TPM.

    Notes:
    - TPM is best-effort: we estimate tokens from input/output text length.
    - Intended to be conservative and avoid 429s; not a guarantee of provider limits.
    - Based on SiliconFlow documentation and INAGENT implementation.
    """

    def __init__(self, *, rpm: int, tpm: int, name: str = "") -> None:
        self._rpm = max(0, int(rpm))
        self._tpm = max(0, int(tpm))
        self._name = name
        self._req_ts: deque[float] = deque()
        self._tok_ts: deque[Tuple[float, int]] = deque()
        self._lock = threading.Lock()
        
        logger.info(
            f"[RateLimiter] Initialized '{name}' - RPM={rpm}, TPM={tpm}"
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate tokens from text (rough heuristic: 1 token ≈ 4 chars for mixed EN/ZH)."""
        if not text:
            return 1
        return max(1, (len(text) + 3) // 4)

    def acquire(self, *, estimated_tokens: int = 0) -> None:
        """Acquire rate limit permission, waits automatically if needed.
        
        Args:
            estimated_tokens: Number of tokens estimated for this request
        """
        if self._rpm <= 0 and self._tpm <= 0:
            return

        with self._lock:
            now = time.time()
            window_start = now - 60.0

            # Clean expired timestamps
            while self._req_ts and self._req_ts[0] < window_start:
                self._req_ts.popleft()
            while self._tok_ts and self._tok_ts[0][0] < window_start:
                self._tok_ts.popleft()

            req_count = len(self._req_ts)
            tok_count = sum(t for _ts, t in self._tok_ts)

            wait_s = 0.0
            # Check RPM limit
            if self._rpm > 0 and req_count >= self._rpm:
                wait_s = max(wait_s, (self._req_ts[0] + 60.0) - now)

            # Check TPM limit
            if self._tpm > 0 and tok_count + max(0, estimated_tokens) > self._tpm:
                if self._tok_ts:
                    wait_s = max(wait_s, (self._tok_ts[0][0] + 60.0) - now)
                else:
                    wait_s = max(wait_s, 1.0)

            # Wait if necessary and log
            if wait_s > 0:
                logger.warning(
                    f"[RateLimiter:{self._name}] Rate limit reached, waiting {wait_s:.2f}s "
                    f"(RPM={req_count}/{self._rpm}, TPM={tok_count}/{self._tpm})"
                )
                time.sleep(wait_s)

            # Record this request
            now2 = time.time()
            self._req_ts.append(now2)
            if self._tpm > 0:
                self._tok_ts.append((now2, max(1, int(estimated_tokens))))
    
    def get_stats(self) -> Dict[str, int]:
        """Get current usage statistics.
        
        Returns:
            Dict with current_rpm, current_tpm, rpm_limit, tpm_limit
        """
        with self._lock:
            now = time.time()
            window_start = now - 60.0
            
            # Clean expired
            while self._req_ts and self._req_ts[0] < window_start:
                self._req_ts.popleft()
            while self._tok_ts and self._tok_ts[0][0] < window_start:
                self._tok_ts.popleft()
            
            return {
                "current_rpm": len(self._req_ts),
                "current_tpm": sum(t for _ts, t in self._tok_ts),
                "rpm_limit": self._rpm,
                "tpm_limit": self._tpm,
            }


class UnifiedRateLimiter:
    """Three-tier unified rate limiter (global, provider, model)."""
    
    def __init__(self):
        self.global_limiter: Optional[_SlidingWindowRateLimiter] = None
        self.provider_limiters: Dict[str, _SlidingWindowRateLimiter] = {}
        self.model_limiters: Dict[str, _SlidingWindowRateLimiter] = {}
        
        logger.info("[UnifiedRateLimiter] Initialized")
    
    def set_global_limits(self, rpm: int, tpm: int):
        """Set global rate limits."""
        self.global_limiter = _SlidingWindowRateLimiter(
            rpm=rpm, tpm=tpm, name="global"
        )
        logger.info(f"[UnifiedRateLimiter] Global limits set: RPM={rpm}, TPM={tpm}")
    
    def set_provider_limits(self, provider: str, rpm: int, tpm: int):
        """Set provider-level rate limits."""
        self.provider_limiters[provider] = _SlidingWindowRateLimiter(
            rpm=rpm, tpm=tpm, name=f"provider:{provider}"
        )
        logger.info(
            f"[UnifiedRateLimiter] Provider '{provider}' limits set: RPM={rpm}, TPM={tpm}"
        )
    
    def set_model_limits(self, model_id: str, rpm: int, tpm: int):
        """Set model-level rate limits."""
        self.model_limiters[model_id] = _SlidingWindowRateLimiter(
            rpm=rpm, tpm=tpm, name=f"model:{model_id}"
        )
        logger.info(
            f"[UnifiedRateLimiter] Model '{model_id}' limits set: RPM={rpm}, TPM={tpm}"
        )
    
    def acquire(
        self, 
        provider: str, 
        model_id: str, 
        estimated_tokens: int = 100
    ) -> None:
        """Acquire rate limit permission at all three tiers.
        
        Args:
            provider: Provider name (e.g., 'siliconflow')
            model_id: Model identifier
            estimated_tokens: Estimated token count for this request
        """
        # Global tier
        if self.global_limiter:
            self.global_limiter.acquire(estimated_tokens=estimated_tokens)
        
        # Provider tier
        if provider in self.provider_limiters:
            self.provider_limiters[provider].acquire(estimated_tokens=estimated_tokens)
        
        # Model tier
        if model_id in self.model_limiters:
            self.model_limiters[model_id].acquire(estimated_tokens=estimated_tokens)
    
    def get_all_stats(self) -> Dict[str, any]:
        """Get statistics for all limiters.
        
        Returns:
            Dict with stats for global, providers, and models
        """
        stats = {
            "global": self.global_limiter.get_stats() if self.global_limiter else None,
            "providers": {
                name: limiter.get_stats() 
                for name, limiter in self.provider_limiters.items()
            },
            "models": {
                name: limiter.get_stats() 
                for name, limiter in self.model_limiters.items()
            },
        }
        return stats
