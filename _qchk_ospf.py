import json

def m(b, key, default='?'):
    return b.get('metadata', b).get(key, b.get(key, default))

blocks = json.loads(open('INAGENT/knowledge_base/reference/cli_1-82.json','r',encoding='utf-8').read())

ospf_all = [b for b in blocks if 'ospf' in m(b,'section_title','').lower() or 'ospf' in b.get('page_content','').lower()[:200] or 'ospf' in m(b,'command_prefix','').lower()]
print(f'=== All OSPF-related blocks ({len(ospf_all)}) ===')
section_titles = set()
for b in ospf_all:
    st = m(b,'section_title')
    if st not in section_titles:
        section_titles.add(st)
        print(f"  [{st}] prefix={m(b,'command_prefix')} tl={m(b,'tree_level')} tp={m(b,'tree_position')} ps={m(b,'parent_section')} mod={m(b,'product_module')}")
        print(f"    content: {b.get('page_content','')[:100].replace(chr(10),' ')}")
print()

auth_blocks = [b for b in blocks if '认证' in m(b,'section_title','') or '认证' in b.get('page_content','')[:200]]
print(f'=== auth-related ({len(auth_blocks)}) ===')
for b in auth_blocks[:5]:
    print(f"  [{m(b,'section_title')}] prefix={m(b,'command_prefix')} tl={m(b,'tree_level')}")
    print(f"    content: {b.get('page_content','')[:100].replace(chr(10),' ')}")
print()

ct = json.loads(open('INAGENT/knowledge_base/reference/commandtree_base.json','r',encoding='utf-8').read())
ospf_ct = [c for c in ct if 'ospf' in m(c,'command_prefix','').lower()]
print(f'=== CommandTree: ospf ({len(ospf_ct)}) ===')
for c in ospf_ct[:20]:
    print(f"  {m(c,'command_prefix')} | mod={m(c,'product_module')} | tl={m(c,'tree_level')}")

arch = json.loads(open('INAGENT/knowledge_base/reference/ustack设计架构V3.json','r',encoding='utf-8').read())
linux_b = [b for b in arch if 'linux' in b.get('page_content','').lower()[:300] or 'linux' in m(b,'section_title','').lower()]
print(f'\n=== ARCH linux ({len(linux_b)}) ===')
for b in linux_b[:5]:
    print(f"  [{m(b,'section_title')}] tl={m(b,'tree_level')} mod={m(b,'product_module')}")
    print(f"    content: {b.get('page_content','')[:200].replace(chr(10),' ')}")
    print()
