import json
from pathlib import Path

def verify_operations():
    """验证操作类型处理是否正确"""
    graph_path = Path(r"C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\cli_keyword_graph.json")
    
    print("=== 验证操作类型处理 ===")
    
    with open(graph_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 统计操作类型节点
    operation_nodes = []
    module_nodes = []
    
    for node in data['nodes']:
        if node['type'] == 'operation':
            operation_nodes.append(node)
        elif node['type'] == 'module':
            module_nodes.append(node)
    
    print(f"操作类型节点数: {len(operation_nodes)}")
    for op in operation_nodes:
        print(f"  - {op['id']}: {op['commands_count']} 个命令")
    
    print(f"\n模块节点数: {len(module_nodes)}")
    
    # 统计操作类型与模块的关系
    operation_edges = []
    for edge in data['edges']:
        if edge['source'] in ['clear', 'no', 'show']:
            operation_edges.append(edge)
    
    print(f"\n操作类型相关边数: {len(operation_edges)}")
    
    # 统计每个操作类型连接的命令数
    operation_commands = {}
    for edge in operation_edges:
        op = edge['source']
        if op not in operation_commands:
            operation_commands[op] = set()
        operation_commands[op].add(edge['target'])
    
    print("\n操作类型连接的命令数:")
    for op, commands in operation_commands.items():
        print(f"  - {op}: {len(commands)} 个命令")
    
    # 查看一些具体的连接关系
    print("\n=== 示例连接关系 ===")
    sample_edges = []
    for edge in operation_edges[:10]:
        sample_edges.append(edge)
    
    for edge in sample_edges:
        print(f"  {edge['source']} -> {edge['target']} ({edge['type']})")
    
    # 检查是否有操作类型直接连接到模块
    operation_to_module = []
    for edge in data['edges']:
        if edge['source'] in ['clear', 'no', 'show']:
            # 检查target是否是模块
            target_id = edge['target']
            for node in data['nodes']:
                if node['id'] == target_id and node['type'] == 'module':
                    operation_to_module.append(edge)
                    break
    
    print(f"\n操作类型直接连接到模块的边数: {len(operation_to_module)}")
    if operation_to_module:
        print("具体连接:")
        for edge in operation_to_module[:5]:
            print(f"  {edge['source']} -> {edge['target']} ({edge['type']})")
    
    # 检查关键词分析中的热门关键词
    print("\n=== 关键词分析验证 ===")
    keyword_counts = {}
    for node in data['nodes']:
        if 'keywords' in node:
            for keyword in node['keywords']:
                if keyword in ['clear', 'no', 'show']:
                    keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
    
    print("操作类型作为关键词出现的次数:")
    for keyword, count in keyword_counts.items():
        print(f"  - {keyword}: {count} 次")

if __name__ == "__main__":
    verify_operations()