#!/usr/bin/env python
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Tee utility for Windows - pipe output to both console and file

import io
import sys

if len(sys.argv) < 2:
    print("Usage: tee <logfile>")
    sys.exit(1)

logfile = sys.argv[1]

# Force UTF-8 output on Windows to avoid garbled logs
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
    )

with open(logfile, 'w', encoding='utf-8', buffering=1, errors='replace') as f:
    for line in sys.stdin:
        # Write to console
        sys.stdout.write(line)
        sys.stdout.flush()
        # Write to log file with error handling
        try:
            f.write(line)
            f.flush()
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Replace problematic characters
            safe_line = line.encode('utf-8', errors='replace').decode('utf-8')
            f.write(safe_line)
            f.flush()
