#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI关键字提取和图谱生成脚本

从CLI PDF转换的JSON和command tree中提取CLI关键字，生成对应关系图谱。

功能：
1. 从cli_content_list.json中提取CLI命令和描述
2. 从command_tree-Beta_APV_10_5_0_73.xml中提取命令层级结构
3. 提取CLI关键字和命令结构
4. 生成对应关系图谱（JSON格式）
5. 可视化图谱（可选）

使用方法：
    python extract_cli_keywords_and_graph.py --help
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
import argparse

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding='utf-8'
)
logger = logging.getLogger(__name__)


def extract_cli_commands_from_json(json_path: Path) -> Dict[str, Any]:
    """
    从cli_content_list.json中提取CLI命令和描述
    
    Args:
        json_path: cli_content_list.json文件路径
        
    Returns:
        {
            "commands": [
                {
                    "text": "show ip address",
                    "type": "text",
                    "page_idx": 10,
                    "description": "显示IP地址配置",
                    "keywords": ["show", "ip", "address"]
                },
                ...
            ],
            "command_patterns": {
                "show": ["show ip address", "show system status", ...],
                "slb": ["slb virtual", "slb real", ...],
                "ip": ["ip address", "ip dhcp", ...]
            },
            "total_commands": 123
        }
    """
    if not json_path.exists():
        logger.warning(f"CLI JSON文件不存在: {json_path}")
        return {"commands": [], "command_patterns": {}, "total_commands": 0}
    
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"读取CLI JSON文件失败: {e}")
        return {"commands": [], "command_patterns": {}, "total_commands": 0}
    
    commands = []
    command_patterns = defaultdict(list)
    
    # CLI命令模式识别
    cli_patterns = [
        r'^\s*(show|no|clear|slb|ip|interface|http2|ssl|ha|vrrp|nat|llb|aaa|webui|webagent|webclassify|webwall|write|system|health|real|virtual|group|policy|method|port|address|dhcp|arp|statistics|tune|warning|openport|tech|ipmi|status)\s+',
        r'^\s*[a-zA-Z]+\s+\w+\s*[<\[].*[>\]]',  # 包含参数的命令
        r'^\s*Demo\(config\)#',  # 配置示例
    ]
    
    for item in data:
        if isinstance(item, dict) and 'text' in item:
            text = item.get('text', '').strip()
            if not text:
                continue
            
            # 检查是否是CLI命令
            is_cli_command = False
            for pattern in cli_patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    is_cli_command = True
                    break
            
            if is_cli_command:
                # 清理命令文本
                clean_text = re.sub(r'^\s*Demo\(config\)#\s*', '', text)
                clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                
                # 提取命令前缀
                cmd_parts = clean_text.split()
                if cmd_parts:
                    # 提取基础命令（去除参数）
                    base_cmd_parts = []
                    for part in cmd_parts:
                        if part.startswith('<') or part.startswith('[') or part.startswith('{'):
                            break
                        base_cmd_parts.append(part)
                    
                    if base_cmd_parts:
                        base_cmd = ' '.join(base_cmd_parts)
                        # 添加到命令列表
                        command_entry = {
                            "text": clean_text,
                            "base_command": base_cmd,
                            "type": item.get('type', 'text'),
                            "page_idx": item.get('page_idx', 0),
                            "bbox": item.get('bbox', []),
                            "keywords": extract_keywords_from_command(clean_text)
                        }
                        commands.append(command_entry)
                        
                        # 添加到命令模式
                        first_word = base_cmd_parts[0].lower()
                        command_patterns[first_word].append(base_cmd)
    
    # 去重和排序
    unique_commands = []
    seen = set()
    for cmd in commands:
        key = cmd["base_command"]
        if key not in seen:
            seen.add(key)
            unique_commands.append(cmd)
    
    # 按命令字母排序
    unique_commands.sort(key=lambda x: x["base_command"])
    
    # 排序命令模式
    for key in command_patterns:
        command_patterns[key] = sorted(set(command_patterns[key]))
    
    return {
        "commands": unique_commands,
        "command_patterns": dict(command_patterns),
        "total_commands": len(unique_commands)
    }


