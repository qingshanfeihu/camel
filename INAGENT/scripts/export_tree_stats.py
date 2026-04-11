"""导出知识树统计: 整体分布 + 按层级分文档输出每个节点的内容."""

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

KB_PATH = Path("INAGENT/knowledge_base/reference/knowledge_base.json")
OUTPUT_DIR = Path("INAGENT/knowledge_base/logs/tree_stats")


def load_kb():
    with open(KB_PATH, encoding="utf-8") as f:
        return json.load(f)


def gather_stats(chunks):
    level_counts = Counter()
    level_nodes = defaultdict(lambda: defaultdict(list))

    for chunk in chunks:
        meta = chunk.get("metadata", {})
        tp = meta.get("tree_position", {})
        level = tp.get("tree_level", "unknown")
        linked = tp.get("linked_nodes", [])
        node_key = " > ".join(linked) if linked else "(unlinked)"

        level_counts[level] += 1
        level_nodes[level][node_key].append(chunk)

    return level_counts, level_nodes


def print_summary(level_counts, total):
    order = ["root", "trunk", "branch", "leaf", "new_leaf", "unknown"]
    print("=" * 60)
    print(f"  知识树统计  —  共 {total} chunks")
    print("=" * 60)
    for lv in order:
        cnt = level_counts.get(lv, 0)
        if cnt:
            pct = cnt / total * 100
            bar = "█" * int(pct / 2)
            print(f"  {lv:<10s}  {cnt:>5d}  ({pct:5.1f}%)  {bar}")
    print("=" * 60)


def write_level_md(level, nodes_map, out_dir):
    path = out_dir / f"{level}.md"
    unique_nodes = len(nodes_map)
    total_chunks = sum(len(v) for v in nodes_map.values())

    lines = [
        f"# 树层级: {level}",
        "",
        f"- 节点数: {unique_nodes}",
        f"- 内容块数: {total_chunks}",
        "",
        "---",
        "",
    ]

    sorted_nodes = sorted(nodes_map.items(), key=lambda x: -len(x[1]))
    for node_key, chunks in sorted_nodes:
        lines.append(f"## {node_key}  ({len(chunks)} chunks)")
        lines.append("")

        for i, chunk in enumerate(chunks, 1):
            meta = chunk.get("metadata", {})
            tp = meta.get("tree_position", {})
            content = chunk.get("page_content", "")
            section_title = meta.get("section_title", "")
            source = meta.get("source_file", "")
            block_id = meta.get("block_id", "?")
            confidence = tp.get("confidence", "?")
            role = tp.get("knowledge_role", "")

            preview = content[:200].replace("\n", " ").strip()
            if len(content) > 200:
                preview += "..."

            lines.append(f"### [{i}] block={block_id}  conf={confidence}  role={role}")
            lines.append(f"- 来源: `{source}` | 章节: {section_title}")
            lines.append(f"- 内容({len(content)}字): {preview}")
            lines.append("")

        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  -> {path}  ({unique_nodes} nodes, {total_chunks} chunks)")


def main():
    chunks = load_kb()
    level_counts, level_nodes = gather_stats(chunks)
    print_summary(level_counts, len(chunks))

    out_dir = OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n导出目录: {out_dir}/")
    for level in sorted(level_nodes.keys()):
        write_level_md(level, level_nodes[level], out_dir)

    summary_path = out_dir / "summary.md"
    order = ["root", "trunk", "branch", "leaf", "new_leaf", "unknown"]
    lines = [
        "# 知识树统计总览",
        "",
        f"总计: {len(chunks)} chunks",
        "",
        "| 层级 | 节点数 | 内容块数 | 占比 |",
        "|------|--------|----------|------|",
    ]
    for lv in order:
        if lv not in level_nodes:
            continue
        nm = level_nodes[lv]
        n_nodes = len(nm)
        n_chunks = sum(len(v) for v in nm.values())
        pct = n_chunks / len(chunks) * 100
        lines.append(f"| {lv} | {n_nodes} | {n_chunks} | {pct:.1f}% |")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  -> {summary_path}")
    print("\n完成。")


if __name__ == "__main__":
    main()
