#!/usr/bin/env python3
"""
rebuild_cli_docs.py — 用 cli_keyword_graph.json 的结构化叶子节点
替换 knowledge_base.json 中原有的低质量 MinerU cli/reference 文档块

步骤：
1. 备份现有 knowledge_base.json
2. 从 cli_keyword_graph.json 生成结构化 CLI 文档块
3. 过滤掉旧的 cli/reference 块，注入新块
4. 写入新 knowledge_base.json

用法：
    python INAGENT/scripts/rebuild_cli_docs.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_INAGENT = Path(__file__).resolve().parent.parent
_GRAPH_PATH = _INAGENT / "knowledge_base" / "cli_keyword_graph.json"
_KB_PATH    = _INAGENT / "knowledge_base" / "reference" / "knowledge_base.json"


# ── 模块根推断 ────────────────────────────────────────────────────

def build_root_map(graph: dict) -> Dict[str, str]:
    """从 parent_of 边向上导航，返回 cmd_id → root_id 映射。

    root_id 是无父节点的顶层命令 id（如 slb, aaa, ip...），
    用于 product_module 字段。
    """
    child_to_parent: Dict[str, str] = {}
    for e in graph.get("edges", []):
        if e.get("type") == "parent_of":
            child_to_parent[e["target"]] = e["source"]

    root_cache: Dict[str, str] = {}

    def get_root_id(nid: str, depth: int = 0) -> str:
        if nid in root_cache:
            return root_cache[nid]
        if depth > 20:
            return nid
        parent = child_to_parent.get(nid)
        if not parent or parent == nid:
            root_cache[nid] = nid
            return nid
        root_id = get_root_id(parent, depth + 1)
        root_cache[nid] = root_id
        return root_id

    result: Dict[str, str] = {}
    for node in graph.get("nodes", []):
        nid = node.get("id", "")
        if nid and node.get("type") in ("command", "operation_command"):
            result[nid] = get_root_id(nid)
    return result


# ── 节点 → 文档块 ─────────────────────────────────────────────────

def _fmt_params(params: List[Dict[str, Any]]) -> str:
    """格式化参数列表为可读文本。"""
    lines = []
    for p in params:
        req  = "必填" if p.get("required") else "可选"
        typ  = p.get("type", "string")
        name = p.get("name", "?")
        bra  = "<>" if p.get("required") else "[]"
        line = f"  {bra[0]}{name}{bra[1]}  {req}  类型={typ}"
        if p.get("values"):
            line += "  可选值: " + "/".join(str(v) for v in p["values"])
        if p.get("default"):
            line += f"  默认={p['default']}"
        if p.get("constraint"):
            line += f"  范围={p['constraint']}"
        if p.get("description"):
            line += f"  # {p['description']}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (无参数)"


def node_to_chunk(
    node: dict,
    root_id: str,
) -> Optional[Dict[str, Any]]:
    """将单个命令节点转换为 knowledge_base.json 格式的文档块。

    只处理有 XML 数据的节点（有 func 或 help_string），
    其他孤立节点跳过（返回 None）。
    """
    nid   = node.get("id", "")
    label = node.get("label", nid.replace("_", " "))
    desc  = node.get("description", label)
    full  = node.get("full_syntax") or desc
    help_ = node.get("help_string", "")
    func  = node.get("func", "")
    scope = node.get("scope", ["global"])
    ops   = node.get("operations", {})
    params = node.get("parameters", [])

    # 只导出 XML 匹配的节点：必须有 func 或 help_string
    if not func and not help_:
        return None

    # ── 确定是否是 set 命令或变体 ──────────────────────────────────
    _OP_PREFIXES = ("no_", "show_", "clear_", "display_")
    is_variant = any(nid.startswith(p) for p in _OP_PREFIXES)

    # ── 构建 page_content ──────────────────────────────────────────
    lines: List[str] = []

    # 标题行（用于 BM25/GraphRAG 关键词命中）
    lines.append(f"[命令] {label}")
    if help_:
        lines.append(f"[说明] {help_}")
    lines.append(f"语法: {full}")

    if params:
        lines.append("参数:")
        lines.append(_fmt_params(params))

    scope_str = " / ".join(scope)
    lines.append(f"适用范围: {scope_str}")

    # 操作变体列表（仅在 set 基本命令上列出）
    if not is_variant and len(ops) > 1:
        op_names = " / ".join(sorted(ops.keys()))
        lines.append(f"相关操作: {op_names}")

    if func:
        lines.append(f"内部函数: {func}")

    page_content = "\n".join(lines)

    # ── 构建 metadata ─────────────────────────────────────────────
    # 变体命令（operation_command）的 parent_of 边指向 clear/no/show 而非原始模块，
    # build_root_map() 无法正确追溯；改用图谱节点上直接标注的 actual_module。
    if is_variant:
        actual = node.get("actual_module", "") or ""
        effective_module = actual if actual else root_id
    else:
        effective_module = root_id

    meta: Dict[str, Any] = {
        "document_category": "cli/reference",
        "source_file": _GRAPH_PATH.name,
        "product_module": effective_module,
        "command_prefix": label,
        "scope": ",".join(scope),
        "node_id": nid,
        "is_variant": is_variant,
    }
    if func:
        meta["func"] = func

    return {"page_content": page_content, "metadata": meta}


# ── 主逻辑 ────────────────────────────────────────────────────────

def build_cli_chunks(graph_path: Path) -> List[Dict[str, Any]]:
    """从 cli_keyword_graph.json 生成所有 CLI 文档块。"""
    logger.info("加载 CLI 图谱: %s", graph_path)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    logger.info("构建模块根节点映射...")
    root_map = build_root_map(graph)

    chunks = []
    skipped = 0
    for node in graph.get("nodes", []):
        if node.get("type") not in ("command", "operation_command"):
            continue
        nid = node.get("id", "")
        root_id = root_map.get(nid, nid.split("_")[0])
        chunk = node_to_chunk(node, root_id)
        if chunk:
            chunks.append(chunk)
        else:
            skipped += 1

    logger.info(
        "CLI 块生成完毕: %d 块 (%d 跳过 / 无XML数据)", len(chunks), skipped
    )
    return chunks


def rebuild(dry_run: bool = False) -> None:
    # ── 1. 生成 CLI 块（唯一数据源：xml 叶子节点） ────────────────
    cli_chunks = build_cli_chunks(_GRAPH_PATH)

    if dry_run:
        logger.info("Dry-run: 生成 %d 块，不写入文件", len(cli_chunks))
        # 打印几个样例
        for chunk in cli_chunks[:3]:
            logger.info("  SAMPLE ---\n%s", chunk["page_content"][:300])
            logger.info("  META: %s", chunk["metadata"])
        return

    # ── 2. 备份现有 knowledge_base.json ──────────────────────────
    if _KB_PATH.exists():
        existing = json.loads(_KB_PATH.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            existing = existing.get("chunks", [])
        before_count = len(existing)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = _KB_PATH.parent / f"knowledge_base_bak_{ts}.json"
        shutil.copy2(_KB_PATH, bak_path)
        logger.info(
            "备份原文件: %s (%d 块, %.1f MB)",
            bak_path, before_count, bak_path.stat().st_size / 1_048_576,
        )

    # ── 3. 写入新文件（仅 CLI 叶子） ─────────────────────────────
    _KB_PATH.write_text(
        json.dumps(cli_chunks, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size_mb = _KB_PATH.stat().st_size / 1_048_576
    logger.info(
        "写入新 knowledge_base.json: 仅 cli/reference 叶子 %d 块 (%.1f MB)",
        len(cli_chunks), size_mb,
    )


def main(argv=None):
    p = argparse.ArgumentParser(description="用 CLI 图谱叶子节点替换 knowledge_base.json 的 cli/reference 部分")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    rebuild(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
