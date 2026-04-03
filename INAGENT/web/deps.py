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

from INAGENT.config.project_config import cfg_bool, cfg_str
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
        # graphrag_retriever 保存在模块级别供 get_unified_rag 使用
        global _graphrag_retriever
        _graphrag_retriever = result[2] if len(result) > 2 else None
        logger.info("RAG 系统已初始化")
    return _hybrid_retriever, _reranker


_graphrag_retriever: Any = None
_unified_rag: Any = None
_neo4j_store: Any = None
_entity_link_store: Any = None
_hybrid_fusion: Any = None


def get_unified_rag():
    """获取 UnifiedRAGRetriever 单例。"""
    global _unified_rag
    if _unified_rag is None:
        from INAGENT.rag.unified_rag import UnifiedRAGRetriever
        hybrid_retriever, reranker = get_rag()
        _unified_rag = UnifiedRAGRetriever(
            hybrid_retriever=hybrid_retriever,
            reranker=reranker,
            graphrag_retriever=_graphrag_retriever,
        )
        logger.info("UnifiedRAGRetriever 已初始化")
    return _unified_rag


def get_neo4j_store():
    """获取 Neo4jStore 单例（未配置时自动禁用）。"""
    global _neo4j_store
    if _neo4j_store is None:
        from INAGENT.rag.neo4j_store import Neo4jStore
        enabled = cfg_bool(
            "storage.neo4j.enabled",
            False,
            env="INAGENT_ENABLE_NEO4J",
        )
        _neo4j_store = Neo4jStore(
            uri=cfg_str("storage.neo4j.uri", "bolt://127.0.0.1:7687", env="NEO4J_URI"),
            username=cfg_str("storage.neo4j.username", "neo4j", env="NEO4J_USERNAME"),
            password=cfg_str("storage.neo4j.password", "neo4j", env="NEO4J_PASSWORD"),
            database=cfg_str("storage.neo4j.database", "neo4j", env="NEO4J_DATABASE"),
            enabled=enabled,
        )
    return _neo4j_store


def get_entity_link_store():
    """获取跨存储实体对齐索引单例。（已迁移到 SkeletonIndex，保留兼容）"""
    global _entity_link_store
    if _entity_link_store is None:
        from INAGENT.rag.entity_link_store import EntityLinkStore
        db_path = INAGENT_DIR / "vector_store" / "entity_link.db"
        _entity_link_store = EntityLinkStore(db_path=db_path)
    return _entity_link_store


def get_skeleton_index():
    """获取 SkeletonIndex 单例。"""
    from INAGENT.rag.skeleton_index import get_skeleton_index as _get_si
    return _get_si()


def get_hybrid_fusion():
    """获取 HybridKnowledgeFusion 单例。"""
    global _hybrid_fusion
    if _hybrid_fusion is None:
        from INAGENT.rag.hybrid_knowledge_fusion import HybridKnowledgeFusion
        _hybrid_fusion = HybridKnowledgeFusion(
            unified_rag=get_unified_rag(),
            neo4j_store=get_neo4j_store(),
            skeleton_index=get_skeleton_index(),
        )
    return _hybrid_fusion


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
            unified_rag=get_unified_rag(),
            rules_engine=get_rules_engine(),
            hybrid_fusion=get_hybrid_fusion(),
            hybrid_retriever=hybrid_retriever,
            reranker=reranker,
        )
        logger.info("知识路由器已初始化")
    return _knowledge_router


# ── 生命周期 ────────────────────────────────────────────────────────

