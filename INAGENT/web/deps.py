# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
共享依赖 — 全局单例管理 (RAG, LLM model, DB)

所有路由通过 ``get_*()`` 函数获取已初始化的共享资源,
避免每次请求重复建连/初始化。
"""
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from INAGENT.utils import env_utils

logger = logging.getLogger(__name__)

# ── 路径常量 ────────────────────────────────────────────────────────
INAGENT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = INAGENT_DIR.parent
KB_DIR = INAGENT_DIR / "knowledge_base"
REFERENCE_DIR = KB_DIR / "reference"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
INDEX_PATH = KB_DIR / "function_structure_index.json"
JOBS_DIR = INAGENT_DIR / "jobs"
REPORTS_DIR = INAGENT_DIR / "reports"
LOG_DIR = KB_DIR / "logs"
DB_PATH = INAGENT_DIR / "web" / "inagent.db"

# ── 全局单例 ────────────────────────────────────────────────────────
_llm_model: Any = None
_hybrid_retriever: Any = None
_reranker: Any = None
_function_index: Dict[str, Any] = {}
_db_conn: Optional[sqlite3.Connection] = None


# ── SQLite ──────────────────────────────────────────────────────────
def _init_sqlite():
    """初始化轻量级 SQLite 数据库, 用于存储会话历史/系统配置等。"""
    global _db_conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    _db_conn.execute("PRAGMA journal_mode=WAL")

    _db_conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES chat_sessions(id),
            role        TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content     TEXT NOT NULL,
            sources     TEXT DEFAULT '[]',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS test_runs (
            id          TEXT PRIMARY KEY,
            job_file    TEXT NOT NULL,
            job_content TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','success','failed','cancelled')),
            verdict     TEXT DEFAULT '',
            report_md   TEXT DEFAULT '',
            result_json TEXT DEFAULT '{}',
            log_text    TEXT DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS system_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
    """)
    _db_conn.commit()
    logger.info("SQLite 初始化完成: %s", DB_PATH)


def get_db() -> sqlite3.Connection:
    if _db_conn is None:
        _init_sqlite()
    return _db_conn


# ── LLM / RAG 单例 ──────────────────────────────────────────────────
def get_llm_model():
    global _llm_model
    if _llm_model is None:
        from INAGENT.workflow_config_generator import initialize_llm_model
        _llm_model = initialize_llm_model()
        logger.info("LLM 模型已初始化")
    return _llm_model


def get_rag():
    """返回 (hybrid_retriever, reranker)。首次调用时初始化。"""
    global _hybrid_retriever, _reranker
    if _hybrid_retriever is None:
        from INAGENT.workflow_config_generator import initialize_rag_system
        result = initialize_rag_system()
        _hybrid_retriever = result[0]
        _reranker = result[1]
        logger.info("RAG 系统已初始化")
    return _hybrid_retriever, _reranker


def get_function_index() -> Dict[str, Any]:
    global _function_index
    if not _function_index and INDEX_PATH.exists():
        from INAGENT.utils.index_utils import load_function_structure_index
        _function_index = load_function_structure_index(INDEX_PATH)
        logger.info("功能结构索引已加载: %d 场景", len(_function_index.get("scenarios", {})))
    return _function_index


# ── CLI 检索 / Rules 引擎 / 知识路由器 ──────────────────────────────
_cli_retriever: Any = None
_rules_engine: Any = None
_knowledge_router: Any = None


def get_cli_retriever():
    """获取 CLI 参考检索器单例。"""
    global _cli_retriever
    if _cli_retriever is None:
        from INAGENT.rag.cli_reference import CLIReferenceRetriever
        _cli_retriever = CLIReferenceRetriever()
        logger.info("CLI 检索器已初始化: %d 条目", _cli_retriever.entry_count)
    return _cli_retriever


def get_rules_engine():
    """获取测试规则引擎单例。"""
    global _rules_engine
    if _rules_engine is None:
        from INAGENT.rag.test_rules import TestRulesEngine
        _rules_engine = TestRulesEngine()
        logger.info("测试规则引擎已初始化: %d 已有测试项", _rules_engine.test_item_count)
    return _rules_engine


def get_knowledge_router():
    """获取知识路由器单例。"""
    global _knowledge_router
    if _knowledge_router is None:
        from INAGENT.rag.knowledge_router import KnowledgeRouter
        hybrid_retriever, reranker = get_rag()
        _knowledge_router = KnowledgeRouter(
            cli_retriever=get_cli_retriever(),
            rules_engine=get_rules_engine(),
            hybrid_retriever=hybrid_retriever,
            reranker=reranker,
        )
        logger.info("知识路由器已初始化")
    return _knowledge_router


# ── 生命周期 ────────────────────────────────────────────────────────
async def startup_deps():
    """应用启动时的初始化 — 仅初始化轻量级资源, RAG/LLM 按需懒加载。"""
    env_utils.load_inagent_env()
    _init_sqlite()

    # 确保关键目录存在
    for d in (JOBS_DIR, REPORTS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


async def shutdown_deps():
    """应用关闭时释放资源。"""
    global _db_conn
    if _db_conn:
        _db_conn.close()
        _db_conn = None
