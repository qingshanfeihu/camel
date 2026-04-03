"""
深入诊断：_real_spotcheck 中 GraphRAG "无实体" 的 8 条命令
- 打印 KB 原文，看 LLM 喂了什么
- 在 entities 里模糊搜索，看提取出了哪些相关实体
- 展示所有匹配实体的 description，判断是命名差异还是真正漏提取
"""
import json, re, pandas as pd

with open('INAGENT/knowledge_base/reference/knowledge_base.json', encoding='utf-8') as f:
    blocks = json.load(f)
if isinstance(blocks, dict):
    blocks = blocks.get('blocks', [])

block_idx = {b['metadata']['func']: b for b in blocks if 'func' in b.get('metadata', {})}

entities = pd.read_parquet('INAGENT/graphrag_index/output/entities.parquet')
rels     = pd.read_parquet('INAGENT/graphrag_index/output/relationships.parquet')

MISSING = [
    'acl_rule_add',
    'bond_interface_add',
    'hcsettime',
    'bond_hc_icmp_on',
    'slb_group_health_threshold',
    'health_proxyip_group_kern',
    'show_separation_mode',
    'no_proxy_logtype',
]

# ====== 模糊搜索：取 func_key 各 token，找包含任何一个的实体 ======
def tokens(func_key):
    """提取有意义的关键词（长度≥4，去掉数字纯token）"""
    parts = re.sub(r'^(show|clear|no)_', '', func_key).split('_')
    return [p.upper() for p in parts if len(p) >= 3 and not p.isdigit()]

def fuzzy_search(func_key, max_tokens=3):
    toks = tokens(func_key)[:max_tokens]
    if not toks:
        return pd.DataFrame()
    # 每个 token 都要包含（AND 语义）
    mask = entities['title'].str.contains(toks[0], na=False)
    for t in toks[1:]:
        mask = mask & entities['title'].str.contains(t, na=False)
    result = entities[mask]
    if result.empty and len(toks) > 1:
        # 降级：只要第一个 token
        result = entities[entities['title'].str.contains(toks[0], na=False)]
    return result

# ====== 获取实体的关系 ======
def get_rels(title):
    mask = (rels['source'] == title) | (rels['target'] == title)
    sub = rels[mask][['source', 'target', 'description']].head(5)
    return sub

# ====== 主循环 ======
for func_key in MISSING:
    print(f'\n{"="*70}')
    print(f'  MISSING: {func_key}  →  期望实体: {func_key.upper()}')
    print(f'{"="*70}')

    # 1. KB 原文
    b = block_idx.get(func_key)
    if b:
        pc = b['page_content']
        meta = b['metadata']
        syn_m = re.search(r'语法:\s*(.+)', pc)
        desc_m = re.search(r'(?:描述|功能说明|说明):\s*(.+)', pc)
        func_m = re.search(r'内部函数:\s*(.+)', pc)
        print(f'  KB func   : {meta.get("func")}')
        print(f'  KB scope  : {meta.get("scope")}')
        print(f'  KB module : {meta.get("product_module")}')
        print(f'  KB prefix : {meta.get("command_prefix")}')
        print(f'  语法      : {syn_m.group(1).strip() if syn_m else "(无)"}')
        print(f'  描述      : {(desc_m.group(1).strip()[:80] if desc_m else "(无)")}')
        print(f'  内部函数  : {(func_m.group(1).strip()[:80] if func_m else "(无)")}')
    else:
        print('  ⚠️  KB 中不存在此 func_key')

    # 2. 精确查找
    exact = entities[entities['title'] == func_key.upper()]
    print(f'\n  精确匹配 ({func_key.upper()}):')
    if not exact.empty:
        for _, r in exact.iterrows():
            print(f'    ✅ title={r["title"]}  type={r["type"]}')
            print(f'       desc: {str(r.get("description",""))[:100]}')
    else:
        print('    — 无 —')

    # 3. 模糊搜索
    toks = tokens(func_key)
    fuzzy = fuzzy_search(func_key)
    print(f'\n  模糊搜索 (tokens={toks}):  命中 {len(fuzzy)} 条')
    for _, r in fuzzy.head(8).iterrows():
        print(f'    title={r["title"]}  type={r["type"]}')
        print(f'       desc: {str(r.get("description",""))[:100]}')

    # 4. 最宽泛：只用首个主词
    first_tok = toks[0] if toks else func_key.split('_')[0].upper()
    broad = entities[entities['title'].str.startswith(first_tok, na=False)]
    if len(broad) > len(fuzzy):
        print(f'\n  宽泛搜索 (前缀={first_tok}): 共 {len(broad)} 条，前 10 条:')
        for _, r in broad.head(10).iterrows():
            print(f'    {r["title"]}  ({r["type"]})')

    # 5. 该实体在关系表里（可能命名不同但有关系）
    if not fuzzy.empty:
        print('\n  相关实体的 relationships:')
        for _, r in fuzzy.head(3).iterrows():
            sub = get_rels(r['title'])
            if not sub.empty:
                print(f'    [{r["title"]}]')
                for _, rel in sub.iterrows():
                    print(f'      {rel["source"]} → {rel["target"]} | {str(rel["description"])[:60]}')
