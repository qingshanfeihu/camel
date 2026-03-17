#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
GraphRAG ��ʼ���ű�

���ڳ�ʼ�� GraphRAG �����ռ�͹���������

ʹ�÷�ʽ��
    # ��ʼ�������ռ䣨�������ú� Prompts��
    python scripts/init_graphrag.py --init
    
    # ��������
    python scripts/init_graphrag.py --build
    
    # ���״̬
    python scripts/init_graphrag.py --status
    
    # ������ʼ������������ + ����������
    python scripts/init_graphrag.py --init --build
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ������Ŀ·��
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.utils import env_utils
from INAGENT.rag.graphrag_adapter import (
    initialize_graphrag_index,
    validate_graphrag_index,
    get_graphrag_status,
    load_siliconflow_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ��̱���־����ʱ���������ˢ�£����ڶ�λ����
def _milestone(msg: str, *args) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args:
        logger.info("[MILESTONE] %s | " + msg, ts, *args)
    else:
        logger.info("[MILESTONE] %s | %s", ts, msg)
    sys.stdout.flush()
    sys.stderr.flush()


def init_workspace(workspace_dir: Path, knowledge_base_path: Path):
    """��ʼ�� GraphRAG �����ռ�"""
    _milestone("init_workspace START")
    logger.info("=" * 60)
    logger.info("��ʼ�� GraphRAG �����ռ�")
    logger.info("=" * 60)

    _milestone("load_siliconflow_config START")
    config = load_siliconflow_config()
    logger.info("Chat Model: %s | Embedding Model: %s", config.chat_model, config.embedding_model)
    _milestone("load_siliconflow_config DONE")

    if not knowledge_base_path.exists():
        logger.error("֪ʶ���ļ�������: %s", knowledge_base_path)
        return False
    _milestone("initialize_graphrag_index START (kb=%s)", knowledge_base_path)
    workspace = initialize_graphrag_index(
        knowledge_base_path=knowledge_base_path,
        workspace_dir=workspace_dir,
    )
    _milestone("initialize_graphrag_index DONE | workspace=%s", workspace)

    _milestone("validate_graphrag_index START")
    validation = validate_graphrag_index(workspace)
    if validation["valid"]:
        logger.info("�����ռ���֤ͨ��")
    else:
        logger.error("�����ռ���֤ʧ�ܣ�ȱʧ: %s", validation["missing"])
        return False
    _milestone("init_workspace DONE")
    return True


async def build_index(workspace_dir: Path):
    """构建 GraphRAG 索引"""
    _milestone("build_index (async) START | workspace=%s", str(workspace_dir))
    logger.info("=" * 60)
    logger.info("构建 GraphRAG 索引")
    logger.info("=" * 60)

    try:
        _milestone("import GraphRAGRetriever START")
        from INAGENT.rag.graphrag_integration import GraphRAGRetriever
        _milestone("import GraphRAGRetriever DONE")

        retriever = GraphRAGRetriever(workspace_dir=workspace_dir)

        # Start a background log monitor to display real-time progress
        monitor = _LogProgressMonitor(
            workspace_dir / "logs" / "indexing-engine.log",
            interval=15,
        )
        monitor.start()

        try:
            _milestone("retriever.build_index(force_rebuild=True) START")
            success = await retriever.build_index(force_rebuild=True)
            _milestone("retriever.build_index DONE | success=%s", success)
        finally:
            monitor.stop()

        if success:
            logger.info("索引构建成功")
        else:
            logger.error("索引构建失败")
        return success

    except Exception as e:
        logger.error("构建过程出错: %s", e, exc_info=True)
        _milestone("build_index EXCEPTION | %s", str(e))
        return False


class _LogProgressMonitor:
    """Background thread that tails the GraphRAG indexing-engine.log and
    prints periodic human-readable progress summaries to stdout so the
    pipeline script doesn't look stuck."""

    _PROGRESS_RE = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ - INFO - "
        r"graphrag\.logger\.progress - (.+?) progress: (\d+)/(\d+)"
    )
    _WORKFLOW_RE = re.compile(
        r"Workflow started: (.+)"
    )

    _PHASE_LABELS = {
        "extract_graph": "实体/关系抽取",
        "Summarize entity/relationship description": "描述摘要",
        "Generate text embeddings": "文本向量化",
        "Graph Embedding": "图向量化",
        "Create community reports": "社区报告",
    }

    def __init__(self, log_path: Path, interval: float = 15):
        self._log_path = log_path
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_offset = 0
        self._current_phase = ""
        self._current_done = 0
        self._current_total = 0
        self._phase_start: float | None = None
        self._last_print = ""

    def start(self):
        # Seek to end of existing log so we only track new entries
        if self._log_path.exists():
            self._last_offset = self._log_path.stat().st_size
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        # Print a final newline to clean up the progress line
        print()

    def _run(self):
        while not self._stop_event.is_set():
            self._read_new_lines()
            self._print_status()
            self._stop_event.wait(self._interval)

    def _read_new_lines(self):
        if not self._log_path.exists():
            return
        try:
            with open(self._log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._last_offset)
                new_data = f.read()
                self._last_offset = f.tell()
        except OSError:
            return
        if not new_data:
            return

        for line in new_data.splitlines():
            m = self._PROGRESS_RE.search(line)
            if m:
                phase = m.group(2)
                done = int(m.group(3))
                total = int(m.group(4))
                if phase != self._current_phase:
                    # New phase started
                    if self._current_phase:
                        print()  # newline after previous phase
                    self._current_phase = phase
                    self._phase_start = time.monotonic()
                    label = self._PHASE_LABELS.get(phase, phase)
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{ts}] >> 阶段: {label} (共 {total} 项)")
                    sys.stdout.flush()
                self._current_done = done
                self._current_total = total
                continue

            wm = self._WORKFLOW_RE.search(line)
            if wm:
                wf_name = wm.group(1)
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{ts}] * 工作流: {wf_name}")
                sys.stdout.flush()

    def _print_status(self):
        if not self._current_phase or self._current_total == 0:
            return

        pct = self._current_done / self._current_total * 100
        label = self._PHASE_LABELS.get(self._current_phase, self._current_phase)
        elapsed = time.monotonic() - (self._phase_start or time.monotonic())

        # Calculate ETA
        eta_str = ""
        if self._current_done > 0 and elapsed > 5:
            rate = self._current_done / elapsed
            remaining = (self._current_total - self._current_done) / rate
            if remaining > 3600:
                eta_str = f" | 预计剩余 {remaining/3600:.1f}h"
            elif remaining > 60:
                eta_str = f" | 预计剩余 {remaining/60:.0f}min"
            else:
                eta_str = f" | 预计剩余 {remaining:.0f}s"

        bar_width = 30
        filled = int(bar_width * self._current_done / self._current_total)
        bar = "#" * filled + "-" * (bar_width - filled)

        status = (
            f"\r  [{bar}] {pct:5.1f}%  "
            f"{self._current_done}/{self._current_total}  "
            f"({elapsed:.0f}s 已用{eta_str})"
        )
        # Only reprint if changed
        if status != self._last_print:
            print(status, end="", flush=True)
            self._last_print = status


