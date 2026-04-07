import json

backup = json.load(open('INAGENT/knowledge_base/reference/app_1-40.json', 'r', encoding='utf-8'))

# Find nearby blocks around page 178 (HTTP compression section)
nearby = [e for e in backup if 173 <= e.get('metadata', {}).get('page_idx', 0) <= 185]
nearby.sort(key=lambda e: (e.get('metadata', {}).get('page_idx', 0), e.get('metadata', {}).get('block_id', 0)))

print("=== Blocks around page 173-185 (HTTP compression chapter) ===")
for e in nearby[:30]:
    m = e.get('metadata', {})
    title = m.get('section_title', '')
    parent = m.get('parent_section', '')
    path = m.get('section_path', '')
    mod = m.get('product_module', '')
    pg = m.get('page_idx', 0)
    blk = m.get('block_id', 0)
    bt = m.get('block_type', '')
    pc = e.get('page_content', '')[:60]
    print(f"  p{pg:3d} blk{blk:4d} [{bt:6s}] title={title!r:25} parent={parent!r:25} path={path!r:30} mod={mod!r:15} | {pc}")

# Also: how many entries total have empty parent_section but non-generic title?
no_parent = [e for e in backup if not e.get('metadata', {}).get('parent_section', '') and e.get('metadata', {}).get('section_title', '') not in ('', '概述', '注意', '注意：', '说明')]
print(f"\n=== Entries with empty parent_section (non-generic title): {len(no_parent)} / {len(backup)} total ===")
for e in no_parent[:10]:
    m = e.get('metadata', {})
    print(f"  title={m.get('section_title','')!r:30} path={m.get('section_path','')!r:35} mod={m.get('product_module','')!r}")
