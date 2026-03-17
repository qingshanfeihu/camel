# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
功能结构索引构建器

自动从多个数据源提取并构建功能结构索引：
1. 从 CLI XML 树自动提取命令层级和模块
2. 从文档结构自动提取功能层级和关键词
3. 从 knowledge_base.json 自动提取实际使用的 metadata 和关键词
4. 自动构建配置场景，不依赖硬编码定义

使用方法:
    python build_function_structure_index.py --help
"""
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

logger = logging.getLogger(__name__)


def extract_command_hierarchy_from_xml(xml_path: Path) -> Dict[str, Any]:
    """
    从 CLI XML 树自动提取命令层级结构，不硬编码任何分类
    
    Returns:
        {
            "modules": {
                "slb": {
                    "prefix": "slb",
                    "commands": ["slb real", "slb group", ...],
                    "subcommands": {
                        "slb real": {...},
                        "slb group": {...}
                    }
                },
                ...
            },
            "command_to_module": {
                "slb real": "slb",
                "llb link": "llb",
                ...
            },
            "command_hierarchy": {
                "slb": ["slb real", "slb group", ...],
                "slb real": ["slb real ip", "slb real http", ...],
                ...
            }
        }
    """
    if not xml_path.exists():
        logger.warning(f"CLI XML 文件不存在: {xml_path}")
        return {}
    
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        logger.error(f"解析 CLI XML 失败: {e}")
        return {}
    
    modules: Dict[str, Dict[str, Any]] = {}
    command_to_module: Dict[str, str] = {}
    command_hierarchy: Dict[str, List[str]] = defaultdict(list)
    all_commands: Set[str] = set()
    
    def _extract_recursive(element: ET.Element, prefix: str = "", depth: int = 0):
        """递归提取命令，构建层级结构"""
        if depth > 15:  # 防止过深递归
            return
        
        # 提取命令文本
        tag = element.tag.lower()
        text = (element.text or "").strip()
        attrib = element.attrib
        cmd_text = text or attrib.get("name", "") or attrib.get("command", "")
        
        if not cmd_text:
            # 如果没有命令文本，尝试从子元素提取
            for child in element:
                _extract_recursive(child, prefix, depth + 1)
            return
        
        # 构建完整命令
        if prefix:
            full_cmd = f"{prefix} {cmd_text}".strip()
        else:
            full_cmd = cmd_text
        
        if full_cmd in all_commands:
            return  # 避免重复
        all_commands.add(full_cmd)
        
        # 自动识别模块（基于命令前缀）
        cmd_parts = full_cmd.split()
        if cmd_parts:
            module_prefix = cmd_parts[0].lower()
            
            # 初始化模块
            if module_prefix not in modules:
                modules[module_prefix] = {
                    "prefix": module_prefix,
                    "commands": [],
                    "subcommands": {}
                }
            
            # 添加到模块
            if full_cmd not in modules[module_prefix]["commands"]:
                modules[module_prefix]["commands"].append(full_cmd)
            
            command_to_module[full_cmd] = module_prefix
            
            # 构建层级关系
            if len(cmd_parts) > 1:
                parent_cmd = " ".join(cmd_parts[:-1])
                if parent_cmd in all_commands:
                    command_hierarchy[parent_cmd].append(full_cmd)
            else:
                # 顶级命令
                command_hierarchy[module_prefix].append(full_cmd)
        
        # 递归处理子元素
        for child in element:
            _extract_recursive(child, full_cmd, depth + 1)
    
    _extract_recursive(root)
    
    # 构建子命令结构
    for module_name, module_data in modules.items():
        for cmd in module_data["commands"]:
            if cmd in command_hierarchy:
                module_data["subcommands"][cmd] = command_hierarchy[cmd]
    
    return {
        "modules": modules,
        "command_to_module": command_to_module,
        "command_hierarchy": dict(command_hierarchy),
        "all_commands": sorted(all_commands)
    }


def extract_document_hierarchy_from_markdown(md_path: Path) -> Dict[str, Any]:
    """
    从 Markdown 文档自动提取标题层级结构，自动分词提取关键词
    
    Returns:
        {
            "sections": [
                {
                    "level": 1,
                    "title": "服务器负载均衡配置",
                    "path": ["服务器负载均衡配置"],
                    "keywords": ["服务器", "负载均衡", "配置"],  # 自动分词
                    "children": [...]
                }
            ],
            "module_keywords": {
                "SLB": ["负载均衡", "slb", ...],  # 自动提取
                "LLB": ["链路", "llb", ...]
            }
        }
    """
    if not md_path.exists():
        logger.warning(f"Markdown 文件不存在: {md_path}")
        return {}
    
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"读取 Markdown 文件失败: {e}")
        return {}
    
    sections = []
    section_stack = []  # 用于构建层级结构
    
    lines = content.split("\n")
    for i, line in enumerate(lines):
        match = re.match(r"^(#+)\s+(.+)$", line.strip())
        if not match:
            continue
        
        level = len(match.group(1))
        title = match.group(2).strip()
        
        # 跳过纯编号标题
        if re.match(r"^\d+\.\d+\.\d+\.?\d*\.?\s*$", title):
            continue
        
        # 自动分词提取关键词（简单的中英文分词）
        keywords = _extract_keywords_from_title(title)
        
        section = {
            "level": level,
            "title": title,
            "keywords": keywords,
            "line_number": i + 1,
            "children": []
        }
        
        # 构建层级结构
        while section_stack and section_stack[-1]["level"] >= level:
            section_stack.pop()
        
        if section_stack:
            section_stack[-1]["children"].append(section)
            section["path"] = section_stack[-1].get("path", []) + [title]
        else:
            section["path"] = [title]
        
        sections.append(section)
        section_stack.append(section)
    
    # 自动提取模块关键词（基于标题中的常见模块名称）
    module_keywords = _extract_module_keywords_from_sections(sections)
    
    return {
        "sections": [s for s in sections if s["level"] == 1],  # 只返回顶级章节
        "module_keywords": module_keywords
    }


def _extract_keywords_from_title(title: str) -> List[str]:
    """从标题自动分词提取关键词"""
    keywords = []
    
    # 中文分词（简单按常见分隔符分割）
    chinese_parts = re.split(r'[、，,。\s]+', title)
    keywords.extend([p.strip() for p in chinese_parts if p.strip() and len(p.strip()) > 1])
    
    # 英文分词
    english_words = re.findall(r'\b[a-zA-Z]+\b', title)
    keywords.extend([w.lower() for w in english_words if len(w) > 2])
    
    # 提取缩写（如 SLB, LLB, GSLB）
    abbreviations = re.findall(r'\b[A-Z]{2,}\b', title)
    keywords.extend([ab.lower() for ab in abbreviations])
    
    return list(set(keywords))  # 去重


def _extract_module_keywords_from_sections(sections: List[Dict]) -> Dict[str, List[str]]:
    """从文档章节自动提取模块关键词"""
    module_keywords: Dict[str, List[str]] = defaultdict(list)
    
    # 从已有索引或 metadata_statistics 中获取模块信息（不硬编码）
    # 如果索引不存在，则基于文档内容自动推断模块名称
    # 这里不硬编码模块缩写，而是基于文档标题和关键词自动识别
    
    for section in sections:
        title_lower = section["title"].lower()
        keywords = section.get("keywords", [])
        
        # 自动识别模块：基于标题和关键词中的常见模式
        # 提取可能的模块名称（大写缩写、中文名称等）
        detected_modules = []
        
        # 检查标题中的大写缩写（如 SLB, LLB, GSLB）
        import re
        abbrevs = re.findall(r'\b[A-Z]{2,}\b', section["title"])
        for abbrev in abbrevs:
            if abbrev not in detected_modules:
                detected_modules.append(abbrev)
        
        # 检查关键词中的模块标识（基于常见模式，但不硬编码具体模块）
        # 这里可以进一步优化，从已有索引中学习模块识别模式
        
        # 为检测到的模块添加关键词
        for module_name in detected_modules:
            if module_name not in module_keywords:
                module_keywords[module_name] = []
            # 添加相关关键词
            for k in keywords:
                if k not in module_keywords[module_name]:
                    module_keywords[module_name].append(k)
    
    return dict(module_keywords)


def extract_metadata_statistics(kb_path: Path) -> Dict[str, Any]:
    """
    从 knowledge_base.json 统计实际使用的 metadata，自动生成关键词映射
    
    Returns:
        {
            "product_modules": {
                "SLB": {
                    "count": 100,
                    "keywords": [...],  # 从实际文本中提取
                    "step_types": {...}  # 该模块使用的步骤类型
                }
            },
            "protocol_types": {...},
            "step_types": {...}
        }
    """
    if not kb_path.exists():
        logger.warning(f"knowledge_base.json 不存在: {kb_path}")
        return {}
    
    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            kb_data = json.load(f)
    except Exception as e:
        logger.error(f"读取 knowledge_base.json 失败: {e}")
        return {}
    
    # 定义协议类型列表（用于过滤）
    PROTOCOL_TYPES = ["HTTP", "HTTPS", "TCP", "UDP", "ICMP", "DNS", "FTP", "SIP", "RTSP", "IP", "IPV4", "IPV6"]
    
    product_modules: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "keywords": set(),
        "step_types": {},  # 改为字典，存储带模块前缀的步骤类型
        "protocol_types": set(),  # 该模块使用的协议类型
        "section_titles": set(),  # 该模块出现的章节标题
        "basic_config_steps": set(),  # 从文档中识别的基本配置步骤（如"定义后台服务"、"定义虚拟服务"等）
    })
    protocol_types: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "keywords": set()
    })
    step_types: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "keywords": set()
    })
    
    chunks = kb_data if isinstance(kb_data, list) else kb_data.get("chunks", [])
    
    for chunk in chunks:
        metadata = chunk.get("metadata", {})
        regex_meta = metadata.get("regex_metadata", {})
        text = chunk.get("page_content", "") or chunk.get("text", "")
        clean_text = metadata.get("clean_text", "") or text
        
        # 识别基本配置步骤说明（例如"配置SLB分以下几个步骤"）
        # 匹配模式：配置XX分以下几个步骤、配置XX包括以下步骤、配置XX的步骤等
        config_steps_pattern = re.compile(
            r'配置\w+分以下[几个]*步骤[：:]?|配置\w+包括以下步骤[：:]?|配置\w+的步骤[：:]?',
            re.IGNORECASE
        )
        is_config_steps_section = bool(config_steps_pattern.search(clean_text))
        
        # 识别基本配置步骤（例如"1. 定义后台服务"、"2. 定义虚拟服务"等）
        # 匹配模式：数字. 定义XX、数字. 配置XX等
        basic_step_pattern = re.compile(
            r'^\s*\d+\.\s*(定义|配置|添加|创建|设置)\s*([^。，,]+)',
            re.MULTILINE
        )
        basic_steps_matches = basic_step_pattern.findall(clean_text)
        
        # 提取 product_module（动态识别，不限制）
        pm = metadata.get("product_module") or regex_meta.get("product_module")
        # 过滤掉协议类型和unknown
        if pm and pm != "unknown" and pm.upper() not in [p.upper() for p in PROTOCOL_TYPES]:
            product_modules[pm]["count"] += 1
            # 从文本中提取关键词
            keywords = _extract_keywords_from_text(text)
            product_modules[pm]["keywords"].update(keywords)
            
            # 如果是在基本配置步骤说明部分，记录这些步骤
            if is_config_steps_section and basic_steps_matches:
                for action, step_name in basic_steps_matches:
                    # 规范化步骤名称（去除多余空格）
                    step_name_clean = step_name.strip()
                    if step_name_clean:
                        product_modules[pm]["basic_config_steps"].add(step_name_clean)
            
            # 统计该模块使用的步骤类型（添加模块前缀）
            st = metadata.get("step_type") or regex_meta.get("step_type")
            if st:
                # 构建带模块前缀的步骤类型
                prefixed_step_type = _build_prefixed_step_type(pm, st)
                if prefixed_step_type not in product_modules[pm]["step_types"]:
                    product_modules[pm]["step_types"][prefixed_step_type] = {
                        "count": 0,
                        "base_step_type": st,
                        "keywords": set(),
                        "is_basic_step": False  # 标记是否是基本配置步骤
                    }
                product_modules[pm]["step_types"][prefixed_step_type]["count"] += 1
                product_modules[pm]["step_types"][prefixed_step_type]["keywords"].update(keywords)
                
                # 检查当前步骤是否匹配基本配置步骤
                step_name_lower = clean_text.lower()
                for basic_step in product_modules[pm]["basic_config_steps"]:
                    if basic_step.lower() in step_name_lower or any(
                        kw in step_name_lower for kw in basic_step.split()
                    ):
                        product_modules[pm]["step_types"][prefixed_step_type]["is_basic_step"] = True
                        break
            
            # 收集协议类型
            pt = metadata.get("protocol_type") or regex_meta.get("protocol_type")
            if pt:
                if isinstance(pt, list):
                    product_modules[pm]["protocol_types"].update([str(p).upper() for p in pt])
                else:
                    product_modules[pm]["protocol_types"].add(str(pt).upper())
            
            # 收集章节标题
            section_title = metadata.get("section_title") or regex_meta.get("section_title")
            if section_title:
                product_modules[pm]["section_titles"].add(section_title)
        
        # 提取 protocol_type
        pt = metadata.get("protocol_type") or regex_meta.get("protocol_type")
        if pt:
            if isinstance(pt, list):
                for p in pt:
                    protocol_types[str(p).upper()]["count"] += 1
                    keywords = _extract_keywords_from_text(text)
                    protocol_types[str(p).upper()]["keywords"].update(keywords)
            else:
                protocol_types[str(pt).upper()]["count"] += 1
                keywords = _extract_keywords_from_text(text)
                protocol_types[str(pt).upper()]["keywords"].update(keywords)
        
        # 提取 step_type（全局统计，不带前缀）
        st = metadata.get("step_type") or regex_meta.get("step_type")
        if st:
            step_types[st]["count"] += 1
            keywords = _extract_keywords_from_text(text)
            step_types[st]["keywords"].update(keywords)
    
    # 转换为列表并限制关键词数量
    def limit_keywords(keyword_set: Set[str], max_count: int = 20) -> List[str]:
        return sorted(list(keyword_set))[:max_count]
    
    # 处理product_modules的step_types，转换为可序列化格式
    processed_product_modules = {}
    for k, v in product_modules.items():
        processed_step_types = {}
        for step_type_key, step_type_data in v["step_types"].items():
            processed_step_types[step_type_key] = {
                "count": step_type_data["count"],
                "base_step_type": step_type_data["base_step_type"],
                "keywords": limit_keywords(step_type_data["keywords"]),
                "is_basic_step": step_type_data.get("is_basic_step", False)  # 保留基本步骤标记
            }
        processed_product_modules[k] = {
            "count": v["count"],
            "keywords": limit_keywords(v["keywords"]),
            "step_types": processed_step_types,
            "basic_config_steps": list(v["basic_config_steps"]),  # 保留基本配置步骤列表
            "protocol_types": sorted(list(v["protocol_types"])),
            "section_titles": sorted(list(v["section_titles"]))[:10]  # 限制数量
        }
    
    result = {
        "product_modules": processed_product_modules,
        "protocol_types": {
            k: {
                "count": v["count"],
                "keywords": limit_keywords(v["keywords"])
            }
            for k, v in protocol_types.items()
        },
        "step_types": {
            k: {
                "count": v["count"],
                "keywords": limit_keywords(v["keywords"])
            }
            for k, v in step_types.items()
        }
    }
    
    return result


def _build_prefixed_step_type(module_name: str, base_step_type: str) -> str:
    """
    构建带模块前缀的步骤类型
    
    Args:
        module_name: 功能模块名称（如"SLB"、"LLB"）
        base_step_type: 基础步骤类型（如"health_checks"）
    
    Returns:
        带模块前缀的步骤类型（如"slb_health_checks"）
    """
    # 规范化模块名称（转小写，处理中文）
    module_normalized = module_name.lower()
    if module_name == "基础网络":
        module_normalized = "network"
    elif module_name == "安全":
        module_normalized = "security"
    elif module_name == "GSLB":
        module_normalized = "gslb"
    elif module_name == "LLB":
        module_normalized = "llb"
    elif module_name == "SLB":
        module_normalized = "slb"
    
    return f"{module_normalized}_{base_step_type}"


def infer_module_hierarchy(
    product_modules_stats: Dict[str, Any],
    cli_hierarchy: Dict[str, Any],
    document_hierarchy: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """
    动态推断模块的层级和依赖关系
    
    策略：
    1. 基于章节标题和内容推断层级（基础层/应用层/高级功能层）
    2. 基于CLI命令前缀推断模块关系
    3. 基于配置示例推断依赖关系
    """
    module_hierarchy = {}
    
    for module_name, module_data in product_modules_stats.items():
        section_titles = module_data.get("section_titles", [])
        keywords = module_data.get("keywords", [])
        cli_commands = cli_hierarchy.get("modules", {}).get(module_name.lower(), {}).get("commands", [])
        
        # 推断层级
        level = "其他"
        order = 999
        
        # 基于关键词和章节标题推断
        title_text = " ".join(section_titles).lower()
        keyword_text = " ".join(keywords).lower()
        combined_text = f"{title_text} {keyword_text}"
        
        if any(kw in combined_text for kw in ["基础", "网络", "路由", "接口", "interface", "route", "ip", "vlan"]):
            level = "基础层"
            order = 1
        elif any(kw in combined_text for kw in ["负载均衡", "slb", "llb", "gslb", "virtual", "backend", "服务器"]):
            level = "应用层"
            order = 2
        elif any(kw in combined_text for kw in ["安全", "firewall", "acl", "ssl", "证书", "认证"]):
            level = "安全层"
            order = 3
        elif any(kw in combined_text for kw in ["高级", "高级功能", "advanced", "qos", "会话", "cookie"]):
            level = "高级功能层"
            order = 4
        
        # 推断依赖关系
        depends_on = []
        if level == "应用层":
            # 应用层通常依赖基础层
            depends_on.append("基础网络")
        elif level == "安全层":
            depends_on.extend(["基础网络", "SLB"])  # 安全通常依赖基础网络和业务模块
        
        module_hierarchy[module_name] = {
            "level": level,
            "order": order,
            "depends_on": depends_on,
        }
    
    return module_hierarchy


def _extract_keywords_from_text(text: str) -> Set[str]:
    """从文本中提取关键词"""
    if not text:
        return set()
    
    keywords = set()
    text_lower = text.lower()
    
    # 提取英文单词（长度 > 2）
    english_words = re.findall(r'\b[a-zA-Z]{3,}\b', text_lower)
    keywords.update(english_words)
    
    # 提取缩写
    abbreviations = re.findall(r'\b[A-Z]{2,}\b', text)
    keywords.update([ab.lower() for ab in abbreviations])
    
    # 提取中文词组（简单提取2-4字词组）
    chinese_phrases = re.findall(r'[\u4e00-\u9fff]{2,4}', text)
    keywords.update(chinese_phrases)
    
    return keywords


def auto_build_scenarios(
    cli_hierarchy: Dict[str, Any],
    doc_hierarchy: Dict[str, Any],
    metadata_stats: Dict[str, Any],
) -> Dict[str, Any]:
    """
    自动构建场景，不依赖硬编码定义
    
    策略：
    1. 从文档中识别包含"配置示例"、"完整配置"等关键词的章节
    2. 从章节标题和内容自动推断模块、协议、步骤类型
    3. 基于 CLI 命令层级和文档结构自动关联
    """
    scenarios = {}
    
    # 从文档章节中识别场景
    # document_hierarchy的结构可能是 {"app": {...}, "cli": {...}} 或者直接是 {"sections": [...]}
    # 需要合并app和cli的sections
    if isinstance(doc_hierarchy, dict):
        if "app" in doc_hierarchy or "cli" in doc_hierarchy:
            app_sections = doc_hierarchy.get("app", {}).get("sections", [])
            cli_sections = doc_hierarchy.get("cli", {}).get("sections", [])
            sections = app_sections + cli_sections
        else:
            # 如果直接是sections结构
            sections = doc_hierarchy.get("sections", [])
    else:
        sections = []
    
    # 合并module_keywords（优先使用app的）
    if isinstance(doc_hierarchy, dict):
        if "app" in doc_hierarchy or "cli" in doc_hierarchy:
            app_module_keywords = doc_hierarchy.get("app", {}).get("module_keywords", {})
            cli_module_keywords = doc_hierarchy.get("cli", {}).get("module_keywords", {})
            module_keywords = {**cli_module_keywords, **app_module_keywords}  # app优先
        else:
            module_keywords = doc_hierarchy.get("module_keywords", {})
    else:
        module_keywords = {}
    
    # 如果仍然没有module_keywords，从metadata_stats中提取
    if not module_keywords:
        product_modules_stats = metadata_stats.get("product_modules", {})
        module_keywords = {
            module: stats.get("keywords", [])
            for module, stats in product_modules_stats.items()
        }
    
    # 扩展的场景识别关键词
    scenario_keywords = ["配置示例", "完整配置", "configuration example", "full config", "配置步骤", "负载均衡配置", "配置"]
    
    # 从metadata_stats中提取协议类型统计
    protocol_stats = metadata_stats.get("protocol_types", {})
    protocol_keywords = list(protocol_stats.keys()) if protocol_stats else []
    
    # 如果sections为空，尝试从knowledge_base中基于section_title和metadata生成场景
    if not sections:
        logger.warning("文档章节为空，尝试从knowledge_base.json中生成场景")
        scenarios = _build_scenarios_from_knowledge_base(metadata_stats)
        if scenarios:
            logger.info(f"从knowledge_base.json生成了 {len(scenarios)} 个场景")
            return scenarios
    
    for section in sections:
        title_lower = section["title"].lower()
        
        # 方法1: 检查是否包含明确的场景关键词
        has_scenario_keyword = any(kw in title_lower for kw in scenario_keywords)
        
        # 方法2: 检查是否包含协议类型 + 功能模块 + 配置相关关键词
        # 例如："HTTP/TCP/FTP/UDP/HTTPS/TCPS/DNS协议的负载均衡配置"
        has_protocol = any(protocol.lower() in title_lower for protocol in protocol_keywords)
        has_module = any(any(kw in title_lower for kw in keywords) for keywords in module_keywords.values())
        has_config_keyword = any(kw in title_lower for kw in ["配置", "configure", "setup", "设置", "负载均衡", "load balance"])
        
        # 判断是否是场景章节
        is_scenario = has_scenario_keyword or (has_protocol and has_module and has_config_keyword)
        
        if is_scenario:
            # 自动推断模块（只包含功能模块，不包含协议类型）
            PROTOCOL_TYPES = ["HTTP", "HTTPS", "TCP", "UDP", "ICMP", "DNS", "FTP", "SIP", "RTSP", "IP", "IPV4", "IPV6"]
            detected_modules = []
            for module_name, keywords in module_keywords.items():
                # 过滤掉协议类型和unknown
                if (module_name != "unknown" and 
                    module_name.upper() not in [p.upper() for p in PROTOCOL_TYPES] and
                    any(kw in title_lower for kw in keywords)):
                    detected_modules.append(module_name)
            
            # 自动推断协议类型（单独列出）
            detected_protocols = []
            protocol_stats = metadata_stats.get("protocol_types", {})
            for protocol, stats in protocol_stats.items():
                keywords = stats.get("keywords", [])
                if any(kw in title_lower for kw in keywords):
                    detected_protocols.append(protocol)
            
            # 自动推断步骤类型（基于子章节和章节标题）
            detected_steps = []
            step_stats = metadata_stats.get("step_types", {})
            
            # 方法1: 从章节标题中提取步骤类型
            for step_type, stats in step_stats.items():
                keywords = stats.get("keywords", [])
                if any(kw in title_lower for kw in keywords):
                    if step_type not in detected_steps:
                        detected_steps.append(step_type)
            
            # 方法2: 从子章节标题中提取步骤类型
            def _extract_steps_from_children(s: Dict):
                for child in s.get("children", []):
                    child_title_lower = child["title"].lower()
                    for step_type, stats in step_stats.items():
                        keywords = stats.get("keywords", [])
                        if any(kw in child_title_lower for kw in keywords):
                            if step_type not in detected_steps:
                                detected_steps.append(step_type)
                    _extract_steps_from_children(child)
            
            _extract_steps_from_children(section)
            
            # 方法3: 如果仍然为空，基于协议类型和模块类型推断通用步骤类型
            if not detected_steps:
                detected_steps = _infer_default_steps(
                    detected_modules,
                    detected_protocols,
                    metadata_stats
                )
            
            # 生成场景 ID
            if detected_modules:
                module_abbrev = detected_modules[0].upper() if len(detected_modules[0]) <= 4 else detected_modules[0][:4].upper()
                protocol_abbrev = detected_protocols[0].upper() if detected_protocols else "FULL"
                scenario_id = f"{module_abbrev}_{protocol_abbrev}_FULL_CONFIG"
                
                # 将步骤类型转换为带模块前缀的格式
                prefixed_steps = []
                for step_type in detected_steps:
                    # 为每个模块生成带前缀的步骤类型
                    for module in detected_modules:
                        prefixed_step = _build_prefixed_step_type(module, step_type)
                        prefixed_steps.append(prefixed_step)
                
                scenarios[scenario_id] = {
                    "scenario_id": scenario_id,
                    "product_modules": detected_modules,  # 只包含功能模块，不包含协议类型
                    "protocol_types": detected_protocols,  # 协议类型单独列出
                    "required_steps": prefixed_steps,  # 使用带模块前缀的步骤类型
                    "source_section": section["title"],
                    "cli_commands": {}  # 后续从 CLI 层级关联
                }
    
    return scenarios


def _infer_default_steps(
    detected_modules: List[str],
    detected_protocols: List[str],
    metadata_stats: Dict[str, Any]
) -> List[str]:
    """
    基于协议类型和模块类型推断通用步骤类型
    
    如果无法从章节中提取步骤类型，则基于模块和协议的常见步骤类型组合推断
    """
    inferred_steps = []
    
    # 从metadata_statistics中查找该模块的常见步骤类型
    product_modules_stats = metadata_stats.get("product_modules", {})
    
    for module in detected_modules:
        module_stats = product_modules_stats.get(module, {})
        step_types = module_stats.get("step_types", {})
        
        if step_types:
            # 选择最常见的步骤类型（按使用频率排序）
            # step_types的结构是: {prefixed_step_type: {count: int, base_step_type: str, ...}}
            sorted_steps = sorted(
                step_types.items(), 
                key=lambda x: x[1].get("count", 0) if isinstance(x[1], dict) else x[1], 
                reverse=True
            )
            # 取前3个最常见的步骤类型（已经是带前缀的格式）
            top_steps = [step for step, step_info in sorted_steps[:3]]
            inferred_steps.extend(top_steps)
    
    # 如果没有找到，使用通用的步骤类型
    if not inferred_steps:
        # 基于模块类型推断通用步骤
        if "SLB" in detected_modules:
            # SLB通常需要这些步骤
            inferred_steps = ["virtual_services", "backend_servers", "health_checks"]
        elif "LLB" in detected_modules:
            # LLB通常需要这些步骤
            inferred_steps = ["network_basics", "health_checks"]
        else:
            # 默认步骤类型
            inferred_steps = ["network_basics"]
    
    return list(set(inferred_steps))  # 去重


def _build_scenarios_from_knowledge_base(metadata_stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    从knowledge_base.json的metadata统计中生成场景
    
    当文档章节结构不可用时，基于metadata统计生成常用场景
    """
    scenarios = {}
    
    product_modules_stats = metadata_stats.get("product_modules", {})
    protocol_stats = metadata_stats.get("protocol_types", {})
    step_stats = metadata_stats.get("step_types", {})
    
    # 为每个产品模块和协议类型组合生成场景
    for module_name, module_data in product_modules_stats.items():
        if module_name in ["SLB", "GSLB", "LLB"]:
            # 获取该模块常用的协议类型
            module_protocols = []
            for protocol, protocol_data in protocol_stats.items():
                # 检查该协议是否在该模块的文档中出现
                if protocol in ["HTTP", "HTTPS", "TCP", "UDP", "FTP", "SIP", "DNS"]:
                    module_protocols.append(protocol)
            
            # 为每个协议类型生成场景
            for protocol in module_protocols[:5]:  # 限制为前5个协议
                scenario_id = f"{module_name}_{protocol}_CONFIG"
                
                # 获取该模块的常用步骤类型（已经是带模块前缀的格式）
                step_types = module_data.get("step_types", {})
                if step_types:
                    # 选择最常见的步骤类型（已经是带前缀的格式）
                    sorted_steps = sorted(step_types.items(), key=lambda x: x[1].get("count", 0) if isinstance(x[1], dict) else x[1], reverse=True)
                    required_steps = [step for step, step_info in sorted_steps[:5]]
                else:
                    # 使用默认步骤类型，并添加模块前缀
                    if module_name == "SLB":
                        base_steps = ["virtual_services", "backend_servers", "health_checks"]
                    elif module_name == "LLB":
                        base_steps = ["network_basics", "health_checks"]
                    else:
                        base_steps = ["network_basics"]
                    required_steps = [_build_prefixed_step_type(module_name, step) for step in base_steps]
                
                scenarios[scenario_id] = {
                    "scenario_id": scenario_id,
                    "product_modules": [module_name],  # 只包含功能模块
                    "protocol_types": [protocol],  # 协议类型单独列出
                    "required_steps": required_steps,  # 使用带模块前缀的步骤类型
                    "source": "auto_generated_from_metadata",
                }
    
    return scenarios


