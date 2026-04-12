# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
统一的 LLM 配置管理模块

功能：
1. 统一管理所有 LLM API 配置（统一通过 LLM 网关调用）
2. 从环境变量加载配置，不硬编码
3. 提供统一的配置接口
4. 支持配置验证和错误处理

限速标准（按网关默认策略）：

- 对话模型：RPM 1000, TPM 50000
  * deepseek-ai/DeepSeek-R1-0528-Qwen3-8B: RPM 1000, TPM 50000
  * Qwen/Qwen3-8B: RPM 1000, TPM 50000
  * THUDM/GLM-Z1-9B-0414: RPM 1000, TPM 50000
  * deepseek-ai/DeepSeek-R1-Distill-Qwen-7B: RPM 1000, TPM 50000
- 嵌入模型：RPM 2000, TPM 500000
  * BAAI/bge-m3: RPM 2000, TPM 500000
  * netease-youdao/bce-embedding-base_v1: RPM 2000, TPM 500000
  * BAAI/bge-large-zh-v1.5: RPM 2000, TPM 500000
  * BAAI/bge-large-en-v1.5: RPM 2000, TPM 500000
- 重排序模型：RPM 2000, TPM 500000
  * BAAI/bge-reranker-v2-m3: RPM 2000, TPM 500000
  * netease-youdao/bce-reranker-base_v1: RPM 2000, TPM 500000

