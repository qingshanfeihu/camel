# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模块3: 产品测试功能 — Pipeline 执行 API

功能:
- 测试任务文件 CRUD
- 启动测试 (调用 pipeline_runner / workforce_pipeline)
- 实时日志流 (WebSocket)
- 取消执行中的测试
"""
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, WebSocket

from INAGENT.web.deps import get_db, INAGENT_DIR, JOBS_DIR, REPORTS_DIR
from INAGENT.web.models import (
    JobCreateRequest,
    JobFile,
    TestRunRequest,
)
from INAGENT.web import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 测试任务文件管理 ────────────────────────────────────────────────
@router.get("/jobs", response_model=List[JobFile])
async def list_jobs():
    """列出所有测试任务文件。"""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for p in sorted(JOBS_DIR.glob("*.txt")):
        jobs.append(JobFile(
            name=p.name,
            content=p.read_text(encoding="utf-8"),
            size=p.stat().st_size,
        ))
    return jobs


@router.get("/jobs/{name}")
async def get_job(name: str):
    p = (JOBS_DIR / name).resolve()
    if not str(p).startswith(str(JOBS_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not p.exists():
        raise HTTPException(404, "任务文件不存在")
    return JobFile(name=p.name, content=p.read_text(encoding="utf-8"), size=p.stat().st_size)


@router.post("/jobs")
async def create_job(req: JobCreateRequest):
    """创建新的测试任务文件。"""
    if not req.name.endswith(".txt"):
        req.name += ".txt"
    p = JOBS_DIR / req.name
    if p.exists():
        raise HTTPException(409, "任务文件已存在")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    return JobFile(name=p.name, content=req.content, size=p.stat().st_size)


@router.put("/jobs/{name}")
async def update_job(name: str, req: JobCreateRequest):
    p = (JOBS_DIR / name).resolve()
    if not str(p).startswith(str(JOBS_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not p.exists():
        raise HTTPException(404, "任务文件不存在")
    p.write_text(req.content, encoding="utf-8")
    return JobFile(name=p.name, content=req.content, size=p.stat().st_size)


@router.delete("/jobs/{name}")
async def delete_job(name: str):
    p = (JOBS_DIR / name).resolve()
    if not str(p).startswith(str(JOBS_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not p.exists():
        raise HTTPException(404, "任务文件不存在")
    p.unlink()
    return {"deleted": name}


# ── 测试执行 ────────────────────────────────────────────────────────
@router.post("/run")
async def start_test_run(req: TestRunRequest):
    """
    启动测试流水线 (Stages 1-9)。

    在子进程中运行 pipeline_runner.py, 通过 WebSocket 推送实时日志。
    使用统一的 task_manager 管理后台进程。
    """
    # 防止重复启动
    running = task_manager.active_tasks("test_run")
    if running:
        raise HTTPException(
            409,
            f"已有测试正在运行 (task_id: {running[0].task_id})，请等待完成或先取消",
        )

    db = get_db()

    # 确定要执行的 job 文件
    job_files = req.job_files
    if not job_files:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        job_files = [p.name for p in sorted(JOBS_DIR.glob("*.txt"))]
    if not job_files:
        raise HTTPException(400, "没有找到测试任务文件")

    # 校验传入的任务文件是否存在
    missing = [jf for jf in job_files if not (JOBS_DIR / jf).exists()]
    if missing:
        raise HTTPException(400, f"任务文件不存在: {', '.join(missing)}")

    run_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()

    # 为每个 job 创建记录
    for jf in job_files:
        jpath = JOBS_DIR / jf
        content = jpath.read_text(encoding="utf-8") if jpath.exists() else ""
        db.execute(
            "INSERT INTO test_runs (id, job_file, job_content, status, created_at) VALUES (?, ?, ?, 'running', ?)",
            (f"{run_id}_{jf}", jf, content, now),
        )
    db.commit()

    # 如果指定了部分 job，则只运行指定文件（通过临时 jobs-dir）
    temp_jobs_dir = None
    jobs_dir_to_run = JOBS_DIR
    if req.job_files:
        temp_jobs_dir = INAGENT_DIR / "web" / "_temp_jobs" / run_id
        temp_jobs_dir.mkdir(parents=True, exist_ok=True)
        for jf in req.job_files:
            src = JOBS_DIR / jf
            if src.exists():
                (temp_jobs_dir / jf).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        jobs_dir_to_run = temp_jobs_dir

    # 子进程执行 pipeline_runner.py
    runner_path = INAGENT_DIR / "pipeline_runner.py"
    if not runner_path.exists():
        raise HTTPException(500, f"pipeline_runner.py 不存在: {runner_path}")

    cmd = [
        sys.executable, "-u",
        str(runner_path),
        "--jobs-dir", str(jobs_dir_to_run),
        "--output-dir", str(REPORTS_DIR),
        "--log-dir", str(INAGENT_DIR / "knowledge_base" / "logs"),
    ]

    # 完成回调: 更新 DB + 清理临时目录
    def _on_complete(info: task_manager.TaskInfo):
        _finalize_test_run(run_id, info, job_files, temp_jobs_dir)

    info = await task_manager.start_task(
        task_type="test_run",
        description=f"测试流水线 ({len(job_files)} 个任务)",
        cmd=cmd,
        on_complete=_on_complete,
    )
    # 保存 run_id<->task_id 映射 (兼容前端现有逻辑)
    info.extra["run_id"] = run_id

    return {"run_id": info.task_id, "job_files": job_files, "status": "running"}


def _finalize_test_run(
    run_id: str,
    info: task_manager.TaskInfo,
    job_files: List[str],
    temp_dir=None,
):
    """任务结束后: 读取报告 → 更新 DB → 清理临时文件。"""
    db = get_db()
    log_text = "".join(info.log_lines)
    status = info.status  # success / failed / cancelled
    now = datetime.now().isoformat()

    for jf in job_files:
        rid = f"{run_id}_{jf}"
        stem = Path(jf).stem

        report_md = ""
        result_json = {}
        verdict = ""
        report_path = REPORTS_DIR / f"{stem}_test_report.md"
        result_path = REPORTS_DIR / f"{stem}_test_result.json"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8")
        if result_path.exists():
            try:
                result_json = json.loads(result_path.read_text(encoding="utf-8"))
                verdict = result_json.get("verdict", "")
            except Exception:
                pass

        db.execute(
            """UPDATE test_runs
               SET status=?, verdict=?, report_md=?, result_json=?, log_text=?, finished_at=?
               WHERE id=?""",
            (status, verdict, report_md, json.dumps(result_json, ensure_ascii=False),
             log_text[-50000:], now, rid),
        )
    db.commit()

    # 清理临时目录
    if temp_dir and temp_dir.exists():
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


# ── 实时日志 WebSocket (统一 task_manager) ──────────────────────────
@router.websocket("/ws/{task_id}")
async def ws_log(ws: WebSocket, task_id: str):
    """WebSocket 实时日志流 — 通过统一的 task_manager 管理。"""
    info = task_manager.get_task(task_id)
    if not info:
        await ws.close(code=4004, reason="任务不存在")
        return
    await ws.accept()
    await task_manager.subscribe_ws(task_id, ws)


# ── 取消测试 ────────────────────────────────────────────────────────
@router.post("/cancel/{task_id}")
async def cancel_run(task_id: str):
    ok = task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(404, "运行不存在或已结束")
    return {"run_id": task_id, "status": "cancelled"}
