#!/usr/bin/env python3
"""
GraphRAG 索引质量验证脚本

构建完成后运行:
    python INAGENT/scripts/verify_graphrag_index.py

检查项:
  1. 所有 parquet 文件存在且非空
  2. 实体类型分布 (entity_types)
  3. 引号残留检测
  4. 社区结构合理性
  5. 文本单元覆盖率
  6. 关系质量抽样
  7. LanceDB 向量存储完整性
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

OUTPUT_DIR = Path(__file__).parent.parent / "graphrag_index" / "output"

REQUIRED_FILES = [
    "entities.parquet",
    "relationships.parquet",
    "communities.parquet",
    "community_reports.parquet",
    "text_units.parquet",
    "documents.parquet",
]

EXPECTED_ENTITY_TYPES = {
    "product_module", "protocol", "feature", "design_knowledge",
    "command", "parameter", "step_type", "configuration",
    "config_example", "requirement", "scenario", "test_case",
    "test_standard", "business_state", "state_transition",
    "error_code_trigger",
}


def check_files():
    """检查所有必须的 parquet 文件是否存在且非空。"""
    print("=" * 60)
    print("1. 文件完整性检查")
    print("=" * 60)
    missing = []
    empty = []
    for f in REQUIRED_FILES:
        p = OUTPUT_DIR / f
        if not p.exists():
            missing.append(f)
            print(f"  [FAIL] {f} 不存在")
        elif p.stat().st_size < 100:
            empty.append(f)
            print(f"  [WARN] {f} 文件过小 ({p.stat().st_size} bytes)")
        else:
            size_kb = p.stat().st_size / 1024
            print(f"  [OK]   {f} ({size_kb:.1f} KB)")
    if missing:
        print(f"\n  !! 缺失文件: {missing}")
        print("  !! 索引构建不完整，请重新运行 --build")
        return False
    return True


def check_entities():
    """检查实体质量: 数量、类型分布、引号残留。"""
    import pandas as pd

    print("\n" + "=" * 60)
    print("2. 实体质量检查")
    print("=" * 60)

    df = pd.read_parquet(OUTPUT_DIR / "entities.parquet")
    total = len(df)
    print(f"  实体总数: {total}")

    if total == 0:
        print("  [FAIL] 实体为空!")
        return False

    ok = True

    # 类型分布
    if "type" in df.columns:
        type_counts = df["type"].value_counts()
        print(f"\n  实体类型分布 ({len(type_counts)} 种):")
        for t, c in type_counts.items():
            marker = "  "
            if t.upper() in {x.upper() for x in EXPECTED_ENTITY_TYPES}:
                marker = "ok"
            else:
                marker = "??"
            print(f"    [{marker}] {t}: {c} ({c/total*100:.1f}%)")

        # 引号残留
        quoted = df["type"].str.startswith('"') | df["type"].str.endswith('"')
        n_quoted = quoted.sum()
        if n_quoted > 0:
            print(f"\n  [WARN] {n_quoted} 个实体类型含引号残留 (需后处理)")
            ok = False
        else:
            print(f"\n  [OK]   无引号残留")

        # 未知类型
        known_upper = {x.upper() for x in EXPECTED_ENTITY_TYPES}
        unknown = type_counts[~type_counts.index.str.upper().isin(known_upper)]
        if len(unknown) > 0:
            print(f"  [WARN] {len(unknown)} 种非预期实体类型: {list(unknown.index[:10])}")
    else:
        print("  [WARN] 无 type 列")

    # 名称去重
    if "name" in df.columns:
        n_unique = df["name"].nunique()
        dup_ratio = 1 - n_unique / total
        print(f"\n  唯一名称: {n_unique}/{total} (重复率 {dup_ratio*100:.1f}%)")
        if dup_ratio > 0.3:
            print("  [WARN] 重复率偏高，可能有过度抽取")

    # 描述质量
    if "description" in df.columns:
        empty_desc = df["description"].isna() | (df["description"].str.strip() == "")
        n_empty = empty_desc.sum()
        if n_empty > 0:
            print(f"  [WARN] {n_empty} 个实体缺少描述 ({n_empty/total*100:.1f}%)")
        else:
            print(f"  [OK]   所有实体都有描述")

        avg_desc_len = df.loc[~empty_desc, "description"].str.len().mean()
        print(f"  平均描述长度: {avg_desc_len:.0f} 字符")

    return ok


def check_relationships():
    """检查关系质量。"""
    import pandas as pd

    print("\n" + "=" * 60)
    print("3. 关系质量检查")
    print("=" * 60)

    df = pd.read_parquet(OUTPUT_DIR / "relationships.parquet")
    total = len(df)
    print(f"  关系总数: {total}")

    if total == 0:
        print("  [FAIL] 关系为空!")
        return False

    # 源/目标实体
    if "source" in df.columns and "target" in df.columns:
        n_sources = df["source"].nunique()
        n_targets = df["target"].nunique()
        print(f"  唯一源实体: {n_sources}, 唯一目标实体: {n_targets}")

        # 自环
        self_loops = (df["source"] == df["target"]).sum()
        if self_loops > 0:
            print(f"  [WARN] {self_loops} 条自环关系")
        else:
            print(f"  [OK]   无自环关系")

    # 权重分布
    if "weight" in df.columns:
        print(f"  权重: min={df['weight'].min():.2f}, "
              f"max={df['weight'].max():.2f}, "
              f"mean={df['weight'].mean():.2f}")

    # 描述
    if "description" in df.columns:
        empty_desc = df["description"].isna() | (df["description"].str.strip() == "")
        n_empty = empty_desc.sum()
        print(f"  无描述关系: {n_empty}/{total} ({n_empty/total*100:.1f}%)")

    return True


def check_communities():
    """检查社区结构。"""
    import pandas as pd

    print("\n" + "=" * 60)
    print("4. 社区结构检查")
    print("=" * 60)

    df = pd.read_parquet(OUTPUT_DIR / "communities.parquet")
    total = len(df)
    print(f"  社区总数: {total}")

    if total == 0:
        print("  [WARN] 无社区 (可能所有实体孤立)")
        return True

    if "level" in df.columns:
        level_counts = df["level"].value_counts().sort_index()
        print(f"  层级分布:")
        for level, cnt in level_counts.items():
            print(f"    Level {level}: {cnt} 社区")

    # 社区报告
    reports_path = OUTPUT_DIR / "community_reports.parquet"
    if reports_path.exists():
        rdf = pd.read_parquet(reports_path)
        print(f"\n  社区报告数: {len(rdf)}")
        if "full_content" in rdf.columns:
            avg_len = rdf["full_content"].str.len().mean()
            print(f"  平均报告长度: {avg_len:.0f} 字符")
        if len(rdf) < total:
            print(f"  [WARN] 报告数 ({len(rdf)}) < 社区数 ({total})")
    else:
        print("  [WARN] community_reports.parquet 不存在")

    return True


def check_text_units():
    """检查文本单元覆盖率。"""
    import pandas as pd

    print("\n" + "=" * 60)
    print("5. 文本单元覆盖率")
    print("=" * 60)

    df = pd.read_parquet(OUTPUT_DIR / "text_units.parquet")
    total = len(df)
    print(f"  文本单元总数: {total}")

    if "entity_ids" in df.columns:
        has_entities = df["entity_ids"].apply(
            lambda x: len(x) > 0 if isinstance(x, (list, tuple)) else False
        )
        n_with = has_entities.sum()
        print(f"  含实体的 text_unit: {n_with}/{total} ({n_with/total*100:.1f}%)")
        if n_with / total < 0.5:
            print("  [WARN] 超过一半的文本单元无实体覆盖")

    if "relationship_ids" in df.columns:
        has_rels = df["relationship_ids"].apply(
            lambda x: len(x) > 0 if isinstance(x, (list, tuple)) else False
        )
        n_with = has_rels.sum()
        print(f"  含关系的 text_unit: {n_with}/{total} ({n_with/total*100:.1f}%)")

    # 文本长度
    if "text" in df.columns:
        lengths = df["text"].str.len()
        print(f"\n  文本长度: min={lengths.min()}, max={lengths.max()}, "
              f"mean={lengths.mean():.0f}, median={lengths.median():.0f}")

    return True


def check_lancedb():
    """检查 LanceDB 向量存储。"""
    print("\n" + "=" * 60)
    print("6. LanceDB 向量存储检查")
    print("=" * 60)

    lancedb_dir = OUTPUT_DIR / "lancedb"
    if not lancedb_dir.exists():
        print("  [WARN] lancedb 目录不存在 (embed_text 可能未运行)")
        return True

    import os
    total_size = sum(
        f.stat().st_size for f in lancedb_dir.rglob("*") if f.is_file()
    )
    file_count = sum(1 for f in lancedb_dir.rglob("*") if f.is_file())
    print(f"  文件数: {file_count}")
    print(f"  总大小: {total_size / 1024 / 1024:.1f} MB")

    try:
        import lancedb as ldb
        db = ldb.connect(str(lancedb_dir))
        tables = db.table_names()
        print(f"  表: {tables}")
        for t in tables:
            tbl = db.open_table(t)
            row_count = tbl.count_rows()
            print(f"    {t}: {row_count} 行")
    except ImportError:
        print("  [INFO] lancedb 未安装，跳过表检查")
    except Exception as e:
        print(f"  [WARN] LanceDB 读取失败: {e}")

    return True


def check_graphml():
    """检查 GraphML 快照。"""
    print("\n" + "=" * 60)
    print("7. GraphML 快照检查")
    print("=" * 60)

    graphml_files = list(OUTPUT_DIR.glob("*.graphml"))
    if not graphml_files:
        print("  [INFO] 无 GraphML 快照 (snapshots.graphml=false?)")
        return True

    for f in graphml_files:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}: {size_mb:.1f} MB")

    return True


def print_summary(results):
    """打印总结。"""
    print("\n" + "=" * 60)
    print("总结")
    print("=" * 60)
    all_ok = all(results.values())
    for name, ok in results.items():
        status = "[OK]  " if ok else "[FAIL]"
        print(f"  {status} {name}")

    if all_ok:
        print("\n  >> 索引质量检查通过!")
    else:
        print("\n  >> 部分检查未通过，请查看上方详细信息")

    return all_ok


def main():
    if not OUTPUT_DIR.exists():
        print(f"[FAIL] 输出目录不存在: {OUTPUT_DIR}")
        print("请先运行: python INAGENT/scripts/init_graphrag.py --build")
        sys.exit(1)

    results = {}
    results["文件完整性"] = check_files()

    # 如果文件不全则跳过后续
    if not results["文件完整性"]:
        print("\n文件不完整，跳过后续检查。")
        sys.exit(1)

    try:
        import pandas  # noqa: F401
    except ImportError:
        print("\n[WARN] pandas 未安装，跳过数据质量检查")
        sys.exit(0)

    results["实体质量"] = check_entities()
    results["关系质量"] = check_relationships()
    results["社区结构"] = check_communities()
    results["文本覆盖"] = check_text_units()
    results["向量存储"] = check_lancedb()
    results["GraphML"] = check_graphml()

    ok = print_summary(results)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