注意：已禁用直接调用第三方提供商，所有请求必须通过 LLM 网关。
"""
import os
import logging
from typing import Dict, Optional, Any, List
from pathlib import Path

from INAGENT.config.project_config import cfg_float, cfg_int, cfg_str
from INAGENT.utils.env_utils import load_inagent_env, resolve_env_placeholder

logger = logging.getLogger(__name__)

# 确保环境变量已加载
load_inagent_env()


class LLMConfig:
    """LLM 配置类：统一管理所有 LLM 相关配置"""
    
    def __init__(self):
        self._config_cache: Dict[str, Any] = {}
    
    def get_qianfan_config(self) -> Dict[str, Any]:
        """
        获取主对话模型提供商配置（已统一切换为 LLM 网关）
        
        说明：
        - 历史上这里用于第三方直连，现在统一改为网关；
        - 此方法为向后兼容保留，实际返回网关配置。
        
        限速标准（按网关默认策略）：
        - 对话模型：RPM 1000, TPM 50000
        
        Returns:
            {
                "api_key": str,
                "base_url": str,
                "model": str,
                "timeout": float,
                "rpm": int,
                "tpm": int,
                "temperature": float
            }
        """
        # 向后兼容：直接返回网关配置
        gateway_cfg = self.get_gateway_config()
        
        # 转换为与旧接口兼容的格式
        config = {
            "api_key": gateway_cfg.get("api_key", ""),
            "base_url": gateway_cfg.get("base_url", ""),
            "model": gateway_cfg.get("chat_model", ""),
            "timeout": gateway_cfg.get("timeout", 60),
            # 网关对话模型限速：RPM 1000, TPM 50000
            "rpm": gateway_cfg.get("chat_rpm", 1000),
            "tpm": gateway_cfg.get("chat_tpm", 50000),
            "temperature": 0.7,  # 网关推荐温度
        }
        
        return config
    
    def get_openai_config(self) -> Dict[str, Any]:
        """
        获取 OpenAI 兼容配置（已统一切换为 LLM 网关）
        
        说明：
        - 历史上这里用于直连 OpenAI，现在统一改为网关；
        - 此方法为向后兼容保留，实际返回网关配置。
        
        Returns:
            {
                "api_key": str,
                "base_url": str,
                "model": str,
                "timeout": float
            }
        """
        # 向后兼容：直接返回网关配置
        gateway_cfg = self.get_gateway_config()
        
        config = {
            "api_key": gateway_cfg.get("api_key", ""),
            "base_url": gateway_cfg.get("base_url", ""),
            "model": gateway_cfg.get("chat_model", ""),
            "timeout": gateway_cfg.get("timeout", 60),
        }
        
        return config
    
    def get_gateway_config(self) -> Dict[str, Any]:
        """
        获取 LLM 网关配置
        
        限速标准（按网关默认策略）：
        - 对话模型：RPM 1000, TPM 50000
        - 嵌入模型：RPM 2000, TPM 500000
        - 重排序模型：RPM 2000, TPM 500000
        
        支持的模型列表：
        - 对话模型：
          * deepseek-ai/DeepSeek-R1-0528-Qwen3-8B
          * Qwen/Qwen3-8B
          * THUDM/GLM-Z1-9B-0414
          * deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
        - 嵌入模型：
          * BAAI/bge-m3
          * netease-youdao/bce-embedding-base_v1
          * BAAI/bge-large-zh-v1.5
          * BAAI/bge-large-en-v1.5
        - 重排序模型：
          * BAAI/bge-reranker-v2-m3
          * netease-youdao/bce-reranker-base_v1
        
        Returns:
            {
                "api_key": str,
                "base_url": str,
                "timeout": float,
                "chat_model": str,  # 对话模型名称
                "chat_rpm": int,  # 对话模型限速
                "chat_tpm": int,  # 对话模型限速
                "embedding_model": str,  # 嵌入模型名称
                "embedding_rpm": int,  # 嵌入模型限速
                "embedding_tpm": int,  # 嵌入模型限速
                "reranker_model": str,  # 重排序模型名称
                "reranker_rpm": int,  # 重排序模型限速
                "reranker_tpm": int,  # 重排序模型限速
            }
        """
        if "gateway" in self._config_cache:
            return self._config_cache["gateway"]
        
        api_key = cfg_str("llm.siliconflow.api_key", "", env="SILICONFLOW_API_KEY")
        gateway_base_url = self._normalize_base_url(
            cfg_str("llm.gateway.base_url", "", env="LLM_GATEWAY_BASE_URL")
        )
        gateway_api_key = cfg_str("llm.gateway.api_key", "", env="LLM_GATEWAY_API_KEY")

        if not gateway_base_url:
            raise ValueError(
                "LLM_GATEWAY_BASE_URL 未配置。当前项目禁止直连 API，"
                "请先启用本地网关。"
            )

        if not gateway_base_url.endswith("/v1"):
            gateway_base_url = f"{gateway_base_url}/v1"
        base_url = gateway_base_url
        if not gateway_api_key:
            gateway_api_key = api_key or "local-gateway"
        api_key = gateway_api_key
        logger.info("已启用 LLM Gateway: %s", base_url)
        # 对接说明（采购员 L2 等使用 ChatAgent.step(..., response_format=Pydantic)）：
        # CAMEL OpenAICompatibleModel 走 beta.chat.completions.parse，请求仍发到网关
        # POST /v1/chat/completions，body 含 response_format（json_schema/json_object）。
        # 网关 _dispatch_chat_completion 已透传至上游；ensure_json_keyword 仅对 json_object 补全「json」字样。
        # 若路由解析为 anthropic_messages，网关会 400（需改用 OpenAI 兼容模型名）。
        
        # 对话模型配置（优先使用网关配置）
        chat_model = (
            cfg_str("llm.gateway.chat_model", "", env="LLM_GATEWAY_CHAT_MODEL").strip()
            or cfg_str("llm.siliconflow.chat_model", "mineru-vlm", env="SILICONFLOW_CHAT_MODEL")
        )
        if not chat_model:
            chat_model = "mineru-vlm"
            logger.warning(
                "LLM_GATEWAY_CHAT_MODEL 未配置，使用默认值: %s。"
                "建议在 .env 文件中明确配置。",
                chat_model,
            )
        
        # 嵌入模型配置（优先使用网关配置）
        embedding_model = (
            cfg_str("llm.gateway.embedding_model", "", env="LLM_GATEWAY_EMBEDDING_MODEL").strip()
            or cfg_str("llm.siliconflow.embedding_model", "text-embedding-v4", env="SILICONFLOW_EMBEDDING_MODEL")
        )
        if not embedding_model:
            embedding_model = "text-embedding-v4"
            logger.warning(
                "LLM_GATEWAY_EMBEDDING_MODEL 未配置，使用默认值: %s。"
                "建议在 .env 文件中明确配置。",
                embedding_model,
            )
        
        # 重排序模型配置（优先使用网关配置）
        reranker_model = (
            cfg_str("llm.gateway.rerank_model", "", env="LLM_GATEWAY_RERANK_MODEL").strip()
            or cfg_str("llm.siliconflow.reranker_model", "qwen3-rerank", env="SILICONFLOW_RERANKER_MODEL")
        )
        if not reranker_model:
            reranker_model = "qwen3-rerank"
            logger.warning(
                "LLM_GATEWAY_RERANK_MODEL 未配置，使用默认值: %s。"
                "建议在 .env 文件中明确配置。",
                reranker_model,
            )
        
        config = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": cfg_float("llm.gateway.timeout_seconds", 60.0, env="LLM_GATEWAY_TIMEOUT") or cfg_float("llm.siliconflow.timeout_seconds", 60.0, env="SILICONFLOW_TIMEOUT"),
            # 模型名称
            "chat_model": chat_model,
            "embedding_model": embedding_model,
            "reranker_model": reranker_model,
            # 对话模型限速
            "chat_rpm": cfg_int("llm.gateway.chat_rpm", 0, env="LLM_GATEWAY_CHAT_RPM") or cfg_int("llm.siliconflow.chat_rpm", 1000, env="SILICONFLOW_CHAT_RPM"),
            "chat_tpm": cfg_int("llm.gateway.chat_tpm", 0, env="LLM_GATEWAY_CHAT_TPM") or cfg_int("llm.siliconflow.chat_tpm", 50000, env="SILICONFLOW_CHAT_TPM"),
            # 嵌入模型限速
            "embedding_rpm": cfg_int("llm.gateway.embedding_rpm", 0, env="LLM_GATEWAY_EMBEDDING_RPM") or cfg_int("llm.siliconflow.embedding_rpm", 2000, env="SILICONFLOW_EMBEDDING_RPM"),
            "embedding_tpm": cfg_int("llm.gateway.embedding_tpm", 0, env="LLM_GATEWAY_EMBEDDING_TPM") or cfg_int("llm.siliconflow.embedding_tpm", 500000, env="SILICONFLOW_EMBEDDING_TPM"),
            # 重排序模型限速
            "reranker_rpm": cfg_int("llm.gateway.reranker_rpm", 0, env="LLM_GATEWAY_RERANKER_RPM") or cfg_int("llm.siliconflow.reranker_rpm", 2000, env="SILICONFLOW_RERANKER_RPM"),
            "reranker_tpm": cfg_int("llm.gateway.reranker_tpm", 0, env="LLM_GATEWAY_RERANKER_TPM") or cfg_int("llm.siliconflow.reranker_tpm", 500000, env="SILICONFLOW_RERANKER_TPM"),
            "gateway_enabled": bool(gateway_base_url),
        }
        
        self._config_cache["gateway"] = config
        self._config_cache["siliconflow"] = config
        return config

    # Backward compatible alias
    def get_siliconflow_config(self) -> Dict[str, Any]:
        return self.get_gateway_config()
    
    def get_llm_config(
        self,
        provider: str = "gateway",
        use_config_file: bool = True
    ) -> Dict[str, Any]:
        """
        获取指定提供商的 LLM 配置（已统一使用硅基流动）
        
        Args:
            provider: 提供商名称（已弃用，所有请求都返回硅基流动配置）
            use_config_file: 是否从配置文件加载（如果支持）
        
        Returns:
            LLM 配置字典（硅基流动配置）
        """
        # 统一使用硅基流动配置，忽略 provider 参数
        # 为向后兼容保留 provider 参数但记录警告
        provider_lower = provider.lower()
        
        if provider_lower not in ("gateway", "siliconflow", "qianfan", "openai"):
            logger.warning("未知的 LLM 提供商: %s，使用网关配置", provider)
        elif provider_lower not in ("gateway", "siliconflow"):
            logger.debug("已将 LLM 提供商从 %s 统一切换为网关", provider)
        
        # 统一返回网关配置
        config = self.get_gateway_config()
        
        # 如果支持从配置文件加载，尝试加载
        if use_config_file:
            config = self._merge_with_config_file(config, "gateway")
        
        return config
    
    def _merge_with_config_file(
        self,
        config: Dict[str, Any],
        provider: str
    ) -> Dict[str, Any]:
        """
        从配置文件合并配置（如果存在）
        
        优先级：配置文件 > 环境变量 > 默认值
        """
        # 目前 mineru.json 中的 "llm-aided-config.metadata_extraction"
        # 默认是为网关准备的配置：
        # - api_key/base_url/model 都指向网关相关的环境变量
        #
        # 如果无差别地对所有 provider（包括 "qianfan"）应用这些覆盖，
        # 会出现 base_url 是网关，而 model 仍然是历史遗留模型名。
        # 这种跨提供商混配，最终导致 auto_convert 在调用时发送错误模型
        # 请求并报 “Model does not exist” 之类的 400 错误。
        #
        # 为避免这种错误，将配置文件合并限定在默认 provider 上，
        # 其它 provider 继续只使用各自的环境变量配置。
        provider_lower = provider.lower()
        if provider_lower not in ("siliconflow", "gateway"):
            return config
        
        # 检查是否有配置文件（如 mineru.json）
        config_file = Path(__file__).parent.parent / "mineru.json"
        if not config_file.exists():
            return config
        
        try:
            import json
            with open(config_file, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            
            # 从配置文件中提取 LLM 配置
            llm_config = file_config.get("llm-aided-config", {}).get("metadata_extraction", {})
            if llm_config:
                gateway_enabled = bool(
                    cfg_str("llm.gateway.base_url", "", env="LLM_GATEWAY_BASE_URL").strip()
                )
                # 合并配置（配置文件优先级更高）
                for key in ["api_key", "base_url", "model", "timeout"]:
                    if gateway_enabled and key in {"api_key", "base_url"}:
                        continue
                    file_value = resolve_env_placeholder(llm_config.get(key))
                    if file_value:
                        config[key] = file_value
        except Exception as e:
            logger.debug("从配置文件加载 LLM 配置失败: %s", e)
        
        return config
    
    def _normalize_base_url(self, url: str) -> str:
        """
        规范化 base_url（移除 /chat/completions 等路径）
        """
        if not url:
            return ""
        
        url = url.strip().rstrip("/")
        
        # 移除可能的完整端点路径
        if url.endswith("/chat/completions"):
            url = url[:-len("/chat/completions")]
        if url.endswith("/v1/chat/completions"):
            url = url[:-len("/v1/chat/completions")]
        
        return url.rstrip("/")
    
    def _get_gateway_temperature(self) -> float:
        """获取网关温度参数"""
        raw = cfg_str("llm.siliconflow.temperature", "0.7", env="SILICONFLOW_TEMPERATURE")
        try:
            value = float(raw)
        except Exception:
            value = 0.7
        
        # 网关推荐温度范围 [0, 2.0]，默认 0.7
        if value < 0.0:
            value = 0.0
        if value > 2.0:
            value = 2.0
        
        return value
    
    # 向后兼容
    def _get_qianfan_temperature(self) -> float:
        """获取温度参数（向后兼容，实际返回网关温度）"""
        return self._get_gateway_temperature()
    
    def validate_config(self, config: Dict[str, Any], provider: str = "qianfan") -> tuple[bool, List[str]]:
        """
        验证配置是否完整
        
        Returns:
            (is_valid, missing_fields)
        """
        required_fields = {
            "qianfan": ["api_key", "base_url", "model"],
            "openai": ["api_key", "base_url"],
            "gateway": ["api_key", "base_url"],
            "siliconflow": ["api_key", "base_url"],
        }
        
        required = required_fields.get(provider.lower(), [])
        missing = []
        
        for field in required:
            if not config.get(field):
                missing.append(field)
        
        return len(missing) == 0, missing
    
    def clear_cache(self):
        """清除配置缓存（用于重新加载配置）"""
        self._config_cache.clear()


# 全局配置实例
_llm_config_instance: Optional[LLMConfig] = None


def get_llm_config(provider: str = "gateway") -> Dict[str, Any]:
    """
    获取 LLM 配置（统一入口）- 已统一使用 LLM 网关
    
    Args:
        provider: 提供商名称（已弃用，所有请求都返回网关配置）
    
    Returns:
        LLM 配置字典（网关配置）
    """
    global _llm_config_instance
    if _llm_config_instance is None:
        _llm_config_instance = LLMConfig()
    
    return _llm_config_instance.get_llm_config(provider)


def get_qianfan_config() -> Dict[str, Any]:
    """获取千帆配置（便捷函数）- 已统一返回网关配置"""
    return get_llm_config("gateway")


def get_openai_config() -> Dict[str, Any]:
    """获取 OpenAI 配置（便捷函数）- 已统一返回网关配置"""
    return get_llm_config("gateway")


def get_gateway_config() -> Dict[str, Any]:
    """获取 LLM 网关配置（首选命名）"""
    return get_llm_config("gateway")


def get_siliconflow_config() -> Dict[str, Any]:
    """获取 LLM 网关配置（兼容旧命名）"""
    return get_gateway_config()


def create_openai_client(provider: str = "gateway", **kwargs) -> Any:
    """
    创建 OpenAI 兼容客户端（统一接口）- 已统一使用 LLM 网关
    
    Args:
        provider: 提供商名称（已弃用，所有请求都使用网关）
        **kwargs: 额外的客户端参数
    
    Returns:
        OpenAI 客户端实例（连接到网关）
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("OpenAI 库未安装，无法创建客户端")
        return None
    
    # 统一使用网关配置
    config = get_llm_config("gateway")
    
    # 合并额外参数
    client_config = {
        "api_key": config.get("api_key") or kwargs.get("api_key"),
        "base_url": config.get("base_url") or kwargs.get("base_url"),
        "timeout": config.get("timeout", 60),
    }
    
    # 移除 None 值
    client_config = {k: v for k, v in client_config.items() if v is not None}
    
    if not client_config.get("api_key"):
        logger.warning("LLM 网关 API Key 未配置，无法创建客户端")
        return None
    
    return OpenAI(**client_config)


