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
"""Test Plan Agent — 从任务文本自动提取测试计划参数，消除硬编码。

该 Agent 分析 job 文本，输出一份结构化 JSON（TestPlan），供 pipeline
后续阶段（环境搭建、流量验证、故障注入、SSH 状态查询、AI 分析）直接消费。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import BaseModelBackend

logger = logging.getLogger(__name__)

# ── 默认测试计划（当 AI 解析失败时使用的保底值） ─────────────────────
DEFAULT_TEST_PLAN: Dict[str, Any] = {
    "keyword": "",
    "keyword_replacement": "",
    "html_page_path": "/index.html",
    "html_content_must_contain": "",
    "default_html_template": "<html><body><h1>{keyword}</h1></body></html>",
    "health_check_interval": 15,
    "health_check_retries": 3,
    "health_check_timeout": 5,
    "buffer_seconds": 10,
    "fault_injection_steps": [],
    "status_check_commands": [],
    "verify_show_commands": [],
    "product_module": "slb",
    "protocol_type": "http",
}

# ── 故障注入步骤类型定义 ─────────────────────────────────────────────
# pipeline 引擎根据 type 分派具体操作
STEP_TYPES = {
    "stop_service",       # 停止后端服务
    "restore_service",    # 恢复后端服务
    "modify_content",     # 修改页面内容（去除/替换关键词）
    "restore_content",    # 恢复页面原始内容
    "device_command",     # 在设备上执行 CLI 配置命令（如 disable real server）
}


# ── Agent 构造 ───────────────────────────────────────────────────────

def build_test_plan_agent(model: BaseModelBackend) -> ChatAgent:
    r"""Build the test plan extraction agent.

    This agent reads a job description and outputs a structured TestPlan
    JSON that parameterises every downstream pipeline stage.

    Args:
        model: The LLM backend.

    Returns:
        ChatAgent configured for test plan extraction.
    """
    return ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="Test Plan Extractor",
            content=_SYSTEM_PROMPT,
        ),
        model=model,
    )


_SYSTEM_PROMPT = """\
你是一个测试计划提取专家。你的任务是阅读网络设备测试需求文本，输出一份 **结构化 JSON**（TestPlan），
供自动化测试管线直接消费。你必须从任务文本中提取所有参数，**绝不能自行编造测试任务中未提及的值**。

输出 JSON 必须严格遵循以下 schema（所有字段必填）：

```json
{
  "keyword": "<str, 健康检查/内容匹配的关键词，如任务中的 chinamobile>",
  "keyword_replacement": "<str, 故障注入时关键词被替换为的内容，从任务步骤中提取>",
  "html_page_path": "<str, 健康检查请求的页面路径，如 /index.html>",
  "html_content_must_contain": "<str, HTML 页面必须包含的关键字符串，通常同 keyword>",
  "default_html_template": "<str, 默认 HTML 模板，{keyword} 是占位符>",
  "health_check_interval": <int, 健康检查间隔秒数>,
  "health_check_retries": <int, 连续失败多少次判定故障>,
  "health_check_timeout": <int, 单次检查超时秒数>,
  "buffer_seconds": <int, 等待时额外缓冲秒数，默认 10>,
  "fault_injection_steps": [
    {
      "id": "<str, 步骤标识如 A/B/C/D/E>",
      "type": "<str, stop_service|restore_service|modify_content|restore_content|device_command>",
      "description": "<str, 步骤描述>",
      "expected_status": "<str, UP 或 DOWN>",
      "params": {
        "<可选参数>": "<值, 如 remove_keyword / replace_with 等>"
      }
    }
  ],
  "status_check_commands": [
    "<str, 故障注入后查询设备状态的 show 命令，按产品模块确定>"
  ],
  "verify_show_commands": [
    "<str, 最终验证阶段用的 show 命令（可选，LB Ops Agent 也会生成）>"
  ],
  "product_module": "<str, 产品模块: slb / llb / ssl / ha / nat / 其他>",
  "protocol_type": "<str, 协议: http / https / tcp / udp / icmp / 其他>"
}
```

