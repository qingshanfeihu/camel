# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
VMControllerToolkit: 封装 VMController，为 CAMEL Agent 提供远程 Linux VM
的网络配置、HTTP 服务管理和流量探测能力。

连接生命周期由 toolkit 内部管理（惰性连接 + 自动重连）。
"""
import json
import logging
from typing import List, Optional

from camel.toolkits.base import BaseToolkit
from camel.toolkits.function_tool import FunctionTool

from INAGENT.utils.vm_controller import VMController

logger = logging.getLogger(__name__)


class VMControllerToolkit(BaseToolkit):
    r"""Toolkit for managing a remote Linux VM used as the test traffic
    endpoint — configure network interfaces, deploy/stop HTTP services,
    and probe URLs.

    This toolkit wraps :class:`VMController` and exposes its capabilities
    as CAMEL-compatible :class:`FunctionTool` objects.

    Args:
        vm (VMController, optional): A pre-configured VMController
            instance.  When *None*, one is created via environment variables
            (``VM_MGMT_IP``, ``VM_USERNAME``, ``VM_PASSWORD``).
        timeout (float, optional): Timeout threshold inherited from
            BaseToolkit.
    """

    def __init__(
        self,
        vm: Optional[VMController] = None,
        timeout: Optional[float] = None,
    ):
        super().__init__(timeout=timeout)
        self._vm = vm
        self._connected = False

    # ── 内部连接管理 ──────────────────────────────────────────────────

    def _ensure_connected(self) -> VMController:
        """保证 VM SSH 已连接，返回控制器实例。"""
        if self._vm is None:
            self._vm = VMController.from_env()
        if not self._connected:
            self._vm.connect()
            self._connected = True
        return self._vm

    def disconnect(self) -> None:
        """断开 VM SSH 连接并释放资源。"""
        if self._vm:
            self._vm.disconnect()
            self._connected = False

    # ── 工具方法 ──────────────────────────────────────────────────────

    def configure_vm_interface(
        self, iface: str, ip_addr: str, netmask: str = "255.255.255.0"
    ) -> str:
        r"""Configure a network interface on the VM with the specified IP
        address and bring it up.

        Args:
            iface (str): Interface name, e.g. ``"eth1"`` or ``"eth2"``.
            ip_addr (str): IPv4 address to assign, e.g. ``"10.0.3.100"``.
            netmask (str): Subnet mask.  Defaults to ``"255.255.255.0"``.

        Returns:
            str: Summary of the configuration result.
        """
        vm = self._ensure_connected()
        result = vm.configure_interface(iface, ip_addr, netmask, bring_up=True)
        return result

    def add_vm_route(
        self, destination: str, gateway: str, iface: str = ""
    ) -> str:
        r"""Add a static route on the VM.

        Args:
            destination (str): Destination network in CIDR notation, e.g.
                ``"27.16.9.0/24"``.
            gateway (str): Next-hop gateway IP, e.g. ``"10.0.3.1"``.
            iface (str): Optional outgoing interface name.

        Returns:
            str: Route addition result summary.
        """
        vm = self._ensure_connected()
        return vm.add_route(destination, gateway, iface)

    def deploy_http_server(
        self,
        html_content: str,
        port: int = 80,
        bind_ip: str = "0.0.0.0",
    ) -> str:
        r"""Deploy (or redeploy) an HTTP service on the VM serving the given
        HTML content.

        Any existing service on the same port is automatically stopped first.

        Args:
            html_content (str): The index.html body content to serve.
            port (int): Listening port number.  Defaults to ``80``.
            bind_ip (str): IP address to bind to.  Defaults to ``"0.0.0.0"``.

        Returns:
            str: JSON object with ``server_url``, ``port``, ``bind_ip``,
                ``pid``, etc.
        """
        vm = self._ensure_connected()
        info = vm.deploy_http_server(html_content, port=port, bind_ip=bind_ip)
        return json.dumps(info, ensure_ascii=False, indent=2)

    def deploy_multi_http_servers(
        self,
        html_content: str,
        ports: str,
        bind_ip: str = "0.0.0.0",
    ) -> str:
        r"""Deploy multiple HTTP service instances on the VM (same IP,
        different ports) to simulate multiple backend real servers.

        Args:
            html_content (str): Shared index.html body content.
            ports (str): Comma-separated port numbers, e.g. ``"80,8081,8082"``.
            bind_ip (str): IP address to bind to.  Defaults to ``"0.0.0.0"``.

        Returns:
            str: JSON array of each instance deployment result.
        """
        vm = self._ensure_connected()
        port_list = [int(p.strip()) for p in ports.split(",") if p.strip()]
        servers = [{"name": f"rs_{p}", "port": p} for p in port_list]
        results = vm.deploy_multi_http_servers(html_content, servers, bind_ip)
        return json.dumps(results, ensure_ascii=False, indent=2)

    def stop_http_server(self, port: int = 80) -> str:
        r"""Stop the HTTP service on the VM for the given port.

        Args:
            port (int): The port whose HTTP service should be stopped.
                Defaults to ``80``.

        Returns:
            str: ``"ok"`` on success.
        """
        vm = self._ensure_connected()
        vm.stop_http_server(port=port)
        return "ok"

    def stop_multi_http_servers(self, ports: str) -> str:
        r"""Stop multiple HTTP service instances on the VM.

        Args:
            ports (str): Comma-separated port numbers, e.g. ``"80,8081"``.

        Returns:
            str: ``"ok"`` on success.
        """
        vm = self._ensure_connected()
        port_list = [int(p.strip()) for p in ports.split(",") if p.strip()]
        vm.stop_multi_http_servers(port_list)
        return "ok"

    def modify_html_content(self, new_content: str) -> str:
        r"""Modify the index.html content on the VM.  The HTTP service keeps
        running and the new content takes effect on the next request.

        Args:
            new_content (str): The new HTML content for index.html.

        Returns:
            str: ``"ok"`` on success.
        """
        vm = self._ensure_connected()
        vm.modify_html_content(new_content)
        return "ok"

    def http_probe(
        self, url: str, keyword: str = "", timeout: int = 5
    ) -> str:
        r"""Send an HTTP request from the VM (simulating client traffic) and
        check the response.

        Args:
            url (str): Target URL, typically the VIP address, e.g.
                ``"http://27.16.9.200:80/"``.
            keyword (str): Expected keyword in the response body.  Leave
                empty to skip content matching.
            timeout (int): Request timeout in seconds.  Defaults to ``5``.

        Returns:
            str: JSON object with ``success``, ``status_code``,
                ``content_match``, ``body_snippet``, and optionally ``error``.
        """
        vm = self._ensure_connected()
        result = vm.http_probe(url, keyword=keyword, timeout=timeout)
        return json.dumps(result, ensure_ascii=False, indent=2)

    def run_vm_command(self, command: str) -> str:
        r"""Execute an arbitrary shell command on the VM and return stdout.

        Use this for ad-hoc diagnostics such as ``ip addr show``,
        ``ss -tlnp``, ``curl ...``, etc.

        Args:
            command (str): The shell command to execute.

        Returns:
            str: JSON object with ``exit_code``, ``stdout``, ``stderr``.
        """
        vm = self._ensure_connected()
        rc, stdout, stderr = vm.run(command)
        return json.dumps(
            {"exit_code": rc, "stdout": stdout, "stderr": stderr},
            ensure_ascii=False,
            indent=2,
        )

    # ── get_tools ─────────────────────────────────────────────────────

    def get_tools(self) -> List[FunctionTool]:
        r"""Returns a list of FunctionTool objects representing the
        functions in the toolkit.

        Returns:
            List[FunctionTool]: A list of FunctionTool objects.
        """
        return [
            FunctionTool(self.configure_vm_interface),
            FunctionTool(self.add_vm_route),
            FunctionTool(self.deploy_http_server),
            FunctionTool(self.deploy_multi_http_servers),
            FunctionTool(self.stop_http_server),
            FunctionTool(self.stop_multi_http_servers),
            FunctionTool(self.modify_html_content),
            FunctionTool(self.http_probe),
            FunctionTool(self.run_vm_command),
        ]
