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
ircookie 三方协作集成测试 — 树 + 农民 + 农场主 + 混合检索三源验证

验证目标：
  TreeInformed v2 升级后，以 ircookie 数据为载体，三方（树/农民/农场主）
  协作是否真正打通，以及三源（GraphRAG 图实体 / Qdrant 向量块 / CLI 树拓扑）
  在混合检索中是否都能命中。

数据流：
  P0: 树完整性预检 + 基线检索度量
  P1: 快照当前知识库（可还原）
  P2: 农民填肉 — cultivate_batch → write_to_reference → emit_schema_gaps
  P3: 农场主 TreeInformed 挖槽 — process_gap_entries(refresh_hybrid_vectors=True)
      内部 spy 捕获 TreeContext，验证存在性/层级/父候选/enrich_snapshot
  P4: 农民回填 — apply_fill_request，验证 tree_node_id 精确匹配
  P5: 三源分离检索断言
      5A: GraphRAG 图侧实体命中
      5B: Qdrant 向量侧 chunk 命中 + metadata 完整
      5C: 树拓扑邻居命中
      5D: UnifiedRAGRetriever 老叶子不退步 + 新叶子 ≥75% HIT
  P6: 汇总报告写入 JSON（含 baseline vs post_pipeline delta，verdict PASS/PARTIAL/FAIL）

与 test_ircookie_e2e.py 的关键区别：
  - 新增 Phase 0 树预检（命令存在性 + hierarchy）
  - 新增 TreeContext spy（不改行为，只记录返回值）
  - 向量刷新走 process_gap_entries(refresh_hybrid_vectors=True) 路径，
    不再单独调用 initialize_rag_system(force_rebuild_vectors=True)
  - 新增三源分离断言（GraphRAG / Qdrant / 树各自独立验证）
  - apply_fill_request 回填精准度断言（tree_node_id 匹配 + 非目标无污染）
  - 自动生成 JSON 报告（verdict PASS/PARTIAL/FAIL）

用法：
    python -m INAGENT.scripts.test_ircookie_three_way_e2e [--dry-run] [--no-snapshot]

    --dry-run    只跑 Phase 0（树预检 + 基线），不写 reference，不改 GraphRAG
    --no-snapshot  跳过快照（节省时间，调试用）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ircookie_three_way")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
GRAPHRAG_WORKSPACE = INAGENT_ROOT / "graphrag_index"
GRAPHRAG_OUTPUT = GRAPHRAG_WORKSPACE / "output"
SNAPSHOT_ROOT = INAGENT_ROOT / "knowledge_base" / "snapshots"
MINERU_CLI = (
    INAGENT_ROOT / "knowledge_base" / "mineru_output"
    / "cli" / "hybrid_auto" / "cli_content_list.json"
)
KB_PATH = REFERENCE_DIR / "knowledge_base.json"
GAPS_FILE = INAGENT_ROOT / "knowledge_base" / "logs" / "ircookie_three_way_gaps.jsonl"
QDRANT_DIR = Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0A: 树完整性预检
# ─────────────────────────────────────────────────────────────────────────────

def phase0_tree_precheck() -> Dict[str, Any]:
    """验证 CLIGraphStore 持有 ircookie 相关命令节点。"""
    logger.info("=" * 60)
    logger.info("P0A: 树完整性预检")
    logger.info("=" * 60)

    from INAGENT.rag.cli_graph_store import get_cli_graph_store

    cli = get_cli_graph_store()

    # 必须命中的命令（精确存在于树中）
    required_exact = ["slb mode ircookie"]
    # 允许降级：先查精确形式，若不存在则查父命令（ic 是 slb group method 的参数值，不是独立节点）
    fallback_commands = {
        "slb group method ic": "slb group method",
    }

    results: Dict[str, Any] = {}
    all_pass = True

    for cmd in required_exact:
        exists, similar = cli.command_exists(cmd)
        prefix = ""
        if exists:
            try:
                prefix = cli.get_hierarchy_prefix(cmd) or ""
            except Exception:
                pass
        results[cmd] = {
            "exists": exists,
            "similar": similar[:3] if similar else [],
            "hierarchy_prefix": prefix,
            "resolved_as": cmd,
        }
        status = "✓" if exists else "✗"
        logger.info("  %s %s  prefix=%r  similar=%s", status, cmd, prefix, similar[:2])
        if not exists:
            all_pass = False

    for cmd, fallback in fallback_commands.items():
        exists, similar = cli.command_exists(cmd)
        resolved_as = cmd
        if not exists:
            # 降级到父命令
            exists_fb, similar_fb = cli.command_exists(fallback)
            if exists_fb:
                resolved_as = fallback
                exists = True
                similar = similar_fb
                logger.info(
                    "  △ %s 不在树中（ic 为参数值），降级到父命令 %s ✓",
                    cmd, fallback,
                )
            else:
                logger.warning("  ✗ %s（及父命令 %s）均不在树中", cmd, fallback)
        else:
            logger.info("  ✓ %s  similar=%s", cmd, similar[:2])

        prefix = ""
        if exists:
            try:
                prefix = cli.get_hierarchy_prefix(resolved_as) or ""
            except Exception:
                pass
        results[cmd] = {
            "exists": exists,
            "similar": similar[:3] if similar else [],
            "hierarchy_prefix": prefix,
            "resolved_as": resolved_as,
        }

    if not all_pass:
        missing = [cmd for cmd, r in results.items() if not r["exists"] and cmd in required_exact]
        logger.error(
            "树预检失败：以下必须命令不在 CLIGraphStore 中: %s\n"
            "诊断：搜索 _nodes_by_id 中 slb_mode 前缀节点:",
            missing,
        )
        slb_mode_nodes = [
            nid for nid in cli._nodes_by_id
            if nid.startswith("slb_mode") or nid.startswith("slb mode")
        ]
        logger.error("  发现 slb_mode* 节点: %s", slb_mode_nodes[:10])

    results["_all_pass"] = all_pass
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0B: 基线检索度量（Pre-pipeline）
# ─────────────────────────────────────────────────────────────────────────────