def show_status(workspace_dir: Path):
    """��ʾ GraphRAG ״̬"""
    logger.info("=" * 60)
    logger.info("GraphRAG ״̬")
    logger.info("=" * 60)
    
    status = get_graphrag_status()
    
    print(f"\n�����ռ�: {status['workspace']}")
    print(f"����: {'? ��' if status['available'] else '? ��'}")
    print(f"�����ѹ���: {'? ��' if status['index_built'] else '? ��'}")
    
    if status.get('entity_count'):
        print(f"\n����ͳ��:")
        print(f"  - ʵ������: {status.get('entity_count', 0)}")
        print(f"  - ��ϵ����: {status.get('relationship_count', 0)}")
        print(f"  - ��������: {status.get('community_count', 0)}")
        print(f"  - �ı���Ԫ: {status.get('text_unit_count', 0)}")
    
    validation = status.get('validation', {})
    if validation.get('missing'):
        print(f"\n?? ȱʧ�ļ�: {validation['missing']}")
    
    print()


async def test_search(workspace_dir: Path, query: str):
    """���� GraphRAG ����"""
    logger.info("=" * 60)
    logger.info("���� GraphRAG ����")
    logger.info("=" * 60)
    
    try:
        from INAGENT.rag.graphrag_integration import GraphRAGRetriever
        
        retriever = GraphRAGRetriever(workspace_dir=workspace_dir)
        
        if not retriever.is_available():
            logger.error("GraphRAG ����δ�������������� --build")
            return
        
        print(f"\n��ѯ: {query}")
        print("-" * 40)
        
        # ���� local search
        response, results = await retriever.local_search(query, top_k=5)
        
        print(f"\n[Local Search] ��Ӧ:")
        print(response[:500] + "..." if len(response) > 500 else response)
        print(f"\n������ {len(results)} �����")
        
        for i, result in enumerate(results[:3]):
            print(f"\n��� {i+1}:")
            print(f"  ����: {result.score:.4f}")
            print(f"  ��Դ: {result.source}")
            print(f"  �ı�: {result.text[:200]}...")
        
    except Exception as e:
        logger.error(f"�������Գ���: {e}", exc_info=True)

