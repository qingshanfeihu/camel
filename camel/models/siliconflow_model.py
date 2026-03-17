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
import os
import random
import time
from typing import Any, Dict, List, Optional, Type, Union

from openai import AsyncStream, RateLimitError, Stream
from openai.lib.streaming.chat import ChatCompletionStreamManager
from pydantic import BaseModel

from camel.configs import SiliconFlowConfig
from camel.logger import get_logger
from camel.messages import OpenAIMessage
from camel.models.openai_compatible_model import OpenAICompatibleModel
from camel.types import (
    ChatCompletion,
    ChatCompletionChunk,
    ModelType,
)
from camel.utils import (
    BaseTokenCounter,
    api_keys_required,
)

logger = get_logger(__name__)

_SF_LLM_MAX_RETRIES = int(os.getenv("SILICONFLOW_LLM_RETRIES", "6"))
_SF_LLM_BACKOFF_INITIAL = float(
    os.getenv("SILICONFLOW_LLM_BACKOFF_INITIAL", "1")
)
_SF_LLM_BACKOFF_MAX = float(
    os.getenv("SILICONFLOW_LLM_BACKOFF_MAX", "60")
)
_SF_LLM_BACKOFF_JITTER = os.getenv(
    "SILICONFLOW_LLM_BACKOFF_JITTER", "1"
).strip().lower() not in {"0", "false", "no", "off"}
_SF_LLM_MIN_INTERVAL = float(os.getenv("SILICONFLOW_LLM_MIN_INTERVAL", "0"))

_LAST_SF_LLM_CALL_TS: Optional[float] = None

_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY")
if _GATEWAY_API_KEY and not os.getenv("SILICONFLOW_API_KEY"):
    os.environ["SILICONFLOW_API_KEY"] = _GATEWAY_API_KEY


def _sf_maybe_sleep_for_rate_limit() -> None:
    if _SF_LLM_MIN_INTERVAL <= 0:
        return
    global _LAST_SF_LLM_CALL_TS
    now = time.monotonic()
    if _LAST_SF_LLM_CALL_TS is not None:
        elapsed = now - _LAST_SF_LLM_CALL_TS
        if elapsed < _SF_LLM_MIN_INTERVAL:
            time.sleep(_SF_LLM_MIN_INTERVAL - elapsed)
    _LAST_SF_LLM_CALL_TS = time.monotonic()


def _sf_is_rate_limit_error(exc: Exception) -> bool:
    if RateLimitError is not Exception and isinstance(exc, RateLimitError):
        return True
    message = str(exc).lower()
    return "429" in message or "rate limit" in message


def _sf_call_openai_with_backoff(request_fn, *, label: str):
    last_error: Optional[Exception] = None
    for attempt in range(1, _SF_LLM_MAX_RETRIES + 1):
        try:
            _sf_maybe_sleep_for_rate_limit()
            return request_fn()
        except Exception as exc:
            last_error = exc
            if not _sf_is_rate_limit_error(exc):
                raise
            if attempt >= _SF_LLM_MAX_RETRIES:
                break
            base_delay = min(
                _SF_LLM_BACKOFF_MAX,
                _SF_LLM_BACKOFF_INITIAL * (2 ** (attempt - 1)),
            )
            if _SF_LLM_BACKOFF_JITTER:
                delay = base_delay * (1 + random.random())
            else:
                delay = base_delay
            logger.warning(
                "[%s] rate limit, retry %d/%d after %.2fs: %s",
                label,
                attempt,
                _SF_LLM_MAX_RETRIES,
                delay,
                exc,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"[{label}] exceeded retry limit due to rate limiting: {last_error}"
    )


class SiliconFlowModel(OpenAICompatibleModel):
    r"""SiliconFlow API in a unified OpenAICompatibleModel interface.

    Args:
        model_type (Union[ModelType, str]): Model for which a backend is
            created.
        model_config_dict (Optional[Dict[str, Any]], optional): A dictionary
            that will be fed into OpenAI client. If :obj:`None`,
            :obj:`SiliconFlowConfig().as_dict()` will be used.
            (default: :obj:`None`)
        api_key (Optional[str], optional): The API key for authenticating with
            the SiliconFlow service. (default: :obj:`None`)
        url (Optional[str], optional): The URL to the SiliconFlow service. If
            not provided, :obj:`https://api.siliconflow.cn/v1/` will be used.
            (default: :obj:`None`)
        token_counter (Optional[BaseTokenCounter], optional): Token counter to
            use for the model. If not provided, :obj:`OpenAITokenCounter(
            ModelType.GPT_4O_MINI)` will be used.
            (default: :obj:`None`)
        timeout (Optional[float], optional): The timeout value in seconds for
            API calls. If not provided, will fall back to the MODEL_TIMEOUT
            environment variable or default to 180 seconds.
            (default: :obj:`None`)
        max_retries (int, optional): Maximum number of retries for API calls.
            (default: :obj:`3`)
        **kwargs (Any): Additional arguments to pass to the client
            initialization.
    """

    @api_keys_required(
        [
            ("api_key", 'SILICONFLOW_API_KEY'),
        ]
    )
    def __init__(
        self,
        model_type: Union[ModelType, str],
        model_config_dict: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        token_counter: Optional[BaseTokenCounter] = None,
        timeout: Optional[float] = None,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> None:
        if model_config_dict is None:
            model_config_dict = SiliconFlowConfig().as_dict()
        gateway_url = os.environ.get("LLM_GATEWAY_BASE_URL")
        if not gateway_url:
            raise ValueError(
                "LLM_GATEWAY_BASE_URL 未配置。当前项目禁止直连 SiliconFlow API。"
            )
        api_key = api_key or os.environ.get("SILICONFLOW_API_KEY")
        url = url or os.environ.get(
            "LLM_GATEWAY_BASE_URL",
            os.environ.get(
                "SILICONFLOW_API_BASE_URL",
                "https://api.siliconflow.cn/v1/",
            ),
        )
        if url and url.rstrip("/").endswith("/v1") is False:
            url = f"{url.rstrip('/')}/v1"
        timeout = timeout or float(os.environ.get("MODEL_TIMEOUT", 180))
        super().__init__(
            model_type=model_type,
            model_config_dict=model_config_dict,
            api_key=api_key,
            url=url,
            token_counter=token_counter,
            timeout=timeout,
            max_retries=max_retries,
            **kwargs,
        )

    def _run(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[
        ChatCompletion,
        Stream[ChatCompletionChunk],
        ChatCompletionStreamManager[BaseModel],
    ]:
        return _sf_call_openai_with_backoff(
            lambda: OpenAICompatibleModel._run(self, messages, response_format, tools),
            label="siliconflow-chat",
        )

    async def _arun(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]:
        raise NotImplementedError(
            "SiliconFlow does not support async inference."
        )
