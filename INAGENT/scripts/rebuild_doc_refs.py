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
rebuild_doc_refs.py — 将 auto_convert 输出的手册 JSON 文档集成进统一 KB

功能：
  1. 扫描 knowledge_base/doc_local_reference/ 目录下所有 *.json 文件
     （auto_convert 的输出源，支持 app.json、spec.json、arch.json 等任意手册）
  2. 规范化为 knowledge_base.json 统一格式，补充 document_category 字段
  3. 构建富文本 page_content：section_title + clean_text + description + function_hierarchy
  4. 向 knowledge_base.json 追加/替换 app/reference doc 块（保留 cli/reference 不动）
  5. 写出独立快照 doc_reference.json 供审查

模式：
  - 默认扫描目录          （无参数）
  - --source <file>        只处理指定 JSON 文件
  - --source-dir <dir>     指定扫描目录（默认 knowledge_base/doc_local_reference/）
  - --dry-run              仅统计不写入
  - --force-rebuild-vec    写 KB 后顺便重建 Qdrant 向量索引

用法:
    python -m INAGENT.scripts.rebuild_doc_refs
    python -m INAGENT.scripts.rebuild_doc_refs --source INAGENT/backup/_rag_backup/doc_local_reference/app.json
    python -m INAGENT.scripts.rebuild_doc_refs --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("rebuild_doc_refs")

_INAGENT = Path(__file__).resolve().parent.parent
_KB_PATH = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"
_DOC_REF_PATH = _INAGENT / "knowledge_base" / "reference" / "doc_reference.json"
_DEFAULT_SOURCE_DIR = _INAGENT / "knowledge_base" / "doc_local_reference"

# Fallback locations when default dir is empty
_FALLBACK_SOURCE_DIRS = [
    _INAGENT / "backup" / "_rag_backup" / "doc_local_reference",
]

# CLI-like suffixes to skip (these belong to cli/reference)
_CLI_SKIP_NAMES = {"cli.json", "knowledge_base.json", "knowledge_base_bak"}

# ──────────────────────────────────────────────────────────────────────────────
# Category inference
# ──────────────────────────────────────────────────────────────────────────────

def _infer_category(source_name: str, meta: Dict[str, Any]) -> str:
    """Infer document_category from file name and metadata hints."""
    name = source_name.lower()

    # Already set — respect it
    existing = (meta.get("document_category") or "").strip()
    if existing:
        return existing

    # File name patterns
    if re.search(r"\bcli\b", name):
        return "cli/reference"
    if re.search(r"\bapp\b", name):
        return "app/reference"
    if re.search(r"\barch(itecture)?\b", name):
        return "architecture/design"
    if re.search(r"\bprd\b", name):
        return "spec/prd"
    if re.search(r"\bspec\b|\bfunc\b", name):
        return "spec/func_spec"
    if re.search(r"\bdesign\b", name):
        return "spec/design"
    if re.search(r"\btest\b|\bcase\b", name):
        return "test/test_list"
    if re.search(r"\brule\b|\breview\b", name):
        return "review/rules"
    if re.search(r"\bbug\b|\bfix\b", name):
        return "review/bug_fix"

    # Metadata hints
    intent = (meta.get("intent") or "").lower()
    if "spec" in intent or "design" in intent:
        return "architecture/design"
    if "test" in intent:
        return "test/test_list"

    # Default: treat as app reference manual
    return "app/reference"


# ──────────────────────────────────────────────────────────────────────────────
# Entry → KB chunk conversion
# ──────────────────────────────────────────────────────────────────────────────

def _build_page_content(source_name: str, entry: Dict[str, Any]) -> str:
    """
    Build rich page_content from auto_convert entry.

    Priority: section_title + clean_text + description + function_hierarchy + required_keywords
    This is the text that gets embedded and BM25-indexed.
    """
    meta = entry.get("metadata", {})
    raw_pc = (entry.get("page_content") or "").strip()
    clean = (meta.get("clean_text") or "").strip()
    title = (meta.get("section_title") or "").strip()
    desc = (meta.get("description") or "").strip()
    fh = (meta.get("function_hierarchy") or "").strip()
    scenario = (meta.get("scenario_id") or "").strip()
    keywords = meta.get("required_keywords") or []
    parent = (meta.get("parent_section") or "").strip()

    lines: List[str] = []

    # 标题行 (section heading)
    if title:
        lines.append(f"[章节] {title}")
    elif clean:
        lines.append(f"[章节] {clean}")
    elif raw_pc:
        lines.append(f"[章节] {raw_pc}")

    # Hierarchy breadcrumb
    if fh:
        lines.append(f"路径: {fh}")

    # Main descriptive text
    if desc and desc.lower() not in ("no description generated.", ""):
        lines.append(f"说明: {desc}")

    # Raw clean text if it contains more content than just the heading
    if clean and clean != title and len(clean) > len(title) + 5:
        lines.append(f"原文: {clean}")
    elif raw_pc and raw_pc != title and raw_pc != clean:
        lines.append(f"原文: {raw_pc}")

    # Scenario
    if scenario and scenario.lower() not in ("unknown", ""):
        lines.append(f"场景: {scenario}")

    # Required keywords (for BM25 boost)
    if keywords:
        kw_str = " | ".join(str(k) for k in keywords[:12])
        lines.append(f"关键词: {kw_str}")

    # Source context
    source_pdf = (meta.get("source_pdf") or source_name).strip()
    source_label = Path(source_pdf).name if source_pdf else source_name
    lines.append(f"来源: {source_label}")

    return "\n".join(lines)