def extract_keywords_from_command(command: str) -> List[str]:
    """从CLI命令中提取关键词"""
    keywords = []
    
    # 提取命令部分
    cmd_parts = command.split()
    for part in cmd_parts:
        # 跳过参数标记
        if part.startswith('<') or part.startswith('[') or part.startswith('{'):
            continue
        # 跳过常见连接词
        if part.lower() in ['and', 'or', 'the', 'a', 'an', 'to', 'for', 'in', 'on', 'at', 'by']:
            continue
        # 添加有意义的部分
        if len(part) > 1:
            keywords.append(part.lower())
    
    # 提取协议类型
    protocol_patterns = [
        r'\b(tcp|udp|http|https|http2|icmp|ssl|tls|ftp|smtp|dns|dhcp|arp|ip|ipv4|ipv6)\b',
        r'\b(health|check|monitor|status|statistics)\b',
        r'\b(virtual|real|server|service|group|pool|policy|method)\b',
        r'\b(show|no|clear|interface|port|address|config|configure)\b'
    ]
    
    for pattern in protocol_patterns:
        matches = re.findall(pattern, command, re.IGNORECASE)
        keywords.extend([m.lower() for m in matches])
    
    return list(set(keywords))


def extract_command_hierarchy_from_xml(xml_path: Path) -> Dict[str, Any]:
    """
    从command tree XML中提取命令层级结构
    
    特殊处理：no, show, clear 是命令操作类型，不是功能模块
    
    Args:
        xml_path: command tree XML文件路径
        
    Returns:
        {
            "modules": {
                "slb": {
                    "prefix": "slb",
                    "commands": ["slb real", "slb virtual", ...],
                    "subcommands": {
                        "slb real": ["slb real ip", "slb real port", ...],
                        "slb virtual": ["slb virtual http", "slb virtual https", ...]
                    },
                    "help_string": "SLB commands"
                },
                ...
            },
            "command_to_module": {
                "slb real": "slb",
                "slb virtual": "slb",
                ...
            },
            "command_hierarchy": {
                "slb": ["slb real", "slb virtual", ...],
                "slb real": ["slb real ip", "slb real port", ...],
                ...
            },
            "all_commands": ["slb real", "slb virtual", ...],
            "operation_types": {
                "no": ["no slb", "no ip", ...],
                "show": ["show slb", "show ip", ...],
                "clear": ["clear slb", "clear ip", ...]
            }
        }
    """
    if not xml_path.exists():
        logger.warning(f"Command tree XML文件不存在: {xml_path}")
        return {}
    
    try:
        # 解析XML
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        logger.error(f"解析Command tree XML失败: {e}")
        return {}
    
    modules: Dict[str, Dict[str, Any]] = {}
    command_to_module: Dict[str, str] = {}
    command_hierarchy: Dict[str, List[str]] = defaultdict(list)
    all_commands: Set[str] = set()
    operation_types: Dict[str, List[str]] = defaultdict(list)
    
    # 定义特殊操作类型
    SPECIAL_OPERATIONS = {"no", "show", "clear"}
    
    def extract_from_element(element: ET.Element, parent_path: str = "") -> None:
        """递归提取命令元素"""
        # 获取元素属性
        tag = element.tag
        name = element.get('name', '')
        help_string = element.get('help_string', '')
        
        # 构建当前路径
        current_path = f"{parent_path} {name}".strip() if parent_path else name
        
        if name and tag in ['menu', 'item']:
            # 添加到命令列表
            if current_path and current_path not in all_commands:
                all_commands.add(current_path)
                
                # 识别模块（跳过特殊操作类型）
                parts = current_path.split()
                if parts:
                    first_word = parts[0].lower()
                    
                    # 如果是特殊操作类型，单独处理
                    if first_word in SPECIAL_OPERATIONS:
                        # 添加到操作类型
                        operation_types[first_word].append(current_path)
                        
                        # 尝试提取实际的功能模块（第二个词）
                        if len(parts) > 1:
                            # 尝试识别功能模块
                            for i in range(1, min(3, len(parts))):
                                potential_module = parts[i].lower()
                                # 检查是否是有效的功能模块（不是参数）
                                if (potential_module not in SPECIAL_OPERATIONS and 
                                    not potential_module.startswith(('<', '[', '{')) and
                                    len(potential_module) > 1):
                                    # 这里不添加到modules，因为操作类型不是模块
                                    break
                    else:
                        # 正常的功能模块
                        module_prefix = first_word
                        
                        # 初始化模块
                        if module_prefix not in modules:
                            modules[module_prefix] = {
                                "prefix": module_prefix,
                                "commands": [],
                                "subcommands": {},
                                "help_string": ""
                            }
                        
                        # 添加到模块
                        if current_path not in modules[module_prefix]["commands"]:
                            modules[module_prefix]["commands"].append(current_path)
                        
                        command_to_module[current_path] = module_prefix
                        
                        # 构建层级关系
                        if len(parts) > 1:
                            parent_cmd = " ".join(parts[:-1])
                            if parent_cmd in all_commands:
                                command_hierarchy[parent_cmd].append(current_path)
                        else:
                            # 顶级命令
                            command_hierarchy[module_prefix].append(current_path)
                        
                        # 更新帮助信息
                        if help_string and not modules[module_prefix]["help_string"]:
                            modules[module_prefix]["help_string"] = help_string
        
        # 递归处理子元素
        for child in element:
            extract_from_element(child, current_path)
    
    # 开始提取
    extract_from_element(root)
    
    # 构建子命令结构
    for module_name, module_data in modules.items():
        for cmd in module_data["commands"]:
            if cmd in command_hierarchy:
                module_data["subcommands"][cmd] = command_hierarchy[cmd]
    
    # 排序
    for module_name in modules:
        modules[module_name]["commands"] = sorted(modules[module_name]["commands"])
        for cmd in modules[module_name]["subcommands"]:
            modules[module_name]["subcommands"][cmd] = sorted(modules[module_name]["subcommands"][cmd])
    
    # 排序操作类型
    for op_type in operation_types:
        operation_types[op_type] = sorted(set(operation_types[op_type]))
    
    return {
        "modules": modules,
        "command_to_module": command_to_module,
        "command_hierarchy": dict(command_hierarchy),
        "all_commands": sorted(all_commands),
        "operation_types": dict(operation_types)
    }


