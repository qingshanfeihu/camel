#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析CLI关键字图谱，提取关键关系
"""

import json
import logging
from pathlib import Path
from collections import defaultdict

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    encoding='utf-8'
)
logger = logging.getLogger(__name__)


def analyze_cli_graph(graph_path: Path):
    """分析CLI图谱"""
    try:
        with open(graph_path, 'r', encoding='utf-8') as f:
            graph = json.load(f)
    except Exception as e:
        logger.error(f"读取图谱文件失败: {e}")
        return
    
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    stats = graph.get("statistics", {})
    
    logger.info("=== CLI关键字图谱分析 ===")
    logger.info(f"总节点数: {len(nodes)}")
    logger.info(f"总边数: {len(edges)}")
    logger.info(f"总模块数: {stats.get('total_modules', 0)}")
    logger.info(f"总命令数: {stats.get('total_commands', 0)}")
    
    # 1. 分析模块
    modules = [n for n in nodes if n.get("type") == "module"]
    logger.info(f"\n=== 模块分析 ===")
    logger.info(f"总模块数: {len(modules)}")
    
    # 按命令数量排序
    modules_sorted = sorted(modules, key=lambda x: x.get("commands_count", 0), reverse=True)
    
    logger.info("\nTop 10 模块（按命令数量）:")
    for i, module in enumerate(modules_sorted[:10]):
        logger.info(f"  {i+1}. {module.get('label')}: {module.get('commands_count')} 个命令")
        logger.info(f"     帮助: {module.get('help_string', '')[:80]}...")
        logger.info(f"     关键词: {', '.join(module.get('keywords', [])[:5])}")
    
    # 2. 分析SLB相关
    logger.info("\n=== SLB相关分析 ===")
    slb_modules = [m for m in modules if 'slb' in m.get('id', '').lower() or 'slb' in m.get('label', '').lower()]
    logger.info(f"SLB相关模块数: {len(slb_modules)}")
    
    for module in slb_modules:
        logger.info(f"\n模块: {module.get('label')}")
        logger.info(f"  命令数: {module.get('commands_count')}")
        logger.info(f"  帮助: {module.get('help_string', '')}")
        logger.info(f"  关键词: {', '.join(module.get('keywords', [])[:10])}")
    
    # 3. 分析命令
    commands = [n for n in nodes if n.get("type") == "command"]
    logger.info(f"\n=== 命令分析 ===")
    logger.info(f"总命令数: {len(commands)}")
    
    # SLB相关命令
    slb_commands = [c for c in commands if 'slb' in c.get('label', '').lower()]
    logger.info(f"SLB相关命令数: {len(slb_commands)}")
    
    # 按命令类型分组
    command_types = defaultdict(list)
    for cmd in slb_commands[:50]:  # 只查看前50个
        label = cmd.get("label", "")
        if label.startswith("slb "):
            parts = label.split()
            if len(parts) >= 2:
                cmd_type = parts[1]
                command_types[cmd_type].append(label)
    
    logger.info("\nSLB命令类型分布:")
    for cmd_type, cmds in sorted(command_types.items(), key=lambda x: len(x[1]), reverse=True):
        logger.info(f"  {cmd_type}: {len(cmds)} 个命令")
        if len(cmds) <= 5:  # 如果数量少，显示具体命令
            for cmd in cmds[:5]:
                logger.info(f"    - {cmd}")
    
    # 4. 分析关系
    logger.info("\n=== 关系分析 ===")
    
    # 模块-命令关系
    module_command_edges = [e for e in edges if e.get("type") == "contains"]
    logger.info(f"模块-命令关系数: {len(module_command_edges)}")
    
    # 命令层级关系
    parent_child_edges = [e for e in edges if e.get("type") == "parent_of"]
    logger.info(f"命令层级关系数: {len(parent_child_edges)}")
    
    # 关键词共享关系
    keyword_edges = [e for e in edges if e.get("type") == "shares_keyword"]
    logger.info(f"关键词共享关系数: {len(keyword_edges)}")
    
    # 5. 关键词分析
    keywords_index = graph.get("keywords_index", {})
    logger.info(f"\n=== 关键词分析 ===")
    logger.info(f"总关键词数: {len(keywords_index)}")
    
    # 热门关键词
    keyword_popularity = {kw: len(nodes) for kw, nodes in keywords_index.items()}
    top_keywords = sorted(keyword_popularity.items(), key=lambda x: x[1], reverse=True)[:20]
    
    logger.info("\nTop 20 热门关键词:")
    for i, (kw, count) in enumerate(top_keywords):
        logger.info(f"  {i+1}. {kw}: 关联 {count} 个节点")
    
    # 6. 生成简化的SLB子图
    logger.info("\n=== 生成SLB子图 ===")
    generate_slb_subgraph(graph, "slb")
    
    logger.info("\n=== 分析完成 ===")


def generate_slb_subgraph(graph: dict, module_name: str):
    """生成指定模块的子图"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    
    # 找到模块节点
    module_node = None
    for node in nodes:
        if node.get("type") == "module" and node.get("id") == module_name:
            module_node = node
            break
    
    if not module_node:
        logger.warning(f"未找到模块: {module_name}")
        return
    
    # 找到模块包含的命令
    module_commands = []
    for edge in edges:
        if edge.get("type") == "contains" and edge.get("source") == module_name:
            target_id = edge.get("target")
            # 找到对应的命令节点
            for node in nodes:
                if node.get("id") == target_id and node.get("type") == "command":
                    module_commands.append(node)
    
    logger.info(f"模块 {module_name} 包含 {len(module_commands)} 个命令")
    
    # 显示前20个命令
    logger.info(f"\n{module_name.upper()} 模块的前20个命令:")
    for i, cmd in enumerate(module_commands[:20]):
        logger.info(f"  {i+1}. {cmd.get('label')}")
        logger.info(f"     关键词: {', '.join(cmd.get('keywords', [])[:5])}")
    
    # 生成简化的子图数据
    subgraph = {
        "module": module_node,
        "commands": module_commands[:50],  # 只取前50个
        "total_commands": len(module_commands)
    }
    
    # 保存子图
    output_path = Path(__file__).parent.parent / "knowledge_base" / f"{module_name}_subgraph.json"
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(subgraph, f, ensure_ascii=False, indent=2)
        logger.info(f"子图已保存到: {output_path}")
    except Exception as e:
        logger.error(f"保存子图失败: {e}")


def main():
    """主函数"""
    graph_path = Path(__file__).parent.parent / "knowledge_base" / "cli_keyword_graph.json"
    
    if not graph_path.exists():
        logger.error(f"图谱文件不存在: {graph_path}")
        return
    
    logger.info(f"分析图谱文件: {graph_path}")
    analyze_cli_graph(graph_path)


if __name__ == "__main__":
    main()