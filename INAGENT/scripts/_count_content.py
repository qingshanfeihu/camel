import json, pathlib, sys
sys.stdout.reconfigure(encoding='utf-8')
full = json.loads(pathlib.Path('INAGENT/graphrag_index/documents_full.json.bak').read_text('utf-8'))

test_count = 0
for d in full:
    t = d['text']
    test_count += t.count('测试项:')
print(f'Total test items across all docs: {test_count}')

for d in full:
    t = d['text']
    c = t.count('测试项:')
    if c > 0:
        print(f'  {d["id"]}: {d["title"][:50]} -> {c} test items')

# 统计CLI命令数(cli.json中的行以命令开头的)
for d in full:
    if d['id'] == 'src_0006':
        lines = d['text'].split('\n')
        cli_cmds = [l.strip() for l in lines if l.strip().startswith(('slb ','ssl ','show ','no ','clear ','ip ','http2 ','ecc '))]
        print(f'\nCLI commands in cli.json (approx): {len(cli_cmds)}')
        # Show some examples
        for cmd in cli_cmds[:20]:
            print(f'  {cmd[:100]}')
        break

# 看功能规格书中包含的信息类型
print('\n--- Func spec unique content types ---')
for d in full:
    if 'spec/func_spec' in d['text'][:300]:
        t = d['text']
        sections = []
        for kw in ['Problem Statement','High Level Overview','Customer Requirement','Competitor Analysis',
                    'CLI:','CLI prompts','WebUI','SNMP','Log:','Backward Compatibility',
                    'Security Consideration','Dependencies','Scalability and Performance',
                    'User Experience','Test Strategy','Acceptance Criteria']:
            if kw in t:
                sections.append(kw)
        print(f'  {d["id"]}: {d["title"][:50]}')
        print(f'    Sections: {", ".join(sections)}')