def build_cli_keyword_graph(
    json_commands: Dict[str, Any],
    xml_hierarchy: Dict[str, Any]
) -> Dict[str, Any]:
    """
    构建CLI关键字关系图谱
    
    特殊处理：no, show, clear 是命令操作类型，不是功能模块
    
    Args:
        json_commands: 从JSON提取的命令数据
        xml_hierarchy: 从XML提取的层级数据
        
    Returns:
        {
            "nodes": [
                {
                    "id": "slb",
                    "type": "module",
                    "label": "SLB",
                    "size": 50,
                    "commands_count": 25,
                    "keywords": ["slb", "load", "balance", "server"]
                },
                {
                    "id": "slb_virtual",
                    "type": "command",
                    "label": "slb virtual",
                    "size": 30,
                    "parent": "slb",
                    "description": "配置虚拟服务器",
                    "keywords": ["virtual", "server", "service"]
                },
                {
                    "id": "show",
                    "type": "operation",
                    "label": "show",
                    "size": 40,
                    "commands_count": 100,
                    "description": "显示命令"
                },
                ...
            ],
            "edges": [
                {
                    "source": "slb",
                    "target": "slb_virtual",
                    "type": "contains",
                    "weight": 1.0
                },
                {
                    "source": "show",
                    "target": "show_slb",
                    "type": "operation_of",
                    "weight": 0.9
                },
                ...
            ],
            "keywords_index": {
                "slb": ["slb", "slb_virtual", "slb_real", ...],
                "http": ["slb_virtual_http", "health_check_http", ...],
                "show": ["show_slb", "show_ip", "show_system", ...]
            },
            "statistics": {
                "total_modules": 10,
                "total_operations": 3,
                "total_commands": 150,
                "total_keywords": 500,
                "total_relationships": 300
            }
        }
    """
    nodes = []
    edges = []
    keywords_index = defaultdict(list)
    
    # 定义特殊操作类型
    SPECIAL_OPERATIONS = {"no", "show", "clear"}
    
    # 1. 添加操作类型节点
    operation_types = xml_hierarchy.get("operation_types", {})
    for op_type, op_commands in operation_types.items():
        # 创建操作类型节点
        op_node = {
            "id": op_type,
            "type": "operation",
            "label": op_type,
            "size": max(30, min(100, len(op_commands) // 10)),
            "commands_count": len(op_commands),
            "description": f"{op_type}命令操作类型",
            "keywords": [op_type]
        }
        nodes.append(op_node)
        
        # 添加到关键词索引
        keywords_index[op_type].append(op_type)
    
    # 2. 添加模块节点（来自XML，跳过特殊操作类型）
    modules = xml_hierarchy.get("modules", {})
    for module_name, module_data in modules.items():
        # 跳过特殊操作类型（它们不是真正的模块）
        if module_name in SPECIAL_OPERATIONS:
            continue
            
        # 计算命令数量
        commands_count = len(module_data.get("commands", []))
        
        # 提取模块关键词
        module_keywords = extract_module_keywords(module_name, module_data)
        
        # 创建模块节点
        module_node = {
            "id": module_name,
            "type": "module",
            "label": module_name.upper(),
            "size": max(20, min(100, commands_count * 2)),
            "commands_count": commands_count,
            "keywords": module_keywords,
            "help_string": module_data.get("help_string", "")
        }
        nodes.append(module_node)
        
        # 添加到关键词索引
        for kw in module_keywords:
            keywords_index[kw].append(module_name)
    
    # 3. 添加命令节点（结合JSON和XML）
    json_cmds = json_commands.get("commands", [])
    xml_cmds = xml_hierarchy.get("all_commands", [])
    
    # 合并命令
    all_commands_set = set()
    for cmd in json_cmds:
        base_cmd = cmd.get("base_command", "")
        if base_cmd:
            all_commands_set.add(base_cmd)
    
    for cmd in xml_cmds:
        all_commands_set.add(cmd)
    
    # 创建命令节点
    for cmd in sorted(all_commands_set):
        # 查找命令信息
        cmd_info = None
        for json_cmd in json_cmds:
            if json_cmd.get("base_command", "") == cmd:
                cmd_info = json_cmd
                break
        
        # 确定命令类型
        parts = cmd.split()
        if not parts:
            continue
            
        first_word = parts[0].lower()
        
        # 如果是操作类型命令
        if first_word in SPECIAL_OPERATIONS:
            cmd_type = "operation_command"
            parent = first_word  # 父节点是操作类型
            
            # 尝试提取实际的功能模块
            actual_module = ""
            for i in range(1, min(3, len(parts))):
                potential_module = parts[i].lower()
                if (potential_module not in SPECIAL_OPERATIONS and 
                    not potential_module.startswith(('<', '[', '{')) and
                    len(potential_module) > 1):
                    actual_module = potential_module
                    break
        else:
            cmd_type = "command"
            parent = xml_hierarchy.get("command_to_module", {}).get(cmd, "")
            if not parent:
                parent = first_word
        
        # 提取命令关键词
        if cmd_info:
            cmd_keywords = cmd_info.get("keywords", [])
            description = cmd_info.get("text", "")
        else:
            cmd_keywords = extract_keywords_from_command(cmd)
            description = cmd
        
        # 创建命令节点
        cmd_node = {
            "id": cmd.replace(" ", "_"),
            "type": cmd_type,
            "label": cmd,
            "size": max(10, min(50, len(cmd.split()) * 5)),
            "parent": parent,
            "description": description[:100],  # 截断描述
            "keywords": cmd_keywords,
            "actual_module": actual_module if cmd_type == "operation_command" else ""
        }
        nodes.append(cmd_node)
        
        # 添加到关键词索引
        for kw in cmd_keywords:
            keywords_index[kw].append(cmd.replace(" ", "_"))
        
        # 添加关系边
        if parent:
            if cmd_type == "operation_command":
                # 操作类型-命令边
                edges.append({
                    "source": parent,
                    "target": cmd_node["id"],
                    "type": "operation_of",
                    "weight": 0.9
                })
                
                # 如果找到了实际模块，添加模块-操作命令边
                if actual_module and actual_module in modules:
                    edges.append({
                        "source": actual_module,
                        "target": cmd_node["id"],
                        "type": "has_operation",
                        "operation": first_word,
                        "weight": 0.7
                    })
            else:
                # 模块-命令边
                edges.append({
                    "source": parent,
                    "target": cmd_node["id"],
                    "type": "contains",
                    "weight": 1.0
                })
    
    # 4. 添加命令层级关系（来自XML）
    hierarchy = xml_hierarchy.get("command_hierarchy", {})
    for parent_cmd, child_cmds in hierarchy.items():
        parent_id = parent_cmd.replace(" ", "_")
        for child_cmd in child_cmds:
            child_id = child_cmd.replace(" ", "_")
            edges.append({
                "source": parent_id,
                "target": child_id,
                "type": "parent_of",
                "weight": 0.8
            })
    
    # 5. 添加关键词关系（限制数量，避免边过多）
    keyword_to_nodes = defaultdict(list)
    for node in nodes:
        if "keywords" in node:
            for kw in node["keywords"][:5]:  # 只取前5个关键词
                keyword_to_nodes[kw].append(node["id"])
    
    # 为共享关键词的节点添加边（限制每个关键词最多10个关系）
    for kw, node_ids in keyword_to_nodes.items():
        if len(node_ids) > 1:
            # 限制关系数量
            limited_ids = node_ids[:10]
            for i in range(len(limited_ids)):
                for j in range(i + 1, len(limited_ids)):
                    edges.append({
                        "source": limited_ids[i],
                        "target": limited_ids[j],
                        "type": "shares_keyword",
                        "keyword": kw,
                        "weight": 0.5
                    })
    
    # 6. 统计信息
    total_modules = len([m for m in modules.keys() if m not in SPECIAL_OPERATIONS])
    total_operations = len(operation_types)
    total_commands = len(all_commands_set)
    
    statistics = {
        "total_modules": total_modules,
        "total_operations": total_operations,
        "total_commands": total_commands,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "total_keywords": len(keywords_index),
        "json_commands_count": len(json_cmds),
        "xml_commands_count": len(xml_cmds),
        "operation_counts": {op: len(cmds) for op, cmds in operation_types.items()}
    }
    
    return {
        "nodes": nodes,
        "edges": edges,
        "keywords_index": dict(keywords_index),
        "statistics": statistics,
        "metadata": {
            "source_json": "cli_content_list.json",
            "source_xml": "command_tree-Beta_APV_10_5_0_73.xml",
            "generated_at": "2026-03-20T00:00:00Z",
            "special_operations_handled": True
        }
    }


def extract_module_keywords(module_name: str, module_data: Dict[str, Any]) -> List[str]:
    """提取模块关键词"""
    keywords = [module_name.lower()]
    
    # 从帮助字符串中提取关键词
    help_string = module_data.get("help_string", "")
    if help_string:
        # 简单分词
        words = re.findall(r'\b[a-zA-Z]{3,}\b', help_string)
        keywords.extend([w.lower() for w in words])
    
    # 从命令中提取常见词汇
    commands = module_data.get("commands", [])
    for cmd in commands[:10]:  # 只检查前10个命令
        cmd_parts = cmd.split()
        for part in cmd_parts[1:]:  # 跳过模块前缀
            if len(part) > 2 and not part.startswith(('<', '[', '{')):
                keywords.append(part.lower())
    
    return list(set(keywords))[:20]  # 限制关键词数量


def save_graph_to_json(graph: Dict[str, Any], output_path: Path) -> None:
    """保存图谱到JSON文件"""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
        logger.info(f"图谱已保存到: {output_path}")
    except Exception as e:
        logger.error(f"保存图谱失败: {e}")


def generate_graph_visualization(graph: Dict[str, Any], output_path: Path) -> None:
    """
    生成简单的图谱可视化（HTML格式）
    
    注意：这需要网络连接来加载D3.js库
    """
    try:
        # 读取模板
        template_path = Path(__file__).parent / "templates" / "graph_visualization.html"
        if template_path.exists():
            with open(template_path, 'r', encoding='utf-8') as f:
                template = f.read()
        else:
            # 创建基本模板
            template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CLI Keyword Graph Visualization</title>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        body { margin: 0; padding: 20px; font-family: Arial, sans-serif; }
        .graph-container { width: 100%; height: 800px; border: 1px solid #ccc; }
        .node { cursor: pointer; }
        .node.module { fill: #4CAF50; }
        .node.command { fill: #2196F3; }
        .node.keyword { fill: #FF9800; }
        .link { stroke: #999; stroke-opacity: 0.6; }
        .tooltip { position: absolute; padding: 10px; background: white; border: 1px solid #ccc; border-radius: 5px; pointer-events: none; }
    </style>
</head>
<body>
    <h1>CLI Keyword Relationship Graph</h1>
    <div id="graph" class="graph-container"></div>
    <div id="tooltip" class="tooltip" style="display: none;"></div>
    
    <script>
        // 这里将插入图谱数据
        const graphData = {{GRAPH_DATA}};
        
        // D3.js可视化代码
        // 由于代码较长，这里只显示框架
        console.log('Graph data loaded:', graphData);
        document.getElementById('graph').innerHTML = '<p>Graph visualization would be rendered here with D3.js</p><p>Total nodes: ' + graphData.nodes.length + ', Total edges: ' + graphData.edges.length + '</p>';
    </script>
</body>
</html>"""
        
        # 替换数据
        graph_json = json.dumps(graph, ensure_ascii=False)
        html_content = template.replace("{{GRAPH_DATA}}", graph_json)
        
        # 保存HTML文件
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"可视化文件已生成: {output_path}")
        logger.info(f"使用浏览器打开查看: file://{output_path.absolute()}")
        
    except Exception as e:
        logger.error(f"生成可视化失败: {e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="从CLI PDF JSON和command tree提取关键字并生成图谱")
    parser.add_argument(
        "--cli-json",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "mineru_output" / "cli" / "hybrid_auto" / "cli_content_list.json",
        help="CLI PDF转换的JSON文件路径"
    )
    parser.add_argument(
        "--cli-xml",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "input" / "command_tree-Beta_APV_10_5_0_73.xml",
        help="Command tree XML文件路径"
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "cli_keyword_graph.json",
        help="输出JSON图谱文件路径"
    )
    parser.add_argument(
        "--output-html",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "cli_keyword_graph.html",
        help="输出HTML可视化文件路径"
    )
    parser.add_argument(
        "--no-visualization",
        action="store_true",
        help="不生成HTML可视化文件"
    )
    
    args = parser.parse_args()
    
    logger.info("开始提取CLI关键字并生成图谱...")
    
    # 1. 从JSON提取CLI命令
    logger.info(f"从JSON提取CLI命令: {args.cli_json}")
    json_commands = extract_cli_commands_from_json(args.cli_json)
    logger.info(f"从JSON提取到 {json_commands.get('total_commands', 0)} 个命令")
    
    # 2. 从XML提取命令层级
    logger.info(f"从XML提取命令层级: {args.cli_xml}")
    xml_hierarchy = extract_command_hierarchy_from_xml(args.cli_xml)
    logger.info(f"从XML提取到 {len(xml_hierarchy.get('all_commands', []))} 个命令")
    
    # 3. 构建图谱
    logger.info("构建CLI关键字关系图谱...")
    graph = build_cli_keyword_graph(json_commands, xml_hierarchy)
    
    # 4. 保存JSON图谱
    logger.info(f"保存JSON图谱到: {args.output_json}")
    save_graph_to_json(graph, args.output_json)
    
    # 5. 生成可视化（可选）
    if not args.no_visualization:
        logger.info(f"生成HTML可视化到: {args.output_html}")
        generate_graph_visualization(graph, args.output_html)
    
    # 6. 打印统计信息
    stats = graph.get("statistics", {})
    logger.info("=== 图谱统计信息 ===")
    logger.info(f"总模块数: {stats.get('total_modules', 0)}")
    logger.info(f"总命令数: {stats.get('total_commands', 0)}")
    logger.info(f"总节点数: {stats.get('total_nodes', 0)}")
    logger.info(f"总边数: {stats.get('total_edges', 0)}")
    logger.info(f"总关键词数: {stats.get('total_keywords', 0)}")
    logger.info(f"JSON命令数: {stats.get('json_commands_count', 0)}")
    logger.info(f"XML命令数: {stats.get('xml_commands_count', 0)}")
    
    # 7. 显示一些示例
    logger.info("=== 示例数据 ===")
    nodes = graph.get("nodes", [])
    if nodes:
        # 显示前5个模块
        modules = [n for n in nodes if n.get("type") == "module"]
        logger.info(f"前5个模块:")
        for module in modules[:5]:
            logger.info(f"  - {module.get('label')}: {module.get('commands_count')} 个命令")
        
        # 显示前10个命令
        commands = [n for n in nodes if n.get("type") == "command"]
        logger.info(f"前10个命令:")
        for cmd in commands[:10]:
            logger.info(f"  - {cmd.get('label')}")
    
    logger.info("=== 完成 ===")


if __name__ == "__main__":
    main()