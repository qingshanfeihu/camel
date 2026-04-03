import json
from pathlib import Path
from collections import defaultdict

def check_operation_examples():
    """检查操作类型命令的具体示例"""
    graph_path = Path(r"C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\cli_keyword_graph.json")
    
    print("=== 检查操作类型命令示例 ===")
    
    with open(graph_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 收集操作类型命令
    operation_commands = defaultdict(list)
    operation_to_module = defaultdict(set)
    
    for node in data['nodes']:
        if node['type'] == 'operation_command':
            # 提取操作类型（第一个词）
            parts = node['label'].split()
            if parts:
                op_type = parts[0].lower()
                operation_commands[op_type].append(node['label'])
                
                # 检查是否有实际模块
                if 'actual_module' in node and node['actual_module']:
                    operation_to_module[op_type].add(node['actual_module'])
    
    print("操作类型命令统计:")
    for op_type, commands in operation_commands.items():
        print(f"\n{op_type.upper()} 命令 ({len(commands)} 个):")
        # 显示前10个示例
        for i, cmd in enumerate(commands[:10]):
            print(f"  {i+1}. {cmd}")
        if len(commands) > 10:
            print(f"  ... 还有 {len(commands)-10} 个命令")
    
    print("\n=== 操作类型与模块的关联 ===")
    for op_type, modules in operation_to_module.items():
        print(f"\n{op_type.upper()} 关联的模块 ({len(modules)} 个):")
        for module in sorted(modules)[:20]:  # 显示前20个
            print(f"  - {module}")
        if len(modules) > 20:
            print(f"  ... 还有 {len(modules)-20} 个模块")
    
    # 检查SLB模块的操作类型命令
    print("\n=== SLB模块的操作类型命令示例 ===")
    slb_operation_commands = defaultdict(list)
    
    for node in data['nodes']:
        if node['type'] == 'operation_command':
            label = node['label'].lower()
            if 'slb' in label:
                parts = label.split()
                if parts:
                    op_type = parts[0].lower()
                    slb_operation_commands[op_type].append(node['label'])
    
    for op_type, commands in slb_operation_commands.items():
        print(f"\nSLB相关的 {op_type.upper()} 命令 ({len(commands)} 个):")
        for i, cmd in enumerate(commands[:15]):  # 显示前15个
            print(f"  {i+1}. {cmd}")
        if len(commands) > 15:
            print(f"  ... 还有 {len(commands)-15} 个命令")
    
    # 检查图谱中的关系类型
    print("\n=== 关系类型统计 ===")
    relation_types = defaultdict(int)
    for edge in data['edges']:
        relation_types[edge['type']] += 1
    
    for rel_type, count in sorted(relation_types.items(), key=lambda x: x[1], reverse=True):
        print(f"  {rel_type}: {count} 个关系")
    
    # 检查操作类型相关的边
    print("\n=== 操作类型相关的关系 ===")
    operation_edges_by_type = defaultdict(list)
    for edge in data['edges']:
        if edge['type'] in ['operation_of', 'has_operation']:
            operation_edges_by_type[edge['type']].append(edge)
    
    for rel_type, edges in operation_edges_by_type.items():
        print(f"\n{rel_type} 关系 ({len(edges)} 个):")
        for i, edge in enumerate(edges[:5]):  # 显示前5个示例
            print(f"  {i+1}. {edge['source']} -> {edge['target']}")
        if len(edges) > 5:
            print(f"  ... 还有 {len(edges)-5} 个关系")

if __name__ == "__main__":
    check_operation_examples()