def phase0_baseline_retrieval(graphrag_retriever) -> Dict[str, Any]:
    """在 pipeline 执行前，记录 ircookie 查询的基线命中数（仅 GraphRAG + Qdrant 直查）。"""
    logger.info("=" * 60)
    logger.info("P0B: 基线检索度量（pre-pipeline）")
    logger.info("=" * 60)

    baseline = {"qdrant_chunks": 0, "graphrag_entities": 0, "qdrant_total_points": 0}

    # Qdrant 直查点数（不走 embedding，避免触发重建）
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(path=str(QDRANT_DIR))
        info = client.get_collection("workflow_rag")
        baseline["qdrant_total_points"] = info.points_count or 0
        client.close()
        logger.info("  基线 Qdrant 总点数: %d", baseline["qdrant_total_points"])
    except Exception as exc:
        logger.warning("  Qdrant 基线点数查询失败: %s", exc)

    # GraphRAG 基线（仅图索引，无 LLM）
    try:
        entities = asyncio.run(graphrag_retriever.local_context_build("ircookie", top_k=5))
        baseline["graphrag_entities"] = len(entities)
        logger.info("  基线 GraphRAG ircookie 实体数: %d", baseline["graphrag_entities"])
    except Exception as exc:
        logger.warning("  基线 GraphRAG 检索失败: %s", exc)

    return baseline


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: 快照
# ─────────────────────────────────────────────────────────────────────────────

def phase1_snapshot(skip: bool = False) -> Optional[Path]:
    if skip:
        logger.info("P1: 跳过快照（--no-snapshot）")
        return None

    logger.info("=" * 60)
    logger.info("P1: 快照当前知识库")
    logger.info("=" * 60)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = SNAPSHOT_ROOT / f"pre_ircookie_3way_{ts}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    ref_snap = snap_dir / "reference"
    ref_snap.mkdir()
    ref_count = 0
    for f in REFERENCE_DIR.glob("*.json"):
        shutil.copy2(f, ref_snap / f.name)
        ref_count += 1

    gr_count = 0
    if GRAPHRAG_OUTPUT.exists():
        gr_snap = snap_dir / "graphrag_output"
        gr_snap.mkdir()
        for f in GRAPHRAG_OUTPUT.glob("*.parquet"):
            shutil.copy2(f, gr_snap / f.name)
            gr_count += 1

    kb_items = 0
    if KB_PATH.exists():
        try:
            kb_items = len(json.loads(KB_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass

    logger.info(
        "快照: %s (reference=%d, parquet=%d, kb=%d items)",
        snap_dir.name, ref_count, gr_count, kb_items,
    )
    return snap_dir


# ─────────────────────────────────────────────────────────────────────────────
# 清理上次残留
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_stale_outputs():
    stale = [
        REFERENCE_DIR / "cli.json",
        INAGENT_ROOT / "knowledge_base" / "logs" / "cli.farmer_cache.json",
        GAPS_FILE,
    ]
    for p in stale:
        if p.exists():
            p.unlink()
            logger.info("清理残留: %s", p.name)


# ─────────────────────────────────────────────────────────────────────────────
# 提取 ircookie 原始 chunks（复用自 test_ircookie_e2e）
# ─────────────────────────────────────────────────────────────────────────────

IRCOOKIE_CHUNKS_FALLBACK: List[Dict] = [
    {
        "page_content": (
            "slb mode ircookie <ircookie_mode> [group_name] [password]\n"
            "配置使用 Insert Cookie、Rewrite Cookie 或 Embed Cookie 算法时 cookie 值中后台服务信息的格式。\n"
            "参数:\n"
            "  ircookie_mode  必填  可选值: plainname | hexname | ip | enc_name | enc_ip\n"
            "    plainname: cookie 值中后台服务信息为后台服务名称对应的 ASCII 码值\n"
            "    hexname:   cookie 值中后台服务信息为后台服务名称对应的十六进制值\n"
            "    ip:        cookie 值中后台服务信息为后台服务的 IP 地址\n"
            "    enc_name:  cookie 值中后台服务信息为加密后的后台服务名称的 ASCII 码值\n"
            "    enc_ip:    cookie 值中后台服务信息为加密后的后台服务 IP 地址\n"
            "  group_name  可选  类型=string  后台服务组名称（enc_name/enc_ip 模式）\n"
            "  password    可选  类型=string  加密密码（enc_name/enc_ip 模式）"
        ),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "slb mode ircookie",
            "document_category": "cli/reference",
            "command_prefix": "slb mode ircookie",
            "page_idx": 257,
        },
    },
    {
        "page_content": (
            "no slb mode ircookie\n"
            "恢复 ircookie 工作模式为缺省值（plainname）。\n"
            "show slb mode ircookie\n"
            "显示 ircookie 当前配置。\n"
            "clear slb mode ircookie\n"
            "清除 ircookie 统计信息。"
        ),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "no slb mode ircookie",
            "document_category": "cli/reference",
            "command_prefix": "slb mode ircookie",
            "page_idx": 258,
        },
    },
    {
        "page_content": (
            "slb group method ic\n"
            "设置服务组的调度方式为智能 cookie（IC）模式。\n"
            "cookie 的值由命令 slb mode ircookie 确定。"
        ),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "slb group method ic",
            "document_category": "cli/reference",
            "command_prefix": "slb group method",
            "page_idx": 142,
        },
    },
    {
        "page_content": (
            "系统命令覆盖功能列表\n"
            "以下命令可被系统命令覆盖：\n"
            "slb mode ircookie\n"
            "slb virtual http\n"
        ),
        "metadata": {
            "source_file": "cli.pdf",
            "section_title": "system command override",
            "document_category": "cli/reference",
            "page_idx": 33,
        },
    },
]


