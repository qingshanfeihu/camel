"""
完整双源抽样检查 v2: Qdrant + GraphRAG
覆盖: 全库清洁度 + 分层命令抽样 (scope级别/参数语法类型/no-show-clear)
"""
import re, json, collections
import pandas as pd
from qdrant_client import QdrantClient

with open('INAGENT/knowledge_base/reference/knowledge_base.json', encoding='utf-8') as f:
    kb = json.load(f)
blocks = kb if isinstance(kb, list) else kb.get('blocks', [])

entities = pd.read_parquet('INAGENT/graphrag_index/output/entities.parquet')
rels     = pd.read_parquet('INAGENT/graphrag_index/output/relationships.parquet')
client   = QdrantClient(path=r'C:\Users\jiang\AppData\Local\INAGENT\vector_store\qdrant')

print('加载完成')
qdrant_all, _ = client.scroll('workflow_rag', limit=5000, with_payload=True)
print(f'  Qdrant: {len(qdrant_all)} points | GraphRAG: {len(entities)} entities {len(rels)} rels | KB: {len(blocks)} blocks')

def find_qdrant(cmd_text):
    q = cmd_text.lower()
    hits = [r for r in qdrant_all if q in r.payload.get('text','').lower()]
    cats = {r.payload.get('metadata',{}).get('regex_metadata',{}).get('document_category','?') for r in hits}
    return len(hits), cats

def find_graphrag(cmd_prefix):
    key = cmd_prefix.upper().replace(' ','_').replace('-','_')
    exact = entities[entities['title'] == key]
    if not exact.empty:
        return [(r['title'], r['type']) for _, r in exact.iterrows()]
    prefix = entities[entities['title'].str.startswith(key + '_', na=False)]
    if not prefix.empty:
        return [(r['title'], r['type']) for _, r in prefix.head(3).iterrows()]
    # 去掉 show_/clear_/no_ 前缀重试
    for pfx in ('SHOW_','CLEAR_','NO_'):
        if key.startswith(pfx):
            return find_graphrag(cmd_prefix[len(pfx):].strip())
    return []

def check(label, cmd_prefix, syntax, scope):
    cnt, cats = find_qdrant(cmd_prefix)
    q_ok = 'OK' if cats == {'cli/reference'} or cnt == 0 else f'DIRTY{cats}'
    q_tag = f'✅ {cnt}条' if cats == {'cli/reference'} else (f'⚠️ {cnt}条(无结果?)' if cnt==0 else f'❌ {cats}')

    g = find_graphrag(cmd_prefix)
    if g:
        g_tag = '✅ ' + ' | '.join(f'{t}({tp})' for t,tp in g[:2])
    else:
        g_tag = '⚠️  (LLM未提取此粒度实体)'

    print(f'\n  [{label}]  scope={scope}')
    print(f'  语法     : {syntax}')
    print(f'  Qdrant   : {q_tag}')
    print(f'  GraphRAG : {g_tag}')

# =====================================================
print('\n' + '='*65)
print('【一、全库清洁度核查】')
print('='*65)

qdrant_cats = collections.Counter(
    r.payload.get('metadata',{}).get('regex_metadata',{}).get('document_category','?')
    for r in qdrant_all)
print(f'\nQdrant {sum(qdrant_cats.values())} points:')
for k,v in qdrant_cats.items():
    print(f'  {"✅" if k=="cli/reference" else "❌"} {k}: {v}')

ALLOWED = {'COMMAND','PARAMETER','PRODUCT_MODULE','PROTOCOL','FEATURE','CONFIGURATION'}
type_dist = entities['type'].value_counts()
dirty_cnt = sum(v for k,v in type_dist.items() if str(k).upper() not in ALLOWED)
print(f'\nGraphRAG {len(entities)} entities:')
for k,v in type_dist.items():
    print(f'  {"✅" if str(k).upper() in ALLOWED else "❌"} {k}: {v}')
print(f'  → 脏实体: {dirty_cnt} ({dirty_cnt/len(entities)*100:.1f}%)')

# =====================================================
print('\n' + '='*65)
print('【二、scope 权限/配置级别分层】')
print('='*65)

print('\n  -- scope=global (全局命令) --')
check('全局必选param', 'aaa ldap bind static',
      'aaa ldap bind static <ldap_server_name> <dn_prefix> <dn_suffix>', 'global')
