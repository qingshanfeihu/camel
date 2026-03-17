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
from typing import Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend


def build_task_analysis_agent(model: BaseModelBackend) -> ChatAgent:
    r"""Build the task analysis agent (QA/Verifier).

    Args:
        model (BaseModelBackend): The model backend.

    Returns:
        ChatAgent: Configured task analysis agent.
    """
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Task Analyst",
            content=(
                "你负责对任务执行结果进行综合分析与验证。"
                "请根据环境搭建情况、配置下发日志、核对命令输出以及测试步骤执行记录，"
                "判断任务是否成功，并输出简明扼要的分析报告。"
                "请将最终分析文本放在 JSON 的 content 字段中。\n\n"
                "【重要设备显示注意事项 — 必须严格遵守】\n\n"
                "1. **`show slb health` 的显示局限（关键）**:\n"
                "   此命令仅显示健康检查策略的基础摘要行，固定格式为：\n"
                '   slb health "<name>" http <interval> <timeout> <up> <down> GET "/" "200"\n'
                "   **末尾的 GET/\"/\"200\" 永远是默认值，不会因为配置了 health request/response/server 而改变！**\n"
                "   内容健康检查（4步配置法）使用 health request + health response + health server 三条子命令，\n"
                "   这些子命令的详细参数（自定义URL路径、期望响应关键词）**永远不出现在 show slb health 输出中**。\n"
                "   ★ 结论：绝对不能因为 show slb health 输出中看到 '\"200\"' 或 GET \"/\" 就判定内容匹配健康检查未生效。\n"
                "   ★ 判定健康检查是否生效的正确方法：查看 deploy_logs 中 health request / health response / health server 命令是否执行成功。\n\n"
                "2. **deploy_logs 生效判定**:\n"
                "   如果部署日志中以下三条命令均执行成功（返回 NSAE(config)# 无错误），\n"
                "   则内容健康检查已正确配置，应判定为成功：\n"
                '   - health request <id> "GET /<path> HTTP/1.0\\r\\n\\r\\n"\n'
                '   - health response <id> "<关键词>"\n'
                '   - health server "<hc_name>" <request_id> <response_id>\n\n'
                "3. **`show slb real http` 的 UP/DOWN 状态**:\n"
                "   故障注入阶段会查询此命令，输出包含每个 real server 的运行状态。\n"
                "   - 步骤A（停止服务）后、步骤C（去除关键词）后：应看到对应 server 状态为 DOWN\n"
                "   - 步骤B（恢复服务）后、步骤D（恢复内容）后：应看到对应 server 状态为 UP\n"
                "   如果能看到 UP/DOWN 状态变化，则证明故障注入验证成功。\n"
                "   如果 show slb real http 输出中看不到状态字段，不应仅凭此判定失败。\n\n"
                "4. **VIP 冲突处理**:\n"
                "   如果日志中出现 'already has this ip and port' 且被标记为 warning，\n"
                "   说明系统已自动复用已有虚拟服务，这不算配置失败。\n"
                "   流量验证使用的VIP可能与任务原文中的VIP不同（因为复用了已有虚拟服务），这是正常的。\n\n"
                "5. **组成员(group member)说明**:\n"
                "   设备上的 SLB 组可能已包含多个 real server 成员（历史配置），\n"
                "   本次测试只需验证新添加的成员和健康检查配置是否正确，\n"
                "   不需要因为组中有额外的历史成员就判定为错误。\n"
            ),
        ),
        model=model,
    )


