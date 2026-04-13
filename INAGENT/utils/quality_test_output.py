# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Helpers for quality inspector test output under ``test_data/质检员输出``."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

_INAGENT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REFERENCE_DIR = _INAGENT_ROOT / "knowledge_base" / "reference"
DEFAULT_OUTPUT_BASE = _INAGENT_ROOT / "test_data" / "质检员输出"


def prepare_run_dir(
    run_id: str | None = None,
    *,
    reference_dir: Path | None = None,
    out_base: Path | None = None,
    copy_reference: bool = False,
) -> Path:
    """Create ``test_data/质检员输出/<run_id>/{ingest,procurement_contract}``.

    Optionally copy files from ``reference_dir`` into ``ingest/``.
    """
    rid = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    base = out_base or DEFAULT_OUTPUT_BASE
    ref_src = reference_dir or DEFAULT_REFERENCE_DIR
    run_root = base / rid
    ingest = run_root / "ingest"
    ingest.mkdir(parents=True, exist_ok=True)
    (run_root / "procurement_contract").mkdir(exist_ok=True)

    if copy_reference and ref_src.is_dir():
        for p in ref_src.iterdir():
            if p.is_file():
                shutil.copy2(p, ingest / p.name)
    return run_root
