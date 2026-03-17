# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
TrafficVerifyToolkit: 面向端到端流量验证和故障注入场景的组合工具集。

提供多轮 VIP 流量探测、设备健康状态检查（含 NSAE 输出注释注入）、
以及健康检查收敛等待等高层操作，补充 NSAEDeviceToolkit 和
VMControllerToolkit 未覆盖的编排型能力。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from camel.toolkits.base import BaseToolkit
from camel.toolkits.function_tool import FunctionTool

from INAGENT.utils.ssh_client import NSAESSHClient, create_ssh_client_from_env
from INAGENT.utils.vm_controller import VMController

logger = logging.getLogger(__name__)


class TrafficVerifyToolkit(BaseToolkit):
    r"""Toolkit for end-to-end traffic verification and health-check status
    inspection in NSAE load-balancer testing scenarios.

    This toolkit combines VM-side HTTP probing with device-side health
    status queries to provide compound verification operations that
    individual device or VM tools cannot offer alone.

    Args:
        vm (VMController, optional): A pre-configured VMController for HTTP
            probing.  Created from environment variables when *None*.
        ssh_client (NSAESSHClient, optional): A pre-configured SSH client
            for health status queries.  Created from environment variables
            when *None*.
        timeout (float, optional): Timeout threshold inherited from
            BaseToolkit.
    """

    def __init__(
        self,
        vm: Optional[VMController] = None,
        ssh_client: Optional[NSAESSHClient] = None,
        timeout: Optional[float] = None,
    ):
        super().__init__(timeout=timeout)
        self._vm = vm
        self._ssh_client = ssh_client
        self._vm_connected = False
        self._ssh_connected = False

    # ── 内部连接管理 ──────────────────────────────────────────────────

    def _ensure_vm(self) -> VMController:
        if self._vm is None:
            self._vm = VMController.from_env()
        if not self._vm_connected:
            self._vm.connect()
            self._vm_connected = True
        return self._vm

    def _ensure_ssh(self) -> NSAESSHClient:
        if self._ssh_client is None:
            self._ssh_client = create_ssh_client_from_env()
        # Detect stale / dropped SSH sessions after long waits.
        if self._ssh_connected:
            try:
                transport = self._ssh_client.ssh and self._ssh_client.ssh.get_transport()
                if transport is None or not transport.is_active():
                    logger.warning("SSH transport 不活跃，重新连接...")
                    self._ssh_connected = False
            except Exception:
                self._ssh_connected = False
        if not self._ssh_connected:
            self._ssh_client.connect()
            self._ssh_client.enter_enable_mode()
            self._ssh_connected = True
        return self._ssh_client

    def disconnect(self) -> None:
        """释放 VM 和 SSH 连接。"""
        if self._vm:
            self._vm.disconnect()
            self._vm_connected = False
        if self._ssh_client:
            self._ssh_client.disconnect()
            self._ssh_connected = False

    # ── 工具方法 ──────────────────────────────────────────────────────

    def verify_vip_traffic(
        self,
        target_url: str,
        keyword: str = "",
        count: int = 3,
        timeout: int = 5,
    ) -> str:
        r"""Send multiple HTTP requests from the VM to the target URL (usually
        the VIP) and return an aggregate result.

        This simulates real client traffic traversing the load balancer.
        Each request checks HTTP status code and optionally matches a
        keyword in the response body.

        Args:
            target_url (str): The URL to probe, e.g.
                ``"http://27.16.9.200:80/"``.
            keyword (str): Expected keyword in the response body.  Leave
                empty to skip content matching.
            count (int): Number of HTTP requests to send.  Defaults to ``3``.
            timeout (int): Per-request timeout in seconds.  Defaults to ``5``.

        Returns:
            str: JSON object with ``total``, ``success_count``,
                ``match_count``, ``results`` (per-request detail), and
                ``summary`` (human-readable one-liner).
        """
        vm = self._ensure_vm()
        results: List[Dict[str, Any]] = []
        for i in range(1, count + 1):
            probe = vm.http_probe(target_url, keyword=keyword, timeout=timeout)
            probe["attempt"] = i
            probe["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            results.append(probe)

        total = len(results)
        success_count = sum(1 for r in results if r.get("success"))
        match_count = sum(1 for r in results if r.get("content_match"))
        summary = (
            f"向 {target_url} 发起 {total} 次 HTTP 请求，"
            f"成功 {success_count} 次"
        )
        if keyword:
            summary += f"，内容匹配('{keyword}') {match_count} 次"

        return json.dumps(
            {
                "total": total,
                "success_count": success_count,
                "match_count": match_count,
                "summary": summary,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )

    def check_device_health_status(self, commands: str = "") -> str:
        r"""Query health-check related status on the NSAE device via SSH.

        If no commands are provided, the following defaults are used:
        ``show slb group health``, ``show slb health``,
        ``show statistics slb real http``.

        The output for ``show slb health`` is annotated with a caveat about
        the limitations of its display format (fixed default values for
        request/response fields) to prevent downstream AI agents from
        making incorrect judgments.

        Args:
            commands (str): Show commands separated by ``\n``.  Leave empty
                to use the default health-check command set.

        Returns:
            str: JSON object mapping each command to its annotated output.
        """
        client = self._ensure_ssh()

        if commands.strip():
            cmd_list = [c.strip() for c in commands.strip().splitlines() if c.strip()]
        else:
            cmd_list = [
                "show slb group health",
                "show slb health",
                "show statistics slb real http",
            ]

        results = client.execute_show_commands(cmd_list)
        outputs: Dict[str, str] = {}
        for r in results:
            cmd = r["command"]
            output = r["output"]
            # 为 show slb health 输出注入说明，防止 AI 误判
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

        return json.dumps(outputs, ensure_ascii=False, indent=2)

    def wait_for_health_convergence(
        self,
        interval: int = 15,
        retries: int = 3,
        buffer: int = 10,
    ) -> str:
        r"""Wait for the device health-check mechanism to converge after a
        fault injection or recovery action.

        The total wait time is calculated as:
        ``interval * retries + buffer`` seconds.

        Args:
            interval (int): Health-check interval in seconds.  Defaults to
                ``15``.
            retries (int): Number of consecutive health-check failures/
                successes before the device changes server status.  Defaults
                to ``3``.
            buffer (int): Extra buffer seconds.  Defaults to ``10``.

        Returns:
            str: A message confirming the wait duration.
        """
        wait_seconds = interval * retries + buffer
        logger.info("等待健康检查收敛: %d 秒 (%d × %d + %d)",
                     wait_seconds, interval, retries, buffer)
        # 使用分段 sleep 以抵抗 KeyboardInterrupt
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                time.sleep(min(remaining, 5.0))
            except KeyboardInterrupt:
                logger.warning("收到 KeyboardInterrupt，继续等待剩余 %.0f 秒",
                               max(deadline - time.monotonic(), 0))
        return f"已等待 {wait_seconds} 秒（健康检查收敛: interval={interval}, retries={retries}, buffer={buffer}）"

    def run_fault_injection_step(
        self,
        step_type: str,
        port: int = 80,
        bind_ip: str = "0.0.0.0",
        html_content: str = "",
        remove_keyword: str = "",
        replace_with: str = "",
        command: str = "",
    ) -> str:
        r"""Execute a single fault-injection step on the VM or device and
        return the result.

        Supported step types:
        - ``stop_service``: Stop the HTTP backend service on *port*.
        - ``restore_service``: Redeploy the HTTP backend with *html_content*.
        - ``modify_content``: Replace *remove_keyword* with *replace_with*
          in the served HTML page.
        - ``restore_content``: Overwrite the served HTML page with
          *html_content* (the original version).
        - ``device_command``: Execute a CLI configuration command on the
          NSAE device via SSH (e.g. ``slb real disable "rs1"``).

        Args:
            step_type (str): One of ``"stop_service"``, ``"restore_service"``,
                ``"modify_content"``, ``"restore_content"``,
                ``"device_command"``.
            port (int): HTTP service port.  Defaults to ``80``.
            bind_ip (str): Bind IP for service restoration.  Defaults to
                ``"0.0.0.0"``.
            html_content (str): HTML content for restore operations.
            remove_keyword (str): Keyword to remove in ``modify_content``.
            replace_with (str): Replacement string for ``modify_content``.
            command (str): CLI command for ``device_command`` type.

        Returns:
            str: JSON object describing the action taken and its outcome.
        """
        result: Dict[str, Any] = {
            "step_type": step_type,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if step_type == "stop_service":
            vm = self._ensure_vm()
            vm.stop_http_server(port=port)
            result["action"] = f"已停止端口 {port} 上的 HTTP 服务"
            result["success"] = True

        elif step_type == "restore_service":
            vm = self._ensure_vm()
            if not html_content:
                result["success"] = False
                result["error"] = "restore_service 需要提供 html_content"
            else:
                info = vm.deploy_http_server(html_content, port=port, bind_ip=bind_ip)
                result["action"] = f"已在 {bind_ip}:{port} 恢复 HTTP 服务"
                result["deploy_info"] = info
                result["success"] = True

        elif step_type == "modify_content":
            vm = self._ensure_vm()
            if not remove_keyword:
                result["success"] = False
                result["error"] = "modify_content 需要提供 remove_keyword"
            else:
                current = vm.read_html_content()
                modified = current.replace(remove_keyword, replace_with)
                vm.modify_html_content(modified)
                result["action"] = f"已将页面中 '{remove_keyword}' 替换为 '{replace_with}'"
                result["success"] = True

        elif step_type == "restore_content":
            vm = self._ensure_vm()
            if not html_content:
                result["success"] = False
                result["error"] = "restore_content 需要提供 html_content (原始内容)"
            else:
                vm.modify_html_content(html_content)
                result["action"] = "已恢复页面原始内容"
                result["success"] = True

        elif step_type == "device_command":
            if not command:
                result["success"] = False
                result["error"] = "device_command 需要提供 command 参数"
            else:
                client = self._ensure_ssh()
                cmd_list = [c.strip() for c in command.strip().splitlines() if c.strip()]
                cmd_results = client.execute_config_commands(cmd_list)
                result["action"] = f"已在设备上执行命令: {command}"
                result["device_output"] = cmd_results
                has_error = any(r.get("status") == "error" for r in cmd_results)
                result["success"] = not has_error

        else:
            result["success"] = False
            result["error"] = f"未知的步骤类型: {step_type}"

        return json.dumps(result, ensure_ascii=False, indent=2)

    # ── get_tools ─────────────────────────────────────────────────────

    def get_tools(self) -> List[FunctionTool]:
        r"""Returns a list of FunctionTool objects representing the
        functions in the toolkit.

        Returns:
            List[FunctionTool]: A list of FunctionTool objects.
        """
        return [
            FunctionTool(self.verify_vip_traffic),
            FunctionTool(self.check_device_health_status),
            FunctionTool(self.wait_for_health_convergence),
            FunctionTool(self.run_fault_injection_step),
        ]
