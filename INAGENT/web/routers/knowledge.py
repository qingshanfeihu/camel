# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模块1: 产品功能学习 — 知识库管理 API

功能:
- 文档上传 (PDF) → MinerU 解析 → JSON 格式化 → 入库
- 知识库状态/统计
- 知识库 CRUD (create/update/rebuild/delete)
- 知识条目浏览/搜索/编辑
- GraphRAG 索引管理
"""
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket

from INAGENT.web.deps import (
    INAGENT_DIR,
    KB_DIR,
    KB_PATH,
    INDEX_PATH,
    REFERENCE_DIR,
    REPO_ROOT,
)
from INAGENT.web.models import (
    DBActionRequest,
    DBActionResponse,
    KBChunk,
    KBStats,
    TaskStatusResponse,
)
from INAGENT.web import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# 注意: manage_database.py 仅扫描 knowledge_base 根目录下的 *.pdf
# 因此上传文件需要落在 KB_DIR 根目录, 以保持最大兼容。
_PDF_DIR = KB_DIR
_GRAPHRAG_DIR = INAGENT_DIR / "graphrag_index"


# ── 知识库统计 ──────────────────────────────────────────────────────
@router.get("/stats", response_model=KBStats)
async def get_kb_stats():
    """获取知识库整体统计信息。"""
    stats = KBStats(
        kb_exists=KB_PATH.exists(),
        index_exists=INDEX_PATH.exists(),
        graphrag_ready=(_GRAPHRAG_DIR / "output").exists(),
    )
    if not KB_PATH.exists():
        return stats

    chunks = json.loads(KB_PATH.read_text(encoding="utf-8"))
    if isinstance(chunks, dict):
        chunks = chunks.get("chunks", chunks.get("data", []))
    if not isinstance(chunks, list):
        return stats

    stats.total_chunks = len(chunks)
    for c in chunks:
        meta = c if isinstance(c, dict) else {}
        pm = meta.get("product_module", meta.get("metadata", {}).get("product_module", "unknown"))
        pt = meta.get("protocol_type", meta.get("metadata", {}).get("protocol_type", "unknown"))
        # Normalise values that may be lists (e.g. ["HTTP","HTTPS"]) to strings
        if isinstance(pm, list):
            pm = ", ".join(str(v) for v in pm) if pm else "unknown"
        if isinstance(pt, list):
            pt = ", ".join(str(v) for v in pt) if pt else "unknown"
        stats.modules[pm] = stats.modules.get(pm, 0) + 1
        stats.protocols[pt] = stats.protocols.get(pt, 0) + 1
    return stats


# ── 知识条目浏览 ────────────────────────────────────────────────────
@router.get("/chunks", response_model=List[KBChunk])
async def list_chunks(
    offset: int = 0,
    limit: int = 50,
    module: str = "",
    protocol: str = "",
    search: str = "",
):
    """分页浏览/搜索知识库条目。"""
    if not KB_PATH.exists():
        return []

    chunks = json.loads(KB_PATH.read_text(encoding="utf-8"))
    if isinstance(chunks, dict):
        chunks = chunks.get("chunks", chunks.get("data", []))
    if not isinstance(chunks, list):
        return []

    results = []
    for i, c in enumerate(chunks):
        meta = c if isinstance(c, dict) else {}
        nested = meta.get("metadata", {})
        pm = meta.get("product_module", nested.get("product_module", ""))
        pt = meta.get("protocol_type", nested.get("protocol_type", ""))
        text = meta.get("text", meta.get("content", ""))
        st = meta.get("step_type", nested.get("step_type", ""))
        sec = meta.get("section_title", nested.get("section_title", ""))
        src = meta.get("source_file", nested.get("source_file", ""))

        # Normalise list values to comma-separated strings
        if isinstance(pm, list):
            pm = ", ".join(str(v) for v in pm) if pm else ""
        if isinstance(pt, list):
            pt = ", ".join(str(v) for v in pt) if pt else ""
        if isinstance(st, list):
            st = ", ".join(str(v) for v in st) if st else ""
        if isinstance(sec, list):
            sec = ", ".join(str(v) for v in sec) if sec else ""
        if isinstance(src, list):
            src = ", ".join(str(v) for v in src) if src else ""

        if module and pm != module:
            continue
        if protocol and pt != protocol:
            continue
        if search and search.lower() not in text.lower():
            continue

        results.append(KBChunk(
            id=str(i),
            text=text[:500],
            product_module=pm,
            protocol_type=pt,
            step_type=st,
            section_title=sec,
            source_file=src,
        ))

    return results[offset: offset + limit]


# ── 数据库操作 (create/update/rebuild/delete) ───────────────────────
#
# status / delete 仍然同步返回 (很快);
# create / update / rebuild 走后台任务 + WebSocket 日志流。

@router.post("/db", response_model=DBActionResponse)
async def db_action(req: DBActionRequest):
    """执行数据库管理操作。

    - status / delete: 同步返回结果。
    - create / update / rebuild: 启动后台任务, 立即返回 task_id,
      前端通过 ``/ws/{task_id}`` WebSocket 接收实时日志。
    """
    action = req.action
    logger.info("数据库操作: %s", action)

    # ── 轻量操作: 同步执行并直接返回 ──
    if action in ("status", "delete"):
        return _run_db_sync(action)

    # ── 重量操作: 后台执行 ──
    # 检查是否已有同类任务运行中
    running = task_manager.active_tasks("kb_build")
    if running:
        raise HTTPException(
            409,
            f"已有知识库构建任务运行中 (task_id: {running[0].task_id})，请等待完成或先取消",
        )

    info = await _start_kb_build(action)
    return DBActionResponse(
        action=action,
        success=True,
        message=f"后台任务已启动 (task_id={info.task_id})。请通过 WebSocket 查看进度。",
        details={"task_id": info.task_id},
    )


def _run_db_sync(action: str) -> DBActionResponse:
    """同步执行 status / delete 操作。"""
    cmd = [sys.executable, "-u", str(INAGENT_DIR / "manage_database.py"), "--action", action]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=120, cwd=str(REPO_ROOT), encoding="utf-8",
        )
        success = result.returncode == 0
        msg = result.stdout[-2000:] if result.stdout else ""
        if result.stderr:
            msg += "\n[STDERR]\n" + result.stderr[-1000:]

        # status 时追加 GraphRAG 状态
        if action == "status":
            gr = subprocess.run(
                [sys.executable, "-u",
                 str(INAGENT_DIR / "scripts" / "init_graphrag.py"), "--status"],
                capture_output=True, text=True,
                timeout=30, cwd=str(REPO_ROOT), encoding="utf-8",
            )
            if gr.stdout:
                msg += "\n" + gr.stdout[-1000:]

        return DBActionResponse(action=action, success=success, message=msg.strip())
    except subprocess.TimeoutExpired:
        return DBActionResponse(action=action, success=False, message="操作超时")
    except Exception as e:
        return DBActionResponse(action=action, success=False, message=str(e))


async def _start_kb_build(action: str) -> task_manager.TaskInfo:
    """启动 manage_database + GraphRAG 后台任务。

    用一个 wrapper 脚本串联两步, 这样日志流完整连贯。
    """
    # 构造一段 inline Python 来串联 manage_database → init_graphrag
    script = (
        "import subprocess, sys, os\n"
        f"action = {action!r}\n"
        f"repo = {str(REPO_ROOT)!r}\n"
        f"inagent = {str(INAGENT_DIR)!r}\n"
        "os.chdir(repo)\n"
        "print(f'[1/2] manage_database --action {action}')\n"
        "sys.stdout.flush()\n"
        "r1 = subprocess.run([sys.executable, '-u',\n"
        "    os.path.join(inagent, 'manage_database.py'),\n"
        "    '--action', action], cwd=repo)\n"
        "if r1.returncode != 0:\n"
        "    print(f'[ERROR] manage_database failed (exit {r1.returncode})')\n"
        "    sys.exit(r1.returncode)\n"
        "kb = os.path.join(inagent, 'knowledge_base', 'reference', 'knowledge_base.json')\n"
        "if action in ('create','update','rebuild') and os.path.exists(kb):\n"
        "    print()\n"
        "    print('[2/2] GraphRAG: init + build')\n"
        "    sys.stdout.flush()\n"
        "    r2 = subprocess.run([sys.executable, '-u',\n"
        "        os.path.join(inagent, 'scripts', 'init_graphrag.py'),\n"
        "        '--init', '--build'], cwd=repo)\n"
        "    sys.exit(r2.returncode)\n"
        "else:\n"
        "    print('[OK] No GraphRAG rebuild needed.')\n"
    )

    cmd = [sys.executable, "-u", "-c", script]
    info = await task_manager.start_task(
        task_type="kb_build",
        description=f"知识库 {action} + GraphRAG 构建",
        cmd=cmd,
    )
    logger.info("知识库后台任务已启动: task_id=%s action=%s", info.task_id, action)
    return info


# ── 任务状态 / WebSocket / 取消 ─────────────────────────────────────
@router.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_kb_task_status(task_id: str):
    """查询知识库构建任务状态 (polling 方式)。"""
    info = task_manager.get_task(task_id)
    if not info:
        raise HTTPException(404, "任务不存在")
    log_tail = "".join(info.log_lines[-50:])
    return TaskStatusResponse(
        task_id=info.task_id,
        task_type=info.task_type,
        description=info.description,
        status=info.status,
        created_at=info.created_at,
        finished_at=info.finished_at,
        exit_code=info.exit_code,
        log_tail=log_tail[-3000:],
    )


@router.get("/tasks")
async def list_kb_tasks():
    """列出所有知识库相关的后台任务 (含历史)。"""
    items = [
        {
            "task_id": t.task_id,
            "status": t.status,
            "description": t.description,
            "created_at": t.created_at,
            "finished_at": t.finished_at,
        }
        for t in task_manager._tasks.values()
        if t.task_type == "kb_build"
    ]
    return items


@router.websocket("/ws/{task_id}")
async def ws_kb_task_log(ws: WebSocket, task_id: str):
    """WebSocket 实时日志流 — 与 testing.py 的 /ws/{run_id} 统一模式。"""
    info = task_manager.get_task(task_id)
    if not info:
        await ws.close(code=4004, reason="任务不存在")
        return
    await ws.accept()
    await task_manager.subscribe_ws(task_id, ws)


@router.post("/cancel/{task_id}")
async def cancel_kb_task(task_id: str):
    """取消正在运行的知识库构建任务。"""
    ok = task_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(404, "任务不存在或已结束")
    return {"task_id": task_id, "status": "cancelled"}


# ── 文档上传 ────────────────────────────────────────────────────────
@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    """上传 PDF 文档到知识库输入目录, 待后续 create/update 处理。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    dest = _PDF_DIR / file.filename
    content = await file.read()
    dest.write_bytes(content)
    logger.info("文档已上传: %s (%d bytes)", file.filename, len(content))
    return {"filename": file.filename, "size": len(content), "path": str(dest)}


# ── 已上传文档列表 ──────────────────────────────────────────────────
@router.get("/documents")
async def list_documents():
    """列出已上传到知识库输入目录的文档。"""
    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    docs = []
    for p in sorted(_PDF_DIR.glob("*.pdf")):
        docs.append({
            "name": p.name,
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
        })
    return docs


@router.delete("/documents/{filename}")
async def delete_document(filename: str):
    """删除已上传的文档。"""
    target = (_PDF_DIR / filename).resolve()
    if not str(target).startswith(str(_PDF_DIR.resolve())):
        raise HTTPException(400, "无效文件名")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    target.unlink()
    return {"deleted": filename}
