"""
增量补丁：为 GraphRAG 索引中未被分配社区的连通分量创建社区 + 社区报告。

原理：
  GraphRAG 默认 use_lcc=True，Leiden 仅对最大连通分量(LCC)聚类，
  导致 39% 的实体（含 health 模块 206 节点子图）无社区分配。

  本脚本利用修改后的 cluster_graph（对非 LCC 连通分量做增量 Leiden），
  重建 communities.parquet，然后仅为新增社区调 LLM 生成 community report，
  追加到 community_reports.parquet。

  对 LCC 节点：社区分配完全不变（零回归）。

用法:
  python INAGENT/scripts/patch_orphan_communities.py [--dry-run]
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("patch_orphan")


OUTPUT_DIR = Path("INAGENT/graphrag_index/output")
SETTINGS_YAML = Path("INAGENT/graphrag_index/settings.yaml")
PROMPT_PATH = Path("INAGENT/graphrag_index/prompts/community_report_graph.txt")


def backup_parquet():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = OUTPUT_DIR / f"_backup_{ts}"
    backup_dir.mkdir(exist_ok=True)
    for name in ("communities.parquet", "community_reports.parquet"):
        src = OUTPUT_DIR / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)
    logger.info("Backup saved to %s", backup_dir)
    return backup_dir


def rebuild_communities() -> tuple[pd.DataFrame, set]:
    """Rebuild communities.parquet using patched cluster_graph. Returns (new_df, new_community_ids)."""
    from graphrag.index.workflows.create_communities import create_communities

    ent = pd.read_parquet(OUTPUT_DIR / "entities.parquet")
    rel = pd.read_parquet(OUTPUT_DIR / "relationships.parquet")

    import yaml
    with open(SETTINGS_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cg = cfg.get("cluster_graph", {})
    max_cluster_size = cg.get("max_cluster_size", 25)
    seed = cg.get("seed", 0xDEADBEEF)

    for _, e in ent.iterrows():
        pass

    new_communities = create_communities(
        ent, rel,
        max_cluster_size=max_cluster_size,
        use_lcc=True,
        seed=seed,
    )

    old_communities = pd.read_parquet(OUTPUT_DIR / "communities.parquet")
    old_ids = set(old_communities["community"].tolist())
    new_ids = set(new_communities["community"].tolist()) - old_ids

    logger.info(
        "Communities: old=%d new_total=%d added=%d",
        len(old_ids), len(set(new_communities["community"].tolist())), len(new_ids),
    )
    return new_communities, new_ids


def build_context_for_community(
    community_id: int,
    communities: pd.DataFrame,
    entities: pd.DataFrame,
    relationships: pd.DataFrame,
) -> str:
    """Build entity+relationship context string for a single community (for LLM summarization)."""
    com_row = communities[communities["community"] == community_id]
    if com_row.empty:
        return ""

    entity_ids = com_row.iloc[0].get("entity_ids", [])
    if not isinstance(entity_ids, list):
        entity_ids = list(entity_ids)

    com_entities = entities[entities["id"].isin(entity_ids)]

    lines = ["Entities\n"]
    lines.append("id,entity,description")
    for idx, (_, e) in enumerate(com_entities.iterrows()):
        title = e.get("title", "")
        etype = e.get("type", "")
        desc = str(e.get("description", ""))
        lines.append(f"{idx},{title} ({etype}),\"{desc}\"")

    entity_titles = set(com_entities["title"].tolist())
    com_rels = relationships[
        relationships["source"].isin(entity_titles) &
        relationships["target"].isin(entity_titles)
    ]

    if len(com_rels) > 0:
        lines.append("\nRelationships\n")
        lines.append("id,source,target,description,weight")
        for idx, (_, r) in enumerate(com_rels.iterrows()):
            src = r.get("source", "")
            tgt = r.get("target", "")
            desc = str(r.get("description", ""))
            w = r.get("weight", 1.0)
            lines.append(f"{idx},{src},{tgt},\"{desc}\",{w}")

    return "\n".join(lines)


async def generate_reports_for_new_communities(
    new_community_ids: set,
    communities: pd.DataFrame,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Call LLM to generate community reports for new communities only."""
    if not new_community_ids:
        logger.info("No new communities to generate reports for.")
        return pd.DataFrame()

    entities = pd.read_parquet(OUTPUT_DIR / "entities.parquet")
    relationships = pd.read_parquet(OUTPUT_DIR / "relationships.parquet")

    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")

    from INAGENT.utils.llm_config import get_llm_config
    llm_cfg = get_llm_config("gateway")

    from openai import OpenAI
    client = OpenAI(
        api_key=llm_cfg["api_key"],
        base_url=llm_cfg["base_url"],
        timeout=llm_cfg.get("timeout", 120),
    )
    model_name = llm_cfg.get("model", "qwen-plus")

    sorted_ids = sorted(new_community_ids)
    logger.info("Generating reports for %d new communities...", len(sorted_ids))

    reports = []
    for cid in sorted_ids:
        context = build_context_for_community(cid, communities, entities, relationships)
        if not context.strip():
            logger.warning("Empty context for community %d, skipping.", cid)
            continue

        com_row = communities[communities["community"] == cid].iloc[0]
        level = int(com_row.get("level", 0))

        if dry_run:
            logger.info("[DRY-RUN] Would generate report for community %d (level=%d, context=%d chars)", cid, level, len(context))
            continue

        user_prompt = prompt_template + "\n\n# Real Data\n\n" + context

        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=0.3,
                max_tokens=4096,
            )
            raw = resp.choices[0].message.content or ""

            report = _parse_report(raw, cid, level)
            reports.append(report)
            logger.info("Community %d: title=%s rank=%.1f", cid, report["title"], report["rank"])
        except Exception as e:
            logger.error("Failed to generate report for community %d: %s", cid, e)

    if not reports:
        return pd.DataFrame()

    return pd.DataFrame(reports)


