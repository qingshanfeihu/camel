# -*- coding: utf-8 -*-
"""
Workflow 流程日志：LLM 调用输入/输出同时输出到终端并写入日志文件。
用于完整记录任务分解、RAG、配置生成等步骤。
"""
import logging
import sys
from pathlib import Path
from datetime import datetime

# 当前会话的 workflow 日志文件路径（由 setup_workflow_logger 设置）
_workflow_log_file: Path = None
_workflow_logger: logging.Logger = None

# 单条日志最大字符数（超出则截断并注明）
MAX_LOG_PREVIEW = 8000


def setup_workflow_logger(log_dir: Path = None, log_prefix: str = "workflow") -> logging.Logger:
    """
    创建并返回 workflow 专用 logger，同时输出到终端和日志文件。
    若 log_dir 为 None，使用 INAGENT/knowledge_base/logs。
    """
    global _workflow_log_file, _workflow_logger
    if log_dir is None:
        log_dir = Path(__file__).parent / "knowledge_base" / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _workflow_log_file = log_dir / f"{log_prefix}_{ts}.log"

    _workflow_logger = logging.getLogger("INAGENT.workflow")
    _workflow_logger.setLevel(logging.DEBUG)
    _workflow_logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(_workflow_log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    _workflow_logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    _workflow_logger.addHandler(ch)

    _workflow_logger.info("Workflow 日志文件: %s", _workflow_log_file)
    return _workflow_logger


def get_workflow_logger() -> logging.Logger:
    """返回已创建的 workflow logger；若未创建则用默认目录创建。"""
    global _workflow_logger
    if _workflow_logger is None:
        setup_workflow_logger()
    return _workflow_logger


def get_workflow_log_path() -> Path:
    """返回当前 workflow 日志文件路径。"""
    return _workflow_log_file


def _truncate(s: str, max_len: int = MAX_LOG_PREVIEW) -> str:
    if not s or len(s) <= max_len:
        return s
    return s[:max_len] + "\n... [已截断，共 %d 字符]" % len(s)


def log_llm_input(role: str, prompt: str, logger: logging.Logger = None):
    """记录 LLM 输入（终端 + 日志文件）。"""
    log = logger or get_workflow_logger()
    log.info("")
    log.info("=" * 60)
    log.info("[LLM 输入] %s", role)
    log.info("=" * 60)
    log.info(_truncate(prompt))
    log.info("")


def log_llm_output(role: str, response: str, logger: logging.Logger = None):
    """记录 LLM 输出（终端 + 日志文件）。"""
    log = logger or get_workflow_logger()
    log.info("")
    log.info("-" * 60)
    log.info("[LLM 输出] %s", role)
    log.info("-" * 60)
    log.info(_truncate(response))
    log.info("")


def log_section(title: str, body: str = "", logger: logging.Logger = None):
    """记录流程中的一节标题与可选正文。"""
    log = logger or get_workflow_logger()
    log.info("")
    log.info("### %s ###", title)
    if body:
        log.info(_truncate(body))
    log.info("")
