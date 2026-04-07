import json, sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from collections import Counter

app = json.loads(Path('INAGENT/backup/_rag_backup/doc_local_reference/app.json').read_text('utf-8'))

# SLB TCP 章节
tcp_entries = [e for e in app 
               if e.get('metadata',{}).get('product_module','') == 'SLB'
               and 'TCP' in e.get('metadata',{}).get('section_title','')]
print(f"SLB+TCP section entries: {len(tcp_entries)}")
for e in tcp_entries[:5]:
    m = e.get('metadata',{})
    sec = m.get('section_title','')