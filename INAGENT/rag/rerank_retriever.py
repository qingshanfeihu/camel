# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
网关重排序检索器

使用 LLM 网关的重排序模型对检索结果进行重排序
"""
import logging
from typing import List, Dict, Any, Optional
import requests

from camel.retrievers.base import BaseRetriever
from INAGENT.config.project_config import cfg_str

logger = logging.getLogger(__name__)

DEFAULT_TOP_K_RESULTS = 5


class SiliconFlowRerankRetriever(BaseRetriever):
    """使用 LLM 网关重排序模型的检索器"""
    
    def __init__(
        self,
        model_name: str = "qwen3-rerank",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        """
        初始化网关重排序检索器
        
        Args:
            model_name: 重排序模型名称，默认：qwen3-rerank
            api_key: API密钥，如果未提供则从环境变量读取
            base_url: API基础URL，默认来自 LLM 网关
        """
        gateway_base_url = cfg_str(
            "llm.gateway.base_url",
            "",
            env="LLM_GATEWAY_BASE_URL",
        )
        if gateway_base_url and not gateway_base_url.rstrip("/").endswith("/v1"):
            gateway_base_url = f"{gateway_base_url.rstrip('/')}/v1"
        gateway_api_key = cfg_str(
            "llm.gateway.api_key",
            "",
            env="LLM_GATEWAY_API_KEY",
        )

        self.api_key = api_key or gateway_api_key or cfg_str(
            "llm.siliconflow.api_key",
            "",
            env="SILICONFLOW_API_KEY",
        )
        if not gateway_base_url and not base_url:
            raise ValueError(
                "LLM_GATEWAY_BASE_URL 未配置。当前项目禁止直连 API。"
            )

        self.base_url = (base_url or gateway_base_url).rstrip("/")

        if not self.api_key:
            self.api_key = "local-gateway"
        
        self.model_name = model_name
    
    def query(
        self,
        query: str,
        retrieved_result: List[Dict[str, Any]],
        top_k: int = DEFAULT_TOP_K_RESULTS,
    ) -> List[Dict[str, Any]]:
        """
        使用网关重排序模型对检索结果进行重排序
        
        Args:
            query: 查询字符串
            retrieved_result: 待重排序的检索结果列表
            top_k: 返回的top结果数量
            
        Returns:
            重排序后的结果列表
        """
        if not retrieved_result:
            return []
        
        # 准备文档列表（提取text字段）
        documents = []
        for item in retrieved_result:
            if isinstance(item, dict):
                text = item.get("text", "")
                if not text:
                    # 尝试从其他字段获取文本
                    text = item.get("page_content", "") or str(item)
            else:
                text = str(item)
            documents.append(text)
        
        if not documents:
            return []
        
        # 调用网关重排序 API
        try:
            url = f"{self.base_url}/rerank"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model_name,
                "query": query,
                "documents": documents,
                "top_n": min(top_k, len(documents)),
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            result_data = response.json()
            
            rerank_results = (
                result_data.get("results")
                or result_data.get("output", {}).get("results")
            )

            formatted_results = []
            if rerank_results:
                results = sorted(
                    rerank_results,
                    key=lambda x: x.get("relevance_score", 0.0),
                    reverse=True
                )

                for result in results[:top_k]:
                    index = result.get("index", 0)
                    if 0 <= index < len(retrieved_result):
                        selected_chunk = retrieved_result[index].copy() if isinstance(retrieved_result[index], dict) else {"text": str(retrieved_result[index])}
                        selected_chunk["similarity score"] = result.get("relevance_score", 0.0)
                        formatted_results.append(selected_chunk)
            else:
                # 如果没有 results 字段，返回原始结果
                logger.warning("重排序API返回格式异常，缺少 results 字段，返回原始结果")
                formatted_results = retrieved_result[:top_k]

            if not formatted_results:
                logger.warning("重排序结果为空，返回原始结果")
                formatted_results = retrieved_result[:top_k]
            
            return formatted_results
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"网关重排序API调用失败: {e}，返回原始结果")
            return retrieved_result[:top_k]
        except Exception as e:
            logger.warning(f"重排序处理失败: {e}，返回原始结果")
            return retrieved_result[:top_k]
