#!/usr/bin/env python3
"""Build _quality_rule_proposals.json from a validated feedback inbox."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from INAGENT.data_tools.quality_feedback_loop import (
    QUALITY_FEEDBACK_INBOX_FILE,
    QUALITY_RULE_PROPOSALS_FILE,
    build_rule_proposals_from_inbox,
    load_and_validate_inbox,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reference-dir", type=Path, default=None)
    p.add_argument("--inbox", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None, help="Output JSON path")
    args = p.parse_args()
    inagent_root = Path(__file__).resolve().parent.parent
    ref = args.reference_dir or (inagent_root / "knowledge_base" / "reference")
    inbox = args.inbox or (ref / QUALITY_FEEDBACK_INBOX_FILE)
    out = args.out or (ref / QUALITY_RULE_PROPOSALS_FILE)

    valid, errors = load_and_validate_inbox(inbox)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1

    doc = build_rule_proposals_from_inbox(valid, inbox_path=str(inbox))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[proposals] wrote {out} proposals={doc['proposal_count']} conflicts={len(doc['conflicts'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
