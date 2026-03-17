import json, pathlib
full = json.loads(pathlib.Path('INAGENT/graphrag_index/documents_full.json.bak').read_text('utf-8'))
for d in full:
    if d['id'] in ['src_0000','src_0005','src_0006']:
        t = d['text']
        did = d['id']
        title = d['title']
        print(f'=== {did}: {title} ({len(t)} chars) ===')
        print(t[:1500])
        print('...')
        mid = len(t)//2
        print(t[mid:mid+800])
        print('---')
