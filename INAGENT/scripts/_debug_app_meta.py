"""
诊断报告：APP 数据 section_path / parent_section / product_module 缺失问题
"""
import json
from pathlib import Path
from collections import Counter

ROOT = Path('INAGENT/knowledge_base/reference')

# --- 1. app_1-40.json 统计 ---
app = json.load(open(ROOT / 'app_1-40.json', 'r', encoding='utf-8'))
print(f"=== app_1-40.json 总条数: {len(app)} ===")

empty_parent = sum(1 for e in app if not e.get('metadata', {}).get('parent_section', ''))
empty_path_eq_title = sum(1 for e in app if e.get('metadata', {}).get('section_path', '') == e.get('metadata', {}).get('section_title', ''))
empty_mod = sum(1 for e in app if not e.get('metadata', {}).get('product_module', ''))
empty_cat = sum(1 for e in app if not e.get('metadata', {}).get('document_category', ''))
empty_page = sum(1 for e in app if not e.get('metadata', {}).get('page_number', ''))

print(f"  parent_section 为空: {empty_parent} ({100*empty_parent//len(app)}%)")
print(f"  section_path == section_title (无层级): {empty_path_eq_title} ({100*empty_path_eq_title//len(app)}%)")
print(f"  product_module 为空: {empty_mod} ({100*empty_mod//len(app)}%)")
print(f"  document_category 为空: {empty_cat} ({100*empty_cat//len(app)}%)")
print(f"  page_number 为空: {empty_page} ({100*empty_page//len(app)}%)")

# --- 2. 找所有 section_title='概述' 的记录 ---
guoshu = [e for e in app if e.get('metadata', {}).get('section_title', '') in ('概述', '概述：')]
print(f"\n=== section_title='概述' 的记录: {len(guoshu)} ===")
mod_dist = Counter(e.get('metadata', {}).get('product_module', '(empty)') for e in guoshu)
print(f"  product_module 分布: {dict(mod_dist)}")

# Show sample with empty product_module
empty_guoshu = [e for e in guoshu if not e.get('metadata', {}).get('product_module', '')]
print(f"  其中 product_module 为空: {len(empty_guoshu)}")
for e in empty_guoshu[:5]:
    m = e.get('metadata', {})
    pc = e.get('page_content', '')[:100]
    print(f"    p{m.get('page_idx',0)} blk{m.get('block_id',0)} | path={m.get('section_path','')!r} | content: {pc}")

# --- 3. 检查 page_idx=178 附近的 product_module 连续性 ---
p178 = sorted([e for e in app if 176 <= e.get('metadata', {}).get('page_idx', 0) <= 182],
              key=lambda e: (e.get('metadata', {}).get('page_idx', 0), e.get('metadata', {}).get('block_id', 0)))
print(f"\n=== page 176-182 blocks (HTTP压缩章节) ===")
for e in p178:
    m = e.get('metadata', {})
    print(f"  p{m.get('page_idx',0):3d} blk{m.get('block_id',0):4d} | title={m.get('section_title','')!r:30} | mod={m.get('product_module','')!r:12} | {e.get('page_content','')[:60]}")

# --- 4. 检查 section_path 层级深度分布 ---
depths = Counter()
for e in app:
    path = e.get('metadata', {}).get('section_path', '')
    depth = len(path.split('/')) if '/' in path else 1
    depths[depth] += 1
print(f"\n=== section_path 层级深度分布 ===")
for d in sorted(depths):
    print(f"  depth={d}: {depths[d]}")

# Show some multi-level paths
multi = [e for e in app if '/' in e.get('metadata', {}).get('section_path', '')]
print(f"\n=== 有父路径 (含'/') 的条目示例: {len(multi)} ===")
for e in multi[:5]:
    m = e.get('metadata', {})
    print(f"  path={m.get('section_path','')!r:50} mod={m.get('product_module','')!r}")
