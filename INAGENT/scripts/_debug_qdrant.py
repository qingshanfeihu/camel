"""分析 Qdrant payload 结构"""
import re, collections
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = QdrantClient(path=r'C:\Users\jiang\AppData\Local\INAGENT\vector_store\qdrant')

all_r, _ = client.scroll('workflow_rag', limit=5000, with_payload=True)
print(f'总点数: {len(all_r)}')

pmods = collections.Counter(
    r.payload.get('metadata', {}).get('regex_metadata', {}).get('product_module', '?')
    for r in all_r
)
print(f'distinct product_module 数: {len(pmods)}')
print('分布 top20:')
for k, v in pmods.most_common(20):
    print(f'  {k!r}: {v}')

# 查找 aaa_ldap_defaultgroup 的块（通过 text 内容）
hits = [r for r in all_r if 'aaa ldap defaultgroup' in r.payload.get('text', '')]
print(f'\n包含 "aaa ldap defaultgroup" 的块: {len(hits)}')
for r in hits[:2]:
    txt = r.payload.get('text', '')
    meta_json = re.search(r'INAGENT_META_JSON:(\{.+?\})', txt)
    node_m = re.search(r'内部函数:\s*(\S+)', txt)
    print(f'  INAGENT_META_JSON: {meta_json.group(1) if meta_json else "?"}')
    print(f'  内部函数: {node_m.group(1) if node_m else "?"}')
    print(f'  product_module key: {r.payload.get("metadata",{}).get("regex_metadata",{}).get("product_module","?")}')
