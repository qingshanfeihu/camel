# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
"""Copy reference into test_data quality output folder for a named run."""

from __future__ import annotations

import argparse
from pathlib import Path

from INAGENT.utils.quality_test_output import prepare_run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Run id subdir under test_data output (default: timestamp)",
    )
    parser.add_argument(
        "--copy-reference",
        action="store_true",
        help="Copy knowledge_base/reference files into <run_id>/ingest/",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Source reference directory (default: knowledge_base/reference)",
    )
    args = parser.parse_args()
    ref = args.reference
    root = prepare_run_dir(
        args.run_id,
        reference_dir=ref,
        copy_reference=args.copy_reference,
    )
    print(root)


if __name__ == "__main__":
    main()
