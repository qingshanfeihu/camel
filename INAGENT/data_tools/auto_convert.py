import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
import concurrent.futures
import threading
try:
    import json_repair
except ImportError:
    json_repair = None
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camel.loaders.local_mineru_reader import LocalMinerUReader
from camel.logger import set_log_file, set_log_level

from INAGENT.config.project_config import cfg_bool, cfg_float, cfg_int, cfg_str
from INAGENT.utils.env_utils import load_inagent_env, resolve_env_placeholder

try:
    from openai import OpenAI, RateLimitError
except Exception:  # pragma: no cover - optional dependency at runtime
    OpenAI = None
    RateLimitError = Exception

BASE_DIR = Path(__file__).parent
# DOC_LOCAL_DIR 指向 INAGENT/knowledge_base（与 manage_database.py 一致）
DOC_LOCAL_DIR = BASE_DIR.parent / "knowledge_base"
REFERENCE_DIR = DOC_LOCAL_DIR / "reference"

LOG_DIR = DOC_LOCAL_DIR / "logs"
LOG_FILE = LOG_DIR / "auto_convert.log"

logger = logging.getLogger("auto_convert")

# Bump this when auto_convert logic changes in a way that should invalidate cache
# (e.g., text extraction, metadata extraction, block selection).
AUTO_CONVERT_SCHEMA_VERSION = "2026-01-26.1"


def _match_protocols_word_boundary(
    protocol_map: Dict[str, list], text: str
) -> List[str]:
    lower_text = text.lower()
    found: List[str] = []
    for proto, keywords in protocol_map.items():
        for k in keywords:
            if re.search(rf"\b{re.escape(k.lower())}\b", lower_text):
                found.append(proto)
                break
    return found


def _remove_section_number(title: str) -> str:
    """
    去除章节标题中的编号
    
    规则：
    - 移除开头的数字编号（如"11.3.1. "、"11.3.1 "、"第11章 "等）
    - 只保留有意义的标题内容
    
    示例：
    - "11.3.1. HTTP配置" -> "HTTP配置"
    - "11.3.1 HTTP配置" -> "HTTP配置"
    - "第11章 服务器负载均衡" -> "服务器负载均衡"
    - "HTTP配置" -> "HTTP配置"（无编号，保持不变）
    """
    if not title:
        return title
    
    # 匹配开头的数字编号模式
    # 模式1: "11.3.1. " 或 "11.3.1 "
    pattern1 = r"^(?:\d+\.)+\s*"
    # 模式2: "第11章 " 或 "第11节 "
    pattern2 = r"^第\d+[章节]\s*"
    
    # 先尝试匹配模式1
    title = re.sub(pattern1, "", title)
    # 再尝试匹配模式2
    title = re.sub(pattern2, "", title)
    
    return title.strip()


def _infer_section_level_from_heading(title: str) -> Optional[int]:
    """Infer section level from numbered headings.

    Examples:
        "11.3.1. HTTP配置" -> level 3
        "11. 服务器负载均衡" -> level 1
        "第11章 服务器负载均衡" -> level 1
    """
    if not title:
        return None
    number_match = re.match(r"^(\d+(?:\.\d+)*)\s*[.．]?\s+", title)
    if number_match:
        return len(number_match.group(1).split("."))
    chapter_match = re.match(r"^第\d+[章节]\s+", title)
    if chapter_match:
        return 1
    return None


# vLLM probing timeouts
VLLM_MODELS_TIMEOUT = cfg_float(
    "auto_convert.vllm.models_timeout_seconds",
    8.0,
    env="VLLM_MODELS_TIMEOUT",
)
VLLM_INFER_TIMEOUT = cfg_float(
    "auto_convert.vllm.infer_timeout_seconds",
    30.0,
    env="VLLM_INFER_TIMEOUT",
)


def _normalize_openai_base_url(url: str) -> str:
    """Normalize OpenAI-compatible base_url.

    The OpenAI Python SDK appends path segments like "/chat/completions" to
    the configured base_url. If callers accidentally pass a full endpoint
    (e.g., ".../v1/chat/completions"), requests become
    ".../v1/chat/completions/chat/completions" and will 404.
    """
    url = (url or "").strip()
    if not url:
        return url
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url.rstrip("/")


def _load_project_config() -> Dict:
    load_inagent_env()
    config_path = cfg_str(
        "auto_convert.mineru_tools_config_json",
        "",
        env="MINERU_TOOLS_CONFIG_JSON",
    )
    candidate_paths = []

    if config_path:
        candidate_paths.append(Path(config_path))
    candidate_paths.append(BASE_DIR.parent / "mineru.json")
    candidate_paths.append(Path.cwd() / "mineru.json")
    candidate_paths.append(Path.home() / "magic-pdf.json")

    for path in candidate_paths:
        if path.exists():
            try:
                config_data = json.loads(path.read_text(encoding="utf-8"))

                def recursive_resolve(data):
                    if isinstance(data, dict):
                        return {k: recursive_resolve(v) for k, v in data.items()}
                    if isinstance(data, list):
                        return [recursive_resolve(v) for v in data]
                    if isinstance(data, str):
                        return resolve_env_placeholder(data)
                    return data

                resolved = recursive_resolve(config_data)
                return resolved if isinstance(resolved, dict) else {}
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse config %s: %s", path, exc)
                return {}
    return {}


def _load_product_modules_registry() -> Dict[str, Any]:
    """Load product module registry from knowledge_base.

    Supports JSON/YAML. Missing file will return empty dict.
    """
    registry_paths = [
        DOC_LOCAL_DIR / "product_modules_registry.yaml",
        DOC_LOCAL_DIR / "product_modules_registry.yml",
        DOC_LOCAL_DIR / "product_modules_registry.json",
    ]
    for path in registry_paths:
        if not path.exists():
            continue
        try:
            if path.suffix in {".yaml", ".yml"}:
                try:
                    import yaml  # type: ignore
                except Exception:
                    logger.warning(
                        "product_modules_registry.yaml detected but PyYAML is not installed."
                    )
                    return {}
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to load product module registry %s: %s", path, exc)
            return {}
    return {}


def _merge_product_modules_map(base_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    registry = _load_product_modules_registry()
    modules = registry.get("modules", {}) if isinstance(registry, dict) else {}
    merged = {**base_map}
    if isinstance(modules, dict):
        for module_id, module_info in modules.items():
            keywords = []
            if isinstance(module_info, dict):
                keywords = module_info.get("keywords", [])
            if keywords:
                merged.setdefault(module_id, [])
                merged[module_id].extend(list(keywords))
    elif isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            module_id = module.get("id") or module.get("display_name")
            keywords = module.get("keywords", [])
            if module_id and keywords:
                merged.setdefault(str(module_id), [])
                merged[str(module_id)].extend(list(keywords))
    # Normalize duplicates
    for key, values in merged.items():
        seen = set()
        merged[key] = [v for v in values if not (v in seen or seen.add(v))]
    return merged


def _extract_page_texts(blocks: List[Dict], max_chars: int = 800) -> Dict[int, str]:
    page_texts: Dict[int, List[str]] = {}
    for block in blocks:
        text = block.get("text") or ""
        if not text.strip():
            continue
        page_idx = int(block.get("page_idx", 0))
        page_texts.setdefault(page_idx, []).append(text.strip())

    compact_texts: Dict[int, str] = {}
    for page_idx, texts in page_texts.items():
        joined = " ".join(texts)
        compact_texts[page_idx] = joined[:max_chars]
    return compact_texts


def _parse_json_array(response_text: str) -> List[int]:
    try:
        start = response_text.index("[")
        end = response_text.rindex("]")
        payload = response_text[start : end + 1]
        data = json.loads(payload)
        if isinstance(data, list):
            return [int(x) for x in data if isinstance(x, (int, float, str))]
    except Exception:
        return []
    return []


def _filter_front_matter_blocks(blocks: List[Dict]) -> List[Dict]:
    """
    使用 LLM 过滤前置页（已统一使用硅基流动）
    """
    config = _load_project_config()
    llm_config = config.get("llm-aided-config", {})
    filter_config = llm_config.get("front_matter_filter", {})

    if not filter_config.get("enable", False):
        return blocks

    max_pages = int(filter_config.get("max_pages", 10))
    if OpenAI is None:
        logger.warning(
            "OpenAI client not available; skipping front-matter filter."
        )
        return blocks

    # 统一使用硅基流动
    runtime = _get_siliconflow_runtime()
    api_key = str(runtime.get("api_key") or "")
    base_url = str(runtime.get("base_url") or "")
    model = str(runtime.get("model") or "")

    if not api_key:
        logger.warning("网关 api_key 未配置，跳过 front-matter filter")
        return blocks

    page_texts = _extract_page_texts(blocks)
    if not page_texts:
        return blocks

    page_items = []
    for page_idx in sorted(page_texts.keys())[:max_pages]:
        page_items.append({"page_idx": page_idx, "text": page_texts[page_idx]})

    prompt = (
        "你是文档结构清理助手。下面是文档前几页的内容摘要。\n"
        "请判断哪些页面属于目录/版权/声明/前言/致谢/修订记录等前置页，"
        "这些页面对正文检索价值很低，应被删除。\n"
        "仅返回需要删除的 page_idx 列表（JSON 数组），不要输出其他文字。\n\n"
        f"Pages: {json.dumps(page_items, ensure_ascii=False)}"
    )

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=600)
        response = _call_llm_with_retry_and_fallback(
            client=client,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=_siliconflow_temperature(),  # 统一使用硅基流动温度
            allow_fallback=False,  # frontmatter filter 不需要故障转移
        )
        content = response.choices[0].message.content or ""
        drop_pages = _parse_json_array(content)
        if not drop_pages:
            logger.info("Front-matter filter: no pages selected for removal.")
            return blocks

        drop_set = {int(p) for p in drop_pages}
        logger.info(
            "Front-matter filter: removing pages %s", sorted(drop_set)
        )
        return [block for block in blocks if block.get("page_idx") not in drop_set]
    except Exception as exc:
        logger.warning(
            "Front-matter filter failed (网关, base_url=%s, model=%s): %s",
            base_url,
            model,
            exc,
        )
        return blocks


# MinerU 输出统一放到 knowledge_base/mineru_output，避免散落到外面
MINERU_OUTPUT_DIR = DOC_LOCAL_DIR / "mineru_output"
# 备份目录：大文件移到这里而非删除，MinerU 重新生成代价很高
MINERU_BACKUP_DIR = DOC_LOCAL_DIR / "mineru_backup"

# vLLM Docker Configuration
# Set to True if using Docker-hosted MinerU vLLM server (e.g. port 30000)
# Docs: https://opendatalab.github.io/MinerU/zh/quick_start/docker_deployment/
USE_DOCKER_VLLM = True
DOCKER_VLLM_URL = (
    cfg_str("auto_convert.vllm.url", "", env="MINERU_VLLM_URL")
    or cfg_str("auto_convert.vllm.url", "", env="VLLM_URL")
    or "http://127.0.0.1:8000"
)

# Cached availability checks
_VLLM_AVAILABLE: Optional[bool] = None
_LLM_METADATA_AVAILABLE: Optional[bool] = None
_QIANFAN_AVAILABLE: Optional[bool] = None

# Force all OpenAI-compatible LLM calls to use LLM Gateway runtime settings.
# 已弃用其他提供商（百度千帆、腾讯混元），统一使用 LLM Gateway。
FORCE_SILICONFLOW = cfg_str(
    "auto_convert.force_siliconflow",
    "1",
    env="AUTO_CONVERT_FORCE_SILICONFLOW",
).strip().lower() not in {
    "0",
    "false",
    "no",
    "n",
}
# 向后兼容
FORCE_QIANFAN = FORCE_SILICONFLOW

# 双API供应商负载均衡：已禁用
# 所有请求都使用硅基流动
ENABLE_DUAL_PROVIDER_LOAD_BALANCE = False  # 已禁用，统一使用硅基流动

# 提供商选择器（用于负载均衡）
_provider_selector_lock = threading.Lock()
_provider_selector_counter = 0

# 固定的模型配置（简化逻辑）
# 已统一使用 LLM Gateway，弃用其他提供商。
# 模型优先从环境变量 LLM_GATEWAY_CHAT_MODEL 读取，未配置时使用默认模型。
SILICONFLOW_MODEL = (
    cfg_str("llm.gateway.chat_model", "", env="LLM_GATEWAY_CHAT_MODEL").strip()
    or cfg_str("llm.siliconflow.chat_model", "mineru-vlm", env="SILICONFLOW_CHAT_MODEL")
)

# 向后兼容：QIANFAN_MODEL 现在返回硅基流动模型
QIANFAN_MODEL = SILICONFLOW_MODEL

# 为兼容旧的模型校验逻辑，保留允许模型集合（现阶段不做严格校验，仅避免 NameError）
QIANFAN_ALLOWED_MODELS: set[str] = set()
SILICONFLOW_ALLOWED_CHAT_MODELS: set[str] = set()

# 简单的 cache：logs 下存在同名 cache.json 就视为已转换

NET_RETRY_ATTEMPTS = cfg_int(
    "auto_convert.network.retry_attempts",
    3,
    env="AUTO_CONVERT_NET_RETRIES",
)
NET_RETRY_DELAY = cfg_float(
    "auto_convert.network.retry_delay_seconds",
    5.0,
    env="AUTO_CONVERT_NET_RETRY_DELAY",
)
NET_TIMEOUT = cfg_float(
    "auto_convert.network.timeout_seconds",
    8.0,
    env="AUTO_CONVERT_NET_TIMEOUT",
)
HUGGINGFACE_API_CHECK = cfg_str(
    "auto_convert.network.hf_check_url",
    "https://huggingface.co/api/models/opendatalab/PDF-Extract-Kit-1.0",
    env="AUTO_CONVERT_HF_CHECK",
)

def get_baidu_access_token() -> str:
    """
    获取百度 Access Token。
    如果是 bce-v3 格式的 API Key，直接返回。
    否则尝试使用 Client Credentials Flow 获取 Token。
    
    注意：使用统一的配置管理，不硬编码默认值
    """
    # 使用统一的配置管理
    from INAGENT.utils.llm_config import get_qianfan_config
    
    try:
        config = get_qianfan_config()
        api_key = config.get("api_key", "")
        # 如果配置中有 secret_key，需要单独获取
        secret_key = cfg_str("llm.baidu.secret_key", "", env="BAIDU_SECRET_KEY")
    except Exception as e:
        logger.warning("从统一配置获取 API Key 失败，回退到环境变量: %s", e)
        api_key = cfg_str("llm.baidu.qianfan_api_key", "", env="QIANFAN_API_KEY") or cfg_str(
            "llm.baidu.api_key",
            "",
            env="BAIDU_API_KEY",
        )
        secret_key = cfg_str("llm.baidu.secret_key", "", env="BAIDU_SECRET_KEY")

    if not api_key:
        logger.warning(
            "QIANFAN_API_KEY 或 BAIDU_API_KEY 未配置。"
            "请在 INAGENT/.env 文件中配置 API Key。"
        )
        return ""

    # 如果是 V2 风格的 Key (bce-v3/...)，直接作为 Token 使用
    if api_key.startswith("bce-v3"):
        return api_key

    # 兼容旧版 V1 鉴权
    url = f"https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials&client_id={api_key}&client_secret={secret_key}"
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            result = response.read()
            return json.loads(result).get("access_token", "")
    except Exception as e:
        logger.error(f"Failed to get Baidu access token: {e}")
        return ""