def _postprocess_entity_types(workspace_dir: Path):
    """清理实体类型中的引号残留。

    当 model_supports_json=False 时，LLM 有时会在 type 字段中返回
    带双引号的值（如 '"COMMAND"' 而非 'COMMAND'）。此函数统一去除
    首尾引号，使同类实体可以被正确聚合。
    """
    entities_path = workspace_dir / "output" / "entities.parquet"
    if not entities_path.exists():
        return
    try:
        import pandas as pd

        df = pd.read_parquet(entities_path)
        if "type" not in df.columns:
            return
        cleaned = df["type"].str.strip('"')
        changed = (cleaned != df["type"]).sum()
        if changed == 0:
            logger.info("实体类型无需清理")
            return
        df["type"] = cleaned
        df.to_parquet(entities_path, index=False)
        logger.info("已清理 %d 个实体的类型引号 (共 %d)", changed, len(df))
    except Exception as e:
        logger.warning("实体类型后处理失败: %s", e)

def main():
    parser = argparse.ArgumentParser(description="GraphRAG ��ʼ���ű�")
    
    parser.add_argument(
        "--init",
        action="store_true",
        help="��ʼ�������ռ䣨�������ú� Prompts��"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="���� GraphRAG ����"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="��ʾ GraphRAG ״̬"
    )
    parser.add_argument(
        "--test",
        type=str,
        metavar="QUERY",
        help="����������ָ����ѯ���ݣ�"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(__file__).parent.parent / "graphrag_index",
        help="GraphRAG �����ռ�Ŀ¼"
    )
    parser.add_argument(
        "--knowledge-base",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json",
        help="֪ʶ���ļ�·��"
    )
    
    args = parser.parse_args()

    # ���ػ�������
    env_utils.load_inagent_env()

    # ��ѡ��������������־ͬʱд���ļ������ڶ�λ����
    log_dir = Path(__file__).parent.parent / "knowledge_base" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ("graphrag_init_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(fh)
    logger.info("����������־�ļ�: %s", log_file)

    # ִ�в���
    if args.status:
        show_status(args.workspace)
    
    if args.init:
        success = init_workspace(args.workspace, args.knowledge_base)
        if not success:
            sys.exit(1)
    
    if args.build:
        success = asyncio.run(build_index(args.workspace))
        if not success:
            sys.exit(1)
        _postprocess_entity_types(args.workspace)
    
    if args.test:
        asyncio.run(test_search(args.workspace, args.test))
    
    if not any([args.init, args.build, args.status, args.test]):
        parser.print_help()


if __name__ == "__main__":
    main()
