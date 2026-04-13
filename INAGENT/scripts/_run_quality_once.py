"""One-shot quality pipeline runner for manual test runs."""
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

from INAGENT.utils.env_utils import load_inagent_env  # noqa: E402

load_inagent_env()

from INAGENT.data_tools.quality_ingest import run_quality_pipeline  # noqa: E402

ingest_dir = Path("INAGENT/test_data/质检员输出/quality_20260413_170858/ingest")
archive_to = Path("INAGENT/test_data/质检员输出/quality_20260413_170858")

result = run_quality_pipeline(
    reference_dir=ingest_dir,
    archive_to=archive_to,
)

print()
print("=== 质检结果 ===")
ir = result.get("ingest_report") or {}
total = ir.get("total", "?")
accepted = ir.get("accepted", "?")
rejected = ir.get("rejected", "?")
duplicates = ir.get("duplicates", "?")
quarantined = ir.get("quarantined", "?")
gate_total = result.get("quality_gate_total", "?")
gate_passed = result.get("quality_gate_passed", "?")
gate_blocked = result.get("quality_gate_blocked", "?")
hook = result.get("owner_rule_hook", {})
cond_total = hook.get("condition_rules_total", 0)
cond_pass = hook.get("condition_rules_to_pass", 0)
cond_block = hook.get("condition_rules_to_block", 0)
ovr_total = hook.get("overrides_total", 0)

print(f"  total chunks   : {total}")
print(f"  accepted       : {accepted}")
print(f"  rejected       : {rejected}")
print(f"  duplicates     : {duplicates}")
print(f"  quarantined    : {quarantined}")
print(f"  gate total     : {gate_total}")
print(f"  gate passed    : {gate_passed}")
print(f"  gate blocked   : {gate_blocked}")
print(f"  condition rules: total={cond_total} pass={cond_pass} block={cond_block}")
print(f"  per-block hook : total={ovr_total}")
print(f"  filtered ref   : {result.get('reference_for_owner_path')}")
print()
print(f"output dir: {archive_to.resolve()}")
print("files:")
for p in sorted(archive_to.iterdir()):
    if p.is_file():
        size = p.stat().st_size
        print(f"  {p.name}  ({size:,} bytes)")
print("subdirs:")
for p in sorted(archive_to.iterdir()):
    if p.is_dir():
        count = sum(1 for _ in p.iterdir())
        print(f"  {p.name}/  ({count} items)")
