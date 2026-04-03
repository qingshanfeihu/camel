# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
VM Controller: 通过 SSH 在远程 Linux 虚拟机上部署/控制 HTTP 测试服务、
配置网络接口、执行业务流量探测。

设计原则:
- 172.16.6.x 仅做 SSH 管理面 (连接 VM_MGMT_IP)
- 业务流量通过 VM eth1 (服务器侧) 和 eth2 (客户端侧) 承载
- VM 无公网，所有依赖通过 python3 内置模块完成
"""
import logging
import os
import time
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import paramiko

logger = logging.getLogger(__name__)

# ── 默认值 ────────────────────────────────────────────────────────────
_VM_WEB_ROOT = "/tmp/inagent_webroot"
_VM_PIDFILE = "/tmp/inagent_httpd.pid"
_VM_HTTP_SCRIPT = "/tmp/inagent_httpd.py"


class VMController:
    """通过 SSH 管理远程 Linux 测试虚拟机。

    典型用法:
        vm = VMController.from_env()
        vm.connect()
        vm.configure_interface("eth1", "10.0.3.100", "255.255.255.0")
        vm.deploy_http_server(html_content, port=80, bind_ip="10.0.3.100")
        vm.stop_http_server()
        vm.disconnect()
    """

    def __init__(
        self,
        host: str,
        username: str = "root",
        password: str = "click1",
        port: int = 22,
        timeout: int = 15,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self._ssh: Optional[paramiko.SSHClient] = None

    # ── 工厂方法 ──────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "VMController":
        """从环境变量创建实例 (VM_MGMT_IP / VM_USERNAME / VM_PASSWORD / VM_SSH_PORT)。"""
        host = os.environ.get("VM_MGMT_IP", "")
        if not host:
            raise EnvironmentError("VM_MGMT_IP 未设置，无法创建 VMController")
        return cls(
            host=host,
            username=os.environ.get("VM_USERNAME", "root"),
            password=os.environ.get("VM_PASSWORD", "click1"),
            port=int(os.environ.get("VM_SSH_PORT", "22")),
        )

    # ── 连接管理 ──────────────────────────────────────────────────────

    def connect(self) -> None:
        """通过管理 IP 建立 SSH 连接。"""
        if self._ssh is not None:
            return
        logger.info("SSH 连接到 VM %s:%d (用户: %s)", self.host, self.port, self.username)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=self.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self._ssh = ssh
        logger.info("VM SSH 连接成功")

    def disconnect(self) -> None:
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None
            logger.info("VM SSH 连接已断开")

    @property
    def connected(self) -> bool:
        return self._ssh is not None and self._ssh.get_transport() is not None

    def _ensure_connected(self) -> None:
        if not self.connected:
            self.connect()

    # ── 命令执行 ──────────────────────────────────────────────────────

    def run(self, cmd: str, timeout: int = 30, check: bool = False) -> Tuple[int, str, str]:
        """在 VM 上执行命令，返回 (exit_code, stdout, stderr)。"""
        self._ensure_connected()
        assert self._ssh is not None
        logger.debug("VM exec: %s", cmd)
        _in, _out, _err = self._ssh.exec_command(cmd, timeout=timeout)
        exit_code = _out.channel.recv_exit_status()
        stdout = _out.read().decode("utf-8", errors="replace")
        stderr = _err.read().decode("utf-8", errors="replace")
        if check and exit_code != 0:
            raise RuntimeError(
                f"VM 命令失败 (exit={exit_code}): {cmd}\nSTDOUT: {stdout}\nSTDERR: {stderr}"
            )
        return exit_code, stdout, stderr

    # ── 网络接口配置 ──────────────────────────────────────────────────

    def configure_interface(
        self,
        iface: str,
        ip_addr: str,
        netmask: str = "255.255.255.0",
        *,
        bring_up: bool = True,
    ) -> str:
        """配置 VM 的网络接口 IP 并启用。

        Args:
            iface: 接口名 (eth1/eth2)
            ip_addr: IPv4 地址
            netmask: 子网掩码
            bring_up: 是否同时 UP 接口

        Returns:
            命令输出摘要
        """
        logger.info("VM 配置接口 %s: %s/%s", iface, ip_addr, netmask)
        commands = []
        if bring_up:
            commands.append(f"ip link set {iface} up")
        # 先清除已有 IP，再设置新 IP
        commands.append(f"ip addr flush dev {iface}")
        commands.append(f"ip addr add {ip_addr}/{self._mask_to_prefix(netmask)} dev {iface}")
        combined = " && ".join(commands)
        rc, out, err = self.run(combined, check=True)
        # 等待接口 UP
        if bring_up:
            time.sleep(1)
        return f"{iface}={ip_addr}/{netmask} UP (rc={rc})"

    def add_route(self, destination: str, gateway: str, iface: str = "") -> str:
        """在 VM 上添加静态路由。"""
        cmd = f"ip route replace {destination} via {gateway}"
        if iface:
            cmd += f" dev {iface}"
        rc, out, err = self.run(cmd)
        logger.info("VM 添加路由: %s (rc=%d)", cmd, rc)
        return f"route {destination} via {gateway} (rc={rc})"

    def get_interface_ip(self, iface: str) -> Optional[str]:
        """获取接口当前的 IPv4 地址。"""
        rc, out, _ = self.run(
            f"ip -4 addr show {iface} | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){{3}}'"
        )
        ip = out.strip().split("\n")[0].strip() if out.strip() else None
        return ip

    # ── HTTP 服务管理 ─────────────────────────────────────────────────
    # 使用 python3 内置 http.server 模块，不依赖任何额外安装

    def deploy_http_server(
        self,
        html_content: str,
        port: int = 80,
        bind_ip: str = "0.0.0.0",
        web_root: str = _VM_WEB_ROOT,
    ) -> Dict[str, Any]:
        """在 VM 上部署 HTTP 服务并返回信息。

        Args:
            html_content: index.html 的内容
            port: 监听端口
            bind_ip: 绑定的 IP (建议用业务口 IP)
            web_root: web 根目录路径

        Returns:
            包含 server_url, port, bind_ip, web_root, html_file 的 dict
        """
        logger.info("VM 部署 HTTP 服务: %s:%d (webroot=%s)", bind_ip, port, web_root)
        # 1. 先停止可能存在的旧服务
        self._kill_existing_httpd(port=port)
        # 2. 准备目录和文件
        self.run(f"mkdir -p {web_root}", check=True)
        # 写入 index.html (使用 heredoc 安全写入)
        self._write_remote_file(f"{web_root}/index.html", html_content)
        # 3. 创建自定义 HTTP 服务脚本 — 支持绑定指定 IP
        http_script = self._make_http_script(web_root, port, bind_ip)
        self._write_remote_file(_VM_HTTP_SCRIPT, http_script)
        # 4. 后台启动
        self.run(
            f"nohup python3 {_VM_HTTP_SCRIPT} > /tmp/inagent_httpd.log 2>&1 & "
            f"echo $! > {_VM_PIDFILE}",
            check=True,
        )
        time.sleep(1)
        # 5. 验证启动
        rc, pid_out, _ = self.run(f"cat {_VM_PIDFILE}")
        pid = pid_out.strip()
        rc2, _, _ = self.run(f"kill -0 {pid} 2>/dev/null")
        if rc2 != 0:
            rc3, log_out, _ = self.run("cat /tmp/inagent_httpd.log")
            raise RuntimeError(f"VM HTTP 服务启动失败，日志:\n{log_out}")
        logger.info("VM HTTP 服务已启动: PID=%s, %s:%d", pid, bind_ip, port)
        server_url = f"http://{bind_ip}:{port}"
        return {
            "server_url": server_url,
            "port": port,
            "bind_ip": bind_ip,
            "web_root": web_root,
            "html_file": f"{web_root}/index.html",
            "pid": pid,
        }

    def stop_http_server(self, port: int = 80) -> bool:
        """停止 VM 上的 HTTP 服务。"""
        logger.info("VM 停止 HTTP 服务")
        self._kill_existing_httpd(port=port)
        return True

    def restart_http_server(
        self,
        html_content: str,
        port: int = 80,
        bind_ip: str = "0.0.0.0",
        web_root: str = _VM_WEB_ROOT,
    ) -> Dict[str, Any]:
        """停止后重新启动 HTTP 服务（用于 restore_service 步骤）。"""
        return self.deploy_http_server(html_content, port, bind_ip, web_root)

    def deploy_multi_http_servers(
        self,
        html_content: str,
        servers: List[Dict[str, Any]],
        bind_ip: str = "0.0.0.0",
    ) -> List[Dict[str, Any]]:
        """在 VM 上部署多个 HTTP 服务实例（同 IP 不同端口），模拟多台后端服务器。

        Args:
            html_content: 所有实例共用的 index.html 内容。
            servers: 服务器列表，每个元素至少包含 ``port`` 和可选 ``name``。
                例: [{"name": "rs1", "port": 80}, {"name": "rs2", "port": 8081}]
            bind_ip: 绑定 IP，默认 0.0.0.0（覆盖所有本地 IP）。

        Returns:
            每个实例的部署信息列表。
        """
        results: List[Dict[str, Any]] = []
        for srv in servers:
            port = int(srv.get("port", 80))
            name = srv.get("name", f"rs_{port}")
            web_root = f"/tmp/inagent_webroot_{port}"
            pid_file = f"/tmp/inagent_httpd_{port}.pid"
            script_file = f"/tmp/inagent_httpd_{port}.py"
            logger.info("部署 HTTP 实例 %s: %s:%d (webroot=%s)", name, bind_ip, port, web_root)

            # 1. 停止该端口上可能存在的旧服务
            self._kill_existing_httpd(port=port)

            # 2. 准备目录和文件
            self.run(f"mkdir -p {web_root}", check=True)
            self._write_remote_file(f"{web_root}/index.html", html_content)

            # 3. 创建 HTTP 服务脚本
            http_script = self._make_http_script(web_root, port, bind_ip)
            self._write_remote_file(script_file, http_script)

            # 4. 后台启动
            self.run(
                f"nohup python3 {script_file} > /tmp/inagent_httpd_{port}.log 2>&1 & "
                f"echo $! > {pid_file}",
                check=True,
            )
            time.sleep(0.5)

            # 5. 验证启动
            rc, pid_out, _ = self.run(f"cat {pid_file}")
            pid = pid_out.strip()
            rc2, _, _ = self.run(f"kill -0 {pid} 2>/dev/null")
            if rc2 != 0:
                rc3, log_out, _ = self.run(f"cat /tmp/inagent_httpd_{port}.log")
                logger.warning("HTTP 实例 %s (端口 %d) 启动失败: %s", name, port, log_out[:200])
                results.append({"name": name, "port": port, "success": False, "error": log_out[:200]})
                continue

            logger.info("HTTP 实例 %s 已启动: PID=%s, %s:%d", name, pid, bind_ip, port)
            results.append({
                "name": name,
                "port": port,
                "bind_ip": bind_ip,
                "web_root": web_root,
                "pid": pid,
                "success": True,
                "server_url": f"http://{bind_ip}:{port}",
            })

        return results

    def stop_multi_http_servers(self, ports: List[int]) -> None:
        """停止指定端口列表上的所有 HTTP 服务实例。"""
        for port in ports:
            self._kill_existing_httpd(port=port)
        logger.info("已停止 %d 个 HTTP 实例 (端口: %s)", len(ports), ports)

    def restart_multi_http_servers(
        self,
        html_content: str,
        servers: List[Dict[str, Any]],
        bind_ip: str = "0.0.0.0",
    ) -> List[Dict[str, Any]]:
        """停止后重新启动多个 HTTP 服务实例。"""
        ports = [int(s.get("port", 80)) for s in servers]
        self.stop_multi_http_servers(ports)
        return self.deploy_multi_http_servers(html_content, servers, bind_ip)

    def modify_html_content(self, new_content: str, web_root: str = _VM_WEB_ROOT) -> None:
        """修改 VM 上的 index.html 内容 (HTTP 服务保持运行，下次请求立即生效)。"""
        html_path = f"{web_root}/index.html"
        logger.info("VM 修改 HTML: %s (%d bytes)", html_path, len(new_content))
        self._write_remote_file(html_path, new_content)

    def read_html_content(self, web_root: str = _VM_WEB_ROOT) -> str:
        """读取 VM 上的 index.html 内容。"""
        rc, out, _ = self.run(f"cat {web_root}/index.html")
        return out

    # ── HTTP 探测 (从 VM 发起) ────────────────────────────────────────

    def http_probe(
        self,
        url: str,
        keyword: str = "",
        timeout: int = 5,
    ) -> Dict[str, Any]:
        """在 VM 上用 curl 发起 HTTP 请求 (模拟客户端流量)。

        Args:
            url: 目标 URL (通常是 VIP)
            keyword: 预期在响应 body 中出现的关键词
            timeout: 超时秒数

        Returns:
            包含 success, status_code, content_match, body_snippet, error 的 dict
        """
        # 使用 curl 进行 HTTP 探测
        cmd = (
            f'curl -s -o /tmp/_probe_body -w '
            f'"HTTP_CODE:%{{http_code}}" '
            f'--connect-timeout {timeout} --max-time {timeout} '
            f'"{url}" 2>/tmp/_probe_err; '
            f'echo; cat /tmp/_probe_body 2>/dev/null'
        )
        rc, out, _ = self.run(cmd, timeout=timeout + 10)
        # 解析结果
        result: Dict[str, Any] = {"success": False}
        if "HTTP_CODE:" in out:
            parts = out.split("HTTP_CODE:", 1)
            code_part = parts[1].split("\n", 1)
            try:
                status_code = int(code_part[0].strip())
            except ValueError:
                status_code = 0
            body = code_part[1] if len(code_part) > 1 else ""
            result = {
                "success": status_code > 0 and status_code < 400,
                "status_code": status_code,
                "body_snippet": body[:300],
                "content_match": (
                    keyword.lower() in body.lower() if keyword else True
                ),
            }
        else:
            # curl 完全失败
            _, err_out, _ = self.run("cat /tmp/_probe_err 2>/dev/null")
            result = {"success": False, "error": out[:200] + err_out[:200]}
        return result

    # ── 辅助方法 ──────────────────────────────────────────────────────

    def _kill_existing_httpd(self, port: int = 80) -> None:
        """停止任何已存在的测试 HTTP 服务进程，以及占用目标端口的进程。"""
        # 1. 先按 PID 文件杀
        self.run(f"if [ -f {_VM_PIDFILE} ]; then kill $(cat {_VM_PIDFILE}) 2>/dev/null; fi")
        # 2. 杀所有 inagent_httpd.py 进程
        self.run(f"pkill -f inagent_httpd.py 2>/dev/null")
        self.run(f"rm -f {_VM_PIDFILE}")
        # 3. 停止常见 web 服务（nginx/apache）
        self.run("systemctl stop nginx 2>/dev/null; nginx -s stop 2>/dev/null")
        self.run("systemctl stop apache2 2>/dev/null; apachectl stop 2>/dev/null")
        # 4. 兜底：杀掉占用目标端口的所有进程
        self.run(f"fuser -k {port}/tcp 2>/dev/null")
        time.sleep(0.5)
        # 5. 确认端口已释放
        rc, out, _ = self.run(f"ss -tlnp | grep ':{port} '")
        if out.strip():
            logger.warning("端口 %d 仍被占用: %s — 尝试强制清理", port, out.strip())
            self.run(f"fuser -k -9 {port}/tcp 2>/dev/null")
            time.sleep(0.5)

    def _write_remote_file(self, remote_path: str, content: str) -> None:
        """通过 SFTP 将内容写入 VM 上的文件。"""
        self._ensure_connected()
        assert self._ssh is not None
        sftp = self._ssh.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    @staticmethod
    def _make_http_script(web_root: str, port: int, bind_ip: str) -> str:
        """生成在 VM 上运行的 HTTP 服务脚本。"""
        return f'''#!/usr/bin/env python3
"""INAGENT 测试用 HTTP 服务 — 自动生成，请勿手动编辑"""
import http.server
import socketserver
import os
import signal
import sys

PORT = {port}
BIND = "{bind_ip}"
DIRECTORY = "{web_root}"

os.chdir(DIRECTORY)

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志

# 允许端口复用
socketserver.TCPServer.allow_reuse_address = True

httpd = socketserver.TCPServer((BIND, PORT), Handler)

def shutdown(sig, frame):
    httpd.shutdown()
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

print(f"Serving {{DIRECTORY}} on {{BIND}}:{{PORT}}")
httpd.serve_forever()
'''

    @staticmethod
    def _mask_to_prefix(mask: str) -> int:
        """将子网掩码转为 CIDR 前缀长度 (如 255.255.255.0 -> 24)。"""
        try:
            parts = mask.split(".")
            bits = sum(bin(int(p)).count("1") for p in parts)
            return bits
        except Exception:
            return 24
