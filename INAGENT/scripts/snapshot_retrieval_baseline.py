#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检索基线快照 — 树 / reference / GraphRAG 输出 / 本地 Qdrant 一体备份与恢复

目标：农民或农场主污染 CLI 骨架、reference 合并结果、GraphRAG 表或向量库后，
      能回退到此前「高质量混合检索」所依赖的磁盘状态（在相同嵌入模型与配置下）。

不包含：
  - 远端 Qdrant（设 QDRANT_URL 时仅记录 manifest，不拷贝 collection；需服务端自有快照或回退后重跑导入）
  - GraphRAG 重建流水线本身（恢复的是已 build 好的 output/）

用法:
  python INAGENT/scripts/snapshot_retrieval_baseline.py create [--label hq_cli]
  python INAGENT/scripts/snapshot_retrieval_baseline.py restore INAGENT/knowledge_base/retrieval_baselines/20260403_120000_hq_cli

恢复前请停止占用 Qdrant 与 GraphRAG 文件的进程（本机 Web 服务、索引任务等）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

INAGENT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = INAGENT_ROOT / "knowledge_base" / "reference"
KB_DIR = INAGENT_ROOT / "knowledge_base"
CLI_GRAPH = KB_DIR / "cli_keyword_graph.json"
SKELETON_DB = INAGENT_ROOT / "vector_store" / "skeleton_index.db"
GRAPHRAG_OUTPUT = INAGENT_ROOT / "graphrag_index" / "output"
BASELINES_ROOT = KB_DIR / "retrieval_baselines"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _local_qdrant_dir() -> Path:
    return Path(
        os.environ.get(
            "QDRANT_LOCAL_DIR",
            str(Path.home() / "AppData" / "Local" / "INAGENT" / "vector_store" / "qdrant"),
        )
    )


def _kb_chunk_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
        return len(data.get("chunks", []))
    except Exception:
        return -1


