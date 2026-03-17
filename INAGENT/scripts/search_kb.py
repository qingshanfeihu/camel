"""Temp script to search knowledge base for health check related chunks."""
import sys, json, re
sys.stdout.reconfigure(encoding='utf-8')

kb_path = 'INAGENT/knowledge_base/reference/knowledge_base.json'
with open(kb_path, 'r', encoding='utf-8') as f:
    chunks = json.load(f)

print(f'Total chunks: {len(chunks)}')

keywords = ['health request', 'health response', 'health server', 'slb health', 'health check', 'slb real health', 'health_check', 'hc_', 'slb health.*http']
for kw in keywords:
    matches = [c for c in chunks if re.search(kw, c.get('text', ''), re.IGNORECASE)]
    print(f'\n--- Keyword: "{kw}" ({len(matches)} matches) ---')
    for m in matches[:2]:
        text = m.get('text', '')
        meta = m.get('metadata', {})
        mod = meta.get('product_module', '?')
        scn = meta.get('scenario_id', '?')
        stp = meta.get('step_type', '?')
        print(f'  Module={mod}, Scenario={scn}, Step={stp}')
        print(f'  Text: {text[:250]}')
        print()

# Also check what the top health-check related chunks look like
print('\n=== Chunks with step_type containing "health" ===')
health_chunks = [c for c in chunks if 'health' in str(c.get('metadata', {}).get('step_type', '')).lower()]
print(f'Found {len(health_chunks)} chunks')
for h in health_chunks[:5]:
    text = h.get('text', '')
    meta = h.get('metadata', {})
    print(f'  Step={meta.get("step_type","?")}, Module={meta.get("product_module","?")}, Scenario={meta.get("scenario_id","?")}')
    print(f'  Text: {text[:200]}')
    print()
