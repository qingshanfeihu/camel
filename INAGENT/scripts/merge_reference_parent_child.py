#!/usr/bin/env python3
"""Run mechanical parent/child reference merge from YAML or JSON config."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from INAGENT.data_tools.merge_reference_parent_child import merge_reference_parent_child


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reference-dir", type=Path, default=None)
    p.add_argument("--config", type=Path, required=True, help="YAML or JSON config path")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write parent and empty child files (default dry-run)",
    )
    p.add_argument(
        "--no-strict-inagent",
        action="store_true",
        help="Allow reference-dir outside INAGENT tree (tests only)",
    )
    args = p.parse_args()
    inagent_root = Path(__file__).resolve().parent.parent
    ref = args.reference_dir or (inagent_root / "knowledge_base" / "reference")

    report = merge_reference_parent_child(
        ref,
        args.config,
        dry_run=not args.apply,
        strict_inagent=not args.no_strict_inagent,
    )
    print(
        json.dumps(
            {
                "dry_run": report.dry_run,
                "jobs": report.jobs,
                "chunks_moved": report.chunks_moved,
                "child_files_emptied": report.child_files_emptied,
                "skipped": report.skipped,
                "block_id_collisions": report.block_id_collisions,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
