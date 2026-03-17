# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
NSAEDeviceToolkit: 封装 NSAESSHClient，为 CAMEL Agent 提供 NSAE/InfosecOS
设备的 CLI 操作能力——执行 show 命令、下发配置、查询模块配置。

连接生命周期由 toolkit 内部管理（惰性连接 + 自动重连）。
"""
import json
import logging
from typing import List, Optional

from camel.toolkits.base import BaseToolkit
from camel.toolkits.function_tool import FunctionTool

from INAGENT.utils.ssh_client import NSAESSHClient, create_ssh_client_from_env

logger = logging.getLogger(__name__)


class NSAEDeviceToolkit(BaseToolkit):
    r"""Toolkit for interacting with NSAE/InfosecOS load balancer devices
    via SSH CLI.

    This toolkit wraps :class:`NSAESSHClient` and exposes its capabilities as
    CAMEL-compatible :class:`FunctionTool` objects so that LLM agents can
    execute show commands, deploy configuration, and query running-config
    sections autonomously.

    Args:
        ssh_client (NSAESSHClient, optional): A pre-configured SSH client
            instance.  When *None*, one is created via environment variables
            (``LB_DEVICE_IP``, ``LB_USERNAME``, ``LB_PASSWORD``,
            ``LB_SSH_PORT``).
        timeout (float, optional): Timeout threshold inherited from
            BaseToolkit.
    """

    def __init__(
        self,
        ssh_client: Optional[NSAESSHClient] = None,
        timeout: Optional[float] = None,
    ):
        super().__init__(timeout=timeout)
        self._ssh_client = ssh_client
        self._connected = False

    # ── 内部连接管理 ──────────────────────────────────────────────────

    def _ensure_connected(self) -> NSAESSHClient:
        """保证 SSH 已连接并处于 enable 模式，返回客户端实例。"""
        if self._ssh_client is None:
            self._ssh_client = create_ssh_client_from_env()
        # Detect stale / dropped SSH sessions.
        if self._connected:
            try:
                transport = self._ssh_client.ssh and self._ssh_client.ssh.get_transport()
                if transport is None or not transport.is_active():
                    logger.warning("SSH transport 不活跃，重新连接...")
                    self._connected = False
            except Exception:
                self._connected = False
        if not self._connected:
            self._ssh_client.connect()
            self._ssh_client.enter_enable_mode()
            self._connected = True
        return self._ssh_client

    def disconnect(self) -> None:
        """断开 SSH 连接并释放资源。"""
        if self._ssh_client:
            self._ssh_client.disconnect()
            self._connected = False

    # ── 工具方法 ──────────────────────────────────────────────────────

    def execute_show_commands(self, commands: str) -> str:
        r"""Execute one or more show/verification commands on the NSAE device
        and return the device output.

        Commands are separated by newlines. Each command is executed in enable
        mode and the raw device output is returned.

        Args:
            commands (str): Show commands separated by ``\n``.  For example:
                ``"show slb virtual http\nshow slb real http"``.

        Returns:
            str: JSON array of results.  Each element contains ``command``,
                ``output``, and ``status`` fields.
        """
        client = self._ensure_connected()
        cmd_list = [c.strip() for c in commands.strip().splitlines() if c.strip()]
        if not cmd_list:
            return json.dumps({"error": "未提供任何命令"}, ensure_ascii=False)
        results = client.execute_show_commands(cmd_list)
        return json.dumps(results, ensure_ascii=False, indent=2)

    def execute_config_commands(self, commands: str) -> str:
        r"""Deploy configuration commands to the NSAE device.

        Commands are separated by newlines.  The toolkit automatically enters
        enable → config terminal mode, executes all commands sequentially, and
        then exits config mode.

        Args:
            commands (str): Configuration commands separated by ``\n``.
                For example: ``"slb virtual http VS1\n virtual 27.16.9.200"``.

        Returns:
            str: JSON array of results.  Each element contains ``command``,
                ``output``, and ``status`` (``"success"`` or ``"error"``).
        """
        client = self._ensure_connected()
        cmd_list = [c.strip() for c in commands.strip().splitlines() if c.strip()]
        if not cmd_list:
            return json.dumps({"error": "未提供任何命令"}, ensure_ascii=False)
        results = client.execute_config_commands(cmd_list)
        return json.dumps(results, ensure_ascii=False, indent=2)

    def get_module_config(self, module: str) -> str:
        r"""Retrieve the running-config summary for a specific module on the
        NSAE device.

        Supported module keywords: ``slb``, ``health``, ``llb``, ``ssl``,
        ``ha`` (alias ``vrrp``), ``nat``.  Pass an empty string to retrieve
        all known modules.

        Args:
            module (str): Module keyword such as ``"slb"`` or ``"health"``.

        Returns:
            str: The running-config section text for the specified module.
        """
        client = self._ensure_connected()
        config_text = client.get_running_config_section(module)
        return config_text

    def check_config_exists(self, config_type: str, name: str) -> str:
        r"""Check whether a specific named configuration object exists on the
        device by querying related show commands and searching for the given
        name.

        Useful for verifying whether a virtual server, real server, health
        check, or SLB group has been successfully created.

        Args:
            config_type (str): The type of configuration to check.  Supported
                values: ``"virtual"``, ``"real"``, ``"group"``, ``"health"``.
            name (str): The name of the configuration object to search for.

        Returns:
            str: A JSON object with ``found`` (bool) and ``detail`` (str).
        """
        _type_cmd_map = {
            "virtual": "show slb virtual http",
            "real": "show slb real http",
            "group": "show slb group method",
            "health": "show slb health",
        }
        cmd = _type_cmd_map.get(config_type.lower())
        if not cmd:
            return json.dumps(
                {"found": False, "detail": f"不支持的配置类型: {config_type}"},
                ensure_ascii=False,
            )
        client = self._ensure_connected()
        results = client.execute_show_commands([cmd])
        output = results[0]["output"] if results else ""
        found = name.lower() in output.lower()
        return json.dumps(
            {"found": found, "detail": output[:1000]},
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
            FunctionTool(self.execute_show_commands),
            FunctionTool(self.execute_config_commands),
            FunctionTool(self.get_module_config),
            FunctionTool(self.check_config_exists),
        ]
