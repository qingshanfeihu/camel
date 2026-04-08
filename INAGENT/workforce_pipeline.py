# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
Workforce Pipeline for INAGENT

This module adapts the INAGENT test execution (stages 5-9) into a CAMEL
Workforce Pipeline.  Stages 1-4 (test plan, env planning, config generation,
VIP precheck, VM deploy) remain in pipeline_runner.py and produce the
structured data consumed here.

Pipeline topology::

    Deploy ──► fork ─┬── VerifyShow ──┐
                     └── Traffic ─────┘── join → Analysis ──► Cleanup
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.societies.workforce import Workforce, WorkforceMode
from camel.societies.workforce.workforce_callback import WorkforceCallback
from camel.societies.workforce.events import (
    AllTasksCompletedEvent,
    TaskAssignedEvent,
    TaskCompletedEvent,
    TaskCreatedEvent,
    TaskDecomposedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    WorkerCreatedEvent,
    WorkerDeletedEvent,
)
from camel.tasks import Task

from camel.toolkits.note_taking_toolkit import NoteTakingToolkit

from INAGENT.toolkits import NSAEDeviceToolkit, VMControllerToolkit, TrafficVerifyToolkit
from INAGENT.toolkits.knowledge_toolkit import KnowledgeToolkit
from INAGENT.config.project_config import cfg_float
from INAGENT.utils.ssh_client import create_ssh_client_from_env
from INAGENT.utils.vm_controller import VMController
from INAGENT.utils.env_utils import get_product_name

logger = logging.getLogger(__name__)


# Tunable timeouts for long-running verify/traffic stages.
_VERIFY_SHOW_WORKER_TIMEOUT_SECONDS = float(
    cfg_float("workforce.verify_worker_timeout_seconds", 420.0, env="INAGENT_VERIFY_WORKER_TIMEOUT")
)
_TRAFFIC_WORKER_TIMEOUT_SECONDS = float(
    cfg_float("workforce.traffic_worker_timeout_seconds", 600.0, env="INAGENT_TRAFFIC_WORKER_TIMEOUT")
)
_PIPELINE_TASK_TIMEOUT_SECONDS = max(
    _VERIFY_SHOW_WORKER_TIMEOUT_SECONDS,
    _TRAFFIC_WORKER_TIMEOUT_SECONDS,
    600.0,
)

# ── CLI 命令删除参考（供 Cleanup Agent System Prompt 使用） ─────────
_NSAE_DELETE_REFERENCE_TEMPLATE = """\
{product_name} 删除命令语法参考:
- 删除 virtual server:       no slb virtual http "<name>"
- 删除 real server:          no slb real http "<name>"
- 删除 SLB group:            no slb group method "<name>"
- 删除 health check:         no slb health "<name>"
- 删除 IP route:             no ip route static <dest> <mask> <gw>
- 删除 IP address:           no ip address <interface>
- 顺序要求: virtual → group → real → health → route → ip address
"""


def _get_delete_reference() -> str:
    return _NSAE_DELETE_REFERENCE_TEMPLATE.format(product_name=get_product_name())


# ── O3: WorkforceCallback 实时日志 ──────────────────────────────────
class INAGENTPipelineCallback(WorkforceCallback):
    """将 Workforce 生命周期事件转发到 Python logging，提供实时可观测性。"""

    def log_task_created(self, event: TaskCreatedEvent) -> None:
        logger.info("[Workforce] 任务创建: id=%s desc=%s",
                     event.task_id, event.description[:80])

    def log_task_decomposed(self, event: TaskDecomposedEvent) -> None:
        logger.info("[Workforce] 任务分解: parent=%s → %s",
                     event.parent_task_id, event.subtask_ids)

    def log_task_assigned(self, event: TaskAssignedEvent) -> None:
        logger.info("[Workforce] 任务分配: task=%s → worker=%s",
                     event.task_id, event.worker_id)

    def log_task_started(self, event: TaskStartedEvent) -> None:
        logger.info("[Workforce] 任务开始: task=%s worker=%s",
                     event.task_id, event.worker_id)

    def log_task_completed(self, event: TaskCompletedEvent) -> None:
        summary = (event.result_summary or "")[:120]
        logger.info("[Workforce] 任务完成: task=%s worker=%s (%.1fs) %s",
                     event.task_id, event.worker_id,
                     event.processing_time_seconds or 0, summary)

    def log_task_failed(self, event: TaskFailedEvent) -> None:
        logger.warning("[Workforce] 任务失败: task=%s error=%s",
                        event.task_id, event.error_message[:200])

    def log_worker_created(self, event: WorkerCreatedEvent) -> None:
        logger.info("[Workforce] Worker 创建: id=%s role=%s type=%s",
                     event.worker_id, event.role, event.worker_type)

    def log_worker_deleted(self, event: WorkerDeletedEvent) -> None:
        logger.info("[Workforce] Worker 删除: id=%s reason=%s",
                     event.worker_id, event.reason)

    def log_all_tasks_completed(self, event: AllTasksCompletedEvent) -> None:
        logger.info("[Workforce] 所有任务已完成 ✓")


