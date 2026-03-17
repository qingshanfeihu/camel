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
import os

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend


def _build_network_topology_hint() -> str:
    """根据 .env 中的 VM/设备信息构建网络拓扑描述供 Agent 参考。"""
    device_ip = os.environ.get("LB_DEVICE_IP", "未知")
    vm_mgmt = os.environ.get("VM_MGMT_IP", "")
    rs_subnet = os.environ.get("LB_REAL_SERVER_SUBNET", "N/A")
    vip_subnet = os.environ.get("LB_VIP_SUBNET", "N/A")
    if not vm_mgmt:
        return ""
    return (
        "\n\n[网络拓扑 — 必须严格遵守]\n"
        f"- 负载均衡设备管理IP: {device_ip}\n"
        f"  - port1: {device_ip}/24 (管理口, 仅管理流量)\n"
        "  - port3: 与 VM eth1 同 VLAN (服务器侧业务网络)\n"
        "  - port4: 27.16.9.52/24, 与 VM eth2 同 VLAN (客户端侧业务网络)\n"
        f"- 测试虚拟机管理IP: {vm_mgmt} (仅管理面)\n"
        "  - eth0: 管理口 (172.16.6.x, 不用于业务)\n"
        "  - eth1: 服务器侧 (与设备 port3 同 VLAN, 用于 real server HTTP 后端)\n"
        "  - eth2: 客户端侧 (与设备 port4 同 VLAN, 用于 VIP 访问和流量验证)\n"
        f"- .env LB_REAL_SERVER_SUBNET: {rs_subnet}\n"
        f"- .env LB_VIP_SUBNET: {vip_subnet}\n"
        "\n[关键规则]\n"
        "1. 172.16.6.x 网段仅用于管理，业务流量禁止经过该网段\n"
        "2. LB_REAL_SERVER_IP 必须是 VM eth1 的业务 IP (需要你规划并分配)\n"
        "3. LB_VIP 必须绑定在设备 port4 上 (客户端侧)，且在 port4 的子网内 (27.16.9.0/24)\n"
        "4. 设备 port3 和 VM eth1 需要在同一子网 (你来规划 IP 段)\n"
        "5. 设备 port4 已有 IP 27.16.9.52/24, 所以 VIP 应在 27.16.9.0/24 段内\n"
        "6. VM eth2 的客户端 IP 也应在 27.16.9.0/24 段内\n"
        "7. env 中必须额外给出以下字段:\n"
        "   - VM_ETH1_IP: VM 服务器侧 eth1 的 IP\n"
        "   - VM_ETH1_MASK: eth1 子网掩码\n"
        "   - VM_ETH2_IP: VM 客户端侧 eth2 的 IP\n"
        "   - VM_ETH2_MASK: eth2 子网掩码\n"
        "   - DEVICE_PORT3_IP: 设备 port3 需要配置的 IP (与 eth1 同子网)\n"
        "   - DEVICE_PORT3_MASK: port3 子网掩码\n"
    )


