"""
从 KB 真实命令中按 scope / 语法类型 / 模块 分层抽样，
再对 Qdrant 和 GraphRAG 做双源核查。
不构造任何不存在的命令。
"""
import re, json, collections
import pandas as pd
from qdrant_client import QdrantClient

# ===== 加载 =====
with open('INAGENT/knowledge_base/reference/knowledge_base.json', encoding='utf-8') as f:
    kb = json.load(f)
blocks = kb if isinstance(kb, list) else kb.get('blocks', [])
entities = pd.read_parquet('INAGENT/graphrag_index/output/entities.parquet')
rels     = pd.read_parquet('INAGENT/graphrag_index/output/relationships.parquet')
client   = QdrantClient(path=r'C:\Users\jiang\AppData\Local\INAGENT\vector_store\qdrant')
qdrant_all, _ = client.scroll('workflow_rag', limit=5000, with_payload=True)
print(f'KB {len(blocks)}b | Qdrant {len(qdrant_all)}p | GraphRAG {len(entities)}e')

# ===== 工具 =====
def get_syntax(pc):
    m = re.search(r'语法:\s*(.+)', pc)
    return m.group(1).strip() if m else ''

def get_scope(pc):
    m = re.search(r'适用范围:\s*(.+)', pc)
    return m.group(1).strip() if m else ''

def find_qdrant(cmd_text):
    q = cmd_text.lower()
    hits = [r for r in qdrant_all if q in r.payload.get('text', '').lower()]
    cats = {r.payload.get('metadata', {}).get('regex_metadata', {}).get('document_category', '?')
            for r in hits}
    return len(hits), cats

def find_graphrag(func_key):
    key = func_key.upper()
    exact = entities[entities['title'] == key]
    if not exact.empty:
        return [(r['title'], r['type']) for _, r in exact.iterrows()][:2]
    prefix = entities[entities['title'].str.startswith(key + '_', na=False)]
    if not prefix.empty:
        return [(r['title'], r['type']) for _, r in prefix.head(2).iterrows()]
    # 去前缀重试
    for pfx in ('SHOW_', 'CLEAR_', 'NO_'):
        if key.startswith(pfx):
            return find_graphrag(func_key[len(pfx):])
    return []

def check(func_key, label=''):
    b = block_idx.get(func_key)
    if not b:
        print(f'  ⚠️  [{func_key}] 在 KB 中不存在！')
        return
    pc   = b['page_content']
    meta = b['metadata']
    syn  = get_syntax(pc)
    scope = meta.get('scope', get_scope(pc))
    mod   = meta.get('product_module', '?')

    # Qdrant
    # 用命令前缀 (command_prefix) 搜索，最精确
    prefix = meta.get('command_prefix', '')
    cnt, cats = (0, set())
    if prefix:
        cnt, cats = find_qdrant(prefix)
    if cnt == 0:
        cnt, cats = find_qdrant(func_key.replace('_', ' '))

    q_ok   = cats == {'cli/reference'}
    q_tag  = f'✅ {cnt}条' if q_ok else (f'⚠️ 0条' if cnt == 0 else f'❌ cats={cats}')

    # GraphRAG
    g = find_graphrag(func_key)
    if g:
        g_tag = '✅ ' + ' | '.join(f'{t}({tp})' for t, tp in g)
    else:
        g_tag = '⚠️  无实体'

    tag = f'[{label}] ' if label else ''
    print(f'\n  {tag}func={func_key}')
    print(f'  语法 : {syn}')
    print(f'  scope: {scope}  module: {mod}')
    print(f'  Qdrant   : {q_tag}')
    print(f'  GraphRAG : {g_tag}')

block_idx = {b['metadata']['func']: b for b in blocks if 'func' in b.get('metadata', {})}

# ===== 1. 真实命令分层枚举 =====
print('\n===== 从 KB 真实枚举代表性命令 =====\n')

