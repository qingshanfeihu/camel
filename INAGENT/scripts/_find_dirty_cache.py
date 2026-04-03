"""找产生脏实体的 cache，分析 LLM 原始输出"""
import json, os, glob, re
from collections import defaultdict

cache_dir = 'INAGENT/graphrag_index/cache/extract_graph'
files = sorted(glob.glob(os.path.join(cache_dir, 'chat_*')))

def extract_entities(content):
    out = []
    for m in re.finditer(r'\("entity"<\|>([^<]+)<\|>([^<]*)<\|>([^)]*)\)', content, re.IGNORECASE):
        title, etype, desc = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        out.append((title, etype, desc))
    return out

def get_content(data):
    try:
        return data['result']['response']['choices'][0]['message']['content']
    except Exception:
        return ''

ALLOWED = {'command','parameter','product_module','protocol','feature','configuration'}
found_dirty = []

for f in files:
    try:
        with open(f, 'rb') as fp:
            data = json.loads(fp.read().decode('utf-8', errors='replace'))
        content = get_content(data)
        if not content:
            continue
        for title, etype, desc in extract_entities(content):
            if etype.lower() not in ALLOWED:
                found_dirty.append((os.path.basename(f), title, etype, desc[:60]))
    except Exception:
        pass

print(f'cache 文件总数: {len(files)}')
print(f'LLM 原始输出中含脏类型实体记录: {len(found_dirty)}')
print()

by_file = defaultdict(list)
for fn, title, etype, desc in found_dirty:
    by_file[fn].append((title, etype, desc))

print(f'涉及 cache 文件数: {len(by_file)}')
for fn, ents in list(by_file.items())[:8]:
    print(f'\n[{fn[:52]}]')
    for title, etype, desc in ents[:6]:
        tag = '(空)' if etype == '' else f'type={etype!r}'
        print(f'  {tag}  title={title!r}  desc={desc[:55]}')
    if len(ents) > 6:
        print(f'  ... 共 {len(ents)} 条')

# 按 type 值分组汇总
print()
from collections import Counter
type_counter = Counter(etype for _, _, etype, _ in found_dirty)
print('脏 type 值分布:')
for k,v in type_counter.most_common():
    print(f'  {k!r}: {v}')
