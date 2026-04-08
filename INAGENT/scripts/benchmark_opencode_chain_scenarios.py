#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
模拟 INFOAGEN 典型链路，对 OpenCode Go 推荐模型做延迟 + 简易质量评测。

对齐概念（与运维选型一致，非改生产代码）：
  - Workflow / 主 Agent：kimi-k2.5、mimo-v2-pro；极难子任务：glm-5
  - GraphRAG / 长文本抽取：偏质量 kimi / glm；偏省钱短 prompt：mimo-v2-pro、mimo-v2-omni
  - auto_convert / 轻量判断：minimax-m2.5（无 tools/stream 路径）；否则 mimo-v2-pro
  - 发布前终审：glm-5

依赖：httpx、python-dotenv
  - OPENCODE_GO_API_KEY：OpenCode Go 模型
  - DASHSCOPE_API_KEY：对照组 qwen-plus（百炼兼容 OpenAI）
  可选 DASHSCOPE_API_BASE_URL（默认 compatible-mode/v1）、LLM_GATEWAY_CHAT_MODEL（默认 qwen-plus）

用法（仓库根目录）：
  python INAGENT/scripts/benchmark_opencode_chain_scenarios.py
  python INAGENT/scripts/benchmark_opencode_chain_scenarios.py --json
  python INAGENT/scripts/benchmark_opencode_chain_scenarios.py --only workflow_main,graphrag_cheap
  python INAGENT/scripts/benchmark_opencode_chain_scenarios.py --ascii-table
  python INAGENT/scripts/benchmark_opencode_chain_scenarios.py --skip-qwen-baseline

默认会跑 qwen-plus 对照并在文末打印「按任务组 vs qwen-plus」汇总。
Workflow / GraphRAG 的 JSON 评分对各家返回一律用 json.JSONDecoder.raw_decode 从正文或代码块中抽取首个合法对象/数组（不依赖「整段只能是 JSON」）。

Windows 终端中文乱码时可设：set PYTHONUTF8=1 或 chcp 65001；或使用 --ascii-table。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / "INAGENT" / ".env"

OPENCODE_CHAT_COMPLETIONS_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


# --- Prompts（贴近链路形态，体量控制以免烧额度过快）---

WORKFLOW_USER = """你是 INFOAGEN 工作流规划器。根据下面任务，只输出一个 JSON 对象（不要用 markdown 代码块），键必须包含：
  "steps": string 数组（至少 3 步）,
  "risks": string 数组（至少 1 条风险）,
  "estimate_minutes": 整数。

任务：为「路由器 CLI 自动化回归」生成执行计划：登录设备、查看接口状态、对指定端口注入 CRC 错误、校验告警与计数增长。"""

WORKFLOW_HARD_USER = WORKFLOW_USER + "\n\n附加约束：需显式包含回滚步骤与安全检查。"

GRAPHRAG_DOC = """
[产品手册片段] ACME Router X200 第 4.2 节
管理口 MGMT0 默认地址 192.168.1.1。接口 GigabitEthernet0/1 属于 VLAN 10，描述为 uplink-to-core。
命令行：show interface GigabitEthernet0/1 可查看 CRC、errors。若 CRC 持续增长，可能为线缆故障。
与 Core-SW-01 通过 OSPF area 0 邻接；Router-ID 为 10.0.0.1。
维护窗口：每周日 02:00-04:00 UTC。
"""

GRAPHRAG_USER = f"""根据下列文本抽取「实体」与「关系」，只输出 JSON 数组，元素形如 {{"entity": "...", "type": "...", "relations": [{{"predicate": "...", "object": "..."}}]}}。至少 4 个实体。

文本：
{GRAPHRAG_DOC.strip()}
"""

AUTO_CONVERT_CHUNK = """
# X200 旗舰路由 —— 超越速度想象
立即升级，企业网络快人一步！限时优惠请咨询销售。
支持 100G 线卡，冗余电源，五年质保。
"""

