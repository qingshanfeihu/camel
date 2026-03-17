# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
统一的后台任务管理器

为所有 WebUI 长耗时操作提供统一的:
  - 异步子进程启动 (非阻塞)
  - WebSocket 实时日志推送
  - 任务状态查询 / 取消
  - 并发控制 (同类任务互斥)

使用方:
  - knowledge.py: 知识库构建 (manage_database + GraphRAG)
  - testing.py:   测试流水线 (pipeline_runner)
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from fastapi import WebSocket

from INAGENT.web.deps import REPO_ROOT

logger = logging.getLogger(__name__)


# ── 数据结构 ────────────────────────────────────────────────────────
@dataclass
class TaskInfo:
    task_id: str
    task_type: str          # "kb_build", "test_run", ...
    description: str
    status: str = "running"  # running / success / failed / cancelled
    created_at: str = ""
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_lines: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    # internal — not serialised
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)


def _new_task_id() -> str:
    return str(uuid.uuid4())[:8]


def _default_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    no_proxy = env.get("NO_PROXY", "")
    if "127.0.0.1" not in no_proxy:
        env["NO_PROXY"] = f"127.0.0.1,localhost,{no_proxy}".rstrip(",")
        env["no_proxy"] = env["NO_PROXY"]
    return env


# ── 全局注册表 ──────────────────────────────────────────────────────
_tasks: Dict[str, TaskInfo] = {}
_ws_subs: Dict[str, List[WebSocket]] = {}


# ── 公共 API ────────────────────────────────────────────────────────
def active_tasks(task_type: Optional[str] = None) -> List[TaskInfo]:
    """返回正在运行的任务列表, 可按 type 过滤。"""
    return [
        t for t in _tasks.values()
        if t.status == "running" and (task_type is None or t.task_type == task_type)
    ]


def get_task(task_id: str) -> Optional[TaskInfo]:
    return _tasks.get(task_id)


async def start_task(
    *,
    task_type: str,
    description: str,
    cmd: List[str],
    cwd: str | None = None,
    env: dict | None = None,
    on_complete: Optional[Callable[[TaskInfo], Any]] = None,
) -> TaskInfo:
    """
    启动一个后台子进程任务。

    Returns:
        TaskInfo (status="running"), 后台 asyncio task 自动收集输出。
    """
    task_id = _new_task_id()
    now = datetime.now().isoformat()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd or str(REPO_ROOT),
        env=env or _default_env(),
        encoding="utf-8",
        errors="replace",
    )

    info = TaskInfo(
        task_id=task_id,
        task_type=task_type,
        description=description,
        created_at=now,
        _process=proc,
    )
    _tasks[task_id] = info

    asyncio.get_event_loop().create_task(
        _stream_output(info, on_complete)
    )
    return info


def cancel_task(task_id: str) -> bool:
    """取消正在运行的任务。成功返回 True。"""
    info = _tasks.get(task_id)
    if not info or info.status != "running" or not info._process:
        return False
    info._process.terminate()
    info.status = "cancelled"
    info.finished_at = datetime.now().isoformat()
    return True


# ── WebSocket 管理 ──────────────────────────────────────────────────
async def subscribe_ws(task_id: str, ws: WebSocket) -> None:
    """
    订阅任务日志流。保持连接直到任务结束或客户端断开。

    调用前请先 ``await ws.accept()``。
    """
    _ws_subs.setdefault(task_id, []).append(ws)

    # 先发送已有日志 (断线重连场景)
    info = _tasks.get(task_id)
    if info:
        for line in info.log_lines:
            try:
                await ws.send_text(line)
            except Exception:
                break

    try:
        while task_id in _tasks and _tasks[task_id].status == "running":
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # keepalive
                try:
                    await ws.send_text("")
                except Exception:
                    break
            except Exception:
                break
    finally:
        subs = _ws_subs.get(task_id, [])
        if ws in subs:
            subs.remove(ws)


# ── 内部: 后台日志收集 ──────────────────────────────────────────────
async def _stream_output(
    info: TaskInfo,
    on_complete: Optional[Callable[[TaskInfo], Any]] = None,
) -> None:
    """后台读子进程 stdout, 广播到 WebSocket 订阅者。"""
    proc = info._process
    if proc is None:
        return

    loop = asyncio.get_event_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line and proc.poll() is not None:
                break
            if line:
                info.log_lines.append(line)
                # 只保留最近 50 000 行, 避免内存泄漏
                if len(info.log_lines) > 50_000:
                    info.log_lines = info.log_lines[-40_000:]
                for ws in list(_ws_subs.get(info.task_id, [])):
                    try:
                        await ws.send_text(line)
                    except Exception:
                        pass
    except Exception as e:
        logger.error("日志收集异常 [%s]: %s", info.task_id, e)
    finally:
        exit_code = proc.wait()
        info.exit_code = exit_code
        if info.status == "running":
            info.status = "success" if exit_code == 0 else "failed"
        info.finished_at = datetime.now().isoformat()

        # 通知所有 WebSocket 订阅者任务结束
        done_msg = f"\n[DONE] task_id={info.task_id} status={info.status}\n"
        for ws in list(_ws_subs.pop(info.task_id, [])):
            try:
                await ws.send_text(done_msg)
                await ws.close()
            except Exception:
                pass

        # 回调
        if on_complete:
            try:
                result = on_complete(info)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("on_complete callback 异常 [%s]: %s", info.task_id, e)