def entry_to_chunk(
    source_name: str,
    entry: Dict[str, Any],
    category: str,
) -> Optional[Dict[str, Any]]:
    """Convert a single auto_convert entry to KB chunk format."""
    meta = entry.get("metadata", {})
    raw_pc = (entry.get("page_content") or "").strip()
    clean = (meta.get("clean_text") or "").strip()
    title = (meta.get("section_title") or "").strip()
    desc = (meta.get("description") or "").strip()

    # Skip entries with no usable content
    if not any([raw_pc, clean, title, desc]):
        return None

    # Skip obviously empty heading-only entries (1-2 word titles, no description)
    combined_text = " ".join([raw_pc, clean, desc])
    if len(combined_text.strip()) < 5:
        return None

    page_content = _build_page_content(source_name, entry)
    if not page_content.strip():
        return None

    # Build output metadata — keep all original fields + add document_category
    out_meta: Dict[str, Any] = {}
    for k, v in meta.items():
        if v is not None:
            out_meta[k] = v

    out_meta["document_category"] = category
    out_meta["source_file"] = source_name

    # Normalize product_module to lowercase for consistent matching
    pm = out_meta.get("product_module", "")
    if pm:
        out_meta["product_module"] = pm  # keep original case for display

    return {"page_content": page_content, "metadata": out_meta}


# ──────────────────────────────────────────────────────────────────────────────
# product_module 章节继承修正
# ──────────────────────────────────────────────────────────────────────────────

_CHAPTER_MODULE_MAP = {
    "高可用": ["高可用", "ha", "active/standby", "active/active", "vrrp",
               "高可靠", "集群", "failover", "故障转移"],
    "SLB": ["slb", "服务器负载均衡", "负载均衡", "real server", "virtual service",
            "虚拟服务", "后台服务", "健康检查"],
    "LLB": ["llb", "链路负载均衡"],
    "安全": ["安全", "acl", "防火墙", "firewall", "nat", "ssl"],
    "基础网络": ["vlan", "接口", "路由", "arp", "dns", "dhcp", "链路聚合"],
}


def _fix_product_module_by_chapter_inheritance(data: List[Dict[str, Any]]) -> int:
    """Fix product_module for blocks that inherit from chapter headings.

    Strategy: scan through blocks in order. When a block has a distinctive
    chapter-level title mapped to a known module, set it as the "current chapter
    module". Subsequent blocks that have generic/wrong product_module AND whose
    parent_section matches the chapter heading (by title or block_id) get
    corrected.

    Returns the number of blocks fixed.
    """
    fixed = 0

    bid_to_entry: Dict[Any, Dict[str, Any]] = {}
    for entry in data:
        bid = entry.get("metadata", {}).get("block_id")
        if bid is not None:
            bid_to_entry[bid] = entry

    title_to_module: Dict[str, str] = {}
    bid_to_module: Dict[Any, str] = {}

    for entry in data:
        m = entry.get("metadata", {})
        title = (m.get("section_title") or "").strip()
        pm = (m.get("product_module") or "").strip()
        bid = m.get("block_id")

        if not title or not pm or pm in ("unknown", "基础网络"):
            continue

        for mod, keywords in _CHAPTER_MODULE_MAP.items():
            title_lower = title.lower()
            if mod == pm and any(kw.lower() in title_lower for kw in keywords):
                title_to_module[title] = pm
                if bid is not None:
                    bid_to_module[bid] = pm
                break

    if not title_to_module and not bid_to_module:
        return 0

    for entry in data:
        m = entry.get("metadata", {})
        current_pm = (m.get("product_module") or "").strip()
        parent = (m.get("parent_section") or "").strip()

        if not parent:
            continue

        inherited_pm = None
        if parent in title_to_module:
            inherited_pm = title_to_module[parent]
        else:
            try:
                parent_bid = int(parent)
                if parent_bid in bid_to_module:
                    inherited_pm = bid_to_module[parent_bid]
            except (ValueError, TypeError):
                pass

        if inherited_pm and current_pm != inherited_pm:
            if current_pm in ("基础网络", "unknown", ""):
                m["product_module"] = inherited_pm
                fh = (m.get("function_hierarchy") or "").strip()
                if fh and fh.startswith("基础网络"):
                    m["function_hierarchy"] = fh.replace("基础网络", inherited_pm, 1)
                fixed += 1

    # Pass 2: forward-scan inheritance for blocks with empty parent_section
    # that sit between two chapter anchors (use most recent chapter module)
    current_chapter_pm: Optional[str] = None
    for entry in data:
        m = entry.get("metadata", {})
        bid = m.get("block_id")
        pm = (m.get("product_module") or "").strip()
        title = (m.get("section_title") or "").strip()

        if bid is not None and bid in bid_to_module:
            current_chapter_pm = bid_to_module[bid]
            continue

        if current_chapter_pm and pm in ("基础网络", "unknown", ""):
            parent = (m.get("parent_section") or "").strip()
            # Only inherit if parent is empty or a bare number or matches
            # a known chapter pattern
            if not parent or parent.isdigit():
                m["product_module"] = current_chapter_pm
                fh = (m.get("function_hierarchy") or "").strip()
                if fh and fh.startswith("基础网络"):
                    m["function_hierarchy"] = fh.replace("基础网络", current_chapter_pm, 1)
                fixed += 1

    if fixed:
        logger.info("  → 章节继承修正 product_module: %d 条", fixed)
    return fixed


