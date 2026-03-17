# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
INAGENT Web Application — FastAPI 主入口

模块结构:
  web/
  ├── app.py              ← 本文件: FastAPI 应用入口
  ├── deps.py             ← 共享依赖 (RAG, LLM model, SSH 等单例)
  ├── models.py           ← Pydantic 数据模型
  ├── routers/
  │   ├── knowledge.py    ← 模块1: 产品功能学习 (知识库管理)
  │   ├── chat.py         ← 模块2: 产品知识展示 (RAG 问答)
  │   ├── testing.py      ← 模块3: 产品测试功能 (Pipeline 执行)
  │   ├── reports.py      ← 模块4: 测试结果展示
  │   └── system.py       ← 模块5: 系统管理
  └── static/             ← 前端静态文件 (Vue 3 SPA)
"""
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 确保项目路径可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from INAGENT.web.deps import startup_deps, shutdown_deps
from INAGENT.web.routers import knowledge, chat, testing, reports, system

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时初始化共享资源, 关闭时释放。"""
    logger.info("INAGENT Web 启动中...")
    await startup_deps()
    logger.info("INAGENT Web 就绪 ✓")
    yield
    logger.info("INAGENT Web 关闭中...")
    await shutdown_deps()


app = FastAPI(
    title="INAGENT — 智能网络设备自动化测试平台",
    version="0.5.0",
    lifespan=lifespan,
)

# CORS — 开发阶段允许本地前端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ────────────────────────────────────────────────────────
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["知识库管理"])
app.include_router(chat.router, prefix="/api/chat", tags=["知识问答"])
app.include_router(testing.router, prefix="/api/testing", tags=["测试执行"])
app.include_router(reports.router, prefix="/api/reports", tags=["测试结果"])
app.include_router(system.router, prefix="/api/system", tags=["系统管理"])

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.5.0"}


# ── favicon ──────────────────────────────────────────────────────────
from fastapi.responses import Response          # noqa: E402

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#2563eb"/>'
    '<text x="16" y="24" font-size="22" font-family="Arial,sans-serif" '
    'fill="#fff" text-anchor="middle">I</text></svg>'
)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ── 前端静态文件 ─────────────────────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="frontend")
