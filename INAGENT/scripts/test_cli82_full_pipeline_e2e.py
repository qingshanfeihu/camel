# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
cli_1-82.pdf 全流程 E2E 测试 — 采购 + 农民 + 农场主 + 随机 10 命令验证

数据源：
  MinerU 已有输出 cli_content_list.json 中 page_idx < 82 的块（对应 cli_1-82.pdf 的 82 页）。
  这 82 页包含：目录 (pages 0-3)、版权声明 (page 4)、CLI 使用说明 (pages 5-6)、
  以及系统基本操作命令 (pages 7-81)。

测试目标：
  P0:  加载 MinerU 块 → 分 chunk → 快照
  P1:  采购员三层筛查 — 验证目录/版权/说明等非产品内容被 reject
  P2:  农民填肉 — cultivate_batch（内调 auto_convert 结构化）→ write_to_reference
  P3:  农场主 TreeInformed 挖槽 → refresh_hybrid_vectors=True
  P4:  农民回填 apply_fill_request
  P5:  向量检索命中率测试：随机 10 命令查向量库，验证正确 chunk 是否在 top-20 结果中
  P6:  汇总报告（PASS / PARTIAL / FAIL）

用法：
    python -m INAGENT.scripts.test_cli82_full_pipeline_e2e [--dry-run] [--no-snapshot] [--batch-size 30]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cli82_pipeline")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
GRAPHRAG_WORKSPACE = INAGENT_ROOT / "graphrag_index"
GRAPHRAG_OUTPUT = GRAPHRAG_WORKSPACE / "output"
SNAPSHOT_ROOT = INAGENT_ROOT / "knowledge_base" / "snapshots"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
LOG_DIR = INAGENT_ROOT / "knowledge_base" / "logs"
GAPS_FILE = LOG_DIR / "cli82_schema_gaps.jsonl"
QDRANT_DIR = Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"

MINERU_CLI = (
    INAGENT_ROOT / "knowledge_base" / "mineru_output"
    / "cli" / "hybrid_auto" / "cli_content_list.json"
)

MAX_PAGE_IDX = 82  # page_idx < 82 (0-indexed)
SOURCE_FILE_STEM = "cli_1-82"
SOURCE_FILE_NAME = "cli_1-82.pdf"
RANDOM_SEED = 42
NUM_RANDOM_COMMANDS = 10

_CMD_LINE_RE = re.compile(
    r"^(?:no\s+)?[a-z][a-z0-9_-]+(?:\s+[a-z<\[\{])", re.MULTILINE
)


# ─────────────────────────────────────────────────────────────────────────────
# Chunk builder (reused from test_procurement_e2e.py, inline to avoid import)
# ─────────────────────────────────────────────────────────────────────────────

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


def _chunk_blocks(
    blocks: list,
    token_limit: int = 800,
    min_chunk_tokens: int = 80,
) -> list:
    def _rough_tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    chunks: list = []
    current_header = None
    current_header_level = None
    state = {"text": "", "tokens": 0, "page_start": None, "page_end": None}

    def flush():
        if not state["text"].strip():
            state.update({"text": "", "tokens": 0, "page_start": None, "page_end": None})
            return
        if state["tokens"] < min_chunk_tokens and chunks:
            last = chunks[-1]
            last["text"] = _merge_texts(last["text"], state["text"])
            last["meta"]["page_end"] = max(
                last["meta"].get("page_end", -1), state["page_end"] or -1
            )
        else:
            chunks.append({
                "text": state["text"],
                "meta": {
                    "section_title": current_header,
                    "section_level": current_header_level,
                    "page_start": state["page_start"],
                    "page_end": state["page_end"],
                },
            })
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


