import json, pathlib, collections, time

ref_dir = pathlib.Path(r'C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\reference')
out_dir = pathlib.Path(r'C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\logs')
stats = collections.Counter()
src_stats = {}
by_level = collections.defaultdict(list)

for f in sorted(ref_dir.glob('*.json')):
    if f.name == 'commandtree_base.json' or '_bak' in f.stem:
        continue
    data = json.loads(f.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        continue
    for b in data:
        m = b.get('metadata', {})
        tp = m.get('tree_position', {})
        tl = tp.get('tree_level', 'unknown') if isinstance(tp, dict) else 'unknown'
        sf = m.get('source_file', f.stem)
        stats[tl] += 1
        if sf not in src_stats:
            src_stats[sf] = collections.Counter()
        src_stats[sf][tl] += 1
        by_level[tl].append(b)

total = sum(stats.values())
print('=== 知识库树层级总体统计 ===')
for level in ['root', 'trunk', 'branch', 'new_leaf', 'leaf', 'unknown']:
    if stats[level]:
        pct = 100 * stats[level] / total
        print(f'  {level:10s}: {stats[level]:5d}  ({pct:.1f}%)')
print(f'  {"total":10s}: {total:5d}')
print()
print('=== 按文档分布 ===')
for sf in sorted(src_stats):
    c = src_stats[sf]
    parts = ' | '.join(f'{k}={v}' for k, v in sorted(c.items(), key=lambda x: -x[1]))
    print(f'  {sf}: total={sum(c.values())} ({parts})')

timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
overview_lines = [
    f"# 知识库树层级总览",
    f"",
    f"> 生成时间: {timestamp}  |  总节点数: {total}",
    f"",
    f"| 层级 | 节点数 | 占比 | 说明 |",
    f"|------|--------|------|------|",
    f"| root | {stats['root']} | {100*stats['root']/total:.1f}% | 产品/模块根节点 |",
    f"| trunk | {stats['trunk']} | {100*stats['trunk']/total:.1f}% | 功能主干 |",
    f"| branch | {stats['branch']} | {100*stats['branch']/total:.1f}% | 功能分支 |",
    f"| leaf | {stats['leaf']} | {100*stats['leaf']/total:.1f}% | 命令/配置叶子 |",
    f"",
    f"## 按文档分布",
    f"",
    f"| 文档 | 总数 | root | trunk | branch | leaf |",
    f"|------|------|------|-------|--------|------|",
]
for sf in sorted(src_stats):
    c = src_stats[sf]
    overview_lines.append(
        f"| {sf} | {sum(c.values())} | {c.get('root',0)} | {c.get('trunk',0)} | {c.get('branch',0)} | {c.get('leaf',0)} |"
    )
overview_lines.append("")
overview_lines.append("## 各层级详情文件")
overview_lines.append("")
for level in ['root', 'trunk', 'branch', 'leaf']:
    if stats[level]:
        overview_lines.append(f"- [tree_nodes_{level}.md](tree_nodes_{level}.md) ({stats[level]} 节点)")

(out_dir / "tree_overview.md").write_text("\n".join(overview_lines), encoding="utf-8")
print(f"\n已生成: tree_overview.md")

for level in ['root', 'trunk', 'branch', 'leaf']:
    nodes = by_level.get(level, [])
    if not nodes:
        continue
    by_src = collections.defaultdict(list)
    for b in nodes:
        m = b.get('metadata', {})
        sf = m.get('source_file', '?')
        by_src[sf].append(b)

    lines = [
        f"# {level} 层节点详情 ({len(nodes)} 个)",
        f"",
        f"> 生成时间: {timestamp}",
        f"",
    ]
    for sf in sorted(by_src):
        blocks = by_src[sf]
        lines.append(f"## {sf} ({len(blocks)} 个 {level})")
        lines.append("")
        lines.append(f"| # | section_title | command_prefix | module | content_len | content_preview |")
        lines.append(f"|---|---------------|----------------|--------|-------------|-----------------|")
        for i, b in enumerate(blocks):
            m = b.get('metadata', {})
            title = m.get('section_title', '-')
            cp = m.get('command_prefix', '-')
            mod = m.get('product_module', '-')
            pc = b.get('page_content', '')
            preview = pc[:80].replace('\n', ' ').replace('|', '\\|')
            lines.append(f"| {i+1} | {title} | {cp} | {mod} | {len(pc)} | {preview} |")
        lines.append("")

    fname = f"tree_nodes_{level}.md"
    (out_dir / fname).write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成: {fname} ({len(nodes)} 节点, {len(lines)} 行)")
