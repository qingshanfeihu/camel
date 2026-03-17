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
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Dict, List, Optional

from camel.logger import get_logger
from camel.toolkits import FunctionTool
from camel.toolkits.base import BaseToolkit
from camel.utils import Constants

logger = get_logger(__name__)


class WindowsNetOpsToolkit(BaseToolkit):
    r"""Windows network operations toolkit.

    Provides helpers for:
    - Packet capture (tshark, requires Wireshark + Npcap)
    - PCAP parsing (tshark)
    - HTTP validation (curl)
    - Load testing (JMeter)
    - Fault injection (clumsy)
    - Connectivity checks (ping, tracert)
    """

    def __init__(self, timeout: Optional[float] = Constants.TIMEOUT_THRESHOLD):
        super().__init__(timeout=timeout)

    def _resolve_executable(self, env_name: str, fallback: str) -> Optional[str]:
        override = os.getenv(env_name)
        if override and os.path.exists(override):
            return override
        return shutil.which(fallback)

    def _run_command(self, args: list[str]) -> Dict[str, str]:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            return {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "return_code": str(completed.returncode),
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Command timed out.",
                "return_code": "-1",
            }
        except Exception as exc:
            return {
                "stdout": "",
                "stderr": f"Command failed: {exc}",
                "return_code": "-1",
            }

    def _log_tool_result(
        self, tool_name: str, args: list[str], result: Dict[str, str]
    ) -> None:
        stdout_preview = (result.get("stdout") or "").strip()[:1000]
        stderr_preview = (result.get("stderr") or "").strip()[:1000]
        logger.info(
            "[%s] args=%s return_code=%s",
            tool_name,
            args,
            result.get("return_code"),
        )
        if stdout_preview:
            logger.info("[%s] stdout: %s", tool_name, stdout_preview)
        if stderr_preview:
            logger.info("[%s] stderr: %s", tool_name, stderr_preview)
        print(
            f"[{tool_name}] return_code={result.get('return_code')}\n"
            f"stdout:\n{stdout_preview}\n"
            f"stderr:\n{stderr_preview}"
        )

    def windows_net_ops_capture_packets_tshark(
        self,
        interface: str,
        output_pcap_path: str,
        duration_seconds: int = 10,
        capture_filter: Optional[str] = None,
    ) -> Dict[str, str]:
        r"""Capture packets using tshark.

        Args:
            interface (str): Interface name or index for tshark.
            output_pcap_path (str): Output pcap file path.
            duration_seconds (int): Capture duration seconds.
            capture_filter (Optional[str]): BPF capture filter.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        tshark = self._resolve_executable("TSHARK_PATH", "tshark")
        if not tshark:
            return {
                "stdout": "",
                "stderr": "tshark not found. Install Wireshark + Npcap.",
                "return_code": "-1",
            }
        args = [
            tshark,
            "-i",
            interface,
            "-a",
            f"duration:{duration_seconds}",
            "-w",
            output_pcap_path,
        ]
        if capture_filter:
            args.extend(["-f", capture_filter])
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_capture_packets_tshark", args, result)
        return result

    def windows_net_ops_parse_pcap_tshark(
        self,
        pcap_path: str,
        display_filter: Optional[str] = None,
        max_packets: int = 200,
    ) -> Dict[str, str]:
        r"""Parse pcap with tshark output.

        Args:
            pcap_path (str): Path to pcap file.
            display_filter (Optional[str]): Display filter.
            max_packets (int): Max packets to output.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        tshark = self._resolve_executable("TSHARK_PATH", "tshark")
        if not tshark:
            return {
                "stdout": "",
                "stderr": "tshark not found. Install Wireshark + Npcap.",
                "return_code": "-1",
            }
        args = [tshark, "-r", pcap_path, "-c", str(max_packets)]
        if display_filter:
            args.extend(["-Y", display_filter])
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_parse_pcap_tshark", args, result)
        return result

    def windows_net_ops_http_request_curl(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[list[str]] = None,
        data: Optional[str] = None,
    ) -> Dict[str, str]:
        r"""Send HTTP request using curl.

        Args:
            url (str): Target URL.
            method (str): HTTP method.
            headers (Optional[List[str]]): Headers like "Key: Value".
            data (Optional[str]): Request body.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        curl = self._resolve_executable("CURL_PATH", "curl")
        if not curl:
            return {
                "stdout": "",
                "stderr": "curl not found.",
                "return_code": "-1",
            }
        args = [curl, "-X", method, url]
        if headers:
            for header in headers:
                args.extend(["-H", header])
        if data is not None:
            args.extend(["--data", data])
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_http_request_curl", args, result)
        return result

    def windows_net_ops_run_jmeter(
        self,
        test_plan_path: str,
        result_jtl_path: str,
        log_path: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
    ) -> Dict[str, str]:
        r"""Run JMeter in non-GUI mode.

        Args:
            test_plan_path (str): JMX test plan path.
            result_jtl_path (str): Output JTL results path.
            log_path (Optional[str]): JMeter log path.
            extra_args (Optional[List[str]]): Extra args.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        jmeter = self._resolve_executable("JMETER_PATH", "jmeter")
        if not jmeter:
            return {
                "stdout": "",
                "stderr": "JMeter not found.",
                "return_code": "-1",
            }
        args = [jmeter, "-n", "-t", test_plan_path, "-l", result_jtl_path]
        if log_path:
            args.extend(["-j", log_path])
        if extra_args:
            args.extend(extra_args)
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_run_jmeter", args, result)
        return result

    def windows_net_ops_inject_fault_clumsy(
        self,
        clumsy_args: list[str],
    ) -> Dict[str, str]:
        r"""Run clumsy for fault injection.

        Args:
            clumsy_args (List[str]): Arguments for clumsy.exe.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        clumsy = self._resolve_executable("CLUMSY_PATH", "clumsy")
        if not clumsy:
            return {
                "stdout": "",
                "stderr": "clumsy not found.",
                "return_code": "-1",
            }
        args = [clumsy, *clumsy_args]
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_inject_fault_clumsy", args, result)
        return result

    def windows_net_ops_ping(self, host: str, count: int = 4) -> Dict[str, str]:
        r"""Ping a host.

        Args:
            host (str): Target host.
            count (int): Echo request count.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        args = ["ping", "-n", str(count), host]
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_ping", args, result)
        return result

    def windows_net_ops_tracert(self, host: str) -> Dict[str, str]:
        r"""Run tracert to a host.

        Args:
            host (str): Target host.

        Returns:
            Dict[str, str]: stdout/stderr/return_code.
        """
        args = ["tracert", host]
        result = self._run_command(args)
        self._log_tool_result("windows_net_ops_tracert", args, result)
        return result

    def get_tools(self) -> List[FunctionTool]:
        return [
            FunctionTool(self.windows_net_ops_capture_packets_tshark),
            FunctionTool(self.windows_net_ops_parse_pcap_tshark),
            FunctionTool(self.windows_net_ops_http_request_curl),
            FunctionTool(self.windows_net_ops_run_jmeter),
            FunctionTool(self.windows_net_ops_inject_fault_clumsy),
            FunctionTool(self.windows_net_ops_ping),
            FunctionTool(self.windows_net_ops_tracert),
        ]