def build_task_analysis_prompt(
    task_content: str,
    env_summary: str,
    config_commands: List[str],
    deploy_summary: str,
    deploy_logs: str,
    step_records: List[str],
    verify_summary: str,
    verify_logs: str,
    traffic_summary: str = "",
    traffic_logs: Optional[List[str]] = None,
    fault_injection_info: Optional[Dict] = None,
    precheck_info: Optional[Dict] = None,
) -> str:
    r"""Build the prompt for the task analysis agent.

    Args:
        task_content (str): The original task content/instruction.
        env_summary (str): Summary of environment setup.
        config_commands (List[str]): List of configuration commands executed.
        deploy_summary (str): Summary of deployment (config execution).
        deploy_logs (str): Detailed logs from deployment.
        step_records (List[str]): Records of operational steps executed.
        verify_summary (str): Summary of verification commands.
        verify_logs (str): Detailed logs from verification.
        traffic_summary (str): Summary of traffic verification results.
        traffic_logs (Optional[List[str]]): Detailed traffic verification logs.
        fault_injection_info (Optional[Dict]): Fault injection test results.
        precheck_info (Optional[Dict]): VIP pre-check info.

    Returns:
        str: Prompt string.
    """
    cmds_str = "\n".join(config_commands) if config_commands else "无"
    steps_str = "\n".join(step_records) if step_records else "无"
    traffic_logs_str = "\n".join(traffic_logs) if traffic_logs else "无"

    prompt = (
        "请分析以下任务执行全过程，给出最终结论（成功/失败）及原因分析。\n"
        f"\n[任务目标]\n{task_content}"
        f"\n\n[环境搭建]\n{env_summary}"
        f"\n\n[配置下发]\n命令列表：\n{cmds_str}\n\n下发结果摘要：\n{deploy_summary}\n\n详细日志：\n{deploy_logs}"
        f"\n\n[测试步骤执行]\n{steps_str}"
    )

    # VIP 预检查信息
    if precheck_info and precheck_info.get("checked"):
        prompt += "\n\n[VIP 预检查]"
        if precheck_info.get("conflict"):
            prompt += (
                f"\n发现VIP冲突，已自动复用已有虚拟服务 '{precheck_info.get('reuse_vs', '')}'。"
                "\n这是自动化系统的正常处理，不算配置错误。"
            )
        else:
            prompt += "\n未发现VIP冲突。"
        existing_vs = precheck_info.get("existing_vs", {})
        if existing_vs:
            prompt += "\n设备已有虚拟服务：\n"
            for name, info in existing_vs.items():
                prompt += f"  {name}: {info.get('vip', '')}:{info.get('port', '')}\n"

    # 添加流量验证段（如有）
    if traffic_summary:
        prompt += (
            f"\n\n[流量验证（HTTP 端到端）]"
            f"\n摘要：{traffic_summary}"
            f"\n详细日志：\n{traffic_logs_str}"
        )

    # 添加故障注入段（如有）
    if fault_injection_info and not fault_injection_info.get("skipped"):
        prompt += "\n\n[故障注入与恢复验证]"
        prompt += f"\n故障检测等待时间: {fault_injection_info.get('fault_wait_seconds', 'N/A')}秒"
        prompt += f"\n恢复检测等待时间: {fault_injection_info.get('recovery_wait_seconds', 'N/A')}秒"
        for step in fault_injection_info.get("steps", []):
            step_id = step.get("step", "?")
            action = step.get("action", "")
            expected = step.get("expected", "")
            prompt += f"\n\n步骤{step_id}: {action}"
            prompt += f"\n  预期结果: {expected}"
            # 本地探测
            for probe_key in ("local_probe_after_stop", "local_probe_after_restore", "local_probe_after_modify"):
                if probe_key in step:
                    probe = step[probe_key]
                    probe_status = "可达" if probe.get("success") else f"不可达({probe.get('error', '')})"
                    content_match = probe.get("content_match", "N/A")
                    prompt += f"\n  本地HTTP探测: {probe_status}, 内容匹配: {content_match}"
            # SSH 健康状态
            if "health_status" in step:
                hs = step["health_status"]
                for cmd, output in hs.get("outputs", {}).items():
                    prompt += f"\n  SSH [{cmd}]:\n{output}"

    prompt += (
        f"\n\n[最终核对]\n核对摘要：\n{verify_summary}\n\n核对日志：\n{verify_logs}"
        "\n\n请输出最终分析报告。"
    )

    return prompt