def build_function_index(
    cli_xml_path: Optional[Path] = None,
    app_md_path: Optional[Path] = None,
    cli_md_path: Optional[Path] = None,
    kb_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    构建功能结构索引：完全自动提取，不硬编码
    
    Args:
        cli_xml_path: CLI XML 树文件路径
        app_md_path: APP 手册 Markdown 路径
        cli_md_path: CLI 手册 Markdown 路径
        kb_path: knowledge_base.json 路径
    
    Returns:
        Dict: 功能结构索引
    """
    logger.info("开始自动构建功能结构索引...")
    
    # 1. 从 CLI XML 提取命令层级
    cli_hierarchy = {}
    if cli_xml_path and cli_xml_path.exists():
        logger.info(f"从 CLI XML 提取命令层级: {cli_xml_path}")
        cli_hierarchy = extract_command_hierarchy_from_xml(cli_xml_path)
        logger.info(f"提取到 {len(cli_hierarchy.get('all_commands', []))} 个命令")
    
    # 2. 从文档提取结构
    app_hierarchy = {}
    if app_md_path and app_md_path.exists():
        logger.info(f"从 APP 文档提取结构: {app_md_path}")
        app_hierarchy = extract_document_hierarchy_from_markdown(app_md_path)
    
    cli_doc_hierarchy = {}
    if cli_md_path and cli_md_path.exists():
        logger.info(f"从 CLI 文档提取结构: {cli_md_path}")
        cli_doc_hierarchy = extract_document_hierarchy_from_markdown(cli_md_path)
    
    # 3. 从 knowledge_base.json 提取 metadata 统计
    metadata_stats = {}
    if kb_path and kb_path.exists():
        logger.info(f"从 knowledge_base.json 提取 metadata 统计: {kb_path}")
        metadata_stats = extract_metadata_statistics(kb_path)
        logger.info(f"统计到 {len(metadata_stats.get('product_modules', {}))} 个产品模块")
    
    # 4. 自动构建场景
    # 合并app和cli的hierarchy
    document_hierarchy = {
        "app": app_hierarchy,
        "cli": cli_doc_hierarchy
    }
    
    scenarios = auto_build_scenarios(
        cli_hierarchy,
        document_hierarchy,
        metadata_stats,
    )
    logger.info(f"自动构建了 {len(scenarios)} 个场景")
    
    # 5. 推断模块层级和依赖关系
    product_modules_stats = metadata_stats.get("product_modules", {})
    module_hierarchy = infer_module_hierarchy(
        product_modules_stats,
        cli_hierarchy,
        document_hierarchy
    )
    logger.info(f"推断了 {len(module_hierarchy)} 个模块的层级关系")
    
    # 6. 构建最终索引
    modules = {}
    for module_name in product_modules_stats.keys():
        module_data = product_modules_stats[module_name]
        hierarchy_info = module_hierarchy.get(module_name, {
            "level": "其他",
            "order": 999,
            "depends_on": []
        })
        
        # 处理step_types（已经是带模块前缀的格式）
        step_types_dict = {}
        for prefixed_step_type, step_info in module_data.get("step_types", {}).items():
            step_types_dict[prefixed_step_type] = {
                "count": step_info.get("count", 0),
                "base_step_type": step_info.get("base_step_type", ""),
                "keywords": step_info.get("keywords", []),
                "is_advanced": _is_advanced_feature(
                    step_info.get("base_step_type", ""), 
                    module_data.get("keywords", []),
                    is_basic_step=step_info.get("is_basic_step", False),
                    module_basic_steps=module_data.get("basic_config_steps", [])
                ),
                "order": _get_step_order(step_info.get("base_step_type", ""), hierarchy_info.get("level", "其他"))
            }
        
        modules[module_name] = {
            "module_name": module_name,
            "level": hierarchy_info.get("level", "其他"),
            "order": hierarchy_info.get("order", 999),
            "depends_on": hierarchy_info.get("depends_on", []),
            "keywords": module_data.get("keywords", []),
            "step_types": step_types_dict,
            "protocol_types": module_data.get("protocol_types", []),
            "cli_commands": cli_hierarchy.get("modules", {}).get(module_name.lower(), {}).get("commands", []),
            "basic_config_steps": module_data.get("basic_config_steps", [])  # 保留基本配置步骤列表
        }
    
    index = {
        "version": "3.0",
        "source": "auto_extracted",
        "cli_hierarchy": cli_hierarchy,
        "document_hierarchy": {
            "app": app_hierarchy,
            "cli": cli_doc_hierarchy
        },
        "metadata_statistics": metadata_stats,
        "scenarios": scenarios,
        "modules": modules
    }
    
    return index


def _is_advanced_feature(
    base_step_type: str, 
    keywords: List[str], 
    is_basic_step: bool = False,
    module_basic_steps: List[str] = None
) -> bool:
    """
    判断步骤类型是否是高级功能
    
    策略：
    1. 如果is_basic_step为True，则不是高级功能
    2. 如果步骤名称或关键词包含高级功能关键词，则是高级功能
    3. 如果步骤不在模块的基本配置步骤列表中，且包含高级功能关键词，则是高级功能
    """
    # 如果明确标记为基本步骤，则不是高级功能
    if is_basic_step:
        return False
    
    advanced_keywords = ["cookie", "persistence", "qos", "session", "affinity", "高级", "advanced", "优化", "enhancement"]
    step_type_lower = base_step_type.lower()
    keywords_lower = " ".join([k.lower() for k in keywords])
    
    # 检查是否包含高级功能关键词
    if any(kw in step_type_lower for kw in advanced_keywords):
        return True
    if any(kw in keywords_lower for kw in advanced_keywords):
        return True
    
    # 如果模块有基本配置步骤列表，且当前步骤不在其中，可能是高级功能
    if module_basic_steps:
        step_name_matched = False
        for basic_step in module_basic_steps:
            if basic_step.lower() in step_type_lower or basic_step.lower() in keywords_lower:
                step_name_matched = True
                break
        # 如果不在基本步骤列表中，且包含高级功能关键词，则认为是高级功能
        if not step_name_matched and any(kw in keywords_lower for kw in advanced_keywords):
            return True
    
    return False


def _get_step_order(base_step_type: str, module_level: str) -> int:
    """
    获取步骤类型的配置顺序
    
    基于模块层级和步骤类型名称推断
    """
    # 基础配置步骤通常在前
    basic_steps = ["network_basics", "basic_config", "routing_config", "virtual_services", "backend_servers"]
    # 高级功能步骤通常在后
    advanced_steps = ["cookie_persistence", "qos", "session_affinity", "load_balancing_algorithm"]
    
    step_type_lower = base_step_type.lower()
    
    if any(bs in step_type_lower for bs in basic_steps):
        return 1
    elif any(as_step in step_type_lower for as_step in advanced_steps):
        return 5
    elif "health" in step_type_lower:
        return 3
    else:
        return 2


def main():
    """主函数：自动构建功能结构索引"""
    import argparse
    
    parser = argparse.ArgumentParser(description="构建功能结构索引")
    parser.add_argument(
        "--cli-xml",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "command_tree-Beta_APV_10_5_0_73.xml",
        help="CLI XML 树文件路径"
    )
    parser.add_argument(
        "--app-md",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "mineru_output" / "app" / "hybrid_auto" / "app.md",
        help="APP 手册 Markdown 路径"
    )
    parser.add_argument(
        "--cli-md",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "mineru_output" / "cli" / "hybrid_auto" / "cli.md",
        help="CLI 手册 Markdown 路径"
    )
    parser.add_argument(
        "--kb",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "reference" / "knowledge_base.json",
        help="knowledge_base.json 路径"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "function_structure_index.json",
        help="输出 JSON 文件路径"
    )
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    index = build_function_index(
        cli_xml_path=args.cli_xml if args.cli_xml.exists() else None,
        app_md_path=args.app_md if args.app_md.exists() else None,
        cli_md_path=args.cli_md if args.cli_md.exists() else None,
        kb_path=args.kb if args.kb.exists() else None,
    )
    
    # 保存索引
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    
    logger.info(f"功能结构索引已保存到: {args.output}")
    logger.info(f"共识别 {len(index.get('scenarios', {}))} 个场景")
    logger.info(f"共识别 {len(index.get('modules', {}))} 个模块")


if __name__ == "__main__":
    main()
