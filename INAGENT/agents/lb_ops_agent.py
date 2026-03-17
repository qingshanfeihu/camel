# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
from typing import Callable, Iterable, Optional, Dict, List, Any

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend


def build_lb_ops_agent(
    model: BaseModelBackend,
    rag_retrieve: Callable[[str], str],
    extra_tools: Optional[Iterable[Callable]] = None,
) -> ChatAgent:
    r"""Build the load balancer operations agent.

    Args:
        model (BaseModelBackend): The model backend.
        rag_retrieve (Callable[[str], str]): RAG retrieval tool.

    Returns:
        ChatAgent: Configured LB ops agent.
    """
    tools = [rag_retrieve]
    if extra_tools:
        tools.extend(list(extra_tools))

    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="LB Ops",
            content=(
                "你是负载均衡器操作专家，目标设备为 InfosecOS NSAE（vNSAE 系列，Infosec Technologies）。\n"
                "必须根据功能树结构和配置顺序（最小生成树）生成配置命令。\n"
                "输出严格 JSON，包含：config_commands（数组）、verify_commands（数组）、notes。\n"
                "配置命令必须按照prompt中指定的配置顺序生成，为每个步骤生成对应的配置命令。\n"
                "如果Context中没有直接示例，根据步骤类型、协议类型和产品模块推断配置命令。\n\n"
                "【NSAE CLI 命令参考（CRITICAL：必须严格遵循此语法，不要自创不存在的命令）】\n"
                "进入模式: enable → config terminal\n\n"
                "1. 虚拟服务（virtual service）:\n"
                '   配置: slb virtual http "<name>" <vip> <port> [arp|noarp] [max_conn]\n'
                '   示例: slb virtual http "vs_http" 172.16.6.100 80 arp 0\n'
                '   删除: no slb virtual http <name>\n'
                '   查看: show slb virtual（若机型支持也可 show slb virtual http）\n\n'
                "2. 真实服务器（real server）:\n"
                '   配置: slb real http "<name>" <ip> <port> [max_conn] [none|cookie|url|insert|header] [weight] [weight_value]\n'
                '   示例: slb real http "rs1" 192.168.96.161 80 0 none 3 3\n'
                '   删除: no slb real http <name>\n'
                '   查看: show slb real（若机型支持也可 show slb real http）\n\n'
                "3. 健康检查（health check）:\n"
                '   [3a] 基础HTTP状态码检查（只验证HTTP响应码，不检查内容）:\n'
                '   配置: slb health "<name>" http <interval> <timeout> <up_retries> <down_retries> <method> "<url>" "<expected_http_status_code>"\n'
                '   ⚠️ expected_http_status_code 参数：只能填 HTTP 状态码（如"200"、"200 404"），务必不能填响应内容字符串！\n'
                '   示例: slb health "hc_http" http 15 5 1 3 GET "/index.html" "200"\n'
                '   删除: no slb health <name>\n'
                '   查看: show slb health\n\n'
                '   [3b] 基于HTTP响应内容/关键词匹配健康检查（必须用4步三命令法）:\n'
                '   ⚠️ 当需要检查响应体中是否包含某个关键词/字符串时，不能使用 slb health 的 expected_codes 参数，\n'
                '      必须使用 health request + health response + slb health + health server 四步完整配置法！\n'
                '   步骤1 - 定义自定义请求: health request <request_idx> "GET /path HTTP/1.0\\r\\n\\r\\n"\n'
                '   步骤2 - 定义期望内容:   health response <response_idx> "<关键词字符串>"\n'
                '   步骤3 - 定义健康检查:   slb health "<hc_name>" http <interval> <timeout> <up> <down>\n'
                '     （注意: 此处 slb health 不带 http_method/url/expected_codes 参数！）\n'
                '   步骤4 - 绑定请求/应答:  health server "<hc_name>" <request_idx> <response_idx>\n'
                '   完整示例（检查页面包含"chinamobile"）:\n'
                '     health request 1 "GET /index.html HTTP/1.0\\r\\n\\r\\n"\n'
                '     health response 1 "chinamobile"\n'
                '     slb health "hc_content" http 15 5 1 3\n'
                '     health server "hc_content" 1 1\n\n'
                "4. 服务器组（group）:\n"
                '   负载算法: slb group method "<name>" <rr|lc|pi|hi|sr|grr|least-response>\n'
                '   添加成员: slb group member "<group_name>" "<real_name>"\n'
                '   绑定健康检查: slb group health "<group_name>" "<health_name>"\n'
                '   查看: show slb group method, show slb group member, show slb group health\n\n'
                "5. 虚拟服务绑定组（policy default）:\n"
                '   绑定: slb policy default "<virtual_name>" "<group_name>"\n'
                '   删除: no slb policy default <virtual_name>\n'
                '   查看: show slb policy default\n\n'
                "6. 启用/禁用虚拟服务:\n"
                '   启用: slb virtual enable "<virtual_name>"\n'
                '   禁用: slb virtual disable "<virtual_name>"\n\n'
                "7. LLB 链路负载均衡（link load balancing）:\n"
                '   链路配置: llb link "<name>" <interface> <gateway> <weight> [enable|disable]\n'
                '   链路健康检查: llb link health on "<name>"\n'
                '   健康检查器: llb link health checker icmp "<name>" <target_ip>\n'
                '   出站算法: llb method outbound <rr|lc|hi|sr|wrr|lb|dd|hip>\n'
                '   入站算法: llb method inbound <rr|wrr|proximity>\n'
                '   DNS代理: llb dnsproxy on / llb dnsproxy server <ip>\n'
                '   DNS监听: llb dns listener <ip> <port>\n'
                '   远程站点: llb rsite "<name>" <ip>\n'
                '   查看: show llb link, show llb method, show llb dnsproxy, show llb rsite\n\n'
                "8. SSL/TLS 证书管理:\n"
                '   导入证书: ssl import certificate "<cert_name>" <filename>\n'
                '   导入密钥: ssl import key "<key_name>" <filename>\n'
                '   激活证书: ssl activate certificate "<cert_name>"\n'
                '   停用证书: ssl deactivate certificate "<cert_name>"\n'
                '   生成CSR: ssl csr "<name>" <key_size> "<CN>" "<O>" "<OU>" "<L>" "<ST>" "<C>"\n'
                '   全局SSL协议: ssl globals protocol virtual <sslv3|tls1|tls11|tls12|tls13>\n'
                '   查看: show ssl certificate, show ssl globals\n\n'
                "9. 高可用（HA）:\n"
                '   HA组配置: ha group <group_id> <priority>\n'
                '   HA组启用: ha group enable <group_id>\n'
                '   浮动IP: ha group fip <group_id> <vip>\n'
                '   决策规则: ha decision rule <vhid> <rule_type>\n'
                '   浮动MAC: ha floatmac on / ha floatmac off\n'
                '   同步配置: ha synconfig on\n'
                '   查看: show ha group, show ha decision, show ha info\n\n'
                "10. NAT地址转换:\n"
                '   静态NAT: nat static <real_ip> <mapped_ip> [netmask]\n'
                '   NAT端口映射: nat port <real_ip> <mapped_ip> <port> <protocol>\n'
                '   NAT协议: nat protocol ftp on / nat protocol dns on\n'
                '   查看: show nat static, show nat port\n\n'
                "11. 网络基础设施（接口 IP / 路由 — CRITICAL: 必须在 SLB 等应用层之前配置）:\n"
                '   配置接口IP: ip address <interface_name> <ip> <mask>\n'
                '     (例: ip address port3 10.0.3.1 255.255.255.0)\n'
                '   ⚠️ NSAE 没有 interface 子模式，不要使用 interface <name> 进入子模式!\n'
                '   ⚠️ ip address 命令必须带接口名称参数，在 config 模式下直接执行!\n'
                '   静态路由: ip route static <dest_ip> <mask> <gateway>\n'
                '     (例: ip route static 27.16.9.0 255.255.255.0 27.16.9.52)\n'
                '   默认路由: ip route default <gateway>\n'
                '   删除接口IP: no ip address <interface_name>\n'
                '   删除静态路由: no ip route static <dest_ip> <mask> <gateway>\n'
                '   查看: show ip address, show ip route\n'
                '   ⚠️ ip address / ip route 命令必须放在 config_commands 的最前面，\n'
                '      先完成网络互通再做 SLB/LLB/NAT 等应用层配置!\n\n'
                "【verify_commands 规则】\n"
                "验证命令必须使用以下 NSAE 专用 show 命令（不要使用 show running-config）：\n"
                "  [网络] show ip address / show ip route\n"
                "  [SLB] show slb virtual / show slb real / show slb health\n"
                "        show slb group method / show slb group member / show slb group health / show slb policy default\n"
                "  [LLB] show llb link / show llb method / show llb dnsproxy / show llb rsite / show llb dns\n"
                "  [SSL] show ssl certificate / show ssl globals\n"
                "  [HA]  show ha group / show ha decision / show ha info\n"
                "  [NAT] show nat static / show nat port\n"
            ),
        ),
        model=model,
        tools=tools,
    )