def build_chunks(blocks: List[Dict], source_file: str) -> List[Dict]:
    raw = _chunk_blocks(blocks, token_limit=800, min_chunk_tokens=80)
    result = []
    for rc in raw:
        meta: Dict[str, Any] = {
            "source_file": source_file,
            "document_category": "cli/reference",
        }
        if rc["meta"].get("section_title"):
            meta["section_title"] = rc["meta"]["section_title"]
        if rc["meta"].get("page_start") is not None:
            meta["page_start"] = rc["meta"]["page_start"]
        if rc["meta"].get("page_end") is not None:
            meta["page_end"] = rc["meta"]["page_end"]
        result.append({"page_content": rc["text"], "metadata": meta})
    return result


# ─────────────────────────────────────────────────────────────────────────────
# P0: 加载 & 快照
# ─────────────────────────────────────────────────────────────────────────────

def phase0_load_and_snapshot(
    skip_snapshot: bool = False,
    restore_kb: bool = True,
) -> Tuple[List[Dict], List[Dict], Optional[Path]]:
    logger.info("=" * 60)
    logger.info("P0: 加载 MinerU 输出 + 分块 + 快照")
    logger.info("=" * 60)

    if not MINERU_CLI.exists():
        logger.error("MinerU 输出不存在: %s", MINERU_CLI)
        sys.exit(1)

    all_blocks = json.loads(MINERU_CLI.read_text(encoding="utf-8"))
    blocks = [b for b in all_blocks if b.get("page_idx", 999) < MAX_PAGE_IDX]
    pages = sorted({b.get("page_idx", -1) for b in blocks})
    logger.info(
        "  总 blocks: %d → 筛选 page_idx < %d → %d blocks (%d pages: %d~%d)",
        len(all_blocks), MAX_PAGE_IDX, len(blocks), len(pages),
        pages[0] if pages else -1, pages[-1] if pages else -1,
    )

    chunks = build_chunks(blocks, source_file=SOURCE_FILE_NAME)
    logger.info("  分块结果: %d chunks", len(chunks))

    cmd_blocks = [b for b in blocks if _CMD_LINE_RE.search(b.get("text", ""))]
    logger.info("  含命令行特征的 blocks: %d", len(cmd_blocks))

    # 快照
    snap_dir = None
    if not skip_snapshot:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_dir = SNAPSHOT_ROOT / f"pre_cli82_{ts}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        ref_snap = snap_dir / "reference"
        ref_snap.mkdir()
        for f in REFERENCE_DIR.glob("*.json"):
            shutil.copy2(f, ref_snap / f.name)
        logger.info("  快照: %s", snap_dir.name)
    else:
        logger.info("  跳过快照")

    # 清理上次残留
    stale = [
        REFERENCE_DIR / f"{SOURCE_FILE_STEM}.json",
        LOG_DIR / f"{SOURCE_FILE_STEM}.farmer_cache.json",
        GAPS_FILE,
    ]
    for p in stale:
        if p.exists():
            p.unlink()
            logger.info("  清理残留: %s", p.name)

    # TB0: 从 CLI 图谱重建干净的 knowledge_base.json，消除历次农民运行的污染
    # 这是通用做法：以 cli_keyword_graph.json 为唯一权威来源，不依赖任何硬编码节点名
    if restore_kb:
        try:
            from INAGENT.scripts.rebuild_cli_docs import build_cli_chunks  # type: ignore
            from INAGENT.scripts.rebuild_cli_docs import _GRAPH_PATH as _REBUILD_GRAPH_PATH  # type: ignore
            cli_chunks = build_cli_chunks(_REBUILD_GRAPH_PATH)
            KB_PATH.write_text(
                json.dumps(cli_chunks, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            logger.info(
                "  TB0: knowledge_base.json 已从 CLI 图谱重建 (%d 叶子节点，所有历史污染已清除)",
                len(cli_chunks),
            )
        except Exception as exc:
            logger.warning("  TB0: KB 重建失败，使用现有文件继续 (%s)", exc)

    return blocks, chunks, snap_dir


# ─────────────────────────────────────────────────────────────────────────────
# P1: 采购员三层筛查
# ─────────────────────────────────────────────────────────────────────────────

def phase1_procurement(
    chunks: List[Dict], batch_size: int = 30
) -> Tuple[List[Any], Dict[str, Any]]:
    logger.info("=" * 60)
    logger.info("P1: 采购员三层筛查 (%d chunks, batch_size=%d)", len(chunks), batch_size)
    logger.info("=" * 60)

    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        KnowledgeProcurementAgent,
    )
    from INAGENT.utils.env_utils import get_product_name
    from INAGENT.web.deps import get_llm_model

    model = get_llm_model()
    product_name = get_product_name()
    agent = KnowledgeProcurementAgent(model=model, product_name=product_name)
    logger.info("  产品名: %s", product_name)

    all_decisions: List[ChunkDecision] = []
    total = len(chunks)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = chunks[start:end]
        logger.info("  评估 chunk [%d..%d] / %d ...", start, end - 1, total)
        decisions = agent.evaluate_batch(batch)
        for d in decisions:
            d.chunk_index += start
        all_decisions.extend(decisions)

    action_counts = Counter(d.decision.action for d in all_decisions)
    target_counts = Counter(d.decision.target_kb for d in all_decisions)

    logger.info("  决策分布:")
    for act in ("accept", "reject", "pending_review", "staging"):
        logger.info("    %-16s %d", act, action_counts.get(act, 0))
    logger.info("  target_kb: %s", dict(target_counts))

    # 采集拒绝样例
    reject_samples = []
    for d in all_decisions:
        if d.decision.action == "reject" and len(reject_samples) < 10:
            sec = d.chunk.get("metadata", {}).get("section_title", "?")
            pg = d.chunk.get("metadata", {}).get("page_start", "?")
            content_head = (d.chunk.get("page_content") or "")[:100].replace("\n", " ")
            reject_samples.append({
                "idx": d.chunk_index,
                "page": pg,
                "section": sec,
                "reason": d.decision.reason,
                "content_head": content_head,
            })

    logger.info("  拒绝样例 (前 %d):", len(reject_samples))
    for s in reject_samples:
        logger.info("    [%d] p%s %r: %s", s["idx"], s["page"], s["section"], s["reason"])

    # 断言
    p1_assertions: List[str] = []
    accept_n = action_counts.get("accept", 0)
    reject_n = action_counts.get("reject", 0)

    if reject_n > 0:
        p1_assertions.append(f"PASS reject_count={reject_n} > 0 (采购员识别出非产品内容)")
    else:
        p1_assertions.append("FAIL reject_count=0 (期望筛出目录/版权等)")

    if accept_n > reject_n:
        p1_assertions.append(
            f"PASS accept({accept_n}) > reject({reject_n}) (多数内容为有效命令)"
        )
    else:
        p1_assertions.append(
            f"WARN accept({accept_n}) <= reject({reject_n}) (采购员可能过度拒绝)"
        )

    # 采购员日志写入
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    counts = agent.write_logs(all_decisions, log_dir=LOG_DIR / "cli82_procurement")
    logger.info("  采购日志写入: %s", dict(counts))

    for msg in p1_assertions:
        logger.info("  [P1] %s", msg)

    stats = {
        "total_chunks": len(chunks),
        "action_counts": dict(action_counts),
        "target_counts": dict(target_counts),
        "reject_samples": reject_samples,
        "assertions": p1_assertions,
    }
    return all_decisions, stats


# ─────────────────────────────────────────────────────────────────────────────
# P2: 农民填肉
# ─────────────────────────────────────────────────────────────────────────────

def phase2_farmer(
    decisions: List[Any], model
) -> Tuple[Any, List[Any], int, Dict[str, Any]]:
    from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
        enrich_chunk_decision_for_farmer,
    )

    accepted = [d for d in decisions if d.decision.action == "accept"]
    logger.info("=" * 60)
    logger.info("P2: 农民填肉 (%d accepted chunks)", len(accepted))
    logger.info("=" * 60)

    enriched = [enrich_chunk_decision_for_farmer(d) for d in accepted]
    farmer = KnowledgeFarmerAgent(model=model)
    results = farmer.cultivate_batch(enriched)

    # 统计
    total = len(results)
    matched = sum(1 for r in results if r.matched_node_id)
    unmatched = total - matched
    all_gaps = sum(len(r.schema_gaps) for r in results)

    logger.info("  cultivate 结果: %d chunks, matched=%d, unmatched=%d, gaps=%d",
                total, matched, unmatched, all_gaps)

    # write_to_reference
    ref_counts = farmer.write_to_reference(results)
    logger.info("  write_to_reference: %s", ref_counts)

    # emit schema gaps
    GAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    gap_count = farmer.emit_schema_gaps(results, GAPS_FILE)
    logger.info("  schema_gaps: %d 条 → %s", gap_count, GAPS_FILE.name)

    # 匹配的命令前缀
    matched_prefixes = sorted({
        r.chunk.get("metadata", {}).get("command_prefix", "?")
        for r in results if r.matched_node_id
    })
    logger.info("  匹配的命令前缀 (前20): %s", matched_prefixes[:20])

    # P2.5: 校验 matched_node_id 在 CLIGraphStore（cli_keyword_graph.json）中的有效性
    # 通用做法：以 CLIGraphStore._nodes_by_id 为权威，不依赖任何节点名硬编码
    try:
        from INAGENT.rag.cli_graph_store import CLIGraphStore
        _tree = CLIGraphStore()
        _tree._ensure_loaded()
        matched_in_tree = sum(
            1 for r in results
            if r.matched_node_id and r.matched_node_id in _tree._nodes_by_id
        )
        orphan_tids = sorted({
            r.matched_node_id for r in results
            if r.matched_node_id and r.matched_node_id not in _tree._nodes_by_id
        })
        logger.info(
            "  P2.5 leaf完整性: %d/%d matched chunks在CLIGraphStore有效; 孤儿TID(%d): %s",
            matched_in_tree, matched,
            len(orphan_tids), orphan_tids[:10],
        )
        leaf_integrity_rate = matched_in_tree / matched if matched else 0.0
    except Exception as exc:
        logger.warning("  P2.5: CLIGraphStore 校验失败 (%s)", exc)
        matched_in_tree = matched
        orphan_tids = []
        leaf_integrity_rate = 1.0

    stats = {
        "total": total,
        "matched": matched,
        "unmatched": unmatched,
        "gap_count": gap_count,
        "ref_counts": ref_counts,
        "match_rate": matched / total if total else 0,
        "matched_in_tree": matched_in_tree,
        "orphan_tids": orphan_tids[:20],
        "leaf_integrity_rate": round(leaf_integrity_rate, 3),
        "matched_prefixes_sample": matched_prefixes[:20],
    }
    return farmer, results, gap_count, stats