AUTO_CONVERT_USER = f"""你是文档分块分类器。下面文本是「技术文档」还是「营销宣传」？
只回答一个词：technical 或 marketing（小写英文）。

--- 文本开始 ---
{AUTO_CONVERT_CHUNK.strip()}
--- 文本结束 ---"""

REVIEW_DOC = """
特性规格：接口热插拔在未文档化情况下可能导致链路闪断；建议 2.1 版固件修复。
测试缺口：未覆盖 MGMT 口 ACL 与 IPv6 NDP 并发。
发布决策：待安全组签字。
"""

REVIEW_USER = f"""你是发布前终审。阅读下列摘要，输出：
1) 三条具体问题（编号列表）
2) 最后一行单独写 VERDICT: PASS 或 VERDICT: FAIL

摘要：
{REVIEW_DOC.strip()}
"""


@dataclass
class BenchRow:
    chain_label: str
    scenario_id: str
    model: str
    api_style: str
    provider: str
    compare_group: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    quality: float
    quality_detail: str
    text_preview: str = ""
    error: str = ""


@dataclass
class CallOutcome:
    ok: bool
    text: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if ENV_PATH.is_file():
        load_dotenv(ENV_PATH, override=False)


def _opencode_key_optional() -> Optional[str]:
    return (os.getenv("OPENCODE_GO_API_KEY") or "").strip() or None


def _require_opencode_key() -> str:
    k = _opencode_key_optional()
    if not k:
        print("ERROR: 请设置 OPENCODE_GO_API_KEY（例如 INAGENT/.env）", file=sys.stderr)
        sys.exit(2)
    return k


def _dashscope_key() -> Optional[str]:
    return (os.getenv("DASHSCOPE_API_KEY") or "").strip() or None


def _dashscope_base_url() -> str:
    u = (os.getenv("DASHSCOPE_API_BASE_URL") or "").strip().rstrip("/")
    if not u:
        return DEFAULT_DASHSCOPE_BASE
    if not u.endswith("/v1"):
        u = f"{u}/v1"
    return u


def _qwen_model_name() -> str:
    return (os.getenv("LLM_GATEWAY_CHAT_MODEL") or "qwen-plus").strip() or "qwen-plus"


_JSON_DEC = json.JSONDecoder()


def _scan_json_object(s: str) -> Optional[dict]:
    """从任意前缀/后缀混排文本中抽出第一个完整 JSON 对象（与各厂商 content/reasoning 格式无关）。"""
    s = s.strip()
    if not s:
        return None
    i = 0
    while i < len(s):
        j = s.find("{", i)
        if j < 0:
            break
        try:
            val, end = _JSON_DEC.raw_decode(s, j)
            if isinstance(val, dict):
                return val
            i = end
        except json.JSONDecodeError:
            i = j + 1
    return None


def _scan_json_array(s: str) -> Optional[list]:
    """从任意混排文本中抽出第一个完整 JSON 数组。"""
    s = s.strip()
    if not s:
        return None
    i = 0
    while i < len(s):
        j = s.find("[", i)
        if j < 0:
            break
        try:
            val, end = _JSON_DEC.raw_decode(s, j)
            if isinstance(val, list):
                return val
            i = end
        except json.JSONDecodeError:
            i = j + 1
    return None


def _extract_json_object(s: str) -> Optional[dict]:
    """先扫正文，再扫 ``` / ```json 代码块（避免非贪婪正则截断嵌套大括号）。"""
    s = s.strip()
    if not s:
        return None
    obj = _scan_json_object(s)
    if obj is not None:
        return obj
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE):
        inner = m.group(1).strip()
        obj = _scan_json_object(inner)
        if obj is not None:
            return obj
    return None


def _extract_json_array(s: str) -> Optional[list]:
    s = s.strip()
    if not s:
        return None
    arr = _scan_json_array(s)
    if arr is not None:
        return arr
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE):
        inner = m.group(1).strip()
        arr = _scan_json_array(inner)
        if arr is not None:
            return arr
    return None


