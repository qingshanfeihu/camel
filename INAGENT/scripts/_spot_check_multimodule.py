"""
多模块扩展 spot check：SLB / SSL / HTTP / SDNS / HA / LLB / ACL / Health
同时汇报脏实体根因统计
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

qdrant_all, _ = client.scroll('workflow_rag', limit=5000, with_payload=True)
print(f'加载完成: Qdrant {len(qdrant_all)}p | GraphRAG {len(entities)}e {len(rels)}r | KB {len(blocks)}b')

ALLOWED = {'command','parameter','product_module','protocol','feature','configuration'}

def find_qdrant(cmd_text):
    q = cmd_text.lower()
    hits = [r for r in qdrant_all if q in r.payload.get('text','').lower()]
    cats = {r.payload.get('metadata',{}).get('regex_metadata',{}).get('document_category','?') for r in hits}
    return len(hits), cats

def find_graphrag(cmd_prefix):
    # 规范化为大写下划线，去掉 show_/clear_/no_ 前缀再递归
    key = cmd_prefix.upper().replace(' ','_').replace('-','_')
    exact = entities[entities['title'] == key]
    if not exact.empty:
        return [(r['title'], r['type']) for _, r in exact.iterrows()]
    prefix = entities[entities['title'].str.startswith(key + '_', na=False)]
    if not prefix.empty:
        return [(r['title'], r['type']) for _, r in prefix.head(3).iterrows()]
    for pfx in ('SHOW_','CLEAR_','NO_'):
        if key.startswith(pfx):
            return find_graphrag(cmd_prefix[len(pfx):].strip())
    return []

def get_syntax(func_key):
    for b in blocks:
        if b.get('metadata',{}).get('func','') == func_key:
            m = re.search(r'语法:\s*(.+)', b.get('page_content',''))
            return m.group(1).strip() if m else ''
    return ''

def check(label, cmd_prefix, syntax_override=None):
    syntax = syntax_override or get_syntax(cmd_prefix.replace(' ','_'))
    cnt, cats = find_qdrant(cmd_prefix)
    if cats == {'cli/reference'}:
        q = f'✅ {cnt}条'
    elif cnt == 0:
        q = f'⚠️ 0条'
    else:
        q = f'❌ cats={cats}'

    g = find_graphrag(cmd_prefix)
    if g:
        # 检查是否有脏类型
        dirty_g = [(t,tp) for t,tp in g if tp.lower().strip('"') not in ALLOWED]
        if dirty_g:
            g_tag = f'❌ 脏类型 {dirty_g[:2]}'
        else:
            g_tag = '✅ ' + ' | '.join(f'{t}({tp})' for t,tp in g[:2])
    else:
        g_tag = '⚠️  无实体'

    print(f'\n  [{label}]')
    if syntax:
        print(f'  语法 : {syntax}')
    print(f'  Qdrant   : {q}')
    print(f'  GraphRAG : {g_tag}')

# =====================================================
print('\n' + '='*65)
print('【SLB — Server Load Balancing 模块】')
print('='*65)
check('slb virtual 虚拟IP定义',       'slb virtual')
check('slb real 真实服务器',           'slb real')
check('slb group 服务器组',            'slb group')
check('slb virtual bindport 端口绑定', 'slb virtual bindport')
check('slb ssl profile SSL配置',       'slb ssl profile')
check('show slb virtual',             'show slb virtual')
check('show slb real',                'show slb real')
check('no slb virtual 删除虚IP',       'no slb virtual')

print('\n' + '='*65)
print('【SSL/TLS 模块】')
print('='*65)
check('ssl profile 创建profile',      'ssl profile')
check('ssl cert 证书绑定',            'ssl cert')
check('ssl client auth 客户端认证',   'ssl client auth')
check('ssl cipher 加密套件',          'ssl cipher')
check('show ssl profile',             'show ssl profile')
check('no ssl cert',                  'no ssl cert')

print('\n' + '='*65)
print('【HTTP 模块】')
print('='*65)
check('http rewrite URL重写',         'http rewrite')
check('http redirect 重定向策略',     'http redirect')
check('http insert header 注入头',    'http insert header')
check('http compress 压缩',           'http compress')
check('show http rewrite',            'show http rewrite')

print('\n' + '='*65)
print('【SDNS — 全局DNS负载均衡模块】')
print('='*65)
check('sdns virtual 虚拟域名',        'sdns virtual')
check('sdns record DNS记录',          'sdns record')
check('sdns ttl TTL设置',             'sdns ttl')
check('show sdns virtual',            'show sdns virtual')
check('clear sdns',                   'clear sdns')

print('\n' + '='*65)
print('【HA — 高可用模块】')
print('='*65)
check('ha active active 双活',        'ha active active')
check('ha group heartbeat 心跳',      'ha group heartbeat')
check('ha decision rule 决策规则',    'ha decision rule')
check('ha group preemption 抢占',     'ha group preemption')
check('show ha',                      'show ha')
check('no ha group',                  'no ha group')

print('\n' + '='*65)
print('【LLB — 链路负载均衡模块】')
print('='*65)
check('llb link 链路配置',            'llb link')
check('llb rsite 远端站点',           'llb rsite')
check('llb method outbound 出站算法', 'llb method outbound')
check('llb dns host DNS主机',         'llb dns host')
check('show llb link',                'show llb link')
check('clear llb link',               'clear llb link')

print('\n' + '='*65)
print('【Health Check — 健康检查模块】')
print('='*65)
check('health check http HTTP探测',   'health check http')
check('health check tcp TCP探测',     'health check tcp')
check('health interval 检查间隔',     'health interval')
check('show health',                  'show health')

print('\n' + '='*65)
print('【ACL / Filter 模块】')
print('='*65)
check('acl policy ACL策略',           'acl policy')
check('acl rule ACL规则',             'acl rule')
check('filter group 过滤组',          'filter group')
check('clear statistics acl',         'clear statistics acl')

# =====================================================
print('\n' + '='*65)
print('【脏实体根因分析】')
print('='*65)

ALLOWED_UP = {'COMMAND','PARAMETER','PRODUCT_MODULE','PROTOCOL','FEATURE','CONFIGURATION'}
dirty = entities[~entities['type'].str.upper().str.strip('"').isin(ALLOWED_UP)]
print(f'\n脏实体总数: {len(dirty)} / {len(entities)} ({len(dirty)/len(entities)*100:.1f}%)')

# 细分三类根因
empty_type   = dirty[dirty['type'].str.strip() == '']
quoted_type  = dirty[dirty['type'].str.startswith('"', na=False)]
other_type   = dirty[(dirty['type'].str.strip() != '') & (~dirty['type'].str.startswith('"', na=False))]

print(f'\n根因分类:')
print(f'  A. 空 type (LLM未给类型):   {len(empty_type)} 条')
print(f'     → 这些实体实为参数名/功能概念，title含括号/引号的是格式泄漏')
print(f'  B. 带JSON引号的type:          {len(quoted_type)} 条')
print(f'     → LLM 输出 "command" 而非 command，graphrag parser 未清洁')
print(f'  C. 其他非法type (function等): {len(other_type)} 条')
other_tc = collections.Counter(other_type['type'].tolist())
for k,v in other_tc.most_common(5):
    print(f'     {k!r}: {v}')

print(f'\n样本-B (带引号type):')
for _, r in quoted_type.head(5).iterrows():
    print(f'  type={r["type"]!r}  title={r["title"]!r}')

print(f'\n修复方案:')
print(f'  A. → 改 prompt：无法确定类型时缺省归为 parameter')
print(f'  B. → 改 graphrag_adapter.py：提取后 strip 掉类型字段的引号')
print(f'  C. → 改 prompt：明确禁止输出 function 类型，遇到内部函数名跳过')