def _parse_report(raw: str, community_id: int, level: int) -> dict:
    """Parse LLM JSON response into community report dict."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            data = {"title": f"Community {community_id}", "summary": text, "rating": 3.0, "rating_explanation": "", "findings": []}

    title = data.get("title", f"Community {community_id}")
    summary = data.get("summary", "")
    rating = float(data.get("rating", 3.0))
    rating_explanation = data.get("rating_explanation", "")
    findings = data.get("findings", [])

    full_content = f"# {title}\n\n{summary}"
    if findings:
        full_content += "\n\n## Findings\n"
        for f in findings:
            full_content += f"\n### {f.get('summary', '')}\n{f.get('explanation', '')}\n"

    return {
        "community": community_id,
        "level": level,
        "title": title,
        "summary": summary,
        "full_content": full_content,
        "rank": rating,
        "rating_explanation": rating_explanation,
        "findings": findings,
        "full_content_json": json.dumps(data, ensure_ascii=False),
    }


def finalize_and_save(
    new_communities: pd.DataFrame,
    new_reports: pd.DataFrame,
    old_reports: pd.DataFrame,
):
    """Save updated communities.parquet and community_reports.parquet."""
    new_communities.to_parquet(OUTPUT_DIR / "communities.parquet", index=False)
    logger.info("Saved communities.parquet: %d rows", len(new_communities))

    if new_reports.empty:
        logger.info("No new reports to append.")
        return

    new_reports_final = _finalize_new_reports(new_reports, new_communities)
    merged = pd.concat([old_reports, new_reports_final], ignore_index=True)
    merged.to_parquet(OUTPUT_DIR / "community_reports.parquet", index=False)
    logger.info(
        "Saved community_reports.parquet: %d rows (old=%d + new=%d)",
        len(merged), len(old_reports), len(new_reports_final),
    )


def _finalize_new_reports(reports: pd.DataFrame, communities: pd.DataFrame) -> pd.DataFrame:
    """Add metadata columns to new reports, matching existing schema."""
    reports = reports.merge(
        communities.loc[:, ["community", "parent", "children", "size", "period"]],
        on="community",
        how="left",
        copy=False,
    )
    reports["community"] = reports["community"].astype("int32")
    reports["human_readable_id"] = reports["community"].astype("int32")
    reports["id"] = [uuid4().hex for _ in range(len(reports))]
    reports["level"] = reports["level"].astype("int64")
    reports["parent"] = reports["parent"].astype("int32")

    expected_cols = [
        "id", "human_readable_id", "community", "level", "parent", "children",
        "title", "summary", "full_content", "rank", "rating_explanation",
        "findings", "full_content_json", "period", "size",
    ]
    for col in expected_cols:
        if col not in reports.columns:
            reports[col] = None
    return reports[expected_cols]


async def main():
    parser = argparse.ArgumentParser(description="Patch orphan communities into GraphRAG index")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be done")
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent.parent)

    logger.info("=== Orphan Community Patch ===")

    backup_dir = backup_parquet()

    old_reports = pd.read_parquet(OUTPUT_DIR / "community_reports.parquet")
    logger.info("Existing community reports: %d", len(old_reports))

    new_communities, new_ids = rebuild_communities()

    if not new_ids:
        logger.info("No orphan communities found. Nothing to patch.")
        return

    new_reports = await generate_reports_for_new_communities(
        new_ids, new_communities, dry_run=args.dry_run,
    )

    if not args.dry_run:
        finalize_and_save(new_communities, new_reports, old_reports)
        logger.info("=== Patch complete ===")
    else:
        logger.info("=== Dry run complete (no files modified) ===")


if __name__ == "__main__":
    asyncio.run(main())
