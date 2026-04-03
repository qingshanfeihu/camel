#!/usr/bin/env python3
"""
GraphRAG Delta 手动合并脚本

将 update_output/delta 中已完成的增量索引数据合并到 output/ 主索引中，
避免重新运行全量 LLM 调用。

操作步骤：
1. 备份当前 output/ 中的 parquet 文件
2. 将 delta parquet concat 到 output parquet
3. 重建 lancedb 向量索引（仅需 embedding，无 LLM chat 调用）
4. 验证合并结果
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# 项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

WORKSPACE = Path(__file__).parent.parent / "graphrag_index"
OUTPUT_DIR = WORKSPACE / "output"
DELTA_ROOT = WORKSPACE / "update_output"

# 需要合并的 parquet 表
TABLES = [
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
    "communities.parquet",
    "community_reports.parquet",
]


def find_latest_delta() -> Path:
    """找到最新的 delta 目录"""
    if not DELTA_ROOT.exists():
        raise FileNotFoundError(f"update_output 目录不存在: {DELTA_ROOT}")
    
    subdirs = sorted(
        [d for d in DELTA_ROOT.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not subdirs:
        raise FileNotFoundError("update_output 中没有增量目录")
    
    delta_dir = subdirs[0] / "delta"
    if not delta_dir.exists():
        raise FileNotFoundError(f"delta 目录不存在: {delta_dir}")
    
    return delta_dir


def backup_output(backup_suffix: str) -> Path:
    """备份当前 output 文件"""
    backup_dir = OUTPUT_DIR / f"backup_{backup_suffix}"
    backup_dir.mkdir(exist_ok=True)
    
    for table in TABLES:
        src = OUTPUT_DIR / table
        if src.exists():
            dst = backup_dir / table
            shutil.copy2(src, dst)
            print(f"  备份: {table} -> backup_{backup_suffix}/")
    
    return backup_dir


def merge_parquet(delta_dir: Path) -> dict:
    """合并 delta parquet 到 output"""
    stats = {}
    
    for table in TABLES:
        delta_path = delta_dir / table
        output_path = OUTPUT_DIR / table
        
        if not delta_path.exists():
            print(f"  跳过 {table}: delta 文件不存在")
            continue
        
        if not output_path.exists():
            print(f"  跳过 {table}: output 文件不存在")
            continue
        
        # 读取
        df_output = pd.read_parquet(output_path)
        df_delta = pd.read_parquet(delta_path)
        
        if len(df_delta) == 0:
            print(f"  跳过 {table}: delta 为空")
            continue
        
        # 检查 ID 重叠
        if "id" in df_output.columns and "id" in df_delta.columns:
            overlap = set(df_delta["id"]) & set(df_output["id"])
            if overlap:
                print(f"  警告 {table}: 有 {len(overlap)} 个重复 ID，将去重（保留 delta 版本）")
                df_output = df_output[~df_output["id"].isin(overlap)]
        
        # 修正 human_readable_id 连续性
        if "human_readable_id" in df_output.columns and "human_readable_id" in df_delta.columns:
            max_hrid = df_output["human_readable_id"].max()
            df_delta = df_delta.copy()
            df_delta["human_readable_id"] = range(
                max_hrid + 1, max_hrid + 1 + len(df_delta)
            )
        
        # Concat
        df_merged = pd.concat([df_output, df_delta], ignore_index=True)
        
        # 写入
        df_merged.to_parquet(output_path, index=False)
        
        before = len(df_output)
        after = len(df_merged)
        stats[table] = {"before": before, "added": len(df_delta), "after": after}
        print(f"  合并 {table}: {before} + {len(df_delta)} = {after}")
    
    return stats


def rebuild_lancedb():
    """重建 lancedb 向量索引
    
    当前 lancedb 只包含 delta 数据（42+9+1 条），需要为全部合并后的数据重建。
    仅需 embedding API 调用（bge-m3），无 LLM chat 调用。
    """
    print("\n重建 LanceDB 向量索引...")
    print("  说明: 当前 lancedb 只有 delta 数据，需为全部数据重建向量")
    
    try:
        import lancedb
        import time
        
        lance_dir = OUTPUT_DIR / "lancedb"
        
        # 加载合并后的数据
        entities = pd.read_parquet(OUTPUT_DIR / "entities.parquet")
        community_reports = pd.read_parquet(OUTPUT_DIR / "community_reports.parquet")
        text_units = pd.read_parquet(OUTPUT_DIR / "text_units.parquet")
        
        # 使用项目的 embedding 模型
        from INAGENT.utils import env_utils
        env_utils.load_inagent_env()
        from INAGENT.utils.llm_config import get_gateway_config
        config = get_gateway_config()
        
        api_key = config.get("api_key")
        base_url = config.get("base_url", "http://127.0.0.1:9000/v1")
        embed_model = config.get("embedding_model", "text-embedding-v4")
        
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)
        
        # bge-m3 max tokens = 8192; most texts are short descriptions.
        # Use batch_size=64 for max throughput via gateway.
        BATCH_SIZE = 64
        MAX_TEXT_LEN = 4096  # truncate very long texts
        
        def embed_texts(texts: list, label: str = "") -> list:
            """批量 embedding，带进度显示"""
            all_embeddings = []
            total = len(texts)
            n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
            t0 = time.time()
            
            for i in range(0, total, BATCH_SIZE):
                batch = texts[i:i + BATCH_SIZE]
                # 清理空文本 & 截断过长文本
                batch = [
                    (t[:MAX_TEXT_LEN] if t and isinstance(t, str) else " ")
                    for t in batch
                ]
                
                retries = 0
                while retries < 3:
                    try:
                        resp = client.embeddings.create(
                            model=embed_model, input=batch
                        )
                        for item in resp.data:
                            all_embeddings.append(item.embedding)
                        break
                    except Exception as e:
                        retries += 1
                        if retries >= 3:
                            raise RuntimeError(
                                f"Embedding 失败 ({label} batch {i//BATCH_SIZE}): {e}"
                            )
                        time.sleep(2 ** retries)
                
                done = min(i + BATCH_SIZE, total)
                if done % (BATCH_SIZE * 10) == 0 or done == total:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(
                        f"    {label}: {done}/{total} "
                        f"({done*100//total}%) "
                        f"[{elapsed:.0f}s, ~{eta:.0f}s remaining]"
                    )
            
            return all_embeddings
        
        db = lancedb.connect(str(lance_dir))
        
        # 计算总量
        total_items = len(entities) + len(community_reports) + len(text_units)
        print(f"  总计需嵌入: {total_items} 条 "
              f"(entities={len(entities)}, community_reports={len(community_reports)}, "
              f"text_units={len(text_units)})")
        print(f"  batch_size={BATCH_SIZE}, 预计 ~{(total_items + BATCH_SIZE - 1)//BATCH_SIZE} batches")
        
        t_start = time.time()
        
        # 1. Entity descriptions
        print("\n  [1/3] 嵌入 entity descriptions...")
        ent_texts = entities["description"].fillna("").tolist()
        ent_ids = entities["id"].tolist()
        ent_embeddings = embed_texts(ent_texts, label="entities")
        
        # 批量构建表数据并写入（必须包含 attributes 列，GraphRAG 的 LanceDBVectorStore 需要 4 列）
        print("    写入 lancedb...")
        ent_data = [
            {"id": eid, "text": txt, "vector": vec, "attributes": "{}"}
            for eid, txt, vec in zip(ent_ids, ent_texts, ent_embeddings)
        ]
        try:
            db.drop_table("default-entity-description")
        except Exception:
            pass
        db.create_table("default-entity-description", ent_data)
        del ent_data, ent_embeddings  # free memory
        print(f"    entity-description: {len(entities)} vectors 写入完成")
        
        # 2. Community full_content
        print("\n  [2/3] 嵌入 community full_content...")
        cr_texts = community_reports["full_content"].fillna("").tolist()
        cr_ids = community_reports["id"].tolist()
        cr_embeddings = embed_texts(cr_texts, label="communities")
        
        print("    写入 lancedb...")
        cr_data = [
            {"id": cid, "text": txt, "vector": vec, "attributes": "{}"}
            for cid, txt, vec in zip(cr_ids, cr_texts, cr_embeddings)
        ]
        try:
            db.drop_table("default-community-full_content")
        except Exception:
            pass
        db.create_table("default-community-full_content", cr_data)
        del cr_data, cr_embeddings
        print(f"    community-full_content: {len(community_reports)} vectors 写入完成")
        
        # 3. Text unit text
        print("\n  [3/3] 嵌入 text_unit text...")
        tu_texts = text_units["text"].fillna("").tolist()
        tu_ids = text_units["id"].tolist()
        tu_embeddings = embed_texts(tu_texts, label="text_units")
        
        print("    写入 lancedb...")
        tu_data = [
            {"id": tid, "text": txt, "vector": vec, "attributes": "{}"}
            for tid, txt, vec in zip(tu_ids, tu_texts, tu_embeddings)
        ]
        try:
            db.drop_table("default-text_unit-text")
        except Exception:
            pass
        db.create_table("default-text_unit-text", tu_data)
        del tu_data, tu_embeddings
        print(f"    text_unit-text: {len(text_units)} vectors 写入完成")
        
        elapsed = time.time() - t_start
        print(f"\n  LanceDB 重建完成! 总耗时: {elapsed/60:.1f} 分钟")
        return True
        
    except Exception as e:
        print(f"  LanceDB 重建失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def verify_merge():
    """验证合并结果"""
    print("\n验证合并结果...")
    
    # 1. 检查 text_units 包含合成文档
    tu = pd.read_parquet(OUTPUT_DIR / "text_units.parquet")
    has_synthetic = tu["text"].str.contains(
        "health request.*health response|四步|chinamobile",
        case=False, na=False, regex=True,
    ).any()
    print(f"  text_units 包含合成健康检查文档: {'YES' if has_synthetic else 'NO'}")
    
    # 2. 检查 entities 包含新实体
    ent = pd.read_parquet(OUTPUT_DIR / "entities.parquet")
    new_ent = ent[ent["title"].str.contains(
        "HEALTH_REQUEST_CONFIGURATION|HEALTH_RESPONSE_CONFIGURATION",
        case=False, na=False, regex=True,
    )]
    print(f"  新增 HEALTH_REQUEST/RESPONSE 实体: {len(new_ent)}")
    
    # 3. 总量统计
    for f in TABLES:
        df = pd.read_parquet(OUTPUT_DIR / f)
        print(f"  {f}: {len(df)} rows")
    
    # 4. 检查 lancedb
    try:
        import lancedb
        db = lancedb.connect(str(OUTPUT_DIR / "lancedb"))
        for tname in db.table_names():
            tbl = db.open_table(tname)
            print(f"  lancedb/{tname}: {tbl.count_rows()} vectors")
    except Exception as e:
        print(f"  LanceDB 检查失败: {e}")
    
    return has_synthetic


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GraphRAG Delta 手动合并")
    parser.add_argument(
        "--skip-lancedb", action="store_true",
        help="跳过 LanceDB 重建（仅合并 parquet 文件）",
    )
    parser.add_argument(
        "--skip-backup", action="store_true",
        help="跳过备份步骤",
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("GraphRAG Delta 手动合并")
    print("=" * 60)
    
    # 1. 找到 delta
    delta_dir = find_latest_delta()
    print(f"\nDelta 目录: {delta_dir}")
    
    # 显示 delta 内容
    for f in TABLES:
        fp = delta_dir / f
        if fp.exists():
            df = pd.read_parquet(fp)
            print(f"  {f}: {len(df)} rows")
    
    # 2. 备份
    if not args.skip_backup:
        print("\n备份当前 output...")
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = backup_output(suffix)
        print(f"  备份目录: {backup_dir}")
    else:
        print("\n跳过备份")
    
    # 3. 合并 parquet
    print("\n合并 parquet 文件...")
    stats = merge_parquet(delta_dir)
    
    # 4. 更新 documents.parquet（delta 可能已包含新文档记录）
    doc_delta = delta_dir / "documents.parquet"
    if doc_delta.exists():
        df_doc_delta = pd.read_parquet(doc_delta)
        df_doc_main = pd.read_parquet(OUTPUT_DIR / "documents.parquet")
        # 去重
        existing_ids = set(df_doc_main["id"])
        new_docs = df_doc_delta[~df_doc_delta["id"].isin(existing_ids)]
        if len(new_docs) > 0:
            df_doc_merged = pd.concat([df_doc_main, new_docs], ignore_index=True)
            df_doc_merged.to_parquet(OUTPUT_DIR / "documents.parquet", index=False)
            print(f"  合并 documents.parquet: {len(df_doc_main)} + {len(new_docs)} = {len(df_doc_merged)}")
        else:
            print(f"  documents.parquet: 无新文档需要添加（已有 {len(df_doc_main)} 行）")
    
    # 5. 重建 lancedb
    if args.skip_lancedb:
        print("\n跳过 LanceDB 重建（使用 --skip-lancedb）")
        lance_ok = None
    else:
        lance_ok = rebuild_lancedb()
    
    # 6. 验证
    ok = verify_merge()
    
    # 7. 更新 stats.json
    stats_path = OUTPUT_DIR / "stats.json"
    if stats_path.exists():
        with open(stats_path, "r") as f:
            s = json.load(f)
        s["delta_merge"] = {
            "timestamp": datetime.now().isoformat(),
            "delta_dir": str(delta_dir),
            "tables": stats,
            "lancedb_rebuilt": bool(lance_ok) if lance_ok is not None else None,
            "verified": bool(ok),
        }
        with open(stats_path, "w") as f:
            json.dump(s, f, indent=2, ensure_ascii=False, default=str)
    
    print("\n" + "=" * 60)
    if ok and lance_ok is not False:
        print("合并完成！GraphRAG 索引已更新。")
        if lance_ok is None:
            print("提示: LanceDB 未重建，搜索功能需要先运行重建。")
            print("  运行: python INAGENT/scripts/merge_graphrag_delta.py")
    else:
        print("合并完成，但有警告，请检查上面的输出。")
        if lance_ok is False:
            print("LanceDB 重建失败 — 可能需要手动处理向量索引。")
    print("=" * 60)


if __name__ == "__main__":
    main()
