"""relabel_tree_levels — 驱动农场主对全量 KB 重新判定 tree_level

流程：
  1. 加载 knowledge_base.json（不走 overlay，读原始数据）
  2. 规则层（高置信度直接标）：
     - CLI 文档 + CLI graph 命中 → leaf（复用 farmer_link / _infer_tree_level_from_cli）
     - CATEGORY_TO_TREE_LEVEL 映射且已有 confidence ≥ 0.9 → 保留
  3. 结构推断层：_infer_tree_level_from_meta（复用农场主方法）
  4. LLM 层（模糊块）：批量调 _llm_infer_tree_level
  5. 输出 tree_level_overlay.json + 统计报告

用法：
  python -m INAGENT.scripts.relabel_tree_levels [--dry-run] [--batch-size 20]
"""
import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.utils.env_utils import load_inagent_env

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Relabel tree_level for all KB chunks")
    parser.add_argument("--dry-run", action="store_true", help="Only print stats, do not write overlay")
    parser.add_argument("--batch-size", type=int, default=20, help="LLM batch size")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    load_inagent_env()

    kb_dir = Path(__file__).resolve().parent.parent / "knowledge_base" / "reference"
    kb_path = kb_dir / "knowledge_base.json"
    if not kb_path.exists():
        logger.error("knowledge_base.json not found at %s", kb_path)
        return 1

    logger.info("Loading knowledge_base.json ...")
    with open(kb_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)
    logger.info("Loaded %d chunks", len(chunks))

    from INAGENT.rag.knowledge_config import CATEGORY_TO_TREE_LEVEL, TREE_LEVELS
    from INAGENT.rag.cli_graph_store import CLIGraphStore

    cli = CLIGraphStore()
    try:
        cli._ensure_loaded()
        logger.info("CLI graph loaded: %d nodes", len(cli._nodes_by_id))
    except Exception as exc:
        logger.warning("CLI graph not available: %s", exc)
        cli = None

    overlay = {}
    stats = Counter()

    rule_chunks = []
    meta_chunks = []
    llm_chunks = []

    for chunk in chunks:
        meta = chunk.get("metadata", {})
        cid = meta.get("chunk_id") or meta.get("block_id") or meta.get("node_id") or ""
        if not cid:
            stats["no_id"] += 1
            continue

        tp = meta.get("tree_position")
        existing_level = ""
        existing_conf = 0.0
        if isinstance(tp, dict):
            existing_level = tp.get("tree_level", "")
            existing_conf = float(tp.get("confidence", 0))

        if existing_level in TREE_LEVELS and existing_conf >= 0.9:
            overlay[str(cid)] = {
                "tree_level": existing_level,
                "confidence": existing_conf,
                "source": "preserved",
            }
            stats["preserved"] += 1
            continue

        cmd_prefix = meta.get("command_prefix", "").strip()
        if cmd_prefix and cli is not None:
            try:
                exists, _ = cli.command_exists(cmd_prefix)
                if exists:
                    overlay[str(cid)] = {
                        "tree_level": "leaf",
                        "confidence": 0.95,
                        "source": "rule_cli",
                    }
                    stats["rule_cli_leaf"] += 1
                    continue
            except Exception:
                pass

        from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
        inferred = KnowledgeFarmOwnerAgent._infer_tree_level_from_meta(meta)
        if inferred != "unknown":
            overlay[str(cid)] = {
                "tree_level": inferred,
                "confidence": 0.75,
                "source": "meta_structure",
            }
            stats["meta_structure"] += 1
            continue

        doc_cat = meta.get("document_category", "")
        if doc_cat in CATEGORY_TO_TREE_LEVEL:
            level = CATEGORY_TO_TREE_LEVEL[doc_cat]
            overlay[str(cid)] = {
                "tree_level": level,
                "confidence": 0.8,
                "source": "rule_category",
            }
            stats["rule_category"] += 1
            continue

        llm_chunks.append((str(cid), chunk))

    logger.info(
        "Rule pass: preserved=%d, cli_leaf=%d, category=%d, meta=%d | LLM needed=%d",
        stats["preserved"], stats["rule_cli_leaf"], stats["rule_category"],
        stats["meta_structure"], len(llm_chunks),
    )

    if llm_chunks and not args.dry_run:
        logger.info("Starting LLM inference for %d chunks (batch_size=%d) ...",
                     len(llm_chunks), args.batch_size)
        from INAGENT.agents.knowledge_farm_owner_agent import KnowledgeFarmOwnerAgent
        from INAGENT.rag.knowledge_schema import TreeContext

        owner = KnowledgeFarmOwnerAgent()

        for batch_start in range(0, len(llm_chunks), args.batch_size):
            batch = llm_chunks[batch_start:batch_start + args.batch_size]
            for cid, chunk in batch:
                meta = chunk.get("metadata", {})
                pc = chunk.get("page_content", "")
                chunk_meta = dict(meta)
                chunk_meta["_content_preview"] = pc[:300]
                ctx = TreeContext(entity_title=meta.get("section_title", ""))
                try:
                    level = owner._llm_infer_tree_level(ctx, chunk_meta)
                except Exception as exc:
                    logger.warning("LLM inference failed for %s: %s, fallback branch", cid, exc)
                    level = "branch"
                overlay[cid] = {
                    "tree_level": level,
                    "confidence": 0.6,
                    "source": "llm",
                }
                stats["llm"] += 1
            done = min(batch_start + args.batch_size, len(llm_chunks))
            logger.info("LLM progress: %d/%d", done, len(llm_chunks))
    elif llm_chunks:
        for cid, chunk in llm_chunks:
            overlay[cid] = {
                "tree_level": "branch",
                "confidence": 0.3,
                "source": "dry_run_default",
            }
            stats["dry_run_default"] += 1

    level_dist = Counter(v["tree_level"] for v in overlay.values())
    source_dist = Counter(v["source"] for v in overlay.values())

    print("\n" + "=" * 60)
    print("  Tree Level 重打标统计")
    print("=" * 60)
    print(f"\n总 chunks: {len(chunks)}")
    print(f"有 ID 的 chunks: {len(overlay)}")
    print(f"无 ID (跳过): {stats['no_id']}")
    print(f"\n层级分布:")
    for level in ["root", "trunk", "branch", "new_leaf", "leaf"]:
        cnt = level_dist.get(level, 0)
        pct = cnt / len(overlay) * 100 if overlay else 0
        print(f"  {level:10s}: {cnt:6d} ({pct:5.1f}%)")
    print(f"\n来源分布:")
    for src, cnt in source_dist.most_common():
        print(f"  {src:20s}: {cnt:6d}")

    if not args.dry_run:
        overlay_path = kb_dir / "tree_level_overlay.json"
        with open(overlay_path, "w", encoding="utf-8") as f:
            json.dump(overlay, f, ensure_ascii=False, indent=2)
        logger.info("Overlay written to %s (%d entries)", overlay_path, len(overlay))
    else:
        logger.info("[DRY RUN] No overlay written")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
