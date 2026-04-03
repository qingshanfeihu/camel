"""
ircookie 端到端集成测试 — 农民填肉 + 农场主挖槽 + 混合检索验证

数据流：
  1. 快照当前知识库（reference/ + graphrag/ + Qdrant 指纹）
  2. MinerU ircookie chunks → 模拟采购员 accept → 农民 cultivate_batch（填肉）
  3. 农民 write_to_reference(cli.json) + emit_schema_gaps
  4. merge_knowledge_base: cli.json + 其他 → knowledge_base.json（合并到小树苗）
  5. 农场主 process_gaps → GraphRAG 加实体/加属性列（挖槽）
  6. 农民 apply_fill_request（回填属性）
  7. initialize_rag_system → 重建 Qdrant 向量（小树苗长大）
  8. 混合检索验证：老叶子 + 新叶子都能命中
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ircookie_e2e")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
GRAPHRAG_OUTPUT = INAGENT_ROOT / "graphrag_index" / "output"
SNAPSHOT_ROOT = INAGENT_ROOT / "knowledge_base" / "snapshots"
MINERU_CLI = (
    INAGENT_ROOT / "knowledge_base" / "mineru_output"
    / "cli" / "hybrid_auto" / "cli_content_list.json"
)
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
QDRANT_DIR = Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"
KB_LOGS_DIR = INAGENT_ROOT / "knowledge_base" / "logs"


# ── 1. 快照 — 保存当前"出色的小树苗" ─────────────────────────────────────────

def take_snapshot() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = SNAPSHOT_ROOT / f"pre_ircookie_{ts}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    ref_snap = snap_dir / "reference"
    ref_snap.mkdir()
    for f in REFERENCE_DIR.glob("*.json"):
        shutil.copy2(f, ref_snap / f.name)

    if GRAPHRAG_OUTPUT.exists():
        gr_snap = snap_dir / "graphrag_output"
        gr_snap.mkdir()
        for f in GRAPHRAG_OUTPUT.glob("*.parquet"):
            shutil.copy2(f, gr_snap / f.name)

    kb_items = 0
    if KB_PATH.exists():
        kb_items = len(json.loads(KB_PATH.read_text(encoding="utf-8")))

    logger.info(
        "快照保存: %s (reference=%d files, kb=%d items)",
        snap_dir.name,
        len(list(ref_snap.glob("*.json"))),
        kb_items,
    )
    return snap_dir


# ── 2. 从 MinerU 提取 ircookie chunks（模拟采购员已 accept） ──────────────────

def extract_ircookie_chunks():
    data = json.loads(MINERU_CLI.read_text(encoding="utf-8"))

    chunks = []

    main_parts = [data[i].get("text", "") for i in range(5013, 5016) if data[i].get("text")]
    chunks.append({
        "page_content": "\n".join(main_parts),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "slb mode ircookie",
            "document_category": "cli/reference",
            "page_idx": 257,
        },
    })

    for label, idx_pairs in [
        ("no slb mode ircookie", (5017, 5018)),
        ("show slb mode ircookie", (5019, 5020)),
        ("clear slb mode ircookie", (5021, 5022)),
    ]:
        parts = [data[i].get("text", "") for i in idx_pairs if data[i].get("text")]
        chunks.append({
            "page_content": "\n".join(parts),
            "metadata": {
                "source_file": "cli.pdf",
                "section_title": label,
                "document_category": "cli/reference",
                "page_idx": 258,
            },
        })

    ic_parts = [data[i].get("text", "") for i in range(2867, 2870) if data[i].get("text")]
    chunks.append({
        "page_content": "\n".join(ic_parts),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "slb group method ic",
            "document_category": "cli/reference",
            "page_idx": 142,
        },
    })

    override_text = data[766].get("text", "")
    if override_text:
        chunks.append({
            "page_content": override_text,
            "metadata": {
                "source_file": "cli.pdf",
                "section_title": "system command override",
                "document_category": "cli/reference",
                "page_idx": 33,
            },
        })

    return chunks


# ── 3. 农民填肉 ──────────────────────────────────────────────────────────────

def run_farmer(chunks, model):
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
        enrich_chunk_decision_for_farmer,
    )
    from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent

    decisions = []
    for idx, chunk in enumerate(chunks):
        decisions.append(ChunkDecision(
            chunk=chunk,
            decision=ProcurementDecision(
                action="accept",
                target_kb="product",
                confidence=0.95,
                reason="ircookie e2e — 采购员已审核通过",
            ),
            source_file="cli.pdf",
            chunk_index=5000 + idx,
        ))

    farmer = KnowledgeFarmerAgent(model=model)

    logger.info("=== 农民开始填肉: %d chunks ===", len(decisions))
    decisions = [enrich_chunk_decision_for_farmer(d) for d in decisions]
    results = farmer.cultivate_batch(decisions)
    logger.info("农民富化完成: %d results", len(results))

    for r in results:
        meta = r.chunk.get("metadata", {})
        logger.info(
            "  block=%s tree_node=%s refs=%s gaps=%d enriched=%s",
            r.block_id[:30],
            meta.get("tree_node_id", "-"),
            meta.get("command_refs", []),
            len(r.schema_gaps),
            r.enriched_fields,
        )

    counts = farmer.write_to_reference(results)
    logger.info("写入 reference/: %s", counts)

    gaps_file = INAGENT_ROOT / "knowledge_base" / "logs" / "ircookie_schema_gaps.jsonl"
    gap_count = farmer.emit_schema_gaps(results, gaps_file)
    logger.info("schema gaps: %d 条 → %s", gap_count, gaps_file.name)

    return farmer, results, gaps_file, gap_count


# ── 4. merge — 农民产出追加到小树苗 ────────────────────────────────────────

def merge_to_tree():
    import hashlib

    cli_json = REFERENCE_DIR / "cli.json"
    if not cli_json.exists():
        logger.warning("cli.json 不存在，跳过合并")
        return 0, 0

    kb_data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    kb_before = len(kb_data)

    existing_hashes = set()
    for chunk in kb_data:
        parts = [
            str(chunk.get("page_content", "")),
            str(chunk.get("metadata", {}).get("source_file", "")),
            str(chunk.get("metadata", {}).get("page_idx", "")),
            str(chunk.get("metadata", {}).get("block_id", "")),
        ]
        existing_hashes.add(hashlib.md5("|".join(parts).encode("utf-8")).hexdigest())

    cli_data = json.loads(cli_json.read_text(encoding="utf-8"))
    added = 0
    for chunk in cli_data:
        parts = [
            str(chunk.get("page_content", "")),
            str(chunk.get("metadata", {}).get("source_file", "")),
            str(chunk.get("metadata", {}).get("page_idx", "")),
            str(chunk.get("metadata", {}).get("block_id", "")),
        ]
        h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
        if h not in existing_hashes:
            kb_data.append(chunk)
            existing_hashes.add(h)
            added += 1

    KB_PATH.write_text(json.dumps(kb_data, ensure_ascii=False, indent=2), encoding="utf-8")
    kb_after = len(kb_data)
    logger.info("=== 合并 cli.json → knowledge_base.json ===")
    logger.info("追加 %d 条 (去重后), %d → %d items", added, kb_before, kb_after)
    return kb_before, kb_after


# ── 5. 农场主挖槽 ────────────────────────────────────────────────────────────

def run_farm_owner(gaps_file, gap_count):
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent

    workspace = INAGENT_ROOT / "graphrag_index"
    graphrag = GraphRAGRetriever(workspace_dir=workspace)

    if not graphrag.is_available():
        logger.warning("GraphRAG 不可用，跳过农场主")
        return None

    owner = KnowledgeFarmOwnerAgent(graphrag)

    if gap_count == 0:
        logger.info("无 schema gaps，农场主无需操作")
        return None

    logger.info("=== 农场主处理 schema gaps: %d 条 ===", gap_count)
    report = owner.process_gaps(gaps_file)
    logger.info(
        "农场主报告: +%d entities, +%s columns, %d reembedded, %d fill_requests, errors=%s",
        report.entities_added,
        report.columns_added,
        report.entities_reembedded,
        len(report.fill_requests),
        report.errors or "none",
    )
    return report


# ── 6. 农民回填 ──────────────────────────────────────────────────────────────

def run_fill_cycle(farmer, report):
    if not report or not report.fill_requests:
        logger.info("无 fill_requests，跳过回填")
        return 0

    logger.info("=== 农民执行回填: %d 条 ===", len(report.fill_requests))
    filled = farmer.apply_fill_request(report.fill_requests)
    logger.info("回填完成: %d 条", filled)
    return filled


# ── 7. 重建 Qdrant 向量 ──────────────────────────────────────────────────────

def rebuild_qdrant():
    from INAGENT.workflow_config_generator import initialize_rag_system

    logger.info("=== 重建 Qdrant 向量索引（force_rebuild_vectors） ===")
    hybrid, reranker, graphrag = initialize_rag_system(force_rebuild_vectors=True)

    vec_count = 0
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(path=str(QDRANT_DIR))
        info = client.get_collection("workflow_rag")
        vec_count = info.points_count
    except Exception:
        pass

    logger.info("Qdrant 重建完成: %d vectors", vec_count)
    return hybrid, reranker, graphrag, vec_count


# ── 8. 混合检索验证 — 老叶子 + 新叶子 ────────────────────────────────────────

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
    {
        "id": "OLD3-group-method",
        "query": "slb group method 负载均衡算法 rr lc",
        "expected_keywords": ["group", "method"],
        "description": "老叶子: SLB group method",
    },
]

NEW_LEAF_QUERIES = [
    {
        "id": "NEW1-ircookie-modes",
        "query": "slb mode ircookie 有哪些工作模式 plainname hexname ip enc_name enc_ip",
        "expected_keywords": ["ircookie", "plainname"],
        "description": "新叶子: ircookie 参数模式",
    },
    {
        "id": "NEW2-ircookie-purpose",
        "query": "配置 cookie值 后台服务信息格式 Insert Cookie",
        "expected_keywords": ["cookie"],
        "description": "新叶子: ircookie 功能描述",
    },
    {
        "id": "NEW3-no-show-clear",
        "query": "no slb mode ircookie 删除格式 show clear",
        "expected_keywords": ["cookie"],
        "description": "新叶子: no/show/clear 变体",
    },
    {
        "id": "NEW4-ic-method",
        "query": "slb group method ic 插入cookie算法",
        "expected_keywords": ["ic"],
        "description": "新叶子: ic method 关联",
    },
]

ALL_QUERIES = OLD_LEAF_QUERIES + NEW_LEAF_QUERIES


def run_retrieval_check(hybrid, reranker, graphrag_retriever):
    logger.info("=" * 60)
    logger.info("混合检索验证: 老叶子 + 新叶子")
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
        logger.info("  %s found=%s missed=%s ctx_len=%d", status, found_kw, missed_kw, len(context_text))
        if not hit:
            logger.info("  preview: %s", context_text[:200])

    logger.info("=" * 60)
    old_hits = sum(1 for r in results if r["id"].startswith("OLD") and r.get("hit"))
    new_hits = sum(1 for r in results if r["id"].startswith("NEW") and r.get("hit"))
    total_hits = sum(1 for r in results if r.get("hit"))

    logger.info(
        "结果: 老叶子 %d/%d  新叶子 %d/%d  总计 %d/%d",
        old_hits, len(OLD_LEAF_QUERIES),
        new_hits, len(NEW_LEAF_QUERIES),
        total_hits, len(ALL_QUERIES),
    )
    for r in results:
        mark = "✓" if r.get("hit") else "✗"
        logger.info("  %s %s: found=%s missed=%s", mark, r["id"], r.get("found", []), r.get("missed", []))
    logger.info("=" * 60)

    return results


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 60)
    logger.info("ircookie e2e: 农民填肉 → 合并小树苗 → 农场主挖槽 → 混合检索")
    logger.info("=" * 60)

    # 1. 快照
    snap_dir = take_snapshot()

    # 1.5 清理旧的 farmer 产出和 cache — 确保干净起点
    cli_json = REFERENCE_DIR / "cli.json"
    cli_cache = KB_LOGS_DIR / "cli.farmer_cache.json"
    for p in [cli_json, cli_cache]:
        if p.exists():
            p.unlink()
            logger.info("清理: %s", p.name)

    # 2. 提取 ircookie chunks
    chunks = extract_ircookie_chunks()
    logger.info("%d 个 ircookie chunks 提取完毕", len(chunks))
    for i, c in enumerate(chunks):
        logger.info(
            "  [%d] %s (len=%d)",
            i, c["metadata"].get("section_title", ""), len(c["page_content"]),
        )

    # 3. LLM 初始化
    from INAGENT.workflow_config_generator import initialize_llm_model
    model = initialize_llm_model()

    # 4. 农民填肉
    farmer, results, gaps_file, gap_count = run_farmer(chunks, model)

    # 5. 合并到小树苗
    kb_before, kb_after = merge_to_tree()

    # 6. 农场主挖槽
    report = run_farm_owner(gaps_file, gap_count)

    # 7. 回填
    filled = run_fill_cycle(farmer, report)

    # 8. 重建 Qdrant
    hybrid, reranker, graphrag, vec_count = rebuild_qdrant()

    # 9. 混合检索验证
    retrieval_results = run_retrieval_check(hybrid, reranker, graphrag)

    logger.info("")
    logger.info("===== 端到端测试完成 =====")
    logger.info("快照: %s", snap_dir.name)
    logger.info("小树苗: %d → %d items", kb_before, kb_after)
    logger.info("Qdrant: %d vectors", vec_count)
    if retrieval_results:
        total = len(retrieval_results)
        hits = sum(1 for r in retrieval_results if r.get("hit"))
        logger.info("命中率: %d/%d (%.0f%%)", hits, total, hits / total * 100 if total else 0)


if __name__ == "__main__":
    main()
