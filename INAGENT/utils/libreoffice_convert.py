"""LibreOffice 无头模式：将旧版 Word .doc 转为 .docx，供 MarkItDown / spec_parser 使用。"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DocToDocxResult:
    """convert_doc_to_docx 成功时的产物；用完后请删除 ``temp_root`` 整目录。"""

    docx_path: Path
    temp_root: Path


def find_soffice_executable() -> Optional[Path]:
    """解析 soffice 路径：环境变量 → PATH → 常见安装路径。"""
    for env_key in ("INAGENT_LIBREOFFICE_SOFFICE", "SOFFICE_PATH"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
            if p.is_dir():
                for name in ("soffice.exe", "soffice"):
                    cand = p / name
                    if cand.is_file():
                        return cand

    for name in ("soffice", "soffice.exe"):
        w = shutil.which(name)
        if w:
            return Path(w)

    if sys.platform == "win32":
        for pf in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            cand = Path(pf) / "LibreOffice" / "program" / "soffice.exe"
            if cand.is_file():
                return cand
    else:
        for cand in (
            Path("/usr/lib/libreoffice/program/soffice"),
            Path("/usr/bin/soffice"),
            Path("/usr/local/bin/soffice"),
        ):
            if cand.is_file():
                return cand
    return None


def convert_doc_to_docx(
    doc_path: Path,
    *,
    timeout_sec: int = 180,
) -> Optional[DocToDocxResult]:
    """将 ``.doc`` 转为临时目录下的 ``同名.docx``。失败返回 None。"""
    if doc_path.suffix.lower() != ".doc":
        return None

    soffice = find_soffice_executable()
    if soffice is None:
        logger.warning(
            "[libreoffice] 未找到 soffice，请安装 LibreOffice 或设置环境变量 "
            "INAGENT_LIBREOFFICE_SOFFICE 为 soffice 可执行文件路径。"
        )
        return None

    doc_path = doc_path.resolve()
    if not doc_path.is_file():
        logger.warning("[libreoffice] 源文件不存在: %s", doc_path)
        return None

    out_root = Path(tempfile.mkdtemp(prefix="inagent_lo_doc_"))
    try:
        cmd = [
            str(soffice),
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--convert-to",
            "docx",
            "--outdir",
            str(out_root),
            str(doc_path),
        ]
        logger.info(
            "[libreoffice] 转换 %s → docx（超时 %ds）",
            doc_path.name,
            timeout_sec,
        )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if proc.returncode != 0:
            logger.warning(
                "[libreoffice] 转换失败 rc=%s: %s %s",
                proc.returncode,
                proc.stdout[:500] if proc.stdout else "",
                proc.stderr[:500] if proc.stderr else "",
            )
            shutil.rmtree(out_root, ignore_errors=True)
            return None

        expected = out_root / (doc_path.stem + ".docx")
        if expected.is_file():
            return DocToDocxResult(docx_path=expected, temp_root=out_root)

        # 个别环境大小写或编码差异：取目录内第一个 docx
        for f in out_root.glob("*.docx"):
            if f.is_file():
                return DocToDocxResult(docx_path=f, temp_root=out_root)

        logger.warning("[libreoffice] 转换后未找到 docx 输出: %s", out_root)
        shutil.rmtree(out_root, ignore_errors=True)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("[libreoffice] 转换超时（%ds）: %s", timeout_sec, doc_path.name)
        shutil.rmtree(out_root, ignore_errors=True)
        return None
    except OSError as exc:
        logger.warning("[libreoffice] 转换异常: %s", exc)
        shutil.rmtree(out_root, ignore_errors=True)
        return None
