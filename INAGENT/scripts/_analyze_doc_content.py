"""
全面分析 19 个文档的内容类型，找出现有 entity_types 未覆盖的信息。
"""
import json
from pathlib import Path

full = json.loads(Path('INAGENT/graphrag_index/documents_full.json.bak').read_text('utf-8'))

# 按文档类型分组采样
groups = {
    "spec_design": [],       # 设计文档
    "spec_func_spec": [],    # 功能规格书
    "spec_prd": [],          # PRD
    "test_list": [],         # 测试列表
    "cli_ref": [],           # CLI参考
    "app_config": [],        # 应用配置
}

for d in full:
    text = d["text"]
    title = d["title"]
    if "test" in title.lower() or "test_list" in text[:300].lower():
        groups["test_list"].append(d)
    elif "spec/func_spec" in text[:300]:
        groups["spec_func_spec"].append(d)
    elif "spec/design" in text[:300]:
        groups["spec_design"].append(d)
    elif "spec/prd" in text[:300]:
        groups["spec_prd"].append(d)
    elif title.endswith(".json"):
        groups["cli_ref"].append(d)
    elif title.endswith(".pdf"):
        groups["cli_ref"].append(d)
    else:
        groups["app_config"].append(d)

for gname, docs in groups.items():
    print(f"\n{'='*80}")
    print(f"GROUP: {gname} ({len(docs)} docs)")
    print('='*80)
    for d in docs:
        print(f"\n--- {d['id']}: {d['title']} ({len(d['text'])} chars) ---")
        # 取5段不同位置的采样
        text = d["text"]
        total = len(text)
        positions = [0, total//5, total*2//5, total*3//5, total*4//5]
        for i, pos in enumerate(positions):
            chunk = text[pos:pos+400].replace("\n", " \\n ")
            print(f"  [sample@{pos}]: {chunk[:350]}")
        print()
