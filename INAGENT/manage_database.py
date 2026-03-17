"""
数据库管理脚本：更新/创建/删除 INAGENT 的"数据库"

数据库定义：
  - Schema层：knowledge_base/function_structure_index.json
  - Data层：knowledge_base/reference/knowledge_base.json
  - 缓存层：knowledge_base/*.json.cache（文档处理缓存）

支持的源文件类型：
  - PDF（.pdf）—— 通过 MinerU 提取
  - Office 文档（.docx/.doc/.xlsx/.xls）—— 通过 MarkItDown + 自动分类器

自动分类：
  - 产品规格（spec/prd, spec/func_spec, spec/design）
  - 测试文档（test/test_list, test/test_strategy, test/test_template）
  - 分类结果作为 metadata 嵌入知识块，供 GraphRAG 和 CAMEL 使用

支持动作：
  - status : 显示文件状态、分类摘要和变更检测
  - create : 仅在缺失时创建（尽量不触发昂贵流程）
  - update : 增量更新（检测变化，只处理新增/修改文件，自动分类）
  - rebuild: 清理后全量重建
  - delete : 删除数据库文件与本地RAG索引
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
INAGENT_DIR = REPO_ROOT / "INAGENT"
DOC_LOCAL_DIR = INAGENT_DIR / "knowledge_base"
REFERENCE_DIR = DOC_LOCAL_DIR / "reference"
MINERU_OUTPUT_DIR = DOC_LOCAL_DIR / "mineru_output"
MINERU_BACKUP_DIR = DOC_LOCAL_DIR / "mineru_backup"

KB_PATH = REFERENCE_DIR / "knowledge_base.json"
INDEX_PATH = DOC_LOCAL_DIR / "function_structure_index.json"
CACHE_TRACKER_PATH = DOC_LOCAL_DIR / ".file_tracker.json"  # 跟踪文件变化

# 常见的本地检索/索引目录（不同实现可能存在其一或多个）
LOCAL_RAG_DIRS = [
    INAGENT_DIR / "local_qdrant_db",
    INAGENT_DIR / "test_qdrant_db",
    INAGENT_DIR / "graphrag_index",
    INAGENT_DIR / "graphrag_output",
]


def _maybe_postprocess_unknown_module() -> int:
    if os.getenv("POSTPROCESS_UNKNOWN_MODULE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "y",
    }:
        return 0

    if not KB_PATH.exists():
        print("[info] knowledge_base.json 不存在，跳过 unknown 后处理")
        return 0

    try:
        from INAGENT.scripts.postprocess_unknown_product_module import (
            process_knowledge_base,
        )
    except Exception as e:
        print(f"[警告] 导入 unknown 后处理脚本失败: {e}")
        return 0

    registry_path = DOC_LOCAL_DIR / "product_modules_registry.json"
    updated, module_counts = process_knowledge_base(KB_PATH, registry_path)
    print(f"[ok] unknown 后处理完成，更新 {updated} 条")
    if module_counts:
        print(f"[info] 模块分布: {module_counts}")
    return 0


def _fmt_mtime(p: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    except FileNotFoundError:
        return "N/A"


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        return


def _safe_rmtree(p: Path) -> None:
    if not p.exists():
        return
    shutil.rmtree(p, ignore_errors=True)


def _compute_file_hash(file_path: Path) -> str:
    """计算文件的MD5哈希值"""
    try:
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception:
        return ""


# 支持的源文件扩展名
SOURCE_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls"}

# 需要跳过的目录名（与 auto_convert 保持一致）
SKIP_DIRS = {"reference", "mineru_output", "mineru_backup", "logs", ".cache"}


def _get_source_files(doc_dir: Path) -> List[Path]:
    """递归获取 knowledge_base 目录下的所有源文件（PDF + Office）"""
    source_files = []
    if not doc_dir.exists():
        return source_files

    for ext in SOURCE_EXTENSIONS:
        for fpath in doc_dir.rglob(f"*{ext}"):
            # 跳过 reference / mineru_output 等生成目录
            if any(skip in fpath.parts for skip in SKIP_DIRS):
                continue
            source_files.append(fpath)

    return sorted(source_files)


def _get_pdf_files(doc_dir: Path) -> List[Path]:
    """向后兼容：仅获取 PDF 文件（递归）"""
    return [f for f in _get_source_files(doc_dir) if f.suffix.lower() == ".pdf"]


def _classify_source_files(files: List[Path]) -> Dict[str, List[Path]]:
    """按文档类别对源文件进行分类统计

    Returns:
        {category_label: [file_paths]}
    """
    categories: Dict[str, List[Path]] = {
        "PDF文档": [],
        "Office规格文档 (docx/doc)": [],
        "Office测试列表 (xlsx/xls)": [],
    }
    for f in files:
        ext = f.suffix.lower()
        if ext == ".pdf":
            categories["PDF文档"].append(f)
        elif ext in {".docx", ".doc"}:
            categories["Office规格文档 (docx/doc)"].append(f)
        elif ext in {".xlsx", ".xls"}:
            categories["Office测试列表 (xlsx/xls)"].append(f)
    return categories


def _load_file_tracker() -> Dict[str, Dict[str, any]]:
    """加载文件跟踪器"""
    if not CACHE_TRACKER_PATH.exists():
        return {}
    
    try:
        with open(CACHE_TRACKER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_file_tracker(tracker: Dict[str, Dict[str, any]]) -> None:
    """保存文件跟踪器"""
    CACHE_TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_TRACKER_PATH, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)


def _detect_file_changes() -> Tuple[List[Path], List[Path], List[Path]]:
    """
    检测knowledge_base目录中的文件变化（PDF + Office）
    
    Returns:
        (new_files, modified_files, deleted_files)
    """
    tracker = _load_file_tracker()
    source_files = _get_source_files(DOC_LOCAL_DIR)
    
    current_files: Set[str] = {str(f) for f in source_files}
    tracked_files: Set[str] = set(tracker.keys())
    
    new_files = []
    modified_files = []
    deleted_files = []
    
    # 检测新增和修改的文件
    for src_file in source_files:
        file_path_str = str(src_file)
        file_hash = _compute_file_hash(src_file)
        file_mtime = src_file.stat().st_mtime
        
        if file_path_str not in tracker:
            # 新文件
            new_files.append(src_file)
            tracker[file_path_str] = {
                "hash": file_hash,
                "mtime": file_mtime,
                "last_processed": None
            }
        else:
            # 检查是否修改
            tracked_info = tracker[file_path_str]
            if (tracked_info.get("hash") != file_hash or 
                tracked_info.get("mtime") != file_mtime):
                modified_files.append(src_file)
                tracker[file_path_str] = {
                    "hash": file_hash,
                    "mtime": file_mtime,
                    "last_processed": tracked_info.get("last_processed")
                }
    
    # 检测删除的文件
    for tracked_file in tracked_files:
        if tracked_file not in current_files:
            deleted_files.append(Path(tracked_file))
            del tracker[tracked_file]
    
    # 保存更新后的跟踪器
    _save_file_tracker(tracker)
    
    return new_files, modified_files, deleted_files


def action_status() -> int:
    """显示数据库状态和文件变化"""
    print("=== INAGENT 数据库状态 ===")
    print(f"- knowledge_base.json: {KB_PATH}")
    print(f"  exists={KB_PATH.exists()}  mtime={_fmt_mtime(KB_PATH)}")
    if KB_PATH.exists():
        try:
            with open(KB_PATH, "r", encoding="utf-8") as f:
                kb_data = json.load(f)
                if isinstance(kb_data, list):
                    print(f"  records={len(kb_data)}")
                elif isinstance(kb_data, dict):
                    print(f"  keys={list(kb_data.keys())}")
        except Exception as e:
            print(f"  [警告] 无法读取文件: {e}")
    
    print(f"\n- function_structure_index.json: {INDEX_PATH}")
    print(f"  exists={INDEX_PATH.exists()}  mtime={_fmt_mtime(INDEX_PATH)}")
    if INDEX_PATH.exists():
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                idx_data = json.load(f)
                if isinstance(idx_data, dict):
                    scenarios = idx_data.get("scenarios", {})
                    modules = idx_data.get("modules", {})
                    print(f"  scenarios={len(scenarios)}  modules={len(modules)}")
        except Exception as e:
            print(f"  [警告] 无法读取文件: {e}")
    
    print("\n- 本地RAG索引目录:")
    for d in LOCAL_RAG_DIRS:
        exists = d.exists()
        print(f"  {d.name}: exists={exists}")
        if exists and d.is_dir():
            try:
                file_count = sum(1 for _ in d.rglob("*") if _.is_file())
                print(f"    files={file_count}")
            except Exception:
                pass
    
    # 检测文件变化
    print("\n=== knowledge_base 目录文件变化检测 ===")
    source_files = _get_source_files(DOC_LOCAL_DIR)
    categories = _classify_source_files(source_files)
    total = len(source_files)
    print(f"当前源文件数量: {total}")
    for cat_label, cat_files in categories.items():
        if cat_files:
            print(f"  {cat_label}: {len(cat_files)} 个")
            for cf in cat_files:
                # 显示相对路径以便于识别 input/ 子目录
                try:
                    rel = cf.relative_to(DOC_LOCAL_DIR)
                except ValueError:
                    rel = cf.name
                print(f"    - {rel} ({_fmt_mtime(cf)})")

    # 尝试用 document_classifier 做精细分类摘要
    try:
        from INAGENT.data_tools.document_classifier import classify_document
        office_files = [f for f in source_files if f.suffix.lower() in {".docx", ".doc", ".xlsx", ".xls"}]
        if office_files:
            print("\n=== 文档自动分类摘要 ===")
            class_buckets: Dict[str, List[str]] = {}
            for of in office_files:
                cat, conf = classify_document(of, "")
                class_buckets.setdefault(cat, []).append(f"{of.name} ({conf:.0%})")
            for cat_name, items in sorted(class_buckets.items()):
                print(f"  [{cat_name}] ({len(items)} 个)")
                for item in items:
                    print(f"    - {item}")
    except Exception as e:
        print(f"\n[info] 文档分类器不可用: {e}")

    new_files, modified_files, deleted_files = _detect_file_changes()
    
    if new_files:
        print(f"\n[新增] 发现 {len(new_files)} 个新文件:")
        for f in new_files:
            print(f"  + {f.name}")
    
    if modified_files:
        print(f"\n[修改] 发现 {len(modified_files)} 个已修改文件:")
        for f in modified_files:
            print(f"  ~ {f.name}")
    
    if deleted_files:
        print(f"\n[删除] 发现 {len(deleted_files)} 个已删除文件:")
        for f in deleted_files:
            print(f"  - {f.name}")
    
    if not new_files and not modified_files and not deleted_files:
        print("\n[无变化] knowledge_base目录中的源文件无变化")
    
    return 0


def _build_index_from_kb() -> int:
    """从knowledge_base.json构建function_structure_index.json"""
    if not KB_PATH.exists():
        print(f"[错误] 缺少 knowledge_base.json，无法构建索引: {KB_PATH}")
        return 1
    try:
        # 延迟import，避免不必要的依赖加载
        from INAGENT.data_tools.build_function_structure_index import build_function_index
    except Exception as e:
        print(f"[错误] 导入 build_function_structure_index 失败: {e}")
        return 1

    # 尽量复用 build_function_structure_index.py 的默认输入路径（若存在则传入）：
    cli_xml = DOC_LOCAL_DIR / "command_tree-Beta_APV_10_5_0_73.xml"
    app_md = MINERU_OUTPUT_DIR / "app" / "hybrid_auto" / "app.md"
    cli_md = MINERU_OUTPUT_DIR / "cli" / "hybrid_auto" / "cli.md"

    try:
        result = build_function_index(
            cli_xml_path=cli_xml if cli_xml.exists() else None,
            app_md_path=app_md if app_md.exists() else None,
            cli_md_path=cli_md if cli_md.exists() else None,
            kb_path=KB_PATH,
        )
        if isinstance(result, dict) and result:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            INDEX_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[ok] 已写入索引: {INDEX_PATH}")
        else:
            # 如果函数内部写盘，则检查是否存在
            if INDEX_PATH.exists():
                print(f"[ok] 索引已生成: {INDEX_PATH}")
            else:
                print("[警告] build_function_index 未返回dict且未生成默认索引文件，请检查实现")
                return 2
        return 0
    except Exception as e:
        print(f"[错误] 构建索引失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _run_initialize_pipeline() -> int:
    """运行完整的初始化流程"""
    # 直接调用 initialize_pipeline.main，避免 subprocess 在 Windows 下编码/路径问题
    try:
        import asyncio
        from INAGENT.initialize_pipeline import main as init_main

        asyncio.run(init_main())
        return 0
    except SystemExit as e:
        return int(getattr(e, "code", 1) or 0)
    except Exception as e:
        print(f"[错误] initialize_pipeline 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _run_auto_convert_incremental(new_files: List[Path], modified_files: List[Path]) -> int:
    """运行增量auto_convert处理"""
    try:
        import asyncio
        from INAGENT.data_tools.auto_convert import main as auto_convert_main
        
        # auto_convert会自动检测文件变化并处理
        # 这里我们只是触发它运行
        asyncio.run(auto_convert_main())
        return 0
    except SystemExit as e:
        return int(getattr(e, "code", 1) or 0)
    except Exception as e:
        print(f"[错误] auto_convert 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


def action_delete(*, clean_mineru: bool = False) -> int:
    """删除数据库（knowledge_base / function_structure_index / 本地索引）

    Args:
        clean_mineru: 如果为 True，将 mineru_output 移入 mineru_backup（rebuild 场景需要全量重跑）
    """
    print("=== 删除数据库（knowledge_base / function_structure_index / 本地索引） ===")
    
    # 删除主要数据库文件
    _safe_unlink(KB_PATH)
    _safe_unlink(INDEX_PATH)
    _safe_unlink(CACHE_TRACKER_PATH)
    
    # 删除本地RAG索引目录
    for d in LOCAL_RAG_DIRS:
        if d.exists():
            print(f"  删除: {d}")
            _safe_rmtree(d)
    
    # 清理缓存文件（但保留原始PDF和MinerU输出）
    cache_files = list(DOC_LOCAL_DIR.glob("*.json.cache"))
    for cache_file in cache_files:
        print(f"  删除缓存: {cache_file.name}")
        _safe_unlink(cache_file)

    # 清理 reference 目录中的转换输出
    if REFERENCE_DIR.exists():
        for json_file in REFERENCE_DIR.glob("*.json"):
            print(f"  删除 reference: {json_file.name}")
            _safe_unlink(json_file)

    # 清理 logs 下的 cache.json
    logs_dir = DOC_LOCAL_DIR / "logs"
    if logs_dir.exists():
        for cache_file in logs_dir.glob("*.cache.json"):
            print(f"  删除 cache: {cache_file.name}")
            _safe_unlink(cache_file)

    # rebuild 场景：将 mineru_output 移入 backup 而不是删除
    # MinerU 重新生成代价很高，备份保留以便快速恢复
    if clean_mineru and MINERU_OUTPUT_DIR.exists():
        MINERU_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        for task_dir in MINERU_OUTPUT_DIR.iterdir():
            if not task_dir.is_dir():
                continue
            backup_dest = MINERU_BACKUP_DIR / task_dir.name
            if backup_dest.exists():
                _safe_rmtree(backup_dest)
            try:
                print(f"  备份 mineru_output/{task_dir.name} -> mineru_backup/")
                shutil.move(str(task_dir), str(backup_dest))
            except Exception as e:
                print(f"  [警告] 备份失败 {task_dir.name}: {e}")
    
    print("[ok] 删除完成")
    return 0


def action_create() -> int:
    """
    create 的语义：仅在缺失时补齐。
    - 若 kb 不存在：走 initialize_pipeline（会生成kb并索引RAG）
    - 若 kb 存在但 index 不存在：仅从 kb 构建索引
    """
    kb_exists = KB_PATH.exists()
    idx_exists = INDEX_PATH.exists()

    if not kb_exists:
        print("[info] knowledge_base.json 不存在，执行初始化流程以创建数据库...")
        rc = _run_initialize_pipeline()
        if rc != 0:
            return rc
        _maybe_postprocess_unknown_module()
        return _build_index_from_kb()

    if kb_exists and not idx_exists:
        print("[info] function_structure_index.json 不存在，从 knowledge_base.json 构建索引...")
        _maybe_postprocess_unknown_module()
        return _build_index_from_kb()

    print("[ok] 数据库已存在，无需 create")
    return 0


def action_update() -> int:
    """
    update 的语义：增量更新。
    检测knowledge_base目录中的文件变化，处理新增/修改的PDF和Office文件。
    自动分类文档内容（测试/产品/示例数据/示例模板等），按分类生成或更新数据库。
    """
    print("[info] 检测knowledge_base目录中的文件变化（PDF + Office）...")
    new_files, modified_files, deleted_files = _detect_file_changes()
    
    # 显示分类摘要
    changed_files = new_files + modified_files
    if changed_files:
        try:
            from INAGENT.data_tools.document_classifier import classify_document
            print("\n[分类] 变更文件自动分类:")
            for f in changed_files:
                cat, conf = classify_document(f, "")
                tag = "新增" if f in new_files else "修改"
                print(f"  [{tag}] {f.name} -> {cat} ({conf:.0%})")
        except Exception:
            pass

    if deleted_files:
        print(f"[info] 发现 {len(deleted_files)} 个已删除文件，需要重新构建knowledge_base")
        print("[info] 执行完整初始化流程以确保数据一致性...")
        return _run_initialize_pipeline()
    
    if new_files or modified_files:
        total_changed = len(new_files) + len(modified_files)
        n_pdf = sum(1 for f in changed_files if f.suffix.lower() == ".pdf")
        n_office = total_changed - n_pdf
        print(f"[info] 发现 {len(new_files)} 个新文件和 {len(modified_files)} 个修改的文件")
        print(f"       PDF: {n_pdf} / Office: {n_office} / 合计: {total_changed}")
        
        # 运行auto_convert进行增量处理
        # auto_convert内部已经处理PDF和Office文件（含自动分类）
        print("[info] 执行增量更新（auto_convert 自动处理并分类所有文件类型）...")
        rc = _run_auto_convert_incremental(new_files, modified_files)
        
        if rc == 0:
            # 更新索引
            _maybe_postprocess_unknown_module()
            print("[info] 更新功能结构索引...")
            rc = _build_index_from_kb()
        
        return rc
    else:
        print("[info] 没有检测到文件变化，无需更新")
        return 0


def action_rebuild() -> int:
    """
    rebuild 的语义：清理后全量重建数据库。
    清理包括 mineru_output，确保从源 PDF 和 Office 文档完全重新转换和分类。
    """
    print("[info] rebuild: 先 delete (含 mineru_output)，再 initialize_pipeline，最后确保 index 存在")
    rc = action_delete(clean_mineru=True)
    if rc != 0:
        return rc

    rc = _run_initialize_pipeline()
    if rc != 0:
        return rc

    _maybe_postprocess_unknown_module()

    if not INDEX_PATH.exists():
        # 兜底：从kb构建一次
        return _build_index_from_kb()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 控制台编码兼容：尽量使用 UTF-8 输出，避免中文乱码
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="INAGENT 数据库管理")
    parser.add_argument(
        "--action",
        required=True,
        choices=["status", "create", "update", "rebuild", "delete"],
        help="数据库动作",
    )
    args = parser.parse_args(argv)

    # 确保在 repo root 运行时 import 路径正确
    os.chdir(str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(INAGENT_DIR))

    if args.action == "status":
        return action_status()
    if args.action == "create":
        return action_create()
    if args.action == "update":
        return action_update()
    if args.action == "rebuild":
        return action_rebuild()
    if args.action == "delete":
        return action_delete()

    print(f"[错误] 未知 action: {args.action}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
