"""
严格根因分析：app_1-40.json 的 parent_section='' / product_module='' 来自哪一层

步骤:
1. 用当前 auto_convert._build_section_context_map() 模拟处理 p176-p180 的块
2. 对比实际 backup 输出，确认在哪个步骤丢失了 parent/section info
3. 检查 rebuild_doc_refs chapter_inheritance 为何无法修复
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path('INAGENT').parent))

import json
from INAGENT.data_tools.auto_convert import (
    _build_section_context_map,
    _infer_section_level_from_heading,
    _remove_section_number,
)

# ── 1. 模拟 MinerU 原始块（从 page_content 重构最小块结构）──────────────────
# 按 page 178 实际内容手工构造 raw blocks (type=text, no text_level)
raw_blocks_simulation = [
    {"type": "text", "lines": [{"spans": [{"content": "15. DNS缓存"}]}]},  # chapter heading
    {"type": "text", "lines": [{"spans": [{"content": "15.1. 概述"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "本章将介绍DNS缓存（DNS Cache）的相关配置。"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "16. HTTP压缩"}]}]},  # chapter heading we care about
    {"type": "text", "lines": [{"spans": [{"content": "16.1. 概述"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "本章将介绍设备的HTTP压缩（HTTP Compression）功能。"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "设备⽀持对HTTP对象的在线压缩。此功能有助于优化特定⽹站的流量。"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "16.2. HTTP压缩的原理"}]}]},
    {"type": "text", "lines": [{"spans": [{"content": "HTTP压缩使用公共域压缩算法。"}]}]},
]

print("=== 1. 模拟 _build_section_context_map() 对 p176-178 块的输出 ===")
ctx_map = _build_section_context_map(raw_blocks_simulation)
for idx, block in enumerate(raw_blocks_simulation):
    text = block["lines"][0]["spans"][0]["content"]
    ctx = ctx_map.get(idx, {})
    print(f"  [{idx}] {text[:40]:40s} | title={ctx.get('section_title','')!r:15} | parent={ctx.get('parent_section','')!r:15} | path={ctx.get('section_path','')!r}")

# ── 2. 对比 backup 实际输出 ──────────────────────────────────────────────────
print()
print("=== 2. backup app_1-40.json 中 p176-178 的实际值 ===")
app = json.load(open('INAGENT/knowledge_base/reference/app_1-40.json', 'r', encoding='utf-8'))
p176_178 = sorted([e for e in app if 176 <= e.get('metadata',{}).get('page_idx',0) <= 178],
                  key=lambda e: e.get('metadata',{}).get('block_id',0))
seen = set()
for e in p176_178:
    m = e.get('metadata', {})
    bid = m.get('block_id', 0)
    title = m.get('section_title', '')
    parent = m.get('parent_section', '')
    path = m.get('section_path', '')
    pc = e.get('page_content', '')[:40]
    if bid not in seen:
        seen.add(bid)
        print(f"  blk{bid:4d} | title={title!r:30} | parent={parent!r:20} | path={path!r:30} | {pc}")

# ── 3. 检查 rebuild_doc_refs chapter_inheritance 的有效性 ────────────────────
print()
print("=== 3. chapter_inheritance 能修复多少 ===")
from INAGENT.scripts.rebuild_doc_refs import _fix_product_module_by_chapter_inheritance
import copy
app_copy = copy.deepcopy(app)
fixed = _fix_product_module_by_chapter_inheritance(app_copy)
print(f"  能修复: {fixed} 条")
# Check the HTTP compression blocks after fix
for e in app_copy:
    m = e.get('metadata', {})
    if m.get('page_idx', 0) == 178 and '压缩' in e.get('page_content', '')[:50]:
        print(f"  After fix: title={m.get('section_title','')!r} mod={m.get('product_module','')!r} parent={m.get('parent_section','')!r}")
        break

# ── 4. 推断 _infer_section_level_from_heading 对关键标题的识别结果 ──────────
print()
print("=== 4. _infer_section_level_from_heading 对各级标题的识别 ===")
test_titles = [
    "16. HTTP压缩",
    "16.1. 概述",
    "16.2. HTTP压缩的原理",
    "HTTP压缩",
    "概述",
    "DNS缓存",
    "安全服务链",
    "14.2.3. HTTP内容改写的工作方式",
]
for t in test_titles:
    lvl = _infer_section_level_from_heading(t)
    norm = _remove_section_number(t) if lvl else t
    print(f"  {t!r:40} -> level={lvl}, normalized={norm!r}")
