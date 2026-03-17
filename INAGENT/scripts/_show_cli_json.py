import json, pathlib, sys
sys.stdout.reconfigure(encoding='utf-8')

full = json.loads(pathlib.Path('INAGENT/graphrag_index/documents_full.json.bak').read_text('utf-8'))
for d in full:
    if d['id'] in ['src_0006']:
        t = d['text']
        did = d['id']
        title = d['title']
        print(f'=== {did}: {title} ({len(t)} chars) ===')
        # 找到 SLB 和 HTTP 相关内容
        lines = t.split('\n')
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in ['http2', 'http/2', 'slb virtual', 'slb real', 'slb health', 'ssl profile']):
                start = max(0, i-1)
                end = min(len(lines), i+5)
                for j in range(start, end):
                    print(f"  L{j}: {lines[j][:200]}")
                print()
                if i > 200:  # 只打印前面一些
                    break
