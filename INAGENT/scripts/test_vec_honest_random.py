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
向量检索诚实基准测试 — 随机 N 条命令

核心设计原则（从 test_cli82_full_pipeline_e2e.py 的 TB0+P5 提炼）:
  1. [TB0] 以 cli_keyword_graph.json 为唯一权威，运行前可选从图重建 knowledge_base.json，
     消除历史 farmer 污染（默认开启）。
  2. [权威] 采样候选池仅从 CLIGraphStore._nodes_by_id 中实际存在的节点里选取，
     不接受历史遗留的孤儿 tree_node_id。
  3. [诚实] 查询串只用 command_prefix，不在 query 中注入文档自身内容，
     还原真实用户的检索场景。
  4. [只读] 不写入 Qdrant/GraphRAG，不依赖 farmer/procurement pipeline。

用法:
    python -m INAGENT.scripts.test_vec_honest_random
    python -m INAGENT.scripts.test_vec_honest_random --n 20 --top-k 20 --seed 42
    python -m INAGENT.scripts.test_vec_honest_random --no-rebuild   # 跳过 TB0 重建
    python -m INAGENT.scripts.test_vec_honest_random --seed 0       # seed=0 → 纯随机（不固定）
    python -m INAGENT.scripts.test_vec_honest_random --cmd "enable" # 测试单条指定命令
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vec_honest")

_INAGENT = Path(__file__).resolve().parent.parent
_KB_PATH = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"
_GRAPH_PATH = _INAGENT / "knowledge_base" / "cli_keyword_graph.json"
_DEFAULT_N = 10
_DEFAULT_TOP_K = 20
_DEFAULT_SEED = 42


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class VerifyEntry:
    command_prefix: str
    tree_node_id: str
    hit: bool = False
    hit_rank: Optional[int] = None
    candidates: int = 0
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.hit


# ── TB0: 从 CLI 图谱重建干净 KB ───────────────────────────────────────────────

