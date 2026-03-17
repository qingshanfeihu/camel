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
"""Merge all reference/*.json files into a single knowledge_base.json."""
import json
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def merge_knowledge_base(
    reference_dir: Path,
    output_file: Path,
    deduplicate: bool = True,
    logger: Optional[Any] = None,
) -> None:
    """Merge all JSON files under *reference_dir* into one knowledge_base.json.

    Args:
        reference_dir: Directory containing per-source JSON files.
        output_file: Destination path for merged output.
        deduplicate: Whether to skip duplicate chunks (based on content hash).
        logger: Optional logger instance.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    if not reference_dir.exists():
        raise FileNotFoundError(f"Reference directory not found: {reference_dir}")

    all_chunks: List[Dict[str, Any]] = []
    seen_hashes: set = set()

    json_files = sorted(reference_dir.glob("*.json"))
    json_files = [f for f in json_files if f.name != "knowledge_base.json"]

    if not json_files:
        logger.warning("[merge] No JSON files found in %s", reference_dir)
        return

    logger.info("[merge] Found %d JSON files to merge", len(json_files))
    for jf in json_files:
        logger.info("  - %s", jf.name)

    total_loaded = 0
    total_skipped = 0

    for json_file in json_files:
        logger.info("[merge] Processing: %s", json_file.name)
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                chunks = json.load(f)

            if not isinstance(chunks, list):
                logger.warning("[merge] %s is not a list, skipping", json_file.name)
                continue

            file_count = len(chunks)
            total_loaded += file_count
            logger.info("[merge] Loaded %d blocks", file_count)

            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue

                if "metadata" not in chunk:
                    chunk["metadata"] = {}

                if "source_file" not in chunk["metadata"]:
                    chunk["metadata"]["source_file"] = json_file.name

                if deduplicate:
                    dedup_parts = [
                        str(chunk.get("page_content", "")),
                        str(chunk["metadata"].get("source_file", "")),
                        str(chunk["metadata"].get("page_idx", "")),
                        str(chunk["metadata"].get("block_id", "")),
                    ]
                    h = hashlib.md5("|".join(dedup_parts).encode("utf-8")).hexdigest()
                    if h in seen_hashes:
                        total_skipped += 1
                        continue
                    seen_hashes.add(h)

                all_chunks.append(chunk)

        except Exception as e:
            logger.error("[merge] Failed to process %s: %s", json_file.name, e)
            continue

    output_file.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[merge] Writing merged file: %s", output_file)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    # Metadata completeness stats
    stats = {k: 0 for k in ("product_module", "protocol_type", "step_type", "scenario_id")}
    for chunk in all_chunks:
        meta = chunk.get("metadata", {})
        for key in stats:
            if meta.get(key):
                stats[key] += 1

    logger.info("=" * 60)
    logger.info("[merge] Done")
    logger.info("  Total blocks: %d", len(all_chunks))
    logger.info("  Deduplicated: %d", total_skipped)
    logger.info("  Output: %s", output_file)
    logger.info("  Metadata coverage:")
    for key, count in stats.items():
        pct = count / len(all_chunks) * 100 if all_chunks else 0
        logger.info("    %s: %d (%.1f%%)", key, count, pct)
