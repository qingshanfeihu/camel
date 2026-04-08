"""
Sapling Pipeline — 采购员→农民→农场主 E2E 验证

数据流：
  1. 从快照恢复小树苗（仅 commandtree 生成的 knowledge_base.json）
  2. 加载 app_1-40.json / cli_1-82.json 原始参考块
  3. 采购员 evaluate_batch → 粗筛 accept/reject
  4. 农民 cultivate_batch (已 accept 块) → FarmResult + schema_gaps
  5. 农民 write_to_reference + emit_schema_gaps
  6. merge_knowledge_base → 合并到小树苗
  7. 农场主 process_gap_entries → FillRequest
  8. 农民 apply_fill_request → 回填
  9. 重建 Qdrant 向量
  10. 混合检索验证：老叶子 + 新叶子 + app 块命中

用法：
    python -m INAGENT.scripts.setup_sapling_pipeline
    python -m INAGENT.scripts.setup_sapling_pipeline --skip-procurement
    python -m INAGENT.scripts.setup_sapling_pipeline --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sapling_pipeline")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
SNAPSHOT_DIR = INAGENT_ROOT / "knowledge_base" / "snapshots" / "pre_ircookie_20260402_222832"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
GRAPHRAG_OUTPUT = INAGENT_ROOT / "graphrag_index" / "output"
GAPS_FILE = INAGENT_ROOT / "knowledge_base" / "logs" / "sapling_schema_gaps.jsonl"
LOGS_DIR = INAGENT_ROOT / "knowledge_base" / "logs"

DOC_LOCAL_REF = INAGENT_ROOT / "knowledge_base" / "doc_local_reference"
CLI_REF = DOC_LOCAL_REF / "cli_1-82.json"
APP_REF = DOC_LOCAL_REF / "app_1-40.json"


# ── 1. 恢复小树苗快照 ──────────────────────────────────────────────────────────

def restore_sapling() -> int:
    snap_ref = SNAPSHOT_DIR / "reference" / "knowledge_base.json"
    if not snap_ref.exists():
        raise FileNotFoundError(f"快照不存在: {snap_ref}")

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

    for f in REFERENCE_DIR.glob("*.json"):
        if f.name in ("commandtree_base.json",):
            continue
        f.unlink()
    for f in REFERENCE_DIR.glob("*.jsonl"):
        f.unlink()

    shutil.copy2(snap_ref, KB_PATH)
    kb_data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    logger.info("小树苗恢复: %d chunks from snapshot", len(kb_data))

    snap_graphrag = SNAPSHOT_DIR / "graphrag_output"
    if snap_graphrag.exists():
        GRAPHRAG_OUTPUT.mkdir(parents=True, exist_ok=True)
        for f in snap_graphrag.glob("*.parquet"):
            shutil.copy2(f, GRAPHRAG_OUTPUT / f.name)
        logger.info("GraphRAG parquets 恢复: %d files", len(list(snap_graphrag.glob("*.parquet"))))

    return len(kb_data)


# ── 2. 加载原始参考块 ──────────────────────────────────────────────────────────

def load_source_blocks(ref_path: Path, limit: int = 0) -> List[Dict]:
    if not ref_path.exists():
        raise FileNotFoundError(f"参考文件不存在: {ref_path}")
    blocks = json.loads(ref_path.read_text(encoding="utf-8"))
    if not isinstance(blocks, list):
        raise ValueError(f"{ref_path.name} 不是 list")

    if limit > 0:
        blocks = blocks[:limit]

    for b in blocks:
        meta = b.get("metadata", {})
        meta.pop("tree_position", None)
        meta.pop("owner_excluded", None)
        meta.pop("tree_node_id", None)
        meta.pop("command_refs", None)

    logger.info("加载 %s: %d blocks%s", ref_path.name, len(blocks),
                f" (limit={limit})" if limit > 0 else "")
    return blocks


# ── 3. 采购员粗筛 ──────────────────────────────────────────────────────────────

def run_procurement(blocks: List[Dict], model, source_file: str) -> Tuple[List, List]:
    from INAGENT.agents.knowledge_procurement_agent import KnowledgeProcurementAgent

    for b in blocks:
        b.setdefault("metadata", {})["source_file"] = source_file

    procurement = KnowledgeProcurementAgent(model=model)
    decisions = procurement.evaluate_batch(blocks)

    accepted = procurement.filter_accepted(decisions)
    log_counts = procurement.write_logs(decisions, log_dir=LOGS_DIR)

    logger.info(
        "采购员 [%s]: total=%d, accept=%d, reject=%d, pending=%d",
        source_file,
        len(decisions),
        log_counts.get("accept", 0),
        log_counts.get("reject", 0),
        log_counts.get("pending_review", 0),
    )
    return accepted, decisions


# ── 4. 农民结构匹配 + 填肉 ────────────────────────────────────────────────────

def run_farmer(
    accepted_blocks: List[Dict],
    decisions_raw: List,
    model,
    source_file: str,
    skip_procurement: bool = False,
):
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
        enrich_chunk_decision_for_farmer,
    )
    from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent

    if skip_procurement:
        decisions = []
        for idx, chunk in enumerate(accepted_blocks):
            decisions.append(ChunkDecision(
                chunk=chunk,
                decision=ProcurementDecision(
                    action="accept",
                    target_kb="product",
                    confidence=0.9,
                    reason="skip-procurement mode",
                ),
                source_file=source_file,
                chunk_index=idx,
            ))
    else:
        decisions = [d for d in decisions_raw if d.decision.action == "accept"]

    decisions = [enrich_chunk_decision_for_farmer(d) for d in decisions]

    farmer = KnowledgeFarmerAgent(model=model)
    logger.info("农民开始 cultivate: %d chunks [%s]", len(decisions), source_file)
    results = farmer.cultivate_batch(decisions)
    logger.info("农民 cultivate 完成: %d results", len(results))

    gap_count_total = sum(len(r.schema_gaps) for r in results)
    matched_count = sum(1 for r in results if r.matched_node_id)
    logger.info(
        "  matched=%d, gaps=%d, enriched_fields=%d",
        matched_count,
        gap_count_total,
        sum(len(r.enriched_fields) for r in results),
    )

    counts = farmer.write_to_reference(results, ref_dir=REFERENCE_DIR, log_dir=LOGS_DIR)
    logger.info("写入 reference/: %s", counts)

    gap_count = farmer.emit_schema_gaps(results, GAPS_FILE)
    logger.info("schema gaps: %d → %s", gap_count, GAPS_FILE.name)

    return farmer, results, gap_count


# ── 5. 合并到小树苗 ──────────────────────────────────────────────────────────

def merge_to_sapling() -> int:
    from INAGENT.data_tools.merge_knowledge_base import merge_knowledge_base
    merge_knowledge_base(REFERENCE_DIR, KB_PATH)
    kb_data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    logger.info("合并完成: knowledge_base.json = %d chunks", len(kb_data))
    return len(kb_data)


# ── 6. 农场主处理 gaps ──────────────────────────────────────────────────────────

def run_farm_owner(gap_count: int):
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    if gap_count == 0:
        logger.info("无 schema gaps，跳过农场主")
        return None

    workspace = INAGENT_ROOT / "graphrag_index"
    graphrag = GraphRAGRetriever(workspace_dir=workspace)
    if not graphrag.is_available():
        logger.warning("GraphRAG 不可用，跳过农场主")
        return None

    owner = KnowledgeFarmOwnerAgent(graphrag)
    logger.info("农场主处理 schema gaps: %d 条", gap_count)
    report = owner.process_gaps(GAPS_FILE)

    logger.info(
        "农场主报告: entities_added=%d, columns=%s, fill_requests=%d, deferred=%d, discarded=%d, errors=%s",
        report.entities_added,
        report.columns_added,
        len(report.fill_requests),
        len(report.deferred),
        report.discarded_count,
        report.errors[:3] if report.errors else "none",
    )

    if report.operation_log:
        op_log_file = GAPS_FILE.parent / "farm_owner_ops.json"
        op_log_file.write_text(
            json.dumps(report.dump_operation_log(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("农场主操作日志: %s (%d ops)", op_log_file, len(report.operation_log))

    if report.tree_mutations:
        mutations_file = GAPS_FILE.parent / "farm_owner_mutations.json"
        mutations_file.write_text(
            json.dumps([m.to_dict() for m in report.tree_mutations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("农场主树变更: %s (%d mutations)", mutations_file, len(report.tree_mutations))

    if report.deferred:
        deferred_file = GAPS_FILE.parent / "farm_owner_deferred.json"
        deferred_file.write_text(
            json.dumps([
                {"entity": d.entity_title, "action": d.action, "tree_level": d.tree_level}
                for d in report.deferred
            ], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.warning("农场主挂起条目: %d 条 (needs_tree_session), 详见 %s",
                        len(report.deferred), deferred_file)

    return owner, report


# ── 7. 农民回填 ──────────────────────────────────────────────────────────────

def run_fill_cycle(farmer, report) -> int:
    if not report or not report.fill_requests:
        logger.info("无 fill_requests，跳过回填")
        return 0

    logger.info("农民执行回填: %d fill_requests", len(report.fill_requests))
    filled = farmer.apply_fill_request(report.fill_requests, ref_dir=REFERENCE_DIR)
    logger.info("回填完成: %d 条", filled)
    return filled


# ── 8. 重建向量索引 ──────────────────────────────────────────────────────────

def rebuild_vectors():
    from INAGENT.workflow_config_generator import refresh_hybrid_vector_index
    logger.info("重建 Qdrant 向量索引...")
    hybrid, reranker, graphrag = refresh_hybrid_vector_index(force=True)
    logger.info("向量索引重建完成")
    return hybrid, reranker, graphrag


# ── 9. 检索验证 ──────────────────────────────────────────────────────────────

OLD_LEAF_QUERIES = [
    {
        "id": "OLD1-slb-virtual",
        "query": "slb virtual http 虚拟服务配置命令语法 port arp",
        "expected_keywords": ["virtual", "http"],
        "description": "老叶子: SLB virtual http",
    },
    {
        "id": "OLD2-health-check",
        "query": "slb health 健康检查 interval timeout 配置",
        "expected_keywords": ["健康检查"],
        "description": "老叶子: SLB health check",
    },
]

CLI_QUERIES = [
    {
        "id": "CLI1-system-service",
        "query": "system service 系统管理命令 配置 系统服务",
        "expected_keywords": ["system", "service"],
        "description": "CLI文档: system service 命令",
    },
    {
        "id": "CLI2-interface",
        "query": "interface 接口配置 网口 端口模式 speed duplex",
        "expected_keywords": ["interface"],
        "description": "CLI文档: 接口配置",
    },
]

APP_QUERIES = [
    {
        "id": "APP1-feature",
        "query": "负载均衡 功能配置 用户界面 操作说明",
        "expected_keywords": ["负载均衡"],
        "description": "APP文档: 负载均衡功能",
    },
    {
        "id": "APP2-management",
        "query": "系统管理 网络配置 设备管理 维护操作",
        "expected_keywords": ["管理"],
        "description": "APP文档: 系统管理",
    },
]

ALL_QUERIES = OLD_LEAF_QUERIES + CLI_QUERIES + APP_QUERIES


def run_retrieval_check(hybrid, reranker, graphrag_retriever) -> List[Dict]:
    logger.info("=" * 60)
    logger.info("混合检索验证")
    logger.info("=" * 60)

    from INAGENT.rag.unified_rag import UnifiedRAGRetriever
    unified = UnifiedRAGRetriever(
        hybrid_retriever=hybrid,
        reranker=reranker,
        graphrag_retriever=graphrag_retriever,
    )

    results = []
    for q in ALL_QUERIES:
        logger.info("--- %s: %s ---", q["id"], q["description"])
        try:
            context_text, constraints, _ = unified.retrieve(
                query=q["query"],
                top_k_retrieval=20,
                top_k_final=8,
                use_graphrag=True,
            )
        except Exception as e:
            logger.error("  检索失败: %s", e)
            results.append({"id": q["id"], "hit": False, "error": str(e)})
            continue

        ctx_lower = context_text.lower()
        found_kw = [kw for kw in q["expected_keywords"] if kw.lower() in ctx_lower]
        missed_kw = [kw for kw in q["expected_keywords"] if kw.lower() not in ctx_lower]
        hit = len(found_kw) >= 1

        results.append({
            "id": q["id"],
            "hit": hit,
            "found": found_kw,
            "missed": missed_kw,
            "context_len": len(context_text),
        })

        status = "HIT" if hit else "MISS"
        logger.info("  %s found=%s missed=%s ctx=%d", status, found_kw, missed_kw, len(context_text))

    logger.info("=" * 60)
    old_hits = sum(1 for r in results if r["id"].startswith("OLD") and r.get("hit"))
    cli_hits = sum(1 for r in results if r["id"].startswith("CLI") and r.get("hit"))
    app_hits = sum(1 for r in results if r["id"].startswith("APP") and r.get("hit"))
    total_hits = sum(1 for r in results if r.get("hit"))

    logger.info(
        "结果: 老叶子 %d/%d  CLI新块 %d/%d  APP新块 %d/%d  总计 %d/%d",
        old_hits, len(OLD_LEAF_QUERIES),
        cli_hits, len(CLI_QUERIES),
        app_hits, len(APP_QUERIES),
        total_hits, len(ALL_QUERIES),
    )
    return results


# ── 10. 质量报告 ─────────────────────────────────────────────────────────────

def print_summary(
    sapling_size: int,
    final_kb_size: int,
    cli_procurement_stats: Optional[Dict],
    app_procurement_stats: Optional[Dict],
    cli_farm_stats: Optional[Tuple],
    app_farm_stats: Optional[Tuple],
    gap_count: int,
    fill_count: int,
    retrieval_results: List[Dict],
):
    WIDTH = 70
    print()
    print("=" * WIDTH)
    print("  Sapling Pipeline 质量报告")
    print("=" * WIDTH)

    print(f"  小树苗基线: {sapling_size} chunks")
    print(f"  最终知识库: {final_kb_size} chunks (+{final_kb_size - sapling_size})")
    print()

    if cli_farm_stats:
        _, results, gc = cli_farm_stats
        matched = sum(1 for r in results if r.matched_node_id)
        print(f"  CLI (cli_1-82): {len(results)} cultivated, {matched} matched, {gc} gaps")
    if app_farm_stats:
        _, results, gc = app_farm_stats
        matched = sum(1 for r in results if r.matched_node_id)
        print(f"  APP (app_1-40): {len(results)} cultivated, {matched} matched, {gc} gaps")

    print(f"  总 schema gaps: {gap_count}")
    print(f"  回填: {fill_count} fill_requests applied")
    print()

    if retrieval_results:
        total = len(retrieval_results)
        hits = sum(1 for r in retrieval_results if r.get("hit"))
        print(f"  检索命中: {hits}/{total} ({hits / total * 100:.0f}%)")
        for r in retrieval_results:
            mark = "OK" if r.get("hit") else "XX"
            print(f"    {mark} {r['id']}: found={r.get('found', [])}")

    print("=" * WIDTH)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Sapling Pipeline E2E")
    ap.add_argument("--skip-procurement", action="store_true",
                    help="跳过采购员，所有块直接 accept")
    ap.add_argument("--dry-run", action="store_true",
                    help="只恢复快照+加载块，不执行 LLM 调用")
    ap.add_argument("--skip-vectors", action="store_true",
                    help="跳过向量重建和检索验证")
    ap.add_argument("--cli-only", action="store_true",
                    help="只处理 CLI 文档")
    ap.add_argument("--app-only", action="store_true",
                    help="只处理 APP 文档")
    ap.add_argument("--limit", type=int, default=0,
                    help="每个文档最多处理 N 块（0=全量）")
    args = ap.parse_args()

    process_cli = not args.app_only
    process_app = not args.cli_only

    logger.info("=" * 60)
    logger.info("Sapling Pipeline: 采购员→农民→农场主 E2E")
    logger.info("=" * 60)

    # 1. 先加载源块（restore_sapling 会清空 reference 目录）
    cli_blocks = load_source_blocks(CLI_REF, limit=args.limit) if process_cli else []
    app_blocks = load_source_blocks(APP_REF, limit=args.limit) if process_app else []

    # 2. 恢复小树苗
    sapling_size = restore_sapling()

    if args.dry_run:
        logger.info("DRY RUN: 加载完成, cli=%d, app=%d, 退出", len(cli_blocks), len(app_blocks))
        return

    # 3. LLM 初始化
    from INAGENT.workflow_config_generator import initialize_llm_model
    model = initialize_llm_model()

    # 清理 gaps 文件
    GAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if GAPS_FILE.exists():
        GAPS_FILE.unlink()

    # 4-5. 采购员 + 农民（CLI）
    cli_farm_stats = None
    cli_farmer = None
    total_gap_count = 0

    if process_cli and cli_blocks:
        if args.skip_procurement:
            cli_accepted = cli_blocks
            cli_decisions = []
        else:
            cli_accepted, cli_decisions = run_procurement(cli_blocks, model, "cli_1-82.pdf")

        cli_farmer, cli_results, cli_gap_count = run_farmer(
            cli_accepted, cli_decisions, model, "cli_1-82.pdf",
            skip_procurement=args.skip_procurement,
        )
        cli_farm_stats = (cli_farmer, cli_results, cli_gap_count)
        total_gap_count += cli_gap_count

    # 4-5. 采购员 + 农民（APP）
    app_farm_stats = None
    app_farmer = None

    if process_app and app_blocks:
        if args.skip_procurement:
            app_accepted = app_blocks
            app_decisions = []
        else:
            app_accepted, app_decisions = run_procurement(app_blocks, model, "app_1-40.pdf")

        app_farmer, app_results, app_gap_count = run_farmer(
            app_accepted, app_decisions, model, "app_1-40.pdf",
            skip_procurement=args.skip_procurement,
        )
        app_farm_stats = (app_farmer, app_results, app_gap_count)
        total_gap_count += app_gap_count

    # 6. 合并到小树苗
    final_kb_size = merge_to_sapling()

    # 7. 农场主处理 gaps
    owner_result = run_farm_owner(total_gap_count)
    if owner_result is not None:
        farm_owner, report = owner_result
    else:
        farm_owner, report = None, None

    # 7.5 农场主对缺少 tree_position 的块进行批量分类
    if farm_owner is not None and report is not None:
        unclassified_chunks = []
        for stats in (cli_farm_stats, app_farm_stats):
            if stats is None:
                continue
            _, results, _ = stats
            for r in results:
                meta = (r.chunk or {}).get("metadata") or {}
                tp = meta.get("tree_position")
                if isinstance(tp, dict) and tp.get("tree_level"):
                    continue
                unclassified_chunks.append(r.chunk)
        if unclassified_chunks:
            classify_reqs = farm_owner.classify_uncovered_chunks(unclassified_chunks, report)
            logger.info("农场主批量分类: %d 未分类块 → %d fill_requests", len(unclassified_chunks), len(classify_reqs))

    # 8. 回填（用第一个可用的 farmer 实例）
    farmer_for_fill = cli_farmer or app_farmer
    fill_count = 0
    if farmer_for_fill:
        fill_count = run_fill_cycle(farmer_for_fill, report)

    # 8.5 回填后重新合并
    if fill_count > 0:
        final_kb_size = merge_to_sapling()

    # 9. 重建向量 + 检索验证
    retrieval_results = []
    if not args.skip_vectors:
        hybrid, reranker, graphrag = rebuild_vectors()
        retrieval_results = run_retrieval_check(hybrid, reranker, graphrag)

    # 10. 质量报告
    print_summary(
        sapling_size=sapling_size,
        final_kb_size=final_kb_size,
        cli_procurement_stats=None,
        app_procurement_stats=None,
        cli_farm_stats=cli_farm_stats,
        app_farm_stats=app_farm_stats,
        gap_count=total_gap_count,
        fill_count=fill_count,
        retrieval_results=retrieval_results,
    )


if __name__ == "__main__":
    main()