check('全局HA配置', 'ha active active',
      'ha active active <group_id>', 'global')
check('全局SLB虚IP', 'slb virtual',
      'slb virtual <virtual_name>', 'global')

print('\n  -- scope=group (虚拟服务级，需先进入 group 上下文) --')
check('虚服AAA-LDAP', 'aaa ldap defaultgroup',
      'aaa ldap defaultgroup <ldap_server_name> <group>', 'group')
check('虚服HA备份', 'ha group backup',
      'ha group backup <group_id1> <group_id2>', 'group')
check('虚服AAA-Radius', 'aaa radius defaultgroup',
      'aaa radius defaultgroup <radius_server_name> <group>', 'group')

print('\n  -- scope=global,group (全局+虚拟服务双级生效) --')
check('双级HA决策规则', 'ha decision rule',
      'ha decision rule <condition_name> <action_name> <group_id>', 'global,group')
check('双级SLB阈值', 'slb group threshold',
      'slb group threshold <granularity> [group_name]', 'global,group')

# =====================================================
print('\n' + '='*65)
print('【三、参数语法类型分层】')
print('='*65)

print('\n  -- {x|y} 枚举，必选择一 --')
check('rank枚举 on|off', 'aaa method rank',
      'aaa method rank {on|off} <virtual_service>', 'global')
check('SSO枚举 on|off', 'aaa sso',
      'aaa sso {on|off} <virtual_service>', 'global')
check('会话复用枚举', 'aaa session virtual reuse',
      'aaa session virtual reuse {on|off} <virtual_service>', 'global')

print('\n  -- [x|y] 可选枚举，可不选 --')
check('Bond接口 [1|0]', 'bond interface',
      'bond interface <bond_name> <if_name> [1|0]', 'global')
check('RTS路由 [all|gw]', 'ip rts on',
      'ip rts on [all|gateway]', 'global')

print('\n  -- 纯必选 <req>，全参数必填 --')
check('LDAP静态DN绑定', 'aaa ldap bind static',
      'aaa ldap bind static <ldap_server_name> <dn_prefix> <dn_suffix>', 'global')
check('LDAP属性组', 'aaa ldap attribute group',
      'aaa ldap attribute group <server_name> <attribute>', 'global')

print('\n  -- <req> + [opt] 混合 --')
check('LDAP空闲时间', 'aaa ldap idletime',
      'aaa ldap idletime <server_name> [idle_time]', 'global')
check('AAA方法服务器', 'aaa method server',
      'aaa method server <method_name> <auth_svr> [authz_svr] [display]', 'global')
check('LDAP Host多参', 'aaa ldap host',
      'aaa ldap host <name> <ip> <port> <user> <pwd> <base_dn> <timeout> [index] [tls]', 'global')

# =====================================================
print('\n' + '='*65)
print('【四、no / show / clear 命令】')
print('='*65)

print('\n  -- show 命令 (查询状态/配置，is_variant=True 变体) --')
check('show LDAP绑定', 'show aaa ldap bind',
      'show aaa ldap bind [ldap_server_name]', 'global')
check('show SLB虚IP', 'show slb virtual',
      'show slb virtual [virtual_name]', 'global')
check('show HA状态', 'show ha',
      'show ha', 'global')

print('\n  -- clear 命令 (清除统计/会话/配置) --')
check('clear AAA-SSO', 'clear aaa sso',
      'clear aaa sso', 'global')
check('clear 缓存设置', 'clear cache settings',
      'clear cache settings', 'global')
check('clear ACL黑名单统计', 'clear statistics acl blacklist',
      'clear statistics acl blacklist', 'global')

print('\n  -- no 命令 (复位/取消配置) --')
check('no 转发模式', 'no fwd mode',
      'no fwd mode', 'global')
check('no HTTP日志', 'no log http',
      'no log http', 'global')

# =====================================================
print('\n' + '='*65)
print('【五、GraphRAG 关系质量抽查 (AAA模块)】')
print('='*65)
aaa_rels = rels[rels['source'].str.startswith('AAA', na=False)].head(8)
for _, r in aaa_rels.iterrows():
    desc = r['description'][:50] if isinstance(r['description'], str) else ''
    print(f'  {r["source"]} → {r["target"]}  [{desc}]')

print('\n' + '='*65)
print('检查完毕')
