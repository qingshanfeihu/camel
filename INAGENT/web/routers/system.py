# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模块5: 系统管理 — 环境配置 & 连通性检测 API

功能:
- 环境变量读取/修改 (.env)
- LLM Gateway / SSH 设备 / VM 连通性检测
- RAG / GraphRAG 状态
- 日志查看
- LLM Gateway 进程控制 (启动/停止/状态/模式切换)
"""
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from INAGENT.config.project_config import cfg_int, cfg_str
from INAGENT.web.deps import (
    INAGENT_DIR, KB_DIR, KB_PATH, INDEX_PATH, LOG_DIR, REPO_ROOT, get_db,
)
from INAGENT.web.models import EnvConfig, SystemStatus

logger = logging.getLogger(__name__)
router = APIRouter()

_ENV_PATH = INAGENT_DIR / ".env"
_GRAPHRAG_DIR = INAGENT_DIR / "graphrag_index"
_GATEWAY_DIR = REPO_ROOT / "llm_gateway"

# Gateway 进程管理
_gateway_proc: Optional[subprocess.Popen] = None
_gateway_log_lines: List[str] = []
_gateway_log_lock = threading.Lock()


# ── 系统状态总览 ────────────────────────────────────────────────────
@router.get("/status", response_model=SystemStatus)
async def system_status():
    """一键检测所有外部依赖的连通性。"""
    status = SystemStatus(
        kb_exists=KB_PATH.exists(),
        rag_ready=KB_PATH.exists() and INDEX_PATH.exists(),
        graphrag_ready=(_GRAPHRAG_DIR / "output").exists(),
    )

    # LLM Gateway
    gw_url = cfg_str("llm.gateway.base_url", "http://127.0.0.1:9000", env="LLM_GATEWAY_BASE_URL")
    try:
        req = urllib.request.Request(f"{gw_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status.llm_gateway = resp.status == 200
    except Exception:
        status.llm_gateway = False

    # NSAE 设备 SSH
    device_ip = cfg_str("system.lb_device_ip", "", env="LB_DEVICE_IP")
    if device_ip:
        status.nsae_device = _tcp_check(device_ip, 22)

    # 测试 VM SSH
    vm_ip = cfg_str("system.vm_mgmt_ip", "", env="VM_MGMT_IP")
    vm_port = cfg_int("system.vm_ssh_port", 22, env="VM_SSH_PORT")
    if vm_ip:
        status.test_vm = _tcp_check(vm_ip, vm_port)

    return status


def _tcp_check(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# ── 详细连通性探测 ──────────────────────────────────────────────────
@router.get("/probe/{target}")
async def probe(target: str):
    """
    对指定目标执行详细连通性探测。
    target: llm_gateway | nsae_device | test_vm | rag | graphrag
    """
    if target == "llm_gateway":
        return _probe_llm_gateway()
    elif target == "nsae_device":
        return _probe_ssh("LB_DEVICE_IP", "LB_USERNAME", "LB_PASSWORD", 22)
    elif target == "test_vm":
        return _probe_ssh(
            "VM_MGMT_IP",
            "VM_USERNAME",
            "VM_PASSWORD",
            cfg_int("system.vm_ssh_port", 22, env="VM_SSH_PORT"),
        )
    elif target == "rag":
        return _probe_rag()
    elif target == "graphrag":
        return _probe_graphrag()
    else:
        raise HTTPException(400, f"未知探测目标: {target}")


def _probe_llm_gateway():
    gw_url = cfg_str("llm.gateway.base_url", "http://127.0.0.1:9000", env="LLM_GATEWAY_BASE_URL")
    result = {"target": "llm_gateway", "url": gw_url, "connected": False, "models": {}}
    try:
        req = urllib.request.Request(f"{gw_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            result["connected"] = resp.status == 200
            result["health"] = json.loads(resp.read())
    except Exception as e:
        result["error"] = str(e)
    if result["connected"]:
        try:
            result["models"] = _gateway_request("/admin/models")
        except Exception:
            pass
    return result


def _probe_ssh(ip_env, user_env, pass_env, port):
    ip_key_map = {
        "LB_DEVICE_IP": "system.lb_device_ip",
        "VM_MGMT_IP": "system.vm_mgmt_ip",
    }
    cfg_key = ip_key_map.get(ip_env, "")
    host = cfg_str(cfg_key, "", env=ip_env) if cfg_key else ""
    result = {"target": ip_env, "host": host, "port": port, "connected": False}
    if not host:
        result["error"] = f"{ip_env} 未配置"
        return result
    result["connected"] = _tcp_check(host, port)
    if not result["connected"]:
        result["error"] = f"TCP {host}:{port} 不可达"
    return result


def _probe_rag():
    result = {"target": "rag", "kb_exists": KB_PATH.exists(),
              "index_exists": INDEX_PATH.exists(), "ready": False}
    if KB_PATH.exists():
        try:
            chunks = json.loads(KB_PATH.read_text(encoding="utf-8"))
            if isinstance(chunks, dict):
                chunks = chunks.get("chunks", chunks.get("data", []))
            result["chunk_count"] = len(chunks) if isinstance(chunks, list) else 0
        except Exception:
            result["chunk_count"] = 0
    result["ready"] = result["kb_exists"] and result["index_exists"]
    return result


def _probe_graphrag():
    output_dir = _GRAPHRAG_DIR / "output"
    result = {"target": "graphrag", "workspace_exists": _GRAPHRAG_DIR.exists(),
              "output_exists": output_dir.exists(), "ready": False}
    if output_dir.exists():
        parquets = list(output_dir.rglob("*.parquet"))
        result["parquet_files"] = len(parquets)
        result["ready"] = len(parquets) > 0
    return result


# ── 环境变量管理 ────────────────────────────────────────────────────
@router.get("/env", response_model=EnvConfig)
async def get_env():
    """获取关键环境变量 (敏感信息隐藏)。"""
    return EnvConfig(
        llm_gateway_url=cfg_str("llm.gateway.base_url", "", env="LLM_GATEWAY_BASE_URL"),
        llm_chat_model=cfg_str("llm.gateway.chat_model", "", env="LLM_GATEWAY_CHAT_MODEL"),
        lb_device_ip=cfg_str("system.lb_device_ip", "", env="LB_DEVICE_IP"),
        lb_username=cfg_str("system.lb_username", "", env="LB_USERNAME"),
        vm_mgmt_ip=cfg_str("system.vm_mgmt_ip", "", env="VM_MGMT_IP"),
        vm_username=cfg_str("system.vm_username", "", env="VM_USERNAME"),
    )


@router.put("/env")
async def update_env(config: EnvConfig):
    """更新 .env 文件中的关键配置。"""
    mapping = {
        "LLM_GATEWAY_BASE_URL": config.llm_gateway_url,
        "LLM_GATEWAY_CHAT_MODEL": config.llm_chat_model,
        "LB_DEVICE_IP": config.lb_device_ip,
        "LB_USERNAME": config.lb_username,
        "VM_MGMT_IP": config.vm_mgmt_ip,
        "VM_USERNAME": config.vm_username,
    }

    if _ENV_PATH.exists():
        content = _ENV_PATH.read_text(encoding="utf-8")
        lines = content.splitlines()
    else:
        lines = []

    updated_keys = set()
    new_lines = []
    for line in lines:
        replaced = False
        for key, val in mapping.items():
            if val is not None and line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={val}")
                updated_keys.add(key)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    # 追加 .env 中不存在的新变量
    for key, val in mapping.items():
        if val is not None and key not in updated_keys:
            new_lines.append(f"{key}={val}")
            updated_keys.add(key)

    _ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # 同步到 os.environ
    for k, v in mapping.items():
        if v is not None:
            os.environ[k] = v

    return {"updated": list(updated_keys)}


# ── 日志查看 ────────────────────────────────────────────────────────
@router.get("/logs")
async def list_logs():
    """列出所有日志文件。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logs = []
    for p in sorted(LOG_DIR.glob("*.log"), reverse=True):
        logs.append({
            "name": p.name,
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
        })
    return logs


