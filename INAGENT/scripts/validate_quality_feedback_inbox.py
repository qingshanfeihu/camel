#!/usr/bin/env python3
"""Validate reference/_quality_feedback_inbox.jsonl (exit 1 on errors)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from INAGENT.data_tools.quality_feedback_loop import (
    QUALITY_FEEDBACK_INBOX_FILE,
    load_and_validate_inbox,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--reference-dir",
        type=Path,
        default=None,
        help="Directory containing the inbox (default: INAGENT/knowledge_base/reference)",
    )
    p.add_argument(
        "--inbox",
        type=Path,
        default=None,
        help=f"Inbox file path (default: <reference-dir>/{QUALITY_FEEDBACK_INBOX_FILE})",
    )
    args = p.parse_args()
    inagent_root = Path(__file__).resolve().parent.parent
    ref = args.reference_dir or (inagent_root / "knowledge_base" / "reference")
    inbox = args.inbox or (ref / QUALITY_FEEDBACK_INBOX_FILE)

    valid, errors = load_and_validate_inbox(inbox)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        print(f"[validate] {len(errors)} error(s), {len(valid)} valid record(s)", file=sys.stderr)
        return 1
    print(f"[validate] OK: {len(valid)} record(s) in {inbox}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
