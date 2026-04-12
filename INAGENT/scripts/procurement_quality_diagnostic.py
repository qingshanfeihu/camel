#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Procurement Quality Diagnostic Script
采购质量诊断脚本，用于快速采集数据有效性指标与已知问题诊断

用法：
  python -m INAGENT.scripts.procurement_quality_diagnostic --run-id official_round_20260412_2230
  python -m INAGENT.scripts.procurement_quality_diagnostic --latest
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("procurement_diagnostic")

INAGENT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATA_ROOT = INAGENT_ROOT / "test_data" / "runs"
KB_REFERENCE = INAGENT_ROOT / "knowledge_base" / "reference"


class DiagnosticResult:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.timestamp = None
        self.findings: Dict[str, Any] = {}
        self.issues: List[Tuple[str, str, str]] = []

    def add_finding(self, key: str, value: Any) -> None:
        self.findings[key] = value

    def add_issue(self, level: str, category: str, message: str) -> None:
        self.issues.append((level, category, message))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "findings": self.findings,
            "issues": [{"level": i[0], "category": i[1], "message": i[2]} for i in self.issues],
        }


def find_latest_run_id() -> Optional[str]:
    if not TEST_DATA_ROOT.exists():
        return None
    runs = sorted([d.name for d in TEST_DATA_ROOT.iterdir() if d.is_dir()])
    return runs[-1] if runs else None


def load_manifest(run_id: str) -> Optional[Dict[str, Any]]:
    path = TEST_DATA_ROOT / run_id / "manifest.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_ingest_report() -> Optional[Dict[str, Any]]:
    path = KB_REFERENCE / "_ingest_report.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_knowledge_base() -> Optional[List[Dict[str, Any]]]:
    path = KB_REFERENCE / "knowledge_base.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_auto_convert_log(run_id: str) -> Optional[str]:
    path = TEST_DATA_ROOT / run_id / "auto_convert.log"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def diagnose_baseline(run_id: str) -> DiagnosticResult:
    """診斷1：基線檢查（manifest、error、日誌）。"""
    result = DiagnosticResult(run_id)

    manifest = load_manifest(run_id)
    if not manifest:
        result.add_issue("ERROR", "baseline", f"manifest.json not found for {run_id}")
        return result

    result.timestamp = manifest.get("finished_at")
    result.add_finding("manifest_status", manifest.get("status"))
    result.add_finding("manifest_error", manifest.get("error"))

    error_json_path = TEST_DATA_ROOT / run_id / "error.json"
    if error_json_path.exists():
        result.add_issue("ERROR", "baseline", "error.json exists - procurement process failed")
    else:
        result.add_finding("error_json_exists", False)

    log_content = load_auto_convert_log(run_id)
    if log_content:
        completion_count = log_content.count("[完成]")
        result.add_finding("log_completion_mark_count", completion_count)
        if completion_count != 1:
            result.add_issue("WARN", "baseline", f"[完成] mark appears {completion_count} times, expected 1 (log append duplicate detection)")
        else:
            result.add_finding("log_integrity", "OK")

        if "[office]" in log_content and "HuaWei" in log_content:
            if "HuaWei NAT64&DS-LITE Config Guide.json" not in log_content:
                result.add_issue("WARN", "baseline", "HuaWei file processing started but no end mark (silent failure risk)")

    return result