# ──────────────────────────────────────────────────────────────────────────────
# File discovery
# ──────────────────────────────────────────────────────────────────────────────

def _discover_source_dir() -> Path:
    """Return the first source directory that contains usable JSON files."""
    if _DEFAULT_SOURCE_DIR.exists():
        files = [f for f in _DEFAULT_SOURCE_DIR.glob("*.json")
                 if f.name not in _CLI_SKIP_NAMES]
        if files:
            return _DEFAULT_SOURCE_DIR
    for fallback in _FALLBACK_SOURCE_DIRS:
        if fallback.exists():
            files = [f for f in fallback.glob("*.json")
                     if f.name not in _CLI_SKIP_NAMES]
            if files:
                logger.info("使用备用目录: %s", fallback)
                return fallback
    return _DEFAULT_SOURCE_DIR  # Return default even if empty


def _collect_json_files(source_dir: Path, source_file: Optional[str]) -> List[Path]:
    if source_file:
        p = Path(source_file)
        if not p.is_absolute():
            p = Path.cwd() / source_file
        if not p.exists():
            raise FileNotFoundError(f"指定文件不存在: {p}")
        return [p]

    if not source_dir.exists():
        logger.warning("源目录不存在: %s", source_dir)
        return []

    files = sorted([
        f for f in source_dir.glob("*.json")
        if f.name not in _CLI_SKIP_NAMES
        and not any(f.name.startswith(skip.rstrip("*")) for skip in _CLI_SKIP_NAMES)
    ])
    logger.info("发现 %d 个文档 JSON: %s", len(files), [f.name for f in files])
    return files


# ──────────────────────────────────────────────────────────────────────────────
# Core builder
# ──────────────────────────────────────────────────────────────────────────────

