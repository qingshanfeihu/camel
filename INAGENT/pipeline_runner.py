# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
测试执行脚本：读取jobs下的测试任务，构造测试步骤，在测试环境下发配置命令执行测试，输出测试结果

完整流程：
1. 读取jobs目录下的测试任务文件
2. 任务分解：分析测试需求，确认每个步骤执行的命令
3. 搭建测试环境：准备测试使用的所有工具（toolkit）
4. 构建设备配置：根据步骤和环境生成配置
5. 下发配置执行测试
6. 检查测试结果：确认测试通过与不通过
"""
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from INAGENT.utils import env_utils
from INAGENT.utils.index_utils import load_function_structure_index
from INAGENT.workflow_config_generator import (
    initialize_rag_system,
    initialize_llm_model,
    process_job,
)
from INAGENT.agents.env_setup_agent import (
    build_env_setup_agent,
    build_env_setup_prompt,
)
from INAGENT.agents.task_analysis_agent import (
    build_task_analysis_agent,
    build_task_analysis_prompt,
)
from INAGENT.agents.cleanup_agent import (
    build_cleanup_agent,
    build_cleanup_prompt,
)
from INAGENT.agents.test_plan_agent import generate_test_plan
from INAGENT.scripts.model_helpers import _build_model, _extract_json_object
from INAGENT.utils.ssh_client import NSAESSHClient, create_ssh_client_from_env
from INAGENT.utils.vm_controller import VMController
from INAGENT.workforce_pipeline import run_workforce_pipeline

# 加载环境变量
env_utils.load_inagent_env()

logger = logging.getLogger(__name__)


def setup_logging(log_file: Optional[Path] = None):
    """设置日志记录，同时输出到终端和文件"""
    handlers = []
    
    # 控制台处理器（实时输出）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    handlers.append(console_handler)
    
    # 文件处理器（记录日志）
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        handlers.append(file_handler)
    
    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers = handlers


def plan_network_environment(job_content: str, model, test_plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """阶段一：网络环境规划（仅 IP 规划，不执行任何实际配置）

    调用 env_setup_agent 获取 IP 地址规划、子网、端口等信息。
    规划结果将传递给「配置生成 Agent」，使其生成包含网络层 + 应用层的统一命令序列。

    Args:
        job_content: 任务原文。
        model: LLM 模型后端。
        test_plan: TestPlan 字典（由 TestPlanAgent 生成）。

    Returns:
        env_plan 字典，包含 port, env, subnet_config 等（不含 _vm 等运行时对象）。
    """
    logger.info("=" * 80)
    logger.info("网络环境规划（IP 分配）")
    logger.info("=" * 80)

    # 从 test_plan 获取关键词，用于构造默认 HTML
    _kw = (test_plan or {}).get("keyword", "")
    _default_html_tpl = (test_plan or {}).get(
        "default_html_template",
        "<html><body><h1>{keyword}</h1></body></html>",
    )
    _default_html = _default_html_tpl.format(keyword=_kw) if _kw else ""

    try:
        env_agent = build_env_setup_agent(model)
        env_prompt = build_env_setup_prompt(job_content)

        logger.info("环境规划Agent输入提示词（前500字符）:")
        logger.info(env_prompt[:500] + "..." if len(env_prompt) > 500 else env_prompt)
        logger.info("")

        logger.info("调用环境规划Agent...")
        env_response = env_agent.step(env_prompt)
        env_text = env_response.msgs[0].content if env_response.msgs else ""

        logger.info("环境规划Agent原始输出:")
        logger.info(env_text)
        logger.info("")

        # 解析环境规划 JSON
        env_plan = _extract_json_object(env_text) or {}

        # 处理 TaskResult 包装格式: {"content": "{...}", "failed": ...}
        if "content" in env_plan and isinstance(env_plan.get("content"), str):
            try:
                inner = json.loads(env_plan["content"])
                if isinstance(inner, dict):
                    logger.info("检测到 TaskResult 包装格式，已展开 content 字段")
                    env_plan = inner
            except json.JSONDecodeError:
                logger.warning("TaskResult content 字段不是有效 JSON，忽略")

        if not env_plan or "port" not in env_plan:
            logger.warning("无法解析环境规划，使用默认值")
            env_plan = {
                "port": 80,
                "env": {
                    "LB_REAL_SERVER_IP": "",
                    "LB_REAL_SERVER_PORT": "80",
                }
            }

        # 确保 HTML 内容已记录（后续部署使用）
        html_content = env_plan.get("html_content") or _default_html or "<html><body><h1>test</h1></body></html>"
        env_plan["original_html"] = html_content

        logger.info("环境规划（解析后的JSON）:")
        logger.info(json.dumps(env_plan, ensure_ascii=False, indent=2))
        logger.info("")

        return env_plan

    except Exception as e:
        logger.error(f"网络环境规划失败: {e}", exc_info=True)
        raise


def deploy_test_environment(env_plan: Dict[str, Any], config_commands: Optional[List[str]] = None) -> Dict[str, Any]:
    """阶段二：部署测试环境（VM 接口配置 + HTTP 服务部署）

    根据 env_plan 中的 IP 规划：
    1. 配置 VM eth1/eth2 接口
    2. 在 VM 上部署 HTTP 测试服务
    3. 如果 config_commands 包含多个 real server IP，自动在 VM eth1 上添加
       IP 别名并为每个 IP 启动独立 HTTP 服务
    （设备侧的接口/路由配置由统一配置命令在 deploy_config_commands 中推送）

    Args:
        env_plan: 由 plan_network_environment 返回的环境规划字典。
        config_commands: 已生成的配置命令列表（用于提取多 real server IP）。

    Returns:
        更新后的 env_plan，增加 server_url, deploy_result, _vm 等字段。
    """
    logger.info("=" * 80)
    logger.info("部署测试环境（VM 接口 + HTTP 服务）")
    logger.info("=" * 80)

    port = env_plan.get("port") or 80
    try:
        port = int(port)
    except ValueError:
        port = 80
    html_content = env_plan.get("original_html", "<html><body><h1>test</h1></body></html>")

    # 从 env_plan 获取 Agent 规划的 VM 接口 IP
    env_vars = env_plan.get("env", {})
    vm_eth1_ip = env_vars.get("VM_ETH1_IP", "")
    vm_eth1_mask = env_vars.get("VM_ETH1_MASK", "255.255.255.0")
    vm_eth2_ip = env_vars.get("VM_ETH2_IP", "")
    vm_eth2_mask = env_vars.get("VM_ETH2_MASK", "255.255.255.0")
    real_server_ip = env_vars.get("LB_REAL_SERVER_IP", vm_eth1_ip)

    if not vm_eth1_ip:
        raise RuntimeError("Agent 未规划 VM_ETH1_IP（服务器侧 IP），无法部署 HTTP 服务")

    # 1) 创建 VMController 并连接
    vm = VMController.from_env()
    vm.connect()

    # 2) 配置 VM 网络接口
    logger.info("配置 VM 网络接口...")
    vm.configure_interface("eth1", vm_eth1_ip, vm_eth1_mask)
    logger.info("VM eth1 已配置: %s/%s", vm_eth1_ip, vm_eth1_mask)
    if vm_eth2_ip:
        vm.configure_interface("eth2", vm_eth2_ip, vm_eth2_mask)
        logger.info("VM eth2 已配置: %s/%s", vm_eth2_ip, vm_eth2_mask)
    else:
        logger.warning("Agent 未提供 VM_ETH2_IP，跳过 eth2 配置（流量验证可能受影响）")

    # 2.5) 检测多服务器场景（LB_REAL_SERVERS 列表 — 同 IP 不同端口）
    real_servers = env_vars.get("LB_REAL_SERVERS", [])
    multi_server_mode = isinstance(real_servers, list) and len(real_servers) > 1
    extra_rs_ips: List[str] = []

    if multi_server_mode:
        logger.info("检测到多服务器模式: %d 台后端服务器（同 IP 不同端口）", len(real_servers))
        for rs in real_servers:
            logger.info("  %s → %s:%s", rs.get("name", "?"), rs.get("ip", "?"), rs.get("port", "?"))
        env_plan["_multi_server_mode"] = True
        env_plan["_real_servers"] = real_servers
    else:
        # 向后兼容：检测 config_commands 中的多 real server IP（不同 IP 场景）
        if config_commands:
            all_rs_ips: List[str] = []
            for cmd in config_commands:
                m = re.match(r'slb\s+real\s+http\s+"[^"]+"\s+(\d+\.\d+\.\d+\.\d+)', cmd.strip())
                if m:
                    rs_ip = m.group(1)
                    if rs_ip not in all_rs_ips:
                        all_rs_ips.append(rs_ip)
            # 过滤出尚未配置的额外 IP（不等于主 eth1 IP 也不等于 real_server_ip）
            configured_ips = {vm_eth1_ip, real_server_ip}
            extra_rs_ips = [ip for ip in all_rs_ips if ip not in configured_ips]
            if extra_rs_ips:
                logger.info("检测到 %d 个额外 real server IP，将添加为 eth1 的 IP 别名:", len(extra_rs_ips))
                for i, ip in enumerate(extra_rs_ips):
                    alias_iface = f"eth1:{i+1}"
                    logger.info("  添加 IP 别名: %s → %s/%s", alias_iface, ip, vm_eth1_mask)
                    try:
                        prefix = VMController._mask_to_prefix(vm_eth1_mask)
                        vm.run(f"ip addr add {ip}/{prefix} dev eth1", check=True)
                    except Exception as e:
                        logger.warning("添加 IP 别名 %s 失败: %s", ip, e)
                env_plan["_all_real_server_ips"] = [real_server_ip or vm_eth1_ip] + extra_rs_ips

        # 同时从 config_commands 中检测同 IP 不同端口场景 (LLM 直接生成了多端口命令)
        if config_commands and not multi_server_mode:
            rs_ip_port_map: List[Dict[str, Any]] = []
            for cmd in config_commands:
                m = re.match(r'slb\s+real\s+http\s+"([^"]+)"\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)', cmd.strip())
                if m:
                    rs_ip_port_map.append({"name": m.group(1), "ip": m.group(2), "port": int(m.group(3))})
            if len(rs_ip_port_map) > 1:
                ports = [r["port"] for r in rs_ip_port_map]
                if len(set(ports)) > 1:
                    logger.info("从 config_commands 检测到多端口 real server: %s", rs_ip_port_map)
                    multi_server_mode = True
                    real_servers = rs_ip_port_map
                    env_plan["_multi_server_mode"] = True
                    env_plan["_real_servers"] = real_servers

    # 3) 在 VM 上部署 HTTP 服务
    bind_ip = real_server_ip or vm_eth1_ip

    if multi_server_mode and real_servers:
        # 多服务器模式：为每个端口部署独立的 HTTP 实例
        logger.info("多服务器模式: 部署 %d 个 HTTP 实例", len(real_servers))
        multi_results = vm.deploy_multi_http_servers(
            html_content=html_content,
            servers=real_servers,
            bind_ip=bind_ip,
        )
        deploy_result = multi_results[0] if multi_results else {}
        server_url = f"http://{bind_ip}:{real_servers[0].get('port', 80)}"
        env_plan["_multi_deploy_results"] = multi_results

        # 验证所有实例
        for dr in multi_results:
            if dr.get("success"):
                p = dr["port"]
                _ok = False
                for _a in range(3):
                    probe = vm.http_probe(f"http://{bind_ip}:{p}/", timeout=3)
                    if probe.get("success"):
                        _ok = True
                        break
                    time.sleep(0.5)
                logger.info("HTTP 实例 %s (端口 %d) 健康检查: %s",
                            dr.get("name", "?"), p, "✅ 通过" if _ok else "❌ 未通过")
            else:
                logger.warning("HTTP 实例 %s (端口 %d) 部署失败", dr.get("name", "?"), dr.get("port", "?"))
    else:
        # 单服务器模式（原逻辑）
        logger.info("在 VM 上部署 HTTP 服务: %s:%d", bind_ip, port)
        deploy_result = vm.deploy_http_server(
            html_content=html_content,
            port=port,
            bind_ip=bind_ip,
        )
        server_url = deploy_result.get("server_url", f"http://{bind_ip}:{port}")
        logger.info("VM HTTP 测试服务已启动: %s", server_url)

        # 3.5) 为额外 real server IP 部署 HTTP 服务（绑定到 0.0.0.0 使所有 IP 可达）
        if extra_rs_ips:
            if bind_ip != "0.0.0.0":
                logger.info("多 real server 模式: 重新部署 HTTP 服务绑定到 0.0.0.0:%d（覆盖所有 IP）", port)
                try:
                    vm.stop_http_server(port=port)
                    deploy_result = vm.deploy_http_server(
                        html_content=html_content,
                        port=port,
                        bind_ip="0.0.0.0",
                    )
                    logger.info("HTTP 服务已重新部署到 0.0.0.0:%d", port)
                except Exception as e:
                    logger.warning("重新部署 HTTP 到 0.0.0.0 失败，保持原绑定: %s", e)

        # 4) 从 VM 本地做一次健康检查
        _health_ok = False
        for _attempt in range(5):
            probe = vm.http_probe(f"http://{bind_ip}:{port}/", timeout=3)
            if probe.get("success"):
                _health_ok = True
                break
            logger.debug("HTTP 健康检查第 %d 次失败: %s", _attempt + 1, probe.get("error", ""))
            time.sleep(0.5)

        if _health_ok:
            logger.info("HTTP 服务健康检查通过 (http://%s:%d/)", bind_ip, port)
        else:
            logger.warning("HTTP 服务健康检查未通过，流量验证可能受影响")

        # 4.5) 验证额外 IP 的可达性
        for extra_ip in extra_rs_ips:
            _extra_ok = False
            for _a in range(3):
                probe = vm.http_probe(f"http://{extra_ip}:{port}/", timeout=3)
                if probe.get("success"):
                    _extra_ok = True
                    break
                time.sleep(0.5)
            if _extra_ok:
                logger.info("HTTP 服务健康检查通过 (http://%s:%d/)", extra_ip, port)
            else:
                logger.warning("HTTP 服务健康检查未通过 (http://%s:%d/)，该 real server 可能不可达", extra_ip, port)

    env_plan["server_url"] = server_url
    env_plan["port"] = port
    env_plan["deploy_result"] = deploy_result
    # 同步更新 env 子字典
    if "env" in env_plan and "LB_REAL_SERVER_PORT" in env_plan["env"]:
        env_plan["env"]["LB_REAL_SERVER_PORT"] = str(port)
    # 保存 VMController 引用，供后续故障注入和清理使用
    env_plan["_vm"] = vm

    logger.info("测试环境部署完成")
    logger.info("")

    return env_plan


# ---------------------------------------------------------------------------
# VIP 冲突预检查：部署前 SSH 查询设备已有 VIP，决定复用/替换
# ---------------------------------------------------------------------------

def precheck_vip_conflict(
    config_commands: List[str],
    env_plan: Dict[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    """部署前通过 SSH 查询设备已有虚拟服务，检测 VIP 冲突。

    如果环境规划的 VIP 已被其他虚拟服务占用：
    - 复用已占用该 VIP 的虚拟服务名称（替换 config_commands 中的 vs 名称）
    - 跳过创建新虚拟服务的命令
    - 将策略绑定改为指向已有虚拟服务

    Returns:
        Tuple: (调整后的config_commands, precheck_info字典)
    """
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"
    precheck_info: Dict[str, Any] = {
        "checked": False,
        "conflict": False,
        "existing_vs": {},  # name → (vip, port)
        "reuse_vs": None,
        "adjusted": False,
    }

    if not enable_push:
        return config_commands, precheck_info

    ssh_client: Optional[NSAESSHClient] = None
    try:
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        ssh_client.enter_enable_mode()

        # 查询当前虚拟服务
        results = ssh_client.execute_show_commands(["show slb virtual http"])
        precheck_info["checked"] = True
        show_output = results[0]["output"] if results else ""

        # 解析: slb virtual http "<name>" <vip> <port> ...
        existing: Dict[str, Tuple[str, str]] = {}
        for line in show_output.splitlines():
            m = re.match(
                r'slb\s+virtual\s+http\s+"([^"]+)"\s+(\S+)\s+(\d+)',
                line.strip(),
            )
            if m:
                vs_name, vip, port = m.group(1), m.group(2), m.group(3)
                existing[vs_name] = (vip, port)

        precheck_info["existing_vs"] = {k: {"vip": v[0], "port": v[1]} for k, v in existing.items()}
        # 将已有 VS 信息存入 env_plan，供 verify_traffic 做 fallback
        env_plan["precheck_existing_vs"] = precheck_info["existing_vs"]
        logger.info("VIP 预检查: 设备已有 %d 个虚拟服务", len(existing))
        for name, (vip, port) in existing.items():
            logger.info("  %s → %s:%s", name, vip, port)

        # 检查 config_commands 中要创建的虚拟服务 VIP 是否冲突
        # 优先从 config_commands 提取（实际要下发的 VIP），env_plan 仅做后备
        target_vip = ""
        new_vs_name_from_cmd = ""
        for cmd in config_commands:
            m = re.match(r'slb\s+virtual\s+http\s+"([^"]+)"\s+(\S+)\s+(\d+)', cmd)
            if m:
                new_vs_name_from_cmd = m.group(1)
                target_vip = m.group(2)
                break
        if not target_vip:
            target_vip = env_plan.get("env", {}).get("LB_VIP", "")

        if target_vip:
            precheck_info["target_vip"] = target_vip
            # 检查是否有已有虚拟服务使用该VIP
            for vs_name, (vip, port) in existing.items():
                if vip == target_vip:
                    precheck_info["conflict"] = True
                    precheck_info["reuse_vs"] = vs_name
                    logger.info(
                        "VIP 冲突检测: %s 已被虚拟服务 '%s' 使用，将复用该服务",
                        target_vip, vs_name,
                    )

                    # 调整 config_commands：替换虚拟服务名称
                    adjusted = []
                    orig_vs = new_vs_name_from_cmd  # 原本要创建的 vs 名称
                    for cmd in config_commands:
                        # 跳过创建虚拟服务命令（VIP 已存在）
                        m_vs = re.match(
                            r'slb\s+virtual\s+http\s+"([^"]+)"\s+' + re.escape(target_vip),
                            cmd,
                        )
                        if m_vs:
                            logger.info("  跳过: %s (VIP 已在 '%s' 上)", cmd, vs_name)
                            continue

                        # 替换引用旧 vs 名称的命令
                        if orig_vs and orig_vs != vs_name:
                            cmd = cmd.replace(f'"{orig_vs}"', f'"{vs_name}"')

                        adjusted.append(cmd)

                    precheck_info["adjusted"] = True
                    config_commands = adjusted
                    # 同步更新环境变量（含 VIP 和 VS 名称，确保流量验证用正确地址）
                    env_plan.setdefault("env", {})["LB_VIRTUAL_SERVICE_NAME"] = vs_name
                    env_plan["env"]["LB_VIP"] = vip
                    env_plan["env"]["LB_VIP_PORT"] = port
                    logger.info(
                        "已同步更新 LB_VIP=%s, LB_VIP_PORT=%s", vip, port,
                    )
                    break

            # 无冲突时：仍然用 config_commands 中提取到的真实 VIP 覆盖 env_plan
            # （LLM 生成的 env_plan.LB_VIP 可能是虚构地址）
            if not precheck_info.get("conflict") and target_vip:
                old_vip = env_plan.get("env", {}).get("LB_VIP", "")
                if old_vip != target_vip:
                    env_plan.setdefault("env", {})["LB_VIP"] = target_vip
                    logger.info(
                        "VIP 同步（无冲突）: env_plan.LB_VIP %s → %s",
                        old_vip, target_vip,
                    )

    except Exception as e:
        logger.warning("VIP 预检查失败（不影响继续执行）: %s", e)
    finally:
        if ssh_client:
            ssh_client.disconnect()

    return config_commands, precheck_info


def _classify_config_commands(config_commands: List[str]) -> Tuple[List[str], List[str]]:
    """将配置命令分为「网络基础设施」和「应用层」两组。

    网络基础设施命令（ip address portX, ip route 等）需要在独立的 SSH
    config session 中先执行，确保网络互通后再下发 SLB 等应用层命令。

    NSAE 设备特点：
    - 没有 interface 子模式，ip address 命令格式为:
      ip address <interface_name> <ip> <mask>
    - 静态路由: ip route static <dest> <mask> <gateway>
    - 默认路由: ip route default <gateway>

    Returns:
        (network_commands, app_commands) 二元组。
    """
    network_cmds: List[str] = []
    app_cmds: List[str] = []

    for cmd in config_commands:
        cmd_lower = cmd.strip().lower()
        if cmd_lower.startswith(("ip address ", "no ip address ",
                                  "ip route ", "no ip route ")):
            network_cmds.append(cmd)
        else:
            app_cmds.append(cmd)

    return network_cmds, app_cmds


def _filter_idempotent_network_commands(
    net_cmds: List[str],
) -> List[str]:
    """通过 SSH 预查询设备当前网络配置，过滤已存在的 ip address / ip route 命令。

    避免重复下发导致设备报错或覆盖已有配置。

    Args:
        net_cmds: 待下发的网络基础设施命令列表。

    Returns:
        过滤后仍需下发的命令列表。
    """
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"
    if not enable_push or not net_cmds:
        return net_cmds

    existing_ips: Dict[str, str] = {}   # interface_name → ip
    existing_routes: List[str] = []     # "dest mask gateway" strings

    ssh_client: Optional[NSAESSHClient] = None
    try:
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        ssh_client.enter_enable_mode()

        # 查询当前接口 IP 配置
        results = ssh_client.execute_show_commands(["show ip address", "show ip route"])
        ip_output = results[0]["output"] if len(results) > 0 else ""
        route_output = results[1]["output"] if len(results) > 1 else ""

        # 解析 show ip address 输出
        # 典型行: port3  10.0.3.1  255.255.255.0  ...
        for line in ip_output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                iface = parts[0].lower()
                ip_addr = parts[1]
                # 简单检查是否为 IP 格式
                if re.match(r'\d+\.\d+\.\d+\.\d+', ip_addr):
                    existing_ips[iface] = ip_addr

        # 解析 show ip route 输出 - 提取静态路由
        for line in route_output.splitlines():
            line_stripped = line.strip().lower()
            existing_routes.append(line_stripped)

        logger.info("幂等性预检查: 设备已有 %d 个接口 IP, 路由表 %d 行",
                    len(existing_ips), len(existing_routes))

    except Exception as e:
        logger.warning("网络幂等性预检查失败（不影响继续）: %s", e)
        return net_cmds
    finally:
        if ssh_client:
            ssh_client.disconnect()

    # 过滤已存在的命令
    filtered: List[str] = []
    for cmd in net_cmds:
        cmd_s = cmd.strip()
        cmd_lower = cmd_s.lower()
        skip = False

        # ip address <interface> <ip> <mask>
        m = re.match(r'ip\s+address\s+(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)', cmd_s, re.IGNORECASE)
        if m:
            iface = m.group(1).lower()
            ip_addr = m.group(2)
            if existing_ips.get(iface) == ip_addr:
                logger.info("幂等性跳过（已存在）: %s", cmd_s)
                skip = True

        # ip route static <dest> <mask> <gateway>
        m2 = re.match(r'ip\s+route\s+static\s+(\S+)\s+(\S+)\s+(\S+)', cmd_s, re.IGNORECASE)
        if m2:
            dest = m2.group(1)
            # 检查路由表中是否已有该目的网络
            if any(dest.lower() in r for r in existing_routes):
                logger.info("幂等性跳过（路由已存在）: %s", cmd_s)
                skip = True

        if not skip:
            filtered.append(cmd)

    skipped = len(net_cmds) - len(filtered)
    if skipped > 0:
        logger.info("幂等性过滤: %d/%d 条网络命令已跳过（设备已配置）", skipped, len(net_cmds))

    return filtered


def deploy_config_commands(config_commands: List[str], env_plan: Dict[str, Any]) -> Dict[str, Any]:
    """通过 SSH 连接设备下发配置命令。

    .. deprecated::
        已迁移到 Workforce Pipeline 的 DeployWorker。
        保留此函数作为回退安全网。

    自动将命令分为「网络基础设施」和「应用层」两批：
    - 网络命令（interface/ip address/route）先用独立 SSH 会话推送
    - 应用命令（slb/health 等）再用第二个 SSH 会话推送（确保 config 模式正确）
    """
    logger.info("=" * 80)
    logger.info("下发配置命令（SSH 真实设备）")
    logger.info("=" * 80)

    deploy_logs: List[str] = []
    step_records: List[str] = []
    ssh_banner = ""
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"

    # 分类命令
    net_cmds, app_cmds = _classify_config_commands(config_commands)

    # 幂等性过滤：跳过设备上已存在的网络基础配置
    if net_cmds:
        logger.info("网络基础设施命令 (%d 条, 过滤前):", len(net_cmds))
        for i, cmd in enumerate(net_cmds, 1):
            logger.info("  N%d. %s", i, cmd)
        net_cmds = _filter_idempotent_network_commands(net_cmds)
        if net_cmds:
            logger.info("幂等性过滤后网络命令 (%d 条):", len(net_cmds))
            for i, cmd in enumerate(net_cmds, 1):
                logger.info("  N%d. %s", i, cmd)
        else:
            logger.info("所有网络命令已存在于设备上，无需下发")
    logger.info("应用层命令 (%d 条):", len(app_cmds))
    for i, cmd in enumerate(app_cmds, 1):
        logger.info("  A%d. %s", i, cmd)
    logger.info("")

    if not enable_push:
        logger.warning("LB_ENABLE_PUSH 未启用，跳过真实设备部署（仅记录）")
        for i, cmd in enumerate(config_commands, 1):
            step_records.append(f"步骤{i}: 命令 '{cmd}' - 跳过（PUSH未启用）")
            deploy_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 跳过: {cmd}\n原因: LB_ENABLE_PUSH=false")
        return {
            "deploy_summary": f"共 {len(config_commands)} 个命令，全部跳过（PUSH未启用）",
            "deploy_logs": "\n".join(deploy_logs),
            "step_records": step_records,
            "ssh_banner": "",
            "command_results": [],
            "real_device": False,
        }

    # 可恢复错误模式
    _RECOVERABLE_PATTERNS = [
        "already has this ip and port",
        "already configured",
        "already exists",
        "duplicate entry",
    ]

    all_cmd_results: List[Dict[str, Any]] = []
    success_count = 0
    error_count = 0
    warn_count = 0

    def _push_batch(cmds: List[str], label: str) -> None:
        nonlocal ssh_banner, success_count, error_count, warn_count
        if not cmds:
            return
        ssh_client: Optional[NSAESSHClient] = None
        try:
            ssh_client = create_ssh_client_from_env()
            banner = ssh_client.connect()
            if not ssh_banner:
                ssh_banner = banner
            deploy_logs.append(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SSH 连接建立 ({label}): {ssh_client.host}"
            )
            deploy_logs.append(f"Banner:\n{banner}")

            cmd_results = ssh_client.execute_config_commands(cmds, enter_config=True)

            for result in cmd_results:
                cmd = result["command"]
                output = result["output"]
                status = result["status"]
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                idx = len(all_cmd_results) + 1

                if status == "success":
                    success_count += 1
                    step_records.append(f"步骤{idx}: 执行命令 '{cmd}' - 成功")
                else:
                    output_lower = output.lower()
                    recoverable = any(pat in output_lower for pat in _RECOVERABLE_PATTERNS)
                    if recoverable:
                        warn_count += 1
                        result["status"] = "warning"
                        step_records.append(f"步骤{idx}: 执行命令 '{cmd}' - 警告(已存在): {output[:200]}")
                        logger.warning("命令 '%s' 返回可恢复错误（配置已存在），继续执行", cmd)
                    else:
                        error_count += 1
                        step_records.append(f"步骤{idx}: 执行命令 '{cmd}' - 错误: {output[:200]}")

                deploy_logs.append(f"[{ts}] 执行: {cmd}\n设备输出:\n{output}\n状态: {result['status']}")
                all_cmd_results.append(result)
        except Exception as e:
            logger.error("SSH 配置下发异常 (%s): %s", label, e, exc_info=True)
            deploy_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SSH 异常 ({label}): {e}")
        finally:
            if ssh_client:
                ssh_client.disconnect()

    try:
        # 第一批：网络基础设施（独立 SSH 会话，避免 interface exit 破坏 config 模式）
        if net_cmds:
            logger.info("=== 第一批：网络基础设施命令 (%d 条) ===", len(net_cmds))
            _push_batch(net_cmds, "网络基础设施")
            logger.info("")

        # 第二批：应用层配置（SLB / health 等）
        if app_cmds:
            logger.info("=== 第二批：应用层命令 (%d 条) ===", len(app_cmds))
            _push_batch(app_cmds, "应用层配置")
            logger.info("")

        total = len(config_commands)
        parts = [f"共下发 {total} 个配置命令，成功 {success_count} 个"]
        if warn_count:
            parts.append(f"警告(已存在) {warn_count} 个")
        if error_count:
            parts.append(f"错误 {error_count} 个")
        deploy_summary = "，".join(parts)
        logger.info(deploy_summary)
        logger.info("")

        return {
            "deploy_summary": deploy_summary,
            "deploy_logs": "\n".join(deploy_logs),
            "step_records": step_records,
            "ssh_banner": ssh_banner,
            "command_results": all_cmd_results,
            "real_device": True,
        }

    except Exception as e:
        logger.error(f"SSH 配置下发异常: {e}", exc_info=True)
        deploy_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SSH 异常: {e}")
        return {
            "deploy_summary": f"配置下发失败: {e}",
            "deploy_logs": "\n".join(deploy_logs),
            "step_records": step_records,
            "ssh_banner": ssh_banner,
            "command_results": all_cmd_results,
            "real_device": True,
            "error": str(e),
        }


def verify_test_results(verify_commands: List[str], job_content: str) -> Dict[str, Any]:  # DEPRECATED: 已迁移到 Workforce VerifyShowWorker
    """通过 SSH 连接设备执行验证命令，检查配置是否生效。

    会根据任务内容自动检测产品模块（SLB / LLB / SSL / HA / NAT），
    并获取对应的 running-config 段落与运行状态。
    """
    logger.info("=" * 80)
    logger.info("步骤3: 验证测试结果（SSH 真实设备）")
    logger.info("=" * 80)

    verify_logs = []
    ssh_client: Optional[NSAESSHClient] = None
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"

    logger.info("验证命令列表:")
    for i, cmd in enumerate(verify_commands, 1):
        logger.info(f"  {i}. {cmd}")
    logger.info("")

    if not enable_push:
        logger.warning("LB_ENABLE_PUSH 未启用，跳过真实设备验证（仅记录）")
        for i, cmd in enumerate(verify_commands, 1):
            verify_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 跳过验证: {cmd}")
        return {
            "verify_summary": f"共 {len(verify_commands)} 个验证命令，全部跳过",
            "verify_logs": "\n".join(verify_logs),
            "command_results": [],
            "real_device": False,
        }

    try:
        # 建立 SSH 连接
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        verify_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SSH 验证连接建立: {ssh_client.host}")

        # 先进入 enable 模式执行 show 命令
        ssh_client.enter_enable_mode()

        # 执行验证命令
        cmd_results = ssh_client.execute_show_commands(verify_commands)

        for i, result in enumerate(cmd_results, 1):
            cmd = result["command"]
            output = result["output"]
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            # 在 show slb health 输出前注入提示，避免 AI 分析误判
            if cmd.strip().lower() == "show slb health":
                output = (
                    "【注意】show slb health 仅显示策略摘要，末尾的 GET \"/\" \"200\" "
                    "是固定默认值，不反映 health request/response/server 配置。\n"
                    + output
                )
            verify_logs.append(f"[{ts}] 验证: {cmd}\n设备输出:\n{output}")

        # 根据任务内容自动检测需要获取哪些模块配置
        module_configs: Dict[str, str] = {}
        _job_lower = job_content.lower()

        # 自动检测模块关键字 → 要获取的配置段
        _module_keywords = {
            "slb": ["slb", "负载均衡", "server load", "real server", "virtual server"],
            "health": ["health", "健康检查", "health check"],
            "llb": ["llb", "链路负载", "link load"],
            "ssl": ["ssl", "证书", "certificate", "tls"],
            "ha": ["ha", "高可用", "vrrp", "failover", "冗余"],
            "nat": ["nat", "地址转换", "snat", "dnat"],
        }
        detected_modules: List[str] = []
        for mod, kws in _module_keywords.items():
            if any(kw in _job_lower for kw in kws):
                detected_modules.append(mod)

        # 如果没有检测到任何模块，默认获取 slb + health
        if not detected_modules:
            detected_modules = ["slb", "health"]

        logger.info("检测到模块: %s，获取对应配置摘要...", detected_modules)

        for mod in detected_modules:
            try:
                config_text = ssh_client.get_running_config_section(mod)
                module_configs[mod] = config_text
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                verify_logs.append(f"\n[{ts}] {mod.upper()} 配置摘要:\n{config_text}")
            except Exception as e:
                logger.warning("获取 %s 配置失败: %s", mod, e)

        verify_summary = (
            f"共执行 {len(verify_commands)} 个验证命令，已获取设备实时输出；"
            f"获取 {len(module_configs)} 个模块配置摘要 ({', '.join(detected_modules)})"
        )
        logger.info(verify_summary)
        logger.info("")

        return {
            "verify_summary": verify_summary,
            "verify_logs": "\n".join(verify_logs),
            "command_results": cmd_results,
            "module_configs": module_configs,
            # 向后兼容旧字段
            "slb_running_config": module_configs.get("slb", ""),
            "health_running_config": module_configs.get("health", ""),
            "real_device": True,
        }

    except Exception as e:
        logger.error(f"SSH 验证异常: {e}", exc_info=True)
        verify_logs.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SSH 验证异常: {e}")
        return {
            "verify_summary": f"验证失败: {e}",
            "verify_logs": "\n".join(verify_logs),
            "command_results": [],
            "real_device": True,
            "error": str(e),
        }
    finally:
        if ssh_client:
            ssh_client.disconnect()


# ---------------------------------------------------------------------------
# 流量验证：向 VIP / 本地后端发起 HTTP 请求，检查端到端连通性
# ---------------------------------------------------------------------------

def verify_traffic(  # DEPRECATED: 已迁移到 Workforce TrafficWorker
    env_plan: Dict[str, Any],
    job_content: str,
    *,
    test_plan: Optional[Dict[str, Any]] = None,
    request_count: int = 3,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """在配置下发后，通过 HTTP 请求验证流量是否可达。

    优先请求 VIP（设备上配置的虚拟服务地址）；如果没有 VIP 信息，
    回退到本地 HTTP 后端服务 URL，确认后端至少可访问。

    Args:
        env_plan: 环境规划字典，应含 server_url / env.LB_VIP 等。
        job_content: 任务原文（目前未使用，保留扩展）。
        test_plan: 测试计划字典，包含 traffic_keyword 等参数。
        request_count: 发起 HTTP 请求的次数。
        timeout: 每次请求的超时秒数。

    Returns:
        Dict 包含 traffic_summary / traffic_logs / traffic_results 等。
    """
    logger.info("=" * 80)
    logger.info("步骤3.5: 流量验证（HTTP 端到端）")
    logger.info("=" * 80)

    # 从 test_plan 提取流量验证关键词
    tp = test_plan or {}
    traffic_keyword = tp.get("traffic_keyword") or tp.get("keyword") or "chinamobile"

    traffic_logs: List[str] = []
    traffic_results: List[Dict[str, Any]] = []
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"

    # 确定目标 URL 列表
    target_urls: List[str] = []

    # 1) 优先使用 VIP
    env_vars = env_plan.get("env", {})
    vip = env_vars.get("LB_VIP") or os.environ.get("LB_VIP", "")
    vip_port = env_vars.get("LB_VIP_PORT") or os.environ.get("LB_VIP_PORT", "80")
    if vip and enable_push:
        target_urls.append(f"http://{vip}:{vip_port}/")
        logger.info("VIP 目标: http://%s:%s/", vip, vip_port)

    # 1b) 如果 precheck 发现其他 VIP，添加为 fallback
    precheck_existing = env_plan.get("precheck_existing_vs", {})
    for vs_name, vs_info in precheck_existing.items():
        fb_vip = vs_info.get("vip", "")
        fb_port = vs_info.get("port", "80")
        fb_url = f"http://{fb_vip}:{fb_port}/"
        if fb_vip and fb_url not in target_urls and enable_push:
            target_urls.append(fb_url)
            logger.info("VIP fallback 目标 (%s): %s", vs_name, fb_url)

    # 2) 直接访问 VM 服务端（real server URL）作为 sanity check
    server_url = env_plan.get("server_url", "")
    if server_url:
        rs_url = server_url if server_url.endswith("/") else server_url + "/"
        if rs_url not in target_urls:
            target_urls.append(rs_url)
            logger.info("Real Server 直连目标: %s", rs_url)

    if not target_urls:
        msg = "无可用目标 URL，跳过流量验证"
        logger.warning(msg)
        return {
            "traffic_summary": msg,
            "traffic_logs": [],
            "traffic_results": [],
        }

    logger.info("目标 URL 列表: %s", target_urls)
    logger.info("每个目标发起 %d 次请求，超时 %.1fs", request_count, timeout)
    logger.info("")

    # 获取 VMController 用于远程探测
    vm: Optional[VMController] = env_plan.get("_vm")

    for url in target_urls:
        for i in range(1, request_count + 1):
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            result: Dict[str, Any] = {"url": url, "attempt": i, "timestamp": ts}
            if vm:
                # 从 VM 发起 HTTP 请求（模拟客户端/服务器端访问）
                probe = vm.http_probe(url, keyword=traffic_keyword, timeout=int(timeout))
                if probe.get("success"):
                    result.update({
                        "status_code": probe.get("status_code", 0),
                        "body_length": len(probe.get("body_snippet", "")),
                        "content_match": probe.get("content_match", False),
                        "body_snippet": probe.get("body_snippet", ""),
                        "success": True,
                    })
                    log_line = (
                        f"[{ts}] {url} 第{i}次(VM) → HTTP {probe.get('status_code', '?')}, "
                        f"内容匹配={'是' if probe.get('content_match') else '否'}"
                    )
                else:
                    result.update({
                        "error": probe.get("error", "未知错误"),
                        "success": False,
                    })
                    log_line = f"[{ts}] {url} 第{i}次(VM) → 连接失败: {probe.get('error', '')}"
            else:
                # 兜底：无 VM 时尝试本地 urllib 探测
                try:
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        status_code = resp.status
                        body = resp.read().decode("utf-8", errors="replace")
                        content_match = traffic_keyword.lower() in body.lower()
                        result.update({
                            "status_code": status_code,
                            "body_length": len(body),
                            "content_match": content_match,
                            "body_snippet": body[:500],
                            "success": True,
                        })
                        log_line = (
                            f"[{ts}] {url} 第{i}次(本地) → HTTP {status_code}, "
                            f"内容匹配={'是' if content_match else '否'}"
                        )
                except Exception as e:
                    result.update({
                        "error": str(e),
                        "success": False,
                    })
                    log_line = f"[{ts}] {url} 第{i}次(本地) → 连接失败: {e}"

            traffic_results.append(result)
            traffic_logs.append(log_line)
            logger.info(log_line)

    # 汇总
    total = len(traffic_results)
    success_count = sum(1 for r in traffic_results if r.get("success"))
    match_count = sum(1 for r in traffic_results if r.get("content_match"))
    traffic_summary = (
        f"共发起 {total} 次 HTTP 请求，成功 {success_count} 次，"
        f"内容匹配({traffic_keyword}) {match_count} 次"
    )
    logger.info("")
    logger.info(traffic_summary)
    logger.info("")

    return {
        "traffic_summary": traffic_summary,
        "traffic_logs": traffic_logs,
        "traffic_results": traffic_results,
    }


# ---------------------------------------------------------------------------
# 故障注入与恢复验证
# ---------------------------------------------------------------------------

def _ssh_health_status(
    ssh_client: "NSAESSHClient",
    status_check_commands: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """通过 SSH 查询健康检查状态。

    Args:
        ssh_client: 已连接的 SSH 客户端。
        status_check_commands: 要执行的 show 命令列表（由 TestPlan 提供）。
            如果为 None 则使用 SLB+HTTP 默认命令（向后兼容）。
    """
    if not status_check_commands:
        # 向后兼容：在未提供 test_plan 时使用旧的默认命令
        # 注意：show slb real http 仅显示配置定义，不含 UP/DOWN 状态
        #       show statistics slb real http 才显示运行时状态
        status_check_commands = [
            "show slb group health",
            "show slb health",
            "show statistics slb real http",
        ]
    results = ssh_client.execute_show_commands(status_check_commands)
    outputs = {}
    for r in results:
        cmd = r["command"]
        output = r["output"]
        # 在 show slb health 输出前加注释，防止 AI 分析误判
        if cmd.strip().lower() == "show slb health":
            output = (
                "【注意】show slb health 仅显示健康检查策略摘要，"
                "末尾的 GET \"/\" \"200\" 是固定默认显示值。"
                "如果配置了 health request/response/server（内容健康检查），"
                "其自定义 URL 和关键词参数不会出现在此输出中，"
                "不能据此判定内容匹配健康检查未生效。\n"
                + output
            )
        outputs[cmd] = output
    return {
        "commands": status_check_commands,
        "outputs": outputs,
    }


def _robust_sleep(seconds: int, label: str = "") -> None:
    """Interrupt-resistant sleep.

    VS Code terminal management may send SIGINT/KeyboardInterrupt during
    long ``time.sleep`` calls which kills the pipeline.  This helper
    absorbs such interrupts and continues sleeping for the remaining
    duration.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            logger.warning(
                "收到 KeyboardInterrupt（%s），继续等待剩余 %.0f 秒",
                label,
                max(deadline - time.monotonic(), 0),
            )