# 按 scope / 语法类型 收集
buckets = {
    'scope_global':  [],  # scope=global (仅全局，不含 group)
    'scope_group':   [],  # scope=group  (虚拟服务级)
    'scope_both':    [],  # scope=global,group (双级)
    'syn_enum_brace':    [],  # {x|y}
    'syn_enum_bracket':  [],  # [x|y]
    'syn_req_only':      [],  # 仅 <req>，无 []
    'syn_mixed':         [],  # <req> + [opt]
    'show_cmds':  [],
    'clear_cmds': [],
    'no_cmds':    [],
}

for b in blocks:
    meta = b.get('metadata', {})
    func = meta.get('func', '')
    if not func:
        continue
    pc    = b['page_content']
    syn   = get_syntax(pc)
    scope = meta.get('scope', get_scope(pc))
    is_var = meta.get('is_variant', False)
    mod   = meta.get('product_module', '')

    # show / clear / no (含 variant)
    if func.startswith('show_'):
        buckets['show_cmds'].append((func, syn, scope, mod))
    if func.startswith('clear_'):
        buckets['clear_cmds'].append((func, syn, scope, mod))
    if func.startswith('no_') and not func.startswith('no_debug'):
        buckets['no_cmds'].append((func, syn, scope, mod))

    # scope 分层（只取非 variant 主命令）
    if is_var:
        continue
    if scope == 'global':
        buckets['scope_global'].append((func, syn, scope, mod))
    elif scope == 'group':
        buckets['scope_group'].append((func, syn, scope, mod))
    elif 'global' in scope and 'group' in scope:
        buckets['scope_both'].append((func, syn, scope, mod))

    # 语法类型
    if '{' in syn and '|' in syn and '}' in syn:
        buckets['syn_enum_brace'].append((func, syn, scope, mod))
    if '[' in syn and '|' in syn and ']' in syn and '{' not in syn:
        buckets['syn_enum_bracket'].append((func, syn, scope, mod))
    if '<' in syn and '[' not in syn:
        buckets['syn_req_only'].append((func, syn, scope, mod))
    if '<' in syn and '[' in syn and '|' not in syn:
        buckets['syn_mixed'].append((func, syn, scope, mod))

print('各桶数量:')
for k, v in buckets.items():
    print(f'  {k}: {len(v)}')

# 从各桶中按模块多样性选 2-3 条
def pick_diverse(lst, n=3):
    """从不同模块各取一条，最多取 n 条"""
    seen_mod = set()
    out = []
    for func, syn, scope, mod in lst:
        # 取模块根（下划线前第一段，且去 show_/clear_/no_ 前缀）
        base_mod = re.sub(r'^(show|clear|no)_', '', func).split('_')[0]
        if base_mod not in seen_mod:
            seen_mod.add(base_mod)
            out.append((func, syn, scope, mod))
        if len(out) >= n:
            break
    return out

PICKS = {}
for key, lst in buckets.items():
    PICKS[key] = pick_diverse(lst, n=3)

print('\n===== 选定代表命令 =====')
for k, items in PICKS.items():
    print(f'\n  [{k}]')
    for func, syn, scope, mod in items:
        print(f'    {func}  scope={scope}  mod={mod}')
        print(f'    语法: {syn}')

# ===== 2. 全库清洁度核查 =====
print('\n' + '='*65)
print('【一、全库清洁度】')
print('='*65)

ALLOWED = {'COMMAND','PARAMETER','PRODUCT_MODULE','PROTOCOL','FEATURE','CONFIGURATION'}
qdrant_cats = collections.Counter(
    r.payload.get('metadata',{}).get('regex_metadata',{}).get('document_category','?')
    for r in qdrant_all)
dirty_ents = entities[~entities['type'].str.upper().isin(ALLOWED)]
print(f'Qdrant: {sum(qdrant_cats.values())} 点  分布: {dict(qdrant_cats)}')
if qdrant_cats.keys() == {'cli/reference'}:
    print('  ✅ Qdrant 全部 cli/reference，零污染')
else:
    print(f'  ❌ 发现非 cli/reference: {dict(qdrant_cats)}')
print(f'GraphRAG: {len(entities)} 实体  脏实体: {len(dirty_ents)}')
if dirty_ents.empty:
    print('  ✅ GraphRAG 零脏实体')
else:
    print(dirty_ents[['title','type']].head(10).to_string())