def build_doc_chunks(source_files: List[Path]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Process all source files and return (chunks, stats)."""
    all_chunks: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}

    for src_path in source_files:
        source_name = src_path.name
        logger.info("处理: %s", source_name)
        try:
            data = json.loads(src_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("读取失败 %s: %s", src_path, exc)
            continue

        if not isinstance(data, list):
            logger.warning("跳过 %s: 不是列表格式", source_name)
            continue

        # Determine category from first entry that has document_category set
        sample_meta = data[0].get("metadata", {}) if data else {}
        category = _infer_category(source_name, sample_meta)
        logger.info("  → 分类: %s, 条目数: %d", category, len(data))

        # Fix product_module via chapter heading inheritance
        _fix_product_module_by_chapter_inheritance(data)

        file_chunks: List[Dict[str, Any]] = []
        skipped = 0
        for entry in data:
            chunk = entry_to_chunk(source_name, entry, category)
            if chunk is None:
                skipped += 1
                continue
            file_chunks.append(chunk)

        logger.info("  → 生成块: %d, 跳过: %d", len(file_chunks), skipped)
        stats[source_name] = {
            "category": category,
            "total": len(data),
            "generated": len(file_chunks),
            "skipped": skipped,
        }
        all_chunks.extend(file_chunks)

    return all_chunks, stats


# ──────────────────────────────────────────────────────────────────────────────
# KB merge
# ──────────────────────────────────────────────────────────────────────────────

def _collect_categories_from_chunks(chunks: List[Dict[str, Any]]) -> Set[str]:
    cats: Set[str] = set()
    for c in chunks:
        cat = c.get("metadata", {}).get("document_category", "")
        if cat:
            cats.add(cat)
    return cats


def merge_into_kb(new_chunks: List[Dict[str, Any]], dry_run: bool = False) -> int:
    """
    Merge new doc chunks into knowledge_base.json.
    Removes old chunks with the same document_category values, then appends new ones.
    CLI chunks (cli/reference) are always preserved.

    Returns number of chunks written.
    """
    if not _KB_PATH.exists():
        existing: List[Dict[str, Any]] = []
        logger.info("knowledge_base.json 不存在，将全新创建")
    else:
        existing = json.loads(_KB_PATH.read_text(encoding="utf-8"))

    # Determine categories to replace
    new_categories = _collect_categories_from_chunks(new_chunks)
    logger.info("将替换的分类: %s", sorted(new_categories))

    # Keep chunks NOT in the replacement categories
    kept = [c for c in existing
            if c.get("metadata", {}).get("document_category") not in new_categories]
    removed = len(existing) - len(kept)
    logger.info("保留: %d 条 (移除旧: %d 条, 新增: %d 条)",
                len(kept), removed, len(new_chunks))

    merged = kept + new_chunks
    logger.info("合并后总计: %d 条", len(merged))

    if dry_run:
        logger.info("[dry-run] 不写入文件")
        return len(new_chunks)

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = _INAGENT / "knowledge_base" / "reference" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if _KB_PATH.exists():
        backup_path = backup_dir / f"knowledge_base_bak_{ts}.json"
        shutil.copy2(_KB_PATH, backup_path)
        logger.info("已备份: %s", backup_path.name)

    _KB_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    logger.info("已写入 knowledge_base.json (%d 条)", len(merged))
    return len(new_chunks)


def write_doc_snapshot(chunks: List[Dict[str, Any]], dry_run: bool = False) -> None:
    """Write standalone doc_reference.json for inspection."""
    if dry_run:
        return
    _DOC_REF_PATH.write_text(
        json.dumps(chunks, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    logger.info("快照已写入: %s (%d 条)", _DOC_REF_PATH.name, len(chunks))


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    from INAGENT.utils.env_utils import load_inagent_env
    load_inagent_env()

    ap = argparse.ArgumentParser(
        description="将 auto_convert 输出的手册 JSON 写入 knowledge_base.json (app/reference)"
    )
    ap.add_argument(
        "--source", type=str, default="",
        help="指定单个 JSON 文件路径"
    )
    ap.add_argument(
        "--source-dir", type=str, default="",
        help="扫描指定目录（默认: knowledge_base/doc_local_reference/）"
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="仅统计输出，不写入任何文件"
    )
    ap.add_argument(
        "--force-rebuild-vec", action="store_true",
        help="写入 KB 后强制重建 Qdrant 向量索引"
    )
    args = ap.parse_args()

    # Determine source directory
    if args.source_dir:
        source_dir = Path(args.source_dir)
    else:
        source_dir = _discover_source_dir()

    source_files = _collect_json_files(source_dir, args.source or None)
    if not source_files:
        logger.error("没有找到可处理的文档 JSON 文件")
        logger.error("请将 auto_convert 输出的 JSON 放到: %s", _DEFAULT_SOURCE_DIR)
        sys.exit(1)

    # Build chunks
    chunks, stats = build_doc_chunks(source_files)
    if not chunks:
        logger.warning("没有生成任何文档块")
        sys.exit(0)

    # Print stats
    print()
    print("=" * 70)
    print("  文档入库统计")
    print("=" * 70)
    for fname, s in stats.items():
        print(f"  {fname}")
        print(f"    分类: {s['category']}  总: {s['total']}  →  块: {s['generated']}  跳过: {s['skipped']}")
    print(f"\n  合计生成: {len(chunks)} 块")
    if args.dry_run:
        print("  [dry-run] 不写入")
    print("=" * 70)

    if args.dry_run:
        return

    # Write snapshot
    write_doc_snapshot(chunks)

    # Merge into KB
    merge_into_kb(chunks, dry_run=False)

    # Optionally rebuild Qdrant
    if args.force_rebuild_vec:
        logger.info("--force-rebuild-vec: 触发 Qdrant 重建")
        try:
            from INAGENT.workflow_config_generator import initialize_rag_system
            initialize_rag_system(force_rebuild_vectors=True)
            logger.info("Qdrant 向量索引重建完成")
        except Exception as exc:
            logger.error("Qdrant 重建失败: %s", exc)
            sys.exit(1)
    else:
        print()
        print("  提示: 运行以下命令重建向量索引以使新内容生效:")
        print("    python -m INAGENT.scripts.rebuild_doc_refs --force-rebuild-vec")
        print("    或在下次 initialize_rag_system(force_rebuild_vectors=True) 时自动触发")


if __name__ == "__main__":
    main()