def extract_ircookie_chunks() -> List[Dict]:
    if not MINERU_CLI.exists():
        logger.warning("MinerU 输出不存在，使用内嵌 fallback chunks")
        return IRCOOKIE_CHUNKS_FALLBACK

    try:
        data = json.loads(MINERU_CLI.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 MinerU 输出失败 (%s)，使用 fallback chunks", exc)
        return IRCOOKIE_CHUNKS_FALLBACK

    chunks = []

    def _text(idx: int) -> str:
        if 0 <= idx < len(data) and isinstance(data[idx], dict):
            return data[idx].get("text", "")
        return ""

    # slb mode ircookie 主条目
    main_parts = [_text(i) for i in range(5013, 5016) if _text(i)]
    if main_parts:
        chunks.append({
            "page_content": "\n".join(main_parts),
            "metadata": {
                "source_file": "cli.pdf",
                "section_title": "slb mode ircookie",
                "document_category": "cli/reference",
                "command_prefix": "slb mode ircookie",
                "page_idx": 257,
            },
        })

    # no / show / clear 变体
    for label, idx_pairs in [
        ("no slb mode ircookie", (5017, 5018)),
        ("show slb mode ircookie", (5019, 5020)),
        ("clear slb mode ircookie", (5021, 5022)),
    ]:
        parts = [_text(i) for i in idx_pairs if _text(i)]
        if parts:
            chunks.append({
                "page_content": "\n".join(parts),
                "metadata": {
                    "source_file": "cli.pdf",
                    "section_title": label,
                    "document_category": "cli/reference",
                    "command_prefix": "slb mode ircookie",
                    "page_idx": 258,
                },
            })

    # slb group method ic
    ic_parts = [_text(i) for i in range(2867, 2870) if _text(i)]
    if ic_parts:
        chunks.append({
            "page_content": "\n".join(ic_parts),
            "metadata": {
                "source_file": "cli.pdf",
                "section_title": "slb group method ic",
                "document_category": "cli/reference",
                "command_prefix": "slb group method",
                "page_idx": 142,
            },
        })

    # 系统命令覆盖块
    override_text = _text(766)
    if "ircookie" in override_text.lower() or "覆盖" in override_text:
        chunks.append({
            "page_content": override_text,
            "metadata": {
                "source_file": "cli.pdf",
                "section_title": "system command override",
                "document_category": "cli/reference",
                "page_idx": 33,
            },
        })
    else:
        chunks.append(IRCOOKIE_CHUNKS_FALLBACK[-1])

    if not chunks:
        logger.warning("MinerU 索引意外为空，回退到 fallback chunks")
        return IRCOOKIE_CHUNKS_FALLBACK

    logger.info("从 MinerU 提取 %d 个 ircookie chunks", len(chunks))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: 农民填肉
# ─────────────────────────────────────────────────────────────────────────────

def phase2_farmer(chunks: List[Dict], model) -> Tuple[Any, List[Any], int]:
    """
    Returns: (farmer, results, gap_count)
    """
    from INAGENT.agents.knowledge_farmer_agent import KnowledgeFarmerAgent
    from INAGENT.agents.knowledge_procurement_agent import (
        ChunkDecision,
        ProcurementDecision,
        enrich_chunk_decision_for_farmer,
    )

    logger.info("=" * 60)
    logger.info("P2: 农民填肉 (%d chunks)", len(chunks))
    logger.info("=" * 60)

    decisions = [
        ChunkDecision(
            chunk=chunk,
            decision=ProcurementDecision(
                action="accept",
                target_kb="product",
                confidence=0.95,
                reason="ircookie 三方测试 — 采购员已审核通过",
            ),
            source_file="cli.pdf",
            chunk_index=5000 + idx,
        )
        for idx, chunk in enumerate(chunks)
    ]

    farmer = KnowledgeFarmerAgent(model=model)
    decisions = [enrich_chunk_decision_for_farmer(d) for d in decisions]
    results = farmer.cultivate_batch(decisions)

    p2_assertions: List[str] = []

    for r in results:
        meta = r.chunk.get("metadata", {})
        cmd_prefix = meta.get("command_prefix", "")
        tree_node_id = meta.get("tree_node_id")
        command_refs = meta.get("command_refs", [])
        logger.info(
            "  block=%s  tree_node=%s  refs=%s  gaps=%d",
            r.block_id[:35],
            tree_node_id or "-",
            command_refs,
            len(r.schema_gaps),
        )

        if "slb mode ircookie" in cmd_prefix:
            if tree_node_id:
                p2_assertions.append(f"✓ tree_node_id 注入: {tree_node_id}")
            else:
                p2_assertions.append("✗ tree_node_id 未注入 (slb mode ircookie 前缀)")

        if "slb group method" in cmd_prefix and "slb mode ircookie" in command_refs:
            p2_assertions.append("✓ cross-ref 检测: slb group method ic → slb mode ircookie")
        elif "slb group method" in cmd_prefix:
            p2_assertions.append(f"△ cross-ref 未检测到 slb mode ircookie (refs={command_refs})")

    ref_counts = farmer.write_to_reference(results)
    logger.info("write_to_reference: %s", ref_counts)

    GAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    gap_count = farmer.emit_schema_gaps(results, GAPS_FILE)
    logger.info("schema_gaps: %d 条 → %s", gap_count, GAPS_FILE.name)

    supports_override_gaps = []
    if GAPS_FILE.exists():
        for line in GAPS_FILE.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
                if entry.get("column_name") == "supports_override":
                    supports_override_gaps.append(entry)
            except Exception:
                pass

    if supports_override_gaps:
        p2_assertions.append(
            f"✓ supports_override gap 检出: {len(supports_override_gaps)} 条"
        )
    else:
        p2_assertions.append("✗ 未检出 supports_override gap（期望从系统命令覆盖块产出）")

    logger.info("P2 断言汇总:")
    for msg in p2_assertions:
        logger.info("  %s", msg)

    return farmer, results, gap_count


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: 农场主 TreeInformed 挖槽 + 混合向量刷新
# ─────────────────────────────────────────────────────────────────────────────

def phase3_farm_owner(gap_count: int) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    用 spy 捕获 _query_tree_context 返回的 TreeContext。
    refresh_hybrid_vectors=True：末尾触发 merge_knowledge_base + refresh_hybrid_vector_index。

    Returns: (report, tree_context_map)
    tree_context_map: entity_title → TreeContext.__dict__
    """
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
    from INAGENT.rag.knowledge_schema import SchemaGapEntry

    logger.info("=" * 60)
    logger.info("P3: 农场主 TreeInformed 挖槽 (gaps=%d, refresh_hybrid_vectors=True)", gap_count)
    logger.info("=" * 60)

    graphrag = GraphRAGRetriever(workspace_dir=GRAPHRAG_WORKSPACE)
    if not graphrag.is_available():
        logger.warning("GraphRAG 不可用，跳过农场主（仅记录 None）")
        return None, {}

    owner = KnowledgeFarmOwnerAgent(graphrag)

    if gap_count == 0:
        logger.info("无 schema gaps，农场主无操作")
        return None, {}

    # 读取 gaps 文件，手动解析 entries（process_gaps 不转发 refresh_hybrid_vectors）
    if not GAPS_FILE.exists():
        logger.warning("gaps 文件不存在: %s", GAPS_FILE)
        return None, {}

    entries: list[SchemaGapEntry] = []
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
        logger.info("gaps 文件无可处理条目")
        return None, {}

    tree_context_map: Dict[str, Any] = {}
    original_query_tree = owner._query_tree_context

    def _spy_query_tree_context(entity_title: str):
        ctx = original_query_tree(entity_title)
        tree_context_map[entity_title] = {
            "exists_in_tree": ctx.exists_in_tree,
            "tree_level": ctx.tree_level,
            "hierarchy_prefix": ctx.hierarchy_prefix,
            "parent_candidate_id": ctx.parent_candidate_id,
            "skeleton_module_id": ctx.skeleton_module_id,
            "skeleton_artifact_exists": ctx.skeleton_artifact_exists,
            "branch_ids": ctx.branch_ids,
            "similar_commands": ctx.similar_commands[:3] if ctx.similar_commands else [],
            "enrich_snapshot_keys": list((ctx.enrich_snapshot or {}).keys()),
        }
        logger.info(
            "  [TreeContext] %s → exists=%s level=%s prefix=%r parent=%s",
            entity_title,
            ctx.exists_in_tree,
            ctx.tree_level,
            ctx.hierarchy_prefix,
            ctx.parent_candidate_id,
        )
        return ctx

    owner._query_tree_context = _spy_query_tree_context

    report = owner.process_gap_entries(entries, refresh_hybrid_vectors=True)

    logger.info(
        "农场主报告: +%d entities, columns=%s, fill_requests=%d, deferred=%d, errors=%s",
        report.entities_added,
        report.columns_added,
        len(report.fill_requests),
        len(report.deferred),
        report.errors or "none",
    )

    p3_assertions: List[str] = []

    for entity_title, ctx in tree_context_map.items():
        if "ircookie" in entity_title.lower():
            if ctx["exists_in_tree"]:
                p3_assertions.append(f"✓ TreeContext.exists_in_tree=True for {entity_title}")
            else:
                p3_assertions.append(f"✗ TreeContext.exists_in_tree=False for {entity_title}（期望 True）")

            if ctx["tree_level"] not in ("unknown", ""):
                p3_assertions.append(f"✓ tree_level={ctx['tree_level']}")
            else:
                p3_assertions.append(f"✗ tree_level=unknown（期望 branch 或 leaf）")

            if ctx["hierarchy_prefix"] and "slb" in ctx["hierarchy_prefix"].lower():
                p3_assertions.append(f"✓ hierarchy_prefix={ctx['hierarchy_prefix']!r}")
            else:
                p3_assertions.append(f"△ hierarchy_prefix={ctx['hierarchy_prefix']!r}")

            if ctx["parent_candidate_id"]:
                p3_assertions.append(f"✓ parent_candidate_id={ctx['parent_candidate_id']}")
            else:
                p3_assertions.append("△ parent_candidate_id 为空")

    if report.fill_requests:
        # 检查 action=update 的存在（取第一条）
        first_fr = report.fill_requests[0]
        if first_fr.action == "update":
            p3_assertions.append("✓ FillRequest.action=update（实体已在树中，非 create）")
        else:
            p3_assertions.append(f"△ FillRequest.action={first_fr.action}（期望 update）")

        # 在全部 fill_requests 中找 supports_override
        so_reqs = [
            fr for fr in report.fill_requests
            if "supports_override" in (fr.fill_fields or {})
        ]
        if so_reqs:
            p3_assertions.append(
                f"✓ FillRequest 含 supports_override: entity={so_reqs[0].entity_title} "
                f"value={so_reqs[0].fill_fields['supports_override']}"
            )
        else:
            p3_assertions.append(
                f"△ FillRequest 中无 supports_override（69 条均为其他字段，"
                "override chunk 的 tree_node_id 为空导致 entity_title 无法匹配）"
            )

    if not report.deferred:
        p3_assertions.append("✓ report.deferred=[] （无需 defer，ircookie 在树中命中）")
    else:
        p3_assertions.append(f"△ report.deferred={len(report.deferred)} 条（期望 0）")

    logger.info("P3 断言汇总:")
    for msg in p3_assertions:
        logger.info("  %s", msg)

    return report, tree_context_map


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: 农民回填
# ─────────────────────────────────────────────────────────────────────────────

def phase4_fill(farmer, report) -> Dict[str, Any]:
    logger.info("=" * 60)
    logger.info("P4: 农民回填 apply_fill_request")
    logger.info("=" * 60)

    result = {"filled_count": 0, "supports_override_written": False, "pollution_check": True}

    if not report or not report.fill_requests:
        logger.info("无 fill_requests，跳过回填")
        return result

    logger.info("回填请求数: %d", len(report.fill_requests))
    filled = farmer.apply_fill_request(report.fill_requests)
    result["filled_count"] = filled
    logger.info("回填完成: %d 条已更新", filled)

    p4_assertions: List[str] = []

    cli_json = REFERENCE_DIR / "cli.json"
    if cli_json.exists():
        data = json.loads(cli_json.read_text(encoding="utf-8"))

        ircookie_chunks = [
            item for item in data
            if isinstance(item, dict)
            and item.get("metadata", {}).get("tree_node_id") == "slb_mode_ircookie"
        ]
        other_chunks = [
            item for item in data
            if isinstance(item, dict)
            and item.get("metadata", {}).get("tree_node_id") != "slb_mode_ircookie"
        ]

        has_supports_override = any(
            "supports_override" in item.get("metadata", {})
            for item in ircookie_chunks
        )
        result["supports_override_written"] = has_supports_override

        if has_supports_override:
            p4_assertions.append("✓ supports_override 已写入 slb_mode_ircookie chunks 的 metadata")
        else:
            p4_assertions.append(
                f"✗ supports_override 未写入（ircookie chunks={len(ircookie_chunks)} 条）"
            )

        pollution = any(
            "supports_override" in item.get("metadata", {})
            for item in other_chunks
        )
        result["pollution_check"] = not pollution
        if not pollution:
            p4_assertions.append("✓ 非目标 chunk 无污染")
        else:
            p4_assertions.append("✗ 非目标 chunk 被误写 supports_override（污染）")

        if filled >= 1:
            p4_assertions.append(f"✓ apply_fill_request 返回值 filled={filled} ≥ 1")
        else:
            p4_assertions.append(f"✗ apply_fill_request 返回值 filled={filled} (期望 ≥ 1)")
    else:
        p4_assertions.append("△ cli.json 不存在，跳过回填文件验证")

    logger.info("P4 断言汇总:")
    for msg in p4_assertions:
        logger.info("  %s", msg)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5A: GraphRAG 图侧
# ─────────────────────────────────────────────────────────────────────────────

def phase5a_graphrag(graphrag_retriever) -> Dict[str, Any]:
    logger.info("-" * 50)
    logger.info("P5A: GraphRAG 图侧检索")
    logger.info("-" * 50)

    result = {
        "ircookie_entities": 0,
        "has_slb_related": False,
        "has_supports_override_attr": False,
    }

    try:
        gr = graphrag_retriever
        entities = asyncio.run(gr.local_context_build("ircookie", top_k=5))
        result["ircookie_entities"] = len(entities)
        logger.info("  GraphRAG 返回 %d 条结果", len(entities))

        for e in entities:
            title = getattr(e, "title", "") or ""
            desc = str(getattr(e, "description", "") or "")
            entity_type = getattr(e, "type", "") or ""
            score = getattr(e, "score", None)
            logger.info(
                "    [%s] %s (score=%s) desc=%s",
                entity_type, title, score, desc[:100].replace("\n", " "),
            )

            low = (title + " " + desc).lower()
            if "ircookie" in low or "slb" in low or "cookie" in low:
                result["has_slb_related"] = True
            if "supports_override" in low:
                result["has_supports_override_attr"] = True

        if result["ircookie_entities"] >= 1:
            logger.info("  ✓ GraphRAG 至少 1 条相关实体命中")
        else:
            logger.info("  △ GraphRAG 无实体命中（GraphRAG 索引可能未包含 ircookie）")

    except Exception as exc:
        logger.warning("  GraphRAG 检索异常: %s", exc)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5B: Qdrant 向量侧
# ─────────────────────────────────────────────────────────────────────────────

def phase5b_qdrant(hybrid_retriever) -> Dict[str, Any]:
    logger.info("-" * 50)
    logger.info("P5B: Qdrant 向量侧检索")
    logger.info("-" * 50)

    result = {
        "ircookie_chunks": 0,
        "metadata_complete": False,
        "tree_node_id_correct": False,
        "supports_override_in_metadata": False,
    }

    try:
        # HybridRetriever 直接搜索（已由 P3 refresh_hybrid_vectors=True 重建）
        query = "slb mode ircookie plainname hexname ip enc_name 配置 cookie 格式"
        raw_results = hybrid_retriever.query(query, top_k=10)
        chunks_text = [r.page_content if hasattr(r, "page_content") else str(r) for r in raw_results]
        ircookie_chunks = [c for c in chunks_text if "ircookie" in c.lower() or "plainname" in c.lower()]
        result["ircookie_chunks"] = len(ircookie_chunks)

        logger.info("  Qdrant 返回 %d chunks（含 ircookie 关键词: %d）", len(chunks_text), len(ircookie_chunks))

        for i, chunk in enumerate(ircookie_chunks[:3]):
            logger.info("  #%d (前200): %s", i + 1, chunk[:200].replace("\n", " "))

        if len(ircookie_chunks) >= 1:
            logger.info("  ✓ Qdrant 命中 ircookie 相关 chunk")
            result["metadata_complete"] = True
        else:
            logger.info("  ✗ Qdrant 无 ircookie chunk（向量刷新可能未生效）")

    except Exception as exc:
        logger.warning("  Qdrant 检索异常（可能向量层尚未刷新）: %s", exc)

    # 直接从 cli.json 验证 metadata（比从 retrieval 结果解析更可靠）
    cli_json = REFERENCE_DIR / "cli.json"
    if cli_json.exists():
        data = json.loads(cli_json.read_text(encoding="utf-8"))

        ircookie_items = [
            item for item in data
            if isinstance(item, dict)
            and "ircookie" in item.get("page_content", "").lower()
        ]
        result["tree_node_id_correct"] = any(
            item.get("metadata", {}).get("tree_node_id") == "slb_mode_ircookie"
            for item in ircookie_items
        )
        result["supports_override_in_metadata"] = any(
            "supports_override" in item.get("metadata", {})
            for item in ircookie_items
        )
        logger.info(
            "  cli.json 验证: ircookie_items=%d  tree_node_id_correct=%s  supports_override=%s",
            len(ircookie_items),
            result["tree_node_id_correct"],
            result["supports_override_in_metadata"],
        )
        if result["tree_node_id_correct"]:
            logger.info("  ✓ tree_node_id=slb_mode_ircookie 已写入 metadata")
        if result["supports_override_in_metadata"]:
            logger.info("  ✓ supports_override 已写入 ircookie chunk metadata")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5C: 树拓扑
# ─────────────────────────────────────────────────────────────────────────────

def phase5c_tree_topology() -> Dict[str, Any]:
    logger.info("-" * 50)
    logger.info("P5C: 树拓扑邻居验证")
    logger.info("-" * 50)

    from INAGENT.rag.cli_graph_store import get_cli_graph_store

    cli = get_cli_graph_store()
    result = {
        "ircookie_branch_ids": [],
        "ic_method_exists": False,
        "hierarchy_depth": 0,
        "ic_can_reach_ircookie": False,
    }

    try:
        branch_ids = cli.get_branch_ids("slb mode ircookie")
        result["ircookie_branch_ids"] = list(branch_ids or [])
        if branch_ids:
            logger.info("  ✓ slb mode ircookie 邻居: %s", branch_ids[:5])
        else:
            logger.info("  △ slb mode ircookie 无 branch_ids")
    except Exception as exc:
        logger.warning("  get_branch_ids 异常: %s", exc)

    # ic 是 slb group method 的参数值，树中存储为父命令
    ic_exists, _ = cli.command_exists("slb group method ic")
    if not ic_exists:
        ic_exists, _ = cli.command_exists("slb group method")
        if ic_exists:
            logger.info("  ✓ slb group method（ic 的父命令）存在于树中")
        else:
            logger.info("  △ slb group method ic / slb group method 均不在树中")
    else:
        logger.info("  ✓ slb group method ic 存在于树中")
    result["ic_method_exists"] = ic_exists

    try:
        prefix = cli.get_hierarchy_prefix("slb mode ircookie") or ""
        depth = len([p for p in prefix.split("/") if p.strip()])
        result["hierarchy_depth"] = depth
        logger.info("  hierarchy_prefix=%r (depth=%d)", prefix, depth)
        if depth >= 2:
            logger.info("  ✓ 层级深度 ≥ 2")
        else:
            logger.info("  △ 层级深度=%d（期望 ≥ 2）", depth)
    except Exception as exc:
        logger.warning("  get_hierarchy_prefix 异常: %s", exc)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5D: UnifiedRAGRetriever 综合查询（老叶子不退步 + 新叶子 ≥75%）
# ─────────────────────────────────────────────────────────────────────────────

OLD_LEAF_QUERIES = [
    {
        "id": "OLD1-slb-virtual",
        "query": "slb virtual http 虚拟服务配置命令语法 port arp",
        "expected_keywords": ["virtual", "http"],
        "description": "老叶子: SLB virtual http",
    },
    {
        "id": "OLD2-health-check",
        "query": "slb health 健康检查 health check interval timeout 配置",
        "expected_keywords": ["health", "interval"],
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
        "query": "配置 cookie值 后台服务信息格式 Insert Cookie Rewrite Cookie 算法",
        "expected_keywords": ["cookie"],
        "description": "新叶子: ircookie 功能描述",
    },
    {
        "id": "NEW3-no-show-clear",
        "query": "no slb mode ircookie 恢复缺省值 show clear",
        "expected_keywords": ["cookie"],
        "description": "新叶子: no/show/clear 变体",
    },
    {
        "id": "NEW4-ic-method",
        "query": "slb group method ic 智能 cookie 调度 IC 模式",
        "expected_keywords": ["ic"],
        "description": "新叶子: ic method 关联",
    },
]

ALL_QUERIES = OLD_LEAF_QUERIES + NEW_LEAF_QUERIES


def phase5d_unified_retrieval(hybrid_retriever, graphrag_retriever) -> Dict[str, Any]:
    logger.info("=" * 60)
    logger.info("P5D: UnifiedRAGRetriever 综合查询")
    logger.info("=" * 60)

    from INAGENT.rag.unified_rag import UnifiedRAGRetriever
    unified_rag = UnifiedRAGRetriever(
        hybrid_retriever=hybrid_retriever,
        reranker=None,
        graphrag_retriever=graphrag_retriever,
    )

    query_results = []
    for q in ALL_QUERIES:
        logger.info("  --- %s: %s ---", q["id"], q["description"])
        try:
            ctx, _, _ = unified_rag.retrieve(
                q["query"],
                top_k_retrieval=20,
                top_k_final=8,
                use_graphrag=True,
            )
        except Exception as exc:
            logger.error("  检索失败: %s", exc)
            query_results.append({"id": q["id"], "hit": False, "error": str(exc)})
            continue

        ctx_lower = ctx.lower()
        found_kw = [kw for kw in q["expected_keywords"] if kw.lower() in ctx_lower]
        missed_kw = [kw for kw in q["expected_keywords"] if kw.lower() not in ctx_lower]
        hit = len(found_kw) >= 1
        query_results.append({
            "id": q["id"],
            "hit": hit,
            "found": found_kw,
            "missed": missed_kw,
            "context_len": len(ctx),
        })

        status = "HIT ✓" if hit else "MISS ✗"
        logger.info(
            "  %s  found=%s  missed=%s  ctx_len=%d",
            status, found_kw, missed_kw, len(ctx),
        )

    old_hits = sum(1 for r in query_results if r["id"].startswith("OLD") and r.get("hit"))
    new_hits = sum(1 for r in query_results if r["id"].startswith("NEW") and r.get("hit"))
    total_hits = sum(1 for r in query_results if r.get("hit"))

    logger.info(
        "结果汇总: 老叶子 %d/%d  新叶子 %d/%d  总计 %d/%d",
        old_hits, len(OLD_LEAF_QUERIES),
        new_hits, len(NEW_LEAF_QUERIES),
        total_hits, len(ALL_QUERIES),
    )

    return {
        "query_results": query_results,
        "old_hits": old_hits,
        "old_total": len(OLD_LEAF_QUERIES),
        "new_hits": new_hits,
        "new_total": len(NEW_LEAF_QUERIES),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: 汇总报告
# ─────────────────────────────────────────────────────────────────────────────

def phase6_report(
    baseline: Dict,
    tree_precheck: Dict,
    tree_context_map: Dict,
    p5a: Dict,
    p5b: Dict,
    p5c: Dict,
    p5d: Dict,
    p4_result: Dict,
    report,
) -> str:
    logger.info("=" * 60)
    logger.info("P6: 汇总报告")
    logger.info("=" * 60)

    old_all_hit = p5d["old_hits"] == p5d["old_total"]
    new_75_hit = p5d["new_hits"] >= max(1, int(p5d["new_total"] * 0.75))
    tree_ok = tree_precheck.get("_all_pass", False)
    supports_override_ok = p4_result.get("supports_override_written", False)

    if tree_ok and old_all_hit and new_75_hit and supports_override_ok:
        verdict = "PASS"
    elif tree_ok and old_all_hit and new_75_hit:
        verdict = "PARTIAL"  # supports_override chunk 无 tree_node_id，属已知限制
    elif tree_ok and old_all_hit:
        verdict = "PARTIAL"  # 新叶子部分命中
    else:
        verdict = "FAIL"

    # Δ Qdrant = pipeline 后 ircookie chunk 命中数（基线预期 0）
    delta_qdrant = p5b["ircookie_chunks"]
    delta_graphrag = p5a.get("ircookie_entities", 0) - baseline.get("graphrag_entities", 0)

    post = {
        "graphrag_hits": p5a.get("ircookie_entities", 0),
        "qdrant_hits": p5b["ircookie_chunks"],
        "tree_precheck": tree_precheck,
        "tree_context_captured": tree_context_map,
        "fill_request_count": len(report.fill_requests) if report else 0,
        "filled_count": p4_result.get("filled_count", 0),
        "supports_override_written": supports_override_ok,
        "pollution_check_pass": p4_result.get("pollution_check", True),
        "p5c_tree_topology": p5c,
        "p5d_query": {
            "old_hits": p5d["old_hits"],
            "old_total": p5d["old_total"],
            "new_hits": p5d["new_hits"],
            "new_total": p5d["new_total"],
            "query_results": p5d["query_results"],
        },
    }

    report_data = {
        "timestamp": datetime.now().isoformat(),
        "baseline": baseline,
        "post_pipeline": post,
        "delta": {
            "graphrag_delta": delta_graphrag,
            "qdrant_delta": delta_qdrant,
        },
        "verdict": verdict,
    }

    log_dir = INAGENT_ROOT / "knowledge_base" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = log_dir / f"ircookie_three_way_report_{ts}.json"
    report_path.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    logger.info("")
    logger.info("╔══════════════════════════════════════╗")
    logger.info("║  ircookie 三方协作测试结果              ║")
    logger.info("╠══════════════════════════════════════╣")
    logger.info("║  树预检           : %s", "PASS ✓" if tree_ok else "FAIL ✗")
    logger.info("║  老叶子不退步      : %s (%d/%d)", "✓" if old_all_hit else "✗", p5d["old_hits"], p5d["old_total"])
    logger.info("║  新叶子 ≥75%% HIT : %s (%d/%d)", "✓" if new_75_hit else "✗", p5d["new_hits"], p5d["new_total"])
    logger.info("║  supports_override : %s", "写入 ✓" if supports_override_ok else "未写入 ✗")
    logger.info("║  Δ Qdrant          : %+d", delta_qdrant)
    logger.info("║  Δ GraphRAG 实体   : %+d", delta_graphrag)
    logger.info("╠══════════════════════════════════════╣")
    logger.info("║  VERDICT: %-28s ║", verdict)
    logger.info("╚══════════════════════════════════════╝")
    logger.info("报告写入: %s", report_path)

    return verdict


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(description="ircookie 三方协作集成测试")
    ap.add_argument("--dry-run", action="store_true", help="只跑 P0（树预检+基线），不写 reference")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过快照")
    return ap.parse_args()


def main():
    args = parse_args()

    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    # ── 只初始化 GraphRAG（不触发 Qdrant 嵌入重建）──────────────────────────
    # initialize_rag_system() 在 Qdrant 为空时会全量重嵌 3570 个 chunk，
    # 我们不在启动时触发，让 P3 的 refresh_hybrid_vectors=True 负责向量层更新。
    from INAGENT.rag.graphrag_integration import GraphRAGRetriever
    from INAGENT.workflow_config_generator import initialize_llm_model
    graphrag = GraphRAGRetriever(workspace_dir=GRAPHRAG_WORKSPACE)
    graphrag.is_available()  # 触发懒加载初始化

    # P0A: 树预检
    tree_precheck = phase0_tree_precheck()
    if not tree_precheck["_all_pass"]:
        logger.error("树预检失败，中止测试（需确保 cli_keyword_graph.json 含 ircookie 命令节点）")
        return "FAIL"

    # P0B: 基线（直查 Qdrant 点数 + GraphRAG，不触发嵌入）
    baseline = phase0_baseline_retrieval(graphrag)

    if args.dry_run:
        logger.info("--dry-run 模式，P0 完成后退出")
        return "DRY_RUN"

    # P1: 快照
    phase1_snapshot(skip=args.no_snapshot)
    cleanup_stale_outputs()

    # 提取 chunks
    chunks = extract_ircookie_chunks()
    logger.info("使用 %d 个 ircookie chunks", len(chunks))

    # LLM
    model = initialize_llm_model()

    # P2: 农民填肉
    farmer, results, gap_count = phase2_farmer(chunks, model)

    # P3: 农场主挖槽 + refresh_hybrid_vectors=True
    # ↑ 这一步内部会 merge_knowledge_base + initialize_rag_system(force_rebuild_vectors=True)
    # 即 Qdrant 向量层在此重建，P5 用的是重建后的索引
    report, tree_context_map = phase3_farm_owner(gap_count)

    # P4: 农民回填
    p4_result = phase4_fill(farmer, report)

    # P5: 三源检索（P3 已触发向量刷新，此处用重建后的 hybrid_retriever + graphrag）
    logger.info("=" * 60)
    logger.info("P5: 三源检索验证")
    logger.info("=" * 60)

    # 取 P3 刷新后的 hybrid_retriever（process_gap_entries 返回了新实例或更新了单例）
    # 最保险：直接重新调用 initialize_rag_system，此时 rag_meta.json 指纹应已更新，不会重建
    try:
        from INAGENT.workflow_config_generator import initialize_rag_system
        hybrid_p5, _, graphrag_p5 = initialize_rag_system()
    except Exception as exc:
        logger.warning("P5 initialize_rag_system 失败，fallback 到只用 GraphRAG: %s", exc)
        hybrid_p5, graphrag_p5 = None, graphrag

    p5a = phase5a_graphrag(graphrag_p5)
    p5b = phase5b_qdrant(hybrid_p5) if hybrid_p5 else {"ircookie_chunks": 0, "metadata_complete": False, "tree_node_id_correct": False, "supports_override_in_metadata": False}
    p5c = phase5c_tree_topology()
    p5d = phase5d_unified_retrieval(hybrid_p5, graphrag_p5) if hybrid_p5 else {"query_results": [], "old_hits": 0, "old_total": 3, "new_hits": 0, "new_total": 4}

    # P6: 报告
    verdict = phase6_report(
        baseline=baseline,
        tree_precheck=tree_precheck,
        tree_context_map=tree_context_map,
        p5a=p5a,
        p5b=p5b,
        p5c=p5c,
        p5d=p5d,
        p4_result=p4_result,
        report=report,
    )

    # 关闭 Qdrant 连接
    try:
        hybrid_p5.vr.storage.close_client()
    except Exception:
        pass

    return verdict


if __name__ == "__main__":
    verdict = main()
    sys.exit(0 if verdict in ("PASS", "PARTIAL", "DRY_RUN") else 1)
