import json, re, sys
from pathlib import Path

KB = Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json"

kb = json.load(open(KB, encoding="utf-8"))
chunks = kb if isinstance(kb, list) else kb.get("chunks", [])

results = {"enum": [], "mixed": [], "neg": [], "cfg": []}
for c in chunks:
    text = c.get("page_content", "")
    meta = c.get("metadata", {})
    mod  = meta.get("product_module", "")
    scope = meta.get("scope", "")

    cmd_m = re.search(r"\[命令\]\s*(.+)", text)
    syn_m = re.search(r"语法:\s*(.+)", text)
    if not cmd_m or not syn_m:
        continue
    cmd = cmd_m.group(1).strip()
    syn = syn_m.group(1).strip()

    has_req  = "<" in syn
    has_opt  = "[" in syn
    has_enum = "{" in syn
    is_neg   = cmd.split()[0] in ("no", "show", "clear")

    entry = dict(cmd=cmd, mod=mod, syn=syn, scope=scope, text=text)

    if has_enum and len(results["enum"]) < 3:
        results["enum"].append(entry)
    if has_req and has_opt and not is_neg and len(results["mixed"]) < 3:
        results["mixed"].append(entry)
    if is_neg and len(results["neg"]) < 4:
        results["neg"].append(entry)
    if has_req and not is_neg and not has_enum and len(results["cfg"]) < 3:
        results["cfg"].append(entry)

    if all(len(v) >= 3 for v in results.values()):
        break

for label, key in [("枚举 {x|y}", "enum"), ("必选+可选", "mixed"), ("no/show/clear", "neg"), ("配置<必选>", "cfg")]:
    print(f"\n{'='*60}")
    print(f"【{label}】")
    for e in results[key]:
        mod = e["mod"]
        print(f"\n  ✦ {e['cmd']}")
        print(f"    module={mod}  scope={e['scope']}")
        print(f"    语法: {e['syn'][:100]}")
        # 显示参数
        for line in e["text"].split("\n"):
            if line.strip().startswith(("<", "[", "  <", "  [")):
                print("      " + line.strip()[:80])
