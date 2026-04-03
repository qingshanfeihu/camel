#!/usr/bin/env python3
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""
GraphRAG 初始化手册

主要包括初始化 GraphRAG 索引和相关操作

使用方式：
    # 初始化工作空间（首次使用 Prompts）
    python scripts/init_graphrag.py --init
    
    # 构建索引
    python scripts/init_graphrag.py --build
    
    # 查看状态
    python scripts/init_graphrag.py --status
    
    # 初始化 + 构建索引
    python scripts/init_graphrag.py --init --build
"""
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 设置项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from INAGENT.utils import env_utils
from INAGENT.rag.graphrag_adapter import (
    initialize_graphrag_index,
    validate_graphrag_index,
    get_graphrag_status,
    load_gateway_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# milestone 日志使用特殊格式，方便解析和过滤，第二列是时间戳
def _milestone(msg: str, *args) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args:
        logger.info("[MILESTONE] %s | " + msg, ts, *args)
    else:
        logger.info("[MILESTONE] %s | %s", ts, msg)
    sys.stdout.flush()
    sys.stderr.flush()


def init_workspace(workspace_dir: Path, knowledge_base_path: Path):
    """初始化 GraphRAG 索引"""
    _milestone("init_workspace START")
    logger.info("=" * 60)
    logger.info("初始化 GraphRAG 索引")
    logger.info("=" * 60)

    _milestone("load_gateway_config START")
    config = load_gateway_config()
    logger.info("Chat Model: %s | Embedding Model: %s", config.chat_model, config.embedding_model)
    _milestone("load_gateway_config DONE")

    if not knowledge_base_path.exists():
        logger.error("知识库文件不存在: %s", knowledge_base_path)
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
        logger.info("索引验证通过")
    else:
        logger.error("索引验证失败: 缺失: %s", validation["missing"])
        return False
    _milestone("init_workspace DONE")
    return True


async def build_index(
    workspace_dir: Path,
    *,
    resume: bool = False,
    fast: bool = False,
    update: bool = False,
):
    """构建 GraphRAG 索引

    Args:
        resume: 续跑中断的构建（保留 cache/output）
        fast: NLP 抽取代替 LLM（跳过 extract_graph LLM 调用）
        update: 增量更新（仅处理新增/变更文档）
    """
    mode_label = "resume" if resume else ("update" if update else "rebuild")
    _milestone("build_index (async) START | workspace=%s mode=%s fast=%s",
              str(workspace_dir), mode_label, fast)
    logger.info("=" * 60)
    logger.info("构建 GraphRAG 索引 (mode=%s, fast=%s)", mode_label, fast)
    logger.info("=" * 60)

    try:
        _milestone("import GraphRAGRetriever START")
        from INAGENT.rag.graphrag_integration import GraphRAGRetriever
        _milestone("import GraphRAGRetriever DONE")

        retriever = GraphRAGRetriever(workspace_dir=workspace_dir)

        # Suppress graphrag's built-in progress logger to avoid duplicate lines
        logging.getLogger("graphrag.logger.progress").setLevel(logging.WARNING)

        # Start a background log monitor to display real-time progress
        monitor = _LogProgressMonitor(
            workspace_dir / "logs" / "indexing-engine.log",
            interval=1,
        )
        monitor.start()

        try:
            _milestone("retriever.build_index(mode=%s, fast=%s) START", mode_label, fast)
            success = await retriever.build_index(
                force_rebuild=not (resume or update),
                resume=resume,
                fast=fast,
                update=update,
            )
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
    _WORKFLOW_RE = re.compile(r"Workflow started: (.+)")
    _WORKFLOW_DONE_RE = re.compile(
        r"Workflow completed successfully: (.+)"   # graphrag.api.index 格式
        r"|Workflow completed: (.+)"               # workflow 内部格式
        r"|Workflow (\S+) completed successfully"  # 另一种格式
    )

    _PHASE_LABELS = {
        "extract_graph": "实体/关系抽取",
        "Summarize entity/relationship description": "描述摘要",
        "Generate text embeddings": "文本向量化",
        "Graph Embedding": "图向量化",
        "Create community reports": "社区报告",
    }

    _WORKFLOW_LABELS = {
        "load_input": "加载输入文档",
        "create_base_text_units": "切分文本单元",
        "create_final_documents": "生成文档索引",
        "extract_graph": "抽取实体与关系",
        "finalize_graph": "图谱后处理",
        "extract_covariates": "抽取协变量",
        "create_communities": "构建社区结构",
        "create_final_text_units": "构建最终文本单元",
        "create_community_reports": "生成社区报告",
        "generate_text_embeddings": "生成文本向量",
    }

    _SPINNER = "|/-\\"

    _TOTAL_WORKFLOWS = 10  # expected workflow count for a full build

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
        self._tick = 0
        self._wf_started: list[str] = []
        self._wf_succeeded: list[str] = []

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
        # Final flush: read any remaining log lines after the thread exits
        self._read_new_lines()
        # Clear the current progress line
        print("\r" + " " * 80 + "\r", end="", flush=True)
        ts = datetime.now().strftime("%H:%M:%S")
        total = len(self._wf_started)
        ok = len(self._wf_succeeded)
        print(f"[{ts}] 构建结束：共启动 {total} 个工作流，成功完成 {ok} 个")

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
                if wf_name not in self._wf_started:
                    self._wf_started.append(wf_name)
                label = self._WORKFLOW_LABELS.get(wf_name, wf_name)
                ts = datetime.now().strftime("%H:%M:%S")
                total = self._TOTAL_WORKFLOWS
                idx = len(self._wf_started)
                ok = len(self._wf_succeeded)
                print(f"\n[{ts}] [{idx}/{total} | ✓{ok}] {label}")
                sys.stdout.flush()
                continue

            dm = self._WORKFLOW_DONE_RE.search(line)
            if dm:
                wf_name = dm.group(1) or dm.group(2) or dm.group(3)
                if wf_name and wf_name not in self._wf_succeeded:
                    self._wf_succeeded.append(wf_name)
                    label = self._WORKFLOW_LABELS.get(wf_name, wf_name)
                    ts = datetime.now().strftime("%H:%M:%S")
                    ok = len(self._wf_succeeded)
                    total = self._TOTAL_WORKFLOWS
                    print(f"\n[{ts}] [✓{ok}/{total}] {label} 完成")
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
        spinner = self._SPINNER[self._tick % len(self._SPINNER)]
        self._tick += 1
        if filled < bar_width:
            bar = "#" * filled + spinner + "-" * (bar_width - filled - 1)
        else:
            bar = "#" * bar_width

        status = (
            f"\r  [{bar}] {pct:5.1f}%  "
            f"{self._current_done}/{self._current_total}  "
            f"({elapsed:.0f}s 已用{eta_str})"
        )
        print(status, end="", flush=True)
        if self._current_done >= self._current_total:
            label = self._PHASE_LABELS.get(self._current_phase, self._current_phase)
            print(f"  ✓ {label} 完成", flush=True)
            self._current_phase = ""
            self._current_done = 0
            self._current_total = 0


def show_status(workspace_dir: Path):
    """显示 GraphRAG 状态"""
    logger.info("=" * 60)
    logger.info("GraphRAG 状态")
    logger.info("=" * 60)
    
    status = get_graphrag_status()
    
    print(f"\n工作空间: {status['workspace']}")
    print(f"可用性: {'[OK] 可用' if status['available'] else '[X] 不可用'}")
    print(f"索引压缩: {'[OK] 已构建' if status['index_built'] else '[X] 未构建'}")
    
    if status.get('entity_count'):
        print(f"\n统计信息:")
        print(f"  - 实体数量: {status.get('entity_count', 0)}")
        print(f"  - 关系数量: {status.get('relationship_count', 0)}")
        print(f"  - 社区数量: {status.get('community_count', 0)}")
        print(f"  - 文本单元: {status.get('text_unit_count', 0)}")
    
    validation = status.get('validation', {})
    if validation.get('missing'):
        print(f"\n? 缺失文件: {validation['missing']}")
    
    print()


def run_coverage_check(terms: str, with_unified_rag: bool = False) -> int:
    """运行知识覆盖门禁脚本。"""
    script = Path(__file__).parent / "check_knowledge_coverage.py"
    cmd = [sys.executable, str(script), "--terms", terms]
    if with_unified_rag:
        cmd.append("--with-unified-rag")
    logger.info("运行知识覆盖检查: %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False)
    return int(proc.returncode)


async def test_search(workspace_dir: Path, query: str):
    """测试 GraphRAG 搜索"""
    logger.info("=" * 60)
    logger.info("测试 GraphRAG 搜索")
    logger.info("=" * 60)
    
    try:
        from INAGENT.rag.graphrag_integration import GraphRAGRetriever
        
        retriever = GraphRAGRetriever(workspace_dir=workspace_dir)
        
        if not retriever.is_available():
            logger.error("GraphRAG 未初始化，请先运行 --build")
            return
        
        print(f"\n查询: {query}")
        print("-" * 40)
        
        # 运行 local search
        response, results = await retriever.local_search(query, top_k=5)
        
        print(f"\n[Local Search] 响应:")
        print(response[:500] + "..." if len(response) > 500 else response)
        print(f"\n返回 {len(results)} 个结果")
        
        for i, result in enumerate(results[:3]):
            print(f"\n结果 {i+1}:")
            print(f"  得分: {result.score:.4f}")
            print(f"  来源: {result.source}")
            print(f"  文本: {result.text[:200]}...")
        
    except Exception as e:
        logger.error(f"搜索过程中出错: {e}", exc_info=True)

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
    parser = argparse.ArgumentParser(description="GraphRAG 初始化手册")
    
    parser.add_argument(
        "--init",
        action="store_true",
        help="初始化工作空间"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="构建 GraphRAG 索引（全量重建，清空 cache）"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="续跑中断的构建（保留 cache/output，已完成的 LLM 调用命中缓存）"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="用 NLP 抽取代替 LLM（跳过 extract_graph 的 LLM 调用，极大提速但质量降低）"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="增量更新（仅处理新增/变更文档，与现有索引合并）"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="显示 GraphRAG 状态"
    )
    parser.add_argument(
        "--test",
        type=str,
        metavar="QUERY",
        help="测试时指定查询内容"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(__file__).parent.parent / "graphrag_index",
        help="GraphRAG 工作空间目录"
    )
    parser.add_argument(
        "--knowledge-base",
        type=Path,
        default=Path(__file__).parent.parent / "knowledge_base" / "reference" / "knowledge_base.json",
        help="知识库文件路径"
    )
    parser.add_argument(
        "--coverage-terms",
        type=str,
        default="",
        help="执行知识覆盖门禁检查的关键词（逗号/分号分隔）",
    )
    parser.add_argument(
        "--coverage-with-unified-rag",
        action="store_true",
        help="覆盖检查时额外验证 UnifiedRAG 最终上下文命中",
    )
    
    args = parser.parse_args()

    # 加载环境变量
    env_utils.load_inagent_env()

    # 可选参数控制日志同时写入文件，第二列是时间戳
    log_dir = Path(__file__).parent.parent / "knowledge_base" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ("graphrag_init_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(fh)
    logger.info("日志文件: %s", log_file)

    # LiteLLM 会产生大量 INFO 日志，控制台只保留 WARNING+，文件保留全部
    for _noisy in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    try:
        import litellm as _ll
        _ll.suppress_debug_info = True
        _ll.set_verbose = False
    except Exception:
        pass

    # 执行命令
    if args.status:
        show_status(args.workspace)
    
    if args.init:
        success = init_workspace(args.workspace, args.knowledge_base)
        if not success:
            sys.exit(1)
    
    if args.build or args.resume or args.update:
        success = asyncio.run(build_index(
            args.workspace,
            resume=args.resume,
            fast=args.fast,
            update=args.update,
        ))
        if not success:
            sys.exit(1)
        _postprocess_entity_types(args.workspace)
    
    if args.test:
        asyncio.run(test_search(args.workspace, args.test))

    if args.coverage_terms:
        rc = run_coverage_check(
            terms=args.coverage_terms,
            with_unified_rag=args.coverage_with_unified_rag,
        )
        if rc != 0:
            sys.exit(rc)
    
    if not any([args.init, args.build, args.resume, args.update, args.status, args.test]):
        parser.print_help()


if __name__ == "__main__":
    main()
