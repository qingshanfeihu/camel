"""
采购员端对端测试 — CLI.pdf MinerU 已有输出路径

直接读取 MinerU 缓存（mineru_output/cli/hybrid_auto/cli_content_list.json），
用 MinerUChunker 分块后喂给 KnowledgeProcurementAgent，打印决策统计。

无需重跑 MinerU 或 auto_convert LLM 元数据提取。

用法:
    python -m INAGENT.scripts.test_procurement_e2e [--pages N] [--batch-size M]

    --pages N      只处理前 N 页（MinerU page_idx < N），默认 20（约 400 个 chunk）
    --batch-size M LLM 每批最多 M 个 chunk（避免单次 prompt 过长），默认 30
    --all          处理全部 12718 个 block（警告：非常慢）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# ── 路径修正 ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from INAGENT.utils.env_utils import get_product_name, load_inagent_env
from INAGENT.web.deps import get_llm_model

load_inagent_env()

_MINERU_OUTPUT = (
    _ROOT / "INAGENT" / "knowledge_base" / "mineru_output"
    / "cli" / "hybrid_auto" / "cli_content_list.json"
)
_LOG_DIR = _ROOT / "INAGENT" / "knowledge_base" / "logs" / "e2e_test"


# ── 内联分块器（避免 apps.agents.mineru_rag_demo 的导入依赖） ──────────────────

from dataclasses import dataclass, field as dc_field
from typing import Optional, Sequence


def _is_header_block(block: Dict[str, Any]) -> bool:
    if block.get("type") != "text":
        return False
    text_level = block.get("text_level")
    if isinstance(text_level, int) and text_level > 0:
        return True
    text = (block.get("text") or "").strip()
    if not text:
        return False
    if len(text) <= 60 and any(text.startswith(p) for p in ("第", "Chapter", "CHAPTER")):
        return True
    if len(text) <= 60 and text[:1].isdigit() and "." in text[:8]:
        return True
    return False


def _merge_texts(prev: str, nxt: str) -> str:
    if not prev:
        return nxt
    if not nxt:
        return prev
    if len(prev) <= 80 and len(nxt) <= 80:
        return f"{prev} {nxt}"
    return f"{prev}\n{nxt}"


@dataclass
class _MinerUChunk:
    text: str
    metadata: Dict[str, Any] = dc_field(default_factory=dict)


def _chunk_blocks(
    blocks: Sequence[Dict[str, Any]],
    token_limit: int = 800,
    min_chunk_tokens: int = 80,
) -> List["_MinerUChunk"]:
    """Simplified MinerU block grouper (no tiktoken dependency)."""
    def _rough_tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    chunks: List[_MinerUChunk] = []
    current_header: Optional[str] = None
    current_header_level: Optional[int] = None
    state: Dict[str, Any] = {"text": "", "tokens": 0, "page_start": None, "page_end": None}

    def flush() -> None:
        if not state["text"].strip():
            state.update({"text": "", "tokens": 0, "page_start": None, "page_end": None})
            return
        if state["tokens"] < min_chunk_tokens and chunks:
            last = chunks[-1]
            last.text = _merge_texts(last.text, state["text"])
            last.metadata["page_end"] = max(last.metadata.get("page_end", -1), state["page_end"] or -1)
        else:
            chunks.append(_MinerUChunk(
                text=state["text"],
                metadata={
                    "section_title": current_header,
                    "section_level": current_header_level,
                    "page_start": state["page_start"],
                    "page_end": state["page_end"],
                },
            ))
        state.update({"text": "", "tokens": 0, "page_start": None, "page_end": None})

    for block in blocks:
        if (block.get("type") or "") != "text":
            continue
        text = (block.get("text") or "").strip()
        if not text:
            continue
        page_idx = block.get("page_idx")
        is_header = _is_header_block(block)
        if isinstance(page_idx, int):
            if state["page_start"] is None:
                state["page_start"] = page_idx
            state["page_end"] = page_idx

        if is_header:
            flush()
            current_header = text
            text_level = block.get("text_level")
            current_header_level = int(text_level) if isinstance(text_level, int) else None
            state["text"] = text
            state["tokens"] = _rough_tokens(text)
            if isinstance(page_idx, int):
                state["page_start"] = page_idx
                state["page_end"] = page_idx
            continue

        candidate = _merge_texts(state["text"], text)
        candidate_tokens = _rough_tokens(candidate)
        if candidate_tokens > token_limit and state["text"].strip():
            flush()
            state["text"] = text
            state["tokens"] = _rough_tokens(text)
            if isinstance(page_idx, int):
                state["page_start"] = page_idx
                state["page_end"] = page_idx
        else:
            state["text"] = candidate
            state["tokens"] = candidate_tokens

    flush()
    return chunks


def build_chunks(
    blocks: List[Dict[str, Any]],
    source_file: str,
    document_category: str,
) -> List[Dict[str, Any]]:
    """Convert MinerU blocks → procurement-agent-compatible chunks."""
    raw_chunks = _chunk_blocks(blocks, token_limit=800, min_chunk_tokens=80)

    result = []
    for rc in raw_chunks:
        meta = {
            "source_file": source_file,
            "document_category": document_category,
        }
        if rc.metadata.get("section_title"):
            meta["section_title"] = rc.metadata["section_title"]
        if rc.metadata.get("page_start") is not None:
            meta["page_start"] = rc.metadata["page_start"]
        if rc.metadata.get("page_end") is not None:
            meta["page_end"] = rc.metadata["page_end"]
        result.append({
            "page_content": rc.text,
            "metadata": meta,
        })
    return result


# ── 批量评估（避免单次 prompt 过大） ─────────────────────────────────────────

def evaluate_in_batches(agent, chunks: List[Dict], batch_size: int):
    from INAGENT.agents.knowledge_procurement_agent import ChunkDecision
    all_decisions: List[ChunkDecision] = []
    total = len(chunks)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = chunks[start:end]
        print(f"  → 评估 chunk [{start}..{end-1}] / {total} ...", flush=True)
        decisions = agent.evaluate_batch(batch)
        # 修正 chunk_index 以反映全局偏移
        for d in decisions:
            d.chunk_index += start
        all_decisions.extend(decisions)
    return all_decisions


# ── 打印结果 ──────────────────────────────────────────────────────────────────

def print_summary(decisions, chunks_total: int) -> None:
    from collections import Counter
    action_counts: Counter = Counter()
    target_counts: Counter = Counter()
    low_conf = []
    sample_rejects = []
    sample_accepts = []

    for d in decisions:
        action_counts[d.decision.action] += 1
        target_counts[d.decision.target_kb] += 1
        if d.decision.confidence < 0.6:
            low_conf.append(d)
        if d.decision.action == "reject" and len(sample_rejects) < 5:
            sample_rejects.append(d)
        if d.decision.action == "accept" and len(sample_accepts) < 5:
            sample_accepts.append(d)

    print("\n" + "=" * 70)
    print(f"采购员决策汇总  (共 {chunks_total} chunks → {len(decisions)} 决策)")
    print("=" * 70)

    print("\n【按 action 分布】")
    for action in ("accept", "reject", "pending_review", "staging"):
        count = action_counts.get(action, 0)
        bar = "█" * (count * 30 // max(len(decisions), 1))
        print(f"  {action:<16} {count:>4}  {bar}")

    print("\n【按 target_kb 分布】")
    for kb in ("product", "test", "unknown"):
        count = target_counts.get(kb, 0)
        print(f"  {kb:<12} {count:>4}")

    print(f"\n【低置信度 (< 0.6) 片段数】: {len(low_conf)}")

    if sample_rejects:
        print("\n--- 拒绝样例 (前5) ---")
        for d in sample_rejects:
            sec = d.chunk.get("metadata", {}).get("section_title", "?")
            content = (d.chunk.get("page_content") or "")[:80].replace("\n", " ")
            print(f"  [{d.chunk_index}] {sec!r}: {content!r}")
            print(f"       → {d.decision.reason}")

    if sample_accepts:
        print("\n--- 接受样例 (前5) ---")
        for d in sample_accepts:
            sec = d.chunk.get("metadata", {}).get("section_title", "?")
            content = (d.chunk.get("page_content") or "")[:80].replace("\n", " ")
            print(f"  [{d.chunk_index}] {sec!r}: {content!r}")
            print(f"       → [{d.decision.target_kb}] {d.decision.reason}")

    print("\n" + "=" * 70)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="采购员端对端测试（CLI.pdf）")
    parser.add_argument("--pages", type=int, default=20, help="只处理前 N 页")
    parser.add_argument("--batch-size", type=int, default=30, help="每批 LLM 评估 chunk 数")
    parser.add_argument("--all", action="store_true", help="处理全部 block（慢）")
    args = parser.parse_args()

    # 1. 加载 MinerU 输出
    print(f"[1] 加载 MinerU 输出: {_MINERU_OUTPUT}")
    if not _MINERU_OUTPUT.exists():
        print(f"  ERROR: 文件不存在: {_MINERU_OUTPUT}")
        sys.exit(1)
    all_blocks: List[Dict[str, Any]] = json.loads(
        _MINERU_OUTPUT.read_text(encoding="utf-8")
    )
    print(f"  总 block 数: {len(all_blocks)}")

    # 2. 筛选页面范围
    if args.all:
        blocks = all_blocks
        print(f"  处理全部 {len(blocks)} blocks")
    else:
        blocks = [b for b in all_blocks if b.get("page_idx", 0) < args.pages]
        pages_hit = sorted({b.get("page_idx", 0) for b in blocks})
        print(
            f"  筛选前 {args.pages} 页 → {len(blocks)} blocks"
            f"  (page_idx: {pages_hit[0] if pages_hit else 'N/A'}"
            f" ~ {pages_hit[-1] if pages_hit else 'N/A'})"
        )

    # 3. 分块
    print("[2] 分块 (MinerUChunker, token_limit=800) ...")
    chunks = build_chunks(
        blocks,
        source_file="cli.pdf",
        document_category="cli/reference",
    )
    print(f"  分块结果: {len(chunks)} chunks")

    # 快速预览前3个 chunk
    print("\n  【前3 chunk 预览】")
    for i, c in enumerate(chunks[:3]):
        sec = c["metadata"].get("section_title", "")
        pg = c["metadata"].get("page_start", "?")
        content_snippet = (c["page_content"] or "")[:100].replace("\n", " ")
        print(f"  [{i}] p{pg} [{sec}]: {content_snippet!r}")

    # 4. 构建采购员
    print("\n[3] 初始化采购员 ...")
    model = get_llm_model()
    product_name = get_product_name()
    from INAGENT.agents.knowledge_procurement_agent import KnowledgeProcurementAgent
    agent = KnowledgeProcurementAgent(model=model, product_name=product_name)
    print(f"  产品名: {product_name}")
    print(f"  LLM: {model}")

    # 5. 批量评估
    print(f"\n[4] 开始评估 (batch_size={args.batch_size}) ...")
    decisions = evaluate_in_batches(agent, chunks, batch_size=args.batch_size)

    # 6. 写日志
    print("\n[5] 写入日志 ...")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    counts = agent.write_logs(decisions, log_dir=_LOG_DIR)
    print(f"  日志目录: {_LOG_DIR}")
    for action, n in sorted(counts.items()):
        print(f"  {action}: {n}")

    # 7. 打印统计
    print_summary(decisions, len(chunks))


if __name__ == "__main__":
    main()
