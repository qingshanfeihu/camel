#!/usr/bin/env python3
"""向知识库添加源地址负载均衡算法的合成文档条目。"""
import json
from pathlib import Path

KB_PATH = Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json"

NEW_ENTRIES = [
    {
        "page_content": (
            "源地址负载均衡算法（Source IP Hashing / hi / sr）完整配置方法\n\n"
            "源地址负载均衡算法根据客户端源IP地址将业务请求分配给不同的后台服务器。\n"
            "NSAE设备支持两种相关算法：\n"
            "- hi (Hash IP): 基于源IP地址的哈希值来选择后台服务器，同一源IP的请求始终分配到同一台服务器\n"
            "- sr (Shortest Response): 基于响应时间最短的算法\n\n"
            "对于「根据不同源IP范围分配到不同服务器」的测试需求，"
            "可使用 hi (Hash IP) 算法实现。\n\n"
            "关键配置步骤：\n"
            "1. 网络基础配置（接口IP + 路由）\n"
            "   ip address port3 <server_side_ip> <mask>\n"
            "   ip route static <client_subnet> <mask> <gateway>\n\n"
            "2. 创建多台Real Server（可以是同IP不同端口，或不同IP同端口）\n"
            '   slb real http "rs1" <ip1> <port1> 0 none 3 3\n'
            '   slb real http "rs2" <ip2> <port2> 0 none 3 3\n'
            '   slb real http "rs3" <ip3> <port3> 0 none 3 3\n\n'
            "3. 创建健康检查（ICMP模式）\n"
            '   slb health "hc_icmp" icmp 10 5 1 3\n\n'
            "4. 创建后台服务器组并设置算法\n"
            '   slb group method "sg1" hi\n'
            '   slb group member "sg1" "rs1"\n'
            '   slb group member "sg1" "rs2"\n'
            '   slb group member "sg1" "rs3"\n'
            '   slb group health "sg1" "hc_icmp"\n\n'
            "5. 创建虚拟服务\n"
            '   slb virtual http "vs_src" <vip> <port> arp 0\n\n'
            "6. 绑定默认策略\n"
            '   slb policy default "vs_src" "sg1"\n\n'
            "7. 启用虚拟服务\n"
            '   slb virtual enable "vs_src"\n\n'
            "验证命令：\n"
            "   show slb virtual http\n"
            "   show slb real http\n"
            "   show slb group method\n"
            "   show slb group member\n"
            "   show slb group health\n"
            "   show slb policy default\n"
            "   show statistics slb real http\n\n"
            "注意事项：\n"
            "- 使用 hi (Hash IP) 算法可以实现基于源地址的负载均衡\n"
            "- 同一源IP的所有请求会被哈希到同一台服务器\n"
            "- 多台后端服务器可以使用同一IP不同端口模拟（VM上运行多个HTTP实例）\n"
            "- ICMP健康检查适用于基础连通性检测\n"
            "- slb health icmp 不需要 method/url/expected_codes 参数"
        ),
        "metadata": {
            "source_pdf": "cli.pdf",
            "source_file": "cli.pdf",
            "page_idx": 236,
            "block_type": "text",
            "block_id": "synthetic_source_address_lb_guide",
            "command_prefix": "slb",
            "config_mode": "global",
            "product_module": "SLB",
            "description": "源地址负载均衡算法(hi/sr)完整配置方法：基于源IP哈希分配请求到不同后台服务器",
            "section_title": "SLB 源地址负载均衡算法配置",
            "parent_section": "服务器负载均衡算法",
            "function_hierarchy": "SLB > 负载均衡算法 > 源地址负载均衡",
            "scenario_id": "SLB_SOURCE_ADDRESS_LB",
            "step_type": "policies_and_algorithms",
            "intent": "源地址负载均衡算法配置",
            "required_keywords": ["slb group method", "hi", "slb real http", "slb policy default"],
            "is_synthetic": True,
            "protocol_type": ["HTTP", "TCP"],
        },
    },
    {
        "page_content": (
            "NSAE SLB 多台后端服务器配置（同IP不同端口模拟）\n\n"
            "当测试环境只有一台VM但需要模拟多台后端服务器时，使用同一IP的不同端口来模拟。\n\n"
            "示例：3台后端服务器使用10.0.3.10的80、8081、8082端口\n\n"
            "配置命令：\n"
            '  slb real http "rs1" 10.0.3.10 80 0 none 3 3\n'
            '  slb real http "rs2" 10.0.3.10 8081 0 none 3 3\n'
            '  slb real http "rs3" 10.0.3.10 8082 0 none 3 3\n\n'
            "将所有服务器加入同一个组：\n"
            '  slb group method "sg_multi" hi\n'
            '  slb group member "sg_multi" "rs1"\n'
            '  slb group member "sg_multi" "rs2"\n'
            '  slb group member "sg_multi" "rs3"\n\n'
            "关键要点：\n"
            "- slb real http 命令中，IP+端口组合唯一标识一台后端服务器\n"
            "- 同一IP不同端口被设备视为不同的Real Server\n"
            "- 每台Real Server需要独立的名称（如rs1, rs2, rs3）\n"
            "- VM上需要为每个端口启动独立的HTTP服务实例\n"
            "- 健康检查会分别检查每个IP:端口的可达性"
        ),
        "metadata": {
            "source_pdf": "cli.pdf",
            "source_file": "cli.pdf",
            "page_idx": 237,
            "block_type": "text",
            "block_id": "synthetic_multi_server_same_ip_guide",
            "command_prefix": "slb",
            "config_mode": "global",
            "product_module": "SLB",
            "description": "多台后端服务器配置方法：同IP不同端口模拟多台服务器",
            "section_title": "SLB 多Real Server配置",
            "parent_section": "后台服务器配置",
            "function_hierarchy": "SLB > 后台服务器 > 多服务器同IP不同端口",
            "scenario_id": "SLB_MULTI_SERVER_SAME_IP",
            "step_type": "backend_servers",
            "intent": "多台后端服务器配置（同IP不同端口）",
            "required_keywords": ["slb real http", "slb group member", "slb group method"],
            "is_synthetic": True,
            "protocol_type": ["HTTP", "TCP"],
        },
    },
    {
        "page_content": (
            "NSAE SLB ICMP健康检查配置\n\n"
            "ICMP健康检查通过发送ping包来检测后端服务器的可达性。\n"
            "适用于不需要检查应用层（HTTP响应内容）的场景。\n\n"
            "配置命令格式：\n"
            '  slb health "<hc_name>" icmp <interval> <timeout> <up_retries> <down_retries>\n\n'
            "参数说明：\n"
            "  - hc_name: 健康检查名称\n"
            "  - interval: 检查间隔（秒）\n"
            "  - timeout: 超时时间（秒）\n"
            "  - up_retries: 检测恢复所需连续成功次数\n"
            "  - down_retries: 检测故障所需连续失败次数\n\n"
            "配置示例：\n"
            '  slb health "hc_icmp" icmp 10 5 1 3\n'
            "  含义：每10秒检查一次，超时5秒，连续1次成功判定恢复，连续3次失败判定故障\n\n"
            "绑定到服务器组：\n"
            '  slb group health "sg1" "hc_icmp"\n\n'
            "验证命令：\n"
            "  show slb health\n"
            "  show slb group health\n"
            "  show statistics slb real http\n\n"
            "注意：ICMP健康检查不带 method/url/expected_codes 参数，与HTTP健康检查格式不同！"
        ),
        "metadata": {
            "source_pdf": "cli.pdf",
            "source_file": "cli.pdf",
            "page_idx": 238,
            "block_type": "text",
            "block_id": "synthetic_icmp_health_check_guide",
            "command_prefix": "slb",
            "config_mode": "global",
            "product_module": "SLB",
            "description": "ICMP健康检查配置方法",
            "section_title": "SLB ICMP健康检查",
            "parent_section": "服务器负载均衡健康检查方法",
            "function_hierarchy": "SLB > 健康检查 > ICMP健康检查",
            "scenario_id": "SLB_ICMP_HEALTH_CHECK",
            "step_type": "health_checks",
            "intent": "ICMP健康检查配置",
            "required_keywords": ["slb health", "icmp"],
            "is_synthetic": True,
            "protocol_type": ["ICMP"],
        },
    },
]


def main():
    print(f"Loading knowledge base from: {KB_PATH}")
    with open(KB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Current entries: {len(data)}")

    existing_ids = {d.get("metadata", {}).get("block_id", "") for d in data}
    added = 0
    for entry in NEW_ENTRIES:
        bid = entry["metadata"]["block_id"]
        if bid in existing_ids:
            print(f"  Skip (already exists): {bid}")
        else:
            data.append(entry)
            added += 1
            print(f"  Added: {bid}")

    if added > 0:
        with open(KB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved. New total: {len(data)}")
    else:
        print("No new entries to add.")


if __name__ == "__main__":
    main()
