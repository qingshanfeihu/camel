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
"""AI-driven cleanup agent: 根据本次下发的配置和设备当前状态，
自动生成完整的反向清理命令（单次 LLM 调用，无需工具）。"""

from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend

# ---------------------------------------------------------------------------
# NSAE CLI 删除命令参考（与 lb_ops_agent 保持一致）
# ---------------------------------------------------------------------------
_NSAE_DELETE_REFERENCE = """\
【NSAE CLI 删除/清理命令参考 — 必须严格遵循此语法】

进入模式: enable → config terminal

1. 虚拟服务:
   禁用:  slb virtual disable "<name>"
   解绑:  no slb policy default "<virtual_name>"
   删除:  no slb virtual http "<name>"

2. 服务器组:
   移除成员:    no slb group member "<group_name>" "<real_name>"
   解除健康检查: no slb group health "<group_name>" "<health_name>"
   删除算法:    (删除组的所有成员后组即消失，无需单独删除 method)

3. 健康检查:
   解绑 health server: no health server "<hc_name>"
   删除健康检查:       no slb health "<hc_name>"
   删除自定义请求:     no health request <idx>
   删除自定义响应:     no health response <idx>

4. 真实服务器:
   删除: no slb real http "<name>"

5. 网络基础:
   删除接口IP:   no ip address <interface_name>
   删除静态路由: no ip route static <dest_ip> <mask> <gateway>

6. LLB:
   删除链路: no llb link "<name>"
   删除远程站点: no llb rsite "<name>"
   关闭DNS代理: llb dnsproxy off

7. SSL:
   停用证书: ssl deactivate certificate "<cert_name>"

8. NAT:
   删除静态NAT: no nat static <real_ip> <mapped_ip>
   删除端口映射: no nat port <real_ip> <mapped_ip> <port> <protocol>

【清理顺序（依赖关系逆序 — CRITICAL）】
必须按照以下顺序删除，否则会因依赖关系导致删除失败:
1. slb policy default → 解除虚拟服务与组的绑定
2. slb virtual disable → 禁用虚拟服务
3. slb group member → 移除组成员
4. slb group health → 解除组的健康检查绑定
5. health server → 解除 health request/response 绑定
6. slb health → 删除健康检查
7. health request / health response → 删除自定义请求/响应
8. slb real http → 删除真实服务器
9. slb virtual http → 删除虚拟服务定义
10. ip route static → 删除静态路由（如有）
11. ip address → 删除接口IP（如有）

【输出格式】
严格输出 JSON:
{
  "cleanup_commands": ["cmd1", "cmd2", ...],
  "notes": "简要说明清理了哪些资源"
}
cleanup_commands 数组中的命令必须按照上述清理顺序排列。
如果某类资源不存在，跳过即可（不要生成空命令）。
"""


def build_cleanup_agent(model: BaseModelBackend) -> ChatAgent:
    r"""构建 AI 驱动的配置清理 Agent。

    该 Agent 根据本次测试下发的配置命令和设备当前状态，
    自动推理并生成完整的反向清理命令。

    Args:
        model: LLM 模型后端。

    Returns:
        配置好的 ChatAgent 实例。
    """
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Config Cleanup",
            content=(
                "你是 InfosecOS NSAE 设备配置清理专家。\n"
                "你的任务是：根据本次测试下发的配置命令和设备当前运行状态，\n"
                "生成完整的反向清理命令，将设备恢复到测试前的状态。\n\n"
                "关键原则:\n"
                "1. 必须清理所有本次测试创建的资源（包括 SLB、网络IP、路由等）\n"
                "2. 如果设备 show 输出中有资源但 config_commands 中没有，\n"
                "   说明是之前残留的——也要清理\n"
                "3. 清理命令必须按照依赖关系逆序排列\n"
                "4. 不要清理设备的管理接口（port1）和默认路由\n"
                "5. 设备 port4 的 IP 27.16.9.52/24 是预设的，不要删除\n"
                "6. 清理时 'not found' 类的错误是正常的，无需担心\n\n"
                + _NSAE_DELETE_REFERENCE
            ),
        ),
        model=model,
    )


def build_cleanup_prompt(
    config_commands: List[str],
    device_state: Dict[str, str],
    env_plan: Optional[Dict[str, Any]] = None,
) -> str:
    r"""构建清理 Agent 的输入提示词。

    Args:
        config_commands: 本次测试下发的配置命令列表。
        device_state: 设备当前状态（show 命令的输出字典），
            key 为 show 命令名，value 为输出文本。
        env_plan: 环境规划（可选），用于确认网络配置细节。

    Returns:
        完整的提示词字符串。
    """
    # 格式化 config_commands
    if config_commands:
        cmds_text = "\n".join(f"  {i}. {cmd}" for i, cmd in enumerate(config_commands, 1))
    else:
        cmds_text = "  （无配置命令记录）"

    # 格式化设备当前状态
    state_parts = []
    for cmd, output in device_state.items():
        state_parts.append(f"--- {cmd} ---\n{output}")
    state_text = "\n\n".join(state_parts) if state_parts else "（无设备状态信息）"

    # 提取 env_plan 中的关键信息
    env_hint = ""
    if env_plan:
        ev = env_plan.get("env", {})
        protected_items = []
        # port4 IP 是预设的，不要删除
        protected_items.append("设备 port4 IP: 27.16.9.52/24 (预设，不要删除)")
        # port1 管理口
        device_ip = ev.get("LB_DEVICE_IP", "")
        protected_items.append(f"设备 port1 管理IP: {device_ip} (管理口，不要删除)")

        env_hint = (
            "\n\n[保护项 — 以下资源不要清理]\n"
            + "\n".join(f"- {item}" for item in protected_items)
        )

    return (
        "请根据以下信息生成完整的清理命令。\n"
        "严格输出 JSON 格式: {\"cleanup_commands\": [...], \"notes\": \"...\"}\n\n"
        f"[本次测试下发的配置命令]\n{cmds_text}\n\n"
        f"[设备当前状态（show 命令输出）]\n{state_text}"
        f"{env_hint}\n\n"
        "请分析上述信息，生成完整的反向清理命令列表。\n"
        "注意：\n"
        "1. 不仅要清理 config_commands 中的资源，还要检查设备当前状态中\n"
        "   是否有额外需要清理的测试残留资源\n"
        "2. 命令必须按照依赖关系逆序排列（先解绑、再删除）\n"
        "3. 网络配置（ip address、ip route）如果是本次测试添加的也要清理\n"
        "   （但不要删除 port1 管理口和 port4 预设IP）"
    )
