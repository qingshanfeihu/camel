import json, pathlib
full = json.loads(pathlib.Path('INAGENT/graphrag_index/documents_full.json.bak').read_text('utf-8'))
for d in full:
    did = d["id"]
    title = d["title"][:50]
    chars = len(d["text"])
    snippet = d["text"][:200].replace("\n", " ")[:120]
    print(f"{did} | {title:50s} | {chars:>7d} | {snippet}")
