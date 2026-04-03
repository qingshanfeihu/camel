import json
from pathlib import Path

ref = Path('INAGENT/knowledge_base/reference')
print('=== reference/ files ===')
for f in sorted(ref.glob('*.json')):
    d = json.loads(f.read_text('utf-8'))
    print(f'  {f.name}: {len(d)} chunks')

print()
print('=== cli.json ircookie chunks (farmer output) ===')
cli_f = ref / 'cli.json'
if cli_f.exists():
    data = json.loads(cli_f.read_text('utf-8'))
    for item in data:
        meta = item.get('metadata', {})
        pc = (item.get('page_content') or '')[:200]
        bid = meta.get('block_id', '?')
        tid = meta.get('tree_node_id', '-')
        refs = meta.get('command_refs', [])
        override = meta.get('supports_override', 'N/A')
        print(f'  block_id={bid}  tree_node={tid}  refs={refs}  override={override}')
        print(f'    {pc}')
        print()

print('=== knowledge_base.json ircookie nodes ===')
kb = json.loads((ref / 'knowledge_base.json').read_text('utf-8'))
for item in kb:
    nid = item.get('metadata', {}).get('node_id', '')
    if 'ircookie' in nid:
        pc = item.get('page_content', '')[:300]
        override = item.get('metadata', {}).get('supports_override', 'N/A')
        print(f'  node_id={nid}  override={override}')
        print(f'    {pc}')
        print()

# Check how many items got supports_override from apply_fill_request
count_override = 0
for item in kb:
    if item.get('metadata', {}).get('supports_override') is not None:
        count_override += 1
print(f'knowledge_base.json items with supports_override: {count_override}/{len(kb)}')

# Check bak file too
bak_files = list(ref.glob('*bak*.json'))
for bf in bak_files:
    bd = json.loads(bf.read_text('utf-8'))
    bcount = sum(1 for item in bd if item.get('metadata', {}).get('supports_override') is not None)
    print(f'{bf.name} items with supports_override: {bcount}/{len(bd)}')
