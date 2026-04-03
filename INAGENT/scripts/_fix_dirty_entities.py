"""
清洗 entities.parquet 中的脏实体类型（post-processing，无需重跑构建）

修复规则:
  A. 带引号 title/type → strip 引号（根本修复在 graph_extractor.py，此处兜底）
  B. function 类型 → command
  C. entity_type 是命令名/模块名（非 ALLOWED 的非空值）→ command
  D. 空 type → 优先读 description（LLM 写的），其次看 title 通用前缀

合法类型 / 推断规则 / 兜底类型 均从 graphrag_adapter 统一维护：
  新增或调整实体类型 → 只需改 graphrag_adapter.py，此脚本无需修改
"""
import re, sys, shutil
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from INAGENT.rag.graphrag_adapter import (
    DEFAULT_ENTITY_TYPES,
    ENTITY_TYPE_INFER_RULES,
    ENTITY_TYPE_FALLBACK,
)

PARQUET = Path('INAGENT/graphrag_index/output/entities.parquet')
BACKUP  = Path('INAGENT/graphrag_index/output/entities.parquet.bak')

# 合法类型集合（大写），从权威列表派生
ALLOWED: set[str] = {t.upper() for t in DEFAULT_ENTITY_TYPES}

# title 前缀：无论类型如何变化，SHOW_/CLEAR_/NO_ 开头必定是命令
_CMD_PREFIXES = ('SHOW_', 'CLEAR_', 'NO_')

def infer_type(title: str, description: str = '') -> str:
    """
    从 description 优先，其次 title 推断实体类型。
    规则定义在 graphrag_adapter.ENTITY_TYPE_INFER_RULES。
    """
    desc = (description or '').lower()
    t = title.strip().strip('"').upper()

    # 1. 用 description 匹配（LLM 亲写，语义最准）
    for pattern, typ in ENTITY_TYPE_INFER_RULES:
        if re.search(pattern, desc, re.IGNORECASE):
            return typ

    # 2. title 通用前缀（无歧义）
    if any(t.startswith(p) for p in _CMD_PREFIXES):
        return 'command'

    # 3. 兜底
    return ENTITY_TYPE_FALLBACK

entities = pd.read_parquet(PARQUET)
print(f'加载: {len(entities)} 实体')

orig_dirty = entities[~entities['type'].str.upper().isin(ALLOWED)]
print(f'修复前脏实体: {len(orig_dirty)}')

# 备份
shutil.copy2(PARQUET, BACKUP)
print(f'已备份到 {BACKUP}')

fixed_count = 0
new_types = entities['type'].tolist()

for idx, row in entities.iterrows():
    t = str(row['type'])
    t_up = t.upper().strip()

    if t_up in ALLOWED:
        continue  # 正常，跳过

    # B: function → command
    if t_up == 'FUNCTION':
        new_types[idx] = 'command'
        fixed_count += 1

    # C: 非空但不在 ALLOWED，且 type 值本身看起来是命令名/模块名
    elif t_up not in ALLOWED and t.strip() != '':
        # 如果 type 看起来是 llb_xxx 函数名或 show_xxx 命令名 → command
        if re.match(r'^[a-z_]+$', t.lower()) or re.match(r'^"[a-z_]+"$', t.lower()):
            new_types[idx] = 'command'
        else:
            new_types[idx] = infer_type(row['title'], row.get('description', ''))
        fixed_count += 1

    # D: 空 type → 描述优先推断
    elif t.strip() == '':
        new_types[idx] = infer_type(row['title'], row.get('description', ''))
        fixed_count += 1

entities['type'] = new_types

# 统一将 ALLOWED 类型的 type 升为大写
entities['type'] = entities['type'].map(
    lambda t: t.upper() if isinstance(t, str) and t.upper() in ALLOWED else t
)

# 验证
remaining = entities[~entities['type'].str.upper().isin(ALLOWED)]
print(f'\n修复后脏实体: {len(remaining)}')
if not remaining.empty:
    print('剩余脏实体:')
    for _, r in remaining.iterrows():
        print(f'  title={r["title"]!r}  type={r["type"]!r}')
else:
    print('✅ 全部干净')

print(f'\n修复数量: {fixed_count}')
print(f'类型分布:')
print(entities['type'].value_counts().to_string())

# 写回
entities.to_parquet(PARQUET, index=False)
print(f'\n✅ 已写回 {PARQUET}')