def build_lb_ops_prompt(
    job_content: str,
    context: str,
    env_context: str,
    constraints: Optional[Dict[str, List[str]]] = None,
    decomposition_result: Optional[Dict[str, Any]] = None,
    function_index: Optional[Dict[str, Any]] = None,
    env_plan: Optional[Dict[str, Any]] = None,
) -> str:
    r"""Build the prompt for the LB ops agent.

    Args:
        job_content (str): Raw job/task content.
        context (str): RAG context snippets.
        env_context (str): Environment context (fallback if env_plan not provided).
        constraints (Optional[Dict[str, List[str]]]): Metadata constraints from RAG.
        decomposition_result (Optional[Dict[str, Any]]): Task decomposition result with required_steps.
        function_index (Optional[Dict[str, Any]]): Function structure index.
        env_plan (Optional[Dict[str, Any]]): Environment plan from env_setup_agent with actual IP addresses.

    Returns:
        str: Prompt string.
    """
    # ── 从 env_plan 构建详细的网络环境上下文 ──
    if env_plan:
        ev = env_plan.get("env", {})
        sc = env_plan.get("subnet_config", {})

        # ── 多服务器支持：检测 LB_REAL_SERVERS 列表 ──
        real_servers = ev.get("LB_REAL_SERVERS", [])
        if real_servers and isinstance(real_servers, list) and len(real_servers) > 1:
            # 多服务器模式：每台服务器有独立的 name/ip/port
            rs_lines = []
            rs_constraint_lines = []
            for i, rs in enumerate(real_servers):
                rs_name = rs.get("name", f"rs{i+1}")
                rs_ip = rs.get("ip", ev.get("LB_REAL_SERVER_IP", "N/A"))
                rs_port = rs.get("port", 80)
                rs_lines.append(f"  Real Server {i+1} ({rs_name}): {rs_ip}:{rs_port}")
                rs_constraint_lines.append(
                    f'{i+1}. slb real http "{rs_name}" 的 IP 必须使用 {rs_ip}，端口必须使用 {rs_port}'
                )
            rs_info_text = "\n".join(rs_lines)
            rs_constraint_text = "\n".join(rs_constraint_lines)

            env_context = (
                "[网络环境规划 — 所有 IP 地址必须严格使用此处的值]\n"
                f"**多服务器模式**: {len(real_servers)} 台后端服务器（同 IP 不同端口）\n"
                f"{rs_info_text}\n"
                f"VIP (设备 port4 侧):       {ev.get('LB_VIP', 'N/A')}\n"
                f"VIP Port:                  {ev.get('LB_VIP_PORT', '80')}\n"
                f"VM eth1 (服务器侧):        {ev.get('VM_ETH1_IP', 'N/A')}/{ev.get('VM_ETH1_MASK', '255.255.255.0')}\n"
                f"VM eth2 (客户端侧):        {ev.get('VM_ETH2_IP', 'N/A')}/{ev.get('VM_ETH2_MASK', '255.255.255.0')}\n"
                f"设备 port3 (服务器侧):     {ev.get('DEVICE_PORT3_IP', 'N/A')}/{ev.get('DEVICE_PORT3_MASK', '255.255.255.0')}\n"
                f"设备 port4 (客户端侧):     27.16.9.52/255.255.255.0 (已有)\n"
                f"Real Server 子网:          {sc.get('LB_REAL_SERVER_SUBNET', 'N/A')}\n"
                f"VIP 子网:                  {sc.get('LB_VIP_SUBNET', 'N/A')}\n"
                f"虚拟服务名称:              {ev.get('LB_VIRTUAL_SERVICE', 'N/A')}\n"
                f"服务组名称:                {ev.get('LB_GROUP_NAME', 'N/A')}\n"
                f"健康检查间隔:              {env_plan.get('health_check_interval', 15)}s\n"
                f"健康检查超时:              {env_plan.get('health_check_timeout', 5)}s\n"
                f"健康检查失败次数:          {env_plan.get('health_check_retries', 3)}\n"
                "\n[关键约束 — 多服务器]\n"
                f"{rs_constraint_text}\n"
                f"{len(real_servers)+1}. 每台 real server 必须使用独立的名称和端口，"
                f"slb real http 命令格式: slb real http \"<name>\" <ip> <port> ...\n"
                f"{len(real_servers)+2}. slb virtual http 命令中的 VIP 必须使用: {ev.get('LB_VIP', '')}\n"
                f"{len(real_servers)+3}. 如果设备 port3 尚未配置 IP，config_commands 开头必须包含:\n"
                f"   ip address port3 {ev.get('DEVICE_PORT3_IP', '')} {ev.get('DEVICE_PORT3_MASK', '')}\n"
                f"   ⚠️ NSAE 没有 interface 子模式，不要使用 interface port3 / exit!\n"
                f"{len(real_servers)+4}. 网络基础配置(ip address / ip route)必须排在 SLB 配置之前\n"
                f"{len(real_servers)+5}. 不要使用 127.0.0.1、192.168.96.x 或其他虚构 IP，只使用上述规划的 IP\n"
                f"{len(real_servers)+6}. slb group member 必须将所有 {len(real_servers)} 台 real server 加入同一个组\n"
            )
        else:
            # 单服务器模式（原逻辑）
            env_context = (
                "[网络环境规划 — 所有 IP 地址必须严格使用此处的值]\n"
                f"Real Server IP (VM eth1):  {ev.get('LB_REAL_SERVER_IP', 'N/A')}\n"
                f"Real Server Port:          {ev.get('LB_REAL_SERVER_PORT', '80')}\n"
                f"VIP (设备 port4 侧):       {ev.get('LB_VIP', 'N/A')}\n"
                f"VIP Port:                  {ev.get('LB_VIP_PORT', '80')}\n"
                f"VM eth1 (服务器侧):        {ev.get('VM_ETH1_IP', 'N/A')}/{ev.get('VM_ETH1_MASK', '255.255.255.0')}\n"
                f"VM eth2 (客户端侧):        {ev.get('VM_ETH2_IP', 'N/A')}/{ev.get('VM_ETH2_MASK', '255.255.255.0')}\n"
                f"设备 port3 (服务器侧):     {ev.get('DEVICE_PORT3_IP', 'N/A')}/{ev.get('DEVICE_PORT3_MASK', '255.255.255.0')}\n"
                f"设备 port4 (客户端侧):     27.16.9.52/255.255.255.0 (已有)\n"
                f"Real Server 子网:          {sc.get('LB_REAL_SERVER_SUBNET', 'N/A')}\n"
                f"VIP 子网:                  {sc.get('LB_VIP_SUBNET', 'N/A')}\n"
                f"虚拟服务名称:              {ev.get('LB_VIRTUAL_SERVICE', 'N/A')}\n"
                f"服务组名称:                {ev.get('LB_GROUP_NAME', 'N/A')}\n"
                f"健康检查间隔:              {env_plan.get('health_check_interval', 15)}s\n"
                f"健康检查超时:              {env_plan.get('health_check_timeout', 5)}s\n"
                f"健康检查失败次数:          {env_plan.get('health_check_retries', 3)}\n"
                "\n[关键约束]\n"
                f"1. slb real http 命令中的 IP 必须使用: {ev.get('LB_REAL_SERVER_IP', '')}\n"
                f"2. slb virtual http 命令中的 VIP 必须使用: {ev.get('LB_VIP', '')}\n"
                f"3. 如果设备 port3 尚未配置 IP，config_commands 开头必须包含:\n"
                f"   ip address port3 {ev.get('DEVICE_PORT3_IP', '')} {ev.get('DEVICE_PORT3_MASK', '')}\n"
                f"   ⚠️ NSAE 没有 interface 子模式，不要使用 interface port3 / exit!\n"
                "4. 网络基础配置(ip address / ip route)必须排在 SLB 配置之前\n"
                "5. 不要使用 127.0.0.1、192.168.96.x 或其他虚构 IP，只使用上述规划的 IP\n"
            )
    constraints_text = ""
    if constraints:
        lines = []
        config_modes = constraints.get("config_modes", [])
        required_keywords = constraints.get("required_keywords", [])
        intents = constraints.get("intents", [])
        function_hierarchies = constraints.get("function_hierarchies", [])  # P2: 功能层级
        descriptions = constraints.get("descriptions", [])  # P2: 文档块描述
        
        if config_modes:
            lines.append(f"必须使用的配置模式/CLI层级: {', '.join(config_modes)}")
        if required_keywords:
            lines.append(f"命令必须包含的关键参数/指令: {', '.join(required_keywords)}")
        if intents:
            lines.append(f"识别到的配置意图: {', '.join(intents)}")
        # P2: 添加功能层级和描述信息，帮助Agent理解上下文
        if function_hierarchies:
            lines.append(f"功能层级关系: {', '.join(function_hierarchies[:3])}")  # 只显示前3个
        if descriptions:
            # 合并描述信息，帮助理解文档块内容
            desc_summary = "; ".join(descriptions[:2])  # 只显示前2个
            if len(descriptions) > 2:
                desc_summary += f" (还有{len(descriptions)-2}个相关描述)"
            lines.append(f"文档块内容描述: {desc_summary}")
            
        if lines:
            constraints_text = "\n\n[严格约束条件]\n" + "\n".join(lines) + "\n请严格遵守上述约束生成命令。"
    
    # 构建功能树和配置顺序信息
    configuration_order_text = ""
    if decomposition_result and function_index:
        required_steps = decomposition_result.get("required_steps", [])
        advanced_features = decomposition_result.get("advanced_features", [])
        product_modules = decomposition_result.get("product_modules", [])
        protocol_type = decomposition_result.get("protocol_type", "")
        
        # 从function_index中获取模块的层级和步骤顺序
        modules = function_index.get("modules", {})
        
        # 构建配置顺序（最小生成树）
        configuration_steps = []
        
        # 1. 按模块层级排序（基础层 -> 应用层 -> 安全层）
        module_order_map = {}
        for module_name in product_modules:
            module_data = modules.get(module_name, {})
            level = module_data.get("level", "其他")
            order = module_data.get("order", 999)
            module_order_map[module_name] = {"level": level, "order": order}
        
        # 2. 为每个required_step找到对应的模块和顺序
        step_info_list = []
        for step in required_steps:
            # step格式可能是 "slb_network_basics" 或 "network_basics"
            step_parts = step.split("_", 1)
            if len(step_parts) == 2:
                module_prefix = step_parts[0]
                base_step = step_parts[1]
                
                # 找到对应的模块
                matched = False
                for module_name, module_data in modules.items():
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
                    
                    if module_normalized == module_prefix:
                        step_types = module_data.get("step_types", {})
                        step_type_info = step_types.get(step, {})
                        step_order = step_type_info.get("order", 999)
                        is_advanced = step_type_info.get("is_advanced", False)
                        module_order = module_data.get("order", 999)
                        
                        step_info_list.append({
                            "step": step,
                            "module": module_name,
                            "base_step": base_step,
                            "module_order": module_order,
                            "step_order": step_order,
                            "is_advanced": is_advanced,
                        })
                        matched = True
                        break
                
                # 如果找不到匹配的模块，仍然添加到列表中（可能是新模块或未识别的模块）
                if not matched:
                    # 尝试从product_modules中推断
                    inferred_module = None
                    for pm in product_modules:
                        pm_normalized = pm.lower()
                        if pm == "基础网络":
                            pm_normalized = "network"
                        elif pm == "安全":
                            pm_normalized = "security"
                        elif pm == "GSLB":
                            pm_normalized = "gslb"
                        elif pm == "LLB":
                            pm_normalized = "llb"
                        elif pm == "SLB":
                            pm_normalized = "slb"
                        
                        if pm_normalized == module_prefix:
                            inferred_module = pm
                            break
                    
                    # 如果仍然找不到，使用模块前缀作为模块名
                    if not inferred_module:
                        inferred_module = module_prefix.upper()
                    
                    step_info_list.append({
                        "step": step,
                        "module": inferred_module,
                        "base_step": base_step,
                        "module_order": 999,  # 未知模块放在最后
                        "step_order": 999,
                        "is_advanced": False,
                    })
        
        # 3. 按模块层级和步骤顺序排序
        step_info_list.sort(key=lambda x: (x["module_order"], x["step_order"]))
        
        # 4. 添加高级功能
        for feature in advanced_features:
            step_info_list.append({
                "step": feature,
                "module": "高级功能",
                "base_step": feature,
                "module_order": 999,
                "step_order": 999,
                "is_advanced": True,
            })
        
        if step_info_list:
            configuration_order_text = "\n\n[配置顺序（最小生成树）]（CRITICAL: 必须严格按照此顺序生成配置命令）\n"
            configuration_order_text += "配置必须按照以下顺序执行，先完成基础配置，再配置高级功能。\n"
            configuration_order_text += "**你必须为每个步骤生成对应的配置命令，即使Context中没有直接示例，也要根据步骤类型推断配置命令。**\n\n"
            
            # 从功能树动态获取步骤信息（命令模式、命令示例等）
            # 不再硬编码，而是从function_index中动态获取
            
            for i, step_info in enumerate(step_info_list, 1):
                step = step_info["step"]
                module = step_info["module"]
                base_step = step_info["base_step"]
                is_advanced = step_info["is_advanced"]
                advanced_mark = " [高级功能]" if is_advanced else ""
                
                # 从功能树动态获取步骤信息
                command_hint = f"{base_step}相关配置"
                command_patterns = []
                command_example = ""
                
                # 从function_index中查找步骤信息
                if function_index and modules:
                    module_data = modules.get(module, {})
                    step_types = module_data.get("step_types", {})
                    step_type_info = step_types.get(step, {})
                    
                    # 从关键词中提取可能的命令模式
                    keywords = step_type_info.get("keywords", [])
                    if keywords:
                        for kw in keywords[:5]:  # 取前5个关键词
                            kw_lower = kw.lower()
                            # 检查是否包含命令相关关键词（扩展到所有主要模块前缀）
                            if any(cmd_kw in kw_lower for cmd_kw in [
                                "slb", "llb", "ssl", "ha", "nat", "real", "virtual",
                                "policy", "health", "check", "group", "ip", "address",
                                "interface", "route", "link", "dns", "certificate",
                            ]):
                                command_patterns.append(kw)
                    
                    # 如果没有找到命令模式，根据模块+base_step推断（NSAE 专用语法）
                    module_lower = module.lower() if module else ""
                    if not command_patterns:
                        # SLB 模块回退
                        if module_lower == "slb" or module == "SLB":
                            if base_step == "virtual_services":
                                command_patterns = ["slb virtual http"]
                            elif base_step == "backend_servers":
                                command_patterns = ["slb real http"]
                            elif base_step == "health_checks":
                                command_patterns = ["slb health"]
                            elif base_step == "policies_and_algorithms":
                                command_patterns = ["slb group method", "slb group member", "slb group health", "slb virtual group"]
                        # LLB 模块回退
                        elif module_lower == "llb" or module == "LLB":
                            if base_step in ("basic_config", "link_config"):
                                command_patterns = ["llb link"]
                            elif base_step == "health_checks":
                                command_patterns = ["llb link health", "llb link health checker"]
                            elif base_step in ("policies_and_algorithms", "algorithm_config"):
                                command_patterns = ["llb method outbound", "llb method inbound"]
                            elif base_step in ("dns_config",):
                                command_patterns = ["llb dnsproxy", "llb dns"]
                        # 高可用 模块回退
                        elif module_lower in ("ha", "高可用"):
                            if base_step in ("high_availability", "basic_config"):
                                command_patterns = ["ha group", "ha decision"]
                            elif base_step == "health_checks":
                                command_patterns = ["ha checkpeer"]
                        # 安全/SSL 模块回退
                        elif module_lower in ("security", "安全", "ssl"):
                            if base_step in ("ssl_config", "ssl_certificate"):
                                command_patterns = ["ssl import certificate", "ssl import key", "ssl activate"]
                            elif base_step in ("acl_config", "access_control"):
                                command_patterns = ["acl", "accesslist"]
                            elif base_step == "health_checks":
                                command_patterns = ["health"]
                        # NAT 模块回退
                        elif module_lower == "nat":
                            command_patterns = ["nat static", "nat port", "nat protocol"]
                        # 通用网络基础回退
                        elif base_step == "network_basics":
                            command_patterns = ["ip address", "interface", "route", "vlan", "network"]
                    
                    # 从关键词推断配置类型描述
                    if keywords:
                        # 尝试从关键词构建描述
                        desc_keywords = [kw for kw in keywords[:3] if len(kw) > 2]
                        if desc_keywords:
                            command_hint = f"{base_step}相关配置（关键词: {', '.join(desc_keywords)}）"
                
                configuration_order_text += f"{i}. **步骤: {step}** (模块: {module}, 基础步骤: {base_step}){advanced_mark}\n"
                configuration_order_text += f"   - **配置类型**: {command_hint}\n"
                configuration_order_text += f"   - **协议类型**: {protocol_type}\n"
                configuration_order_text += f"   - **产品模块**: {module}\n"
                if command_patterns:
                    configuration_order_text += f"   - **命令模式**: 查找包含以下关键词的命令 - {', '.join(command_patterns)}\n"
                if command_example:
                    configuration_order_text += f"   - **命令示例**: {command_example}\n"
                configuration_order_text += f"   - **操作**: \n"
                configuration_order_text += f"     1. 首先从Context中查找与此步骤相关的配置命令（查找包含命令模式关键词的命令）\n"
                configuration_order_text += f"     2. 如果找不到，根据步骤类型、协议类型({protocol_type})和产品模块({module})推断配置命令\n"
                configuration_order_text += f"     3. 确保生成的命令符合命令模式要求\n"
                configuration_order_text += f"   - **要求**: 必须生成至少1个配置命令，命令必须包含协议类型{protocol_type}和模块{module}的信息\n\n"
            
            configuration_order_text += "**CRITICAL规则**：\n"
            configuration_order_text += "1. **必须为每个步骤生成对应的配置命令**，不能跳过任何步骤\n"
            configuration_order_text += "2. **配置命令必须按照上述顺序排列**，先基础配置，后高级功能\n"
            configuration_order_text += "3. **每个步骤至少生成1个配置命令**，如果Context中没有直接示例，根据步骤类型推断\n"
            configuration_order_text += "4. **配置命令必须包含协议类型和产品模块信息**\n"
            configuration_order_text += "5. **验证命令应该覆盖所有配置步骤**，确保每个配置步骤都有对应的验证命令\n"
            configuration_order_text += "6. **输出格式**: config_commands数组中的命令必须按照步骤顺序排列，每个步骤的命令分组在一起\n\n"

    # 自动检测内容匹配健康检查场景，注入强制性提示
    _CONTENT_HEALTH_KEYWORDS = [
        "内容", "关键词", "字符串匹配", "content", "keyword", "响应内容",
        "页面内容", "chinamobile", "content_based_health_check", "string_match_health_check",
    ]
    content_health_warning = ""
    if decomposition_result:
        advanced = decomposition_result.get("advanced_features", [])
        adv_str = " ".join(str(a) for a in advanced).lower() if isinstance(advanced, list) else ""
        check_str = job_content.lower() + " " + adv_str
        if any(kw.lower() in check_str for kw in _CONTENT_HEALTH_KEYWORDS):
            content_health_warning = (
                "\n\n[⚠️ 强制规则：检测到HTTP响应内容关键词匹配需求]\n"
                "本任务需要检查 HTTP 响应体中是否包含特定字符串（关键词）。\n"
                "【禁止】将响应关键词字符串填入 slb health 命令的 expected_codes 参数！\n"
                "【必须】使用以下4步完整配置法：\n"
                "  第1步: health request 1 \"GET /index.html HTTP/1.0\\r\\n\\r\\n\"\n"
                "  第2步: health response 1 \"<从任务中提取的关键词字符串>\"\n"
                "  第3步: slb health \"<hc_name>\" http <interval> <timeout> <up> <down>\n"
                "         （这里不加 method/url/expected_codes 参数）\n"
                "  第4步: health server \"<hc_name>\" 1 1\n"
                "再配合: slb real http / slb group method / slb group member / slb group health / slb policy default\n"
                "Context 中如有 health request/health response/health server 命令，必须使用这些命令！\n"
            )

    return (
        "根据任务内容、功能树结构和配置顺序（最小生成树）提供配置命令和核对命令。"
        "严格输出 JSON，不要包含多余文本。"
        "config_commands 用于下发配置，verify_commands 用于核对测试。"
        f"{content_health_warning}"
        f"{configuration_order_text}"
        f"{constraints_text}"
        f"\n\n任务内容：\n{job_content}"
        f"\n\nContext:\n{context}"
        f"\n\nEnv Context:\n{env_context}"
    )
