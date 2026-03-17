#!/usr/bin/env python3
"""为已有 LanceDB 表补充缺失的 attributes 列。

GraphRAG 的 LanceDBVectorStore 要求表包含 4 列: id, text, vector, attributes。
merge_graphrag_delta.py 之前重建时只写了 3 列，导致 local_search 出现 KeyError。
此脚本直接在现有表上添加 attributes 列（值为 "{}"），无需重新 embedding。
"""
import sys
from pathlib import Path

LANCE_DIR = Path(__file__).resolve().parent.parent / "graphrag_index" / "output" / "lancedb"

TABLES = [
    "default-entity-description",
    "default-community-full_content",
    "default-text_unit-text",
]


def patch():
    try:
        import lancedb
    except ImportError:
        print("ERROR: lancedb 未安装, 请先 pip install lancedb")
        sys.exit(1)

    if not LANCE_DIR.exists():
        print(f"ERROR: LanceDB 目录不存在: {LANCE_DIR}")
        sys.exit(1)

    db = lancedb.connect(str(LANCE_DIR))
    existing_tables = db.table_names()
    print(f"LanceDB 目录: {LANCE_DIR}")
    print(f"已有表: {existing_tables}")

    for tname in TABLES:
        if tname not in existing_tables:
            print(f"\n  [跳过] 表 '{tname}' 不存在")
            continue

        tbl = db.open_table(tname)
        schema = tbl.schema
        col_names = [f.name for f in schema]
        print(f"\n  表 '{tname}': {len(tbl)} 行, 列={col_names}")

        if "attributes" in col_names:
            print(f"    ✓ attributes 列已存在, 跳过")
            continue

        # 读取全部数据
        print(f"    添加 attributes 列...")
        import pyarrow as pa

        full_data = tbl.to_arrow()
        n_rows = full_data.num_rows

        # 添加 attributes 列 (全部为 "{}")
        attrs_array = pa.array(["{}"] * n_rows, type=pa.string())
        new_table = full_data.append_column("attributes", attrs_array)

        # 重写表
        db.drop_table(tname)
        db.create_table(tname, new_table)

        # 验证
        verify_tbl = db.open_table(tname)
        verify_cols = [f.name for f in verify_tbl.schema]
        print(f"    ✓ 完成: {len(verify_tbl)} 行, 列={verify_cols}")

    print("\n补丁完成!")


if __name__ == "__main__":
    patch()
