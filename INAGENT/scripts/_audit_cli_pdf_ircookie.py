"""Scan INAGENT/knowledge_base/input/cli.pdf for ircookie-related text (ground truth vs MinerU).

Also scans MinerU cli_content_list.json: plain ``text`` blocks vs ``table_body`` HTML
(where IC/slb group method cross-refs often live).

Writes UTF-8 report to knowledge_base/logs/cli_pdf_ircookie_audit.txt
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from pypdf import PdfReader

_INAGENT = Path(__file__).resolve().parent.parent
PDF_PATH = _INAGENT / "knowledge_base" / "input" / "cli.pdf"
MINERU_CLI = (
    _INAGENT / "knowledge_base" / "mineru_output" / "cli" / "hybrid_auto" / "cli_content_list.json"
)
LOG_PATH = _INAGENT / "knowledge_base" / "logs" / "cli_pdf_ircookie_audit.txt"


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _mineru_ircookie_stats(path: Path) -> tuple[str, list[str]]:
    if not path.exists():
        return ("MinerU file missing: " + str(path), [])

    data = json.loads(path.read_text(encoding="utf-8"))
    by_key: dict[str, int] = defaultdict(int)
    text_block_indices: list[int] = []
    sample_lines: list[str] = []

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if not isinstance(v, str):
                continue
            if "ircookie" not in v.lower():
                continue
            by_key[k] += 1
            if k == "text":
                text_block_indices.append(i)
                if len(sample_lines) < 6:
                    sample_lines.append(f"  index {i} text: {v.strip()[:120]!r}")

    lines = [
        "",
        "## MinerU cli_content_list.json (ircookie substring)",
        f"# path: {path}",
        f"# total array length: {len(data)}",
        "# occurrences by JSON key on blocks that contain 'ircookie':",
    ]
    for k in sorted(by_key.keys(), key=lambda x: -by_key[x]):
        lines.append(f"  {k}: {by_key[k]} blocks")
    lines.append(f"# blocks with type key=='text' containing ircookie: {len(text_block_indices)}")
    if text_block_indices:
        lines.append(f"  indices: {text_block_indices}")
    lines.extend(sample_lines)
    if by_key.get("table_body", 0) and not sample_lines:
        lines.append("  (ircookie only inside table_body / other keys — plain text chunks miss IC cross-refs)")
    return ("\n".join(lines), sample_lines)


def main() -> None:
    reader = PdfReader(str(PDF_PATH))
    n_pages = len(reader.pages)

    hits: list[tuple[int, str, str]] = []
    pages_with_ircookie: set[int] = set()

    for pi, page in enumerate(reader.pages):
        pnum = pi + 1
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover
            hits.append((pnum, "EXTRACT_ERROR", str(exc)))
            continue
        text = norm(raw)
        tl = text.lower()
        if "ircookie" in tl:
            pages_with_ircookie.add(pnum)
            for m in re.finditer(r".{0,50}ircookie.{0,160}", text, flags=re.IGNORECASE | re.DOTALL):
                s = " ".join(m.group(0).split())
                hits.append((pnum, "ircookie", s[:280]))
        if "irookie" in tl:
            hits.append((pnum, "typo_irookie", "substring irookie present"))

    # Heuristic: IC section referencing ircookie mode without spelling "ircookie"
    ic_ref_pages: list[tuple[int, str]] = []
    for pi, page in enumerate(reader.pages):
        pnum = pi + 1
        text = norm(page.extract_text() or "")
        tl = text.lower()
        if "ircookie" in tl:
            continue
        if "slb mode" in tl and "cookie" in tl:
            if "group method" in tl and " ic" in f" {tl} ":
                ic_ref_pages.append((pnum, text[:800]))

    lines: list[str] = []
    lines.append(f"# cli.pdf ircookie audit")
    lines.append(f"# file: {PDF_PATH}")
    lines.append(f"# pages: {n_pages}")
    lines.append(
        f"# pages containing substring 'ircookie' (any case, NFKC): "
        f"{len(pages_with_ircookie)} -> {sorted(pages_with_ircookie)}"
    )
    lines.append(f"# hit snippets recorded: {len([h for h in hits if h[1] == 'ircookie'])}")
    lines.append("")
    for h in hits:
        lines.append(f"p{h[0]:4d} [{h[1]:14s}] {h[2]}")
    lines.append("")
    lines.append("## Pages: slb mode + cookie + group method ic, but NO substring 'ircookie'")
    lines.append("(MinerU may still carry this text in other blocks; PDF layer check only.)")
    lines.append("")
    for pnum, snippet in ic_ref_pages[:30]:
        one = " ".join(snippet.split())
        lines.append(f"p{pnum}: {one[:500]}")

    mineru_section, _ = _mineru_ircookie_stats(MINERU_CLI)
    lines.append(mineru_section)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {LOG_PATH} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