def check_rag_health(*, require_graphrag: bool = True) -> Dict[str, Any]:
    """检查 RAG 系统健康状态。关键服务不可用时抛出 RuntimeError。

    检查项:
    1. Qdrant 向量库: collection 存在, point_count > 0
    2. GraphRAG 索引: entities.parquet 存在且 row_count > 0
    3. LLM Gateway: POST /v1/embeddings smoke test
    4. Reranker: POST /v1/rerank smoke test

    Returns: {"qdrant_ok": bool, "graphrag_ok": bool, "gateway_ok": bool, "reranker_ok": bool}
    Raises: RuntimeError 如果任一项失败
    """
    import urllib.request
    import urllib.error

    errors = []
    result: Dict[str, Any] = {}

    # ── 1. Qdrant 向量库 ──────────────────────────────────────────
    try:
        hybrid_retriever, _ = get_rag()
        # HybridRetriever stores vector storage at .vr.storage
        vr = getattr(hybrid_retriever, "vr", None)
        storage = getattr(vr, "storage", None) if vr else None
        if storage is None:
            storage = getattr(hybrid_retriever, "_vector_storage", None)
        if storage is None:
            storage = getattr(hybrid_retriever, "vector_storage", None)
        if storage is not None:
            client = getattr(storage, "_client", None)
            coll_name = getattr(storage, "collection_name", "workflow_rag")
            if client is not None:
                info = client.get_collection(coll_name)
                pts = getattr(info, "points_count", 0)
                result["qdrant_points"] = pts
                if pts > 0:
                    result["qdrant_ok"] = True
                    logger.info("[健康检查] Qdrant OK: collection=%s, points=%d", coll_name, pts)
                else:
                    result["qdrant_ok"] = False
                    errors.append(f"Qdrant collection '{coll_name}' 为空 (points=0)")
            else:
                result["qdrant_ok"] = False
                errors.append("Qdrant client 未初始化")
        else:
            result["qdrant_ok"] = False
            errors.append("HybridRetriever 无 vector_storage 属性")
    except Exception as e:
        result["qdrant_ok"] = False
        errors.append(f"Qdrant 检查失败: {e}")

    # ── 2. GraphRAG 索引 ──────────────────────────────────────────
    graphrag_index_dir = INAGENT_DIR / "graphrag_index" / "output"
    entities_path = graphrag_index_dir / "entities.parquet"
    if require_graphrag:
        try:
            if not entities_path.exists():
                result["graphrag_ok"] = False
                errors.append(
                    f"GraphRAG 索引未构建: {entities_path} 不存在。"
                    "请运行: python INAGENT/scripts/init_graphrag.py --init --build"
                )
            else:
                import pandas as pd
                df = pd.read_parquet(entities_path)
                entity_count = len(df)
                result["graphrag_entity_count"] = entity_count
                if entity_count > 0:
                    result["graphrag_ok"] = True
                    logger.info("[健康检查] GraphRAG OK: %d 实体", entity_count)
                else:
                    result["graphrag_ok"] = False
                    errors.append("GraphRAG entities.parquet 为空 (0 实体)")
        except Exception as e:
            result["graphrag_ok"] = False
            errors.append(f"GraphRAG 检查失败: {e}")
    else:
        result["graphrag_ok"] = True  # 不要求时跳过

    # ── 3. LLM Gateway (embedding smoke test) ─────────────────────
    try:
        from INAGENT.utils.llm_config import get_gateway_config
        gw = get_gateway_config()
        base_url = (gw.get("base_url") or "").rstrip("/")
        api_key = gw.get("api_key", "")
        if not base_url:
            result["gateway_ok"] = False
            errors.append("LLM Gateway base_url 未配置")
        else:
            import json as _json
            # base_url may already end with /v1
            embed_url = f"{base_url}/embeddings" if base_url.endswith("/v1") else f"{base_url}/v1/embeddings"
            payload = _json.dumps({
                "model": gw.get("embedding_model", "text-embedding-v4"),
                "input": ["health check"],
            }).encode("utf-8")
            req = urllib.request.Request(
                embed_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    result["gateway_ok"] = True
                    logger.info("[健康检查] Gateway OK: %s", base_url)
                else:
                    result["gateway_ok"] = False
                    errors.append(f"Gateway /v1/embeddings 返回 {resp.status}")
    except urllib.error.URLError as e:
        result["gateway_ok"] = False
        errors.append(f"Gateway 不可达: {e}")
    except Exception as e:
        result["gateway_ok"] = False
        errors.append(f"Gateway 检查失败: {e}")

    # ── 4. Reranker ─────────────────────────────────────────────
    try:
        from INAGENT.utils.llm_config import get_gateway_config as _gw
        gw = _gw()
        base_url = (gw.get("base_url") or "").rstrip("/")
        api_key = gw.get("api_key", "")
        import json as _json
        payload = _json.dumps({
            "model": gw.get("rerank_model", "gte-rerank-v2"),
            "query": "health",
            "documents": ["check"],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/rerank" if base_url.endswith("/v1") else f"{base_url}/v1/rerank",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result["reranker_ok"] = resp.status == 200
        if result["reranker_ok"]:
            logger.info("[健康检查] Reranker OK")
        else:
            result["reranker_ok"] = False
            errors.append(f"Reranker 返回 {resp.status}")
    except Exception as e:
        result["reranker_ok"] = False
        errors.append(f"Reranker 不可用: {e}")

    # ── 汇总 ──────────────────────────────────────────────────────
    if errors:
        msg = "RAG 健康检查失败:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.error(msg)
        raise RuntimeError(msg)

    return result


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
