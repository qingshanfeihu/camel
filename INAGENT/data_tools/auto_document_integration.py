# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
自动文档集成模块

功能：
1. 自动识别新文档所属模块和功能
2. 增量更新功能结构索引
3. 自动重新索引到 RAG
"""
import json
import logging
import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

logger = logging.getLogger(__name__)


def compute_chunk_hash(chunk: Dict[str, Any]) -> str:
    """计算块的 hash 值，用于增量索引"""
    content = chunk.get("page_content", "") or chunk.get("text", "")
    metadata = chunk.get("metadata", {})
    # 使用内容和关键 metadata 计算 hash
    key_metadata = {
        "product_module": metadata.get("product_module"),
        "protocol_type": metadata.get("protocol_type"),
        "step_type": metadata.get("step_type"),
    }
    payload = json.dumps({"content": content, "metadata": key_metadata}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def infer_module_from_path(
    pdf_path: Path,
    function_index: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    从文件路径推断模块（动态加载模块关键词，不硬编码）
    
    策略：
    1. 从功能结构索引加载所有已知模块及其关键词
    2. 基于这些关键词匹配路径
    3. 如果无法匹配，返回空列表（由其他方法处理）
    
    Args:
        pdf_path: PDF 文件路径
        function_index: 功能结构索引（如果为 None，则自动加载）
    
    Returns:
        匹配到的模块列表
    """
    if function_index is None:
        function_index = load_function_structure_index()
    
    path_str = str(pdf_path).lower()
    modules = []
    
    # 从索引动态加载模块关键词
    modules_info = function_index.get("modules", {})
    metadata_stats = function_index.get("metadata_statistics", {})
    product_modules = metadata_stats.get("product_modules", {})
    
    # 合并两个来源的模块信息
    all_modules = {}
    for module_name, module_data in modules_info.items():
        keywords = module_data.get("keywords", [])
        all_modules[module_name] = keywords
    
    for module_name, module_stats in product_modules.items():
        if module_name not in all_modules:
            all_modules[module_name] = []
        # 合并关键词
        existing_keywords = all_modules[module_name]
        new_keywords = module_stats.get("keywords", [])
        all_modules[module_name] = list(set(existing_keywords + new_keywords))
    
    # 基于关键词匹配路径
    for module_name, keywords in all_modules.items():
        # 检查模块名称本身
        if module_name.lower() in path_str:
            modules.append(module_name)
            continue
        
        # 检查关键词
        for keyword in keywords:
            if keyword and keyword.lower() in path_str:
                modules.append(module_name)
                break
    
    return list(set(modules))  # 去重


def infer_document_type(pdf_path: Path, content_blocks: List[Dict]) -> str:
    """
    推断文档类型：app, cli, api, unknown
    """
    path_str = str(pdf_path).lower()
    
    # 从路径推断
    if "cli" in path_str or "command" in path_str:
        return "cli"
    if "api" in path_str or "rest" in path_str:
        return "api"
    if "app" in path_str or "application" in path_str or "配置" in path_str:
        return "app"
    
    # 从内容推断（检查是否有 CLI 命令语法）
    for block in content_blocks[:10]:  # 只检查前 10 个块
        text = block.get("page_content", "") or block.get("text", "")
        if re.search(r'<[^>]+>|\[[^\]]+\]|\{[^}]+\}', text):  # CLI 语法模式
            return "cli"
        if "api" in text.lower() or "rest" in text.lower() or "endpoint" in text.lower():
            return "api"
    
    return "app"  # 默认为 app


def extract_document_statistics(content_blocks: List[Dict]) -> Dict[str, Any]:
    """
    从文档块中提取统计信息（模块、协议、步骤类型）
    """
    product_modules: Dict[str, int] = defaultdict(int)
    protocol_types: Dict[str, int] = defaultdict(int)
    step_types: Dict[str, int] = defaultdict(int)
    keywords: Set[str] = set()
    
    for block in content_blocks:
        metadata = block.get("metadata", {})
        text = block.get("page_content", "") or block.get("text", "")
        text_lower = text.lower()
        
        # 统计 product_module
        pm = metadata.get("product_module")
        if pm:
            product_modules[pm] += 1
        
        # 统计 protocol_type
        pt = metadata.get("protocol_type")
        if pt:
            if isinstance(pt, list):
                for p in pt:
                    protocol_types[p] += 1
            else:
                protocol_types[pt] += 1
        
        # 统计 step_type
        st = metadata.get("step_type")
        if st:
            step_types[st] += 1
        
        # 提取关键词（简单提取）
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text_lower)
        keywords.update(words)
        chinese_words = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
        keywords.update(chinese_words)
    
    return {
        "product_modules": dict(product_modules),
        "protocol_types": dict(protocol_types),
        "step_types": dict(step_types),
        "keywords": sorted(list(keywords))[:50]  # 限制数量
    }


