# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
RAG 评分工具模块

提供动态协议加权、图关系评分等功能，优化 RAG 检索结果的排序。

核心功能：
1. 动态协议加权：根据查询和文档的协议类型匹配度调整分数
2. 图关系评分：基于实体关系计算图得分
3. 综合评分：结合向量分数、rerank 分数、协议分数、图分数

与 camel-ai 的差异：
- camel-ai: 硬编码 HTTP/HTTPS 协议，固定加权值
- INFOAGEN: 动态识别协议类型，支持任意协议扩展
"""
import re
import logging
from typing import Dict, Any, List, Set, Tuple, Optional

logger = logging.getLogger(__name__)

# 协议类型模式（支持动态扩展）
PROTOCOL_PATTERNS = [
    r'\b(https?)\b',      # HTTP, HTTPS
    r'\b(tcp|udp)\b',     # TCP, UDP
    r'\b(ftp|ftps|sftp)\b',  # FTP 系列
    r'\b(dns|dhcp)\b',    # 网络服务
    r'\b(sip|rtsp)\b',    # 流媒体
    r'\b(smtp|pop3|imap)\b',  # 邮件
    r'\b(ssh|telnet)\b',  # 远程访问
    r'\b(ldap|ldaps)\b',  # 目录服务
    r'\b(nfs|smb|cifs)\b',  # 文件共享
    r'\b(mysql|postgresql|redis)\b',  # 数据库
]

# 协议泛化映射（父协议 -> 子协议）
PROTOCOL_GENERALIZATION = {
    "http": ["https"],      # HTTP 可以泛化到 HTTPS
    "ftp": ["ftps", "sftp"],
    "ldap": ["ldaps"],
    "tcp": ["http", "https", "ftp", "ssh", "smtp"],  # TCP 是底层协议
    "udp": ["dns", "dhcp", "sip", "rtsp"],
}

# 协议安全性层级（用于判断升级/降级）
PROTOCOL_SECURITY_LEVEL = {
    "http": 1,
    "https": 2,
    "ftp": 1,
    "ftps": 2,
    "sftp": 2,
    "ldap": 1,
    "ldaps": 2,
    "telnet": 0,
    "ssh": 2,
}


def extract_protocols_from_text(text: str) -> Set[str]:
    """
    从文本中动态提取协议类型
    
    Args:
        text: 输入文本
    
    Returns:
        协议类型集合（小写）
    """
    if not text:
        return set()
    
    text_lower = text.lower()
    protocols = set()
    
    for pattern in PROTOCOL_PATTERNS:
        matches = re.findall(pattern, text_lower)
        protocols.update(matches)
    
    return protocols


def extract_protocols_from_metadata(metadata: Dict[str, Any]) -> Set[str]:
    """
    从元数据中提取协议类型
    
    Args:
        metadata: 文档元数据
    
    Returns:
        协议类型集合（小写）
    """
    protocols = set()
    
    protocol_type = metadata.get("protocol_type", [])
    if isinstance(protocol_type, list):
        protocols.update(p.lower() for p in protocol_type if p)
    elif protocol_type:
        protocols.add(protocol_type.lower())
    
    return protocols


def compute_protocol_boost(
    query: str,
    doc: Dict[str, Any],
    exact_match_boost: float = 0.05,
    partial_match_boost: float = 0.02,
    mismatch_penalty: float = -0.03,
) -> Tuple[float, Dict[str, Any]]:
    """
    计算动态协议加权
    
    与 camel-ai 的差异：
    - 动态识别协议类型，不硬编码
    - 支持协议泛化（HTTP -> HTTPS）
    - 支持多协议匹配
    - 考虑安全性层级
    
    Args:
        query: 查询字符串
        doc: 文档对象（包含 text 和 metadata）
        exact_match_boost: 精确匹配加权值
        partial_match_boost: 部分匹配加权值
        mismatch_penalty: 不匹配惩罚值
    
    Returns:
        (加权值, 详细信息)
    """
    # 提取查询中的协议
    query_protocols = extract_protocols_from_text(query)
    
    # 提取文档中的协议（优先使用 metadata）
    metadata = doc.get("metadata", {}) or {}
    doc_protocols = extract_protocols_from_metadata(metadata)
    
    # 如果 metadata 中没有，从文本中提取
    if not doc_protocols:
        text = doc.get("text", "") or doc.get("content", "")
        doc_protocols = extract_protocols_from_text(text[:500])
    
    boost = 0.0
    details = {
        "query_protocols": list(query_protocols),
        "doc_protocols": list(doc_protocols),
        "exact_matches": [],
        "partial_matches": [],
        "mismatches": [],
    }
    
    if not query_protocols:
        # 查询中没有协议，不需要加权
        return 0.0, details
    
    # 1. 精确匹配
    exact_matches = query_protocols & doc_protocols
    if exact_matches:
        boost += exact_match_boost * len(exact_matches)
        details["exact_matches"] = list(exact_matches)
    
    # 2. 协议泛化匹配
    for q_proto in query_protocols:
        if q_proto in exact_matches:
            continue
        
        # 检查是否可以通过泛化匹配
        generalized = PROTOCOL_GENERALIZATION.get(q_proto, [])
        partial = set(generalized) & doc_protocols
        if partial:
            # 检查安全性层级
            q_level = PROTOCOL_SECURITY_LEVEL.get(q_proto, 1)
            for p_proto in partial:
                p_level = PROTOCOL_SECURITY_LEVEL.get(p_proto, 1)
                if p_level >= q_level:
                    # 文档协议安全性不低于查询协议
                    boost += partial_match_boost
                    details["partial_matches"].append(f"{q_proto}->{p_proto}")
                else:
                    # 文档协议安全性低于查询协议（降级）
                    boost += partial_match_boost * 0.5
                    details["partial_matches"].append(f"{q_proto}->{p_proto}(降级)")
    
    # 3. 不匹配惩罚
    if doc_protocols and not exact_matches and not details["partial_matches"]:
        # 文档有协议但与查询不匹配
        boost += mismatch_penalty
        details["mismatches"] = list(doc_protocols - query_protocols)
    
    return boost, details


def compute_graph_score(
    query_entities: List[str],
    doc_entities: List[str],
    relationships: Optional[List[Dict[str, str]]] = None,
    direct_match_score: float = 1.0,
    one_hop_score: float = 0.5,
    two_hop_score: float = 0.25,
) -> Tuple[float, Dict[str, Any]]:
    """
    计算图关系评分
    
    基于实体和关系计算文档与查询的图相关性。
    
    Args:
        query_entities: 查询实体列表
        doc_entities: 文档实体列表
        relationships: 关系列表（可选）
        direct_match_score: 直接匹配分数
        one_hop_score: 1跳关系分数
        two_hop_score: 2跳关系分数
    
    Returns:
        (图得分, 详细信息)
    """
    if not query_entities or not doc_entities:
        return 0.0, {"direct_matches": [], "hop_matches": []}
    
    score = 0.0
    details = {
        "direct_matches": [],
        "hop_matches": [],
    }
    
    # 1. 直接匹配
    query_set = set(e.lower() for e in query_entities)
    doc_set = set(e.lower() for e in doc_entities)
    
    direct_matches = query_set & doc_set
    if direct_matches:
        score += direct_match_score * len(direct_matches)
        details["direct_matches"] = list(direct_matches)
    
    # 2. 通过关系计算跳数
    if relationships:
        # 构建邻接表
        adjacency = {}
        for rel in relationships:
            source = rel.get("source", "").lower()
            target = rel.get("target", "").lower()
            if source not in adjacency:
                adjacency[source] = set()
            if target not in adjacency:
                adjacency[target] = set()
            adjacency[source].add(target)
            adjacency[target].add(source)
        
        # 计算跳数匹配
        for q_entity in query_set:
            if q_entity in direct_matches:
                continue
            
            # 1跳
            one_hop_neighbors = adjacency.get(q_entity, set())
            one_hop_matches = one_hop_neighbors & doc_set
            if one_hop_matches:
                score += one_hop_score * len(one_hop_matches)
                details["hop_matches"].extend([
                    {"entity": m, "hops": 1, "from": q_entity}
                    for m in one_hop_matches
                ])
                continue
            
            # 2跳
            two_hop_neighbors = set()
            for neighbor in one_hop_neighbors:
                two_hop_neighbors.update(adjacency.get(neighbor, set()))
            two_hop_neighbors -= one_hop_neighbors  # 排除1跳
            two_hop_neighbors.discard(q_entity)  # 排除自身
            
            two_hop_matches = two_hop_neighbors & doc_set
            if two_hop_matches:
                score += two_hop_score * len(two_hop_matches)
                details["hop_matches"].extend([
                    {"entity": m, "hops": 2, "from": q_entity}
                    for m in two_hop_matches
                ])
    
    # 归一化
    if query_entities:
        score /= len(query_entities)
    
    return score, details


def apply_scoring_boost(
    results: List[Dict[str, Any]],
    query: str,
    use_protocol_boost: bool = True,
    use_graph_boost: bool = False,
    query_entities: Optional[List[str]] = None,
    relationships: Optional[List[Dict[str, str]]] = None,
    vector_weight: float = 0.6,
    graph_weight: float = 0.4,
) -> List[Dict[str, Any]]:
    """
    对检索结果应用综合评分
    
    Args:
        results: 检索结果列表
        query: 查询字符串
        use_protocol_boost: 是否使用协议加权
        use_graph_boost: 是否使用图加权
        query_entities: 查询实体列表（用于图加权）
        relationships: 关系列表（用于图加权）
        vector_weight: 向量分数权重
        graph_weight: 图分数权重
    
    Returns:
        调整评分后的结果列表（已排序）
    """
    if not results:
        return results
    
    for item in results:
        if not isinstance(item, dict):
            continue
        
        # 获取原始分数
        base_score = item.get("score", item.get("similarity score", 0.0))
        if base_score is None:
            base_score = 0.0
        
        total_boost = 0.0
        boost_details = {}
        
        # 1. 协议加权
        if use_protocol_boost:
            proto_boost, proto_details = compute_protocol_boost(query, item)
            total_boost += proto_boost
            boost_details["protocol"] = proto_details
            item["protocol_boost"] = proto_boost
        
        # 2. 图加权
        if use_graph_boost and query_entities:
            doc_entities = item.get("entities", [])
            if not doc_entities:
                # 尝试从 metadata 提取
                metadata = item.get("metadata", {})
                doc_entities = metadata.get("entities", [])
            
            graph_score, graph_details = compute_graph_score(
                query_entities,
                doc_entities,
                relationships,
            )
            
            # 综合加权
            graph_contribution = graph_score * graph_weight
            total_boost += graph_contribution
            boost_details["graph"] = graph_details
            item["graph_score"] = graph_score
        
        # 3. 计算最终分数
        final_score = base_score + total_boost
        item["original_score"] = base_score
        item["score"] = final_score
        item["similarity score"] = final_score
        item["boost_details"] = boost_details
    
    # 重新排序
    results = sorted(results, key=lambda x: x.get("score", 0.0), reverse=True)
    
    return results


def extract_entities_from_query(query: str) -> List[str]:
    """
    从查询中提取实体（简单实现）
    
    使用规则匹配提取可能的实体：
    - 产品模块：SLB, LLB, GSLB
    - 协议类型：HTTP, HTTPS, TCP, UDP
    - 配置类型：虚拟服务, 健康检查, 后端服务器
    
    Args:
        query: 查询字符串
    
    Returns:
        实体列表
    """
    entities = []
    query_lower = query.lower()
    
    # 产品模块
    modules = ["slb", "llb", "gslb", "基础网络", "network"]
    for module in modules:
        if module in query_lower:
            entities.append(module.upper() if len(module) <= 4 else module)
    
    # 协议类型
    protocols = extract_protocols_from_text(query)
    entities.extend(proto.upper() for proto in protocols)
    
    # 配置类型关键词
    config_keywords = [
        ("虚拟服务", "virtual_services"),
        ("后端服务", "backend_servers"),
        ("健康检查", "health_checks"),
        ("会话保持", "session_persistence"),
        ("负载均衡", "load_balancing"),
        ("ssl证书", "ssl_certificate"),
        ("qos", "qos_policy"),
    ]
    for keyword, entity in config_keywords:
        if keyword in query_lower:
            entities.append(entity)
    
    return entities


if __name__ == "__main__":
    # 测试代码
    import json
    
    # 测试协议加权
    query = "如何配置HTTP类型的SLB服务"
    
    test_docs = [
        {
            "text": "配置HTTP虚拟服务，使用slb virtual http命令...",
            "metadata": {"protocol_type": ["HTTP"]},
        },
        {
            "text": "配置HTTPS虚拟服务，需要先导入SSL证书...",
            "metadata": {"protocol_type": ["HTTPS"]},
        },
        {
            "text": "配置TCP虚拟服务，支持任意TCP应用...",
            "metadata": {"protocol_type": ["TCP"]},
        },
    ]
    
    print("测试协议加权:")
    for doc in test_docs:
        boost, details = compute_protocol_boost(query, doc)
        print(f"  文档协议: {details['doc_protocols']}")
        print(f"  加权值: {boost:+.3f}")
        print(f"  匹配: {details['exact_matches']}")
        print()
    
    # 测试实体提取
    entities = extract_entities_from_query(query)
    print(f"提取的实体: {entities}")
    
    # 测试综合评分
    print("\n测试综合评分:")
    scored_docs = apply_scoring_boost(
        test_docs.copy(),
        query,
        use_protocol_boost=True,
    )
    for doc in scored_docs:
        print(f"  分数: {doc['score']:.3f}, 协议加权: {doc.get('protocol_boost', 0):.3f}")
