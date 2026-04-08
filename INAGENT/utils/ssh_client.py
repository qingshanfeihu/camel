# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
SSH Device Client: 通过 paramiko 连接网络设备（NSAE/InfosecOS），执行 CLI 命令并捕获输出。
支持交互式 shell、enable/config 模式切换、--More-- 分页处理。
"""
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import paramiko

logger = logging.getLogger(__name__)


class NSAESSHClient:
    """NSAE/InfosecOS 设备 SSH 交互客户端"""

    # 匹配各种模式下的提示符
    PROMPT_PATTERNS = [
        r'[\w\-]+\>',          # 用户模式: NSAE>
        r'[\w\-]+\#',          # enable 模式: NSAE#
        r'[\w\-]+\([^\)]+\)\#', # config 模式: NSAE(config)#
    ]
    MORE_PATTERN = r'--More--'
    # Some NSAE builds do not accept protocol-suffixed show forms.
    SHOW_CMD_FALLBACKS: Dict[str, List[str]] = {
        "show slb virtual http": ["show slb virtual"],
        "show slb real http": ["show slb real"],
        "show statistics slb real http": ["show statistics slb real"],
        "show slb group health": ["show slb group"],
    }

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        timeout: int = 15,
        command_timeout: int = 30,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.ssh: Optional[paramiko.SSHClient] = None
        self.shell: Optional[paramiko.Channel] = None
        self._buffer = ""

    def connect(self) -> str:
        """建立 SSH 连接并打开交互式 shell，返回登录 banner"""
        logger.info("SSH 连接到 %s:%d (用户: %s)", self.host, self.port, self.username)
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=self.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self.shell = self.ssh.invoke_shell(term='vt100', width=200, height=50)
        self.shell.settimeout(self.command_timeout)
        # 等待设备初始化输出
        banner = self._read_until_prompt(initial_wait=3.0)
        logger.info("SSH 登录成功，Banner:\n%s", banner)
        return banner

    def disconnect(self):
        """断开 SSH 连接"""
        if self.shell:
            try:
                self.shell.close()
            except Exception:
                pass
            self.shell = None
        if self.ssh:
            try:
                self.ssh.close()
            except Exception:
                pass
            self.ssh = None
        logger.info("SSH 连接已断开")

    def _read_until_prompt(self, initial_wait: float = 1.0, max_wait: float = 0) -> str:
        """读取输出直到检测到设备提示符或超时，自动处理 --More-- 分页"""
        if max_wait <= 0:
            max_wait = self.command_timeout
        output = ""
        start = time.time()
        time.sleep(initial_wait)

        while time.time() - start < max_wait:
            if self.shell.recv_ready():
                chunk = self.shell.recv(65535).decode('utf-8', errors='replace')
                output += chunk
                # 处理 --More-- 分页
                if re.search(self.MORE_PATTERN, output):
                    # 发送空格继续
                    output = re.sub(r'\s*--More--\s*', '', output)
                    self.shell.send(' ')
                    time.sleep(0.3)
                    continue
                # 检测是否到达提示符
                stripped = output.rstrip()
                if any(re.search(p + r'\s*$', stripped) for p in self.PROMPT_PATTERNS):
                    break
            else:
                time.sleep(0.3)

        return output

    def send_command(self, command: str, wait: float = 2.0) -> str:
        """
        发送单条命令并捕获输出。
        返回命令执行后的设备输出（不含命令本身的回显，但保留提示符用于调试）。
        """
        if not self.shell:
            raise RuntimeError("SSH shell 未连接")

        # 清除缓冲区中的残留数据
        while self.shell.recv_ready():
            self.shell.recv(65535)

        logger.debug("发送命令: %s", command)
        self.shell.send(command + '\n')
        raw_output = self._read_until_prompt(initial_wait=wait)

        # 去除命令回显（第一行通常是命令本身）
        lines = raw_output.splitlines()
        if lines and command.strip() in lines[0]:
            lines = lines[1:]
        output = '\n'.join(lines)

        return output

    def enter_enable_mode(self, password: str = "") -> str:
        """进入 enable（特权）模式，默认使用空密码。已在 enable/config 模式时跳过。"""
        logger.info("进入 enable 模式...")
        output = self.send_command("enable", wait=2.0)
        # 如果设备要求密码，发送空密码或指定密码
        if "assword" in output.lower():
            output = self.send_command(password, wait=2.0)
        # 检查是否已成功在 enable 模式下（提示符含 #）
        if re.search(r'[\w\-]+\#\s*$', output.rstrip()):
            logger.info("已进入 enable 模式")
            return output
        # 如果提示含 denied/failed, 可能是因为已处于 enable 模式再次执行导致。
        # 发送一个无害命令来检查当前提示符类型。
        if "denied" in output.lower() or "failed" in output.lower():
            probe = self.send_command("show clock", wait=2.0)
            if re.search(r'[\w\-]+\#\s*$', probe.rstrip()):
                logger.info("已处于 enable 模式（重复 enable 被拒绝但提示符确认 #），继续")
                return probe
            logger.error("进入 enable 模式失败: %s", output.strip()[:200])
            raise RuntimeError(f"Enable 模式认证失败: {output.strip()[:200]}")
        logger.info("已进入 enable 模式")
        return output

    def enter_config_mode(self) -> str:
        """进入 config（配置）模式，使用 'config terminal' 命令"""
        logger.info("进入 config 模式...")
        output = self.send_command("config terminal", wait=2.0)
        if "^" in output and "(config)" not in output:
            logger.error("进入 config 模式失败: %s", output.strip()[:200])
            raise RuntimeError(f"Config 模式进入失败: {output.strip()[:200]}")
        logger.info("已进入 config 模式")
        return output

    def exit_config_mode(self) -> str:
        """退出 config 模式"""
        output = self.send_command("exit")
        return output

    def exit_enable_mode(self) -> str:
        """退出 enable 模式"""
        output = self.send_command("exit")
        return output

    def execute_config_commands(
        self, commands: List[str], enter_config: bool = True
    ) -> List[Dict[str, str]]:
        """
        在配置模式下批量执行命令。
        返回每条命令的执行结果列表，每项包含 command, output, status。
        """
        results = []

        if enter_config:
            self.enter_enable_mode()
            self.enter_config_mode()

        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            logger.info("执行配置命令: %s", cmd)
            output = self.send_command(cmd, wait=3.0)
            # 检测错误关键字
            error_keywords = [
                "% invalid", "% error", "% unknown", "command not found",
                "syntax error", "% unrecognized", "failed",
            ]
            status = "success"
            for kw in error_keywords:
                if kw.lower() in output.lower():
                    status = "error"
                    break
            # 检测 NSAE/InfosecOS 的 ^ 错误标记（命令回显后紧跟 ^ 表示语法错误）
            if status == "success":
                out_lines = output.strip().splitlines()
                for line in out_lines:
                    stripped_line = line.strip()
                    if stripped_line == "^" or (
                        len(stripped_line) <= 3 and "^" in stripped_line
                    ):
                        status = "error"
                        logger.warning("检测到 ^ 错误标记，命令可能语法错误: %s", cmd)
                        break
            results.append({
                "command": cmd,
                "output": output.strip(),
                "status": status,
            })
            logger.info("  输出: %s", output.strip()[:300])
            logger.info("  状态: %s", status)

        if enter_config:
            self.exit_config_mode()

        return results

    def execute_show_commands(self, commands: List[str]) -> List[Dict[str, str]]:
        """
        执行 show/验证类命令（在 enable 模式下执行）。
        返回每条命令的执行结果列表。
        """
        results = []
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            logger.info("执行验证命令: %s", cmd)
            result = self._execute_show_with_fallback(cmd, wait=3.0)
            results.append(result)
            logger.info("  输出: %s", result.get("output", "")[:500])
            logger.info("  状态: %s", result.get("status", "unknown"))
            if result.get("executed_command"):
                logger.info("  回退命令: %s", result["executed_command"])
        return results

    @staticmethod
    def _has_cli_error(output: str) -> bool:
        """Detect NSAE CLI syntax/command errors from raw output."""
        text = output.strip().lower()
        if any(
            kw in text for kw in (
                "% invalid",
                "% error",
                "% unknown",
                "% unrecognized",
                "syntax error",
                "invalid input",
                "command not found",
            )
        ):
            return True

        for line in output.strip().splitlines():
            stripped_line = line.strip()
            if stripped_line == "^" or (
                len(stripped_line) <= 3 and "^" in stripped_line
            ):
                return True
        return False

    def _execute_show_with_fallback(
        self, command: str, wait: float = 3.0
    ) -> Dict[str, str]:
        """Run show command and retry with compatibility variants on syntax errors."""
        output = self.send_command(command, wait=wait)
        clean_output = output.strip()
        if not self._has_cli_error(clean_output):
            return {
                "command": command,
                "output": clean_output,
                "status": "executed",
            }

        for fallback_cmd in self.SHOW_CMD_FALLBACKS.get(command.lower(), []):
            logger.info("show 命令疑似语法不兼容，尝试回退: %s", fallback_cmd)
            fallback_output = self.send_command(fallback_cmd, wait=wait)
            fallback_clean = fallback_output.strip()
            if self._has_cli_error(fallback_clean):
                continue

            combined_output = (
                f"[primary] {command}\n{clean_output}\n\n"
                f"[fallback] {fallback_cmd}\n{fallback_clean}"
            )
            return {
                "command": command,
                "executed_command": fallback_cmd,
                "output": combined_output.strip(),
                "status": "executed",
            }

        return {
            "command": command,
            "output": clean_output,
            "status": "error",
        }

    def get_running_config_section(self, keyword: str = "") -> str:
        """获取设备指定模块的配置摘要。

        支持模块关键字：
        - slb: SLB 虚拟/真实服务器及分组
        - health: 健康检查配置
        - llb: 链路负载均衡
        - ssl: SSL 配置
        - ha / vrrp: 高可用配置
        - nat: NAT 配置
        - (空): 获取所有已知模块配置
        """
        # 模块 → show 命令映射
        _MODULE_SHOW_CMDS: Dict[str, List[str]] = {
            "slb": [
                "show slb virtual http",
                "show slb real http",
                "show slb group method",
            ],
            "health": [
                "show slb health",
            ],
            "llb": [
                "show llb",
                "show llb link",
            ],
            "ssl": [
                "show ssl",
                "show ssl certificate",
            ],
            "ha": [
                "show ha",
                "show vrrp",
            ],
            "nat": [
                "show nat",
                "show nat pool",
            ],
        }
        # 别名映射
        _ALIASES: Dict[str, str] = {
            "vrrp": "ha",
            "failover": "ha",
            "certificate": "ssl",
            "tls": "ssl",
        }

        keyword = keyword.strip().lower()
        keyword = _ALIASES.get(keyword, keyword)

        if keyword:
            modules_to_query = [keyword] if keyword in _MODULE_SHOW_CMDS else []
        else:
            # 空关键字 → 获取所有模块
            modules_to_query = list(_MODULE_SHOW_CMDS.keys())

        results = []
        for mod in modules_to_query:
            cmds = _MODULE_SHOW_CMDS.get(mod, [])
            for cmd in cmds:
                try:
                    show_result = self._execute_show_with_fallback(cmd, wait=5.0)
                    if show_result.get("status") == "error":
                        continue

                    output = show_result.get("output", "").strip()
                    if not output:
                        continue

                    header = cmd
                    if show_result.get("executed_command"):
                        header = f"{cmd} -> {show_result['executed_command']}"
                    results.append(f"[{header}]\n{output}")
                except Exception as e:
                    logger.warning("执行 %s 失败: %s", cmd, e)

        return "\n\n".join(results) if results else "(无相关配置)"


def create_ssh_client_from_env() -> NSAESSHClient:
    """从环境变量创建 SSH 客户端实例"""
    host = os.environ.get("LB_DEVICE_IP", "")
    if not host:
        raise ValueError("LB_DEVICE_IP 未配置，请在 .env 中设置")
    username = os.environ.get("LB_USERNAME", "admin")
    password = os.environ.get("LB_PASSWORD", "admin")
    port = int(os.environ.get("LB_SSH_PORT", "22"))

    return NSAESSHClient(
        host=host,
        username=username,
        password=password,
        port=port,
    )
