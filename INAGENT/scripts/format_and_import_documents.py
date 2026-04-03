#!/usr/bin/env python3
"""
格式化并导入 GraphRAG 输入文档

操作：
1. 清理 graphrag_index 测试产物 (output/, cache/, logs/)
2. 从 knowledge_base.json 读取全量数据（当前为 cli_keyword_graph.json 生成的 CLI 叶子节点）
3. 按来源文件合并，提取 document_category + product_module 元数据
4. 写入规范化 documents.json，供 graphrag index 消费
"""
import json
import logging
import re
import shutil
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from INAGENT.data_tools.document_classifier import classify_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

INAGENT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = INAGENT_ROOT / "graphrag_index"
KB_PATH = INAGENT_ROOT / "knowledge_base" / "reference" / "knowledge_base.json"

# 来源文件 → 专用检索器（旧架构排除，新架构全部纳入 GraphRAG）
# 新统一架构下已不再排除任何文件；所有文档均通过 GraphRAG 索引。
EXCLUDED_SOURCES: set = set()

# 来源文件 document_category → knowledge_layer 映射
KNOWLEDGE_LAYER_MAP = {
    "cli/reference": "design",
    "app/reference": "design",
    "architecture/design": "design",
}


def clean_workspace():
    """清理 graphrag_index 测试产物。"""
    for dirname in ["output", "cache", "logs"]:
        target = WORKSPACE / dirname
        if target.exists():
            shutil.rmtree(target)
            logger.info("已删除: %s", target)
        target.mkdir(parents=True, exist_ok=True)
        logger.info("已重建空目录: %s", target)


def load_knowledge_base():
    """加载 knowledge_base.json。"""
    if not KB_PATH.exists():
        logger.error("knowledge_base.json 不存在: %s", KB_PATH)
        sys.exit(1)
    data = json.loads(KB_PATH.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("chunks", [])
    logger.info("加载 knowledge_base.json: %d 条目", len(items))
    return items


def classify_source(source_file: str, blocks: list) -> str:
    """使用 document_classifier 分类来源文件。"""
    # 优先使用 block 级别已有的 document_category
    for block in blocks:
        cat = block.get("metadata", {}).get("document_category", "")
        if cat:
            return cat

    # 回退到文件名分类
    fp = Path(source_file)
    preview = ""
    for block in blocks[:5]:
        text = block.get("text", "") or block.get("page_content", "")
        preview += text[:400] + "\n"

    category, confidence = classify_document(fp, content_preview=preview)
    logger.info("  分类: %s → %s (置信度 %.2f)", source_file, category, confidence)
    return category


def format_documents(items: list) -> list:
    """按来源文件合并并格式化为 GraphRAG 输入文档。"""
    # 按来源文件分组
    source_groups: OrderedDict[str, list] = OrderedDict()
    for item in items:
        meta = item.get("metadata", {})
        src = meta.get("source_file", "unknown")
        source_groups.setdefault(src, []).append(item)

    logger.info("来源文件分组: %d 个文件", len(source_groups))

    documents = []
    stats = {"included": 0, "excluded": 0, "by_category": {}, "by_layer": {}}

    for source_file, blocks in source_groups.items():
        # 过滤专用检索器数据
        if source_file in EXCLUDED_SOURCES:
            logger.info("  跳过 (专用检索器): %s (%d blocks)", source_file, len(blocks))
            stats["excluded"] += len(blocks)
            continue

        # 分类
        category = classify_source(source_file, blocks)
        layer = KNOWLEDGE_LAYER_MAP.get(category, "design")

        # 合并文本
        parts = []
        for block in blocks:
            text = block.get("text", "") or block.get("page_content", "")
            if not text.strip():
                continue
            meta = block.get("metadata", {})

            # 构建元数据前缀（仅保留通用字段）
            meta_prefix = []
            if category:
                meta_prefix.append(f"[分类: {category}]")

            product_module = meta.get("product_module", "")
            if product_module and product_module != "unknown":
                meta_prefix.append(f"[模块: {product_module}]")

            enriched = " ".join(meta_prefix) + "\n" + text if meta_prefix else text
            parts.append(enriched)

        merged_text = "\n\n".join(parts)
        if not merged_text.strip():
            continue

        doc_idx = len(documents)
        documents.append({
            "id": f"src_{doc_idx:04d}",
            "text": f"[来源: {source_file}]\n\n{merged_text}",
            "title": source_file,
            "metadata": {
                "source_file": source_file,
                "document_category": category,
                "knowledge_layer": layer,
                "block_count": len(blocks),
            }
        })
        stats["included"] += len(blocks)
        stats["by_category"][category] = stats["by_category"].get(category, 0) + 1
        stats["by_layer"][layer] = stats["by_layer"].get(layer, 0) + 1

    return documents, stats


def write_documents(documents: list):
    """写入格式化后的 documents.json。"""
    input_dir = WORKSPACE / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    # 备份旧文件（写到 input_backups/ 而非 input/，避免 GraphRAG 扫描到备份）
    output_path = input_dir / "documents.json"
    if output_path.exists():
        backup_dir = WORKSPACE / "input_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"documents_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        shutil.copy2(output_path, backup)
        logger.info("已备份: %s", backup)

    output_path.write_text(
        json.dumps(documents, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("已写入: %s (%d 文档)", output_path, len(documents))
    return output_path


def verify_modules(documents: list):
    """验证 GraphRAG 输入文档内容。"""
    print("\n" + "=" * 60)
    print("GraphRAG 输入验证")
    print("=" * 60)

    total_chars = 0
    for doc in documents:
        meta = doc["metadata"]
        chars = len(doc["text"])
        total_chars += chars
        print(f"  {doc['id']} | {doc['title']:50s} | {meta['document_category']:20s} | layer={meta['knowledge_layer']:8s} | {chars:>6d} chars")
    print(f"  总计: {len(documents)} 文档, {total_chars:,} 字符")

    # 抽样验证：显示第一个文档的前 3 个片段
    if documents:
        sample_text = documents[0]["text"]
        # 找前3个命令片段
        segments = sample_text.split("\n\n")[:4]
        print(f"\n[抽样] {documents[0]['id']} 前 4 段:")
        for seg in segments:
            first_line = seg.strip().split("\n")[0][:100]
            print(f"  · {first_line}")


def main():
    print("=" * 60)
    print("GraphRAG 文档格式化与导入")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Step 1: 清理
    print("\n--- Step 1: 清理测试产物 ---")
    clean_workspace()

    # Step 2: 加载
    print("\n--- Step 2: 加载 knowledge_base.json ---")
    items = load_knowledge_base()

    # Step 3: 格式化
    print("\n--- Step 3: 格式化文档 ---")
    documents, stats = format_documents(items)

    print(f"\n格式化统计:")
    print(f"  纳入 GraphRAG: {stats['included']} blocks → {len(documents)} 文档")
    print(f"  排除 (专用检索器): {stats['excluded']} blocks")
    print(f"  按分类: {json.dumps(stats['by_category'], ensure_ascii=False)}")
    print(f"  按知识层: {json.dumps(stats['by_layer'], ensure_ascii=False)}")

    # Step 4: 写入
    print("\n--- Step 4: 写入 documents.json ---")
    write_documents(documents)

    # Step 5: 验证
    print("\n--- Step 5: 验证各模块 ---")
    verify_modules(documents)

    print("\n" + "=" * 60)
    print("完成！下一步可运行 GraphRAG 索引构建:")
    print(f"  cd {WORKSPACE}")
    print("  graphrag index --root .")
    print("=" * 60)


if __name__ == "__main__":
    main()
