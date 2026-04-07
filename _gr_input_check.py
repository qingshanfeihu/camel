import json
from pathlib import Path
from collections import Counter

docs = json.loads(Path("INAGENT/graphrag_index/input/documents.json").read_text("utf-8"))
kb = json.loads(Path("INAGENT/knowledge_base/reference/knowledge_base.json").read_text("utf-8"))
print(f"GraphRAG input docs: {len(docs)}, KB entries: {len(kb)}")

full_text = "\n".join(d.get("text", "") for d in docs)

# Check: which KB command_prefix (more reliable than node_id) appears in input text
found_cp = 0
found_nid = 0
missing_cp = []
for e in kb:
    m = e.get("metadata", {})
    nid = m.get("node_id", "")
    cp = m.get("command_prefix", "")
    text = e.get("page_content", "")

    # Check if the actual content text appears in the input
    if nid and nid in full_text:
        found_nid += 1
    if cp:
        # The formatted document uses [命令] prefix
        marker = f"[命令] {cp}"
        if marker in full_text:
            found_cp += 1
        else:
            missing_cp.append((cp, nid, m.get("product_module", "")))

print(f"\nnode_id in input text:      {found_nid}/{len(kb)} ({found_nid/len(kb):.1%})")
print(f"command_prefix in input:    {found_cp}/{len(kb)} ({found_cp/len(kb):.1%})")
print(f"Missing command_prefix:     {len(missing_cp)}")

# Analyze what modules are missing
missing_mods = Counter(m for _, _, m in missing_cp)
print(f"\nMissing by module (top 15):")
for mod, cnt in missing_mods.most_common(15):
    print(f"  {cnt:4d}  {mod}")

# Check: are these missing because the doc title doesn't match the module?
print(f"\nSample missing commands (first 20):")
for cp, nid, mod in missing_cp[:20]:
    print(f"  {cp} (module={mod})")
