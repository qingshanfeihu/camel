# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
导出所�?product_module �?unknown 的文档块到临时文件，便于人工检查是否为无用产品信息（版权、目录等）�?
用法: python -m INAGENT.scripts.export_unknown_module_chunks
"""
import json
import os
import sys
from pathlib import Path

# 确保 INAGENT �?path �?
INAGENT_DIR = Path(__file__).resolve().parent.parent
if str(INAGENT_DIR) not in sys.path:
    sys.path.insert(0, str(INAGENT_DIR.parent))

KB_PATH = INAGENT_DIR / "knowledge_base" / "reference" / "knowledge_base.json"
OUT_JSON = INAGENT_DIR / "knowledge_base" / "unknown_module_chunks_export.json"
OUT_TXT = INAGENT_DIR / "knowledge_base" / "unknown_module_chunks_export.txt"


def main():
    if not KB_PATH.exists():
        print(f"[错误] 知识库不存在: {KB_PATH}")
        return 1

    with open(KB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    chunks = data if isinstance(data, list) else data.get("chunks", [])
    unknown_chunks = []
    for chunk in chunks:
        meta = chunk.get("metadata", {}) or {}
        pm = meta.get("product_module") or (meta.get("regex_metadata") or {}).get("product_module")
        if pm and str(pm).strip().lower() == "unknown":
            unknown_chunks.append(chunk)

    print(f"知识库总块�? {len(chunks)}")
    print(f"product_module=unknown 的块�? {len(unknown_chunks)}")

    # 写入 JSON（完整结构，便于程序使用�?
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(unknown_chunks, f, ensure_ascii=False, indent=2)

    # 写入 TXT（便于人工快速浏览内容，判断是否全是版权/目录等无用信息）
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(f"# product_module=unknown 的文档块导出（共 {len(unknown_chunks)} 条）\n")
        f.write("# 请检查是否全部为无用产品信息（如版权、目录、关于我们等）\n\n")
        for i, chunk in enumerate(unknown_chunks, 1):
            text = chunk.get("page_content", "") or chunk.get("text", "")
            meta = chunk.get("metadata", {}) or {}
            f.write("-" * 80 + "\n")
            f.write(f"[{i}] metadata: {json.dumps(meta, ensure_ascii=False)}\n\n")
            f.write(f"content (�?2000 �?:\n{text[:2000]}\n")
            if len(text) > 2000:
                f.write("\n... (已截�?\n")
            f.write("\n")

    print(f"已导�?JSON: {OUT_JSON}")
    print(f"已导�?TXT:  {OUT_TXT}")

    # 使用系统默认程序打开 TXT，便于检�?
    if os.name == "nt":
        os.startfile(OUT_TXT)
    else:
        try:
            import subprocess
            subprocess.run(["xdg-open", str(OUT_TXT)], check=False)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