class _SlidingWindowRateLimiter:
    r"""A lightweight sliding-window limiter for RPM and approximate TPM.

    Notes:
    - TPM is best-effort: we estimate tokens from input/output text length.
    - Intended to be conservative and avoid 429s; not a guarantee of provider
      limits.
    - 参考硅基流动文档：https://github.com/siliconflow/siliconcloud-cookbook/blob/main/examples/rate-limit/how-to-handle-rate-limit-in-siliconcloud.ipynb
    """

    def __init__(self, *, rpm: int, tpm: int) -> None:
        self._rpm = max(0, int(rpm))
        self._tpm = max(0, int(tpm))
        self._req_ts: deque[float] = deque()
        self._tok_ts: deque[Tuple[float, int]] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Rough heuristic: 1 token ≈ 4 chars for mixed EN/ZH.
        if not text:
            return 1
        return max(1, (len(text) + 3) // 4)

    def acquire(self, *, estimated_tokens: int = 0) -> None:
        """获取限速许可，如果需要等待则自动等待"""
        if self._rpm <= 0 and self._tpm <= 0:
            return

        with self._lock:
            now = time.time()
            window_start = now - 60.0

            # 清理过期的时间戳
            while self._req_ts and self._req_ts[0] < window_start:
                self._req_ts.popleft()
            while self._tok_ts and self._tok_ts[0][0] < window_start:
                self._tok_ts.popleft()

            req_count = len(self._req_ts)
            tok_count = sum(t for _ts, t in self._tok_ts)

            wait_s = 0.0
            # 检查RPM限制
            if self._rpm > 0 and req_count >= self._rpm:
                wait_s = max(wait_s, (self._req_ts[0] + 60.0) - now)

            # 检查TPM限制
            if self._tpm > 0 and tok_count + max(0, estimated_tokens) > self._tpm:
                if self._tok_ts:
                    wait_s = max(wait_s, (self._tok_ts[0][0] + 60.0) - now)
                else:
                    wait_s = max(wait_s, 1.0)

            # 如果需要等待，记录日志并等待
            if wait_s > 0:
                # 统一格式的限速日志
                provider_name = "未知"
                if hasattr(self, '_provider_name'):
                    provider_name = self._provider_name
                logger.info(
                    "[rate-limit] %s 限速等待 %.2fs (rpm=%d/%d, tpm=%d/%d)",
                    provider_name,
                    wait_s,
                    req_count,
                    self._rpm,
                    tok_count,
                    self._tpm,
                )
                time.sleep(wait_s)

            # 记录本次请求
            now2 = time.time()
            self._req_ts.append(now2)
            if self._tpm > 0:
                self._tok_ts.append((now2, max(1, int(estimated_tokens))))


_QIANFAN_RATE_LIMITER: Optional[_SlidingWindowRateLimiter] = None
_SILICONFLOW_RATE_LIMITER: Optional[_SlidingWindowRateLimiter] = None


def _get_qianfan_runtime() -> Dict[str, object]:
    """
    获取主 LLM 运行时设置（已统一使用硅基流动）
    
    向后兼容函数，实际返回硅基流动配置。
    限速标准：Gateway → DashScope qwen-plus (RPM 10000, TPM 2000000)
    """
    # 统一使用硅基流动
    return _get_siliconflow_runtime()

def _siliconflow_temperature() -> float:
    """获取硅基流动温度参数。
    
    硅基流动支持温度范围 [0, 2.0]，默认使用 0.7。
    """
    raw = cfg_str("llm.siliconflow.temperature", "0.7", env="SILICONFLOW_TEMPERATURE")
    try:
        value = float(raw)
    except Exception:
        value = 0.7
    # 硅基流动支持范围 [0, 2.0]
    if value < 0.0:
        value = 0.0
    if value > 2.0:
        value = 2.0
    return value


def _qianfan_temperature() -> float:
    """获取温度参数（向后兼容，实际返回硅基流动温度）。"""
    return _siliconflow_temperature()


def _is_qianfan_base_url(base_url: str) -> bool:
    """Return True if the base_url appears to be SiliconFlow (向后兼容).
    
    已统一使用硅基流动，此函数现在识别硅基流动的 URL。
    """
    # 统一返回是否为硅基流动的判断结果
    return _is_siliconflow_base_url(base_url)


def _qianfan_rate_limiter() -> _SlidingWindowRateLimiter:
    """获取主提供商限速器（向后兼容，实际返回硅基流动限速器）"""
    return _siliconflow_rate_limiter()


def _validate_model_name(model: str, provider: str) -> tuple[bool, str]:
    """
    验证模型名称（简化版本）
    
    Args:
        model: 模型名称
        provider: 提供商名称 ("qianfan" 或 "siliconflow")
    
    Returns:
        (is_valid, error_message)
    """
    # 简化逻辑：总是返回 True，因为我们使用固定的模型配置
    return True, ""


def _get_siliconflow_runtime() -> Dict[str, object]:
    """
    获取硅基流动运行时设置
    
    默认使用 Qwen/Qwen3-8B 模型（由 LLM_GATEWAY_CHAT_MODEL 覆盖）
    """
    try:
        from INAGENT.utils.llm_config import get_siliconflow_config
        config = get_siliconflow_config()
        
        # 确保base_url为网关地址
        base_url = config.get("base_url", "")
        if not base_url or not str(base_url).strip():
            raise ValueError(
                "LLM_GATEWAY_BASE_URL 未配置，auto_convert 禁止直连 API。"
            )
        
        return {
            "api_key": config.get("api_key", ""),
            "base_url": base_url,
            "model": SILICONFLOW_MODEL,  # 默认 Qwen/Qwen3-8B
            "timeout": config.get("timeout", 60),
            # 客户端限速：Gateway 路由到 DashScope qwen-plus (官方 RPM=30000, TPM=5000000)
            # 保守取一半，避免突发流量触发阿里侧保护
            "rpm": 10000,
            "tpm": 2000000,
        }
    except Exception as e:
        logger.warning("从统一配置获取运行时设置失败: %s", e)
        raise ValueError(
            "LLM_GATEWAY_BASE_URL 未配置，auto_convert 禁止直连 API。"
        )


def _is_siliconflow_base_url(base_url: str) -> bool:
    """判断是否为硅基流动的 base_url"""
    if not base_url:
        return False
    normalized = base_url.strip().lower()
    return "siliconflow" in normalized or "api.siliconflow.cn" in normalized


def _siliconflow_rate_limiter() -> _SlidingWindowRateLimiter:
    global _SILICONFLOW_RATE_LIMITER
    if _SILICONFLOW_RATE_LIMITER is None:
        runtime = _get_siliconflow_runtime()
        _SILICONFLOW_RATE_LIMITER = _SlidingWindowRateLimiter(
            rpm=int(runtime.get("rpm", 0) or 0),
            tpm=int(runtime.get("tpm", 0) or 0),
        )
        # 标记提供商名称用于日志
        _SILICONFLOW_RATE_LIMITER._provider_name = "LLM Gateway"
    return _SILICONFLOW_RATE_LIMITER


def _get_rate_limiter(base_url: str) -> Optional[_SlidingWindowRateLimiter]:
    """返回硅基流动限速器（已统一使用硅基流动）
    
    限速标准（Gateway → DashScope 路由）：
    - 对话模型：RPM 10000, TPM 2000000
    """
    # 统一使用硅基流动限速器
    return _siliconflow_rate_limiter()


def _call_llm_with_retry_and_fallback(
    client: Any,
    base_url: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    response_format: Optional[Dict[str, str]] = None,
    stream: bool = False,
    max_tokens: Optional[int] = None,
    max_retries: int = 6,
    initial_delay: float = 1.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    allow_fallback: bool = True,
    fallback_config: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    统一的LLM API调用函数，包含限速、指数退避重试和故障转移
    
    参考硅基流动文档：
    https://github.com/siliconflow/siliconcloud-cookbook/blob/main/examples/rate-limit/how-to-handle-rate-limit-in-siliconcloud.ipynb
    
    Args:
        client: OpenAI客户端实例
        base_url: API基础URL
        model: 模型名称
        messages: 消息列表
        temperature: 温度参数
        response_format: 响应格式（如JSON）
        stream: 是否流式响应
        max_retries: 最大重试次数
        initial_delay: 初始延迟（秒）
        exponential_base: 指数退避基数
        jitter: 是否添加随机抖动
        allow_fallback: 是否允许故障转移
        fallback_config: 故障转移配置（包含text, meta, config等）
    
    Returns:
        API响应对象
    
    Raises:
        Exception: 如果所有重试和故障转移都失败
    """
    import random
    
    # 估算token数量（用于限速）
    prompt_text = "\n".join([msg.get("content", "") for msg in messages])
    limiter = _get_rate_limiter(base_url)
    estimated_tokens = (
        limiter._estimate_tokens(prompt_text) if limiter else 0
    )
    
    # 应用限速
    if limiter:
        limiter.acquire(estimated_tokens=estimated_tokens)
    
    # 指数退避重试
    num_retries = 0
    delay = initial_delay
    
    while True:
        try:
            # 构建请求参数
            request_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if response_format:
                request_kwargs["response_format"] = response_format
            if stream:
                request_kwargs["stream"] = stream
            if max_tokens is not None:
                request_kwargs["max_tokens"] = max_tokens
            
            # 调用API
            return client.chat.completions.create(**request_kwargs)
        
        except (RateLimitError, Exception) as e:
            error_msg = str(e)
            error_code = getattr(e, 'status_code', None) or getattr(e, 'code', None)
            
            # 统一格式：识别提供商名称
            provider_name = "LLM Gateway"
            
            # 检查是否是429错误或限速相关错误
            is_rate_limit = (
                error_code == 429 or
                "429" in error_msg or
                "rate limit" in error_msg.lower() or
                "TPM limit" in error_msg or
                "RPM limit" in error_msg or
                isinstance(e, RateLimitError)
            )
            
            # 如果是限速错误，尝试重试
            if is_rate_limit:
                num_retries += 1
                
                # 检查是否达到最大重试次数
                if num_retries > max_retries:
                    # 如果允许故障转移，尝试切换到备用提供商
                    if allow_fallback and fallback_config:
                        logger.warning(
                            "[LLM] %s 达到最大重试次数 (%d/%d)，尝试故障转移到备用提供商",
                            provider_name,
                            num_retries,
                            max_retries
                        )
                        try:
                            return _try_fallback_provider_call(
                                fallback_config,
                                messages,
                                temperature,
                                response_format,
                                stream,
                            )
                        except Exception as fallback_error:
                            logger.error(
                                "[LLM] %s 故障转移也失败: %s", provider_name, fallback_error
                            )
                            raise Exception(
                                f"所有重试和故障转移都失败。最后错误: {error_msg}"
                            ) from e
                    else:
                        raise Exception(
                            f"{provider_name} 达到最大重试次数 ({max_retries})。错误: {error_msg}"
                        ) from e
                
                # 计算延迟时间（指数退避 + 随机抖动）
                if jitter:
                    delay_with_jitter = delay * (1 + random.random())
                else:
                    delay_with_jitter = delay
                
                # 统一格式的重试日志
                logger.warning(
                    "[LLM] %s 遇到限速错误 (429)，第 %d/%d 次重试，等待 %.2f 秒后重试 (model=%s)",
                    provider_name,
                    num_retries,
                    max_retries,
                    delay_with_jitter,
                    model,
                )
                
                time.sleep(delay_with_jitter)
                delay *= exponential_base
                
            else:
                # 非限速错误，如果是允许故障转移的错误，尝试故障转移
                provider_name = "LLM Gateway"
                if allow_fallback and fallback_config:
                    logger.warning(
                        "[LLM] %s 遇到非限速错误，尝试故障转移: %s (model=%s)",
                        provider_name,
                        error_msg,
                        model
                    )
                    try:
                        return _try_fallback_provider_call(
                            fallback_config,
                            messages,
                            temperature,
                            response_format,
                            stream,
                        )
                    except Exception as fallback_error:
                        logger.error(
                            "[LLM] %s 故障转移失败: %s", provider_name, fallback_error
                        )
                        raise Exception(
                            f"{provider_name} API调用失败且故障转移失败。错误: {error_msg}"
                        ) from e
                else:
                    # 不允许故障转移或没有配置，直接抛出异常
                    logger.error(
                        "[LLM] %s API调用失败: %s (model=%s)",
                        provider_name,
                        error_msg,
                        model
                    )
                    raise


def _try_fallback_provider_call(
    fallback_config: Dict[str, Any],
    messages: List[Dict[str, str]],
    temperature: float,
    response_format: Optional[Dict[str, str]],
    stream: bool,
) -> Any:
    """
    尝试使用备用提供商进行API调用（已弃用）
    
    由于已统一使用硅基流动，故障转移功能已禁用。
    此函数保留用于向后兼容，但会直接抛出异常。
    """
    # 已统一使用硅基流动，不再支持故障转移
    failed_base_url = fallback_config.get("failed_base_url", "")
    failed_model = fallback_config.get("failed_model", "")
    
    logger.warning(
        "[fallback] 故障转移已禁用（已统一使用硅基流动）。"
        "原始请求失败: base_url=%s, model=%s",
        failed_base_url,
        failed_model
    )
    
    raise Exception(
        f"LLM Gateway API 调用失败，故障转移已禁用。"
        f"原始 base_url: {failed_base_url}, model: {failed_model}"
    )


def _probe_siliconflow_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> Tuple[bool, str]:
    """Probe LLM Gateway OpenAI-compatible chat completions.
    
    使用统一的API调用函数，包含限速和重试机制。
    预检失败不会阻止程序运行（通过AUTO_CONVERT_ASSUME_YES控制）。

    Returns:
        (ok, message)
    """
    if not base_url:
        return False, "LLM Gateway base_url 未配置"
    if not api_key:
        return False, "LLM Gateway api_key 未配置"
    if not model:
        return False, "LLM Gateway model 未配置"

    # 使用OpenAI客户端和统一API调用函数（如果可用）
    if OpenAI is not None:
        try:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            
            # 使用统一的API调用函数，但预检不需要故障转移
            # 预检失败时返回False，让调用者决定是否继续
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                temperature=_siliconflow_temperature(),
                max_tokens=1,  # 预检只需要1个token
                allow_fallback=False,  # 预检不需要故障转移
                max_retries=3,  # 预检减少重试次数
            )
            
            # 如果调用成功，返回True
            if response and response.choices:
                return True, "LLM Gateway 预检成功"
            else:
                return False, "LLM Gateway 预检返回空响应"
        
        except Exception as exc:
            error_msg = str(exc)
            # 检查是否是429错误
            error_code = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)
            is_rate_limit = (
                error_code == 429 or
                "429" in error_msg or
                "rate limit" in error_msg.lower() or
                "TPM limit" in error_msg or
                "RPM limit" in error_msg or
                isinstance(exc, RateLimitError)
            )
            
            if is_rate_limit:
                # 429错误：预检时遇到限速，记录警告但返回False
                # 调用者可以通过AUTO_CONVERT_ASSUME_YES继续运行
                return False, f"LLM Gateway 限速 (HTTP 429): {error_msg}"
            else:
                return False, f"LLM Gateway 预检失败: {error_msg}"
    
    # 如果OpenAI客户端不可用，回退到urllib方式（但不应用限速）
    # 这种情况应该很少见，因为OpenAI是核心依赖
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": _siliconflow_temperature(),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return True, f"LLM Gateway 预检成功 (HTTP {status})"
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return False, f"LLM Gateway HTTP {exc.code}: {err_body or str(exc)}"
    except Exception as exc:
        return False, f"LLM Gateway 预检失败: {str(exc)}"


# 向后兼容
def _probe_qianfan_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> Tuple[bool, str]:
    """向后兼容函数，实际调用 LLM Gateway 预检"""
    return _probe_siliconflow_chat(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
    )


def _is_under_doc_local(path: Path) -> bool:
    try:
        path.resolve().relative_to(DOC_LOCAL_DIR.resolve())
        return True
    except Exception:
        return False


def _output_stem_for_file(file_path: Path) -> str:
    """为输出 JSON 生成稳定 stem；外部文件附加路径哈希避免同名冲突。"""
    if _is_under_doc_local(file_path):
        return file_path.stem
    path_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{file_path.stem}_{path_hash}"


def _source_file_label(file_path: Path) -> str:
    """标准化 source_file 标识，外部文件保留可追溯标签。"""
    if _is_under_doc_local(file_path):
        return file_path.name
    path_hash = hashlib.md5(str(file_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{file_path.name} [external:{path_hash}]"


def _collect_extra_input_paths() -> List[Path]:
    """收集额外知识输入路径（文件或目录），支持逗号/分号分隔。"""
    raw = cfg_str(
        "auto_convert.input.extra_paths",
        "",
        env="AUTO_CONVERT_EXTRA_PATHS",
    ).strip()
    if not raw:
        return []
    tokens = [p.strip() for p in re.split(r"[,\n;]+", raw) if p.strip()]
    paths: List[Path] = []
    for token in tokens:
        p = Path(token)
        if not p.exists():
            logger.warning("[input-extra] 路径不存在，忽略: %s", token)
            continue
        paths.append(p)
    return paths


def _iter_files_by_ext(exts: Set[str]) -> List[Path]:
    """遍历 knowledge_base + 额外路径中的指定后缀文件。"""
    files: List[Path] = []
    seen: Set[str] = set()

    def _maybe_add(path: Path) -> None:
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        files.append(path)

    for ext in exts:
        for path in DOC_LOCAL_DIR.rglob(f"*{ext}"):
            if REFERENCE_DIR in path.parents:
                continue
            if (DOC_LOCAL_DIR / "mineru_output") in path.parents:
                continue
            if (DOC_LOCAL_DIR / "mineru_backup") in path.parents:
                continue
            if (DOC_LOCAL_DIR / "logs") in path.parents:
                continue
            if ext == ".pdf" and path.name.endswith("_layout.pdf"):
                continue
            _maybe_add(path)

    for base in _collect_extra_input_paths():
        if base.is_file():
            if base.suffix.lower() in exts:
                _maybe_add(base)
            continue
        for ext in exts:
            for path in base.rglob(f"*{ext}"):
                if ext == ".pdf" and path.name.endswith("_layout.pdf"):
                    continue
                _maybe_add(path)
    return files

def _iter_pdf_files() -> List[Path]:
    return _iter_files_by_ext({".pdf"})


# ============================================================
# Office 文档（docx/xlsx/doc/xls）处理
# ============================================================
OFFICE_EXTENSIONS = {".docx", ".doc", ".xlsx", ".xls"}
TEXT_EXTENSIONS = {".txt"}


def _iter_office_files() -> List[Path]:
    """遍历 knowledge_base 目录下的 Office 文档"""
    return _iter_files_by_ext(OFFICE_EXTENSIONS)


def _iter_text_files() -> List[Path]:
    """遍历 knowledge_base 目录下的 TXT 文档"""
    return _iter_files_by_ext(TEXT_EXTENSIONS)


def _compute_file_fingerprint(file_path: Path) -> str:
    """计算文件 MD5 指纹"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _can_skip_office_cached(file_path: Path, json_path: Path, cache_path: Path) -> bool:
    """检查 Office 文档是否可以跳过（缓存命中）"""
    if not json_path.exists() or not cache_path.exists():
        return False
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_fp = cache.get("source_file_fingerprint", "")
        cached_schema = cache.get("schema_version", "")
        if cached_schema != AUTO_CONVERT_SCHEMA_VERSION:
            return False
        current_fp = _compute_file_fingerprint(file_path)
        return cached_fp == current_fp
    except Exception:
        return False


def convert_office_file(file_path: Path) -> None:
    """
    处理单个 Office 文档（docx/xlsx/doc/xls），生成知识块 JSON。

    流程:
    1. 用 document_classifier 分类文档
    2. 根据类别调用 spec_parser 或 testlist_parser
    3. 对生成的知识块运行 metadata 增强（规则 + LLM）
    4. 写入 reference/ 目录，与 PDF 流水线输出合并
    """
    from INAGENT.data_tools.document_classifier import classify_document, infer_product_module_from_path
    from INAGENT.data_tools.spec_parser import parse_spec_document
    from INAGENT.data_tools.testlist_parser import parse_test_list

    stem = _output_stem_for_file(file_path)
    source_label = _source_file_label(file_path)
    json_path = REFERENCE_DIR / f"{stem}.json"
    cache_path = LOG_DIR / f"{stem}.cache.json"
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _can_skip_office_cached(file_path, json_path, cache_path):
        logger.info("[office-cache] 跳过 %s，内容未变更", file_path.name)
        return

    start_time = time.perf_counter()
    logger.info("[office] 开始处理: %s", file_path.name)

    # 1. 分类
    # 先尝试读取少量内容作为预览（用于内容匹配）
    content_preview = ""
    try:
        from camel.loaders.markitdown import MarkItDownLoader
        loader = MarkItDownLoader()
        full_text = loader.convert_file(str(file_path))
        content_preview = full_text[:2000] if full_text else ""
    except Exception as e:
        logger.warning("[office] 预览读取失败 %s: %s", file_path.name, e)

    category, confidence = classify_document(file_path, content_preview)
    product_module = infer_product_module_from_path(file_path)
    logger.info(
        "[office] 分类结果: %s -> %s (confidence=%.2f, module=%s)",
        file_path.name, category, confidence, product_module,
    )

    # 2. 根据分类调用对应解析器
    suffix = file_path.suffix.lower()
    knowledge_blocks: List[Dict[str, Any]] = []

    if suffix in {".xlsx", ".xls"} or category.startswith("test/test_list"):
        knowledge_blocks = parse_test_list(
            file_path,
            product_module=product_module,
            document_category=category,
        )
    elif suffix in {".docx", ".doc"} or category.startswith("spec/") or category.startswith("test/"):
        knowledge_blocks = parse_spec_document(
            file_path,
            document_category=category,
            product_module=product_module,
        )
    else:
        # 回退：尝试规格解析
        knowledge_blocks = parse_spec_document(
            file_path,
            document_category=category or "unknown",
            product_module=product_module,
        )

    if not knowledge_blocks:
        logger.warning("[office] 未生成任何知识块: %s", file_path.name)
        return

    # 3. 运行 metadata 增强（规则 + index）
    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    product_modules_map = _merge_product_modules_map(
        meta_rules.get("product_modules", {})
    )
    protocol_map = meta_rules.get("protocol_types", {})

    for block in knowledge_blocks:
        meta = block.get("metadata", {})
        meta["source_file"] = source_label
        content = str(block.get("page_content") or "")
        lower_text = content.lower()

        # product_module（如果解析器未设置，则用规则匹配）
        if not meta.get("product_module") or meta.get("product_module") == "unknown":
            for module, keywords in product_modules_map.items():
                if any(k.lower() in lower_text for k in keywords):
                    meta["product_module"] = module
                    break

        # protocol_type
        if not meta.get("protocol_type"):
            found_protocols = _match_protocols_word_boundary(protocol_map, content)
            if found_protocols:
                meta["protocol_type"] = found_protocols

    # 增强 scenario_id / step_type
    _enhance_metadata_with_function_index(knowledge_blocks)

    # 4. 写入 JSON
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 5. 写入缓存
    cache_payload = {
        "source_file": str(file_path),
        "source_file_fingerprint": _compute_file_fingerprint(file_path),
        "schema_version": AUTO_CONVERT_SCHEMA_VERSION,
        "document_category": category,
        "document_category_confidence": confidence,
        "product_module": product_module,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.perf_counter() - start_time
    logger.info(
        "[office] 完成: %s -> %s (%d 块, %.2fs)",
        file_path.name, json_path.name, len(knowledge_blocks), elapsed,
    )


# ============================================================
# TXT 文档处理
# ============================================================
# Bug fix detail 字段提取模式
_BUG_FIX_FIELD_RE = re.compile(
    r"^(Description|Root Cause|Condition of Occurrence|Fixed Details"
    r"|Testing Suggestions|Affected Release|Extra Impact)\s*:",
    re.MULTILINE,
)


def _parse_bug_fix_text(text: str, source_file: str) -> List[Dict[str, Any]]:
    """将 bug fix detail 文本拆分为结构化知识块。"""
    blocks: List[Dict[str, Any]] = []

    # 提取 Bug ID
    bug_match = re.search(r"Bug\s+(\d+)", text, re.IGNORECASE)
    bug_id = bug_match.group(1) if bug_match else ""

    # 提取关键字段
    fields: Dict[str, str] = {}
    positions = list(_BUG_FIX_FIELD_RE.finditer(text))
    for i, m in enumerate(positions):
        key = m.group(1)
        start = m.end()
        end = positions[i + 1].start() if i + 1 < len(positions) else len(text)
        value = text[start:end].strip().strip(":")
        # 截断到下一个 Comment 行（SVN commit 部分不属于该字段）
        comment_cut = re.search(r"\nComment\s+\d+", value)
        if comment_cut:
            value = value[: comment_cut.start()].strip()
        fields[key] = value

    # 提取 SVN commit 信息
    svn_commits: List[str] = []
    for m in re.finditer(r"New Revision:\s*(\d+)", text):
        rev = m.group(1)
        # 向后找到 Modified 行获取受影响文件
        mod_match = re.search(
            r"Modified:\s*\n\s*(.+?)(?:\nLog:|\n\n)", text[m.start() :]
        )
        file_changed = mod_match.group(1).strip() if mod_match else ""
        svn_commits.append(f"r{rev}: {file_changed}")

    base_meta = {
        "source_file": source_file,
        "document_category": "review/bug_fix",
        "bug_id": bug_id,
    }

    # Block 1: 概要
    first_line = text.split("\n")[0].strip()
    blocks.append(
        {
            "page_content": f"Bug {bug_id} 概述: {first_line}",
            "metadata": {
                **base_meta,
                "block_type": "bug_summary",
                "block_id": f"bug_{bug_id}_summary",
                "section_title": "Bug 概述",
            },
        }
    )

    # Block 2: 详情（Description + Root Cause + Condition + Fix）
    detail_parts = []
    for key in [
        "Description",
        "Root Cause",
        "Condition of Occurrence",
        "Fixed Details",
    ]:
        if key in fields:
            detail_parts.append(f"{key}: {fields[key]}")
    if detail_parts:
        blocks.append(
            {
                "page_content": "\n".join(detail_parts),
                "metadata": {
                    **base_meta,
                    "block_type": "bug_detail",
                    "block_id": f"bug_{bug_id}_detail",
                    "section_title": "Bug 详情与修复",
                },
            }
        )

    # Block 3: 代码修改记录
    if svn_commits:
        affected = fields.get("Affected Release", "")
        commit_text = "代码修改记录:\n" + "\n".join(svn_commits)
        if affected:
            commit_text += f"\nAffected Release: {affected}"
        blocks.append(
            {
                "page_content": commit_text,
                "metadata": {
                    **base_meta,
                    "block_type": "code_change",
                    "block_id": f"bug_{bug_id}_code",
                    "section_title": "代码修改记录",
                },
            }
        )

    # Block 4: 测试建议
    if "Testing Suggestions" in fields:
        blocks.append(
            {
                "page_content": f"Testing Suggestions: {fields['Testing Suggestions']}",
                "metadata": {
                    **base_meta,
                    "block_type": "test_suggestion",
                    "block_id": f"bug_{bug_id}_test",
                    "section_title": "测试建议",
                },
            }
        )

    return blocks


def _parse_generic_text(text: str, source_file: str, category: str) -> List[Dict[str, Any]]:
    """将通用 TXT 文件按段落拆分为知识块。"""
    blocks: List[Dict[str, Any]] = []
    paragraphs = re.split(r"\n{2,}", text.strip())
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para or len(para) < 10:
            continue
        blocks.append(
            {
                "page_content": para,
                "metadata": {
                    "source_file": source_file,
                    "document_category": category,
                    "block_type": "text_paragraph",
                    "block_id": f"para_{i + 1}",
                },
            }
        )
    return blocks


def convert_text_file(file_path: Path) -> None:
    """
    处理单个 TXT 文档，生成知识块 JSON。

    流程：
    1. 读取文本内容
    2. 用 document_classifier 分类
    3. 根据分类选择解析方式（bug_fix 专用解析 / 通用段落解析）
    4. 运行 metadata 增强
    5. 写入 reference/ 目录
    """
    from INAGENT.data_tools.document_classifier import (
        classify_document,
        infer_product_module_from_path,
    )

    stem = _output_stem_for_file(file_path)
    source_label = _source_file_label(file_path)
    json_path = REFERENCE_DIR / f"{stem}.json"
    cache_path = LOG_DIR / f"{stem}.cache.json"
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _can_skip_office_cached(file_path, json_path, cache_path):
        logger.info("[text-cache] 跳过 %s，内容未变更", file_path.name)
        return

    start_time = time.perf_counter()
    logger.info("[text] 开始处理: %s", file_path.name)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    content_preview = text[:2000]

    category, confidence = classify_document(file_path, content_preview)
    product_module = infer_product_module_from_path(file_path)
    logger.info(
        "[text] 分类结果: %s -> %s (confidence=%.2f, module=%s)",
        file_path.name,
        category,
        confidence,
        product_module,
    )

    # 根据分类选择解析方式
    if category == "review/bug_fix":
        knowledge_blocks = _parse_bug_fix_text(text, source_label)
    else:
        knowledge_blocks = _parse_generic_text(text, source_label, category)

    if not knowledge_blocks:
        logger.warning("[text] 未生成任何知识块: %s", file_path.name)
        return

    # metadata 增强
    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    product_modules_map = _merge_product_modules_map(
        meta_rules.get("product_modules", {})
    )
    protocol_map = meta_rules.get("protocol_types", {})

    for block in knowledge_blocks:
        meta = block.get("metadata", {})
        content = str(block.get("page_content") or "")
        lower_text = content.lower()

        if product_module and product_module != "unknown":
            meta.setdefault("product_module", product_module)
        if not meta.get("product_module") or meta["product_module"] == "unknown":
            for module, keywords in product_modules_map.items():
                if any(k.lower() in lower_text for k in keywords):
                    meta["product_module"] = module
                    break

        if not meta.get("protocol_type"):
            found_protocols = _match_protocols_word_boundary(protocol_map, content)
            if found_protocols:
                meta["protocol_type"] = found_protocols

    _enhance_metadata_with_function_index(knowledge_blocks)

    # 写入 JSON
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 写入缓存
    cache_payload = {
        "source_file": str(file_path),
        "source_file_fingerprint": _compute_file_fingerprint(file_path),
        "schema_version": AUTO_CONVERT_SCHEMA_VERSION,
        "document_category": category,
        "document_category_confidence": confidence,
        "product_module": product_module,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.perf_counter() - start_time
    logger.info(
        "[text] 完成: %s -> %s (%d 块, %.2fs)",
        file_path.name,
        json_path.name,
        len(knowledge_blocks),
        elapsed,
    )


def _fallback_convert_pdf_with_markitdown(pdf: Path, reason: str = "") -> bool:
    """当 MinerU 不可用时，回退为 MarkItDown 纯文本抽取。"""
    try:
        from camel.loaders.markitdown import MarkItDownLoader
        from INAGENT.data_tools.document_classifier import (
            classify_document,
            infer_product_module_from_path,
        )
    except Exception as exc:
        logger.warning("[pdf-fallback] 依赖不可用，无法回退: %s", exc)
        return False

    json_path, cache_path = _target_paths(pdf)
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger.warning("[pdf-fallback] 启动 MarkItDown 回退: %s (reason=%s)", pdf.name, reason)
    try:
        text = MarkItDownLoader().convert_file(str(pdf)) or ""
    except Exception as exc:
        logger.warning("[pdf-fallback] 文本抽取失败: %s", exc)
        return False
    if not text.strip():
        logger.warning("[pdf-fallback] 抽取文本为空: %s", pdf.name)
        return False

    category, confidence = classify_document(pdf, text[:2000])
    product_module = infer_product_module_from_path(pdf)
    source_label = json_path.name
    knowledge_blocks = _parse_generic_text(text, source_label, category or "spec/design")
    if not knowledge_blocks:
        logger.warning("[pdf-fallback] 未生成知识块: %s", pdf.name)
        return False

    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    product_modules_map = _merge_product_modules_map(
        meta_rules.get("product_modules", {})
    )
    protocol_map = meta_rules.get("protocol_types", {})

    for block in knowledge_blocks:
        meta = block.get("metadata", {})
        content = str(block.get("page_content") or "")
        lower_text = content.lower()
        if product_module and product_module != "unknown":
            meta.setdefault("product_module", product_module)
        if not meta.get("product_module") or meta["product_module"] == "unknown":
            for module, keywords in product_modules_map.items():
                if any(k.lower() in lower_text for k in keywords):
                    meta["product_module"] = module
                    break
        if not meta.get("protocol_type"):
            found_protocols = _match_protocols_word_boundary(protocol_map, content)
            if found_protocols:
                meta["protocol_type"] = found_protocols

    _enhance_metadata_with_function_index(knowledge_blocks)
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cache_payload = {
        "source_file": str(pdf),
        "source_file_fingerprint": _compute_file_fingerprint(pdf),
        "schema_version": AUTO_CONVERT_SCHEMA_VERSION,
        "document_category": category,
        "document_category_confidence": confidence,
        "product_module": product_module,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
        "fallback_parser": "markitdown",
        "fallback_reason": reason,
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "[pdf-fallback] 完成: %s -> %s (%d 块)",
        pdf.name,
        json_path.name,
        len(knowledge_blocks),
    )
    return True


def _prompt_continue_on_error(component: str, error: str) -> bool:
    if cfg_str("auto_convert.assume_yes", "", env="AUTO_CONVERT_ASSUME_YES").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }:
        logger.info(
            "[%s] AUTO_CONVERT_ASSUME_YES is set; continue despite error.",
            component,
        )
        return True

    prompt = (
        f"{component} 不可用或配置异常：{error}\n"
        "是否继续？继续输入 Y，取消输入 N: "
    )
    print(prompt, end="", flush=True)
    try:
        answer = sys.stdin.readline().strip().lower()
    except Exception:
        return False
    return answer in {"y", "yes"}


def _target_paths(pdf: Path) -> Tuple[Path, Path]:
    stem = _output_stem_for_file(pdf)
    json_path = REFERENCE_DIR / f"{stem}.json"
    cache_path = LOG_DIR / f"{stem}.cache.json"
    return json_path, cache_path


def _find_existing_mineru_output(task_id: str) -> Optional[Path]:
    r"""Find an existing MinerU content list JSON for a given task.

    Prefer *_content_list_v2.json when present.

    Returns:
        Optional[Path]: Path to content list JSON if found.
    """
    task_dir = MINERU_OUTPUT_DIR / task_id
    if not task_dir.exists():
        return None

    preferred = [
        task_dir / "hybrid_auto" / f"{task_id}_content_list_v2.json",
        task_dir / "hybrid_auto" / f"{task_id}_content_list.json",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate

    # Fallback: search recursively
    for root, _dirs, files in os.walk(task_dir):
        for file in files:
            if file.endswith("_content_list_v2.json"):
                return Path(root) / file
    for root, _dirs, files in os.walk(task_dir):
        for file in files:
            if file.endswith("_content_list.json"):
                return Path(root) / file
    return None


def _can_reuse_mineru_output(pdf: Path, content_list_path: Path) -> bool:
    r"""Return True if existing MinerU output is newer than the input PDF."""
    try:
        return content_list_path.stat().st_mtime >= pdf.stat().st_mtime
    except Exception:
        return False


def _compute_pdf_fingerprint(pdf: Path) -> Dict[str, object]:
    stat = pdf.stat()
    sha256 = hashlib.sha256()
    with pdf.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256.hexdigest(),
    }


def _compute_config_fingerprint() -> str:
    """Compute fingerprint of only the LLM-relevant config sections.

    We ignore general MinerU settings (like vLLM URL, timeouts) to avoid
    unnecessary re-processing when infrastructure settings change.
    """
    config = _load_project_config()
    relevant = {
        "llm-aided-config": config.get("llm-aided-config", {}),
        "metadata_rules": config.get("metadata_rules", {}),
        # Include model source just in case it affects which model is used
        "MINERU_MODEL_SOURCE": os.environ.get("MINERU_MODEL_SOURCE", ""),
        "AUTO_CONVERT_SCHEMA_VERSION": AUTO_CONVERT_SCHEMA_VERSION,
    }
    # Stable JSON dump
    payload = json.dumps(relevant, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _update_cache_mtime(cache_path: Path, new_fingerprint: Dict[str, object]) -> None:
    """Update only the file stats in the cache file to match current reality."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        data["source_pdf_fingerprint"] = new_fingerprint
        cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[cache] updated mtime/size for %s (content unchanged)", cache_path.name)
    except Exception as e:
        logger.warning("[cache] failed to update mtime: %s", e)


def _can_skip_cached(
    pdf: Path,
    json_path: Path,
    cache_path: Path,
) -> bool:
    if not (json_path.exists() and cache_path.exists()):
        return False
    try:
        meta = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    cached_fingerprint = meta.get("source_pdf_fingerprint", {})
    if not isinstance(cached_fingerprint, dict):
        return False

    # 1. Config Check (Strict)
    cached_config = meta.get("config_fingerprint")
    if cached_config and cached_config != _compute_config_fingerprint():
        logger.info("[cache] config changed for %s, re-processing.", pdf.name)
        return False

    # 2. Fast Path: Mtime & Size Match
    current_stat = pdf.stat()
    if (
        cached_fingerprint.get("size") == current_stat.st_size
        and cached_fingerprint.get("mtime") == current_stat.st_mtime
    ):
        return True

    # 3. Slow Path: Content Hash Check
    # If mtime/size changed, we must verify if content actually changed.
    logger.info("[cache] mtime/size changed for %s, verifying content...", pdf.name)
    cached_hash = cached_fingerprint.get("sha256")
    if not cached_hash:
        return False  # Old cache format without hash

    current_fingerprint = _compute_pdf_fingerprint(pdf)
    if current_fingerprint.get("sha256") == cached_hash:
        # Content is identical! Update cache file with new mtime/size so next check is fast.
        _update_cache_mtime(cache_path, current_fingerprint)
        return True

    logger.info("[cache] content changed for %s, re-processing.", pdf.name)
    return False


def _clean_chunk_text(text: str) -> str:
    """Normalize text by removing excess whitespace and special characters."""
    # Fix hyphenation (word- break -> wordbreak)
    text = re.sub(r'(\w+)-\s+(\w+)', r'\1\2', text)
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove non-printable characters (basic check)
    text = "".join(ch for ch in text if ch.isprintable())
    return text.strip()

def _apply_llm_metadata_extraction(text: str, meta: Dict[str, object], config: Dict) -> None:
    """外部接口，调用内部实现"""
    _apply_llm_metadata_extraction_internal(text, meta, config, allow_fallback=True)


# ---------------------------------------------------------------------------
# Batch LLM metadata extraction  (reduces API calls by ~10x)
# ---------------------------------------------------------------------------

_BATCH_LLM_SIZE = 40  # chunks per LLM call (was 10; 40 fits within 8K-context models)
_BATCH_TEXT_LIMIT = 500  # chars per chunk in batch prompt (was 800; trimmed to fit larger batches)


def _apply_llm_metadata_extraction_batch(
    items: List[Tuple[str, Dict[str, object]]],
    config: Dict,
) -> None:
    """Apply LLM metadata extraction for a batch of (clean_text, meta) pairs.

    Merges up to _BATCH_LLM_SIZE chunks into a single prompt, asks the LLM
    to return a JSON array of metadata objects (one per chunk, in order).
    Falls back to per-item extraction on any parse failure.
    """
    if not items:
        return

    global _QIANFAN_AVAILABLE, _LLM_METADATA_AVAILABLE
    if _QIANFAN_AVAILABLE is False or _LLM_METADATA_AVAILABLE is False:
        return

    selected_api_key, selected_base_url, selected_model = _select_provider_for_thread()
    if not (selected_api_key and selected_base_url and selected_model):
        runtime = _get_siliconflow_runtime()
        selected_api_key = str(runtime.get("api_key") or "")
        selected_base_url = str(runtime.get("base_url") or "")
        selected_model = str(runtime.get("model") or "")
    if not selected_api_key:
        return

    if OpenAI is None:
        return

    if _LLM_METADATA_AVAILABLE is None:
        if not selected_base_url:
            _LLM_METADATA_AVAILABLE = False
            return
        _LLM_METADATA_AVAILABLE = _ensure_urls_reachable(
            [selected_base_url],
            attempts=NET_RETRY_ATTEMPTS,
            delay=NET_RETRY_DELAY,
            timeout=NET_TIMEOUT,
            label="llm-metadata-batch",
            accept_http_error=True,
        )
        if not _LLM_METADATA_AVAILABLE:
            return

    full_config = _load_project_config()
    meta_rules = full_config.get("metadata_rules", {})
    valid_intents = list(meta_rules.get("intents", {}).keys())
    valid_config_modes = list(meta_rules.get("config_modes", {}).keys())
    product_modules_map = meta_rules.get("product_modules", {})
    valid_product_modules = list(product_modules_map.keys())
    valid_protocol_types = list(meta_rules.get("protocol_types", {}).keys())
    valid_command_prefixes = list(meta_rules.get("command_prefixes", {}).keys())
    pm_descs = [f"{m} (kw: {', '.join(kws[:3])})" for m, kws in product_modules_map.items()]

    # build per-chunk summaries
    chunk_lines = []
    for idx, (text, meta_item) in enumerate(items):
        sec = str(meta_item.get("section_title") or "").strip()
        par = str(meta_item.get("parent_section") or "").strip()
        src = str(meta_item.get("source_file") or "")
        snippet = text[:_BATCH_TEXT_LIMIT]
        header = f"[CHUNK {idx}]"
        if sec:
            header += f" section={sec}"
        if par:
            header += f" parent={par}"
        if src:
            header += f" source={src}"
        chunk_lines.append(f"{header}\n{snippet}")

    chunks_block = "\n---\n".join(chunk_lines)

    prompt = (
        "You are a batch metadata extractor for technical documentation chunks.\n"
        "Extract metadata for EACH chunk below. Return a JSON array with one object per chunk, in order.\n\n"
        "For each chunk, extract:\n"
        "- product_module (string, feature module like SLB/LLB/基础网络/安全)\n"
        "- protocol_type (list of strings, e.g. ['HTTP','TCP'])\n"
        "- intent (string)\n"
        "- config_mode (string: cli/console/api)\n"
        "- command_prefix (string, first command word if CLI)\n"
        "- description (string, max 30 words)\n"
        "- required_keywords (list of strings)\n"
        "- section_title (string, no numbering)\n"
        "- parent_section (string, no numbering)\n"
        "- scenario_id (string)\n"
        "- step_type (string)\n"
        "- function_hierarchy (string, e.g. 'SLB > Health Check > HTTP')\n\n"
        f"Valid Intents: {valid_intents}\n"
        f"Valid Config Modes: {valid_config_modes}\n"
        f"Valid Modules: {', '.join(pm_descs[:20])}\n"
        f"Valid Protocols: {valid_protocol_types}\n"
        f"Valid Prefixes: {valid_command_prefixes}\n\n"
        "Return ONLY a JSON array of objects. No markdown. No explanation.\n\n"
        f"{chunks_block}"
    )

    try:
        client = OpenAI(
            api_key=selected_api_key,
            base_url=selected_base_url,
            timeout=config.get("timeout", 120),
        )
        response = _call_llm_with_retry_and_fallback(
            client=client,
            base_url=selected_base_url,
            model=selected_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=_siliconflow_temperature(),
            response_format={"type": "json_object"},
            allow_fallback=False,
            fallback_config=None,
        )
        content = response.choices[0].message.content or "[]"
        if "```" in content:
            match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if match:
                content = match.group(1).strip()
        # The API returns json_object which is a single object. We asked for an
        # array, but some models wrap it: {"results": [...]} or just [...].
        if json_repair:
            parsed = json_repair.loads(content)
        else:
            parsed = json.loads(content)

        if isinstance(parsed, dict):
            # Unwrap {"results": [...]} or {"chunks": [...]} etc.
            for key in ("results", "chunks", "data", "items", "metadata"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
            else:
                # Single dict — wrap as one-element list
                parsed = [parsed]

        if not isinstance(parsed, list):
            raise ValueError(f"Expected list, got {type(parsed)}")

    except Exception as e:
        logger.warning("[batch-llm] Failed to parse batch response (%s); falling back to per-item", e)
        # Fallback: call per-item extraction
        for text, meta_item in items:
            _apply_llm_metadata_extraction_internal(text, meta_item, config, allow_fallback=True)
        return

    # Merge results back
    for idx, (text, meta_item) in enumerate(items):
        if idx < len(parsed) and isinstance(parsed[idx], dict):
            llm_meta = parsed[idx]
        else:
            # Missing entry — do per-item fallback for this chunk
            _apply_llm_metadata_extraction_internal(text, meta_item, config, allow_fallback=True)
            continue

        # Apply same merge logic as per-item version
        if llm_meta.get("intent"):
            meta_item["intent"] = llm_meta["intent"]
        if llm_meta.get("config_mode"):
            meta_item["config_mode"] = llm_meta["config_mode"]
        if llm_meta.get("required_keywords") and isinstance(llm_meta["required_keywords"], list):
            meta_item["required_keywords"] = llm_meta["required_keywords"]
        if llm_meta.get("product_module"):
            pm = str(llm_meta["product_module"]).strip()
            pm = {"未知": "unknown", "未知模块": "unknown"}.get(pm, pm)
            meta_item["product_module"] = "unknown" if pm.lower() in ["unknown", "未知", ""] else pm
        if llm_meta.get("protocol_type") and isinstance(llm_meta["protocol_type"], list):
            meta_item["protocol_type"] = llm_meta["protocol_type"]
        if llm_meta.get("command_prefix"):
            meta_item["command_prefix"] = llm_meta["command_prefix"]
        if llm_meta.get("description"):
            meta_item["description"] = llm_meta["description"]
        if llm_meta.get("section_title"):
            meta_item["section_title"] = llm_meta["section_title"]
        if llm_meta.get("parent_section"):
            meta_item["parent_section"] = llm_meta["parent_section"]
        if llm_meta.get("scenario_id"):
            meta_item["scenario_id"] = llm_meta["scenario_id"]
        if llm_meta.get("step_type"):
            meta_item["step_type"] = llm_meta["step_type"]
        if llm_meta.get("function_hierarchy"):
            meta_item["function_hierarchy"] = llm_meta["function_hierarchy"]
        if llm_meta.get("command_structure") and isinstance(llm_meta["command_structure"], dict):
            meta_item["command_structure"] = llm_meta["command_structure"]


def _select_provider_for_thread() -> Tuple[str, str, str]:
    """
    为当前线程选择提供商（已统一使用 LLM Gateway）
    
    Returns:
        (api_key, base_url, model) 元组
    """
    thread_id = threading.get_ident()
    
    try:
        # 统一使用 LLM Gateway
        runtime = _get_siliconflow_runtime()
        api_key = str(runtime.get("api_key") or "")
        base_url = str(runtime.get("base_url") or "")
        model = str(runtime.get("model") or "")
        
        # 诊断日志
        if not api_key:
            logger.warning("[gateway] api_key 未配置，请检查 LLM_GATEWAY_API_KEY 环境变量")
        if not base_url:
            logger.warning("[gateway] base_url 未配置，请检查 LLM_GATEWAY_BASE_URL 环境变量")
        if not model:
            logger.warning("[gateway] model 未配置，请检查 LLM_GATEWAY_CHAT_MODEL 环境变量")
        
        if api_key and base_url and model:
            logger.debug("[gateway] 线程 %d 使用网关 (model=%s)", thread_id, model)
            return api_key, base_url, model
        else:
            logger.error(
                "[gateway] 网关配置不完整 (api_key=%s, base_url=%s, model=%s)",
                "已配置" if api_key else "未配置",
                "已配置" if base_url else "未配置",
                "已配置" if model else "未配置"
            )
            return None, None, None
    except Exception as e:
        logger.error("[gateway] 线程 %d 获取配置时出错: %s", thread_id, e)
        return None, None, None


def _apply_llm_metadata_extraction_internal(
    text: str, 
    meta: Dict[str, object], 
    config: Dict,
    allow_fallback: bool = False  # 已禁用故障转移
) -> None:
    """
    应用 LLM 进行元数据提取（已统一使用 LLM Gateway）
    """
    global _QIANFAN_AVAILABLE  # 保留变量名用于向后兼容
    if _QIANFAN_AVAILABLE is False:
        return

    # 统一使用 LLM Gateway
    selected_api_key, selected_base_url, selected_model = _select_provider_for_thread()
    
    if selected_api_key and selected_base_url and selected_model:
        api_key = selected_api_key
        base_url = selected_base_url
        model = selected_model
    else:
        # 如果 _select_provider_for_thread 返回空，直接从网关配置获取
        runtime = _get_siliconflow_runtime()
        api_key = str(runtime.get("api_key") or "")
        base_url = str(runtime.get("base_url") or "")
        model = str(runtime.get("model") or "")
        
        # 如果仍然没有配置，记录警告
        if not api_key:
            logger.warning(
                "LLM_GATEWAY_API_KEY 未配置。"
                "请在 INAGENT/.env 文件中配置网关 API Key。"
            )
        if not base_url:
            logger.warning(
                "LLM_GATEWAY_BASE_URL 未配置。"
                "请在 INAGENT/.env 文件中配置网关地址。"
            )
        if not model:
            logger.warning(
                "LLM_GATEWAY_CHAT_MODEL 未配置。"
                "请在 INAGENT/.env 文件中配置网关模型名称。"
            )

    if not api_key:
        logger.warning(
            "LLM_GATEWAY_API_KEY 未配置，跳过 LLM 元数据提取。"
        )
        return

    global _LLM_METADATA_AVAILABLE
    if _LLM_METADATA_AVAILABLE is False:
        return

    try:
        if OpenAI is None:
            logger.warning("OpenAI client not available for LLM metadata extraction.")
            return

        if _LLM_METADATA_AVAILABLE is None:
            if not base_url:
                logger.warning("base_url not defined for metadata_extraction; skipping.")
                _LLM_METADATA_AVAILABLE = False
                return

            # Simple check if the URL is reachable, no complex auth needed for this check
            _LLM_METADATA_AVAILABLE = _ensure_urls_reachable(
                [base_url],
                attempts=NET_RETRY_ATTEMPTS,
                delay=NET_RETRY_DELAY,
                timeout=NET_TIMEOUT,
                label="llm-metadata",
                accept_http_error=True,
            )
            if not _LLM_METADATA_AVAILABLE:
                logger.warning(f"LLM endpoint '{base_url}' not reachable; skipping.")
                return

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=config.get("timeout", 60),
        )
        
        # 统一使用硅基流动
        provider_name = "硅基流动"
        thread_id = threading.get_ident()
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] 线程 %d 调用 metadata extraction (提供商=%s, model=%s, base_url=%s, text_length=%d)", 
                        thread_id, provider_name, model, base_url, len(text))
        else:
            # INFO级别也记录提供商信息（但频率较低，每10次记录一次）
            if not hasattr(_apply_llm_metadata_extraction_internal, '_provider_log_counter'):
                _apply_llm_metadata_extraction_internal._provider_log_counter = {}
            if provider_name not in _apply_llm_metadata_extraction_internal._provider_log_counter:
                _apply_llm_metadata_extraction_internal._provider_log_counter[provider_name] = 0
            _apply_llm_metadata_extraction_internal._provider_log_counter[provider_name] += 1
            
            # 每10次调用记录一次，或者提供商切换时记录
            if (_apply_llm_metadata_extraction_internal._provider_log_counter[provider_name] % 10 == 1 or
                not hasattr(_apply_llm_metadata_extraction_internal, '_last_logged_provider') or
                _apply_llm_metadata_extraction_internal._last_logged_provider != provider_name):
                logger.info("[LLM] 线程 %d 使用提供商: %s (model=%s, 已调用 %d 次)", 
                           thread_id, provider_name, model,
                           _apply_llm_metadata_extraction_internal._provider_log_counter[provider_name])
                _apply_llm_metadata_extraction_internal._last_logged_provider = provider_name

        full_config = _load_project_config()
        meta_rules = full_config.get("metadata_rules", {})
        valid_intents = list(meta_rules.get("intents", {}).keys())
        valid_config_modes = list(meta_rules.get("config_modes", {}).keys())

        # Get section context if available (for better product module inference)
        # Extract section_title from clean_text if it looks like a section header
        # Pattern: starts with numbers (e.g., "11.3.1. HTTP/TCP...")
        section_title = str(meta.get("section_title") or "").strip()
        parent_section = str(meta.get("parent_section") or "").strip()

        if section_title:
            section_title = _remove_section_number(section_title)
        elif meta.get("clean_text"):
            clean_text = str(meta.get("clean_text", "")).strip()
            # Match section headers: "11.3.1. ..." or "11.3.1 ..." or "第11章 ..."
            section_match = re.match(
                r"^(?:\d+\.)+\s*[^\n]+|^第\d+[章节]\s+[^\n]+",
                clean_text,
            )
            if section_match:
                section_title_raw = section_match.group(0).strip()
                section_title = _remove_section_number(section_title_raw)

        if parent_section:
            parent_section = _remove_section_number(parent_section)

        # Get candidate lists from metadata_rules with descriptions
        product_modules_map = meta_rules.get("product_modules", {})
        # Build product module descriptions for better LLM understanding
        product_modules_desc = []
        for module, keywords in product_modules_map.items():
            desc = f"{module} (keywords: {', '.join(keywords[:3])})"
            product_modules_desc.append(desc)
        valid_product_modules = list(product_modules_map.keys())
        
        protocol_types_map = meta_rules.get("protocol_types", {})
        valid_protocol_types = list(protocol_types_map.keys())
        
        command_prefixes_map = meta_rules.get("command_prefixes", {})
        valid_command_prefixes = list(command_prefixes_map.keys())

        cli_format_rules = meta_rules.get("cli_format_rules", {})
        format_rules_text = "\n".join([f"- {k}: {v}" for k, v in cli_format_rules.items()])

        # Build enhanced prompt with better guidance and examples
        # Check if text is likely generic (table of contents, copyright, etc.)
        text_lower = text.lower()
        is_generic = any(kw in text_lower for kw in ["目录", "copyright", "版权", "商标", "合格声明", "关于我们", "联系我们", "table of contents"])
        
        # Build product module descriptions with examples
        product_modules_desc = []
        for module, keywords in product_modules_map.items():
            desc = f"{module} (keywords: {', '.join(keywords[:5])})"
            product_modules_desc.append(desc)
        
        # Detect document type from source_file or content
        source_file = meta.get("source_file", "")
        is_cli_doc = "cli" in source_file.lower()
        is_app_doc = "app" in source_file.lower()
        
        doc_type_hint = ""
        if is_cli_doc:
            doc_type_hint = "This is from a CLI command manual. Focus on command syntax, command prefixes, and CLI-specific patterns."
        elif is_app_doc:
            doc_type_hint = "This is from an application/configuration guide. Focus on configuration examples, descriptions, and conceptual explanations. Even without CLI syntax, extract product_module from section titles and content keywords."
        else:
            doc_type_hint = "This could be from either CLI or APP documentation. Apply appropriate extraction rules based on content."
        
        prompt = (
            "You are analyzing a technical documentation chunk (CLI manual or Application/Configuration guide). "
            "Extract comprehensive metadata that works for BOTH command syntax and configuration examples.\n\n"
            f"{doc_type_hint}\n\n"
            "CRITICAL RULES FOR METADATA EXTRACTION:\n\n"
            "1. **product_module (功能模块)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - **完全基于内容分析**：根据数据块标题和内容自主识别功能模块，不要依赖预设列表\n"
            "   - **识别策略**（按优先级）：\n"
            "     a) 从章节标题识别：分析章节标题中的功能关键词\n"
            "        - 示例：'HTTP协议的服务器负载均衡配置' -> 识别为'SLB'\n"
            "        - 示例：'路由配置' -> 识别为'基础网络'\n"
            "     b) 从命令前缀识别（CLI文档）：\n"
            "        - 'slb'命令 -> 识别为'SLB'\n"
            "        - 'llb'命令 -> 识别为'LLB'\n"
            "        - 'interface'/'route'命令 -> 识别为'基础网络'\n"
            "     c) 从内容关键词识别：\n"
            "        - 'virtual service'/'backend service'/'健康检查' -> 'SLB'\n"
            "        - 'interface'/'route'/'vlan'/'ip地址' -> '基础网络'\n"
            "        - 'firewall'/'acl'/'ssl'/'证书' -> '安全'\n"
            "   - **模块命名规则**：\n"
            "     - 使用简洁的功能名称（如'SLB'、'LLB'、'基础网络'、'安全'等）\n"
            "     - 如果无法确定具体模块，使用'基础网络'作为默认值\n"
            "     - 只有真正通用的内容（目录、版权、关于我们、联系我们、商标/合格声明）才使用'unknown'\n"
            "     - 不能仅因内容简短就选择'unknown'，应优先根据章节标题推断模块\n"
            "   - **协议类型 vs 功能模块**：\n"
            "     - HTTP、HTTPS、TCP、UDP等是协议类型，不是功能模块\n"
            "     - 功能模块应该是功能性的（如SLB、LLB、基础网络、安全等）\n"
            "     - 协议类型应该放在'protocol_type'字段中\n"
            "   - **不能是章节编号**：不能将'11.3.1'等编号识别为product_module\n\n"
            "2. **protocol_type (协议类型)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - 提取所有提到的协议类型（HTTP、HTTPS、TCP、UDP、ICMP、DNS、FTP、SIP、RTSP等）\n"
            "   - 协议类型是约束条件，不是功能模块\n"
            "   - 可以为空列表[]（如果内容不涉及协议）\n"
            "   - 示例：'HTTP协议的服务器负载均衡配置' -> protocol_type: ['HTTP']\n\n"
            "3. **step_type (步骤类型)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - 根据内容推断步骤类型（如health_checks、virtual_services、basic_config等）\n"
            "   - 使用通用的步骤类型名称（不带模块前缀）\n"
            "   - 后续会添加模块前缀（如slb_health_checks）\n\n"
            "4. **section_title (章节标题，去除编号)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - 从clean_text中提取章节标题\n"
            "   - **必须去除章节编号**（如'11.3.1. HTTP配置' -> 'HTTP配置'）\n"
            "   - 只保留有意义的标题内容\n"
            "   - 用于功能树节点的显示名称\n\n"
            "5. **parent_section (父章节，去除编号)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - 从MinerU的parent_section提取或从层级关系推断\n"
            "   - **必须去除章节编号**\n"
            "   - 用于建立功能树的父子关系\n\n"
            "6. **function_hierarchy (功能层级)** - MUST BE FIRST DETERMINED ⭐\n"
            "   - 从section_title和parent_section构建\n"
            "   - 格式：'SLB > Health Check > HTTP'（不包含编号）\n"
            "   - 用于确定数据块在功能树中的位置\n"
            "   - 用于判断是新功能还是已有功能的子功能\n\n"
            "7. **数据块分类决策**\n"
            "   - 如果function_hierarchy只有一层，且product_module是新出现的 -> 新功能模块\n"
            "   - 如果function_hierarchy有多层 -> 已有功能的子功能\n"
            "   - 如果有明确的parent_section -> 已有功能的子功能\n\n"
            "Return JSON with these keys:\n"
            "- 'intent' (string from Valid Intents)\n"
            "- 'config_mode' (string from Valid Config Modes: 'cli' for CLI syntax, 'console' for GUI/WebUI descriptions, 'api' for API docs)\n"
            "- 'product_module' (string, REQUIRED - 根据内容自主识别，不要依赖预设列表。必须是功能性的，不能是协议类型或章节编号)\n"
            "- 'protocol_type' (list of strings, 所有提到的协议类型，空[]如果无协议)\n"
            "- 'command_prefix' (string, first command word if CLI syntax present, '' otherwise)\n"
            "- 'command_structure' (object, only if CLI syntax: {command_body, subcommands, required_params, optional_params, choice_groups}, {{}} otherwise)\n"
            "- 'function_hierarchy' (string, 从section_title和parent_section构建，格式：'SLB > Health Check > HTTP'，不包含编号)\n"
            "- 'description' (string, max 50 words)\n"
            "- 'required_keywords' (list of strings, important technical terms)\n"
            "- 'section_title' (string, 从clean_text提取，**必须去除编号**，如'11.3.1. HTTP配置' -> 'HTTP配置')\n"
            "- 'parent_section' (string, 从parent_section metadata提取或推断，**必须去除编号**)\n"
            "- 'scenario_id' (string, infer from section_title and content, e.g., 'HTTP_SLB_CONFIG', 'TCP_SLB_CONFIG', or 'unknown' if cannot infer)\n"
            "- 'step_type' (string, infer from content, e.g., 'basic_config', 'health_checks', 'policies_and_algorithms', or '' if cannot infer)\n\n"
            f"Valid Intents: {valid_intents}\n"
            f"Valid Config Modes: {valid_config_modes}\n"
            f"Valid Product Modules:\n" + "\n".join([f"  - {desc}" for desc in product_modules_desc]) + "\n"
            f"Valid Protocol Types: {valid_protocol_types}\n"
            f"Valid Command Prefixes: {valid_command_prefixes}\n"
            f"CLI Format Rules (only applies if CLI syntax present):\n{format_rules_text}\n"
            + (f"Section Title: {section_title}\n" if section_title else "")
            + f"Existing Metadata: {json.dumps({k: v for k, v in meta.items() if k not in ['clean_text', 'page_content']}, ensure_ascii=False)}\n\n"
            "CONCRETE EXAMPLES (BOTH CLI and APP docs):\n"
            "1. CLI: 'slb real <ip> [port]' -> {product_module: 'SLB', command_prefix: 'slb', config_mode: 'cli', command_structure: {...}, section_title: '', scenario_id: 'unknown', step_type: ''}\n"
            "2. APP: '11. 服务器负载均衡（SLB）' -> {product_module: 'SLB', config_mode: 'console', section_title: '11. 服务器负载均衡（SLB）', scenario_id: 'unknown', step_type: ''}\n"
            "3. APP: 'HTTP health check configuration for virtual service' -> {product_module: 'SLB', protocol_type: ['HTTP'], config_mode: 'console', scenario_id: 'HTTP_SLB_CONFIG', step_type: 'health_check'}\n"
            "4. APP: '配置示例：如何配置HTTP类型的SLB服务' -> {product_module: 'SLB', protocol_type: ['HTTP'], config_mode: 'console', scenario_id: 'HTTP_SLB_CONFIG', step_type: 'basic_config'}\n"
            "5. CLI: 'interface eth0 ip <address>' -> {product_module: '基础网络', command_prefix: 'interface', config_mode: 'cli', section_title: '', scenario_id: 'unknown', step_type: ''}\n"
            "6. APP: '11.3.1. HTTP/TCP/FTP/UDP/HTTPS/TCPS/DNS协议的负载均衡配置' -> {product_module: 'SLB', protocol_type: ['HTTP', 'TCP', 'FTP', 'UDP', 'HTTPS', 'TCPS', 'DNS'], config_mode: 'console', section_title: '11.3.1. HTTP/TCP/FTP/UDP/HTTPS/TCPS/DNS协议的负载均衡配置', scenario_id: 'HTTP_SLB_CONFIG', step_type: 'basic_config', function_hierarchy: 'SLB > HTTP > 基础配置'}\n"
            "7. CLI: 'show slb virtual-service' -> {product_module: 'SLB', command_prefix: 'show', config_mode: 'cli', section_title: '', scenario_id: 'unknown', step_type: ''}\n"
            "8. Generic: '目录' -> {product_module: 'unknown', section_title: '', scenario_id: 'unknown', step_type: ''}\n\n"
            "IMPORTANT: Return ONLY valid JSON object. No Markdown, no code blocks, no explanations.\n"
            f"Text to analyze:\n{text[:3000]}"
        )

        llm_start_time = time.perf_counter()
        try:
            # 使用统一的API调用函数，包含限速、重试和故障转移
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=_siliconflow_temperature(),  # 统一使用硅基流动温度配置
                response_format={"type": "json_object"},
                allow_fallback=allow_fallback,
                fallback_config={
                    "text": text,
                    "meta": meta,
                    "config": config,
                    "failed_base_url": base_url,
                    "failed_model": model,
                } if allow_fallback else None,
            )
        except Exception as e:
            # 所有重试和故障转移都失败
            logger.warning(f"LLM metadata extraction failed after retries and fallback: {e}")
            return
        llm_elapsed = time.perf_counter() - llm_start_time
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] metadata extraction 调用完成，耗时 %.2f 秒", llm_elapsed)

        content = response.choices[0].message.content or "{}"

        # Attempt to clean up markdown code blocks if present
        if "```" in content:
            # Try to extract content inside ```json ... ``` or ``` ... ```
            match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if match:
                content = match.group(1).strip()
            else:
                content = content.replace("```json", "").replace("```", "").strip()

        try:
            if json_repair:
                llm_meta = json_repair.loads(content)
            else:
                llm_meta = json.loads(content)
        except Exception as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}. Content: {content[:100]}...")
            llm_meta = {}

        if llm_meta.get("intent"):
            meta["intent"] = llm_meta["intent"]
        if llm_meta.get("config_mode"):
            meta["config_mode"] = llm_meta["config_mode"]
        if llm_meta.get("required_keywords") and isinstance(llm_meta["required_keywords"], list):
            meta["required_keywords"] = llm_meta["required_keywords"]
        # Validate and normalize product_module (信任LLM的判断，移除硬编码验证)
        if llm_meta.get("product_module"):
            pm_value = str(llm_meta["product_module"]).strip()
            # Normalize common variations
            pm_normalized = {
                "未知": "unknown",
                "未知模块": "unknown",
                "关于我们": "unknown",
                "联系我们": "unknown",
            }.get(pm_value, pm_value)
            
            # 移除硬编码验证，信任LLM的判断
            # 只做基本的规范化（处理空字符串、未知等）
            if pm_normalized.lower() in ["unknown", "未知", ""]:
                meta["product_module"] = "unknown"
            else:
                # 直接使用LLM识别的模块名称（不限制在valid_product_modules中）
                meta["product_module"] = pm_normalized
                # 记录日志以便后续分析
                if pm_normalized not in valid_product_modules:
                    logger.debug(f"LLM识别到新模块: '{pm_normalized}' (不在预设列表中，但已接受)")
        else:
            # LLM未返回，使用默认值
            meta["product_module"] = "基础网络"
        
        # Validate protocol_type (must be from valid list)
        if llm_meta.get("protocol_type") and isinstance(llm_meta["protocol_type"], list):
            valid_protocols = [p for p in llm_meta["protocol_type"] if str(p).upper() in [pt.upper() for pt in valid_protocol_types]]
            if valid_protocols:
                meta["protocol_type"] = valid_protocols
        
        # Validate command_prefix (must be from valid list or empty)
        if llm_meta.get("command_prefix"):
            cp_value = str(llm_meta["command_prefix"]).strip().lower()
            if cp_value in [cp.lower() for cp in valid_command_prefixes]:
                meta["command_prefix"] = cp_value
            # If not in list but looks like a command, try to extract first word
            elif cp_value and any(cp in text.lower() for cp in valid_command_prefixes):
                # Extract first matching command prefix from text
                for cp in valid_command_prefixes:
                    if cp.lower() in text.lower():
                        meta["command_prefix"] = cp.lower()
                        break
        
        if llm_meta.get("command_structure") and isinstance(llm_meta["command_structure"], dict):
            meta["command_structure"] = llm_meta["command_structure"]
        if llm_meta.get("function_hierarchy"):
            meta["function_hierarchy"] = llm_meta["function_hierarchy"]

        meta["description"] = llm_meta.get("description", "No description generated.")
        
        # Extract new fields: section_title, parent_section, scenario_id, step_type
        # section_title: 检查LLM返回的值，如果为空字符串则使用fallback，并去除编号
        llm_section_title = llm_meta.get("section_title", "").strip() if llm_meta.get("section_title") else ""
        if llm_section_title:
            # LLM应该已经去除编号，但为了安全再次去除
            meta["section_title"] = _remove_section_number(llm_section_title)
        elif section_title:  # Fallback to extracted section_title
            # section_title已经在提取时去除了编号
            meta["section_title"] = section_title
        else:
            meta["section_title"] = ""
        
        # parent_section: 检查LLM返回的值，如果为空字符串则使用fallback，并去除编号
        llm_parent_section = llm_meta.get("parent_section", "").strip() if llm_meta.get("parent_section") else ""
        if llm_parent_section:
            # LLM应该已经去除编号，但为了安全再次去除
            meta["parent_section"] = _remove_section_number(llm_parent_section)
        elif parent_section:  # Fallback to MinerU's parent_section (已在前面去除编号)
            meta["parent_section"] = parent_section
        else:
            meta["parent_section"] = ""
        
        # function_hierarchy: 确保不包含编号
        if llm_meta.get("function_hierarchy"):
            fh_value = str(llm_meta["function_hierarchy"]).strip()
            # 去除function_hierarchy中每个层级部分的编号
            if fh_value:
                # 分割层级，去除每个部分的编号，然后重新组合
                parts = [part.strip() for part in fh_value.split(">")]
                cleaned_parts = [_remove_section_number(part) for part in parts]
                meta["function_hierarchy"] = " > ".join(cleaned_parts)
            else:
                meta["function_hierarchy"] = ""
        else:
            meta["function_hierarchy"] = ""
        
        # Extract scenario_id (validate against function_index if available)
        # 即使LLM返回"unknown"或空字符串，也要设置，以便增强阶段可以处理
        if llm_meta.get("scenario_id"):
            scenario_id_value = str(llm_meta["scenario_id"]).strip()
            # Normalize: if "unknown", keep it; otherwise validate format
            if scenario_id_value.lower() == "unknown" or scenario_id_value == "":
                meta["scenario_id"] = "unknown"
            else:
                # Accept scenario_id from LLM (will be validated/enhanced in _enhance_metadata_with_function_index)
                meta["scenario_id"] = scenario_id_value
        else:
            # LLM没有返回scenario_id，设置为"unknown"以便增强阶段可以处理
            meta["scenario_id"] = "unknown"
        
        # Extract step_type (validate against function_index if available)
        if llm_meta.get("step_type"):
            step_type_value = str(llm_meta["step_type"]).strip()
            if step_type_value:
                meta["step_type"] = step_type_value

    except Exception as e:
        # 错误已在上面处理，这里只是兜底
        if allow_fallback:
            logger.warning(f"LLM metadata extraction failed: {e}")
        else:
            raise


def _enhance_metadata_with_function_index(knowledge_blocks: List[Dict[str, any]]) -> None:
    """
    基于功能结构索引增强 knowledge_blocks 的 metadata（添加 scenario_id 和 step_type）。
    
    Args:
        knowledge_blocks: 知识库块列表，每个块包含 page_content 和 metadata
    """
    function_index_path = DOC_LOCAL_DIR / "function_structure_index.json"
    if not function_index_path.exists():
        logger.debug("功能结构索引不存在，跳过 metadata 增强: %s", function_index_path)
        return
    
    try:
        with open(function_index_path, "r", encoding="utf-8") as f:
            function_index = json.load(f)
    except Exception as e:
        logger.warning("加载功能结构索引失败，跳过 metadata 增强: %s", e)
        return
    
    scenarios = function_index.get("scenarios", {})
    
    # 从索引动态加载步骤类型关键词（不硬编码）
    from INAGENT.utils.index_utils import get_step_type_keywords
    step_type_keywords = get_step_type_keywords(function_index)
    
    for block in knowledge_blocks:
        metadata = block.get("metadata", {})
        text = block.get("page_content", "")
        text_lower = text.lower()
        
        # 提取 product_module 和 protocol_type
        product_module = metadata.get("product_module", "")
        protocol_types = metadata.get("protocol_type", [])
        if isinstance(protocol_types, str):
            protocol_types = [protocol_types]
        elif not isinstance(protocol_types, list):
            protocol_types = []
        
        # 匹配场景 ID（每次都重新计算，避免旧值残留）
        scenario_id: Optional[str] = None
        for sid, scenario_config in scenarios.items():
            product_match = any(
                pm.lower() in text_lower or pm == product_module
                for pm in scenario_config.get("product_modules", [])
            )
            protocol_match = (
                not scenario_config.get("protocol_types") or
                any(
                    pt.lower() in text_lower or pt in protocol_types
                    for pt in scenario_config.get("protocol_types", [])
                )
            )
            if product_match and protocol_match:
                scenario_id = sid
                break
        
        # 识别步骤类型
        step_type = None
        for stype, keywords in step_type_keywords.items():
            if any(kw.lower() in text_lower for kw in keywords):
                step_type = stype
                break
        
        # 如果已有 function_hierarchy，尝试从中提取 step_type
        function_hierarchy = metadata.get("function_hierarchy", "")
        if function_hierarchy and not step_type:
            hierarchy_lower = function_hierarchy.lower()
            for stype, keywords in step_type_keywords.items():
                if any(kw.lower() in hierarchy_lower for kw in keywords):
                    step_type = stype
                    break
        
        # 更新 metadata（优先使用LLM提取的值，只在缺失时补充）
        # scenario_id: 如果LLM已提取且不是"unknown"，优先使用；否则使用增强阶段匹配的结果
        current_scenario_id = metadata.get("scenario_id")
        if current_scenario_id and current_scenario_id != "unknown":
            # LLM已提取有效的scenario_id，保留它
            pass
        elif scenario_id:
            # LLM未提取或提取为"unknown"，使用增强阶段匹配的结果
            metadata["scenario_id"] = scenario_id
        elif current_scenario_id == "unknown":
            # 保持"unknown"状态
            pass
        else:
            # 如果之前没有设置，至少设置为"unknown"以便后续处理
            if not current_scenario_id:
                metadata["scenario_id"] = "unknown"
        
        # step_type: 如果LLM已提取，优先使用；否则使用增强阶段识别的结果
        current_step_type = metadata.get("step_type")
        if current_step_type:
            # LLM已提取step_type，保留它
            pass
        elif step_type:
            # LLM未提取，使用增强阶段识别的结果
            metadata["step_type"] = step_type


def _extract_chunk_metadata(
    text: str, base_meta: Optional[Dict[str, Any]] = None
) -> Dict[str, object]:
    config = _load_project_config()
    meta_rules = config.get("metadata_rules", {})
    llm_config = config.get("llm-aided-config", {}).get("metadata_extraction", {})

    clean_text = _clean_chunk_text(text)
    meta: Dict[str, object] = {"clean_text": clean_text}
    section_title = ""
    parent_section = ""
    if base_meta:
        section_title = str(base_meta.get("section_title") or "").strip()
        parent_section = str(base_meta.get("parent_section") or "").strip()
        if section_title:
            meta["section_title"] = section_title
        if parent_section:
            meta["parent_section"] = parent_section
        section_path = base_meta.get("section_path")
        if section_path:
            meta["section_path"] = section_path
    lower_text = clean_text.lower()
    lower_section = f"{section_title} {parent_section}".lower()

    # Rule-based Intent Extraction (Externalized in mineru.json)
    intents_map = meta_rules.get("intents", {})
    for intent, keywords in intents_map.items():
        if any(k.lower() in lower_text for k in keywords):
            meta["intent"] = intent
            break

    # Rule-based Product Module Extraction (Externalized in mineru.json)
    product_modules_map = _merge_product_modules_map(
        meta_rules.get("product_modules", {})
    )
    for module, keywords in product_modules_map.items():
        if any(k.lower() in lower_text for k in keywords) or any(
            k.lower() in lower_section for k in keywords
        ):
            meta["product_module"] = module
            break

    # Rule-based Protocol Type Extraction (Externalized in mineru.json)
    protocol_map = meta_rules.get("protocol_types", {})
    found_protocols = _match_protocols_word_boundary(protocol_map, lower_text)
    if found_protocols:
        meta["protocol_type"] = found_protocols

    # Rule-based Command Prefix Extraction (Externalized in mineru.json)
    command_prefixes = meta_rules.get("command_prefixes", {})
    for prefix, keywords in command_prefixes.items():
        if any(k.lower() in lower_text for k in keywords):
            meta["command_prefix"] = prefix
            break

    # Rule-based Config Mode (Externalized in mineru.json)
    modes_map = meta_rules.get("config_modes", {})
    for mode, keywords in modes_map.items():
        if any(k.lower() in lower_text for k in keywords):
            meta["config_mode"] = mode
            break

    # LLM Enhancement (Optional)
    if llm_config.get("enable", False):
        _apply_llm_metadata_extraction(clean_text, meta, llm_config)

    return meta


def _build_models_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _prompt_continue_without_vllm() -> bool:
    if cfg_str("auto_convert.assume_yes", "", env="AUTO_CONVERT_ASSUME_YES").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }:
        logger.info(
            "[vllm] AUTO_CONVERT_ASSUME_YES is set; continue without vLLM."
        )
        return True
    prompt = "vLLM 不可用，是否继续使用本地后端？继续输入 Y，取消输入 N: "
    print(prompt, end="", flush=True)
    try:
        # Use sys.stdin.readline to avoid potential input() buffering issues
        answer = sys.stdin.readline().strip().lower()
    except Exception:
        return False
    return answer in {"y", "yes"}


def _check_url_once(
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    *,
    accept_http_error: bool = False,
) -> Tuple[bool, Optional[str]]:
    try:
        request = urllib.request.Request(url, method="GET", headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout):
            return True, None
    except urllib.error.HTTPError as exc:
        # Some providers return 401/403/404 on base URLs even when the host is
        # reachable. For connectivity checks, treat any <500 status as
        # "reachable" when accept_http_error=True.
        if accept_http_error and getattr(exc, "code", 0) < 500:
            return True, None
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _fetch_vllm_model_name(base_url: str, timeout: float) -> Optional[str]:
    models_url = _build_models_url(base_url)
    request = urllib.request.Request(models_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[vllm] model list request failed: %s", exc)
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("[vllm] model list response is not JSON.")
        return None

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if isinstance(first, dict):
        return str(first.get("id")) if first.get("id") else None
    return None


def _probe_vllm_completion(base_url: str, model: str, timeout: float) -> bool:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except Exception as exc:
        logger.warning("[vllm] inference probe failed: %s", exc)
        return False


def _ensure_urls_reachable(
    urls: List[str],
    *,
    attempts: int,
    delay: float,
    timeout: float,
    label: str,
    headers: Optional[Dict[str, str]] = None,
    accept_http_error: bool = False,
) -> bool:
    if not urls:
        return True
    for attempt in range(1, attempts + 1):
        failures: Dict[str, str] = {}
        for url in urls:
            ok, error = _check_url_once(
                url,
                timeout,
                headers=headers,
                accept_http_error=accept_http_error,
            )
            if not ok:
                failures[url] = error or "unknown error"
        if not failures:
            return True
        logger.warning(
            "[%s] connectivity check failed (attempt %d/%d): %s",
            label,
            attempt,
            attempts,
            failures,
        )
        if attempt < attempts:
            time.sleep(delay * attempt)
    return False


def _get_frontmatter_config() -> Dict:
    config = _load_project_config()
    llm_config = config.get("llm-aided-config", {})
    frontmatter = llm_config.get("frontmatter_filter", {})
    if not frontmatter:
        # Fallback: reuse title_aided config if present
        title_aided = llm_config.get("title_aided", {})
        if title_aided:
            frontmatter = {
                "enable": False,
                "api_key": title_aided.get("api_key"),
                "base_url": title_aided.get("base_url"),
                "model": title_aided.get("model"),
                "timeout": title_aided.get("timeout", 600),
                "stream": False,
                "max_pages": 10,
                "max_chars_per_page": 800,
            }
    return frontmatter


def _extract_text_from_block(block: Dict) -> str:
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return text

    content = block.get("content")
    if not isinstance(content, dict):
        return ""

    if "title_content" in content:
        return "".join(
            part.get("content", "")
            for part in content.get("title_content", [])
            if isinstance(part, dict)
        )
    if "paragraph_content" in content:
        return "".join(
            part.get("content", "")
            for part in content.get("paragraph_content", [])
            if isinstance(part, dict)
        )
    if "list_items" in content:
        items = []
        for item in content.get("list_items", []):
            item_content = item.get("item_content", [])
            items.append(
                "".join(
                    part.get("content", "")
                    for part in item_content
                    if isinstance(part, dict)
                )
            )
        return "\n".join(items)
    return ""


def _build_section_context_map(blocks: List[Dict]) -> Dict[int, Dict[str, str]]:
    """Build section context (section_title/parent_section/section_path)
    for each block index using MinerU hierarchy or numbered headings.
    """
    section_stack: List[Tuple[int, str]] = []
    context_map: Dict[int, Dict[str, str]] = {}

    for idx, block in enumerate(blocks):
        raw_text = _extract_text_from_block(block)
        clean_text = _clean_chunk_text(raw_text)
        block_type = str(block.get("type") or "").lower()
        text_level = block.get("text_level")

        heading_level: Optional[int] = None
        heading_title = ""

        if isinstance(text_level, int) and text_level > 0:
            heading_level = int(text_level)
            heading_title = clean_text
        else:
            inferred_level = _infer_section_level_from_heading(clean_text)
            if inferred_level:
                heading_level = inferred_level
                heading_title = clean_text
            elif block_type in {"title", "heading", "section"} and clean_text:
                heading_level = 1
                heading_title = clean_text

        if heading_level and heading_title:
            normalized_title = _remove_section_number(heading_title)
            if normalized_title:
                while section_stack and section_stack[-1][0] >= heading_level:
                    section_stack.pop()
                section_stack.append((heading_level, normalized_title))

        if section_stack:
            section_title = section_stack[-1][1]
            parent_section = section_stack[-2][1] if len(section_stack) > 1 else ""
            section_path = " > ".join([item[1] for item in section_stack])
        else:
            section_title = ""
            parent_section = ""
            section_path = ""

        context_map[idx] = {
            "section_title": section_title,
            "parent_section": parent_section,
            "section_path": section_path,
        }

    return context_map


def _build_page_previews(blocks: List[Dict], max_chars: int) -> Dict[int, str]:
    pages: Dict[int, List[str]] = {}
    for block in blocks:
        page_idx = block.get("page_idx")
        if page_idx is None:
            continue
        text = _extract_text_from_block(block)
        if not text:
            continue
        pages.setdefault(int(page_idx), []).append(text)

    previews: Dict[int, str] = {}
    for page_idx, chunks in pages.items():
        joined = "\n".join(chunks)
        previews[page_idx] = joined[:max_chars]
    return previews


def _parse_llm_json(content: str) -> Dict[int, bool]:
    content = content.strip()
    if not content:
        return {}
    # Try to extract the first JSON object from the response.
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        raw = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}

    normalized: Dict[int, bool] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                normalized[int(key)] = bool(value)
            except (TypeError, ValueError):
                continue
    return normalized


def _classify_frontmatter_pages(
    previews: Dict[int, str], frontmatter_cfg: Dict
) -> List[int]:
    """
    使用 LLM 分类前置页（已统一使用硅基流动）
    """
    if not previews:
        return []

    if OpenAI is None:
        logger.warning(
            "OpenAI client not available; skipping frontmatter filter."
        )
        return []

    # 统一使用硅基流动
    runtime = _get_siliconflow_runtime()
    api_key = str(runtime.get("api_key") or "")
    base_url = str(runtime.get("base_url") or "")
    model = str(runtime.get("model") or "")
    
    # 如果没有配置，记录警告
    if not api_key:
        logger.warning("LLM_GATEWAY_API_KEY 未配置，跳过 frontmatter filter")
    if not base_url:
        logger.warning("LLM_GATEWAY_BASE_URL 未配置，跳过 frontmatter filter")
    if not model:
        logger.warning("LLM_GATEWAY_CHAT_MODEL 未配置，跳过 frontmatter filter")

    if not api_key:
        logger.warning("硅基流动 api_key 未配置，跳过 frontmatter filter")
        return []

    # Simple connectivity check (lenient for 4xx HTTP status).
    if not _ensure_urls_reachable(
        [base_url],
        attempts=NET_RETRY_ATTEMPTS,
        delay=NET_RETRY_DELAY,
        timeout=NET_TIMEOUT,
        label="llm-frontmatter",
        accept_http_error=True,
    ):
        logger.warning(
            "LLM base_url not reachable (base_url=%s, model=%s); skipping.",
            base_url,
            model,
        )
        return []

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=frontmatter_cfg.get("timeout", 600),
    )

    prompt_lines = [
        "You are given page previews from a technical document.",
        "Mark pages that are frontmatter (TOC, copyright, preface,",
        "legal notices, acknowledgements, about/contact).",
        "Return JSON mapping page_idx to true/false.",
        "Only use page_idx keys provided.",
        "",
    ]
    for page_idx, preview in previews.items():
        prompt_lines.append(f"[page_idx={page_idx}]\n{preview}\n")

    try:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] 调用 frontmatter filter (base_url=%s, model=%s, pages=%d)", 
                        base_url, model, len(previews))
        llm_start_time = time.perf_counter()
        try:
            # 使用统一的API调用函数，包含限速和重试
            # frontmatter filter 不是关键功能，失败时返回空列表即可
            response = _call_llm_with_retry_and_fallback(
                client=client,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": "\n".join(prompt_lines)}],
                temperature=_siliconflow_temperature(),  # 统一使用硅基流动温度
                stream=frontmatter_cfg.get("stream", False),
                allow_fallback=False,  # frontmatter filter 不需要故障转移
                max_retries=3,  # 减少重试次数（非关键功能）
            )
        except Exception as e:
            # frontmatter filter 失败不影响主流程，记录警告即可
            logger.warning(
                "[LLM] frontmatter filter 调用失败，跳过: %s", e
            )
            return []  # 返回空列表，不阻塞处理
        llm_elapsed = time.perf_counter() - llm_start_time
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[LLM] frontmatter filter 调用完成，耗时 %.2f 秒", llm_elapsed)
    except Exception as exc:
        logger.warning(
            "Frontmatter filter failed (base_url=%s, model=%s): %s",
            base_url,
            model,
            exc,
        )
        return []

    if frontmatter_cfg.get("stream", False):
        content_parts = []
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                content_parts.append(chunk.choices[0].delta.content)
        content = "".join(content_parts)
    else:
        content = response.choices[0].message.content or ""

    result = _parse_llm_json(content)
    return [idx for idx, is_front in result.items() if is_front]


def _filter_frontmatter_blocks(blocks: List[Dict]) -> Tuple[List[Dict], List[int]]:
    frontmatter_cfg = _get_frontmatter_config()
    if not frontmatter_cfg.get("enable", False):
        return blocks, []

    max_chars = int(frontmatter_cfg.get("max_chars_per_page", 800))
    max_pages = frontmatter_cfg.get("max_pages", 10)

    previews = _build_page_previews(blocks, max_chars=max_chars)
    if max_pages and len(previews) > max_pages:
        previews = dict(sorted(previews.items())[: int(max_pages)])

    start_time = time.perf_counter()
    frontmatter_pages = _classify_frontmatter_pages(previews, frontmatter_cfg)
    elapsed = time.perf_counter() - start_time
    logger.info(
        "Frontmatter filter: %d page(s) flagged in %.2fs",
        len(frontmatter_pages),
        elapsed,
    )

    if not frontmatter_pages:
        return blocks, []

    filtered = [
        block
        for block in blocks
        if block.get("page_idx") not in frontmatter_pages
    ]
    return filtered, frontmatter_pages


async def convert_one(reader: LocalMinerUReader, pdf: Path, max_pages: Optional[int] = None) -> None:
    json_path, cache_path = _target_paths(pdf)

    if _can_skip_cached(pdf, json_path, cache_path):
        logger.info("[cache] skip %s, content unchanged.", pdf.name)
        return

    start_time = time.perf_counter()
    if max_pages:
        logger.info("[mineru] processing %s (limited to first %d pages for testing)...", pdf, max_pages)
    else:
        logger.info("[mineru] processing %s ...", pdf)
    
    # Configure backend arguments
    extra_args = []
    use_vllm_backend = USE_DOCKER_VLLM
    if use_vllm_backend:
        global _VLLM_AVAILABLE
        if _VLLM_AVAILABLE is None:
            _VLLM_AVAILABLE = _ensure_urls_reachable(
                [_build_models_url(DOCKER_VLLM_URL)],
                attempts=NET_RETRY_ATTEMPTS,
                delay=NET_RETRY_DELAY,
                timeout=NET_TIMEOUT,
                label="mineru-vllm",
            )
        if not _VLLM_AVAILABLE:
            use_vllm_backend = False
            logger.warning(
                "[mineru] vLLM server not reachable; falling back to local backend."
            )

    if use_vllm_backend:
        # hybrid-http-client: Local Layout + Remote VLM
        # Check docs: https://opendatalab.github.io/MinerU/zh/quick_start/extension_modules/#clientopenai-hybrid-http-client
        extra_args = ["-b", "hybrid-http-client", "-u", DOCKER_VLLM_URL]
        logger.info("[mineru] Using Docker vLLM backend: %s", DOCKER_VLLM_URL)
    urls_to_check = [HUGGINGFACE_API_CHECK]
    if USE_DOCKER_VLLM:
        urls_to_check.append(_build_models_url(DOCKER_VLLM_URL))

    # Skip network check for MinerU because modelscope handles connectivity internally
    # and we want to avoid blocking on HuggingFace timeouts.
    # if not _ensure_urls_reachable(
    #     urls_to_check,
    #     attempts=NET_RETRY_ATTEMPTS,
    #     delay=NET_RETRY_DELAY,
    #     timeout=NET_TIMEOUT,
    #     label="mineru",
    # ):
    #     raise RuntimeError("Network check failed for MinerU dependencies.")

    # Stream MinerU CLI logs to terminal in real time (with retry)
    # NOTE: task_id equals the input filename stem in LocalMinerUReader.
    task_id = pdf.stem
    existing_output = _find_existing_mineru_output(task_id)
    if existing_output and _can_reuse_mineru_output(pdf, existing_output):
        logger.info(
            "[mineru] reuse existing output for task_id=%s (%s)",
            task_id,
            existing_output,
        )
    else:
        task_id = ""
        last_error: Optional[Exception] = None
        for attempt in range(1, NET_RETRY_ATTEMPTS + 1):
            try:
                task_id = await reader.submit_task(
                    str(pdf),
                    extra_args=extra_args,
                    cwd=str(MINERU_OUTPUT_DIR),
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[mineru] submit failed (attempt %d/%d): %s",
                    attempt,
                    NET_RETRY_ATTEMPTS,
                    exc,
                )
                if attempt < NET_RETRY_ATTEMPTS:
                    await asyncio.sleep(NET_RETRY_DELAY * attempt)
        if not task_id:
            raise RuntimeError(
                f"MinerU submit failed after retries: {last_error}"
            )
    mineru_elapsed_s = time.perf_counter() - start_time
    logger.info(
        "[mineru] finished task_id=%s in %.2fs",
        task_id,
        mineru_elapsed_s,
    )

    # 1) 读取结构化 JSON（如果没有就跳过，保持 blocks 为空）
    blocks: Dict | List[Dict] | List = []
    try:
        raw_json = await reader.get_task_output(task_id)
        blocks = json.loads(raw_json)
    except FileNotFoundError:
        logger.warning(
            "[warn] no *_content_list.json found for task %s, "
            "metadata blocks will be empty.",
            task_id,
        )
    except json.JSONDecodeError as e:
        logger.warning(
            "[warn] failed to parse JSON output for %s: %s",
            task_id,
            e,
        )

    frontmatter_pages: List[int] = []
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
        # Run synchronous frontmatter filter in thread to avoid blocking loop
        blocks, frontmatter_pages = await asyncio.to_thread(_filter_frontmatter_blocks, blocks)

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Classify the PDF document using filename patterns + content preview
    from INAGENT.data_tools.document_classifier import classify_document, infer_product_module_from_path
    _content_preview_for_classify = ""
    if isinstance(blocks, list):
        _preview_texts = []
        for _blk in blocks[:10]:
            if isinstance(_blk, dict):
                _preview_texts.append(_extract_text_from_block(_blk))
        _content_preview_for_classify = "\n".join(_preview_texts)[:2000]
    _pdf_category, _pdf_category_confidence = classify_document(
        pdf, _content_preview_for_classify
    )
    _pdf_product_module = infer_product_module_from_path(pdf)
    logger.info(
        "[mineru] 文档分类: %s -> %s (confidence=%.2f, module=%s)",
        pdf.name, _pdf_category, _pdf_category_confidence, _pdf_product_module,
    )

    knowledge_blocks: List[Dict[str, object]] = []
    if isinstance(blocks, list):
        section_context_map = _build_section_context_map(blocks)
        # 1. 预处理：收集所有有效的 Block 和基础元数据
        valid_items = []
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            
            # Apply page limit for testing (if max_pages is set)
            if max_pages is not None:
                page_idx = block.get("page_idx")
                if page_idx is not None and page_idx >= max_pages:
                    continue
            
            text = _extract_text_from_block(block)
            if not text.strip():
                continue

            section_context = section_context_map.get(idx, {})
            # Preserve image path reference if the block has one
            img_path = block.get("img_path", "")
            base_meta = {
                "source_pdf": str(pdf),
                "source_file": json_path.name,
                "page_idx": block.get("page_idx"),
                "block_type": block.get("type"),
                "block_id": idx,
                "mineru_task_id": task_id,
                "section_title": section_context.get("section_title", ""),
                "parent_section": section_context.get("parent_section", ""),
                "section_path": section_context.get("section_path", ""),
                "document_category": _pdf_category,
                "product_module": _pdf_product_module or "unknown",
            }
            if img_path:
                base_meta["img_path"] = img_path
            valid_items.append((text, base_meta))

        # 2. 并行处理：使用线程池并发调用 _extract_chunk_metadata
        # 如果启用双提供商负载均衡，增加线程数以充分利用两个提供商
        # 默认线程数：单提供商16，双提供商32
        default_workers = 32 if ENABLE_DUAL_PROVIDER_LOAD_BALANCE else 16
        max_workers = cfg_int(
            "auto_convert.parallel.max_workers",
            default_workers,
            env="AUTO_CONVERT_MAX_WORKERS",
        )
        if valid_items:
            total = len(valid_items)
            processed = 0
            start_time = time.perf_counter()
            
            # Helper to run blocking processing in a thread with progress tracking
            # Phase 1: rule-based extraction (fast, no LLM)
            # Phase 2: batch LLM extraction (N chunks per API call)
            def _process_batch_sync(items):
                nonlocal processed
                config = _load_project_config()
                meta_rules = config.get("metadata_rules", {})
                llm_config = config.get("llm-aided-config", {}).get("metadata_extraction", {})
                llm_enabled = llm_config.get("enable", False)

                # Phase 1: rule-based extraction for all items (fast)
                rule_results = []
                for text, base in items:
                    clean_text = _clean_chunk_text(text)
                    meta_out: Dict[str, object] = {"clean_text": clean_text}
                    section_title = str(base.get("section_title") or "").strip()
                    parent_section = str(base.get("parent_section") or "").strip()
                    if section_title:
                        meta_out["section_title"] = section_title
                    if parent_section:
                        meta_out["parent_section"] = parent_section
                    section_path = base.get("section_path")
                    if section_path:
                        meta_out["section_path"] = section_path
                    lower_text = clean_text.lower()
                    lower_section = f"{section_title} {parent_section}".lower()
                    for intent, keywords in meta_rules.get("intents", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["intent"] = intent
                            break
                    product_modules_map = _merge_product_modules_map(meta_rules.get("product_modules", {}))
                    for module, keywords in product_modules_map.items():
                        if any(k.lower() in lower_text for k in keywords) or any(k.lower() in lower_section for k in keywords):
                            meta_out["product_module"] = module
                            break
                    for proto in _match_protocols_word_boundary(
                        meta_rules.get("protocol_types", {}), lower_text
                    ):
                        meta_out.setdefault("protocol_type", [])
                        meta_out["protocol_type"].append(proto)
                    for prefix, keywords in meta_rules.get("command_prefixes", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["command_prefix"] = prefix
                            break
                    for mode, keywords in meta_rules.get("config_modes", {}).items():
                        if any(k.lower() in lower_text for k in keywords):
                            meta_out["config_mode"] = mode
                            break
                    rule_results.append((clean_text, meta_out))

                processed += len(items)
                elapsed = time.perf_counter() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = total - processed
                eta = remaining / rate if rate > 0 else 0
                logger.info(
                    "[parallel] 规则提取完成: %d/%d (%.1f%%) | %.1f 块/秒 | 剩余: %.1f秒",
                    processed, total, 100.0 * processed / total, rate, eta,
                )

                # Phase 2: batch LLM extraction (if enabled)
                # Skip blocks where rule-based extraction already filled key fields
                if llm_enabled:
                    _KEY_FIELDS = {"product_module", "protocol_type", "intent", "config_mode"}
                    needs_llm = []
                    for ct, m in rule_results:
                        filled = sum(1 for f in _KEY_FIELDS if m.get(f))
                        if filled < 3:  # need LLM if <3 of 4 key fields filled
                            needs_llm.append((ct, m))
                    skipped = len(rule_results) - len(needs_llm)
                    if skipped:
                        logger.info(
                            "[batch-llm] 跳过 %d/%d 块 (规则已覆盖), LLM处理 %d 块",
                            skipped, len(rule_results), len(needs_llm),
                        )
                    batch_size = _BATCH_LLM_SIZE
                    llm_batches = []
                    for i in range(0, len(needs_llm), batch_size):
                        llm_batches.append(needs_llm[i:i + batch_size])

                    llm_done = 0
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, 32)) as executor:
                        def _do_batch(batch):
                            _apply_llm_metadata_extraction_batch(
                                [(ct, m) for ct, m in batch], llm_config
                            )
                        futures = {executor.submit(_do_batch, b): b for b in llm_batches}
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                future.result()
                            except Exception as e:
                                logger.warning("[batch-llm] batch failed: %s", e)
                            llm_done += len(futures[future])
                            llm_total = len(needs_llm)
                            if llm_done % max(1, min(500, llm_total // 5)) < batch_size or llm_done >= llm_total:
                                logger.info(
                                    "[batch-llm] LLM进度: %d/%d (%.1f%%)",
                                    llm_done, llm_total,
                                    100.0 * llm_done / llm_total if llm_total else 100.0,
                                )

                return [meta_out for _, meta_out in rule_results]

            provider_info = ""
            if ENABLE_DUAL_PROVIDER_LOAD_BALANCE:
                provider_info = " (双提供商负载均衡: 百度千帆 + 硅基流动)"
            logger.info(
                "[parallel] 开始提取 metadata: %d 个块 (线程数=%d%s)...",
                total,
                max_workers,
                provider_info,
            )
            
            # Run the batch processing in a separate thread to unblock the event loop
            results = await asyncio.to_thread(_process_batch_sync, valid_items)
            
            elapsed = time.perf_counter() - start_time
            logger.info(
                "[parallel] metadata 提取完成: %d 个块，耗时 %.2f 秒 (平均 %.1f 块/秒)",
                total, elapsed, total / elapsed if elapsed > 0 else 0
            )
            
            # 3. 合并结果
            for (text, base_meta), extracted_meta in zip(valid_items, results):
                base_meta.update(extracted_meta)
                knowledge_blocks.append(
                    {
                        "page_content": text,
                        "metadata": base_meta,
                    }
                )
    
    # 4. 增强 metadata：基于功能结构索引添加 scenario_id 和 step_type
    logger.info("[metadata] 开始增强 metadata...")
    _enhance_metadata_with_function_index(knowledge_blocks)
    logger.info("[metadata] metadata 增强完成，共处理 %d 个块", len(knowledge_blocks))
    
    # 5. 自动识别文档模块和功能（新增）
    logger.info("[auto-identify] 开始自动识别文档模块和功能...")
    try:
        from INAGENT.data_tools.auto_document_integration import auto_identify_document_module, load_function_structure_index
        
        existing_index = load_function_structure_index()
        document_metadata = auto_identify_document_module(
            pdf_path=pdf,
            content_blocks=knowledge_blocks,
            existing_index=existing_index
        )
        
        logger.info(
            "[auto-identify] 文档识别完成: 模块=%s, 协议=%s, 类型=%s, 置信度=%.2f",
            document_metadata.get("product_modules", []),
            document_metadata.get("protocol_types", []),
            document_metadata.get("document_type", "unknown"),
            document_metadata.get("confidence", 0.0)
        )
        
        if document_metadata.get("new_modules_discovered"):
            logger.info(
                "[auto-identify] 发现新模块: %s",
                document_metadata["new_modules_discovered"]
            )
    except Exception as e:
        logger.warning("[auto-identify] 自动识别失败: %s", e)
        document_metadata = {}
    
    json_path.write_text(
        json.dumps(knowledge_blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cache_payload: Dict[str, object] = {
        "source_pdf": str(pdf),
        "source_pdf_fingerprint": _compute_pdf_fingerprint(pdf),
        "config_fingerprint": _compute_config_fingerprint(),
        "mineru_task_id": task_id,
        "mineru_output_dir": str(MINERU_OUTPUT_DIR),
        "frontmatter_pages": frontmatter_pages,
        "record_count": len(knowledge_blocks),
        "output_json": str(json_path),
        "document_category": _pdf_category,
        "document_category_confidence": _pdf_category_confidence,
        "product_module": _pdf_product_module,
        "document_metadata": document_metadata,  # 保存文档识别结果
    }
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("[ok] saved knowledge blocks -> %s", json_path)
    logger.info("[ok] saved cache -> %s", cache_path)

    # Cleanup large MinerU intermediate files to save disk space
    task_dir = MINERU_OUTPUT_DIR / task_id
    if task_dir.exists():
        _cleanup_mineru_intermediate(task_dir)



def _setup_mineru_config() -> None:
    """Check and deploy optimized MinerU configuration from current project."""
    try:
        # PSScriptRoot equivalents logic
        project_root = BASE_DIR.parent
        src_config = project_root / "mineru.json"
        
        # Ensure output directory exists before writing config
        MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        
        # Target: mineru.json in output directory (will be CWD for subprocess)
        dest_config = MINERU_OUTPUT_DIR / "mineru.json"
        
        if src_config.exists():
            # Load and resolve ENV placeholders before writing to destination
            try:
                content = src_config.read_text(encoding="utf-8")
                config_data = json.loads(content)
                
                def recursive_resolve(data):
                    if isinstance(data, dict):
                        return {k: recursive_resolve(v) for k, v in data.items()}
                    elif isinstance(data, list):
                        return [recursive_resolve(v) for v in data]
                    elif isinstance(data, str):
                        return resolve_env_placeholder(data)
                    return data

                resolved_config = recursive_resolve(config_data)
                
                logger.info("[config] writing resolved config to %s", dest_config)
                # print(json.dumps(resolved_config, indent=4))
                dest_config.write_text(
                    json.dumps(resolved_config, ensure_ascii=False, indent=4),
                    encoding="utf-8"
                )
            except Exception as e:
                logger.error("[config] Failed to process config file: %s", e)
                # Fallback to copy if parsing fails
                shutil.copy(src_config, dest_config)
        else:
            logger.warning(
                "[config] Warning: Local config %s not found. "
                "Using system defaults.",
                src_config,
            )
            
    except Exception as e:
        logger.exception("[config] Failed to setup MinerU config: %s", e)


def _cleanup_old_logs(max_age_days: int = 30) -> None:
    """Remove log files older than max_age_days to prevent disk bloat."""
    if not LOG_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for log_file in LOG_DIR.glob("*.log"):
        # Keep the main auto_convert.log
        if log_file.name == "auto_convert.log":
            continue
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("[cleanup] removed %d log files older than %d days", removed, max_age_days)


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Make logging setup idempotent (avoid duplicated logs)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Ensure CAMEL library logs are written to the same file
    set_log_level("INFO")
    set_log_file(str(LOG_FILE))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    existing_files = {
        getattr(h, "baseFilename", None)
        for h in root_logger.handlers
        if isinstance(h, logging.FileHandler)
    }
    if str(LOG_FILE) not in existing_files:
        root_logger.addHandler(file_handler)

    # Avoid multiple console handlers.
    has_stream = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root_logger.handlers
    )
    if not has_stream:
        root_logger.addHandler(console_handler)


def _cleanup_mineru_intermediate(task_dir: Path) -> None:
    """Backup large MinerU intermediate files to mineru_backup/ after conversion.

    Keeps in-place: content_list*.json, images/, *.md (actively used by downstream)
    Moves to backup: *_layout.pdf, *_origin.pdf, *_middle.json, *_model.json
    These files consume ~820 MB per conversion and MinerU regeneration is expensive,
    so we back them up instead of deleting.
    """
    backup_patterns = ["*_layout.pdf", "*_origin.pdf", "*_middle.json", "*_model.json"]
    moved_bytes = 0
    # Mirror the relative structure: mineru_backup/{task_id}/hybrid_auto/...
    rel = task_dir.relative_to(MINERU_OUTPUT_DIR)
    backup_task_dir = MINERU_BACKUP_DIR / rel
    for pattern in backup_patterns:
        for f in task_dir.rglob(pattern):
            try:
                f_rel = f.relative_to(task_dir)
                dest = backup_task_dir / f_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                size = f.stat().st_size
                shutil.move(str(f), str(dest))
                moved_bytes += size
                logger.debug("[backup] moved intermediate: %s -> %s (%.1f MB)", f.name, dest, size / 1048576)
            except Exception as e:
                logger.warning("[backup] failed to move %s: %s", f.name, e)
    if moved_bytes > 0:
        logger.info(
            "[backup] moved %.1f MB of intermediate files from %s to backup",
            moved_bytes / 1048576,
            task_dir.name,
        )


def _cleanup_orphan_files(pdfs: List[Path], office_files: Optional[List[Path]] = None) -> None:
    """Delete JSON/cache files in reference/logs that have no corresponding source file.

    Also cleans up orphan mineru_output directories whose source PDF no longer exists.
    Considers both PDF and Office source files when determining orphans.
    """
    valid_stems = {p.stem for p in pdfs}
    if office_files:
        valid_stems |= {p.stem for p in office_files}
    # Keep merged/derived knowledge base files even though they do not
    # correspond to a single PDF stem.
    reserved_reference_files = {"knowledge_base.json"}
    
    # 1. Cleanup reference/*.json
    if REFERENCE_DIR.exists():
        for json_file in REFERENCE_DIR.glob("*.json"):
            if json_file.name in reserved_reference_files:
                continue
            if json_file.stem not in valid_stems:
                try:
                    logger.info("[cleanup] removing orphan file: %s", json_file.name)
                    json_file.unlink()
                except Exception as e:
                    logger.warning("[cleanup] failed to remove %s: %s", json_file.name, e)

    # 2. Cleanup logs/*.cache.json
    if LOG_DIR.exists():
        for cache_file in LOG_DIR.glob("*.cache.json"):
            # cache file is stem.cache.json
            stem = cache_file.name.replace(".cache.json", "")
            if stem not in valid_stems:
                 try:
                    logger.info("[cleanup] removing orphan cache: %s", cache_file.name)
                    cache_file.unlink()
                 except Exception as e:
                    logger.warning("[cleanup] failed to remove %s: %s", cache_file.name, e)

    # 3. Backup orphan mineru_output directories (source PDF removed)
    if MINERU_OUTPUT_DIR.exists():
        for task_dir in MINERU_OUTPUT_DIR.iterdir():
            if not task_dir.is_dir():
                continue
            if task_dir.name not in valid_stems:
                try:
                    backup_dest = MINERU_BACKUP_DIR / task_dir.name
                    if backup_dest.exists():
                        shutil.rmtree(backup_dest)
                    backup_dest.parent.mkdir(parents=True, exist_ok=True)
                    logger.info("[backup] moving orphan mineru_output to backup: %s/", task_dir.name)
                    shutil.move(str(task_dir), str(backup_dest))
                except Exception as e:
                    logger.warning("[backup] failed to move %s: %s", task_dir.name, e)


async def main() -> None:
    load_inagent_env()
    _setup_logging()
    
    # 调试：输出关键环境变量
    logger.info(
        "[config] HUNYUAN_API_BASE_URL=%s",
        cfg_str("llm.hunyuan.base_url", "NOT SET", env="HUNYUAN_API_BASE_URL"),
    )
    logger.info(
        "[config] HUNYUAN_MODEL=%s",
        cfg_str("llm.hunyuan.model", "NOT SET", env="HUNYUAN_MODEL"),
    )
    logger.info(
        "[config] LLM_GATEWAY_BASE_URL=%s",
        cfg_str("llm.gateway.base_url", "NOT SET", env="LLM_GATEWAY_BASE_URL"),
    )
    
    # 清除 LLM 配置缓存，确保使用最新的环境变量
    try:
        import sys
        if 'INAGENT.utils.llm_config' in sys.modules:
            # 清除模块级单例的缓存
            llm_config_module = sys.modules['INAGENT.utils.llm_config']
            if hasattr(llm_config_module, '_llm_config_instance') and llm_config_module._llm_config_instance:
                llm_config_module._llm_config_instance.clear_cache()
                logger.info("[config] LLM 配置缓存已清除")
            # 强制重新加载环境变量到模块
            llm_config_module.load_inagent_env()
            logger.info("[config] 已重新加载环境变量")
    except Exception as e:
        logger.warning("[config] 清除 LLM 配置缓存失败: %s", e)
    
    os.environ.setdefault("MINERU_MODEL_SOURCE", "modelscope")
    logger.info(
        "[config] MINERU_MODEL_SOURCE=%s",
        os.environ.get("MINERU_MODEL_SOURCE"),
    )
    # 0) 自动同步配置
    _setup_mineru_config()

    global USE_DOCKER_VLLM
    if USE_DOCKER_VLLM:
        global DOCKER_VLLM_URL
        candidate_urls = [DOCKER_VLLM_URL]
        if DOCKER_VLLM_URL.endswith(":8000"):
            candidate_urls.append("http://127.0.0.1:30000")

        logger.info(
            "[vllm] 正在探测 VLLM 服务状态 (/v1/models 超时: %ss, 推理探针超时: %ss)...",
            VLLM_MODELS_TIMEOUT,
            VLLM_INFER_TIMEOUT,
        )
        ok = False
        error = None
        for candidate in candidate_urls:
            logger.info("[vllm] 尝试连接: %s", candidate)
            models_url = _build_models_url(candidate)
            ok, error = _check_url_once(models_url, timeout=VLLM_MODELS_TIMEOUT)
            if not ok:
                continue
            model_name = _fetch_vllm_model_name(candidate, timeout=VLLM_MODELS_TIMEOUT)
            if not model_name:
                error = "model list empty"
                ok = False
                continue

            # Some MinerU vLLM deployments host VLM models and may be slow on
            # cold start or not accept trivial text-only probes. Treat the
            # inference probe as best-effort; /v1/models is the source of truth
            # for connectivity.
            if not _probe_vllm_completion(
                candidate, model_name, timeout=VLLM_INFER_TIMEOUT
            ):
                logger.warning(
                    "[vllm] 推理探针未通过(可能是冷启动/多模态模型限制)，但 /v1/models 可达；继续使用该地址。"
                )
            DOCKER_VLLM_URL = candidate
            logger.info("[vllm] 使用可用地址: %s", DOCKER_VLLM_URL)
            ok = True
            break
        if not ok:
            logger.warning(
                "[vllm] 服务不可用: %s。",
                error or "unknown error",
            )
            if not _prompt_continue_without_vllm():
                logger.info("用户取消操作。")
                sys.exit(0)
            USE_DOCKER_VLLM = False

    # LLM Gateway preflight: probe real /chat/completions
    global _QIANFAN_AVAILABLE
    if _QIANFAN_AVAILABLE is None:
        runtime = _get_siliconflow_runtime()
        base_url = str(runtime.get("base_url") or "")
        model = str(runtime.get("model") or "")

        # 如果可用 vLLM，优先使用其真实模型名
        if USE_DOCKER_VLLM:
            detected_model = _fetch_vllm_model_name(
                DOCKER_VLLM_URL, timeout=VLLM_MODELS_TIMEOUT
            )
            if detected_model and not cfg_str("llm.gateway.chat_model", "", env="LLM_GATEWAY_CHAT_MODEL").strip():
                os.environ["LLM_GATEWAY_CHAT_MODEL"] = detected_model
                model = detected_model
                logger.info("[gateway] 检测到 vLLM 模型: %s", detected_model)

        ok, detail = _probe_siliconflow_chat(
            base_url=base_url,
            api_key=str(runtime.get("api_key") or ""),
            model=model,
            timeout=float(runtime.get("timeout") or NET_TIMEOUT),
        )
        _QIANFAN_AVAILABLE = ok
        if not ok:
            logger.warning(
                "[gateway] 预检失败 (base_url=%s, model=%s): %s",
                base_url,
                model,
                detail,
            )
            if not _prompt_continue_on_error("gateway", detail):
                logger.info("用户取消操作。")
                sys.exit(0)

    if not DOC_LOCAL_DIR.exists():
        logger.error("knowledge_base not found: %s", DOC_LOCAL_DIR)
        raise SystemExit(1)

    pdfs = _iter_pdf_files()
    office_files_early = _iter_office_files()
    text_files_early = _iter_text_files()
    _cleanup_orphan_files(pdfs, office_files_early + text_files_early)
    _cleanup_old_logs()

    if not pdfs and not office_files_early and not text_files_early:
        logger.info("no pdf/office/text files found under knowledge_base, nothing to do.")
        return

    MINERU_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # PDF 处理（MinerU）
    # ============================================================
    if pdfs:
        # ============================================================
        # MinerU 配置说明：
        # ============================================================
        # 1. Backend: hybrid-auto-engine（默认）
        #    - 本地处理，无需远程 HTTP 服务器
        #    - 支持 vLLM 加速（可选）
        #    - 详见: https://opendatalab.github.io/MinerU/zh/quick_start/extension_modules/#vllm-vlm
        #
        # 2. vLLM 加速（推荐用于批量处理）:
        #    - 启动: mineru-vllm-server（或运行 run_vllm_server.ps1）
        #    - 地址: http://127.0.0.1:30000
        #    - 自动从 mineru.json 读取 models-dir.vlm 配置
        #    - GPU 内存利用率自动优化
        #
        # 3. MinerU 命令查找优先级：
        #    a. MINERU_CLI 环境变量（如果设置）
        #    b. INFOAGEN/mineru/venv/Scripts/mineru.exe
        #    c. PATH 中的 mineru 命令
        # ============================================================

        mineru_cmd = cfg_str("auto_convert.mineru.cli", "", env="MINERU_CLI")
        if not mineru_cmd:
            # 兼容两种目录布局：
            # 1) <repo>/INAGENT/mineru/venv/Scripts/mineru.exe
            # 2) <repo>/mineru/venv/Scripts/mineru.exe
            candidates = [
                BASE_DIR.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
                BASE_DIR.parent.parent / "mineru" / "venv" / "Scripts" / "mineru.exe",
            ]
            picked = next((c for c in candidates if c.exists()), None)
            mineru_cmd = str(picked) if picked else "mineru"
        reader = LocalMinerUReader(
            output_dir=str(MINERU_OUTPUT_DIR),
            mineru_command=mineru_cmd,
        )

        logger.info("Found %d pdf(s) under %s", len(pdfs), DOC_LOCAL_DIR)
        logger.info("Using MinerU command: %s", mineru_cmd)
        logger.info(
            "Backend: hybrid-auto-engine (default, supports vLLM acceleration)"
        )
        overall_start = time.perf_counter()

        # Semaphore for file-level concurrency
        # Default to 3 concurrent files (assuming average PDF size, this balances memory/CPU)
        max_concurrent_files = cfg_int(
            "auto_convert.parallel.max_files",
            3,
            env="AUTO_CONVERT_MAX_FILES",
        )
        sem = asyncio.Semaphore(max_concurrent_files)

        # Support page limit for testing (via environment variable)
        max_pages_test = cfg_str(
            "auto_convert.parallel.max_pages_test",
            "",
            env="AUTO_CONVERT_MAX_PAGES_TEST",
        )
        max_pages_limit = None
        if max_pages_test:
            try:
                max_pages_limit = int(max_pages_test)
                logger.info("TEST MODE: Processing only first %d pages of each PDF", max_pages_limit)
            except ValueError:
                logger.warning("Invalid AUTO_CONVERT_MAX_PAGES_TEST value: %s", max_pages_test)

        async def _protected_convert(p):
            async with sem:
                 try:
                     await convert_one(reader, p, max_pages=max_pages_limit)
                 except Exception as e:
                     logger.error("Failed to convert %s: %s", p.name, e)
                     if not _fallback_convert_pdf_with_markitdown(p, reason=str(e)):
                         logger.error("[pdf-fallback] 仍失败: %s", p.name)

        tasks = [_protected_convert(p) for p in pdfs]
        logger.info("Starting conversion with file_concurrency=%d", max_concurrent_files)
        
        if tasks:
            await asyncio.gather(*tasks)
    else:
        logger.info("no pdf files found, skipping MinerU phase.")

    # ============================================================
    # Office 文档处理（docx/xlsx/doc/xls）
    # ============================================================
    office_files = office_files_early
    if office_files:
        logger.info("=" * 80)
        logger.info("[office] 发现 %d 个 Office 文档，开始处理...", len(office_files))
        for ofile in office_files:
            try:
                convert_office_file(ofile)
            except Exception as e:
                logger.error("[office] 处理失败 %s: %s", ofile.name, e)
        logger.info("[office] Office 文档处理完成")
    else:
        logger.info("[office] 未发现 Office 文档，跳过")

    # ============================================================
    # TXT 文档处理
    # ============================================================
    text_files = _iter_text_files()
    if text_files:
        logger.info("=" * 80)
        logger.info("[text] 发现 %d 个 TXT 文档，开始处理...", len(text_files))
        for tfile in text_files:
            try:
                convert_text_file(tfile)
            except Exception as e:
                logger.error("[text] 处理失败 %s: %s", tfile.name, e)
        logger.info("[text] TXT 文档处理完成")
    else:
        logger.info("[text] 未发现 TXT 文档，跳过")

    # Ensure enhanced metadata is applied even when MinerU outputs are reused via cache.
    # When convert_one() is skipped, app.json/cli.json may predate newly added metadata
    # fields (e.g., scenario_id/step_type). We apply the enhancer as a post-pass.
    try:
        for json_file in REFERENCE_DIR.glob("*.json"):
            if json_file.name == "knowledge_base.json":
                continue
            try:
                blocks = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(blocks, list) and blocks:
                    # Post-pass: ensure rule-based product_module/protocol_type/command_prefix are present
                    # even when cached outputs were produced before these fields existed.
                    config = _load_project_config()
                    meta_rules = config.get("metadata_rules", {})
                    product_modules_map = _merge_product_modules_map(
                        meta_rules.get("product_modules", {})
                    )
                    protocol_map = meta_rules.get("protocol_types", {})
                    command_prefixes = meta_rules.get("command_prefixes", {})

                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        meta = block.get("metadata") or {}
                        content = str(block.get("page_content") or "")
                        lower_text = content.lower()

                        # product_module (overwrite when rule-based match exists)
                        for module, keywords in product_modules_map.items():
                            if any(k.lower() in lower_text for k in keywords):
                                meta["product_module"] = module
                                break

                        # protocol_type (list)
                        if not meta.get("protocol_type"):
                            found_protocols = _match_protocols_word_boundary(
                                protocol_map, content
                            )
                            if found_protocols:
                                meta["protocol_type"] = found_protocols

                        # command_prefix
                        if not meta.get("command_prefix"):
                            for prefix, keywords in command_prefixes.items():
                                if any(k.lower() in lower_text for k in keywords):
                                    meta["command_prefix"] = prefix
                                    break

                    _enhance_metadata_with_function_index(blocks)
                    json_file.write_text(
                        json.dumps(blocks, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            except Exception as exc:
                logger.warning("Failed to post-enhance %s: %s", json_file.name, exc)
    except Exception as exc:
        logger.warning("Post-enhance pass failed: %s", exc)

    # Merge reference JSONs into a stable knowledge_base.json for retrieval/indexing.
    # This file is expected by HybridRAG and GraphRAG pipelines.
    logger.info("=" * 80)
    logger.info("[merge] 开始合并 knowledge_base.json...")
    try:
        from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base

        reference_dir = REFERENCE_DIR
        output_file = reference_dir / "knowledge_base.json"
        merge_knowledge_base(reference_dir, output_file, deduplicate=True, logger=logger)
        logger.info("[ok] merged knowledge base -> %s", output_file)
    except Exception as exc:
        logger.warning("Failed to merge knowledge_base.json: %s", exc)
        output_file = None

    # 增量更新功能结构索引（新增）
    logger.info("=" * 80)
    logger.info("[index-update] 开始增量更新功能结构索引...")
    try:
        from INAGENT.data_tools.auto_document_integration import incrementally_update_function_index, load_function_structure_index
        
        # 收集所有新文档信息
        new_documents = []
        for pdf in pdfs:
            cache_path = LOG_DIR / f"{pdf.stem}.cache.json"
            if cache_path.exists():
                try:
                    cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
                    if cache_data.get("document_metadata"):
                        new_documents.append({
                            "pdf_path": pdf,
                            "json_path": REFERENCE_DIR / f"{pdf.stem}.json",
                            "document_metadata": cache_data["document_metadata"]
                        })
                        logger.info(
                            "[index-update] 收集到新文档: %s (模块=%s)",
                            pdf.name,
                            cache_data["document_metadata"].get("product_modules", [])
                        )
                except Exception as e:
                    logger.warning("[index-update] 读取 cache 失败 %s: %s", pdf.name, e)
        
        if new_documents:
            logger.info("[index-update] 共收集到 %d 个新文档，开始更新索引...", len(new_documents))
            index_path = DOC_LOCAL_DIR / "function_structure_index.json"
            updated_index = incrementally_update_function_index(
                new_documents=new_documents,
                existing_index_path=index_path,
                logger=logger
            )
            
            # 保存更新后的索引
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_path.write_text(
                json.dumps(updated_index, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            
            # 统计更新结果
            modules_count = len(updated_index.get("modules", {}))
            scenarios_count = len(updated_index.get("scenarios", {}))
            stats = updated_index.get("metadata_statistics", {})
            product_modules_count = len(stats.get("product_modules", {}))
            protocol_types_count = len(stats.get("protocol_types", {}))
            step_types_count = len(stats.get("step_types", {}))
            
            logger.info("[index-update] 功能结构索引已更新:")
            logger.info("  - 模块数量: %d", modules_count)
            logger.info("  - 场景数量: %d", scenarios_count)
            logger.info("  - 产品模块: %d", product_modules_count)
            logger.info("  - 协议类型: %d", protocol_types_count)
            logger.info("  - 步骤类型: %d", step_types_count)
            logger.info("  - 索引文件: %s", index_path)
        else:
            logger.info("[index-update] 没有新文档，跳过索引更新")
    except Exception as exc:
        logger.warning("[index-update] 增量更新功能结构索引失败: %s", exc)
        import traceback
        logger.debug(traceback.format_exc())

    # 自动重新索引到 RAG（新增）
    logger.info("=" * 80)
    logger.info("[rag-reindex] 检查是否需要重新索引到 RAG...")
    try:
        # 检查 knowledge_base.json 是否存在
        kb_path = REFERENCE_DIR / "knowledge_base.json"
        if not kb_path.exists():
            logger.warning("[rag-reindex] knowledge_base.json 不存在，跳过 RAG 索引")
        else:
            # 检查是否需要重新索引（基于文件 mtime）
            rag_hashes_path = LOG_DIR / "rag_hashes.json"
            needs_reindex = True
            
            if rag_hashes_path.exists():
                try:
                    rag_hashes = json.loads(rag_hashes_path.read_text(encoding="utf-8"))
                    kb_mtime = kb_path.stat().st_mtime
                    cached_mtime = rag_hashes.get("knowledge_base_mtime", 0)
                    
                    if kb_mtime <= cached_mtime:
                        logger.info("[rag-reindex] knowledge_base.json 未变更，跳过重新索引")
                        needs_reindex = False
                    else:
                        logger.info(
                            "[rag-reindex] knowledge_base.json 已更新 (mtime: %.2f -> %.2f)，需要重新索引",
                            cached_mtime,
                            kb_mtime
                        )
                except Exception as e:
                    logger.warning("[rag-reindex] 读取 rag_hashes.json 失败: %s", e)
            
            if needs_reindex:
                logger.info("[rag-reindex] 开始重新索引到 RAG...")
                # 延迟导入，避免循环依赖
                import sys
                import importlib
                
                # 尝试导入 workforce_config_ops
                try:
                    # 这里需要根据实际情况调整导入路径
                    logger.info("[rag-reindex] 注意: RAG 重新索引需要手动调用，或通过单独的脚本执行")
                    logger.info("[rag-reindex] 建议运行: python -m INAGENT.rag.fallback_retrieval --reindex")
                    # 如果需要自动执行，可以在这里调用相关函数
                    # 但为了避免循环依赖和性能问题，建议通过单独的命令执行
                except Exception as e:
                    logger.warning("[rag-reindex] RAG 重新索引失败: %s", e)
                    logger.info("[rag-reindex] 请手动运行 RAG 索引脚本")
    except Exception as exc:
        logger.warning("[rag-reindex] 检查 RAG 索引状态失败: %s", exc)
        import traceback
        logger.debug(traceback.format_exc())

    logger.info("=" * 80)
    logger.info(
        "[完成] 所有处理完成！总耗时: %.2fs", time.perf_counter() - overall_start
    )
    logger.info("=" * 80)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        try:
            _setup_logging()
        except Exception:
            pass
        logger.warning("auto_convert interrupted by user (KeyboardInterrupt)")
    except Exception as exc:
        # Best-effort logging: make sure exceptions are recorded in LOG_FILE.
        try:
            _setup_logging()
        except Exception:
            pass
        logger.exception("auto_convert failed: %s", exc)
        raise