def diagnose_ingest_report() -> DiagnosticResult:
    """診斷2：採購過濾報告。"""
    result = DiagnosticResult("ingest_phase")
    report = load_ingest_report()
    if not report:
        result.add_issue("WARN", "ingest", "_ingest_report.json not found")
        return result

    input_count = report.get("input_count", 0)
    accepted = report.get("accepted", 0)
    rejected_short = report.get("rejected_short", 0)
    rejected_category = report.get("rejected_category", 0)
    rejected_excluded = report.get("rejected_excluded", 0)
    quarantined = report.get("quarantined", 0)
    duplicates = report.get("duplicates", 0)
    tree_linked = report.get("tree_linked", 0)

    accepted_rate = accepted / input_count if input_count else 0
    quarantine_rate = quarantined / input_count if input_count else 0
    duplicate_rate = duplicates / input_count if input_count else 0

    result.add_finding("input_count", input_count)
    result.add_finding("accepted", accepted)
    result.add_finding("accepted_rate", f"{accepted_rate:.2%}")
    result.add_finding("quarantine_rate", f"{quarantine_rate:.2%}")
    result.add_finding("duplicate_rate", f"{duplicate_rate:.2%}")
    result.add_finding("tree_linked_rate", f"{tree_linked / input_count:.2%}" if input_count else "N/A")

    if accepted_rate < 0.90:
        result.add_issue("ERROR", "ingest", f"acceptance rate {accepted_rate:.2%} < 0.90 (blocking)")
    elif accepted_rate < 0.92:
        result.add_issue("WARN", "ingest", f"acceptance rate {accepted_rate:.2%} near lower limit")

    total_rejected = rejected_short + rejected_category + rejected_excluded
    if accepted > 0:
        rejection_ratio = total_rejected / (total_rejected + accepted)
        result.add_finding("rejection_ratio", f"{rejection_ratio:.2%}")
        if rejection_ratio > 0.10:
            result.add_issue("WARN", "L0", f"rejection rate {rejection_ratio:.2%} > 10%, L0 rules may be overly conservative")
        elif rejection_ratio > 0.05:
            result.add_issue("INFO", "L0", f"rejection rate {rejection_ratio:.2%} > 5%, continue monitoring")

    if quarantine_rate > 0.03:
        result.add_issue("ERROR", "ingest", f"quarantine rate {quarantine_rate:.2%} > 3% (blocking)")
    elif quarantine_rate > 0.01:
        result.add_issue("WARN", "ingest", f"quarantine rate {quarantine_rate:.2%} in warning zone")

    return result


def diagnose_metadata_alignment() -> DiagnosticResult:
    """診斷3：元數據對齊檢查。"""
    result = DiagnosticResult("metadata_alignment")
    kb = load_knowledge_base()
    if not kb:
        result.add_issue("WARN", "metadata", "knowledge_base.json not found")
        return result

    doc_cat_unknown = 0
    doc_cat_total = 0
    product_module_unknown = 0
    product_module_total = 0
    docx_fields = Counter()
    pdf_fields = Counter()

    for chunk in kb:
        meta = chunk.get("metadata", {})

        doc_cat = meta.get("document_category")
        if doc_cat:
            doc_cat_total += 1
            if doc_cat == "unknown":
                doc_cat_unknown += 1

        pm = meta.get("product_module")
        if pm:
            product_module_total += 1
            if pm == "unknown" or not pm:
                product_module_unknown += 1

        source_file = meta.get("source_file", "")
        field_set = frozenset(meta.keys())
        if source_file.endswith(".docx"):
            docx_fields[field_set] += 1
        elif source_file.endswith(".pdf"):
            pdf_fields[field_set] += 1

    doc_cat_unknown_rate = doc_cat_unknown / doc_cat_total if doc_cat_total else 0
    pm_unknown_rate = product_module_unknown / product_module_total if product_module_total else 0

    result.add_finding("document_category_unknown_rate", f"{doc_cat_unknown_rate:.2%}")
    result.add_finding("product_module_unknown_rate", f"{pm_unknown_rate:.2%}")

    if doc_cat_unknown_rate > 0.02:
        result.add_issue("WARN", "L3_category", f"document_category unknown {doc_cat_unknown_rate:.2%} > 2%")

    if pm_unknown_rate > 0.05:
        result.add_issue("WARN", "L3_schema", f"product_module unknown {pm_unknown_rate:.2%} > 5%")

    if docx_fields and pdf_fields:
        docx_top = docx_fields.most_common(1)[0][0] if docx_fields else set()
        pdf_top = pdf_fields.most_common(1)[0][0] if pdf_fields else set()
        diff_ratio = len(set(docx_top) ^ set(pdf_top)) / max(len(set(docx_top)), len(set(pdf_top)), 1)
        result.add_finding("metadata_field_diff_ratio", f"{diff_ratio:.2%}")
        if diff_ratio > 0.30:
            result.add_issue("WARN", "parsing", f"docx vs pdf metadata field diff {diff_ratio:.2%} > 30%")

    return result


