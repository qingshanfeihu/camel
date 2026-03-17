# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Postprocess unknown product_module in knowledge_base.json.

Rules:
- Only update chunks with metadata.product_module == "unknown".
- Prefer intent_to_module mapping, then scenario_id_to_module mapping.
- Skip updates when content matches generic_content_keywords.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def load_registry(registry_path: Path) -> Dict[str, Any]:
    if not registry_path.exists():
        logger.warning("Registry not found: %s", registry_path)
        return {}
    try:
        if registry_path.suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except Exception:
                logger.warning("PyYAML not installed; cannot read %s", registry_path)
                return {}
            return yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        return json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load registry %s: %s", registry_path, exc)
        return {}


def _extract_chunks(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("chunks", "documents", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _is_generic(content: str, section_title: str, keywords: List[str]) -> bool:
    haystack = f"{content} {section_title}".lower()
    return any(kw.lower() in haystack for kw in keywords if kw)


def process_knowledge_base(
    kb_path: Path,
    registry_path: Path,
) -> Tuple[int, Dict[str, int]]:
    data = json.loads(kb_path.read_text(encoding="utf-8"))
    chunks = _extract_chunks(data)
    if not chunks:
        logger.warning("No chunks found in %s", kb_path)
        return 0, {}

    registry = load_registry(registry_path)
    intent_map = registry.get("intent_to_module", {}) if isinstance(registry, dict) else {}
    scenario_map = registry.get("scenario_id_to_module", {}) if isinstance(registry, dict) else {}
    generic_keywords = registry.get("generic_content_keywords", []) if isinstance(registry, dict) else []

    updated = 0
    module_counts: Dict[str, int] = {}

    for chunk in chunks:
        metadata = chunk.get("metadata", {}) or {}
        if metadata.get("product_module") != "unknown":
            continue

        content = chunk.get("page_content", "") or ""
        section_title = metadata.get("section_title", "") or ""
        if _is_generic(str(content), str(section_title), list(generic_keywords)):
            continue

        intent = metadata.get("intent")
        scenario_id = metadata.get("scenario_id")

        new_module = None
        if intent and intent in intent_map:
            new_module = intent_map[intent]
        elif scenario_id and scenario_id in scenario_map:
            new_module = scenario_map[scenario_id]

        if new_module:
            metadata["product_module"] = new_module
            chunk["metadata"] = metadata
            updated += 1
            module_counts[new_module] = module_counts.get(new_module, 0) + 1

    if isinstance(data, dict):
        for key in ("chunks", "documents", "data"):
            if isinstance(data.get(key), list):
                data[key] = chunks
                break
    else:
        data = chunks

    kb_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated, module_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Postprocess unknown product_module")
    parser.add_argument(
        "--kb",
        dest="kb_path",
        default=str(Path(__file__).resolve().parents[1] / "knowledge_base" / "reference" / "knowledge_base.json"),
        help="Path to knowledge_base.json",
    )
    parser.add_argument(
        "--registry",
        dest="registry_path",
        default=str(Path(__file__).resolve().parents[1] / "knowledge_base" / "product_modules_registry.json"),
        help="Path to product_modules_registry.json/yaml",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    kb_path = Path(args.kb_path)
    registry_path = Path(args.registry_path)
    if not kb_path.exists():
        logger.error("knowledge_base.json not found: %s", kb_path)
        return 1

    updated, module_counts = process_knowledge_base(kb_path, registry_path)
    logger.info("Updated %d chunks with product_module mapping", updated)
    if module_counts:
        logger.info("Module distribution: %s", module_counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