@router.get("/logs/{filename}")
async def get_log(filename: str, tail: int = 200):
    """获取日志内容 (默认最后 200 行)。"""
    p = (LOG_DIR / filename).resolve()
    if not str(p).startswith(str(LOG_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not p.exists():
        raise HTTPException(404, "日志文件不存在")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return {
        "filename": filename,
        "total_lines": len(lines),
        "lines": lines[-tail:],
    }


# ── LLM Gateway 进程控制 ────────────────────────────────────────────

class GatewayModeRequest(BaseModel):
    mode: str  # balance | race | hybrid


def _collect_gateway_output(proc: subprocess.Popen):
    """后台线程：收集 Gateway 子进程输出写入缓冲区。"""
    try:
        for line in iter(proc.stdout.readline, ""):
            with _gateway_log_lock:
                _gateway_log_lines.append(line.rstrip())
                if len(_gateway_log_lines) > 500:
                    del _gateway_log_lines[:100]
    except Exception:
        pass


def _gateway_url() -> str:
    return cfg_str("llm.gateway.base_url", "http://127.0.0.1:9000", env="LLM_GATEWAY_BASE_URL")


def _gateway_request(path: str, method: str = "GET", body=None):
    """向 Gateway HTTP 接口发起请求，返回解析后的 JSON 或抛出异常。"""
    url = f"{_gateway_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


@router.get("/gateway/status")
async def gateway_status():
    """获取 LLM Gateway 当前状态（进程 + HTTP 健康检查 + 模型列表）。"""
    global _gateway_proc
    running_locally = _gateway_proc is not None and _gateway_proc.poll() is None
    result = {
        "running_locally": running_locally,
        "pid": _gateway_proc.pid if running_locally else None,
        "connected": False,
        "health": {},
        "models": {},
        "mode": None,
        "metrics": {},
    }
    try:
        result["health"] = _gateway_request("/health")
        result["connected"] = True
        result["models"] = _gateway_request("/admin/models")
        result["mode"] = _gateway_request("/admin/mode").get("mode")
        try:
            result["metrics"] = _gateway_request("/metrics/requests")
        except Exception:
            pass
    except Exception as e:
        result["error"] = str(e)
    return result


@router.post("/gateway/start")
async def gateway_start():
    """在本地子进程中启动 LLM Gateway。"""
    global _gateway_proc, _gateway_log_lines

    # 如果已经在运行则直接返回
    if _gateway_proc is not None and _gateway_proc.poll() is None:
        return {"started": False, "message": "Gateway 已在运行", "pid": _gateway_proc.pid}

    start_script = _GATEWAY_DIR / "start.py"
    if not start_script.exists():
        raise HTTPException(500, f"start.py 不存在: {start_script}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    with _gateway_log_lock:
        _gateway_log_lines.clear()

    _gateway_proc = subprocess.Popen(
        [sys.executable, "-u", str(start_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        env=env,
        encoding="utf-8",
        errors="replace",
    )

    t = threading.Thread(
        target=_collect_gateway_output, args=(_gateway_proc,), daemon=True
    )
    t.start()

    logger.info("LLM Gateway 已启动, PID=%d", _gateway_proc.pid)
    return {"started": True, "pid": _gateway_proc.pid, "message": "Gateway 启动中..."}


@router.post("/gateway/stop")
async def gateway_stop():
    """停止本地 Gateway 子进程。"""
    global _gateway_proc
    if _gateway_proc is None or _gateway_proc.poll() is not None:
        return {"stopped": False, "message": "Gateway 未运行"}

    pid = _gateway_proc.pid
    try:
        _gateway_proc.terminate()
        _gateway_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _gateway_proc.kill()
    except Exception as e:
        raise HTTPException(500, f"停止失败: {e}")

    _gateway_proc = None
    logger.info("LLM Gateway 已停止, PID=%d", pid)
    return {"stopped": True, "pid": pid, "message": "Gateway 已停止"}


@router.post("/gateway/mode")
async def gateway_set_mode(req: GatewayModeRequest):
    """动态切换 Gateway 调用模式 (balance / race / hybrid)。"""
    if req.mode not in ("balance", "race", "hybrid"):
        raise HTTPException(400, "mode 必须为 balance / race / hybrid")
    try:
        result = _gateway_request("/admin/mode", method="POST", body={"mode": req.mode})
        return result
    except Exception as e:
        raise HTTPException(502, f"Gateway 未响应: {e}")


@router.get("/gateway/logs")
async def gateway_logs(tail: int = 100):
    """获取 Gateway 进程的最近日志（内存缓冲）。"""
    with _gateway_log_lock:
        lines = list(_gateway_log_lines[-tail:])
    return {"lines": lines, "total": len(_gateway_log_lines)}
