# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模块2: 产品知识展示 — RAG + Agent 问答 API

功能:
- 多轮对话 (会话管理)
- 四种模式:
  - explain: 功能解释 — 流式输出
  - config: 配置生成
  - test_write: 测试用例编写（Rules + CLI + RAG + 已有测试参考）
  - test_review: 测试用例评审（Rules + 已有测试参考）
- 检索来源溯源
- 与 CAMEL ChatAgent + Workflow 最大兼容
"""
import asyncio
import json
import logging
import re
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.models import ModelFactory
from camel.types import ModelPlatformType

from INAGENT.utils import env_utils
from INAGENT.web.deps import (
    get_db, get_llm_model, get_rag, get_function_index,
    get_knowledge_router,
)
from INAGENT.web.models import ChatMessage, ChatRequest, ChatResponse, ChatSession

logger = logging.getLogger(__name__)
router = APIRouter()
_PRODUCT_NAME = env_utils.get_product_name()


# ── 会话管理 ────────────────────────────────────────────────────────
@router.get("/sessions", response_model=List[ChatSession])
async def list_sessions():
    """获取所有聊天会话列表。"""
    db = get_db()
    rows = db.execute(
        """SELECT s.*, COUNT(m.id) as message_count
           FROM chat_sessions s
           LEFT JOIN chat_messages m ON m.session_id = s.id
           GROUP BY s.id
           ORDER BY s.updated_at DESC"""
    ).fetchall()
    return [ChatSession(
        id=r["id"], title=r["title"],
        created_at=r["created_at"], updated_at=r["updated_at"],
        message_count=r["message_count"],
    ) for r in rows]


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    db = get_db()
    db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    db.commit()
    return {"deleted": session_id}


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessage])
async def get_messages(session_id: str):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at",
        (session_id,)
    ).fetchall()
    return [ChatMessage(
        role=r["role"], content=r["content"],
        sources=json.loads(r["sources"]) if r["sources"] else [],
        created_at=r["created_at"],
    ) for r in rows]


# ── 核心问答 (非流式 — 保留给 config 模式) ──────────────────────────
@router.post("/ask", response_model=ChatResponse)
async def ask(req: ChatRequest):
    """
    统一问答入口 (非流式):
    - mode=explain: RAG 检索 + Agent 功能解释
    - mode=config:  RAG + Task Decomposition + LB Ops Agent 配置生成
    - mode=test_write: Rules + CLI + RAG + Agent 测试用例编写
    - mode=test_review: Rules + 已有测试 + Agent 测试用例评审
    """
    db = get_db()
    session_id = req.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        title = req.message[:40] + ("..." if len(req.message) > 40 else "")
        db.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?)",
            (session_id, title),
        )
        db.commit()

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, "user", req.message),
    )
    db.commit()

    model = get_llm_model()

    config_commands: list = []
    verify_commands: list = []
    sources: list = []

    try:
        if req.mode == "config":
            hybrid_retriever, reranker = get_rag()
            answer, config_commands, verify_commands, sources = _generate_config(
                req.message, hybrid_retriever, reranker, model
            )
        elif req.mode == "test_write":
            answer, sources = _generate_test_cases(req.message, model)
        elif req.mode == "test_review":
            answer, sources = _review_test_cases(req.message, model)
        else:
            answer, sources = _generate_explanation(
                req.message, None, None, model
            )
    except Exception as e:
        logger.error("问答失败: %s", e, exc_info=True)
        answer = f"抱歉，处理失败: {e}"

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
        (session_id, "assistant", answer, json.dumps(sources, ensure_ascii=False)),
    )
    db.execute(
        "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.commit()

    return ChatResponse(
        session_id=session_id,
        message=ChatMessage(role="assistant", content=answer, sources=sources),
        config_commands=config_commands,
        verify_commands=verify_commands,
    )


# ── 流式问答 (SSE) ─────────────────────────────────────────────────
@router.post("/ask_stream")
async def ask_stream(req: ChatRequest):
    """流式问答 (Server-Sent Events)，支持思考过程。支持所有四种模式。"""
    db = get_db()
    session_id = req.session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        title = req.message[:40] + ("..." if len(req.message) > 40 else "")
        db.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?)",
            (session_id, title),
        )
        db.commit()

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?, ?, ?)",
        (session_id, "user", req.message),
    )
    db.commit()

    def _sse_event(event: str, data: str) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _sse_json(event: str, obj: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"

    async def event_generator():
        try:
            yield _sse_json("session", {"session_id": session_id})

            mode = req.mode or "explain"

            # ── test_review: 走 ReviewPipeline（非流式三步自主评审）──
            if mode == "test_review":
                yield _sse_event("think", "启动自主评审 Pipeline (plan → knowledge → review)...")
                await asyncio.sleep(0)

                from INAGENT.review import ReviewPipeline
                kr = get_knowledge_router()
                model = get_llm_model()
                pipeline = ReviewPipeline(
                    router=kr, model=model, product_name=_PRODUCT_NAME,
                )
                result = pipeline.run(test_cases_text=req.message)
                full_answer = _postprocess_answer(result.review)

                yield _sse_event("delta", full_answer)
                await asyncio.sleep(0)

                db.execute(
                    "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
                    (session_id, "assistant", full_answer, "[]"),
                )
                db.execute(
                    "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
                    (session_id,),
                )
                db.commit()
                yield _sse_json("done", {"answer_length": len(full_answer)})
                return

            # ── 思考阶段: 分层检索 ──
            if mode == "test_write":
                yield _sse_event("think", f"正在进行分层知识检索 (模式: {mode})...")
            else:
                yield _sse_event("think", "正在检索相关文档...")
            await asyncio.sleep(0)

            # 按模式选择检索策略
            snippets = []
            context = ""
            system_prompt = _EXPLAIN_SYSTEM_PROMPT
            role_name = "技术文档专家"

            if mode == "test_write":
                # 使用知识路由器进行分层检索
                kr = get_knowledge_router()
                retrieval = kr.retrieve(req.message, mode=mode, max_context_chars=20000)
                context = retrieval.get("context", "")
                layers = retrieval.get("layers_used", [])
                yield _sse_event("think", f"已查询知识层: {', '.join(layers)}")

                system_prompt = _TEST_WRITE_SYSTEM_PROMPT
                role_name = "测试用例编写专家"
            else:
                # 统一走知识路由器
                kr = get_knowledge_router()
                retrieval = kr.retrieve(req.message, mode="explain", max_context_chars=10000)
                context = retrieval.get("context", "")

            cleaned_ctx = _clean_context(context) if context else ""
            if not snippets and cleaned_ctx:
                snippets = [s[:500] for s in cleaned_ctx.split("\n\n") if s.strip()][:5]

            n_docs = len(snippets) if snippets else (1 if cleaned_ctx else 0)
            if mode == "test_write":
                yield _sse_event("think", "检索完成，正在生成测试用例...")
            else:
                quality = _assess_context_quality(req.message, cleaned_ctx)
                if quality and "警告" in quality:
                    yield _sse_event("think", f"已检索到 {n_docs} 条文档片段（相关度较低）")
                else:
                    yield _sse_event("think", f"已检索到 {n_docs} 条相关文档片段")
                yield _sse_event("think", "正在生成回答...")
            await asyncio.sleep(0)

            # ── 流式生成 ──
            stream_model = _get_stream_model()
            agent = ChatAgent(
                system_message=BaseMessage.make_assistant_message(
                    role_name=role_name,
                    content=system_prompt,
                ),
                model=stream_model,
            )

            if mode == "test_write":
                prompt = _build_test_prompt(req.message, context, mode)
            else:
                prompt = _build_explain_prompt(req.message, context)
            response = agent.step(prompt)

            full_answer = ""
            for chunk in response:
                if chunk.msgs:
                    text = chunk.msgs[0].content or ""
                    delta = text[len(full_answer):]
                    if delta:
                        full_answer = text
                        yield _sse_event("delta", delta)
                        await asyncio.sleep(0)

            if not full_answer:
                full_answer = response.msgs[0].content if response.msgs else "无法生成回答"

            # 后处理: 清理不专业的表述
            full_answer = _postprocess_answer(full_answer)

            # 保存到数据库
            db.execute(
                "INSERT INTO chat_messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
                (session_id, "assistant", full_answer,
                 json.dumps(snippets[:5], ensure_ascii=False) if snippets else "[]"),
            )
            db.execute(
                "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
                (session_id,),
            )
            db.commit()

            yield _sse_json("done", {"answer_length": len(full_answer)})

        except Exception as e:
            logger.error("流式问答失败: %s", e, exc_info=True)
            yield _sse_event("error", f"处理失败: {e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Prompt 常量 ─────────────────────────────────────────────────────
_EXPLAIN_SYSTEM_PROMPT = (
    f"你是 {_PRODUCT_NAME} 的技术文档专家。\n"
    "职责: 严格基于检索到的文档内容回答用户的技术问题。\n\n"
    "核心规则（必须遵守）:\n"
    "1. 只使用[相关文档内容]中明确出现的信息来回答\n"
    "2. 绝对不要编造或推测文档中没有的命令、参数、配置语法或功能描述\n"
    "3. 如果文档内容与用户问题不相关或信息不足，必须明确告知用户:\n"
    "   - 说明当前知识库未覆盖该主题\n"
    "   - 指出问题涉及哪个功能领域\n"
    "   - 建议用户查阅具体的产品手册章节或联系技术支持\n"
    "4. 不要凭通用网络知识补充文档中没有的内容\n\n"
    "输出规范:\n"
    "- 使用中文，正式技术文档风格\n"
    "- 直接给出答案，不要使用'根据您提供的文档内容'等引导语\n"
    "- 不使用 emoji 或表情符号\n"
    "- 使用 Markdown 标题、列表、代码块来组织内容\n"
    "- 涉及命令或配置时使用 ``` 代码块，且命令必须来自文档原文"
)

# 匹配 INAGENT_META_JSON 行
_META_RE = re.compile(r'^INAGENT_META_JSON:\{.*?\}\s*', re.MULTILINE)


def _clean_context(context: str) -> str:
    """清理 context 中的元数据行，只保留有意义的文档内容。"""
    if not context:
        return ""
    cleaned = _META_RE.sub('', context)
    # 合并多余空行
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def _assess_context_quality(query: str, context: str) -> str:
    """评估检索到的上下文与查询的相关度，返回质量提示。"""
    if not context or len(context.strip()) < 30:
        return (
            f"[警告: 未检索到相关文档内容。"
            f"请仅基于你对{_PRODUCT_NAME}的了解回答，并明确标注信息来源不足。]"
        )

    # 提取查询关键词
    query_lower = query.lower()
    key_terms = [w for w in re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', query_lower)]
    if not key_terms:
        return ""

    ctx_lower = context.lower()
    matched = sum(1 for t in key_terms if t in ctx_lower)
    ratio = matched / len(key_terms) if key_terms else 0

    if ratio < 0.2:
        return (
            f"[警告: 检索到的文档内容与用户问题相关度很低 "
            f"(关键词匹配 {matched}/{len(key_terms)})。"
            f"请如实告知用户当前知识库未覆盖该主题，不要编造答案。]"
        )
    elif ratio < 0.5:
        return (
            f"[提示: 检索到的文档仅部分覆盖用户问题 "
            f"(关键词匹配 {matched}/{len(key_terms)})。"
            f"请只回答文档中有据可查的部分，不确定的内容请明确说明。]"
        )
    return ""


def _build_explain_prompt(query: str, context: str) -> str:
    cleaned = _clean_context(context)
    quality_hint = _assess_context_quality(query, cleaned)

    parts = [f"[用户问题]\n{query}"]
    if quality_hint:
        parts.append(quality_hint)
    parts.append(f"[相关文档内容]\n{cleaned if cleaned else '（无相关文档）'}")
    parts.append("请严格基于上述文档内容回答。如果文档不包含相关信息，请如实说明。")
    return "\n\n".join(parts)


def _build_test_prompt(query: str, context: str, mode: str) -> str:
    """构建测试用例编写的 prompt（test_review 已走 ReviewPipeline，不再经此函数）。"""
    cleaned = _clean_context(context) if context else ""
    parts = [f"[测试需求]\n{query}"]
    if cleaned:
        parts.append(cleaned)
    parts.append(
        "请基于以上信息，为该功能编写完整的测试用例集。"
        "确保覆盖 Configuration/Boundary/Negative/Functional 等测试类型，"
        "CLI 命令必须使用 CLI 参考中的准确语法。"
    )
    return "\n\n".join(parts)


def _postprocess_answer(text: str) -> str:
    """移除不专业的表述。"""
    patterns = [
        r'^(?:好的[，,。！!\s]*)',
        r'^(?:当然[，,。！!\s]*)',
        r'^(?:根据[您你]提供的[文档资料内容]+[，,]\s*)',
        r'^(?:根据以[上下](?:的)?[文档资料内容]+[，,]\s*)',
        r'^(?:根据检索到的[文档资料内容]+[，,]\s*)',
    ]
    for p in patterns:
        text = re.sub(p, '', text, count=1)
    return text.strip()


# ── 流式模型单例 ────────────────────────────────────────────────────
_stream_model = None


def _get_stream_model():
    """获取流式输出的 LLM 模型 (与主 model 配置相同，但 stream=True)。"""
    global _stream_model
    if _stream_model is None:
        from INAGENT.utils.llm_config import get_gateway_config
        from INAGENT.utils import env_utils
        cfg = get_gateway_config()
        api_key = cfg.get("api_key")
        if not api_key or env_utils.is_placeholder_value(api_key):
            raise ValueError("LLM 网关未配置")
        _stream_model = ModelFactory.create(
            model_platform=ModelPlatformType.SILICONFLOW,
            model_type=cfg.get("chat_model", "Qwen/Qwen3-8B"),
            api_key=api_key,
            url=cfg.get("base_url"),
            model_config_dict={"temperature": 0.0, "stream": True},
        )
    return _stream_model


# ── 内部: 功能解释 (非流式 fallback) ─────────────────────────────────
def _generate_explanation(query, hybrid_retriever, reranker, model):
    """通过知识路由器检索 + ChatAgent 解释。"""
    kr = get_knowledge_router()
    retrieval = kr.retrieve(query, mode="explain", max_context_chars=10000)
    context = retrieval.get("context", "")

    snippets = []
    cleaned_ctx = _clean_context(context) if context else ""
    if cleaned_ctx:
        snippets = [s[:500] for s in cleaned_ctx.split("\n\n") if s.strip()][:5]

    agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="技术文档专家",
            content=_EXPLAIN_SYSTEM_PROMPT,
        ),
        model=model,
    )

    prompt = _build_explain_prompt(query, context)
    response = agent.step(prompt)
    answer = response.msgs[0].content if response.msgs else "无法生成回答"
    answer = _postprocess_answer(answer)
    return answer, snippets[:5]


# ── 内部: 配置生成 ──────────────────────────────────────────────────
def _generate_config(query, hybrid_retriever, reranker, model):
    """RAG + TaskDecomposition + LB Ops Agent 配置生成 (复用 process_job)。"""
    from INAGENT.workflow_config_generator import process_job

    function_index = get_function_index()
    result = process_job(query, hybrid_retriever, reranker, model, function_index)

    if not isinstance(result, dict):
        return "未能生成配置命令 (无有效返回)", [], [], []

    config = result.get("config") or {}
    decomp = result.get("decomposition") or {}
    if not isinstance(config, dict):
        config = {}
    if not isinstance(decomp, dict):
        decomp = {}

    cc = config.get("config_commands", [])
    vc = config.get("verify_commands", [])
    notes = config.get("notes", "")

    parts = []
    if decomp.get("scenario_id"):
        parts.append(f"**场景**: {decomp['scenario_id']}")
    if decomp.get("product_modules"):
        parts.append(f"**模块**: {', '.join(decomp['product_modules'])}")
    if cc:
        parts.append("\n**配置命令:**\n```\n" + "\n".join(cc) + "\n```")
    if vc:
        parts.append("\n**验证命令:**\n```\n" + "\n".join(vc) + "\n```")
    if notes:
        parts.append(f"\n**注意事项:** {notes}")

    answer = "\n".join(parts) if parts else "未能生成配置命令"
    return answer, cc, vc, []


# ── 内部: 测试用例编写 ──────────────────────────────────────────────
_TEST_WRITE_SYSTEM_PROMPT = (
    f"你是 {_PRODUCT_NAME} 的测试用例编写专家。\n"
    "职责: 根据产品功能需求、CLI 命令参考和测试规范，为指定功能编写完整的测试用例。\n\n"
    "核心规则:\n"
    "1. 严格遵循[测试规范]中定义的格式、测试类型和优先级标准\n"
    "2. Case ID 必须遵循 MMSSIICCNN 格式\n"
    "3. 测试步骤中的 CLI 命令必须来自[CLI 参考]，语法严格准确\n"
    "4. 每个功能至少覆盖 Configuration + Boundary + Negative 三种测试类型\n"
    "5. Expected Result 必须可量化/可验证（如 '命令执行成功，show 输出包含...'）\n"
    "6. 参考[已有测试]的格式和粒度，保持一致性\n"
    "7. 基于[产品知识]理解功能设计意图和参数约束\n\n"
    "输出格式:\n"
    "- 使用 Markdown 表格输出测试用例\n"
    "- 表头: Case ID | Test Type | Priority | Description | Steps | Expected Result\n"
    "- 每个用例一行\n"
    "- 命令使用 `code` 格式"
)


def _generate_test_cases(query: str, model) -> tuple:
    """通过知识路由器获取分层上下文，调用 Agent 编写测试用例。"""
    router = get_knowledge_router()
    retrieval = router.retrieve(query, mode="test_write", max_context_chars=20000)

    context = retrieval["context"]
    layers = retrieval["layers_used"]
    logger.info("[test_write] 使用知识层: %s", layers)

    agent = ChatAgent(
        system_message=BaseMessage.make_assistant_message(
            role_name="测试用例编写专家",
            content=_TEST_WRITE_SYSTEM_PROMPT,
        ),
        model=model,
    )

    prompt_parts = [f"[测试需求]\n{query}"]
    if context:
        prompt_parts.append(context)
    prompt_parts.append(
        "请基于以上信息，为该功能编写完整的测试用例集。"
        "确保覆盖 Configuration/Boundary/Negative/Functional 等测试类型，"
        "CLI 命令必须使用 CLI 参考中的准确语法。"
    )
    prompt = "\n\n".join(prompt_parts)

    response = agent.step(prompt)
    answer = response.msgs[0].content if response.msgs else "无法生成测试用例"
    answer = _postprocess_answer(answer)

    sources = []
    if retrieval.get("cli_results"):
        sources.append(f"CLI 参考: {len(retrieval['cli_results'])} 条命令")
    if retrieval.get("similar_tests"):
        sources.append(f"已有测试参考: {len(retrieval['similar_tests'])} 条")
    if retrieval.get("rag_context"):
        sources.append("产品设计文档")
    if retrieval.get("rules_context"):
        sources.append("测试规范")

    return answer, sources


# ── 内部: 测试用例评审（统一走 ReviewPipeline）─────────────────────


def _review_test_cases(query: str, model) -> tuple:
    """通过 ReviewPipeline 三步自主评审测试用例（plan → knowledge → review）。"""
    from INAGENT.review import ReviewPipeline

    router = get_knowledge_router()
    pipeline = ReviewPipeline(router=router, model=model, product_name=_PRODUCT_NAME)
    result = pipeline.run(test_cases_text=query)
    answer = _postprocess_answer(result.review)

    sources = []
    if result.knowledge:
        sources.append("测试规范/评审清单")

    return answer, sources
