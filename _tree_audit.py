import json, pathlib, collections

ref_dir = pathlib.Path(r'C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\reference')

total = 0
issues = {
    'title_content_mismatch': [],
    'duplicate_content': [],
    'trivial_short': [],
    'dup_exact': 0,
}
content_seen = {}
title_content_pairs = []

for f in sorted(ref_dir.glob('*.json')):
    if f.name == 'commandtree_base.json' or '_bak' in f.stem:
        continue
    data = json.loads(f.read_text(encoding='utf-8'))
    if not isinstance(data, list):
        continue
    for b in data:
        total += 1
        m = b.get('metadata', {})
        pc = b.get('page_content', '')
        title = m.get('section_title', '')
        sf = m.get('source_file', f.stem)
        tp = m.get('tree_position', {})
        tl = tp.get('tree_level', '?') if isinstance(tp, dict) else '?'

        if len(pc) <= 25:
            issues['trivial_short'].append((sf, title, tl, len(pc), pc[:50]))

        content_key = pc.strip()[:200]
        if content_key in content_seen:
            issues['dup_exact'] += 1
        else:
            content_seen[content_key] = (sf, title)

        first_line = pc.strip().split('\n')[0] if pc.strip() else ''
        if title and first_line:
            import re
            heading_match = re.match(r'^[\d.]+\s*(.+)', first_line.strip())
            if heading_match:
                content_heading = heading_match.group(1).strip()
                if content_heading and title.strip() != content_heading:
                    clean_title = title.replace('⼀', '一').replace('⽤', '用').replace('⽹', '网')
                    clean_ch = content_heading.replace('⼀', '一').replace('⽤', '用').replace('⽹', '网')
                    if clean_title != clean_ch and clean_ch not in clean_title and clean_title not in clean_ch:
                        issues['title_content_mismatch'].append(
                            (sf, tl, title, content_heading, first_line[:80]))

print(f"总节点: {total}")
print()

short = issues['trivial_short']
by_level = collections.Counter(s[2] for s in short)
print(f"=== 极短内容 (≤25 chars): {len(short)} 个 ===")
print(f"  按层级: {dict(by_level)}")
by_src = collections.Counter(s[0] for s in short)
print(f"  按文档: {dict(by_src)}")
print()

print(f"=== 完全重复 (前200字相同): {issues['dup_exact']} 个 ===")
print()

mis = issues['title_content_mismatch']
print(f"=== 标题-内容错位 (标题≠内容首行标题): {len(mis)} 个 ===")
by_src = collections.Counter(m[0] for m in mis)
print(f"  按文档: {dict(by_src)}")

real_offset = []
cjk_variant = []
for sf, tl, title, ch, fl in mis:
    t1 = title.replace('⼀','一').replace('⽤','用').replace('⽹','网').replace('⼯','工').replace('⻆','角').replace('⼤','大').replace('⽰','示').replace('⾼','高').replace('⻣','骨').replace('⽹','网').replace('⼆','二').replace('⾯','面').replace('⽅','方').replace('⽀','支').replace('⽂','文').replace('⼊','入').replace('⽇','日').replace('⽤','用').replace('⽚','片').replace('⼝','口').replace('⽆','无').replace('⼦','子')
    c1 = ch.replace('⼀','一').replace('⽤','用').replace('⽹','网').replace('⼯','工').replace('⻆','角').replace('⼤','大').replace('⽰','示').replace('⾼','高').replace('⻣','骨').replace('⽹','网').replace('⼆','二').replace('⾯','面').replace('⽅','方').replace('⽀','支').replace('⽂','文').replace('⼊','入').replace('⽇','日').replace('⽤','用').replace('⽚','片').replace('⼝','口').replace('⽆','无').replace('⼦','子')
    import unicodedata
    t2 = unicodedata.normalize('NFKC', t1)
    c2 = unicodedata.normalize('NFKC', c1)
    if t2 == c2 or c2 in t2 or t2 in c2:
        cjk_variant.append((sf, tl, title, ch))
    else:
        real_offset.append((sf, tl, title, ch, fl))

print(f"\n  CJK字形差异(非真正错位): {len(cjk_variant)} 个")
print(f"  真正标题-内容错位: {len(real_offset)} 个")
by_src2 = collections.Counter(r[0] for r in real_offset)
print(f"  按文档: {dict(by_src2)}")
by_tl = collections.Counter(r[1] for r in real_offset)
print(f"  按层级: {dict(by_tl)}")
for sf, tl, title, ch, fl in real_offset[:30]:
    print(f"  [{tl:6s}] [{sf}] title={title!r} → content={ch!r}")
if len(real_offset) > 30:
    print(f"  ... 还有 {len(real_offset)-30} 个")
