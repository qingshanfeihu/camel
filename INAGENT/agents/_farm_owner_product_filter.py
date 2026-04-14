# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""农场主产品过滤扩展模块 - 产品特定过滤与质检反馈处理"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def analyze_product_match(
    chunks: List[Dict[str, Any]],
    *,
    product_signatures_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """分析 chunks 是否匹配目标产品，返回不匹配的 feedback records。

    Args:
        chunks: 待分析的知识块列表
        product_signatures_path: 产品特征配置文件路径（默认 config/product_signatures.json）

    Returns:
        List of feedback records for quality_feedback_inbox.jsonl
    """
    if product_signatures_path is None:
        product_signatures_path = (
            Path(__file__).resolve().parent.parent / "config" / "product_signatures.json"
        )

    if not product_signatures_path.exists():
        logger.warning("产品特征配置文件不存在: %s", product_signatures_path)
        return []

    try:
        signatures = json.loads(product_signatures_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("加载产品特征配置失败: %s", exc)
        return []

    target = signatures.get("target_product", {})
    competitors = signatures.get("competitor_products", [])
    rules = signatures.get("filter_rules", {})
    min_target_score = rules.get("min_target_score", 0.3)
    max_competitor_score = rules.get("max_competitor_score", 0.2)

    feedback_records = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source_file = meta.get("source_file", "")
        block_id = meta.get("block_id", "")

        if not source_file or not block_id:
            continue

        target_score = _calculate_product_score(chunk, target)
        competitor_matches = []
        for comp in competitors:
            comp_score = _calculate_product_score(chunk, comp)
            if comp_score > max_competitor_score:
                competitor_matches.append({
                    "name": comp.get("name", "unknown"),
                    "score": comp_score,
                })

        if target_score < min_target_score and competitor_matches:
            best_match = max(competitor_matches, key=lambda x: x["score"])
            feedback_records.append({
                "schema_version": "1.0",
                "feedback_id": f"product_mismatch_{source_file}_{block_id}",
                "source_file": source_file,
                "block_id": block_id,
                "verdict": "reject",
                "reason_code": "wrong_product",
                "notes": (
                    f"目标产品得分 {target_score:.2f} < {min_target_score}，"
                    f"检测到竞品特征: {best_match['name']} (得分 {best_match['score']:.2f})"
                ),
                "suggested_rule": {
                    "type": "competitor_pattern",
                    "competitor": best_match["name"],
                    "confidence": best_match["score"],
                },
            })

    return feedback_records


def _calculate_product_score(
    chunk: Dict[str, Any], product_def: Dict[str, Any]
) -> float:
    """计算 chunk 与产品定义的匹配度（0.0-1.0）。"""
    meta = chunk.get("metadata", {})
    content = (chunk.get("page_content", "") + " " + meta.get("section_title", "")).lower()

    score = 0.0
    total_weight = 0.0

    # 命令前缀匹配（权重 0.4）
    cmd_prefixes = product_def.get("command_prefixes", [])
    if cmd_prefixes:
        matches = sum(1 for prefix in cmd_prefixes if prefix.lower() in content)
        score += (matches / len(cmd_prefixes)) * 0.4
        total_weight += 0.4

    # 产品模块匹配（权重 0.3）
    modules = product_def.get("product_modules", [])
    if modules:
        matches = sum(1 for mod in modules if mod.lower() in content)
        score += (matches / len(modules)) * 0.3
        total_weight += 0.3

    # 产品关键词匹配（权重 0.3）
    keywords = product_def.get("product_keywords", [])
    if keywords:
        matches = sum(1 for kw in keywords if kw.lower() in content)
        score += (matches / len(keywords)) * 0.3
        total_weight += 0.3

    return score / total_weight if total_weight > 0 else 0.0
