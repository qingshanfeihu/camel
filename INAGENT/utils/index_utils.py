# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
索引工具函数：从功能结构索引动态加载所有信息，避免硬编码
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

logger = logging.getLogger(__name__)


def load_function_structure_index(index_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    动态加载功能结构索引（每次调用都重新加载，确保获取最新数据）
    """
    if index_path is None:
        index_path = Path(__file__).parent / "knowledge_base" / "function_structure_index.json"
    
    if not index_path.exists():
        logger.debug("功能结构索引不存在，返回空索引")
        return {}
    
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载功能结构索引失败: {e}")
        return {}


def get_step_type_keywords(function_index: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    从索引动态获取步骤类型关键词（不硬编码）
    
    Returns:
        {
            "backend_servers": ["关键词1", "关键词2", ...],
            "virtual_services": [...],
            ...
        }
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    # 从 metadata_statistics 中提取步骤类型关键词
    step_types = function_index.get("metadata_statistics", {}).get("step_types", {})
    
    step_keywords = {}
    for step_type, stats in step_types.items():
        if not isinstance(stats, dict):
            continue
        keywords = stats.get("keywords", [])
        step_keywords[step_type] = keywords
    
    # 如果索引中没有，返回空字典（而不是硬编码的默认值）
    return step_keywords


def get_product_module_keywords(function_index: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    从索引动态获取产品模块关键词（不硬编码）
    
    Returns:
        {
            "SLB": ["关键词1", "关键词2", ...],
            "LLB": [...],
            ...
        }
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    # 从 metadata_statistics 中提取产品模块关键词
    product_modules = function_index.get("metadata_statistics", {}).get("product_modules", {})
    
    module_keywords = {}
    for module_name, stats in product_modules.items():
        keywords = stats.get("keywords", [])
        module_keywords[module_name] = keywords
    
    # 从 modules 中补充关键词
    modules_info = function_index.get("modules", {})
    for module_name, module_data in modules_info.items():
        if module_name not in module_keywords:
            module_keywords[module_name] = []
        existing_keywords = set(module_keywords[module_name])
        new_keywords = module_data.get("keywords", [])
        module_keywords[module_name] = list(existing_keywords | set(new_keywords))
    
    return module_keywords


def get_protocol_type_keywords(function_index: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    从索引动态获取协议类型关键词（不硬编码）
    
    Returns:
        {
            "HTTP": ["关键词1", "关键词2", ...],
            "HTTPS": [...],
            ...
        }
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    # 从 metadata_statistics 中提取协议类型关键词
    protocol_types = function_index.get("metadata_statistics", {}).get("protocol_types", {})
    
    protocol_keywords = {}
    for protocol_name, stats in protocol_types.items():
        keywords = stats.get("keywords", [])
        protocol_keywords[protocol_name] = keywords
    
    return protocol_keywords


def get_advanced_feature_keywords(function_index: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
    """
    从索引动态获取高级功能关键词（不硬编码）
    
    注意：高级功能关键词可能需要在索引中单独定义，或者从实际使用中提取
    目前返回空字典，后续可以从索引中扩展
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    # 如果索引中有高级功能定义，从这里提取
    # 目前暂时返回空字典，后续可以扩展
    advanced_features = function_index.get("advanced_features", {})
    
    feature_keywords = {}
    for feature_name, feature_data in advanced_features.items():
        if isinstance(feature_data, dict):
            keywords = feature_data.get("keywords", [])
        elif isinstance(feature_data, list):
            keywords = feature_data
        else:
            keywords = []
        feature_keywords[feature_name] = keywords
    
    return feature_keywords


def get_all_scenarios(function_index: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """
    从索引动态获取所有场景（不硬编码）
    
    Returns:
        {
            "SLB_HTTP_FULL_CONFIG": {
                "product_modules": [...],
                "protocol_types": [...],
                "required_steps": [...]
            },
            ...
        }
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    return function_index.get("scenarios", {})


def generate_query_from_decomposition(
    decomposition_result: Dict[str, Any],
    function_index: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    基于任务分解结果动态生成查询（不硬编码模块名称）
    
    Args:
        decomposition_result: 任务分解结果
        function_index: 功能结构索引
    
    Returns:
        查询列表
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    queries = []
    
    # 获取产品模块和协议类型
    product_modules = decomposition_result.get("product_modules", [])
    protocol_types = decomposition_result.get("protocol_type", [])
    if isinstance(protocol_types, str):
        protocol_types = [protocol_types]
    
    # 动态生成查询（不硬编码模块名称）
    if product_modules and protocol_types:
        for module in product_modules:
            for protocol in protocol_types:
                queries.extend([
                    f"{protocol}类型{module}的配置示例",
                    f"{protocol} type {module} configuration example",
                    f"{module} {protocol} 完整配置",
                ])
    elif product_modules:
        for module in product_modules:
            queries.extend([
                f"{module}配置示例",
                f"{module} configuration example",
            ])
    
    return queries


def detect_step_type_from_text(
    text: str,
    function_index: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    从文本中检测步骤类型（基于索引中的关键词，不硬编码）
    
    Args:
        text: 文本内容
        function_index: 功能结构索引
    
    Returns:
        检测到的步骤类型，如果没有匹配则返回 None
    """
    step_keywords = get_step_type_keywords(function_index)
    text_lower = text.lower()
    
    # 按匹配的关键词数量排序，返回最匹配的步骤类型
    matches = []
    for step_type, keywords in step_keywords.items():
        match_count = sum(1 for kw in keywords if kw.lower() in text_lower)
        if match_count > 0:
            matches.append((step_type, match_count))
    
    if matches:
        # 返回匹配最多的步骤类型
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[0][0]
    
    return None


def detect_product_module_from_text(
    text: str,
    function_index: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    从文本中检测产品模块（基于索引中的关键词，不硬编码）
    
    Args:
        text: 文本内容
        function_index: 功能结构索引
    
    Returns:
        检测到的产品模块列表
    """
    module_keywords = get_product_module_keywords(function_index)
    text_lower = text.lower()
    
    detected_modules = []
    for module_name, keywords in module_keywords.items():
        if any(kw.lower() in text_lower for kw in keywords):
            detected_modules.append(module_name)
    
    return detected_modules
