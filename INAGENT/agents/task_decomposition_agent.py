# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the "License" is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Task Decomposition Agent: 负责分析任务需求，基于配置逻辑树分解任务并生成精确的 RAG 查询
"""
from typing import Dict, List, Any, Optional
from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend


def build_task_decomposition_agent(model: BaseModelBackend) -> ChatAgent:
    r"""Build the task decomposition agent.

    Args:
        model (BaseModelBackend): The model backend.

    Returns:
        ChatAgent: Configured task decomposition agent.
    """
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Task Decomposition Specialist",
            content=(
                "你是任务分解专家，负责分析配置需求并生成精确的 RAG 检索查询。"
                "你了解负载均衡器的配置逻辑树结构：基础配置步骤 + 高级功能 + 协议类型。"
                "配置逻辑树结构会通过 function_structure_index 提供给你，包含所有产品模块、协议类型、步骤类型和场景定义。"
                "对于复杂需求，你需要："
                "1. 识别基础配置步骤（required_steps）- 从索引中获取可用步骤类型"
                "2. 识别高级功能需求（如会话保持、QoS等）- 从索引中获取可用高级功能"
                "3. 识别协议类型 - 从索引中获取可用协议类型"
                "4. 识别产品模块 - 从索引中获取可用产品模块"
                "5. 为每个配置步骤生成精确的 RAG 查询"
                "输出严格 JSON 格式，包含："
                "- scenario_id: 场景ID（从索引中的场景列表匹配，如果无法匹配则为 'unknown'）"
                "- product_modules: 产品模块列表（从索引中获取）"
                "- protocol_type: 协议类型（从索引中获取）"
                "- required_steps: 必需的基础配置步骤列表（从索引中获取可用步骤类型）"
                "- advanced_features: 高级功能列表（从索引中获取）"
                "- rag_queries: 为每个配置步骤生成的精确 RAG 查询列表，每个查询包含："
                "  - step_type: 步骤类型（从索引中获取）"
                "  - feature: 功能名称（如 'cookie_persistence'）"
                "  - query: RAG 查询字符串（应包含产品模块、协议类型和步骤类型信息）"
                "  - priority: 优先级（1-5，1最高）"
            ),
        ),
        model=model,
    )


def build_task_decomposition_prompt(
    job_content: str,
    function_structure_index: Optional[Dict[str, Any]] = None,
) -> str:
    r"""Build the prompt for the task decomposition agent.

    Args:
        job_content (str): Raw job/task content.
        function_structure_index (Optional[Dict[str, Any]]): Function structure index.

    Returns:
        str: Prompt string.
    """
    index_info = ""
    if function_structure_index:
        # 1. 提取步骤类型列表（从metadata_statistics中）
        metadata_stats = function_structure_index.get("metadata_statistics", {})
        step_types = metadata_stats.get("step_types", {})
        step_types_list = list(step_types.keys())
        
        # 2. 提取协议类型列表（从metadata_statistics中）
        protocol_types = metadata_stats.get("protocol_types", {})
        protocol_types_list = list(protocol_types.keys())
        
        # 3. 提取产品模块列表（从metadata_statistics和modules中）
        product_modules_stats = metadata_stats.get("product_modules", {})
        modules_info = function_structure_index.get("modules", {})
        product_modules_list = list(set(list(product_modules_stats.keys()) + list(modules_info.keys())))
        
        # 4. 提取场景列表
        scenarios = function_structure_index.get("scenarios", {})
        scenario_list = []
        for scenario_id, scenario_info in list(scenarios.items()):
            scenario_list.append(
                f"- {scenario_id}: "
                f"product_modules={scenario_info.get('product_modules', [])}, "
                f"protocol_types={scenario_info.get('protocol_types', [])}, "
                f"required_steps={scenario_info.get('required_steps', [])}"
            )
        
        # 构建索引信息
        index_parts = []
        
        if step_types_list:
            index_parts.append(
                f"\n[可用步骤类型]（CRITICAL: 只能使用以下步骤类型，不要自己推断）\n"
                f"{', '.join(step_types_list)}"
            )
        
        if protocol_types_list:
            index_parts.append(
                f"\n[可用协议类型]（CRITICAL: 只能使用以下协议类型）\n"
                f"{', '.join(protocol_types_list)}"
            )
        
        if product_modules_list:
            index_parts.append(
                f"\n[可用产品模块]（CRITICAL: 只能使用以下产品模块）\n"
                f"{', '.join(product_modules_list)}"
            )
        
        if scenario_list:
            index_parts.append(
                f"\n[可用场景定义]（如果任务匹配某个场景，使用该场景的required_steps）\n"
                + "\n".join(scenario_list)
            )
        
        if index_parts:
            # 构建模块的标准步骤类型映射（带模块前缀）
            module_standard_steps = {}
            if modules_info:
                for module_name, module_data in modules_info.items():
                    step_types = module_data.get("step_types", {})
                    # 提取基础步骤类型（去除模块前缀）
                    base_steps = set()
                    for prefixed_step, step_info in step_types.items():
                        base_step = step_info.get("base_step_type", "")
                        if base_step:
                            base_steps.add(base_step)
                    module_standard_steps[module_name] = sorted(list(base_steps))
            
            # 各产品模块的标准配置步骤顺序（按最小生成树排列）
            _MODULE_STANDARD_STEPS = {
                "SLB": [
                    ("virtual_services", "虚拟服务配置", "必须先配置"),
                    ("backend_servers", "后台服务器配置", "然后配置"),
                    ("health_checks", "健康检查配置", "可选但建议"),
                    ("policies_and_algorithms", "策略和算法配置", "高级功能，可选"),
                ],
                "LLB": [
                    ("basic_config", "链路基本配置（llb link）", "必须先配置"),
                    ("health_checks", "链路健康检查（llb link health）", "建议配置"),
                    ("policies_and_algorithms", "出入站算法（llb method）", "按需配置"),
                    ("dns_config", "DNS代理/监听（llb dnsproxy/dns）", "按需配置"),
                ],
                "高可用": [
                    ("high_availability", "HA组配置（ha group）", "必须先配置"),
                    ("basic_config", "HA基础参数（ha decision/floatmac）", "按需配置"),
                    ("health_checks", "HA健康检查", "可选"),
                ],
                "安全": [
                    ("ssl_config", "SSL证书导入与激活", "加密协议必须"),
                    ("basic_config", "安全基础配置", "按需配置"),
                    ("acl_config", "访问控制列表（ACL）", "按需配置"),
                    ("health_checks", "安全模块健康检查", "可选"),
                ],
            }

            standard_steps_text = ""
            if module_standard_steps:
                standard_steps_text = "\n\n[各模块的标准配置步骤]（CRITICAL: 必须根据任务涉及的产品模块选择对应的标准步骤）\n"
                for module_name, steps in module_standard_steps.items():
                    if steps:
                        standard_steps_text += f"- {module_name}: {', '.join(steps)}\n"

                # 为每个有标准步骤顺序的模块输出最小生成树
                for mod_name, mod_steps in _MODULE_STANDARD_STEPS.items():
                    standard_steps_text += f"\n**{mod_name}配置的标准步骤顺序**（最小生成树）：\n"
                    for i, (step, desc, note) in enumerate(mod_steps, 1):
                        standard_steps_text += f"{i}. {step} ({desc}) - {note}\n"

                standard_steps_text += "\n**注意**：\n"
                standard_steps_text += "- 对于SLB配置，network_basics通常不是必需的，除非任务明确要求网络基础配置。\n"
                standard_steps_text += "- 对于LLB配置，必须使用llb_前缀步骤（如llb_basic_config、llb_health_checks）。\n"
                standard_steps_text += "- 对于SSL相关配置，使用security_前缀步骤（如security_ssl_config）。\n"
                standard_steps_text += "- 请根据任务实际涉及的产品模块选择对应的标准步骤，不要默认使用SLB步骤。\n"
            
            index_info = (
                "\n\n[配置逻辑树参考]"
                + "".join(index_parts)
                + standard_steps_text
                + "\n\n[CRITICAL规则]"
                + "\n1. required_steps必须使用带模块前缀的格式（如slb_virtual_services、llb_basic_config、security_ssl_config），不能使用不带前缀的基础步骤类型"
                + "\n2. protocol_type必须从上述'可用协议类型'中选择"
                + "\n3. product_modules必须从上述'可用产品模块'中选择"
                + "\n4. **配置任务必须使用对应模块的标准步骤**：SLB→slb_前缀、LLB→llb_前缀、高可用→高可用_前缀、安全→security_前缀"
                + "\n5. 如果任务不匹配任何场景，根据识别到的产品模块使用对应模块的标准配置步骤组合"
                + "\n6. 根据协议类型确定是否需要SSL相关步骤：只有HTTPS、TCPS等加密协议才需要SSL证书步骤"
                + "\n7. **不要使用network_basics作为配置的required_steps**，除非任务明确要求网络基础配置"
            )

    return (
        "请分析以下配置任务，基于配置逻辑树结构生成精确的 RAG 检索查询。"
        f"{index_info}"
        "\n\n[任务内容]\n"
        f"{job_content}"
        "\n\n[要求]"
        "\n1. 识别任务涉及的基础配置步骤（required_steps）- 必须从索引中的'可用步骤类型'选择"
        "\n2. 识别任务涉及的高级功能（如会话保持、QoS、重定向等）- 这些通常属于policies_and_algorithms步骤类型"
        "\n3. 识别协议类型 - 必须从索引中的'可用协议类型'选择"
        "\n4. 识别产品模块 - 必须从索引中的'可用产品模块'选择"
        "\n5. 为每个配置步骤生成精确的 RAG 查询，确保能检索到具体的配置命令和示例"
        "\n6. 对于高级功能，生成专门的查询（如 'cookie persistence configuration example'）"
        "\n\n[CRITICAL: 根据产品模块选择标准步骤顺序]"
        "\n请根据任务涉及的产品模块，从上方[各模块的标准配置步骤]中选择对应的标准步骤顺序。"
        "\n示例："
        "\n- SLB配置任务 → slb_virtual_services, slb_backend_servers, slb_health_checks, slb_policies_and_algorithms"
        "\n- LLB配置任务 → llb_basic_config, llb_health_checks, llb_policies_and_algorithms"
        "\n- SSL/安全任务 → security_ssl_config, security_basic_config"
        "\n- HA高可用任务 → 高可用_high_availability, 高可用_basic_config"
        "\n\n**重要**："
        "\n- 如果任务匹配的场景定义中的required_steps不正确，你应该忽略场景定义，使用对应模块的标准步骤"
        "\n- required_steps必须使用带模块前缀的格式（如slb_virtual_services、llb_basic_config），不能使用不带前缀的基础步骤类型"
        "\n- network_basics通常不是必需的，除非任务明确要求网络基础配置"
        "\n\n[重要提示]"
        "\n- 根据协议类型确定是否需要SSL相关步骤：只有HTTPS、TCPS等加密协议才需要SSL证书步骤"
        "\n- HTTP、TCP、UDP等非加密协议不需要SSL证书步骤"
        "\n\n[CRITICAL: HTTP内容/关键词匹配健康检查 - 专用RAG查询规则]"
        "\n当任务中包含以下任意特征时（内容匹配 / 关键词检查 / 字符串匹配 / 响应内容 / 页面内容包含 / content-based），"
        "\n**必须**在 rag_queries 中额外增加以下3条专用查询（priority=1，step_type=health_checks）："
        "\n  1. query: 'HTTP健康检查内容关键词匹配命令 health request health response health server 三步配置法 自定义请求字符串和期望响应内容'"
        "\n  2. query: 'Web页面关键词检查 health response 期望响应内容 health request 自定义GET请求 health server 绑定命令完整配置示例'"
        "\n  3. query: 'health server group_hc_name 绑定 health request health response 配置HTTP响应体内容匹配健康检查'"
        "\n这3条查询必须出现在 rag_queries 数组中，用于检索 health request/health response/health server 子命令的具体语法。"
        "\n\n请输出 JSON 格式，包含 scenario_id, required_steps, advanced_features, protocol_type, product_modules, rag_queries。"
    )