async def run_workforce_pipeline_async(
    job_content: str,
    config_commands: List[str],
    verify_commands: List[str],
    env_plan: Dict[str, Any],
    model: object,
    *,
    test_plan: Optional[Dict[str, Any]] = None,
    precheck_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute stages 5-9 of the INAGENT test pipeline via CAMEL Workforce.

    Args:
        job_content: Raw test-job text (human intent).
        config_commands: Configuration commands produced by stage 3.
        verify_commands: Show/verify commands produced by stage 3.
        env_plan: Environment plan produced by stages 2/4.  Must already
            contain ``_vm``, ``server_url``, ``port``, ``original_html``, etc.
        model: LLM model backend created by ``initialize_llm_model()``.
        test_plan: Test-plan dict from stage 1 (keyword, fault steps, …).
        precheck_info: VIP pre-check info from stage 3.5.

    Returns:
        Dict with ``verdict`` (``"成功"`` / ``"失败"``), ``final_analysis``,
        per-task results in ``task_results``, and ``raw_result``.
    """
    logger.info("=" * 80)
    logger.info("Workforce Pipeline: 初始化 (阶段 5-9)")
    logger.info("=" * 80)
    logger.info(
        "超时配置: verify=%.0fs traffic=%.0fs pipeline=%.0fs",
        _VERIFY_SHOW_WORKER_TIMEOUT_SECONDS,
        _TRAFFIC_WORKER_TIMEOUT_SECONDS,
        _PIPELINE_TASK_TIMEOUT_SECONDS,
    )

    tp = test_plan or {}
    port = env_plan.get("port", 80)
    original_html = env_plan.get("original_html", "")
    env_vars = env_plan.get("env", {})
    vip = env_vars.get("LB_VIP", "")
    vip_port = env_vars.get("LB_VIP_PORT", str(port))
    vip_url = f"http://{vip}:{vip_port}/" if vip else ""
    keyword = tp.get("keyword", "")
    fault_steps = tp.get("fault_injection_steps", [])
    status_check_commands = tp.get("status_check_commands", [])
    bind_ip = env_vars.get("LB_REAL_SERVER_IP", "")

    # 将 config_commands 列表格式化为多行字符串（供 Agent 阅读）
    config_commands_text = "\n".join(config_commands)
    verify_commands_text = "\n".join(verify_commands)

    # ── P3: 共享 SSH / VM 连接 ─────────────────────────────────────────
    # 所有 Toolkit 共享同一组底层连接，避免多条 SSH/VM 会话冲突
    ssh_client = create_ssh_client_from_env()
    vm = VMController.from_env()

    nsae_tk = NSAEDeviceToolkit(
        ssh_client=ssh_client,
        timeout=_VERIFY_SHOW_WORKER_TIMEOUT_SECONDS,
    )
    vm_tk = VMControllerToolkit(vm=vm)
    traffic_tk = TrafficVerifyToolkit(
        ssh_client=ssh_client,
        vm=vm,
        timeout=_TRAFFIC_WORKER_TIMEOUT_SECONDS,
    )

    # ── 知识协作工具 ─────────────────────────────────────────────────
    import uuid as _uuid
    from pathlib import Path as _Path
    _work_dir = _Path(__file__).resolve().parent / "working_dir" / _uuid.uuid4().hex[:12]
    _work_dir.mkdir(parents=True, exist_ok=True)

    knowledge_tk_config = KnowledgeToolkit(mode="config")
    knowledge_tk_explain = KnowledgeToolkit(mode="explain")
    note_tk = NoteTakingToolkit(working_directory=str(_work_dir))

    # ── 1. Coordinator & Task Agents ───────────────────────────────────
    coordinator_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="Pipeline Coordinator",
            content=(
                "你是 INAGENT 自动化测试的 Pipeline Coordinator。\n"
                "你负责监控下属 Worker 的进度和质量：\n"
                "- 如果 Worker 报告的验证信息不充分（例如 show 命令输出不足），"
                "请标记质量不合格并建议 retry，让 Worker 主动补充更多数据。\n"
                "- 评判标准：Deploy 无 error；VerifyShow 提供了配置生效的正面证据；"
                "Traffic 的 HTTP 探测成功率合理；Analysis 给出了有理有据的 Verdict。\n"
                "你可以使用知识检索工具查询产品文档，了解预期行为和验证标准。\n"
                "你可以使用笔记工具查看各 Worker 写入的结果笔记。"
            ),
        ),
        model=model,
        tools=knowledge_tk_explain.get_tools() + note_tk.get_tools(),
    )

    task_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="Task Planner",
            content=(
                "你是任务分派员。已注册的 Worker 有：\n"
                "- DeployWorker: 配置下发和 VM HTTP 服务部署\n"
                "- VerifyShowWorker: 执行 show 命令验证配置\n"
                "- TrafficWorker: HTTP 流量探测和故障注入\n"
                "- AnalysisWorker: 综合分析并给出 PASS/FAIL\n"
                "- CleanupWorker: 撤销配置和停止服务\n"
                "请根据任务内容分派给最匹配的 Worker。"
            ),
        ),
        model=model,
    )

    # O3: 注册 Callback 实现实时可观测
    workforce = Workforce(
        "INAGENT Pipeline Workforce",
        coordinator_agent=coordinator_agent,
        task_agent=task_agent,
        mode=WorkforceMode.PIPELINE,
        task_timeout_seconds=_PIPELINE_TASK_TIMEOUT_SECONDS,
        callbacks=[INAGENTPipelineCallback()],
    )

    # ── 2. Workers (Agents + Toolkits) ─────────────────────────────────
    deploy_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="DeployWorker",
            content=(
                f"你是 {get_product_name()} 设备配置下发专家。\n"
                "使用 execute_config_commands 工具将配置命令逐条下发到设备。\n"
                "命令格式：每行一条命令，用换行符分隔。\n"
                "如果某条命令返回 error，记录错误并继续后续命令。\n"
                "你还拥有知识检索工具（search_product_knowledge），\n"
                "当遇到不确定的CLI命令语法时可以查询 cli/reference 或 app/reference。\n"
                "你还拥有笔记工具，请将配置下发结果摘要写入笔记 'deploy_result'，\n"
                "供后续 Worker 参考。\n"
                "返回时请列出成功/失败的命令统计。"
            ),
        ),
        model=model,
        tools=nsae_tk.get_tools() + knowledge_tk_config.get_tools() + note_tk.get_tools(),
    )

    verify_show_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="VerifyShowWorker",
            content=(
                f"你是 {get_product_name()} 设备状态验证专家。\n"
                "使用 execute_show_commands 和 get_module_config 工具查询设备状态。\n"
                "如果指定的 show 命令输出不足以判断配置是否生效，你必须主动执行补充命令。\n"
                "例如：如果 show slb health 输出中未见自定义 request/response 字段，"
                "可补充执行 show slb group health、show slb policy default 等。\n"
                "若 show slb virtual http / show slb real http 返回 '^' 语法错误，"
                "应改用兼容命令 show slb virtual / show slb real 再次验证。\n"
                "你还拥有知识检索工具，可以查询CLI命令语法来理解验证命令的含义和预期输出。\n"
                "请将验证结果摘要写入笔记 'verify_result'，供 AnalysisWorker 参考。\n"
                "你的输出必须包含所有已执行命令及其输出。\n"
                "\n"
                "【严禁幻觉】你只能输出设备通过 SSH 实际返回的命令输出，"
                "不得编造、推测或虚构任何设备响应内容。"
                "每条输出必须标注对应的工具调用和原始返回值。"
            ),
        ),
        model=model,
        tools=nsae_tk.get_tools() + knowledge_tk_config.get_tools() + note_tk.get_tools(),
        tool_execution_timeout=_VERIFY_SHOW_WORKER_TIMEOUT_SECONDS,
        step_timeout=_VERIFY_SHOW_WORKER_TIMEOUT_SECONDS,
    )

    traffic_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="TrafficWorker",
            content=(
                "你是流量探测和故障注入专家。\n"
                "1. 使用 verify_vip_traffic 向 VIP 发起 HTTP 请求验证端到端连通性。\n"
                "2. 按提供的故障注入步骤逐一执行 run_fault_injection_step。\n"
                "   对于 device_command 类型的步骤，必须传入 command 参数。\n"
                "3. 每步故障后调用 wait_for_health_convergence 等待健康检查收敛，"
                "然后调用 check_device_health_status 查询设备探针状态。\n"
                "4. 输出每一步的操作结果和设备反馈。\n"
                "\n"
                "【严禁幻觉】你必须且只能通过工具函数执行操作，只能报告工具的"
                "实际返回值（JSON）。若工具不支持某操作或工具调用失败，"
                "必须如实报告'无法执行'及错误信息，绝不能编造执行结果或设备输出。"
            ),
        ),
        model=model,
        tools=traffic_tk.get_tools(),
        tool_execution_timeout=_TRAFFIC_WORKER_TIMEOUT_SECONDS,
        step_timeout=_TRAFFIC_WORKER_TIMEOUT_SECONDS,
    )

    analysis_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="AnalysisWorker",
            content=(
                "你是自动化测试的结果分析专家。\n"
                "综合 Deploy、VerifyShow、Traffic 三个阶段的输出，判断测试是否通过。\n"
                "判定条件：\n"
                "- 配置下发无关键错误\n"
                "- show 命令确认配置已生效（存在正面证据）\n"
                "- VIP 流量可达\n"
                "- 故障注入后设备正确切换服务器状态\n"
                "你可以使用知识检索工具查询产品文档，理解预期行为来辅助判定。\n"
                "你可以使用 read_note 读取 'deploy_result' 和 'verify_result' 笔记。\n"
                "你的最终输出必须包含 'Verdict: PASS' 或 'Verdict: FAIL' 并附详细理由。\n"
                "【注意】show slb health 仅显示策略摘要，末尾的 GET \"/\" \"200\" "
                "是固定默认显示值，不能据此判定内容健康检查未生效。\n"
                "\n"
                "【严禁幻觉信任】若 Worker 输出中缺少工具调用的原始返回数据"
                "（如 JSON 格式的工具响应），应视为不可信并标注为'缺少实际执行证据'。"
                "若发现故障注入步骤的输出中没有实际的工具调用结果，应判定为 FAIL。"
            ),
        ),
        model=model,
        tools=knowledge_tk_explain.get_tools() + note_tk.get_tools(),
    )

    cleanup_agent = ChatAgent(
        BaseMessage.make_assistant_message(
            role_name="CleanupWorker",
            content=(
                "你是测试环境清理专家。\n"
                "根据提供的配置命令列表，生成对应的 no/delete 删除命令并下发到设备。\n"
                f"{_get_delete_reference()}\n"
                "你还拥有知识检索工具，当不确定删除命令语法时可以查询 cli/reference。\n"
                "同时使用 stop_http_server 停止 VM 上的 HTTP 服务。\n"
                "返回清理结果统计。"
            ),
        ),
        model=model,
        tools=nsae_tk.get_tools() + vm_tk.get_tools() + knowledge_tk_config.get_tools(),
    )

    # 注册 Workers
    workforce.add_single_agent_worker("DeployWorker", deploy_agent)
    workforce.add_single_agent_worker("VerifyShowWorker", verify_show_agent)
    workforce.add_single_agent_worker("TrafficWorker", traffic_agent)
    workforce.add_single_agent_worker("AnalysisWorker", analysis_agent)
    workforce.add_single_agent_worker("CleanupWorker", cleanup_agent)

    # ── 3. Pipeline 编排（O1: 使用 Task + additional_info 传递结构化上下文）─
    # 公共上下文字典，各阶段 Task 通过 additional_info 携带
    _common_ctx = {
        "job_content": job_content,
        "config_commands": config_commands,
        "verify_commands": verify_commands,
        "port": port,
        "vip_url": vip_url,
        "keyword": keyword,
        "bind_ip": bind_ip,
    }

    deploy_task_obj = Task(
        content=(
            f"【阶段5: 配置下发】\n"
            f"请将以下配置命令下发到 {get_product_name()} 设备：\n"
            f"```\n{config_commands_text}\n```\n"
        ),
        id="stage5_deploy",
        additional_info={**_common_ctx, "stage": "deploy"},
    )

    verify_show_task_obj = Task(
        content=(
            f"【阶段7: 验证配置】\n"
            f"请在 {get_product_name()} 设备上执行以下验证命令，确认配置是否生效：\n"
            f"```\n{verify_commands_text}\n```\n"
            f"测试目标: {job_content[:200]}\n"
            f"如果输出信息不足以判断，请自行补充执行相关 show 命令。"
        ),
        id="stage7_verify",
        additional_info={**_common_ctx, "stage": "verify_show"},
    )

    traffic_task_parts = ["【阶段6+6.5: 流量验证与故障注入】\n"]
    if vip_url:
        traffic_task_parts.append(
            f"Step 1: 使用 verify_vip_traffic 探测 VIP: {vip_url}"
            + (f" (关键词: {keyword})" if keyword else "")
            + "\n"
        )
    if fault_steps:
        traffic_task_parts.append(
            f"Step 2: 按以下步骤执行故障注入（每步后等待健康检查收敛并查询状态）:\n"
            f"{json.dumps(fault_steps, ensure_ascii=False, indent=2)}\n"
            f"参数: port={port}, bind_ip={bind_ip}, html_content 长度={len(original_html)} 字符\n"
        )
        if keyword:
            traffic_task_parts.append(f"关键词: {keyword}\n")

    traffic_task_obj = Task(
        content="".join(traffic_task_parts),
        id="stage6_traffic",
        additional_info={
            **_common_ctx,
            "stage": "traffic",
            "fault_steps": fault_steps,
            "original_html_len": len(original_html),
        },
    )

    analysis_task_obj = Task(
        content=(
            f"【阶段8: 结果分析】\n"
            f"测试任务: {job_content}\n"
            f"请综合 Deploy、VerifyShow、Traffic 各阶段的执行结果，"
            f"判断该测试任务是否全面通过。\n"
            f"最终结论必须包含 'Verdict: PASS' 或 'Verdict: FAIL'。"
        ),
        id="stage8_analysis",
        additional_info={**_common_ctx, "stage": "analysis"},
    )

    cleanup_task_obj = Task(
        content=(
            f"【阶段9: 环境清理】\n"
            f"请清理本次测试下发的所有配置，并停止 VM HTTP 服务 (端口 {port})。\n"
            f"下发过的配置如下：\n"
            f"```\n{config_commands_text}\n```"
        ),
        id="stage9_cleanup",
        additional_info={**_common_ctx, "stage": "cleanup"},
    )

    workforce.pipeline_add(deploy_task_obj) \
             .pipeline_fork([verify_show_task_obj, traffic_task_obj]) \
             .pipeline_join(analysis_task_obj) \
             .pipeline_add(cleanup_task_obj) \
             .pipeline_build()

    logger.info("Pipeline 已构建: Deploy → fork(VerifyShow, Traffic) → join(Analysis) → Cleanup")

    # ── 4. 执行 ───────────────────────────────────────────────────────
    main_task = Task(
        content=f"INAGENT E2E 自动化测试: {job_content[:100]}",
        id="E2E_Test_Job",
    )

    result = await workforce.process_task_async(main_task)

    # P3: 统一关闭共享连接
    for tk in (nsae_tk, vm_tk, traffic_tk):
        try:
            tk.disconnect()
        except Exception:
            pass

    # 清理笔记工作目录
    import shutil
    try:
        shutil.rmtree(_work_dir, ignore_errors=True)
    except Exception:
        pass

    # 从 workforce 的 completed_tasks 中提取每阶段结果（供 MD 报告使用）
    task_results: Dict[str, str] = {}
    for t in getattr(workforce, "_completed_tasks", []):
        task_results[t.id] = t.result or ""

    # 解析最终结论
    final_output = result.result or ""
    verdict = "成功" if "Verdict: PASS" in final_output else "失败"

    logger.info("=" * 80)
    logger.info("Workforce Pipeline 完成, Verdict: %s", verdict)
    logger.info("=" * 80)

    return {
        "job_id": result.id,
        "verdict": verdict,
        "final_analysis": final_output,
        "task_results": task_results,
        "raw_result": result.model_dump(),
    }


def run_workforce_pipeline(
    job_content: str,
    config_commands: List[str],
    verify_commands: List[str],
    env_plan: Dict[str, Any],
    model: object,
    **kwargs,
) -> Dict[str, Any]:
    """同步入口 — 在已有事件循环中使用 nest_asyncio，否则使用 asyncio.run。"""
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = run_workforce_pipeline_async(
        job_content=job_content,
        config_commands=config_commands,
        verify_commands=verify_commands,
        env_plan=env_plan,
        model=model,
        **kwargs,
    )

    if loop and loop.is_running():
        # 已在异步上下文中（如 Jupyter）
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)
