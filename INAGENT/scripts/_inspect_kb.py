import json
from collections import Counter

with open('INAGENT/knowledge_base/reference/knowledge_base.json', encoding='utf-8') as f:
    kb = json.load(f)

print(f"Total blocks: {len(kb)}")

# Collect all metadata keys
all_keys = Counter()
for item in kb:
    for k in item.get('metadata', {}).keys():
        all_keys[k] += 1

print("\n=== Metadata fields (field: count/total) ===")
for k, c in all_keys.most_common():
    print(f"  {k}: {c}/{len(kb)}")

# Show 3 diverse samples (global set, variant, group scope)
print("\n=== Sample 1: basic set command ===")
for item in kb:
    meta = item.get('metadata', {})
    if not meta.get('is_variant') and 'group' not in meta.get('scope', ''):
        print(json.dumps(item, ensure_ascii=False, indent=2)[:600])
        break

print("\n=== Sample 2: no/show/clear variant ===")
for item in kb:
    meta = item.get('metadata', {})
    if meta.get('is_variant') and meta.get('func'):
        print(json.dumps(item, ensure_ascii=False, indent=2)[:600])
        break

print("\n=== Sample 3: group scope ===")
for item in kb:
    meta = item.get('metadata', {})
    if 'group' in meta.get('scope', ''):
        print(json.dumps(item, ensure_ascii=False, indent=2)[:600])
        break

print("\n=== Sample 4: multi-param command ===")
for item in kb:
    pc = item.get('page_content', '')
    if pc.count('必填') >= 3:
        print(json.dumps(item, ensure_ascii=False, indent=2)[:800])
        break

# cli.json (farmer enriched)
print("\n\n=== cli.json (farmer enriched) ===")
with open('INAGENT/knowledge_base/reference/cli.json', encoding='utf-8') as f:
    cli = json.load(f)
print(f"Total: {len(cli)} blocks")
cli_keys = Counter()
for item in cli:
    for k in item.get('metadata', {}).keys():
        cli_keys[k] += 1
print("Metadata fields:")
for k, c in cli_keys.most_common():
    print(f"  {k}: {c}/{len(cli)}")
print("\nSample:")
print(json.dumps(cli[0], ensure_ascii=False, indent=2)[:600])
