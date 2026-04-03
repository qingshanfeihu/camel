"""分析脏实体"""
import pandas as pd

entities = pd.read_parquet('INAGENT/graphrag_index/output/entities.parquet')
rels = pd.read_parquet('INAGENT/graphrag_index/output/relationships.parquet')

ALLOWED = {'COMMAND','PARAMETER','PRODUCT_MODULE','PROTOCOL','FEATURE','CONFIGURATION'}
dirty = entities[~entities['type'].str.upper().isin(ALLOWED)]
print(f'脏实体总数: {len(dirty)}')
print(f'全部列: {list(entities.columns)}')
print()

empty = dirty[dirty['type'].astype(str).str.strip() == '']
print(f'空 type ({len(empty)} 个) 全部:')
for _, r in empty.iterrows():
    desc = str(r.get('description',''))[:80]
    print(f'  title={r["title"]!r}  desc={desc}')

print()
nonempty = dirty[dirty['type'].astype(str).str.strip() != '']
print(f'非空脏 type ({len(nonempty)} 个):')
for _, r in nonempty.iterrows():
    desc = str(r.get('description',''))[:80]
    print(f'  title={r["title"]!r}  type={r["type"]!r}  desc={desc}')
