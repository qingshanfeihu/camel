import json, sys
from pathlib import Path
from collections import Counter

app = json.loads(Path('INAGENT/backup/_rag_backup/doc_local_reference/app.json').read_text('utf-8'))

out = []

# ===== SLB TCP 相关：找"TCP类协议的负载均衡"大章节 =====
# 找 section_title 包含 TCP 且 parent_section 含 SLB 的
slb_entries = [e for e in app if e.get('metadata',{}).get('product_module','') == 'SLB']

out.append("=== SLB 所有 section_title (unique) ===")
all_secs = Counter(e['metadata'].get('section_title','') for e in slb_entries if e['metadata'].get('section_title',''))
for sec, cnt in sorted(all_secs.items(), key=lambda x:-x[1]):
    out.append(f"  [{cnt}] {sec}")

out.append("\n=== HA 所有 section_title (unique) ===")
ha_entries = [e for e in app if e.get('metadata',{}).get('product_module','') == '高可用']
ha_secs = Counter(e['metadata'].get('section_title','') for e in ha_entries if e['metadata'].get('section_title',''))
for sec, cnt in sorted(ha_secs.items(), key=lambda x:-x[1]):
    out.append(f"  [{cnt}] {sec}")

# 写文件
Path('_app_sections.txt').write_text('\n'.join(out), encoding='utf-8')
print(f"Written {len(out)} lines to _app_sections.txt")