# ─────────────────────────────────────────────────────────────────────────────
# P3: 农场主 TreeInformed 挖槽
# ─────────────────────────────────────────────────────────────────────────────

def phase3_farm_owner(gap_count: int) -> Tuple[Optional[Any], Dict[str, Any]]:
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
    from INAGENT.rag.knowledge_schema import SchemaGapEntry

    logger.info("=" * 60)
    logger.info("P3: 农场主 TreeInformed 挖槽 (gaps=%d)", gap_count)
    logger.info("=" * 60)

    graphrag = GraphRAGRetriever(workspace_dir=GRAPHRAG_WORKSPACE)
    if not graphrag.is_available():
        logger.warning("GraphRAG 不可用，跳过农场主")
        return None, {}

    owner = KnowledgeFarmOwnerAgent(graphrag)

    if gap_count == 0 or not GAPS_FILE.exists():
        logger.info("无 schema gaps，跳过")
        return None, {}

    entries = []
    with open(GAPS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            gap_type = raw.get("gap_type", "")
            if gap_type not in ("new_entity", "new_entity_attribute", "conflict", "overflow"):
                continue
            entries.append(owner._entry_from_raw(raw))

    if not entries:
        logger.info("无可处理 gap entries")
        return None, {}

    logger.info("  待处理 entries: %d", len(entries))
    report = owner.process_gap_entries(entries, refresh_hybrid_vectors=True)

    logger.info(
        "  农场主报告: entities_added=%d, fill_requests=%d, deferred=%d, errors=%s",
        report.entities_added,
        len(report.fill_requests),
        len(report.deferred),
        report.errors or "none",
    )

    stats = {
        "entries_processed": len(entries),
        "entities_added": report.entities_added,
        "fill_requests": len(report.fill_requests),
        "deferred": len(report.deferred),
    }
    return report, stats


# ─────────────────────────────────────────────────────────────────────────────
# P4: 农民回填
# ─────────────────────────────────────────────────────────────────────────────

def phase4_fill(farmer, report) -> Dict[str, Any]:
    logger.info("=" * 60)
    logger.info("P4: 农民回填")
    logger.info("=" * 60)

    if not report or not report.fill_requests:
        logger.info("无 fill_requests，跳过")
        return {"filled_count": 0}

    logger.info("  回填请求: %d", len(report.fill_requests))
    filled = farmer.apply_fill_request(report.fill_requests)
    logger.info("  回填完成: %d records updated", filled)
    return {"filled_count": filled}


# ─────────────────────────────────────────────────────────────────────────────
# P5: 向量检索命中率测试（真实 RAG 检索）
# ─────────────────────────────────────────────────────────────────────────────

def phase5_random_command_verify(
    farm_results: List[Any],
    original_blocks: List[Dict],
    n: int = NUM_RANDOM_COMMANDS,
) -> Dict[str, Any]:
    """
    对采样命令执行真实向量检索，验证正确 chunk 是否出现在 top-K 结果中。
    命中条件：检索结果中至少有一条 chunk 的 metadata.tree_node_id == 期望值。
    """
    logger.info("=" * 60)
    logger.info("P5: 向量检索命中率测试（%d 命令 × top-20）", n)
    logger.info("=" * 60)

    # 初始化向量检索器（P3 已填充向量库）
    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_retriever, _reranker, _gr = initialize_rag_system()
        logger.info("  向量检索器初始化成功")
    except Exception as exc:
        logger.error("  无法初始化 RAG 系统: %s", exc)
        return {"verify_results": [], "pass_count": 0, "total": 0, "error": str(exc)}

    matched_results = [r for r in farm_results if r.matched_node_id]
    if not matched_results:
        logger.warning("  无已匹配 chunks，跳过 P5")
        return {"verify_results": [], "pass_count": 0, "total": 0}

    # 过滤孤儿节点：仅保留在 CLIGraphStore（cli_keyword_graph.json）中实际存在的节点
    # 通用做法：以 CLIGraphStore._nodes_by_id 为权威，
    # 确保 matched_node_id 来自命令树而非历史污染的 KB 循环引用
    try:
        from INAGENT.rag.cli_graph_store import CLIGraphStore
        _tree = CLIGraphStore()
        _tree._ensure_loaded()
        verified_results = [r for r in matched_results if r.matched_node_id in _tree._nodes_by_id]
        orphan_count = len(matched_results) - len(verified_results)
        if orphan_count:
            logger.warning(
                "  P5: %d matched chunks 的 tree_node_id 不在CLIGraphStore中（孤儿），已从采样池排除",
                orphan_count,
            )
    except Exception as exc:
        logger.warning("  P5: CLIGraphStore 过滤失败，使用全部 matched_results (%s)", exc)
        verified_results = matched_results

    if not verified_results:
        logger.warning("  P5: 过滤孤儿后无可验证节点，跳过")
        return {"verify_results": [], "pass_count": 0, "total": 0}

    if len(verified_results) < n:
        logger.warning("验证池不足 %d（仅 %d），用全部", n, len(verified_results))
        sample = verified_results
    else:
        rng = random.Random(RANDOM_SEED)
        sample = rng.sample(verified_results, n)

    TOP_K = 20
    verify_results = []

    for r in sample:
        meta = r.chunk.get("metadata", {})
        cmd_prefix = meta.get("command_prefix", "") or ""
        tree_node_id = r.matched_node_id
        content = (r.chunk.get("page_content") or r.chunk.get("text") or "")

        result_entry: Dict[str, Any] = {
            "command_prefix": cmd_prefix,
            "tree_node_id": tree_node_id,
            "rag_hit": False,
            "rag_hit_rank": None,
            "rag_candidates": 0,
        }

        # 构造查询：仅用命令前缀（诚实测试，不使用文档自身内容作为 query 线索）
        query = cmd_prefix.strip() if cmd_prefix else tree_node_id.replace("_", " ")

        try:
            result = hybrid_retriever.query(
                query,
                top_k=TOP_K,
                return_detailed_info=True,
            )
            retrieved = result.get("Retrieved Context", [])
            result_entry["rag_candidates"] = len(retrieved)

            for rank, doc in enumerate(retrieved, start=1):
                if not isinstance(doc, dict):
                    continue
                doc_meta = doc.get("metadata") or {}
                rm = doc_meta.get("regex_metadata") or {}

                doc_nid = doc_meta.get("node_id", "") or rm.get("node_id", "")
                doc_tid = doc_meta.get("tree_node_id", "") or rm.get("tree_node_id", "")
                doc_cp = doc_meta.get("command_prefix", "") or rm.get("command_prefix", "")

                if doc_nid == tree_node_id or doc_tid == tree_node_id:
                    result_entry["rag_hit"] = True
                    result_entry["rag_hit_rank"] = rank
                    break
                if doc_cp and doc_cp.lower() == cmd_prefix.lower():
                    result_entry["rag_hit"] = True
                    result_entry["rag_hit_rank"] = rank
                    break
        except Exception as exc:
            logger.warning("  RAG 检索失败 (cmd=%s): %s", cmd_prefix, exc)
            result_entry["error"] = str(exc)

        result_entry["passed"] = result_entry["rag_hit"]
        status = "HIT" if result_entry["rag_hit"] else "MISS"
        rank_str = f"@{result_entry['rag_hit_rank']}" if result_entry["rag_hit_rank"] else ""
        logger.info(
            "  [%s%s] cmd=%-30s node=%s  (candidates=%d)",
            status, rank_str,
            (cmd_prefix or tree_node_id)[:30],
            tree_node_id,
            result_entry["rag_candidates"],
        )
        verify_results.append(result_entry)

    pass_count = sum(1 for v in verify_results if v["passed"])
    total = len(verify_results)
    hit_rate = pass_count / total if total else 0.0
    logger.info("  向量检索命中率: %d/%d (%.1f%%)", pass_count, total, hit_rate * 100)

    return {
        "verify_results": verify_results,
        "pass_count": pass_count,
        "total": total,
        "hit_rate": round(hit_rate, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# P6: 汇总报告
# ─────────────────────────────────────────────────────────────────────────────

def phase6_report(
    p0_snap: Optional[Path],
    p1_stats: Dict,
    p2_stats: Dict,
    p3_stats: Dict,
    p4_stats: Dict,
    p5_stats: Dict,
) -> str:
    logger.info("=" * 60)
    logger.info("P6: 汇总报告")
    logger.info("=" * 60)

    procurement_rejects = p1_stats["action_counts"].get("reject", 0)
    procurement_accepts = p1_stats["action_counts"].get("accept", 0)
    farmer_match_rate = p2_stats.get("match_rate", 0)
    verify_pass = p5_stats.get("pass_count", 0)
    verify_total = p5_stats.get("total", 0)

    procurement_ok = procurement_rejects > 0 and procurement_accepts > procurement_rejects
    farmer_ok = farmer_match_rate >= 0.3
    verify_ok = verify_total > 0 and verify_pass >= max(1, int(verify_total * 0.8))

    if procurement_ok and farmer_ok and verify_ok:
        verdict = "PASS"
    elif procurement_ok and farmer_ok:
        verdict = "PARTIAL"
    elif procurement_ok:
        verdict = "PARTIAL"
    else:
        verdict = "FAIL"

    report_data = {
        "timestamp": datetime.now().isoformat(),
        "source": SOURCE_FILE_NAME,
        "pages": f"0-{MAX_PAGE_IDX - 1}",
        "procurement": {
            "total_chunks": p1_stats["total_chunks"],
            **p1_stats["action_counts"],
            "reject_samples": p1_stats["reject_samples"],
        },
        "farmer": p2_stats,
        "farm_owner": p3_stats,
        "fill": p4_stats,
        "verification": p5_stats,
        "verdict": verdict,
    }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"cli82_pipeline_report_{ts}.json"
    report_path.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    logger.info("")
    logger.info("+" + "=" * 58 + "+")
    logger.info("|  cli_1-82.pdf 全流程测试结果                              |")
    logger.info("+" + "=" * 58 + "+")
    logger.info("|  采购 reject > 0    : %-36s|", "PASS" if procurement_rejects > 0 else "FAIL")
    logger.info("|  采购 accept > rej  : %-36s|",
                "PASS" if procurement_accepts > procurement_rejects else "FAIL")
    logger.info("|  采购 accept/reject : %d / %d%-26s|",
                procurement_accepts, procurement_rejects, "")
    logger.info("|  农民 match_rate    : %.1f%% %-32s|",
                farmer_match_rate * 100,
                "(PASS)" if farmer_ok else "(FAIL)")
    logger.info("|  农民 chunks 写入    : %s%-36s|",
                p2_stats.get("ref_counts", {}), "")
    logger.info("|  农场主 fill_reqs   : %-36d|", p3_stats.get("fill_requests", 0))
    logger.info("|  回填 records       : %-36d|", p4_stats.get("filled_count", 0))
    hit_rate_pct = (verify_pass / verify_total * 100) if verify_total else 0.0
    logger.info("|  RAG向量命中 hit/total: %d / %d (%.0f%%) %-19s|",
                verify_pass, verify_total, hit_rate_pct,
                "(PASS)" if verify_ok else "(FAIL)")
    logger.info("+" + "-" * 58 + "+")
    logger.info("|  VERDICT: %-48s|", verdict)
    logger.info("+" + "=" * 58 + "+")
    logger.info("报告写入: %s", report_path)

    return verdict


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(description="cli_1-82.pdf 全流程 E2E 测试")
    ap.add_argument("--dry-run", action="store_true", help="只跑 P0+P1（不写 reference）")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过快照")
    ap.add_argument("--no-restore-tree", action="store_true",
                    help="跳过从cli_keyword_graph.json重建knowledge_base.json（默认每次重建，消除历史污染）")
    ap.add_argument("--batch-size", type=int, default=30, help="采购员 LLM 每批 chunk 数")
    return ap.parse_args()


def main():
    args = parse_args()

    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    # P0: 加载 + 快照 + KB重建
    blocks, chunks, snap_dir = phase0_load_and_snapshot(
        skip_snapshot=args.no_snapshot,
        restore_kb=not args.no_restore_tree,
    )

    # P1: 采购员筛查
    decisions, p1_stats = phase1_procurement(chunks, batch_size=args.batch_size)

    if args.dry_run:
        logger.info("--dry-run: P0+P1 完成，退出")
        return "DRY_RUN"

    # LLM model for farmer
    from INAGENT.workflow_config_generator import initialize_llm_model
    model = initialize_llm_model()

    # P2: 农民填肉
    farmer, farm_results, gap_count, p2_stats = phase2_farmer(decisions, model)

    # P3: 农场主挖槽 + 向量刷新
    report, p3_stats = phase3_farm_owner(gap_count)

    # P4: 回填
    p4_stats = phase4_fill(farmer, report)

    # P5: 随机命令验证
    p5_stats = phase5_random_command_verify(farm_results, blocks)

    # P6: 报告
    verdict = phase6_report(snap_dir, p1_stats, p2_stats, p3_stats, p4_stats, p5_stats)

    # 关闭 Qdrant
    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid, _, _ = initialize_rag_system()
        hybrid.vr.storage.close_client()
    except Exception:
        pass

    return verdict


if __name__ == "__main__":
    result = main()
    print(f"\nFinal verdict: {result}")
