# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模块4: 测试结果展示 — 历史测试报告 API

功能:
- 测试历史列表 (分页、筛选)
- 报告详情 (Markdown 渲染)
- 结果 JSON 详情
- 统计汇总 (PASS/FAIL 分布)
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

from INAGENT.web.deps import get_db, REPORTS_DIR
from INAGENT.web.models import TestRunDetail, TestRunStatus

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/runs", response_model=List[TestRunStatus])
async def list_runs(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """获取历史测试运行列表。"""
    db = get_db()
    query = "SELECT id, job_file, status, verdict, created_at, finished_at FROM test_runs"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = db.execute(query, params).fetchall()
    return [TestRunStatus(
        id=r["id"], job_file=r["job_file"], status=r["status"],
        verdict=r["verdict"] or "", created_at=r["created_at"],
        finished_at=r["finished_at"],
    ) for r in rows]


@router.get("/runs/{run_id}", response_model=TestRunDetail)
async def get_run_detail(run_id: str):
    """获取测试运行详情 (含报告 / 结果 JSON / 日志)。"""
    db = get_db()
    r = db.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "测试运行不存在")

    result_json = {}
    if r["result_json"]:
        try:
            result_json = json.loads(r["result_json"])
        except Exception:
            pass

    return TestRunDetail(
        id=r["id"], job_file=r["job_file"], job_content=r["job_content"],
        status=r["status"], verdict=r["verdict"] or "",
        report_md=r["report_md"] or "", result_json=result_json,
        log_text=r["log_text"] or "", created_at=r["created_at"],
        finished_at=r["finished_at"],
    )


@router.get("/summary")
async def get_summary():
    """获取测试结果统计汇总。"""
    db = get_db()

    total = db.execute("SELECT COUNT(*) as c FROM test_runs").fetchone()["c"]
    by_status = {}
    for r in db.execute("SELECT status, COUNT(*) as c FROM test_runs GROUP BY status").fetchall():
        by_status[r["status"]] = r["c"]
    by_verdict = {}
    for r in db.execute(
        "SELECT verdict, COUNT(*) as c FROM test_runs WHERE verdict != '' GROUP BY verdict"
    ).fetchall():
        by_verdict[r["verdict"]] = r["c"]

    # 最近 10 条
    recent = db.execute(
        "SELECT id, job_file, status, verdict, created_at FROM test_runs ORDER BY created_at DESC LIMIT 10"
    ).fetchall()

    return {
        "total": total,
        "by_status": by_status,
        "by_verdict": by_verdict,
        "recent": [dict(r) for r in recent],
    }


@router.get("/files")
async def list_report_files():
    """列出报告目录中的所有文件 (MD 报告和 JSON 结果)。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(REPORTS_DIR.iterdir()):
        if p.is_file() and p.suffix in (".md", ".json"):
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "type": "report" if p.suffix == ".md" else "result",
                "modified": p.stat().st_mtime,
            })
    return files


@router.get("/files/{filename}")
async def get_report_file(filename: str):
    """获取报告文件内容。"""
    p = (REPORTS_DIR / filename).resolve()
    if not str(p).startswith(str(REPORTS_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "文件不存在")
    content = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        try:
            return json.loads(content)
        except Exception:
            pass
    return {"filename": filename, "content": content}
