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

"""Unified client for INFOAGEN LLM Gateway."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx


class GatewayClient:
    """Synchronous client for INFOAGEN LLM Gateway."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_GATEWAY_BASE_URL") or "http://localhost:9000").rstrip("/")
        self.api_key = api_key or os.getenv("LLM_GATEWAY_API_KEY")
        self.timeout = timeout
        self._client = httpx.Client(timeout=self.timeout)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def health(self) -> Dict[str, Any]:
        response = self._client.get(f"{self.base_url}/health", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def metrics(self) -> Dict[str, Any]:
        response = self._client.get(f"{self.base_url}/metrics", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def chat_completions(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": messages,
        }
        payload.update(kwargs)
        response = self._client.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def embeddings(
        self,
        model: str,
        input_data: str | List[str],
        encoding_format: str = "float",
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "input": input_data,
            "encoding_format": encoding_format,
        }
        response = self._client.post(
            f"{self.base_url}/v1/embeddings",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def rerank(
        self,
        model: str,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        response = self._client.post(
            f"{self.base_url}/v1/rerank",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


class AsyncGatewayClient:
    """Async client for INFOAGEN LLM Gateway."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_GATEWAY_BASE_URL") or "http://localhost:9000").rstrip("/")
        self.api_key = api_key or os.getenv("LLM_GATEWAY_API_KEY")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=self.timeout)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def health(self) -> Dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/health", timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    async def metrics(self) -> Dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/metrics", timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    async def chat_completions(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "messages": messages,
        }
        payload.update(kwargs)
        response = await self._client.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def embeddings(
        self,
        model: str,
        input_data: str | List[str],
        encoding_format: str = "float",
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "input": input_data,
            "encoding_format": encoding_format,
        }
        response = await self._client.post(
            f"{self.base_url}/v1/embeddings",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def rerank(
        self,
        model: str,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload = {
            "model": model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        response = await self._client.post(
            f"{self.base_url}/v1/rerank",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()