**提取规则**：
1. **keyword / keyword_replacement**: 从任务预置条件和测试步骤中提取。
   例如 "页面上有 chinamobile 字符串" → keyword = "chinamobile"。
   "将 chinamobile 变为 china mobile" → keyword_replacement = "china mobile"。
2. **health_check_interval / retries**: 从 "每N秒检查一次" "连续M次" 等描述中提取。
3. **fault_injection_steps**: 依据测试步骤逐步映射：
   - "停止HTTP服务" → type=stop_service, expected_status=DOWN
   - "恢复HTTP服务" → type=restore_service, expected_status=UP
   - "将关键词替换为xxx" → type=modify_content, params.remove_keyword=原词, params.replace_with=xxx, expected_status=根据任务判断
   - "恢复页面内容" → type=restore_content, expected_status=UP
   - "在设备上执行CLI命令"（如 软关机/disable real server/shutdown 等设备侧操作）→
     type=device_command, params.command="<设备CLI命令>"
     例如: 软关机 real server → params.command="slb real disable \"rs1\""
     例如: 恢复 real server → params.command="slb real enable \"rs1\""
     **重要**: 任何需要在设备上执行的操作（而非在后台VM上操作）都应使用 device_command。
   注意：如果任务中有多个修改步骤（如先改为 "china mobile" 再改为 "chinamobile123"），
   必须生成对应数量的 modify_content 步骤。
4. **status_check_commands**: 根据 product_module 和 protocol_type 推断。
   - SLB + HTTP: ["show slb group health", "show slb health", "show statistics slb real http"]
   - SLB + TCP: ["show slb group health", "show slb health", "show statistics slb real tcp"]
   - LLB: ["show llb link status", "show llb policy"]
   - HA: ["show ha status", "show vrrp"]
   - SSL: ["show ssl offload", "show statistics slb real http"]
   如果不确定，至少包含 "show running-config"。
5. **verify_show_commands**: 可留空数组 []，后续 LB Ops Agent 也会自动生成验证命令。
6. **expected_status 判断规则**:
   - 服务停止/关键词消失 → DOWN
   - 服务恢复/关键词正确包含 → UP
   - 特殊情况：如 "chinamobile123" 包含 "chinamobile" 子串 → 仍匹配 → UP

