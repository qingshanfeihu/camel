from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple


def _stringify(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _load_reference_snapshot(
    snapshot_dir: Path,
) -> Tuple[Dict[Tuple[str, str], dict], Dict[Tuple[str, int], dict]]:
    block_index: Dict[Tuple[str, str], dict] = {}
    ordinal_index: Dict[Tuple[str, int], dict] = {}
    for json_file in sorted(snapshot_dir.glob("*.json")):
        if json_file.name == "knowledge_base.json":
            continue
        data = json.loads(json_file.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        for ordinal, entry in enumerate(data):
            metadata = entry.get("metadata") or {}
            source_file = _stringify(metadata.get("source_file") or json_file.name)
            block_id = _stringify(metadata.get("block_id"))
            block_index[(source_file, block_id)] = entry
            ordinal_index[(source_file, ordinal)] = entry
    return block_index, ordinal_index


def _load_gate_records(gate_path: Path) -> List[dict]:
    records: List[dict] = []
    with gate_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _build_record(
    gate_record: dict,
    block_index: Dict[Tuple[str, str], dict],
    ordinal_index: Dict[Tuple[str, int], dict],
) -> dict:
    source_file = _stringify(gate_record.get("source_file"))
    block_id = _stringify(gate_record.get("block_id"))
    chunk_index = int(gate_record.get("chunk_index") or 0)
    snapshot = block_index.get((source_file, block_id))
    if snapshot is None:
        snapshot = ordinal_index.get((source_file, chunk_index), {})
    metadata = snapshot.get("metadata") or {}
    page_content = snapshot.get("page_content") or snapshot.get("text") or ""
    return {
        "gate": gate_record,
        "page_content": page_content,
        "metadata": metadata,
    }


def _render_section(lines: List[str], title: str, records: List[dict]) -> None:
    lines.append(f"## {title}")
    lines.append("")
    lines.append(f"共 {len(records)} 条")
    lines.append("")
    for idx, record in enumerate(records, start=1):
        gate = record["gate"]
        metadata = record["metadata"]
        page_content = str(record["page_content"] or "")
        lines.append(f"### {idx}. {gate.get('source_file', '')} / block_id={gate.get('block_id', '')}")
        lines.append("")
        lines.append("质量门控记录:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(gate, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("原始内容:")
        lines.append("")
        lines.append("```")
        lines.append(page_content)
        lines.append("```")
        lines.append("")
        lines.append("详细 metadata:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(metadata, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")


def export_report(run_dir: Path, output_path: Path) -> None:
    gate_path = run_dir / "_quality_gate_for_owner.jsonl"
    snapshot_dir = run_dir / "reference_snapshot"

    if not gate_path.exists():
        raise FileNotFoundError(f"Missing gate file: {gate_path}")
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Missing reference snapshot dir: {snapshot_dir}")

    block_index, ordinal_index = _load_reference_snapshot(snapshot_dir)
    gate_records = _load_gate_records(gate_path)

    blocked: List[dict] = []
    short_rejected: List[dict] = []
    duplicates: List[dict] = []

    for gate_record in gate_records:
        reason = str(gate_record.get("quality_reason") or "")
        if reason == "passed":
            continue
        record = _build_record(gate_record, block_index, ordinal_index)
        blocked.append(record)
        if reason in {"short_content", "heading_only"}:
            short_rejected.append(record)
        if reason == "simhash_near_duplicate":
            duplicates.append(record)

    lines: List[str] = []
    lines.append("# 质检阻断详情")
    lines.append("")
    lines.append(f"来源目录: {run_dir}")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 总阻断: {len(blocked)}")
    lines.append(f"- 短文本拒绝: {len(short_rejected)}")
    lines.append(f"- 重复: {len(duplicates)}")
    lines.append("")

    _render_section(lines, "阻断内容", blocked)
    _render_section(lines, "短文本拒绝", short_rejected)
    _render_section(lines, "重复", duplicates)

    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export blocked quality items to Markdown.")
    parser.add_argument("--run-dir", required=True, help="Quality run directory path")
    parser.add_argument(
        "--output",
        help="Output markdown path. Defaults to <run-dir>/质检阻断详情.md",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    output_path = Path(args.output).resolve() if args.output else run_dir / "质检阻断详情.md"
    export_report(run_dir, output_path)
    print(output_path)


if __name__ == "__main__":
    main()