# ===== 3. 双源分层抽样检查 =====
print('\n' + '='*65)
print('【二、scope 权限/配置级别分层 (真实命令)】')
print('='*65)

print('\n  ── scope=global  仅全局配置，不进虚拟服务上下文 ──')
check('aaa_ldap_attribute_group',  '全局·AAA·LDAP属性组')
check('bgp_asn',                   '全局·BGP·自治系统号')
check('slb_virtual',               '全局·SLB·创建虚拟服务')

print('\n  ── scope=group  需先进入虚拟服务 (group) 上下文 ──')
check('aaa_ldap_defaultgroup',     '虚拟服务级·AAA·LDAP默认组')
check('ha_group_backup',           '虚拟服务级·HA·备份组')
check('health_proxyip_group_kern', '虚拟服务级·Health·代理IP组')

print('\n  ── scope=global,group  全局+虚拟服务双级生效 ──')
check('ha_decision_rule',          '双级·HA·决策规则')
check('slb_group_health_threshold','双级·SLB·组健康阈值')
check('segment_ha_decision_rule',  '双级·Segment·HA决策')

print('\n' + '='*65)
print('【三、参数语法类型分层 (真实命令)】')
print('='*65)

print('\n  ── {x|y|…} 枚举型：必须从选项中选一个 ──')
check('acl_rule_add',              '全局·ACL·规则 — {netmask|prefix} {acl_mode…}')
check('bgp_network',               '全局·BGP·网络 — {netmask|prefix}')
check('show_aaa_method_rank',      '变体·AAA·method rank — {on|off}')

print('\n  ── [x|y|…] 可选枚举：从选项中选一个或不选 ──')
check('bond_interface_add',        '全局·Bond·接口 — [1|0]')
check('ha_group_port',             '虚拟服务级·HA·端口 — [bond|l2_vip]')
check('hcsettime',                 '全局·Health·间隔 — [real_service|add_hc_name]')

print('\n  ── 纯必选 <req>：全部参数必填 ──')
check('bgp_asn',                   '全局·BGP·AS号 — <as_number>')
check('ha_decision_rule',          '双级·HA·决策规则 — 三个<req>参数')
check('health_proxyip_group_kern', '虚拟服务级·Health·代理IP组 — <group> <pool>')

print('\n  ── <req> + [opt] 混合：部分可选 ──')
check('bond_hc_icmp_on',           '全局·Bond·健康检查 — 多<req>+多[opt]')
check('bridge_member_add',         '全局·Bridge·成员 — <bridge> <if> [broadcast]')
check('slb_group_health_threshold','双级·SLB·组阈值 — <granularity> [group]')

print('\n' + '='*65)
print('【四、no / show / clear 命令 (真实存在)】')
print('='*65)

print('\n  ── show — 查询状态/配置 ──')
check('show_aaa_ldap_bind',        'AAA LDAP绑定查询')
check('show_separation_mode',      '管理员分离模式查询')
check('show_ha_decision',          'HA决策规则查询')

print('\n  ── clear — 清除统计/会话/配置 ──')
check('clear_aaa_oauth_bind',      'AAA OAuth绑定清除')
check('clear_cache_settings',      '缓存设置清除')
check('clear_accessgroup',         '访问控制组清除')

print('\n  ── no — 取消/复位配置 ──')
check('no_fwd_mode',               '转发模式复位')
check('no_proxy_logtype',          'HTTP日志类型复位')
check('no_sdns_record',            'SDNS记录清除')

# ===== 4. 关系质量抽查 =====
print('\n' + '='*65)
print('【五、GraphRAG 关系质量 (跨模块)】')
print('='*65)

for mod in ['ACL', 'HA', 'BOND', 'BGP', 'SLB']:
    mod_rels = rels[rels['source'].str.startswith(mod, na=False)].head(4)
    if not mod_rels.empty:
        print(f'\n  --- {mod} 模块关系 ---')
        for _, r in mod_rels.iterrows():
            desc = str(r.get('description',''))[:45] if r.get('description') else ''
            print(f'  {r["source"]} → {r["target"]}  [{desc}]')

print('\n' + '='*65)
print('检查完毕')
