import json
from pathlib import Path

blocks = json.loads(Path('INAGENT/knowledge_base/doc_local_reference/cli_1-82.json').read_text(encoding='utf-8'))

print("=== First 20 blocks: section_path evolution ===")
for i in range(min(20, len(blocks))):
    b = blocks[i]
    m = b.get('metadata', {})
    sp = m.get('section_path', '')
    st = m.get('section_title', '')
    text = str(b.get('page_content', b.get('text', '')))[:80]
    print(f"[{i:3d}] path={sp!r}")
    print(f"      title={st!r}  text={text!r}")

print("\n=== Where does 管理工具 first appear in section_path? ===")
for i, b in enumerate(blocks):
    m = b.get('metadata', {})
    sp = m.get('section_path', '')
    if '管理' in sp:
        text = str(b.get('page_content', b.get('text', '')))[:80]
        print(f"[{i:3d}] path={sp!r}")
        print(f"      text={text!r}")
        # show 3 before
        for j in range(max(0, i-3), i):
            bj = blocks[j]
            mj = bj.get('metadata', {})
            spj = mj.get('section_path', '')
            textj = str(bj.get('page_content', bj.get('text', '')))[:80]
            print(f"  (before [{j:3d}]) path={spj!r}  text={textj!r}")
        break

print("\n=== Unique section_path prefixes (first segment) ===")
from collections import Counter
segments = Counter()
for b in blocks:
    sp = b.get('metadata', {}).get('section_path', '')
    if sp:
        first = sp.split(' > ')[0]
        segments[first] += 1
for seg, cnt in segments.most_common(20):
    print(f"  {cnt:5d}  {seg}")

print("\n=== How many blocks have polluted paths (containing non-technical frontmatter)? ===")
boilerplate = {'联系我们', '关于我们', '商标声明', '合格声明', '目录', '管理工具'}
polluted = sum(1 for b in blocks if any(bp in str(b.get('metadata',{}).get('section_path','')) for bp in boilerplate))
print(f"Total blocks: {len(blocks)}, polluted: {polluted}")
