"""
修复 entities.parquet 中 title 字段带引号的 547 条实体
（之前 _fix_dirty_entities.py 只修了 type 字段）

规则:
  - title 以 " 开头/结尾 → strip 引号
  - title = 'RELATIONSHIP"' 这类只有尾引号 → 也 strip
"""
import re, shutil
import pandas as pd
from pathlib import Path

PARQUET = Path('INAGENT/graphrag_index/output/entities.parquet')
BACKUP  = Path('INAGENT/graphrag_index/output/entities.parquet.title_bak')

entities = pd.read_parquet(PARQUET)
print(f'加载: {len(entities)} 实体')

# 统计带引号的 title
def has_quote(t):
    s = str(t)
    return s.startswith('"') or s.endswith('"')

before = entities['title'].apply(has_quote).sum()
print(f'修复前 title 有引号: {before} 条')

# 备份
shutil.copy2(PARQUET, BACKUP)
print(f'已备份到 {BACKUP}')

# 修复
def clean_title(t):
    s = str(t).strip()
    # strip leading/trailing quotes
    s = s.strip('"')
    return s

entities['title'] = entities['title'].apply(clean_title)

after = entities['title'].apply(has_quote).sum()
print(f'修复后 title 有引号: {after} 条')

if after == 0:
    print('✅ title 全部干净')
else:
    print('剩余:')
    for _, r in entities[entities['title'].apply(has_quote)].iterrows():
        print(f'  {r["title"]!r}  type={r["type"]}')

entities.to_parquet(PARQUET, index=False)
print(f'\n已写回 {PARQUET}')
print(f'修复数量: {before - after}')

# 验证 type 字段仍干净
ALLOWED = {'COMMAND','PARAMETER','PRODUCT_MODULE','PROTOCOL','FEATURE','CONFIGURATION'}
dirty_types = entities[~entities['type'].str.upper().isin(ALLOWED)]
print(f'type 脏实体: {len(dirty_types)} (应为0)')

# 抽查几条修复后的 SLB/HTTP/DPI 实体
print('\n抽查 SLB_GROUP_THRESHOLD:')
for _, r in entities[entities['title'] == 'SLB_GROUP_THRESHOLD'].iterrows():
    print(f'  title={r["title"]}  type={r["type"]}  desc={str(r.get("description",""))[:60]}')

print('抽查 DPI_ON:')
for _, r in entities[entities['title'] == 'DPI_ON'].iterrows():
    print(f'  title={r["title"]}  type={r["type"]}')

print('\n类型分布:')
print(entities['type'].value_counts().to_string())