def match_similar_documents(
    document_stats: Dict[str, Any],
    existing_index: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    基于统计信息匹配已有文档
    
    Returns:
        List of similar document metadata
    """
    similar_docs = []
    existing_stats = existing_index.get("metadata_statistics", {})
    
    # 比较 product_modules
    doc_modules = set(document_stats.get("product_modules", {}).keys())
    existing_modules = set(existing_stats.get("product_modules", {}).keys())
    
    # 比较 protocol_types
    doc_protocols = set(document_stats.get("protocol_types", {}).keys())
    existing_protocols = set(existing_stats.get("protocol_types", {}).keys())
    
    # 计算相似度
    module_overlap = len(doc_modules & existing_modules) / max(len(doc_modules), 1)
    protocol_overlap = len(doc_protocols & existing_protocols) / max(len(doc_protocols), 1)
    
    if module_overlap > 0.5 or protocol_overlap > 0.5:
        similar_docs.append({
            "similarity": (module_overlap + protocol_overlap) / 2,
            "matched_modules": list(doc_modules & existing_modules),
            "matched_protocols": list(doc_protocols & existing_protocols)
        })
    
    return similar_docs


def discover_new_modules_from_content(
    content_blocks: List[Dict],
    existing_index: Dict[str, Any]
) -> List[str]:
    """
    从文档内容中发现新模块（基于文档内容统计，但不在现有索引中）
    
    策略：
    1. 提取文档中所有 product_module metadata
    2. 与现有索引中的模块比较
    3. 返回不在索引中的模块（可能是新模块）
    
    Returns:
        新发现的模块列表
    """
    existing_modules = set(existing_index.get("metadata_statistics", {}).get("product_modules", {}).keys())
    existing_modules.update(existing_index.get("modules", {}).keys())
    
    document_modules = set()
    for block in content_blocks:
        metadata = block.get("metadata", {})
        pm = metadata.get("product_module")
        if pm and pm != "unknown":
            document_modules.add(pm)
    
    # 找出不在现有索引中的模块
    new_modules = document_modules - existing_modules
    return list(new_modules)


def auto_identify_document_module(
    pdf_path: Path,
    content_blocks: List[Dict],
    existing_index: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    自动识别文档所属模块和功能（完全动态，不硬编码）
    
    策略（优先级从高到低）：
    1. 从文档内容统计识别（基于所有块的 metadata）- 最可靠
    2. 从文件名/目录结构推断（基于索引中的模块关键词）
    3. 从已有索引匹配相似文档
    4. 自动发现新模块（如果文档中的模块不在索引中）
    
    Returns:
        {
            "product_modules": ["SLB", "LLB"] 或 ["webui"]（新模块）,
            "protocol_types": ["HTTP", "TCP"],
            "document_type": "app" | "cli" | "api" | "unknown",
            "confidence": 0.95,
            "matched_similar_docs": [...],
            "document_statistics": {...},
            "new_modules_discovered": ["webui", "SNMP"]  # 新发现的模块
        }
    """
    if existing_index is None:
        existing_index = load_function_structure_index()
    
    # 1. 从文档内容统计识别（最可靠的方法）
    document_stats = extract_document_statistics(content_blocks)
    
    # 取最常见的模块（出现次数最多的）
    product_modules_from_stats = []
    if document_stats.get("product_modules"):
        sorted_modules = sorted(
            document_stats["product_modules"].items(),
            key=lambda x: x[1],
            reverse=True
        )
        # 取前 3 个最常见的模块
        product_modules_from_stats = [m[0] for m in sorted_modules[:3]]
    
    # 取最常见的协议
    protocol_types_from_stats = []
    if document_stats.get("protocol_types"):
        sorted_protocols = sorted(
            document_stats["protocol_types"].items(),
            key=lambda x: x[1],
            reverse=True
        )
        protocol_types_from_stats = [p[0] for p in sorted_protocols[:5]]
    
    # 2. 从文件名/目录结构推断（使用动态加载的模块关键词）
    modules_from_path = infer_module_from_path(pdf_path, existing_index)
    
    # 3. 自动发现新模块（如果文档中的模块不在索引中）
    new_modules = discover_new_modules_from_content(content_blocks, existing_index)
    
    # 4. 合并结果（优先使用统计结果，路径推断作为补充，新模块也包含）
    final_modules = list(set(product_modules_from_stats + modules_from_path + new_modules))
    
    # 如果仍然无法确定，不强制设置默认值（让后续流程处理）
    # 这样可以避免错误分类新功能模块
    
    # 5. 匹配相似文档
    similar_docs = []
    if existing_index:
        similar_docs = match_similar_documents(document_stats, existing_index)
    
    # 6. 推断文档类型
    document_type = infer_document_type(pdf_path, content_blocks)
    
    # 7. 计算置信度
    confidence = 0.5
    if product_modules_from_stats:
        confidence += 0.3
    if modules_from_path:
        confidence += 0.1
    if similar_docs:
        confidence += 0.1
    if new_modules:
        # 新模块的置信度可能较低，因为需要进一步验证
        confidence -= 0.1
    
    return {
        "product_modules": final_modules,
        "protocol_types": protocol_types_from_stats,
        "document_type": document_type,
        "confidence": min(max(confidence, 0.0), 1.0),
        "matched_similar_docs": similar_docs,
        "document_statistics": document_stats,
        "new_modules_discovered": new_modules  # 新发现的模块
    }


def merge_statistics(
    existing_stats: Dict[str, Any],
    new_stats: Dict[str, Any]
) -> Dict[str, Any]:
    """
    合并统计信息（增量更新）
    """
    merged = {
        "product_modules": {},
        "protocol_types": {},
        "step_types": {}
    }
    
    # 合并 product_modules
    for module, count in existing_stats.get("product_modules", {}).items():
        merged["product_modules"][module] = count
    for module, count in new_stats.get("product_modules", {}).items():
        merged["product_modules"][module] = merged["product_modules"].get(module, 0) + count
    
    # 合并 protocol_types
    for protocol, count in existing_stats.get("protocol_types", {}).items():
        merged["protocol_types"][protocol] = count
    for protocol, count in new_stats.get("protocol_types", {}).items():
        merged["protocol_types"][protocol] = merged["protocol_types"].get(protocol, 0) + count
    
    # 合并 step_types
    for step_type, count in existing_stats.get("step_types", {}).items():
        merged["step_types"][step_type] = count
    for step_type, count in new_stats.get("step_types", {}).items():
        merged["step_types"][step_type] = merged["step_types"].get(step_type, 0) + count
    
    return merged


def incrementally_update_function_index(
    new_documents: List[Dict[str, Any]],
    existing_index_path: Path,
    logger: Optional[Any] = None
) -> Dict[str, Any]:
    """
    增量更新功能结构索引
    
    Args:
        new_documents: 新文档列表，每个元素包含：
            - pdf_path: PDF 路径
            - json_path: 转换后的 JSON 路径
            - document_metadata: 文档 metadata（从 auto_identify_document_module 获取）
        existing_index_path: 现有索引文件路径
    
    Returns:
        更新后的索引
    """
    # 使用传入的 logger 或默认 logger
    if logger is None:
        import logging
        logger = logging.getLogger(__name__)
    
    # 加载现有索引
    if existing_index_path.exists():
        try:
            logger.info(f"[index-update] 加载现有索引: {existing_index_path}")
            with open(existing_index_path, "r", encoding="utf-8") as f:
                existing_index = json.load(f)
            logger.info(f"[index-update] 现有索引版本: {existing_index.get('version', 'unknown')}")
        except Exception as e:
            logger.warning(f"[index-update] 加载现有索引失败: {e}，创建新索引")
            existing_index = {
                "version": "2.0",
                "source": "auto_extracted",
                "cli_hierarchy": {},
                "document_hierarchy": {},
                "metadata_statistics": {},
                "scenarios": {},
                "modules": {}
            }
    else:
        logger.info(f"[index-update] 索引文件不存在，创建新索引: {existing_index_path}")
        existing_index = {
            "version": "2.0",
            "source": "auto_extracted",
            "cli_hierarchy": {},
            "document_hierarchy": {},
            "metadata_statistics": {},
            "scenarios": {},
            "modules": {}
        }
    
    # 收集所有新文档的统计信息
    all_new_stats = {
        "product_modules": {},
        "protocol_types": {},
        "step_types": {}
    }
    
    for doc in new_documents:
        doc_stats = doc.get("document_metadata", {}).get("document_statistics", {})
        
        # 合并 product_modules
        for module, count in doc_stats.get("product_modules", {}).items():
            all_new_stats["product_modules"][module] = \
                all_new_stats["product_modules"].get(module, 0) + count
        
        # 合并 protocol_types
        for protocol, count in doc_stats.get("protocol_types", {}).items():
            all_new_stats["protocol_types"][protocol] = \
                all_new_stats["protocol_types"].get(protocol, 0) + count
        
        # 合并 step_types
        for step_type, count in doc_stats.get("step_types", {}).items():
            all_new_stats["step_types"][step_type] = \
                all_new_stats["step_types"].get(step_type, 0) + count
    
    # 更新 metadata_statistics
    logger.info("[index-update] 合并统计信息...")
    existing_stats = existing_index.get("metadata_statistics", {})
    existing_index["metadata_statistics"] = merge_statistics(existing_stats, all_new_stats)
    
    # 更新 modules（基于新的统计信息）
    logger.info("[index-update] 更新模块信息...")
    new_modules_count = 0
    for module_name, count in all_new_stats["product_modules"].items():
        if module_name not in existing_index.get("modules", {}):
            existing_index.setdefault("modules", {})[module_name] = {
                "module_name": module_name,
                "keywords": [],
                "step_types": {},
                "cli_commands": []
            }
            new_modules_count += 1
            logger.info(f"[index-update] 发现新模块: {module_name} (出现次数: {count})")
        else:
            logger.debug(f"[index-update] 更新现有模块: {module_name} (新增次数: {count})")
        # 更新关键词（从新文档中提取）
        # 这里可以进一步优化，从实际文档内容中提取关键词
    
    logger.info(
        f"[index-update] 统计信息更新完成: "
        f"产品模块={len(all_new_stats['product_modules'])}, "
        f"协议类型={len(all_new_stats['protocol_types'])}, "
        f"步骤类型={len(all_new_stats['step_types'])}, "
        f"新模块={new_modules_count}"
    )
    
    return existing_index


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