def create_baseline(
    label: str = "",
    *,
    skip_cli_graph: bool = False,
    skip_qdrant: bool = False,
) -> Path:
    try:
        from INAGENT.utils.env_utils import load_inagent_env

        load_inagent_env()
    except Exception:
        pass

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{label}" if label else ts
    snap = BASELINES_ROOT / name
    snap.mkdir(parents=True, exist_ok=False)

    ref_dst = snap / "reference"
    ref_dst.mkdir()
    gr_dst = snap / "graphrag_output"
    gr_dst.mkdir()
    tree_dst = snap / "tree"
    tree_dst.mkdir()

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "inagent_root": str(INAGENT_ROOT),
        "components": {},
    }

    # ── reference/*.json ───────────────────────────────────────────
    ref_files: List[str] = []
    if REFERENCE_DIR.exists():
        for f in sorted(REFERENCE_DIR.glob("*.json")):
            shutil.copy2(f, ref_dst / f.name)
            ref_files.append(f.name)
        manifest["components"]["reference"] = {
            "files": ref_files,
            "knowledge_base_json_sha256": _sha256_file(ref_dst / "knowledge_base.json")
            if (ref_dst / "knowledge_base.json").exists()
            else "",
            "knowledge_base_chunks": _kb_chunk_count(ref_dst / "knowledge_base.json"),
        }
    else:
        manifest["components"]["reference"] = {"error": "reference dir missing"}

    # ── cli_keyword_graph.json ───────────────────────────────────
    if not skip_cli_graph and CLI_GRAPH.exists():
        dst = tree_dst / "cli_keyword_graph.json"
        shutil.copy2(CLI_GRAPH, dst)
        manifest["components"]["cli_keyword_graph"] = {
            "path": str(CLI_GRAPH),
            "sha256": _sha256_file(dst),
            "size_bytes": dst.stat().st_size,
        }
    else:
        manifest["components"]["cli_keyword_graph"] = {
            "skipped": skip_cli_graph or not CLI_GRAPH.exists(),
        }

    # ── skeleton_index.db ────────────────────────────────────────
    if SKELETON_DB.exists():
        dst = tree_dst / "skeleton_index.db"
        shutil.copy2(SKELETON_DB, dst)
        manifest["components"]["skeleton_index_db"] = {
            "sha256": _sha256_file(dst),
            "size_bytes": dst.stat().st_size,
        }
    else:
        manifest["components"]["skeleton_index_db"] = {"skipped": True}

    # ── graphrag output (parquet + lancedb) ─────────────────────
    if GRAPHRAG_OUTPUT.exists():
        pq_count = 0
        for pq in GRAPHRAG_OUTPUT.glob("*.parquet"):
            shutil.copy2(pq, gr_dst / pq.name)
            pq_count += 1
        ldb = GRAPHRAG_OUTPUT / "lancedb"
        if ldb.exists():
            shutil.copytree(ldb, gr_dst / "lancedb", dirs_exist_ok=True)
        manifest["components"]["graphrag_output"] = {
            "parquet_count": pq_count,
            "lancedb": ldb.exists(),
        }
    else:
        manifest["components"]["graphrag_output"] = {"warning": "output dir missing"}

    # ── Qdrant local persist ────────────────────────────────────
    qdrant_url = (os.environ.get("QDRANT_URL") or "").strip()
    if qdrant_url:
        manifest["components"]["qdrant"] = {
            "mode": "remote",
            "url_set": True,
            "note": "未拷贝向量文件；请在服务端做 snapshot 或恢复 KB/GraphRAG 后重跑向量导入",
        }
    elif skip_qdrant:
        manifest["components"]["qdrant"] = {"skipped": True}
    else:
        qdir = _local_qdrant_dir()
        if qdir.exists() and any(qdir.iterdir()):
            q_dst = snap / "qdrant_local"
            shutil.copytree(qdir, q_dst, dirs_exist_ok=True)
            manifest["components"]["qdrant"] = {
                "mode": "local",
                "source_path": str(qdir),
                "copied": True,
            }
        else:
            manifest["components"]["qdrant"] = {
                "mode": "local",
                "warning": "directory missing or empty",
                "path": str(qdir),
            }

    (snap / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("检索基线已创建: %s", snap)
    return snap


def restore_baseline(snap_dir: Path, *, dry_run: bool = False) -> None:
    snap_dir = snap_dir.resolve()
    man_path = snap_dir / "manifest.json"
    if not man_path.exists():
        raise FileNotFoundError(f"缺少 manifest.json: {snap_dir}")

    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    logger.info("恢复基线 manifest schema=%s created=%s", manifest.get("schema_version"), manifest.get("created_utc"))

    ref_src = snap_dir / "reference"
    if ref_src.exists():
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        for f in ref_src.glob("*.json"):
            dst = REFERENCE_DIR / f.name
            logger.info("%s → %s", f, dst)
            if not dry_run:
                shutil.copy2(f, dst)
    else:
        logger.warning("快照中无 reference/，跳过")

    tree_src = snap_dir / "tree"
    g = tree_src / "cli_keyword_graph.json"
    if g.exists():
        logger.info("%s → %s", g, CLI_GRAPH)
        if not dry_run:
            shutil.copy2(g, CLI_GRAPH)
    sk = tree_src / "skeleton_index.db"
    if sk.exists():
        SKELETON_DB.parent.mkdir(parents=True, exist_ok=True)
        logger.info("%s → %s", sk, SKELETON_DB)
        if not dry_run:
            shutil.copy2(sk, SKELETON_DB)

    gr_src = snap_dir / "graphrag_output"
    if gr_src.exists() and any(gr_src.iterdir()):
        GRAPHRAG_OUTPUT.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            for item in GRAPHRAG_OUTPUT.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        for pq in gr_src.glob("*.parquet"):
            logger.info("%s → %s", pq, GRAPHRAG_OUTPUT / pq.name)
            if not dry_run:
                shutil.copy2(pq, GRAPHRAG_OUTPUT / pq.name)
        ldb_src = gr_src / "lancedb"
        if ldb_src.exists():
            ldb_dst = GRAPHRAG_OUTPUT / "lancedb"
            logger.info("%s → %s", ldb_src, ldb_dst)
            if not dry_run:
                if ldb_dst.exists():
                    shutil.rmtree(ldb_dst)
                shutil.copytree(ldb_src, ldb_dst)
    else:
        logger.warning("快照中无 graphrag_output/，跳过 GraphRAG 磁盘恢复")

    q_src = snap_dir / "qdrant_local"
    q_meta = (manifest.get("components") or {}).get("qdrant") or {}
    if q_meta.get("mode") == "remote":
        logger.warning("基线创建时使用远端 Qdrant，未包含向量目录；请自行处理向量层或重跑导入")
    elif q_src.exists():
        q_dst = _local_qdrant_dir()
        logger.info("%s → %s (将替换本地 Qdrant 目录)", q_src, q_dst)
        if not dry_run:
            q_dst.parent.mkdir(parents=True, exist_ok=True)
            if q_dst.exists():
                shutil.rmtree(q_dst)
            shutil.copytree(q_src, q_dst)
    else:
        logger.warning("快照中无 qdrant_local/；混合检索向量层未恢复（可依赖 knowledge_base 指纹在下次启动时重建）")

    logger.info("恢复完成（dry_run=%s）。请重启服务并视需要 graphrag_integration.reload()", dry_run)


def main() -> None:
    p = argparse.ArgumentParser(description="检索基线快照 create | restore")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="创建新基线目录")
    c.add_argument("--label", default="", help="标签，写入目录名")
    c.add_argument(
        "--skip-cli-graph",
        action="store_true",
        help="不拷贝 cli_keyword_graph.json（极大文件、仅改 reference 时可用）",
    )
    c.add_argument(
        "--skip-qdrant",
        action="store_true",
        help="不拷贝本地 Qdrant 目录",
    )

    r = sub.add_parser("restore", help="从基线目录恢复")
    r.add_argument("snapshot_dir", type=Path, help="retrieval_baselines/<id> 路径")
    r.add_argument("--dry-run", action="store_true", help="只打印将执行的操作")

    args = p.parse_args()
    BASELINES_ROOT.mkdir(parents=True, exist_ok=True)

    if args.cmd == "create":
        create_baseline(
            args.label,
            skip_cli_graph=args.skip_cli_graph,
            skip_qdrant=args.skip_qdrant,
        )
    else:
        restore_baseline(args.snapshot_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