def check_llm_availability(provider: str = "gateway") -> tuple[bool, str]:
    """
    检查 LLM 服务是否可用（已统一使用 LLM 网关）
    
    Returns:
        (is_available, message)
    """
    # 统一使用网关配置
    config = get_llm_config("gateway")
    
    llm_config = LLMConfig()
    is_valid, missing = llm_config.validate_config(config, "gateway")
    
    if not is_valid:
        return False, f"LLM 网关配置不完整，缺少字段: {', '.join(missing)}"
    
    # 尝试连接（简单检查）
    base_url = config.get("base_url")
    if not base_url:
        return False, "LLM 网关 base_url 未配置"
    
    try:
        import urllib.request
        import urllib.error
        
        # 构建检查 URL
        check_url = base_url.rstrip("/") + "/v1/models"
        if not check_url.startswith("http"):
            check_url = "https://" + check_url
        
        req = urllib.request.Request(check_url)
        with urllib.request.urlopen(req, timeout=5) as response:
            return True, f"LLM 网关服务可用 (HTTP {response.status})"
    except urllib.error.HTTPError as e:
        # 4xx 错误可能表示服务存在但需要认证
        if e.code < 500:
            return True, f"LLM 网关服务可达但需要认证 (HTTP {e.code})"
        return False, f"LLM 网关服务不可用 (HTTP {e.code})"
    except Exception as e:
        return False, f"LLM 网关连接失败: {str(e)}"
