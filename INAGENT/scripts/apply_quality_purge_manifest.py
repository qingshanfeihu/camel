#!/usr/bin/env python3
"""Apply _quality_purge_manifest.jsonl to reference/*.json (default dry-run)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from INAGENT.data_tools.quality_feedback_loop import (
    QUALITY_PURGE_MANIFEST_FILE,
    apply_purge_manifest,
    load_and_validate_purge_manifest,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reference-dir", type=Path, default=None)
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify reference files (default is dry-run)",
    )
    p.add_argument(
        "--no-strict-inagent",
        action="store_true",
        help="Allow reference-dir outside INAGENT tree (tests only)",
    )
    args = p.parse_args()
    inagent_root = Path(__file__).resolve().parent.parent
    ref = args.reference_dir or (inagent_root / "knowledge_base" / "reference")
    manifest = args.manifest or (ref / QUALITY_PURGE_MANIFEST_FILE)

    records, errors = load_and_validate_purge_manifest(manifest)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1

    dry_run = not args.apply
    report = apply_purge_manifest(
        ref,
        records,
        dry_run=dry_run,
        strict_inagent=not args.no_strict_inagent,
    )
    print(json.dumps({"dry_run": report.dry_run, "applied": len(report.applied), "skipped": len(report.skipped)}, indent=2))
    for gk, reason in report.skipped[:20]:
        print(f"  skip {gk}: {reason}", file=sys.stderr)
    if len(report.skipped) > 20:
        print(f"  ... and {len(report.skipped) - 20} more", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
