import json
data = json.loads(open(r'C:\SynologyDrive\INFOAGEN\INAGENT\knowledge_base\reference\cli_1-82.json', 'r', encoding='utf-8').read())
for title in ['OSPF接口消息摘要密钥', 'OSPF接口认证方式']:
    print(f"\n{'='*60}")
    print(f"  ALL blocks for: {title}")
    print(f"{'='*60}")
    for b in data:
        m = b.get('metadata', {})
        if m.get('section_title') == title:
            pc = b['page_content']
            bid = m.get('block_id', '')
            cp = m.get('command_prefix', '')
            mod = m.get('product_module', '')
            print(f"  [{bid}] prefix={cp!r} mod={mod} len={len(pc)}")
            print(f"    content: {pc[:200]}")
            print()