只输出 JSON，不要输出其他内容。
"""


# ── Prompt 构造 ──────────────────────────────────────────────────────

def build_test_plan_prompt(job_content: str) -> str:
    r"""Build the user prompt for the test plan agent.

    Args:
        job_content: Raw text of the test job / task description.

    Returns:
        Prompt string.
    """
    return (
        "请从以下测试任务文本中提取 TestPlan JSON。\n\n"
        f"【测试任务原文】\n{job_content}\n\n"
        "请严格按照 schema 输出 JSON。"
    )


# ── 解析与校验 ───────────────────────────────────────────────────────

def parse_test_plan(raw_text: str) -> Dict[str, Any]:
    r"""Parse the agent's raw output into a validated TestPlan dict.

    Performs schema validation and fills missing fields with defaults.

    Args:
        raw_text: The raw LLM output text.

    Returns:
        A validated TestPlan dictionary.
    """
    # 提取 JSON 块
    plan: Dict[str, Any] = {}
    # Try fenced code block first
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw_text)
    if m:
        try:
            plan = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    if not plan:
        # Bare JSON
        m2 = re.search(r"\{[\s\S]*\}", raw_text)
        if m2:
            try:
                plan = json.loads(m2.group(0))
            except json.JSONDecodeError:
                pass

    if not plan:
        logger.warning("无法从 Agent 输出中解析 TestPlan JSON，使用默认值")
        return dict(DEFAULT_TEST_PLAN)

    # 字段校验 & 填充默认值
    validated = dict(DEFAULT_TEST_PLAN)
    for key in DEFAULT_TEST_PLAN:
        if key in plan:
            validated[key] = plan[key]

    # 类型修正
    for int_key in ("health_check_interval", "health_check_retries",
                     "health_check_timeout", "buffer_seconds"):
        try:
            validated[int_key] = int(validated[int_key])
        except (ValueError, TypeError):
            validated[int_key] = DEFAULT_TEST_PLAN[int_key]

    # fault_injection_steps 校验
    valid_steps = []
    for step in validated.get("fault_injection_steps", []):
        if not isinstance(step, dict):
            continue
        if step.get("type") not in STEP_TYPES:
            logger.warning("忽略未知故障注入步骤类型: %s", step.get("type"))
            continue
        # 确保必要字段
        step.setdefault("id", str(len(valid_steps) + 1))
        step.setdefault("description", "")
        step.setdefault("expected_status", "DOWN")
        step.setdefault("params", {})
        # device_command 必须包含 params.command
        if step["type"] == "device_command":
            cmd = step.get("params", {}).get("command", "")
            if not cmd:
                logger.warning(
                    "device_command 步骤 %s 缺少 params.command，已跳过",
                    step.get("id"),
                )
                continue
        valid_steps.append(step)
    validated["fault_injection_steps"] = valid_steps

    # status_check_commands 校验
    if not isinstance(validated.get("status_check_commands"), list):
        validated["status_check_commands"] = []
    # 确保字符串列表
    validated["status_check_commands"] = [
        str(c) for c in validated["status_check_commands"] if c
    ]

    # verify_show_commands 校验
    if not isinstance(validated.get("verify_show_commands"), list):
        validated["verify_show_commands"] = []
    validated["verify_show_commands"] = [
        str(c) for c in validated["verify_show_commands"] if c
    ]

    # html_content_must_contain 默认同 keyword
    if not validated.get("html_content_must_contain") and validated.get("keyword"):
        validated["html_content_must_contain"] = validated["keyword"]

    # ── status_check_commands 协议一致性修正 ──
    # 确保 show statistics 命令与 protocol_type 一致
    # 常见错误：SLB+HTTP 场景下 LLM 输出 "show statistics slb real tcp"
    protocol = validated.get("protocol_type", "").lower()
    if protocol in ("http", "https"):
        fixed_cmds = []
        for cmd in validated["status_check_commands"]:
            if "show statistics slb real tcp" in cmd.lower():
                fixed_cmd = cmd.replace("tcp", "http").replace("TCP", "HTTP")
                logger.info("修正 status_check_command: '%s' → '%s' (protocol_type=%s)",
                            cmd, fixed_cmd, protocol)
                fixed_cmds.append(fixed_cmd)
            else:
                fixed_cmds.append(cmd)
        validated["status_check_commands"] = fixed_cmds
    elif protocol in ("tcp",):
        fixed_cmds = []
        for cmd in validated["status_check_commands"]:
            if "show statistics slb real http" in cmd.lower():
                fixed_cmd = cmd.replace("http", "tcp").replace("HTTP", "TCP")
                logger.info("修正 status_check_command: '%s' → '%s' (protocol_type=%s)",
                            cmd, fixed_cmd, protocol)
                fixed_cmds.append(fixed_cmd)
            else:
                fixed_cmds.append(cmd)
        validated["status_check_commands"] = fixed_cmds

    return validated


# ── 便捷入口 ─────────────────────────────────────────────────────────

def generate_test_plan(
    job_content: str,
    model: BaseModelBackend,
) -> Dict[str, Any]:
    r"""One-shot helper: build agent → call → parse → return TestPlan.

    Args:
        job_content: Raw text of the test job.
        model: LLM backend.

    Returns:
        Validated TestPlan dict.
    """
    agent = build_test_plan_agent(model)
    prompt = build_test_plan_prompt(job_content)

    logger.info("[TestPlanAgent] 正在从任务文本提取测试计划...")
    logger.info("[TestPlanAgent] prompt (前 300 字): %s",
                prompt[:300] + "..." if len(prompt) > 300 else prompt)

    try:
        resp = agent.step(prompt)
        raw = resp.msgs[0].content if resp.msgs else ""
        logger.info("[TestPlanAgent] Agent 原始输出 (前 500 字):\n%s",
                    raw[:500] + "..." if len(raw) > 500 else raw)
    except Exception as exc:
        logger.error("[TestPlanAgent] Agent 调用失败: %s", exc, exc_info=True)
        raw = ""

    plan = parse_test_plan(raw)

    logger.info("[TestPlanAgent] 解析后的 TestPlan:")
    logger.info(json.dumps(plan, ensure_ascii=False, indent=2))
    return plan