def build_env_setup_agent(model: BaseModelBackend) -> ChatAgent:
    r"""Build the network environment setup agent.

    Args:
        model (BaseModelBackend): The model backend.

    Returns:
        ChatAgent: Configured environment setup agent.
    """
    topology_hint = _build_network_topology_hint()
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Env Builder",
            content=(
                "你负责构造负载均衡测试环境。"
                "请输出 TaskResult JSON，字段仅包含 content 与 failed。"
                "content 必须是字符串，内容为 JSON，且仅包含以下键："
                "port, env, health_check_interval, health_check_timeout, "
                "health_check_retries, subnet_config。"
                "\n\n## env 字段规范\n"
                "env 必须包含以下基础字段:\n"
                "  LB_VIP, LB_VIP_PORT, LB_VIRTUAL_SERVICE, LB_GROUP_NAME,\n"
                "  VM_ETH1_IP, VM_ETH1_MASK, VM_ETH2_IP, VM_ETH2_MASK,\n"
                "  DEVICE_PORT3_IP, DEVICE_PORT3_MASK。\n"
                "\n### 后端服务器 (Real Server) 规划 — 必须遵循\n"
                "负载均衡器通过 IP + 端口 区分不同后端服务器。\n"
                "所有后端服务器 HTTP 服务运行在同一台 VM 上，用【不同端口】模拟多台服务器。\n\n"
                "**单服务器场景**（任务只需 1 台后端服务器）:\n"
                "  env 里给出 LB_REAL_SERVER_IP, LB_REAL_SERVER_PORT。\n\n"
                "**多服务器场景**（任务需要 2 台及以上后端服务器）:\n"
                "  env 里给出:\n"
                "  - LB_REAL_SERVER_IP: VM eth1 的主 IP（所有后端共用此 IP）\n"
                "  - LB_REAL_SERVER_PORT: 第一台服务器的端口（如 80）\n"
                "  - LB_REAL_SERVERS: 数组，每个元素包含:\n"
                '    {"name": "rs1", "ip": "<同 LB_REAL_SERVER_IP>", "port": 80}\n'
                '    {"name": "rs2", "ip": "<同 LB_REAL_SERVER_IP>", "port": 8081}\n'
                '    {"name": "rs3", "ip": "<同 LB_REAL_SERVER_IP>", "port": 8082}\n'
                "  端口规划规则: 第1台用 80，后续依次 8081、8082、8083...\n"
                "  所有服务器的 IP 必须相同（= LB_REAL_SERVER_IP = VM_ETH1_IP）\n\n"
                "注意：LB_REAL_SERVER_IP 必须是 VM eth1 的业务 IP（不是管理IP，不是 127.0.0.1）；"
                "LB_VIP 必须在设备 port4 的子网内 (27.16.9.0/24)。"
                "如设置了LB_REAL_SERVER_SUBNET/LB_VIP_SUBNET，必须按子网取值。"
                "subnet_config 必须包含 LB_REAL_SERVER_SUBNET 与 LB_VIP_SUBNET。"
                + topology_hint
                + "\n\n关键提示："
                "1. 如果任务需要特定配置文件且路径未给出，请务必先尝试使用 list_files 查找，不要直接报错。"
                "2. 构造环境时，优先复用已有的配置信息。"
                "3. 不要使用 127.0.0.1 或 localhost 作为任何业务 IP。"
                "4. 多服务器场景下，所有 real server 的 IP 都相同，用不同端口区分！"
            ),
        ),
        model=model,
    )


def build_env_setup_prompt(task_instruction: str, context_info: str = "") -> str:
    r"""Build the prompt for the environment setup agent.

    Args:
        task_instruction (str): Specific task instruction for Env setup.
        context_info (str): Additional context (e.g., file paths).

    Returns:
        str: Prompt string.
    """
    return (
        "请根据以下具体指令和上下文规划测试环境。"
        "请仅输出 TaskResult JSON，content 内嵌环境 JSON 字符串。"
        "content JSON 中可以包含 html_content 字段（HTML 字符串），"
        "用于指定 HTTP 后端返回的页面内容；若不提供则使用默认内容。"
        "\n\n重要：LB_REAL_SERVER_IP 必须是 VM eth1 的业务 IP（服务器侧），"
        "不能是 127.0.0.1 或管理口 IP。LB_VIP 必须在设备 port4 子网 (27.16.9.0/24) 内。"
        "\n\n多服务器规划规则：如果任务需要多台后端服务器（如 3 台），"
        "所有服务器共用同一 VM eth1 IP，用不同端口区分。"
        "必须在 env 中给出 LB_REAL_SERVERS 数组，格式示例："
        '\n  [{"name":"rs1","ip":"10.0.3.10","port":80},'
        '{"name":"rs2","ip":"10.0.3.10","port":8081},'
        '{"name":"rs3","ip":"10.0.3.10","port":8082}]'
        f"\n\n环境搭建指令：\n{task_instruction}"
        f"\n\n上下文信息（文件列表/配置）：\n{context_info}"
    )