def _stringify_openai_part(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        t = part.get("type")
        if t == "text" and "text" in part:
            return str(part.get("text") or "")
        if t in ("output_text", "input_text"):
            return str(part.get("text") or "")
    return ""


def _openai_text_from_message(msg: Dict[str, Any]) -> str:
    """合并 content（字符串或多段）与 reasoning，避免不同网关只填其一或拆成数组。"""
    c = msg.get("content")
    if c is None:
        c = ""
    if isinstance(c, list):
        c = "".join(_stringify_openai_part(p) for p in c)
    else:
        c = str(c)
    r = msg.get("reasoning")
    if r is None:
        r = ""
    if isinstance(r, list):
        r = "".join(_stringify_openai_part(p) for p in r)
    else:
        r = str(r)
    c, r = c.strip(), r.strip()
    if c and r:
        return f"{c}\n{r}".strip()
    return (c or r).strip()


def _anthropic_text_from_body(data: Dict[str, Any]) -> str:
    content = data.get("content")
    text = ""
    thinking = ""
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text += str(block.get("text", ""))
            elif block.get("type") == "thinking":
                thinking += str(block.get("thinking", ""))
    elif isinstance(content, str):
        text = content
    out = text.strip()
    return out if out else thinking.strip()


def _chat_completions_url(base_or_full: str) -> str:
    b = base_or_full.rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return f"{b}/chat/completions"


async def call_openai_compatible(
    client: httpx.AsyncClient,
    completions_url: str,
    api_key: str,
    model: str,
    user_content: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: float = 180.0,
) -> CallOutcome:
    t0 = time.perf_counter()
    url = _chat_completions_url(completions_url)
    try:
        r = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": user_content}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        if r.status_code >= 400:
            return CallOutcome(False, "", elapsed, error=f"HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return CallOutcome(False, "", elapsed, error="no choices")
        msg = choices[0].get("message") or {}
        txt = _openai_text_from_message(msg)
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if not txt:
            return CallOutcome(False, "", elapsed, pt, ct, "empty content")
        return CallOutcome(True, txt, elapsed, pt, ct, "")
    except Exception as e:
        return CallOutcome(False, "", time.perf_counter() - t0, error=str(e))


async def call_openai_chat(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    user_content: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: float = 180.0,
) -> CallOutcome:
    return await call_openai_compatible(
        client,
        OPENCODE_CHAT_COMPLETIONS_URL,
        api_key,
        model,
        user_content,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )


async def call_anthropic_messages(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    user_content: str,
    *,
    max_tokens: int = 256,
    temperature: float = 0.2,
    timeout: float = 180.0,
) -> CallOutcome:
    t0 = time.perf_counter()
    try:
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        r = await client.post(
            MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        if r.status_code >= 400:
            return CallOutcome(False, "", elapsed, error=f"HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        txt = _anthropic_text_from_body(data)
        usage = data.get("usage") or {}
        pt = int(usage.get("input_tokens") or 0)
        ct = int(usage.get("output_tokens") or 0)
        if not txt:
            return CallOutcome(False, "", elapsed, pt, ct, "empty text")
        return CallOutcome(True, txt, elapsed, pt, ct, "")
    except Exception as e:
        return CallOutcome(False, "", time.perf_counter() - t0, error=str(e))


def score_workflow(text: str) -> Tuple[float, str]:
    obj = _extract_json_object(text)
    if not obj:
        return 0.0, "not_valid_json_object"
    steps = obj.get("steps")
    risks = obj.get("risks")
    est = obj.get("estimate_minutes")
    ok_steps = isinstance(steps, list) and len(steps) >= 3
    ok_risks = isinstance(risks, list) and len(risks) >= 1
    ok_est = isinstance(est, int) and est > 0
    n = sum([ok_steps, ok_risks, ok_est])
    return n / 3.0, f"steps={ok_steps},risks={ok_risks},estimate={ok_est}"


def score_graphrag(text: str) -> Tuple[float, str]:
    arr = _extract_json_array(text)
    if not arr:
        return 0.0, "not_valid_json_array"
    if len(arr) < 4:
        return 0.5, f"array_len={len(arr)} (<4)"
    return 1.0, f"entities={len(arr)}"


def score_auto_convert(text: str) -> Tuple[float, str]:
    """允许模型先解释再给结论：在全文或末行中识别 technical / marketing。"""
    t = text.strip()
    if not t:
        return 0.0, "empty"
    low = t.lower()
    pos_m = [m.start() for m in re.finditer(r"\bmarketing\b", low)]
    pos_t = [m.start() for m in re.finditer(r"\btechnical\b", low)]
    if pos_m and not pos_t:
        return 1.0, "expected_marketing"
    if pos_t and not pos_m:
        return 0.0, "got_technical"
    if pos_m and pos_t:
        if max(pos_m) > max(pos_t):
            return 1.0, "expected_marketing_last"
        return 0.0, "got_technical_last"
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for line in reversed(lines):
        tokens = re.findall(r"[a-z]{2,}", line.lower())
        if tokens:
            w = tokens[-1].rstrip(".,;:!?")
            if w == "marketing":
                return 1.0, "expected_marketing_tail"
            if w == "technical":
                return 0.0, "got_technical_tail"
    head = low.split()
    if not head:
        return 0.0, "empty"
    first = head[0].rstrip(".,;:")
    if first == "marketing":
        return 1.0, "expected_marketing"
    return 0.0, f"got={first!r}_want_marketing"


def score_review(text: str) -> Tuple[float, str]:
    u = text.upper()
    if "VERDICT: PASS" in u or "VERDICT: FAIL" in u:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        numbered = sum(1 for ln in lines if re.match(r"^\d+[\).]", ln))
        if numbered >= 3:
            return 1.0, "verdict+3_issues"
        return 0.7, "verdict_but_issues_weak"
    return 0.3, "no_verdict_line"


SCENARIO_MATRIX: List[Dict[str, Any]] = [
    {
        "id": "workflow_main",
        "compare_group": "workflow_main",
        "chain": "Workflow / 主 Agent",
        "models": ["kimi-k2.5", "mimo-v2-pro"],
        "api": "openai",
        "prompt": WORKFLOW_USER,
        "max_tokens": 900,
        "score": score_workflow,
    },
    {
        "id": "workflow_hard",
        "compare_group": "workflow_hard",
        "chain": "Workflow / 极难子任务（升级 GLM）",
        "models": ["glm-5"],
        "api": "openai",
        "prompt": WORKFLOW_HARD_USER,
        "max_tokens": 1024,
        "score": score_workflow,
    },
    {
        "id": "graphrag_quality_kimi",
        "compare_group": "graphrag_extract",
        "chain": "GraphRAG / 抽取（偏质量）",
        "models": ["kimi-k2.5"],
        "api": "openai",
        "prompt": GRAPHRAG_USER,
        "max_tokens": 1800,
        "score": score_graphrag,
    },
    {
        "id": "graphrag_quality_glm",
        "compare_group": "graphrag_extract",
        "chain": "GraphRAG / 抽取（偏质量）",
        "models": ["glm-5"],
        "api": "openai",
        "prompt": GRAPHRAG_USER,
        "max_tokens": 1800,
        "score": score_graphrag,
    },
    {
        "id": "graphrag_cheap_pro",
        "compare_group": "graphrag_extract",
        "chain": "GraphRAG / 抽取（偏省钱）",
        "models": ["mimo-v2-pro"],
        "api": "openai",
        "prompt": GRAPHRAG_USER,
        "max_tokens": 1800,
        "score": score_graphrag,
    },
    {
        "id": "graphrag_cheap_omni",
        "compare_group": "graphrag_extract",
        "chain": "GraphRAG / 抽取（偏省钱）",
        "models": ["mimo-v2-omni"],
        "api": "openai",
        "prompt": GRAPHRAG_USER,
        "max_tokens": 1800,
        "score": score_graphrag,
    },
    {
        "id": "auto_convert_minimax",
        "compare_group": "auto_convert",
        "chain": "auto_convert / 轻量分类（MiniMax，无 tools/stream 路径）",
        "models": ["minimax-m2.5", "minimax-m2.7"],
        "api": "anthropic",
        "prompt": AUTO_CONVERT_USER,
        "max_tokens": 32,
        "score": score_auto_convert,
    },
    {
        "id": "auto_convert_mimo_fallback",
        "compare_group": "auto_convert",
        "chain": "auto_convert / 轻量分类（OpenAI 兼容回退）",
        "models": ["mimo-v2-pro"],
        "api": "openai",
        "prompt": AUTO_CONVERT_USER,
        "max_tokens": 32,
        "score": score_auto_convert,
    },
    {
        "id": "final_review",
        "compare_group": "final_review",
        "chain": "发布前终审",
        "models": ["glm-5"],
        "api": "openai",
        "prompt": REVIEW_USER,
        "max_tokens": 1200,
        "score": score_review,
    },
]


def qwen_baseline_specs(qwen_model: str) -> List[Dict[str, Any]]:
    """与 OpenCode 侧同题对照；also_run_when 用于 --only 时自动带上 qwen 基线。"""
    qm = qwen_model
    return [
        {
            "id": "qwen_baseline_workflow_main",
            "compare_group": "workflow_main",
            "chain": f"Baseline DashScope / {qm}",
            "models": [qm],
            "api": "dashscope",
            "prompt": WORKFLOW_USER,
            "max_tokens": 900,
            "score": score_workflow,
            "also_run_when": frozenset({"workflow_main"}),
        },
        {
            "id": "qwen_baseline_workflow_hard",
            "compare_group": "workflow_hard",
            "chain": f"Baseline DashScope / {qm}",
            "models": [qm],
            "api": "dashscope",
            "prompt": WORKFLOW_HARD_USER,
            "max_tokens": 1024,
            "score": score_workflow,
            "also_run_when": frozenset({"workflow_hard"}),
        },
        {
            "id": "qwen_baseline_graphrag",
            "compare_group": "graphrag_extract",
            "chain": f"Baseline DashScope / {qm}",
            "models": [qm],
            "api": "dashscope",
            "prompt": GRAPHRAG_USER,
            "max_tokens": 1800,
            "score": score_graphrag,
            "also_run_when": frozenset(
                {
                    "graphrag_quality_kimi",
                    "graphrag_quality_glm",
                    "graphrag_cheap_pro",
                    "graphrag_cheap_omni",
                }
            ),
        },
        {
            "id": "qwen_baseline_auto_convert",
            "compare_group": "auto_convert",
            "chain": f"Baseline DashScope / {qm}",
            "models": [qm],
            "api": "dashscope",
            "prompt": AUTO_CONVERT_USER,
            "max_tokens": 32,
            "score": score_auto_convert,
            "also_run_when": frozenset({"auto_convert_minimax", "auto_convert_mimo_fallback"}),
        },
        {
            "id": "qwen_baseline_final_review",
            "compare_group": "final_review",
            "chain": f"Baseline DashScope / {qm}",
            "models": [qm],
            "api": "dashscope",
            "prompt": REVIEW_USER,
            "max_tokens": 1200,
            "score": score_review,
            "also_run_when": frozenset({"final_review"}),
        },
    ]


def _expand_filter_for_qwen(
    filt: Optional[set],
    *,
    compare_qwen: bool,
    qspecs: List[Dict[str, Any]],
) -> Optional[set]:
    if not filt or not compare_qwen:
        return filt
    extra = set(filt)
    for qs in qspecs:
        aw = qs.get("also_run_when")
        if aw and (aw & filt):
            extra.add(qs["id"])
    return extra


async def _run_one(
    client: httpx.AsyncClient,
    creds: Dict[str, str],
    spec: Dict[str, Any],
    model: str,
) -> BenchRow:
    api = spec["api"]
    compare_group = str(spec.get("compare_group") or spec["id"])
    provider = "opencode_go"
    if api == "dashscope":
        provider = "dashscope"
        out = await call_openai_compatible(
            client,
            creds["dashscope_completions_url"],
            creds["dashscope"],
            model,
            spec["prompt"],
            max_tokens=spec["max_tokens"],
        )
    elif api == "openai":
        out = await call_openai_chat(
            client,
            creds["opencode"],
            model,
            spec["prompt"],
            max_tokens=spec["max_tokens"],
        )
    else:
        out = await call_anthropic_messages(
            client,
            creds["opencode"],
            model,
            spec["prompt"],
            max_tokens=spec["max_tokens"],
        )

    if not out.ok:
        return BenchRow(
            chain_label=spec["chain"],
            scenario_id=spec["id"],
            model=model,
            api_style=api,
            provider=provider,
            compare_group=compare_group,
            latency_s=round(out.latency_s, 3),
            prompt_tokens=out.prompt_tokens,
            completion_tokens=out.completion_tokens,
            quality=0.0,
            quality_detail="call_failed",
            error=out.error or "unknown",
        )

    q, qd = spec["score"](out.text)
    preview = out.text.replace("\n", " ")[:120]
    return BenchRow(
        chain_label=spec["chain"],
        scenario_id=spec["id"],
        model=model,
        api_style=api,
        provider=provider,
        compare_group=compare_group,
        latency_s=round(out.latency_s, 3),
        prompt_tokens=out.prompt_tokens,
        completion_tokens=out.completion_tokens,
        quality=round(q, 3),
        quality_detail=qd,
        text_preview=preview,
    )


async def _run_all(
    filter_ids: Optional[set],
    delay_s: float,
    *,
    compare_qwen: bool,
    qwen_model: str,
) -> List[BenchRow]:
    _load_env()
    qspecs = qwen_baseline_specs(qwen_model) if compare_qwen else []
    filter_ids = _expand_filter_for_qwen(filter_ids, compare_qwen=compare_qwen, qspecs=qspecs)

    matrix: List[Dict[str, Any]] = list(SCENARIO_MATRIX)
    if compare_qwen:
        matrix = matrix + qspecs

    selected = [
        s
        for s in matrix
        if filter_ids is None or s["id"] in filter_ids
    ]
    need_opencode = any(s["api"] in ("openai", "anthropic") for s in selected)
    need_dashscope = any(s["api"] == "dashscope" for s in selected)

    k_o = _opencode_key_optional()
    k_d = _dashscope_key()

    if need_opencode:
        k_o = k_o or _require_opencode_key()
    if need_dashscope and not k_d:
        print(
            "ERROR: 对照 qwen 需要 DASHSCOPE_API_KEY，或加 --skip-qwen-baseline 只跑 OpenCode",
            file=sys.stderr,
        )
        sys.exit(2)

    creds = {
        "opencode": k_o or "",
        "dashscope": k_d or "",
        "dashscope_completions_url": _dashscope_base_url(),
    }

    rows: List[BenchRow] = []
    async with httpx.AsyncClient() as client:
        for spec in matrix:
            if filter_ids and spec["id"] not in filter_ids:
                continue
            for model in spec["models"]:
                row = await _run_one(client, creds, spec, model)
                rows.append(row)
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
    return rows


def _print_vs_qwen(rows: List[BenchRow], *, ascii_headers: bool) -> None:
    by_g: Dict[str, List[BenchRow]] = defaultdict(list)
    for r in rows:
        by_g[r.compare_group].append(r)
    print()
    if ascii_headers:
        print("=== vs qwen baseline (heuristic quality; same group) ===\n")
    else:
        print("=== 同任务组 vs 百炼 qwen 基线（启发式质量分；非人工评测）===\n")

    for g in sorted(by_g.keys()):
        lst = by_g[g]
        qw = [r for r in lst if r.provider == "dashscope"]
        rest = [r for r in lst if r.provider != "dashscope"]
        if not qw:
            if ascii_headers:
                print(f"[{g}] no qwen row in this run (--skip-qwen-baseline or filter).")
            else:
                print(f"[{g}] 本跑次无 qwen 对照行（可能用了 --skip-qwen-baseline 或过滤）。")
            continue
        qb = qw[0]
        qerr = qb.error or ""
        if qerr:
            print(f"[{g}] qwen baseline error: {qerr[:120]}")
            continue
        print(
            f"[{g}] qwen: model={qb.model} quality={qb.quality} "
            f"latency={qb.latency_s}s tok_out={qb.completion_tokens}"
        )
        for r in sorted(rest, key=lambda x: (-x.quality, x.latency_s)):
            e = r.error or ""
            if e:
                print(f"  {r.model:20} ERROR {e[:80]}")
                continue
            if r.quality > qb.quality:
                tag = "quality_better_than_qwen"
            elif r.quality < qb.quality:
                tag = "quality_worse_than_qwen"
            else:
                tag = "quality_tie"
            faster = r.latency_s < qb.latency_s
            print(
                f"  {r.model:20} quality={r.quality} lat={r.latency_s}s "
                f"{tag} faster_than_qwen={faster}"
            )
    print()


def _print_table(rows: List[BenchRow], *, ascii_headers: bool) -> None:
    print()
    if ascii_headers:
        print(
            "| chain | scenario | model | api | latency_s | quality | ptok | ctok | detail | err |"
        )
    else:
        print(
            "| 链路 | 场景 | 模型 | API | 延迟s | 质量分 | 输入tok | 输出tok | 详情 | 错误 |"
        )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        err = (r.error or "").replace("|", "\\|")[:40]
        print(
            f"| {r.chain_label[:34]} | {r.scenario_id} | {r.model} | {r.api_style} | "
            f"{r.latency_s} | {r.quality} | {r.prompt_tokens} | {r.completion_tokens} | "
            f"{r.quality_detail[:28]} | {err} |"
        )
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenCode Go 典型链路基准")
    ap.add_argument(
        "--only",
        type=str,
        default="",
        help="逗号分隔 scenario id，仅跑子集",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="每次调用间隔秒数，减轻限流（默认 0.5）",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON 行汇总")
    ap.add_argument(
        "--ascii-table",
        action="store_true",
        help="表格列头用英文，避免 Windows 控制台编码问题",
    )
    ap.add_argument(
        "--skip-qwen-baseline",
        action="store_true",
        help="不跑百炼 qwen 对照，仅 OpenCode Go",
    )
    args = ap.parse_args()

    filt: Optional[set] = None
    if args.only.strip():
        filt = {x.strip() for x in args.only.split(",") if x.strip()}

    compare_qwen = not args.skip_qwen_baseline
    qwen_model = _qwen_model_name()
    rows = asyncio.run(
        _run_all(
            filt,
            args.delay,
            compare_qwen=compare_qwen,
            qwen_model=qwen_model,
        )
    )

    if args.json:
        payload = [
            {
                "chain": r.chain_label,
                "scenario_id": r.scenario_id,
                "model": r.model,
                "api": r.api_style,
                "provider": r.provider,
                "compare_group": r.compare_group,
                "latency_s": r.latency_s,
                "quality": r.quality,
                "quality_detail": r.quality_detail,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "error": r.error,
            }
            for r in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(rows, ascii_headers=args.ascii_table)
        if compare_qwen:
            _print_vs_qwen(rows, ascii_headers=args.ascii_table)
        print(
            "quality = heuristic (JSON / keywords); latency = HTTP round-trip. "
            "Dev note: calls OpenCode directly (not local LLM Gateway); MiniMax uses /messages like gateway."
        )


if __name__ == "__main__":
    main()
