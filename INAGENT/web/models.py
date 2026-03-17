# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Pydantic 数据模型 — 请求/响应 schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── 知识库 (Module 1) ────────────────────────────────────────────────
class KBStats(BaseModel):
    total_chunks: int = 0
    modules: Dict[str, int] = {}
    protocols: Dict[str, int] = {}
    kb_exists: bool = False
    index_exists: bool = False
    graphrag_ready: bool = False


class KBChunk(BaseModel):
    id: str = ""
    text: str = ""
    product_module: str = ""
    protocol_type: str = ""
    step_type: str = ""
    section_title: str = ""
    source_file: str = ""


class DBActionRequest(BaseModel):
    action: str = Field(..., pattern="^(status|create|update|rebuild|delete)$")


class DBActionResponse(BaseModel):
    action: str
    success: bool
    message: str
    details: Dict[str, Any] = {}


class TaskStatusResponse(BaseModel):
    """统一的后台任务状态 (知识库构建 / 测试执行 等)。"""
    task_id: str
    task_type: str
    description: str
    status: str              # running / success / failed / cancelled
    created_at: str
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_tail: str = ""       # 最近的日志片段 (polling 场景)


# ── 问答 (Module 2) ─────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    mode: str = Field("explain", pattern="^(explain|config|test_write|test_review)$")


class ChatMessage(BaseModel):
    role: str
    content: str
    sources: List[str] = []
    created_at: str = ""


class ChatSession(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class ChatResponse(BaseModel):
    session_id: str
    message: ChatMessage
    config_commands: List[str] = []
    verify_commands: List[str] = []


# ── 测试 (Module 3) ──────────────────────────────────────────────────
class JobFile(BaseModel):
    name: str
    content: str
    size: int = 0


class JobCreateRequest(BaseModel):
    name: str
    content: str


class TestRunRequest(BaseModel):
    job_files: List[str] = Field(default_factory=list, description="要执行的 job 文件名列表, 空则执行全部")


class TestRunStatus(BaseModel):
    id: str
    job_file: str
    status: str
    verdict: str = ""
    created_at: str
    finished_at: Optional[str] = None


class TestRunDetail(BaseModel):
    id: str
    job_file: str
    job_content: str
    status: str
    verdict: str = ""
    report_md: str = ""
    result_json: Dict[str, Any] = {}
    log_text: str = ""
    created_at: str
    finished_at: Optional[str] = None


# ── 系统 (Module 5) ──────────────────────────────────────────────────
class SystemStatus(BaseModel):
    llm_gateway: bool = False
    nsae_device: bool = False
    test_vm: bool = False
    rag_ready: bool = False
    graphrag_ready: bool = False
    kb_exists: bool = False


class EnvConfig(BaseModel):
    """可在 Web 上编辑的环境变量子集。"""
    llm_gateway_url: str = ""
    llm_chat_model: str = ""
    lb_device_ip: str = ""
    lb_username: str = ""
    vm_mgmt_ip: str = ""
    vm_username: str = ""