def execute_fault_injection(  # DEPRECATED: 已迁移到 Workforce TrafficWorker
    env_plan: Dict[str, Any],
    job_content: str,
    *,
    test_plan: Optional[Dict[str, Any]] = None,
    health_check_interval: int = 15,
    health_check_retries: int = 3,
) -> Dict[str, Any]:
    """执行故障注入与恢复验证 — 数据驱动版。

    步骤序列由 ``test_plan["fault_injection_steps"]`` 定义，而非硬编码。
    每个步骤的 *type* 决定执行动作：
      - stop_service:    停止 HTTP 后端服务
      - restore_service: 恢复 HTTP 后端服务
      - modify_content:  修改页面内容（去除/替换关键词）
      - restore_content: 恢复页面原始内容

    当 ``test_plan`` 为 None 时使用内置默认 4 步 (A-D) 以保持向后兼容。
    """
    logger.info("=" * 80)
    logger.info("故障注入与恢复验证")
    logger.info("=" * 80)

    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"
    if not enable_push:
        msg = "LB_ENABLE_PUSH 未启用，跳过故障注入"
        logger.warning(msg)
        return {"skipped": True, "reason": msg, "steps": []}

    # ── 从 test_plan 提取参数（或 fallback 到函数默认值） ────────────
    tp = test_plan or {}
    keyword = tp.get("keyword", "")
    keyword_replacement = tp.get("keyword_replacement", "")
    status_check_commands: List[str] = tp.get("status_check_commands", [])

    hc_interval = tp.get("health_check_interval") or env_plan.get("health_check_interval") or health_check_interval
    hc_retries = tp.get("health_check_retries") or env_plan.get("health_check_retries") or health_check_retries
    buffer_seconds = int(tp.get("buffer_seconds", 10))
    try:
        hc_interval = int(hc_interval)
        hc_retries = int(hc_retries)
    except (ValueError, TypeError):
        hc_interval = health_check_interval
        hc_retries = health_check_retries

    # 等待时间
    fault_wait = hc_interval * hc_retries + buffer_seconds
    recovery_wait = hc_interval + buffer_seconds

    # 故障注入步骤序列
    fi_steps_def: List[Dict[str, Any]] = tp.get("fault_injection_steps", [])
    if not fi_steps_def:
        # 向后兼容：无 test_plan 时构造默认 4 步
        fi_steps_def = _build_default_fi_steps(keyword, keyword_replacement)

    port = int(env_plan.get("port", 80))
    deploy_result = env_plan.get("deploy_result", {})
    vm: Optional[VMController] = env_plan.get("_vm")

    # 构建服务端 URL (VM eth1 业务 IP)
    env_vars = env_plan.get("env", {})
    bind_ip = env_vars.get("LB_REAL_SERVER_IP") or deploy_result.get("bind_ip", "")
    server_url = f"http://{bind_ip}:{port}/" if bind_ip else ""
    original_html = env_plan.get("original_html", "")
    if not original_html and vm:
        try:
            original_html = vm.read_html_content()
        except Exception:
            pass

    steps: List[Dict[str, Any]] = []
    ssh_client: Optional[NSAESSHClient] = None

    try:
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        ssh_client.enter_enable_mode()

        for fi_step in fi_steps_def:
            step_id = fi_step.get("id", "?")
            step_type = fi_step.get("type", "")
            step_desc = fi_step.get("description", step_type)
            expected_status = fi_step.get("expected_status", "")
            params = fi_step.get("params", {})

            logger.info("")
            logger.info("── 故障注入 步骤%s: %s ──", step_id, step_desc)
            record: Dict[str, Any] = {
                "step": step_id,
                "action": step_desc,
                "type": step_type,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            # ── 执行动作 ────────────────────────────────────
            if step_type == "stop_service":
                record.update(_exec_stop_service(vm, port, env_plan=env_plan))
                if vm and server_url:
                    probe = vm.http_probe(server_url, keyword=keyword)
                else:
                    probe = {"success": False, "error": "VM 或 server_url 不可用"}
                record["local_probe_after_stop"] = probe
                logger.info("VM 探测: %s", "不可达" if not probe.get("success") else "仍可达(异常)")
                # 等待故障检测
                logger.info("等待 %d 秒（故障检测: %d × %d + %d）...",
                            fault_wait, hc_interval, hc_retries, buffer_seconds)
                _robust_sleep(fault_wait, f"步骤{step_id}-故障检测")

            elif step_type == "restore_service":
                record.update(
                    _exec_restore_service(vm, original_html, port, env_plan)
                )
                if vm and server_url:
                    probe = vm.http_probe(server_url, keyword=keyword)
                else:
                    probe = {"success": False, "error": "VM 或 server_url 不可用"}
                record["local_probe_after_restore"] = probe
                logger.info("VM 探测: %s", "可达" if probe.get("success") else "不可达")
                logger.info("等待 %d 秒（恢复检测）...", recovery_wait)
                _robust_sleep(recovery_wait, f"步骤{step_id}-恢复检测")

            elif step_type == "modify_content":
                remove_kw = params.get("remove_keyword", keyword)
                replace_with = params.get("replace_with", keyword_replacement)
                record.update(
                    _exec_modify_content(vm, original_html, remove_kw, replace_with)
                )
                if vm and server_url:
                    probe = vm.http_probe(server_url, keyword=remove_kw)
                else:
                    probe = {"success": False, "error": "VM 或 server_url 不可用"}
                record["local_probe_after_modify"] = probe
                logger.info("VM 探测: 内容匹配='%s' → %s",
                            remove_kw,
                            "是(异常)" if probe.get("content_match") else "否(预期)")
                logger.info("等待 %d 秒（故障检测）...", fault_wait)
                _robust_sleep(fault_wait, f"步骤{step_id}-故障检测")

            elif step_type == "restore_content":
                record.update(
                    _exec_restore_content(vm, original_html, keyword)
                )
                if vm and server_url:
                    probe = vm.http_probe(server_url, keyword=keyword)
                else:
                    probe = {"success": False, "error": "VM 或 server_url 不可用"}
                record["local_probe_after_restore"] = probe
                logger.info("VM 探测: 内容匹配='%s' → %s",
                            keyword,
                            "是" if probe.get("content_match") else "否(异常)")
                logger.info("等待 %d 秒（恢复检测）...", recovery_wait)
                _robust_sleep(recovery_wait, f"步骤{step_id}-恢复检测")

            else:
                logger.warning("未知故障注入步骤类型: %s，跳过", step_type)
                record["skipped"] = True

            # ── SSH 查询健康状态 ────────────────────────────
            if not record.get("skipped"):
                record["health_status"] = _ssh_health_status(
                    ssh_client, status_check_commands=status_check_commands
                )
            record["expected"] = f"{step_desc} → 预期 {expected_status}"
            steps.append(record)
            logger.info("SSH 健康状态查询完成")

    except Exception as e:
        logger.error("故障注入阶段异常: %s", e, exc_info=True)
        steps.append({"step": "ERROR", "error": str(e)})
    finally:
        if ssh_client:
            ssh_client.disconnect()

    # 汇总
    summary_parts = []
    for s in steps:
        summary_parts.append(f"步骤{s['step']}: {s.get('action', '')} → {s.get('expected', '')}")
    fault_summary = "; ".join(summary_parts)
    logger.info("")
    logger.info("故障注入完成: %s", fault_summary)

    return {
        "skipped": False,
        "steps": steps,
        "summary": fault_summary,
        "fault_wait_seconds": fault_wait,
        "recovery_wait_seconds": recovery_wait,
    }


# ── 故障注入辅助函数 ─────────────────────────────────────────────────

def _build_default_fi_steps(
    keyword: str, keyword_replacement: str
) -> List[Dict[str, Any]]:
    """在无 test_plan 时构造向后兼容的默认 4 步 (A-D)。"""
    kw = keyword or "chinamobile"
    kw_rep = keyword_replacement or "china mobile"
    return [
        {
            "id": "A", "type": "stop_service",
            "description": "停止HTTP服务",
            "expected_status": "DOWN", "params": {},
        },
        {
            "id": "B", "type": "restore_service",
            "description": "恢复HTTP服务",
            "expected_status": "UP", "params": {},
        },
        {
            "id": "C", "type": "modify_content",
            "description": f"修改页面内容_去除关键词({kw})",
            "expected_status": "DOWN",
            "params": {"remove_keyword": kw, "replace_with": kw_rep},
        },
        {
            "id": "D", "type": "restore_content",
            "description": "恢复页面内容",
            "expected_status": "UP", "params": {},
        },
    ]


def _exec_stop_service(
    vm: Optional["VMController"], port: int,
    env_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """停止 VM 上的 HTTP 后端服务（支持多端口模式）。"""
    result: Dict[str, Any] = {}
    if not vm:
        logger.warning("无法停止 HTTP 服务：VMController 不可用")
        result["stopped"] = False
        return result

    ep = env_plan or {}
    multi_mode = ep.get("_multi_server_mode", False)
    real_servers = ep.get("_real_servers", [])

    try:
        if multi_mode and real_servers:
            ports = [int(rs.get("port", 80)) for rs in real_servers]
            vm.stop_multi_http_servers(ports)
            logger.info("VM HTTP 服务已停止（多端口模式，端口: %s）", ports)
        else:
            vm.stop_http_server(port=port)
            logger.info("VM HTTP 服务已停止（端口 %d）", port)
        result["stopped"] = True
    except Exception as e:
        logger.warning("VM 停止 HTTP 服务失败: %s", e)
        result["stopped"] = False
    return result


def _exec_restore_service(
    vm: Optional["VMController"],
    original_html: str,
    port: int,
    env_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """恢复 VM 上的 HTTP 后端服务（支持多端口重新部署）。"""
    result: Dict[str, Any] = {}
    if not (vm and original_html):
        result["redeploy_result"] = False
        logger.warning("无法恢复 HTTP 服务: VMController 或原始 HTML 不可用")
        return result

    multi_mode = env_plan.get("_multi_server_mode", False)
    real_servers = env_plan.get("_real_servers", [])

    try:
        if multi_mode and real_servers:
            env_vars = env_plan.get("env", {})
            bind_ip = env_vars.get("LB_REAL_SERVER_IP") or env_plan.get("deploy_result", {}).get("bind_ip", "0.0.0.0")
            multi_results = vm.restart_multi_http_servers(original_html, real_servers, bind_ip)
            ok_count = sum(1 for r in multi_results if r.get("success"))
            result["redeploy_result"] = ok_count > 0
            logger.info("VM HTTP 服务已恢复（多端口模式: %d/%d 成功）", ok_count, len(real_servers))
        else:
            env_vars = env_plan.get("env", {})
            bind_ip = env_vars.get("LB_REAL_SERVER_IP") or env_plan.get("deploy_result", {}).get("bind_ip", "0.0.0.0")
            vm.restart_http_server(original_html, port, bind_ip)
            result["redeploy_result"] = True
            logger.info("VM HTTP 服务已恢复（%s:%d）", bind_ip, port)
    except Exception as e:
        logger.warning("VM HTTP 服务恢复失败: %s", e)
        result["redeploy_result"] = False
    return result


def _exec_modify_content(
    vm: Optional["VMController"],
    original_html: str,
    remove_keyword: str,
    replace_with: str,
) -> Dict[str, Any]:
    """修改 VM 上的 HTML 页面内容（替换关键词）。"""
    result: Dict[str, Any] = {}
    if vm and remove_keyword:
        modified_html = original_html.replace(remove_keyword, replace_with)
        try:
            vm.modify_html_content(modified_html)
            result["modified_content"] = f"将 '{remove_keyword}' 替换为 '{replace_with}'"
            logger.info("VM HTML 页面已修改: '%s' → '%s'", remove_keyword, replace_with)
        except Exception as e:
            result["error"] = f"修改失败: {e}"
            logger.warning("VM 修改 HTML 失败: %s", e)
    else:
        result["error"] = "VMController 不可用或关键词为空"
        logger.warning("无法修改 HTML: vm=%s, keyword=%s", vm, remove_keyword)
    return result


def _exec_restore_content(
    vm: Optional["VMController"],
    original_html: str,
    keyword: str,
) -> Dict[str, Any]:
    """恢复 VM 上的 HTML 页面原始内容。"""
    result: Dict[str, Any] = {}
    if vm and original_html:
        try:
            vm.modify_html_content(original_html)
            result["restored"] = True
            logger.info("VM HTML 页面已恢复原始内容")
        except Exception as e:
            result["restored"] = False
            logger.warning("VM 恢复 HTML 失败: %s", e)
    else:
        result["restored"] = False
    return result


# ---------------------------------------------------------------------------
# 服务清理
# ---------------------------------------------------------------------------

def _cleanup_device_slb_config(config_commands: List[str], env_plan: Dict[str, Any]) -> None:
    """清理设备上本次测试下发的 SLB 配置（反向删除，确保不影响下一个任务）。

    删除顺序（依赖关系的逆序）：
    1. slb policy default → 解除虚拟服务与组的绑定
    2. slb virtual disable → 禁用虚拟服务
    3. slb virtual enable → （跳过）
    4. slb group member → 移除组成员
    5. slb group method → （method 无需单独删除）
    6. slb group health → 解除健康检查绑定
    7. health server → 解除 health request/response 绑定
    8. slb health → 删除健康检查
    9. health request / health response → 删除自定义请求/响应
    10. slb real http → 删除真实服务器
    11. no slb virtual http → 删除虚拟服务
    12. ip address / ip route → 保留（不删除基础网络，避免影响管理连接）
    """
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"
    if not enable_push:
        logger.info("LB_ENABLE_PUSH 未启用，跳过设备 SLB 清理")
        return

    # 从 config_commands 中提取需要删除的资源名称
    cleanup_cmds: List[str] = []

    # 收集资源名称
    virtual_names = []
    real_names = []
    health_names = []
    group_names = []
    health_req_indices = []
    health_resp_indices = []
    health_server_names = []

    for cmd in config_commands:
        cmd_s = cmd.strip()
        # slb virtual http "name" ...
        m = re.match(r'slb\s+virtual\s+http\s+"([^"]+)"', cmd_s)
        if m:
            virtual_names.append(m.group(1))
            continue
        # slb real http "name" ...
        m = re.match(r'slb\s+real\s+http\s+"([^"]+)"', cmd_s)
        if m:
            real_names.append(m.group(1))
            continue
        # slb health "name" ...
        m = re.match(r'slb\s+health\s+"([^"]+)"', cmd_s)
        if m:
            health_names.append(m.group(1))
            continue
        # slb group method "name" ...  or slb group member "name" ...
        m = re.match(r'slb\s+group\s+(?:method|member|health)\s+"([^"]+)"', cmd_s)
        if m:
            if m.group(1) not in group_names:
                group_names.append(m.group(1))
            continue
        # slb policy default "vs_name" "group_name"
        m = re.match(r'slb\s+policy\s+default\s+"([^"]+)"', cmd_s)
        if m:
            # virtual name already captured above
            continue
        # health request <idx> ...
        m = re.match(r'health\s+request\s+(\d+)', cmd_s)
        if m:
            health_req_indices.append(m.group(1))
            continue
        # health response <idx> ...
        m = re.match(r'health\s+response\s+(\d+)', cmd_s)
        if m:
            health_resp_indices.append(m.group(1))
            continue
        # health server "name" ...
        m = re.match(r'health\s+server\s+"([^"]+)"', cmd_s)
        if m:
            health_server_names.append(m.group(1))
            continue

    if not any([virtual_names, real_names, health_names, group_names]):
        logger.info("无 SLB 配置需要清理")
        return

    logger.info("准备清理设备 SLB 配置: VS=%s, RS=%s, HC=%s, GRP=%s",
                virtual_names, real_names, health_names, group_names)

    # 按依赖反序构建删除命令
    for vs in virtual_names:
        cleanup_cmds.append(f'no slb policy default "{vs}"')
    for vs in virtual_names:
        cleanup_cmds.append(f'slb virtual disable "{vs}"')
    for gn in group_names:
        for rn in real_names:
            cleanup_cmds.append(f'no slb group member "{gn}" "{rn}"')
    for gn in group_names:
        for hn in health_names:
            cleanup_cmds.append(f'no slb group health "{gn}" "{hn}"')
    for hs in health_server_names:
        cleanup_cmds.append(f'no health server "{hs}"')
    for hn in health_names:
        cleanup_cmds.append(f'no slb health "{hn}"')
    for idx in health_req_indices:
        cleanup_cmds.append(f'no health request {idx}')
    for idx in health_resp_indices:
        cleanup_cmds.append(f'no health response {idx}')
    for rn in real_names:
        cleanup_cmds.append(f'no slb real http "{rn}"')
    for vs in virtual_names:
        cleanup_cmds.append(f'no slb virtual http "{vs}"')

    if not cleanup_cmds:
        return

    logger.info("设备 SLB 清理命令 (%d 条):", len(cleanup_cmds))
    for i, cmd in enumerate(cleanup_cmds, 1):
        logger.info("  D%d. %s", i, cmd)

    ssh_client: Optional[NSAESSHClient] = None
    try:
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        results = ssh_client.execute_config_commands(cleanup_cmds, enter_config=True)
        ok = sum(1 for r in results if r["status"] == "success")
        err = len(results) - ok
        # 清理时的"error"很多是"not found"类的，属于正常
        logger.info("设备 SLB 清理完成: %d 成功, %d 失败/跳过 (not-found 属正常)", ok, err)
    except Exception as e:
        logger.warning("设备 SLB 清理异常（不影响后续任务）: %s", e)
    finally:
        if ssh_client:
            ssh_client.disconnect()


def _ai_cleanup_device_config(
    config_commands: List[str],
    env_plan: Dict[str, Any],
    model: object,
) -> bool:
    """使用 AI Agent 生成并执行设备清理命令。

    Args:
        config_commands: 本次下发的配置命令列表。
        env_plan: 环境规划。
        model: LLM 模型后端。

    Returns:
        True 表示 AI 清理成功执行，False 表示需要回退到硬编码清理。
    """
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false").lower() == "true"
    if not enable_push:
        logger.info("LB_ENABLE_PUSH 未启用，跳过 AI 清理")
        return True  # 不需要清理

    ssh_client: Optional[NSAESSHClient] = None
    try:
        # 1) 收集设备当前状态
        ssh_client = create_ssh_client_from_env()
        ssh_client.connect()
        logger.info("AI 清理: 正在收集设备当前状态...")

        show_cmds = [
            "show slb virtual http",
            "show slb real http",
            "show slb group method",
            "show slb group member",
            "show slb group health",
            "show slb policy default",
            "show slb health",
            "show ip address",
            "show ip route",
        ]
        show_results = ssh_client.execute_show_commands(show_cmds)
        device_state: Dict[str, str] = {}
        for r in show_results:
            device_state[r["command"]] = r["output"]

        # 2) 调用 cleanup agent 生成清理命令
        logger.info("AI 清理: 正在调用 cleanup agent 生成清理命令...")
        cleanup_agent = build_cleanup_agent(model)
        prompt = build_cleanup_prompt(config_commands, device_state, env_plan)
        logger.info("AI 清理 prompt (前 500 字符): %s", prompt[:500])

        from camel.messages import BaseMessage as _BM
        response = cleanup_agent.step(
            _BM.make_user_message(role_name="User", content=prompt)
        )
        raw_content = response.msgs[0].content if response.msgs else ""
        logger.info("AI 清理 Agent 返回 (前 800 字符): %s", raw_content[:800])

        # 3) 解析 JSON
        cleanup_data = _extract_json_object(raw_content)
        if not cleanup_data:
            logger.warning("AI 清理: 无法解析 Agent 返回的 JSON，回退到硬编码清理")
            return False

        cleanup_cmds = cleanup_data.get("cleanup_commands", [])
        notes = cleanup_data.get("notes", "")
        if not cleanup_cmds:
            logger.info("AI 清理: Agent 认为无需清理。notes=%s", notes)
            return True

        logger.info("AI 清理: 生成 %d 条清理命令 (notes: %s)", len(cleanup_cmds), notes)
        for i, cmd in enumerate(cleanup_cmds, 1):
            logger.info("  AI-D%d. %s", i, cmd)

        # 4) 执行清理命令
        results = ssh_client.execute_config_commands(cleanup_cmds, enter_config=True)
        ok = sum(1 for r in results if r["status"] == "success")
        err = len(results) - ok
        logger.info(
            "AI 清理完成: %d 成功, %d 失败/跳过 (not-found 属正常)", ok, err
        )
        return True

    except Exception as e:
        logger.warning("AI 清理异常，回退到硬编码清理: %s", e)
        return False
    finally:
        if ssh_client:
            try:
                ssh_client.disconnect()
            except Exception:
                pass


def cleanup_test_environment(  # DEPRECATED: 已迁移到 Workforce CleanupWorker
    env_plan: Dict[str, Any],
    config_commands: Optional[List[str]] = None,
    model: object = None,
) -> None:
    """清理测试环境：停止 VM HTTP 服务、清理设备配置、断开 SSH。

    优先使用 AI Agent 驱动的智能清理；如果 AI 清理失败则回退到硬编码正则清理。

    Args:
        env_plan: 环境规划。
        config_commands: 本次测试下发的配置命令（用于反向清理设备配置）。
        model: LLM 模型后端（传入后启用 AI 清理；为 None 时走硬编码清理）。
    """
    port = env_plan.get("port")
    if not port:
        return
    try:
        port = int(port)
    except (ValueError, TypeError):
        return

    # 1) 清理设备配置（AI 优先，硬编码兜底）
    if config_commands:
        ai_ok = False
        if model is not None:
            logger.info("使用 AI Agent 清理设备配置...")
            try:
                ai_ok = _ai_cleanup_device_config(config_commands, env_plan, model)
            except Exception as e:
                logger.warning("AI 清理调用异常: %s", e)
        if not ai_ok:
            logger.info("回退到硬编码清理设备 SLB 配置...")
            try:
                _cleanup_device_slb_config(config_commands, env_plan)
            except Exception as e:
                logger.warning("设备 SLB 清理失败（不影响后续）: %s", e)

    # 2) 停止 VM HTTP 服务、清理 IP 别名、断开 SSH
    logger.info("清理测试环境: 停止 VM HTTP 服务（端口 %d）...", port)
    try:
        vm: Optional[VMController] = env_plan.pop("_vm", None)
        multi_mode = env_plan.pop("_multi_server_mode", False)
        real_servers = env_plan.pop("_real_servers", [])

        if vm:
            try:
                if multi_mode and real_servers:
                    ports = [int(rs.get("port", 80)) for rs in real_servers]
                    vm.stop_multi_http_servers(ports)
                    logger.info("VM HTTP 服务已停止（多端口模式: %s）", ports)
                else:
                    vm.stop_http_server()
                    logger.info("VM HTTP 服务已停止: 端口 %d", port)
            except Exception as e:
                logger.warning("停止 VM HTTP 服务失败: %s", e)
            # 清理 eth1 上的额外 IP 别名
            extra_ips = env_plan.pop("_all_real_server_ips", [])
            vm_eth1_ip = env_plan.get("env", {}).get("VM_ETH1_IP", "")
            for ip in extra_ips:
                if ip and ip != vm_eth1_ip:
                    try:
                        mask = env_plan.get("env", {}).get("VM_ETH1_MASK", "255.255.255.0")
                        prefix = VMController._mask_to_prefix(mask)
                        vm.run(f"ip addr del {ip}/{prefix} dev eth1")
                        logger.info("已清理 VM eth1 IP 别名: %s", ip)
                    except Exception:
                        pass
            vm.disconnect()
            logger.info("VM SSH 连接已清理")
        else:
            logger.info("无 VM 连接需要清理")
    except Exception as e:
        logger.warning("清理 VM 环境异常（端口 %d）: %s", port, e)


def analyze_test_result(  # DEPRECATED: 已迁移到 Workforce AnalysisWorker
    job_content: str,
    env_plan: Dict[str, Any],
    config_commands: List[str],
    deploy_info: Dict[str, Any],
    verify_info: Dict[str, Any],
    model,
    traffic_info: Optional[Dict[str, Any]] = None,
    fault_injection_info: Optional[Dict[str, Any]] = None,
    precheck_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """分析测试结果，确认测试通过与不通过

    .. deprecated::
        已迁移到 Workforce Pipeline 的 AnalysisWorker。
    """
    logger.info("=" * 80)
    logger.info("步骤4: 分析测试结果")
    logger.info("=" * 80)
    
    try:
        analysis_agent = build_task_analysis_agent(model)
        # 过滤掉不可序列化的内部字段（如 _vm 对象）
        _env_for_json = {k: v for k, v in env_plan.items() if not k.startswith("_")}
        analysis_prompt = build_task_analysis_prompt(
            task_content=job_content,
            env_summary=json.dumps(_env_for_json, ensure_ascii=False, indent=2),
            config_commands=config_commands,
            deploy_summary=deploy_info.get("deploy_summary", ""),
            deploy_logs=deploy_info.get("deploy_logs", ""),
            step_records=deploy_info.get("step_records", []),
            verify_summary=verify_info.get("verify_summary", ""),
            verify_logs=verify_info.get("verify_logs", ""),
            traffic_summary=traffic_info.get("traffic_summary", "") if traffic_info else "",
            traffic_logs=traffic_info.get("traffic_logs", []) if traffic_info else [],
            fault_injection_info=fault_injection_info,
            precheck_info=precheck_info,
        )
        
        logger.info("任务分析Agent输入提示词（前500字符）:")
        logger.info(analysis_prompt[:500] + "..." if len(analysis_prompt) > 500 else analysis_prompt)
        logger.info("")
        
        logger.info("调用任务分析Agent...")
        analysis_response = analysis_agent.step(analysis_prompt)
        analysis_text = analysis_response.msgs[0].content if analysis_response.msgs else ""
        
        logger.info("任务分析Agent输出:")
        logger.info(analysis_text)
        logger.info("")
        
        # 解析分析结果
        analysis_result = _extract_json_object(analysis_text) or {}
        
        return {
            "analysis_text": analysis_text,
            "analysis_result": analysis_result,
        }
        
    except Exception as e:
        logger.error(f"测试结果分析失败: {e}", exc_info=True)
        return {
            "analysis_text": f"分析失败: {e}",
            "analysis_result": {},
        }


def generate_md_report(test_result: Dict[str, Any], output_path: Path):
    """生成详细的 Markdown 测试报告，涵盖整个测试流程"""
    ts = test_result.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
    job_file = test_result.get("job_file", "unknown")
    job_content = test_result.get("job_content", "")
    decomposition = test_result.get("decomposition", {})
    config_commands = test_result.get("config_commands", [])
    verify_commands = test_result.get("verify_commands", [])
    env_plan = test_result.get("env_plan", {})
    deploy_info = test_result.get("deploy_info", {})
    verify_info = test_result.get("verify_info", {})
    analysis = test_result.get("analysis", {})

    # 设备信息
    device_ip = os.environ.get("LB_DEVICE_IP", "N/A")
    device_user = os.environ.get("LB_USERNAME", "N/A")
    enable_push = os.environ.get("LB_ENABLE_PUSH", "false")
    real_server_subnet = os.environ.get("LB_REAL_SERVER_SUBNET", "N/A")
    vip_subnet = os.environ.get("LB_VIP_SUBNET", "N/A")

    lines = []
    lines.append(f"# INAGENT 自动化测试报告")
    lines.append(f"")
    lines.append(f"**生成时间**: {ts}  ")
    lines.append(f"**测试任务**: {Path(job_file).name}  ")
    lines.append(f"**测试状态**: {test_result.get('status', 'unknown')}  ")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    # 1. 测试环境信息
    lines.append(f"## 1. 测试环境")
    lines.append(f"")
    lines.append(f"### 1.1 设备信息")
    lines.append(f"| 参数 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 设备IP | `{device_ip}` |")
    lines.append(f"| 用户名 | `{device_user}` |")
    lines.append(f"| SSH端口 | `22` |")
    lines.append(f"| 配置推送 | `{enable_push}` |")
    lines.append(f"| Real Server 子网 | `{real_server_subnet}` |")
    lines.append(f"| VIP 子网 | `{vip_subnet}` |")
    lines.append(f"")

    ssh_banner = deploy_info.get("ssh_banner", "")
    if ssh_banner:
        lines.append(f"### 1.2 SSH 登录 Banner")
        lines.append(f"```")
        lines.append(ssh_banner.strip())
        lines.append(f"```")
        lines.append(f"")

    lines.append(f"### 1.3 测试 HTTP 环境")
    lines.append(f"| 参数 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 服务URL | `{env_plan.get('server_url', 'N/A')}` |")
    lines.append(f"| 端口 | `{env_plan.get('port', 'N/A')}` |")
    html_content = env_plan.get("html_content", "")
    if html_content:
        lines.append(f"| HTML内容 | 见下方 |")
    lines.append(f"")
    if html_content:
        lines.append(f"**部署的 HTML 内容：**")
        lines.append(f"```html")
        lines.append(html_content[:2000])
        lines.append(f"```")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    # 2. 测试任务描述
    lines.append(f"## 2. 测试任务描述")
    lines.append(f"")
    lines.append(f"```")
    lines.append(job_content.strip())
    lines.append(f"```")
    lines.append(f"")

    # 3. 任务分解
    lines.append(f"## 3. 任务分解结果（AI Agent）")
    lines.append(f"")
    if isinstance(decomposition, dict):
        task_name = decomposition.get("task_name", "N/A")
        lines.append(f"**任务名称**: {task_name}  ")
        rag_queries = decomposition.get("rag_queries", [])
        if rag_queries:
            lines.append(f"")
            lines.append(f"### 3.1 RAG 查询列表")
            for i, q in enumerate(rag_queries, 1):
                lines.append(f"{i}. {q}")
            lines.append(f"")
        steps = decomposition.get("steps", [])
        if steps:
            lines.append(f"### 3.2 分解步骤")
            for i, step in enumerate(steps, 1):
                if isinstance(step, dict):
                    lines.append(f"{i}. **{step.get('name', '')}**: {step.get('description', '')}")
                else:
                    lines.append(f"{i}. {step}")
            lines.append(f"")
    else:
        lines.append(f"```json")
        lines.append(json.dumps(decomposition, ensure_ascii=False, indent=2))
        lines.append(f"```")
        lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    # 4. 生成的配置命令
    lines.append(f"## 4. 生成的配置命令")
    lines.append(f"")
    lines.append(f"### 4.1 配置命令 ({len(config_commands)} 条)")
    lines.append(f"```")
    for i, cmd in enumerate(config_commands, 1):
        lines.append(f"{i:2d}. {cmd}")
    lines.append(f"```")
    lines.append(f"")
    lines.append(f"### 4.2 验证命令 ({len(verify_commands)} 条)")
    lines.append(f"```")
    for i, cmd in enumerate(verify_commands, 1):
        lines.append(f"{i:2d}. {cmd}")
    lines.append(f"```")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    # 5. 配置下发结果
    lines.append(f"## 5. 配置下发结果（SSH）")
    lines.append(f"")
    if deploy_info.get("workforce"):
        # Workforce Pipeline 模式：输出 Worker 文本摘要
        lines.append(f"**执行方式**: CAMEL Workforce Pipeline (DeployWorker)  ")
        lines.append(f"")
        deploy_text = deploy_info.get("deploy_summary", "")
        if deploy_text:
            lines.append(f"### 5.1 Worker 执行输出")
            lines.append(f"")
            lines.append(deploy_text.strip())
            lines.append(f"")
        else:
            lines.append(f"(Worker 无输出)")
            lines.append(f"")
    else:
        # 传统模式：逐条命令结果
        real_device = deploy_info.get("real_device", False)
        lines.append(f"**执行方式**: {'真实 SSH 设备' if real_device else '模拟（PUSH未启用）'}  ")
        lines.append(f"**摘要**: {deploy_info.get('deploy_summary', 'N/A')}  ")
        lines.append(f"")

        cmd_results = deploy_info.get("command_results", [])
        if cmd_results:
            lines.append(f"### 5.1 逐条命令执行详情")
            lines.append(f"")
            for i, r in enumerate(cmd_results, 1):
                status_icon = "✅" if r.get("status") == "success" else "❌"
                lines.append(f"#### 命令 {i}: {status_icon} `{r.get('command', '')}`")
                lines.append(f"")
                lines.append(f"**状态**: {r.get('status', 'unknown')}  ")
                output = r.get("output", "").strip()
                if output:
                    lines.append(f"**设备输出**:")
                    lines.append(f"```")
                    lines.append(output)
                    lines.append(f"```")
                else:
                    lines.append(f"**设备输出**: (无输出)")
                lines.append(f"")
        else:
            # 回退到日志
            deploy_logs = deploy_info.get("deploy_logs", "")
            if deploy_logs:
                lines.append(f"### 5.1 执行日志")
                lines.append(f"```")
                lines.append(deploy_logs)
                lines.append(f"```")
                lines.append(f"")

        step_records = deploy_info.get("step_records", [])
        if step_records:
            lines.append(f"### 5.2 步骤记录")
            for rec in step_records:
                lines.append(f"- {rec}")
            lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    # 6. 流量验证（端到端 HTTP 访问）
    traffic_info = test_result.get("traffic_info", {})
    lines.append(f"## 6. 流量验证（HTTP 端到端）")
    lines.append(f"")
    if traffic_info.get("workforce"):
        # Workforce Pipeline 模式：输出 Worker 文本摘要
        traffic_text = traffic_info.get("traffic_summary", "")
        if traffic_text:
            lines.append(f"**执行方式**: CAMEL Workforce Pipeline (TrafficWorker)  ")
            lines.append(f"")
            lines.append(f"### 6.1 Worker 执行输出")
            lines.append(f"")
            lines.append(traffic_text.strip())
            lines.append(f"")
        else:
            lines.append(f"(Worker 无输出)")
            lines.append(f"")
    else:
        # 传统模式：逐条 HTTP 请求结果
        traffic_summary = traffic_info.get("traffic_summary", "")
        if traffic_summary:
            lines.append(f"**摘要**: {traffic_summary}  ")
            lines.append(f"")
            traffic_results = traffic_info.get("traffic_results", [])
            if traffic_results:
                lines.append(f"### 6.1 HTTP 请求详情")
                lines.append(f"| # | 目标 URL | 状态码 | 内容匹配 | 结果 |")
                lines.append(f"|---|---------|--------|---------|------|")
                for i, tr in enumerate(traffic_results, 1):
                    url = tr.get("url", "N/A")
                    sc = tr.get("status_code", "-")
                    cm = "✅ 是" if tr.get("content_match") else "❌ 否"
                    ok = "✅" if tr.get("success") else "❌"
                    err = tr.get("error", "")
                    if err:
                        cm = f"- ({err[:60]})"
                    lines.append(f"| {i} | `{url}` | {sc} | {cm} | {ok} |")
                lines.append(f"")
            traffic_logs = traffic_info.get("traffic_logs", [])
            if traffic_logs:
                lines.append(f"### 6.2 流量验证日志")
                lines.append(f"```")
                for tl in traffic_logs:
                    lines.append(tl)
                lines.append(f"```")
                lines.append(f"")
        else:
            lines.append(f"(未执行流量验证)")
            lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    # 6.5 VIP 预检查
    precheck_info = test_result.get("precheck_info", {})
    if precheck_info and precheck_info.get("checked"):
        lines.append(f"## 6.5 VIP 预检查")
        lines.append(f"")
        if precheck_info.get("conflict"):
            lines.append(f"**状态**: ⚠️ 发现 VIP 冲突，已自动复用")
            lines.append(f"**复用虚拟服务**: `{precheck_info.get('reuse_vs', 'N/A')}`  ")
            lines.append(f"**目标 VIP**: `{precheck_info.get('target_vip', 'N/A')}`  ")
        else:
            lines.append(f"**状态**: ✅ 未发现 VIP 冲突")
        existing_vs = precheck_info.get("existing_vs", {})
        if existing_vs:
            lines.append(f"")
            lines.append(f"### 设备已有虚拟服务")
            lines.append(f"| 虚拟服务名 | VIP | 端口 |")
            lines.append(f"|-----------|-----|------|")
            for name, info in existing_vs.items():
                lines.append(f"| `{name}` | `{info.get('vip', '')}` | `{info.get('port', '')}` |")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    # 6.8 故障注入与恢复验证
    fault_injection_info = test_result.get("fault_injection_info", {})
    if fault_injection_info and fault_injection_info.get("workforce"):
        # Workforce 模式：故障注入由 TrafficWorker 统一处理
        lines.append(f"## 6.8 故障注入与恢复验证")
        lines.append(f"")
        lines.append(f"ℹ️ 在 Workforce Pipeline 模式下，故障注入已由 TrafficWorker 统一执行，详见第 6 节。")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")
    elif fault_injection_info and not fault_injection_info.get("skipped"):
        lines.append(f"## 6.8 故障注入与恢复验证")
        lines.append(f"")
        lines.append(f"**故障检测等待时间**: {fault_injection_info.get('fault_wait_seconds', 'N/A')} 秒  ")
        lines.append(f"**恢复检测等待时间**: {fault_injection_info.get('recovery_wait_seconds', 'N/A')} 秒  ")
        lines.append(f"")

        fi_steps = fault_injection_info.get("steps", [])
        if fi_steps:
            lines.append(f"### 步骤总览")
            lines.append(f"| 步骤 | 操作 | 预期结果 |")
            lines.append(f"|------|------|---------|")
            for s in fi_steps:
                lines.append(f"| {s.get('step', '?')} | {s.get('action', '')} | {s.get('expected', '')} |")
            lines.append(f"")

            for s in fi_steps:
                step_id = s.get("step", "?")
                action = s.get("action", "")
                expected = s.get("expected", "")
                lines.append(f"#### 步骤 {step_id}: {action}")
                lines.append(f"")
                lines.append(f"**预期**: {expected}  ")
                lines.append(f"**时间**: {s.get('timestamp', 'N/A')}  ")
                lines.append(f"")

                # VM HTTP 探测结果
                for probe_key in ("local_probe_after_stop", "local_probe_after_restore", "local_probe_after_modify"):
                    if probe_key in s:
                        probe = s[probe_key]
                        probe_label = probe_key.replace("local_probe_after_", "").replace("_", " ").title()
                        probe_status = "✅ 可达" if probe.get("success") else f"❌ 不可达 ({probe.get('error', '')})"
                        cm = probe.get("content_match", "N/A")
                        content_label = f"✅ 匹配" if cm is True else (f"❌ 不匹配" if cm is False else str(cm))
                        lines.append(f"**VM探测 ({probe_label})**: {probe_status}, 内容匹配: {content_label}  ")

                # SSH 健康状态查询
                hs = s.get("health_status", {})
                if hs:
                    for cmd, output in hs.get("outputs", {}).items():
                        lines.append(f"")
                        lines.append(f"**SSH `{cmd}`**:")
                        lines.append(f"```")
                        lines.append(str(output).strip() if output else "(无输出)")
                        lines.append(f"```")
                lines.append(f"")
        lines.append(f"---")
        lines.append(f"")
    elif fault_injection_info and fault_injection_info.get("skipped"):
        lines.append(f"## 6.8 故障注入与恢复验证")
        lines.append(f"")
        lines.append(f"⏭️ 已跳过 — {fault_injection_info.get('reason', '未知原因')}")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    # 7. 验证结果
    lines.append(f"## 7. 测试验证结果（SSH）")
    lines.append(f"")
    if verify_info.get("workforce"):
        # Workforce Pipeline 模式：输出 Worker 文本摘要
        lines.append(f"**执行方式**: CAMEL Workforce Pipeline (VerifyShowWorker)  ")
        lines.append(f"")
        verify_text = verify_info.get("verify_summary", "")
        if verify_text:
            lines.append(f"### 7.1 Worker 执行输出")
            lines.append(f"")
            lines.append(verify_text.strip())
            lines.append(f"")
        else:
            lines.append(f"(Worker 无输出)")
            lines.append(f"")
    else:
        # 传统模式：逐条命令结果
        real_verify = verify_info.get("real_device", False)
        lines.append(f"**执行方式**: {'真实 SSH 设备' if real_verify else '模拟'}  ")
        lines.append(f"**摘要**: {verify_info.get('verify_summary', 'N/A')}  ")
        lines.append(f"")

        verify_cmd_results = verify_info.get("command_results", [])
        if verify_cmd_results:
            lines.append(f"### 7.1 逐条验证命令详情")
            lines.append(f"")
            for i, r in enumerate(verify_cmd_results, 1):
                lines.append(f"#### 验证 {i}: `{r.get('command', '')}`")
                lines.append(f"")
                output = r.get("output", "").strip()
                if output:
                    lines.append(f"**设备输出**:")
                    lines.append(f"```")
                    lines.append(output)
                    lines.append(f"```")
                else:
                    lines.append(f"**设备输出**: (无输出)")
                lines.append(f"")
        else:
            verify_logs = verify_info.get("verify_logs", "")
            if verify_logs:
                lines.append(f"### 7.1 验证日志")
                lines.append(f"```")
                lines.append(verify_logs)
                lines.append(f"```")
                lines.append(f"")

        # 模块配置摘要（通用：根据实际检测到的模块动态输出）
        module_configs = verify_info.get("module_configs", {})
        if module_configs:
            sec_idx = 2
            for mod, config_text in module_configs.items():
                if config_text and config_text != "(无相关配置)":
                    lines.append(f"### 7.{sec_idx} 设备 {mod.upper()} 配置摘要")
                    lines.append(f"```")
                    lines.append(config_text)
                    lines.append(f"```")
                    lines.append(f"")
                    sec_idx += 1
        else:
            # 向后兼容旧数据
            slb_config = verify_info.get("slb_running_config", "")
            if slb_config:
                lines.append(f"### 7.2 设备 SLB 配置摘要")
                lines.append(f"```")
                lines.append(slb_config)
                lines.append(f"```")
                lines.append(f"")

            hc_config = verify_info.get("health_running_config", "")
            if hc_config:
                lines.append(f"### 7.3 设备 Health-Check 配置摘要")
                lines.append(f"```")
                lines.append(hc_config)
                lines.append(f"```")
                lines.append(f"")

    lines.append(f"---")
    lines.append(f"")

    # 8. AI 分析结论
    lines.append(f"## 8. AI Agent 分析结论")
    lines.append(f"")
    analysis_text = analysis.get("analysis_text", "")
    if analysis_text:
        # 尝试提取 JSON 中的关键信息
        analysis_result = analysis.get("analysis_result", {})
        if analysis_result:
            overall = analysis_result.get("overall_result", analysis_result.get("result", "N/A"))
            lines.append(f"**总体结果**: {overall}  ")
            conclusion = analysis_result.get("conclusion", analysis_result.get("summary", ""))
            if conclusion:
                lines.append(f"**结论**: {conclusion}  ")
            lines.append(f"")
            # 逐项分析
            items = analysis_result.get("items", analysis_result.get("test_items", []))
            if items:
                lines.append(f"### 8.1 逐项分析")
                lines.append(f"| # | 项目 | 结果 | 说明 |")
                lines.append(f"|---|------|------|------|")
                for i, item in enumerate(items, 1):
                    if isinstance(item, dict):
                        name = item.get("name", item.get("item", f"项目{i}"))
                        result = item.get("result", item.get("status", "N/A"))
                        desc = item.get("description", item.get("detail", ""))
                        lines.append(f"| {i} | {name} | {result} | {desc} |")
                lines.append(f"")

        lines.append(f"### 8.2 完整分析输出")
        lines.append(f"")
        lines.append(f"```")
        lines.append(analysis_text.strip())
        lines.append(f"```")
    else:
        lines.append(f"(无分析输出)")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    # 9. 总结
    _is_workforce = deploy_info.get("workforce", False)
    lines.append(f"## 9. 总结")
    lines.append(f"")
    if _is_workforce:
        # Workforce Pipeline 模式：基于 verdict 判定整体结果
        verdict = analysis.get("analysis_result", {}).get("verdict", "")
        wf_ok = verdict.startswith("成功") or verdict.lower().startswith("pass")
        overall_icon = "✅ 成功" if wf_ok else "❌ 失败"
        lines.append(f"**运行模式**: CAMEL Workforce Pipeline  ")
        lines.append(f"**整体判定**: {overall_icon}  ")
        lines.append(f"")
        lines.append(f"| 阶段 | 状态 |")
        lines.append(f"|------|------|")
        lines.append(f"| 1. 任务分解 + 配置生成 | ✅ 完成 |")
        env_status = "✅ 完成" if env_plan.get("server_url") else "❌ 失败"
        lines.append(f"| 2. 测试环境搭建 | {env_status} |")
        lines.append(f"| 3. Workforce: 配置下发 | ✅ 已执行 |")
        lines.append(f"| 4. Workforce: 流量验证 | ✅ 已执行 |")
        lines.append(f"| 5. Workforce: SSH 验证 | ✅ 已执行 |")
        lines.append(f"| 6. Workforce: AI 分析 | ✅ 已执行 |")
        lines.append(f"| 7. Workforce: 环境清理 | ✅ 已执行 |")
    else:
        # 传统模式：逐阶段状态
        lines.append(f"| 阶段 | 状态 |")
        lines.append(f"|------|------|")
        lines.append(f"| 1. 任务分解 + 配置生成 | ✅ 完成 |")
        env_status = "✅ 完成" if env_plan.get("server_url") else "❌ 失败"
        lines.append(f"| 2. 测试环境搭建 | {env_status} |")
        deploy_error = deploy_info.get("error")
        deploy_status = "❌ 失败" if deploy_error else "✅ 完成"
        lines.append(f"| 3. 配置下发 (SSH) | {deploy_status} |")
        # 流量验证
        traffic_ok = traffic_info.get("traffic_results", [])
        traffic_success = sum(1 for r in traffic_ok if r.get("success"))
        traffic_total = len(traffic_ok)
        if traffic_total > 0:
            traffic_status = f"✅ {traffic_success}/{traffic_total} 成功" if traffic_success > 0 else "❌ 全部失败"
        else:
            traffic_status = "⏭️ 未执行"
        lines.append(f"| 3.5. 流量验证 (HTTP) | {traffic_status} |")
        # VIP 预检查
        if precheck_info and precheck_info.get("checked"):
            if precheck_info.get("conflict"):
                precheck_status = f"⚠️ 发现冲突，已复用 `{precheck_info.get('reuse_vs', '')}`"
            else:
                precheck_status = "✅ 无冲突"
        else:
            precheck_status = "⏭️ 未执行"
        lines.append(f"| 3.6. VIP 预检查 | {precheck_status} |")
        # 故障注入
        if fault_injection_info and not fault_injection_info.get("skipped"):
            fi_steps_total = len(fault_injection_info.get("steps", []))
            fi_errors = sum(1 for s in fault_injection_info.get("steps", []) if s.get("step") == "ERROR")
            if fi_errors > 0:
                fi_status = f"⚠️ {fi_steps_total} 步骤, {fi_errors} 异常"
            else:
                fi_status = f"✅ {fi_steps_total} 步骤完成"
        elif fault_injection_info and fault_injection_info.get("skipped"):
            fi_status = "⏭️ 已跳过"
        else:
            fi_status = "⏭️ 未执行"
        lines.append(f"| 3.8. 故障注入与恢复验证 | {fi_status} |")
        verify_error = verify_info.get("error")
        verify_status = "❌ 失败" if verify_error else "✅ 完成"
        lines.append(f"| 4. 测试验证 (SSH) | {verify_status} |")
        lines.append(f"| 5. AI 分析 | ✅ 完成 |")
        lines.append(f"| 6. 环境清理 | ✅ 完成 |")
    lines.append(f"")

    # 写入文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write('\n'.join(lines))
    logger.info("MD 测试报告已生成: %s", output_path)


def execute_test_job(
    job_file: Path,
    output_dir: Path,
    log_file: Path,
    *,
    hybrid_retriever=None,
    reranker=None,
    graphrag_retriever=None,
    model=None,
    function_index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """执行单个测试任务

    Args:
        hybrid_retriever/reranker/graphrag_retriever/model/function_index:
            预初始化的共享资源。若为 None 则本函数内部初始化（向后兼容）。
    """
    t0_job = time.time()
    logger.info("=" * 80)
    logger.info(f"开始处理测试任务: {job_file.name}")
    logger.info("=" * 80)
    logger.info("")
    
    # 读取测试任务内容
    with open(job_file, "r", encoding="utf-8") as f:
        job_content = f.read()
    
    logger.info("测试任务内容:")
    logger.info(job_content)
    logger.info("")
    
    # 加载功能结构索引（如果未预加载）
    if function_index is None:
        index_path = Path(__file__).parent / "knowledge_base" / "function_structure_index.json"
        if index_path.exists():
            function_index = load_function_structure_index(index_path)
            logger.info(f"已加载功能结构索引: {len(function_index.get('scenarios', {}))} 个场景")
        else:
            function_index = {}
            logger.warning("功能结构索引不存在，某些功能可能受限")
    
    # 初始化RAG系统和LLM模型（如果未预加载）
    if hybrid_retriever is None or model is None:
        logger.info("初始化RAG系统和LLM模型...")
        t0_init = time.time()
        hybrid_retriever, reranker, graphrag_retriever = initialize_rag_system()
        model = initialize_llm_model()
        logger.info("初始化完成 (%.1f 秒)", time.time() - t0_init)
    else:
        logger.info("使用预初始化的 RAG/LLM 资源（跳过重复初始化）")
    logger.info("")
    
    # ================================================================
    # 阶段 1: 生成测试计划 (Test Plan Agent)
    # ================================================================
    logger.info("=" * 80)
    logger.info("阶段1: 生成测试计划（Test Plan Agent）")
    logger.info("=" * 80)
    logger.info("")

    t1 = time.time()
    test_plan = generate_test_plan(job_content, model)
    logger.info("测试计划已生成 (%.1f 秒): keyword=%s, fault_steps=%d, check_cmds=%d",
                time.time() - t1,
                test_plan.get("keyword", ""),
                len(test_plan.get("fault_injection_steps", [])),
                len(test_plan.get("status_check_commands", [])))

    # ================================================================
    # 阶段 2: 网络环境规划 (Env Setup Agent — 仅 IP 规划，不做部署)
    # ================================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("阶段2: 网络环境规划")
    logger.info("=" * 80)
    logger.info("")

    t2 = time.time()
    env_plan = plan_network_environment(job_content, model, test_plan=test_plan)
    logger.info("环境规划完成 (%.1f 秒): RS=%s, VIP=%s, DevPort3=%s",
                time.time() - t2,
                env_plan.get("VM_ETH1_IP", "?"),
                env_plan.get("VIP_ADDRESS", "?"),
                env_plan.get("DEVICE_PORT3_IP", "?"))

    # ================================================================
    # 阶段 3: 任务分解 + 配置生成（携带 env_plan 以确保 IP 一致）
    # ================================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("阶段3: 任务分解和配置生成")
    logger.info("=" * 80)
    logger.info("")

    t3 = time.time()
    workflow_result = process_job(
        job_content, hybrid_retriever, reranker, model, function_index,
        graphrag_retriever=graphrag_retriever, env_plan=env_plan,
    )
    logger.info("配置生成完成 (%.1f 秒)", time.time() - t3)
    config_result = workflow_result.get("config", {})
    config_commands = config_result.get("config_commands", [])
    verify_commands = config_result.get("verify_commands", [])

    if not config_commands:
        logger.error("未生成配置命令，无法继续执行测试")
        return {
            "job_file": str(job_file),
            "status": "failed",
            "error": "未生成配置命令",
        }

    # ================================================================
    # 阶段 3.5: VIP 预检查（检测 VIP 冲突并自动复用已有虚拟服务）
    # ================================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("阶段3.5: VIP 预检查")
    logger.info("=" * 80)
    logger.info("")

    config_commands, precheck_info = precheck_vip_conflict(config_commands, env_plan)

    # ================================================================
    # 阶段 4: 部署 VM 测试环境（配置 VM 网卡 IP、启动 HTTP 服务）
    # ================================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("阶段4: 部署 VM 测试环境")
    logger.info("=" * 80)
    logger.info("")

    deploy_test_environment(env_plan, config_commands=config_commands)

    # ================================================================
    # 阶段 5-9: Workforce Pipeline（配置下发→验证→流量→分析→清理）
    # ================================================================
    logger.info("")
    logger.info("=" * 80)
    logger.info("阶段5-9: 启动 CAMEL Workforce Pipeline")
    logger.info("=" * 80)
    logger.info("")

    t5 = time.time()
    workforce_result = run_workforce_pipeline(
        job_content=job_content,
        config_commands=config_commands,
        verify_commands=verify_commands,
        env_plan=env_plan,
        model=model,
        test_plan=test_plan,
        precheck_info=precheck_info,
    )
    logger.info("Workforce Pipeline 完成 (%.1f 秒)", time.time() - t5)

    # 从 Workforce 结果中提取各阶段详细输出
    _tr = workforce_result.get("task_results", {})
    analysis_info = {
        "analysis_text": workforce_result.get("final_analysis", ""),
        "analysis_result": {"verdict": workforce_result.get("verdict", "失败")},
    }
    deploy_info = {
        "deploy_summary": _tr.get("stage5_deploy", "由 Workforce Pipeline 执行"),
        "real_device": True,
        "workforce": True,
    }
    verify_info = {
        "verify_summary": _tr.get("stage7_verify", "由 Workforce Pipeline 执行"),
        "workforce": True,
    }
    traffic_info = {
        "traffic_summary": _tr.get("stage6_traffic", "由 Workforce Pipeline 执行"),
        "workforce": True,
    }
    fault_injection_info = {
        "summary": "包含在 traffic_info 中",
        "workforce": True,
    }
    
    # 汇总结果（过滤掉不可序列化的内部字段）
    _env_serializable = {k: v for k, v in env_plan.items() if not k.startswith("_")}
    test_result = {
        "job_file": str(job_file),
        "job_content": job_content,
        "decomposition": workflow_result.get("decomposition", {}),
        "config_commands": config_commands,
        "verify_commands": verify_commands,
        "test_plan": test_plan,
        "env_plan": _env_serializable,
        "precheck_info": precheck_info,
        "deploy_info": deploy_info,
        "traffic_info": traffic_info,
        "fault_injection_info": fault_injection_info,
        "verify_info": verify_info,
        "analysis": analysis_info,
        "workforce_result": workforce_result,
        "status": "completed",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    
    # 保存结果（原子写入，防止中断导致文件截断）
    output_file = output_dir / f"{job_file.stem}_test_result.json"
    tmp_file = output_file.with_suffix(".json.tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(test_result, f, ensure_ascii=False, indent=2)
        # 原子替换：写入完成后再替换目标文件
        if output_file.exists():
            output_file.unlink()
        tmp_file.rename(output_file)
    except Exception as e:
        logger.warning(f"保存结果文件时出错: {e}，尝试直接写入")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(test_result, f, ensure_ascii=False, indent=2)

    # 生成详细 MD 报告（原子写入）
    md_file = output_dir / f"{job_file.stem}_test_report.md"
    md_tmp = md_file.with_suffix(".md.tmp")
    try:
        generate_md_report(test_result, md_tmp)
        if md_file.exists():
            md_file.unlink()
        md_tmp.rename(md_file)
    except Exception as e:
        logger.warning(f"保存报告文件时出错: {e}，尝试直接写入")
        generate_md_report(test_result, md_file)
    logger.info(f"MD 报告已保存到: {md_file}")
    
    elapsed_job = time.time() - t0_job
    logger.info("")
    logger.info("=" * 80)
    logger.info("测试执行完成 (总耗时 %.1f 秒 / %.1f 分钟)", elapsed_job, elapsed_job / 60)
    logger.info("=" * 80)
    logger.info(f"结果已保存到: {output_file}")
    logger.info("")
    
    # 打印测试结果摘要
    print("\n" + "=" * 80)
    print("测试结果摘要")
    print("=" * 80)
    print(f"测试任务: {job_file.name}")
    print(f"配置命令数量: {len(config_commands)}")
    print(f"验证命令数量: {len(verify_commands)}")
    print(f"环境服务: {env_plan.get('server_url', 'N/A')}")
    print(f"测试状态: 完成")
    if analysis_info.get("analysis_text"):
        print(f"\n分析结果:")
        _atxt = analysis_info["analysis_text"][:500]
        if len(analysis_info["analysis_text"]) > 500:
            _atxt += "..."
        # Windows GBK 终端无法输出 emoji/特殊 Unicode，替换后输出
        try:
            print(_atxt)
        except UnicodeEncodeError:
            print(_atxt.encode("ascii", errors="replace").decode("ascii"))
    print("=" * 80 + "\n")
    
    return test_result


def main():
    """主函数：处理jobs目录下的所有测试任务"""
    import argparse
    
    parser = argparse.ArgumentParser(description="测试执行：读取jobs下的测试任务并执行")
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path(__file__).parent / "jobs",
        help="Jobs目录路径"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "reports",
        help="输出目录路径"
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(__file__).parent / "knowledge_base" / "logs",
        help="日志目录路径"
    )
    args = parser.parse_args()
    
    # 设置日志
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_file = args.log_dir / f"test_execution_{timestamp}.log"
    setup_logging(log_file)
    
    logger.info("=" * 80)
    logger.info("INAGENT 测试执行器")
    logger.info("=" * 80)
    logger.info(f"日志文件: {log_file}")
    logger.info("")
    
    # 检查jobs目录
    jobs_dir = args.jobs_dir
    if not jobs_dir.exists():
        logger.error(f"Jobs目录不存在: {jobs_dir}")
        return 1
    
    job_files = list(jobs_dir.glob("*.txt"))
    if not job_files:
        logger.warning(f"Jobs目录下没有找到.txt文件: {jobs_dir}")
        return 0
    
    logger.info(f"找到 {len(job_files)} 个测试任务文件")
    logger.info("")
    
    # 创建输出目录
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # ── 一次性初始化 RAG / LLM / 索引（所有 job 共享） ──────────────
    logger.info("初始化RAG系统和LLM模型（全局共享）...")
    t0_init = time.time()
    
    # 加载功能结构索引
    index_path = Path(__file__).parent / "knowledge_base" / "function_structure_index.json"
    if index_path.exists():
        function_index = load_function_structure_index(index_path)
        logger.info(f"已加载功能结构索引: {len(function_index.get('scenarios', {}))} 个场景")
    else:
        function_index = {}
        logger.warning("功能结构索引不存在")
    
    hybrid_retriever, reranker, graphrag_retriever = initialize_rag_system()
    model = initialize_llm_model()
    logger.info("全局初始化完成 (%.1f 秒)", time.time() - t0_init)
    logger.info("")
    
    # 处理每个测试任务（复用已初始化的资源）
    results = []
    for i, job_file in enumerate(job_files, 1):
        logger.info("── 任务 %d/%d: %s ──", i, len(job_files), job_file.name)
        try:
            result = execute_test_job(
                job_file, output_dir, log_file,
                hybrid_retriever=hybrid_retriever,
                reranker=reranker,
                graphrag_retriever=graphrag_retriever,
                model=model,
                function_index=function_index,
            )
            results.append(result)
        except Exception as e:
            logger.error(f"处理测试任务 {job_file.name} 失败: {e}", exc_info=True)
            results.append({
                "job_file": str(job_file),
                "status": "failed",
                "error": str(e),
            })
    
    # 打印汇总
    logger.info("")
    logger.info("=" * 80)
    logger.info("测试执行汇总")
    logger.info("=" * 80)
    success_count = sum(1 for r in results if r.get("status") == "completed")
    failed_count = len(results) - success_count
    logger.info(f"总任务数: {len(results)}")
    logger.info(f"成功: {success_count}")
    logger.info(f"失败: {failed_count}")
    logger.info("")
    
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("\n用户中断操作")
        sys.exit(0)
    except Exception as e:
        logger.error(f"程序异常: {e}", exc_info=True)
        sys.exit(1)
