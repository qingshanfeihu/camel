import json

blocks = json.loads(open('INAGENT/knowledge_base/reference/app_1-40.json','r',encoding='utf-8').read())
for i in range(300, min(385, len(blocks))):
    b = blocks[i]
    m = b.get('metadata',{})
    st = m.get('section_title','')
    tp = m.get('tree_position',{})
    lvl = tp.get('tree_level','?')
    role = tp.get('knowledge_role','')
    pc = (b.get('page_content','') or '')[:60]
    print(f'[{i}] lvl={lvl} role={role[:25]:25s} st="{st}"')

print("\n--- cli_1-82.json 100-160 ---")
blocks2 = json.loads(open('INAGENT/knowledge_base/reference/cli_1-82.json','r',encoding='utf-8').read())
for i in range(100, min(160, len(blocks2))):
    b = blocks2[i]
    m = b.get('metadata',{})
    st = m.get('section_title','')
    tp = m.get('tree_position',{})
    lvl = tp.get('tree_level','?')
    role = tp.get('knowledge_role','')
    print(f'[{i}] lvl={lvl} role={role[:25]:25s} st="{st}"')