def _rebuild_kb_from_graph() -> int:
    """从 cli_keyword_graph.json 重建 knowledge_base.json，返回叶子数量。"""
    from INAGENT.scripts.rebuild_cli_docs import build_cli_chunks
    chunks = build_cli_chunks(_GRAPH_PATH)
    _KB_PATH.write_text(
        json.dumps(chunks, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    logger.info("TB0: knowledge_base.json 已从 CLI 图谱重建 (%d 叶子节点)", len(chunks))
    return len(chunks)


# ── 采样候选池：KB × CLIGraphStore 交集 ──────────────────────────────────────

def _load_verified_candidates() -> List[Dict[str, str]]:
    """
    从 knowledge_base.json 读取全部 KB 条目，
    过滤掉 tree_node_id 不在 CLIGraphStore._nodes_by_id 中的孤儿，
    返回 [{command_prefix, tree_node_id}, ...] 列表。
    """
    from INAGENT.rag.cli_graph_store import CLIGraphStore
    tree = CLIGraphStore()
    tree._ensure_loaded()
    valid_ids = tree._nodes_by_id

    if not _KB_PATH.exists():
        logger.error("knowledge_base.json 不存在: %s", _KB_PATH)
        return []

    kb: List[Dict] = json.loads(_KB_PATH.read_text(encoding="utf-8"))
    candidates: List[Dict[str, str]] = []
    orphans = 0

    for item in kb:
        meta = item.get("metadata", {})
        # KB 中字段可能是 node_id 或 tree_node_id（rebuild 后为 node_id）
        tid = str(meta.get("tree_node_id") or meta.get("node_id") or "").strip()
        cp = str(meta.get("command_prefix") or "").strip()
        if not tid or not cp:
            continue
        if tid not in valid_ids:
            orphans += 1
            continue
        candidates.append({"command_prefix": cp, "tree_node_id": tid})

    logger.info(
        "候选池: %d 条 (孤儿过滤: %d)", len(candidates), orphans
    )
    return candidates


# ── 核心：诚实向量检索 ────────────────────────────────────────────────────────

def _run_honest_verify(
    candidates: List[Dict[str, str]],
    n: int,
    top_k: int,
    seed: Optional[int],
) -> List[VerifyEntry]:
    """对采样命令执行诚实向量检索，query 只用 command_prefix。"""
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_retriever, _reranker, _gr = initialize_rag_system()
        logger.info("向量检索器初始化成功")
    except Exception as exc:
        logger.error("无法初始化 RAG 系统: %s", exc)
        return []

    if len(candidates) <= n:
        sample = candidates
        logger.warning("候选池仅 %d 条，全部检测（要求 %d）", len(candidates), n)
    else:
        rng = random.Random(seed) if seed else random.Random()
        sample = rng.sample(candidates, n)

    entries: List[VerifyEntry] = []
    for item in sample:
        cp = item["command_prefix"]
        tid = item["tree_node_id"]
        entry = VerifyEntry(command_prefix=cp, tree_node_id=tid)

        # 诚实 query：只用命令前缀，不注入文档内容
        query = cp.strip() if cp.strip() else tid.replace("_", " ")

        try:
            result = hybrid_retriever.query(query, top_k=top_k, return_detailed_info=True)
            retrieved = result.get("Retrieved Context", [])
            entry.candidates = len(retrieved)

            for rank, doc in enumerate(retrieved, start=1):
                if not isinstance(doc, dict):
                    continue
                doc_meta = doc.get("metadata") or {}
                rm = doc_meta.get("regex_metadata") or {}

                doc_nid = doc_meta.get("node_id", "") or rm.get("node_id", "")
                doc_tid = doc_meta.get("tree_node_id", "") or rm.get("tree_node_id", "")
                doc_cp = doc_meta.get("command_prefix", "") or rm.get("command_prefix", "")

                if doc_nid == tid or doc_tid == tid:
                    entry.hit = True
                    entry.hit_rank = rank
                    break
                if doc_cp and doc_cp.lower() == cp.lower():
                    entry.hit = True
                    entry.hit_rank = rank
                    break
        except Exception as exc:
            logger.warning("检索失败 (cmd=%s): %s", cp, exc)
            entry.error = str(exc)

        status = "HIT" if entry.hit else "MISS"
        rank_str = f"@{entry.hit_rank}" if entry.hit_rank else "    "
        logger.info(
            "  [%s%s] cmd=%-35s  node=%s  (top%d返回%d条)",
            status, rank_str,
            cp[:35],
            tid,
            top_k,
            entry.candidates,
        )
        entries.append(entry)

    return entries


# ── 报告 ──────────────────────────────────────────────────────────────────────

def _print_report(entries: List[VerifyEntry], top_k: int, seed: Optional[int]) -> None:
    if not entries:
        logger.warning("无检测结果")
        return

    passed = [e for e in entries if e.passed]
    failed = [e for e in entries if not e.passed]
    total = len(entries)
    hit_rate = len(passed) / total if total else 0.0

    print()
    print("=" * 65)
    print(f"  向量诚实检索基准  —  top-{top_k}  seed={seed}")
    print("=" * 65)
    print(f"  总计: {total}  命中: {len(passed)}  未命中: {len(failed)}")
    print(f"  命中率: {hit_rate:.1%}")

    if passed:
        print()
        print(f"  ── HIT ({len(passed)}) ──")
        for e in sorted(passed, key=lambda x: x.hit_rank or 99):
            print(f"  @rank{e.hit_rank:2d}  {e.command_prefix}")

    if failed:
        print()
        print(f"  ── MISS ({len(failed)}) ──")
        for e in failed:
            err = f"  err={e.error[:40]}" if e.error else ""
            print(f"  {e.command_prefix}{err}")

    print("=" * 65)
    verdict = "PASS" if hit_rate >= 0.8 else ("PARTIAL" if hit_rate >= 0.5 else "FAIL")
    print(f"  判定: {verdict}  ({hit_rate:.1%} vs 基准 80%)")
    print("=" * 65)
    print()


# ── 入口 ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="向量检索诚实基准测试（随机N命令）")
    ap.add_argument("--n", type=int, default=_DEFAULT_N,
                    help=f"随机采样命令数（默认 {_DEFAULT_N}）")
    ap.add_argument("--top-k", type=int, default=_DEFAULT_TOP_K,
                    help=f"检索返回 top-K（默认 {_DEFAULT_TOP_K}）")
    ap.add_argument("--seed", type=int, default=_DEFAULT_SEED,
                    help="随机种子（默认 42；传 0 使用纯随机）")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="跳过 TB0 重建 knowledge_base.json（使用现有版本）")
    ap.add_argument("--cmd", type=str, default="",
                    help="指定单条命令前缀测试（忽略 --n 和随机采样）")
    return ap.parse_args()


def main() -> None:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    args = parse_args()
    seed = args.seed if args.seed != 0 else None

    # TB0: 重建 KB（消除历史污染）
    if not args.no_rebuild:
        _rebuild_kb_from_graph()
    else:
        logger.info("TB0: 已跳过重建（--no-rebuild），使用现有 knowledge_base.json")

    # 构造采样候选池（KB × CLIGraphStore 交集）
    candidates = _load_verified_candidates()
    if not candidates:
        logger.error("候选池为空，无法测试")
        sys.exit(1)

    # 若指定了 --cmd，只测这一条
    if args.cmd:
        cmd_lower = args.cmd.strip().lower()
        matched = [c for c in candidates if c["command_prefix"].lower() == cmd_lower]
        if not matched:
            logger.error("--cmd %r 在候选池中找不到（请检查 command_prefix 拼写）", args.cmd)
            sys.exit(1)
        candidates = matched
        args.n = len(candidates)
        logger.info("--cmd 模式: 仅测试 %r", args.cmd)

    # 执行诚实检索
    entries = _run_honest_verify(candidates, args.n, args.top_k, seed)

    # 打印报告
    _print_report(entries, args.top_k, seed)


if __name__ == "__main__":
    main()