def diagnose_file_coverage() -> DiagnosticResult:
    """診斷4：關鍵檔案覆蓋。"""
    result = DiagnosticResult("file_coverage")
    kb = load_knowledge_base()
    if not kb:
        return result

    file_blocks = Counter()
    for chunk in kb:
        source = chunk.get("metadata", {}).get("source_file", "unknown")
        file_blocks[source] += 1

    expected_files = {
        "cli_1-82.json": (1700, 2000),
        "app_1-40.json": (500, 700),
        "app_65-72.json": (80, 120),
    }

    result.add_finding("total_documents", len(file_blocks))
    result.add_finding("total_blocks", len(kb))

    for fname, (min_blocks, max_blocks) in expected_files.items():
        actual = file_blocks.get(fname, 0)
        result.add_finding(f"{fname}_blocks", actual)
        if actual == 0:
            result.add_issue("WARN", "coverage", f"{fname} has no blocks")
        elif actual < min_blocks or actual > max_blocks:
            result.add_issue("INFO", "coverage", f"{fname} blocks {actual} out of range [{min_blocks}, {max_blocks}]")

    return result


def print_diagnostic_report(results: List[DiagnosticResult]) -> None:
    print("\n" + "=" * 80)
    print("PROCUREMENT QUALITY DIAGNOSTIC REPORT")
    print("=" * 80 + "\n")

    for result in results:
        print(f"[{result.run_id}] @ {result.timestamp or 'N/A'}")
        print("-" * 60)

        for key, value in result.findings.items():
            print(f"  {key:.<40} {value}")

        if result.issues:
            print("\n  Issues:")
            for level, category, msg in result.issues:
                prefix = {"ERROR": "❌", "WARN": "⚠️ ", "INFO": "ℹ️ ", "OBSERVE": "👁 "}.get(level, "•")
                print(f"    {prefix} [{category:.<15}] {msg}")
        else:
            print("  ✓ No issues detected")

        print()


def save_diagnostic_results(results: List[DiagnosticResult], output_path: Path) -> None:
    data = [r.to_dict() for r in results]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Diagnostic results saved: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Procurement Quality Diagnostic")
    parser.add_argument("--run-id", type=str, default=None, help="Specific run_id to diagnose")
    parser.add_argument("--latest", action="store_true", help="Use latest run_id")
    parser.add_argument("--output", type=str, default=None, help="Save results to JSON")

    args = parser.parse_args()

    run_id = args.run_id
    if args.latest:
        run_id = find_latest_run_id()
        if not run_id:
            print("❌ No runs found")
            sys.exit(1)
        print(f"Using latest run_id: {run_id}")

    if not run_id:
        print("❌ Please provide --run-id or use --latest")
        sys.exit(1)

    results = [
        diagnose_baseline(run_id),
        diagnose_ingest_report(),
        diagnose_metadata_alignment(),
        diagnose_file_coverage(),
    ]

    print_diagnostic_report(results)

    if args.output:
        save_diagnostic_results(results, Path(args.output))
    else:
        output_dir = TEST_DATA_ROOT / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "diagnostic_report.json"
        save_diagnostic_results(results, output_path)

    all_issues = []
    for r in results:
        all_issues.extend(r.issues)

    errors = [i for i in all_issues if i[0] == "ERROR"]
    warns = [i for i in all_issues if i[0] == "WARN"]

    print("=" * 80)
    print("SUMMARY")
    print("-" * 80)
    print(f"Total Issues: {len(all_issues)} (❌ {len(errors)} blockers, ⚠️  {len(warns)} warnings)")

    if errors:
        print("\n❌ BLOCKERS (need fixing):")
        for _, cat, msg in errors:
            print(f"   [{cat}] {msg}")

    if warns:
        print("\n⚠️  WARNINGS (can continue, needs monitoring):")
        for _, cat, msg in warns:
            print(f"   [{cat}] {msg}")

    print("\n" + "=" * 80)

    if errors:
        print("✗ Status: FAIL (blockers detected, rerun procurement process)")
        sys.exit(1)
    elif warns:
        print("△ Status: WARN (warnings only, can proceed to test data review)")
        sys.exit(0)
    else:
        print("✓ Status: PASS (no issues, can proceed to test data review)")
        sys.exit(0)


if __name__ == "__main__":
    main()
