"""Separate reader-facing research deliverables from machine audit state."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


STATUS_RE = re.compile(
    r"(?:<!--\s*PAPERWIKI_STATUS:\s*(DONE|CONTINUE)\s*-->|\[BENCHMARK_(DONE|CONTINUE)\])",
    flags=re.I,
)


@dataclass(frozen=True)
class DeliverableBundle:
    report_path: Path
    evidence_path: Path
    audit_path: Path
    readme_path: Path
    declared_status: str


def declared_status(answer: str) -> str:
    matches = list(STATUS_RE.finditer(str(answer or "")))
    if not matches:
        return "unknown"
    value = next(group for group in matches[-1].groups() if group)
    return "done" if value.upper() == "DONE" else "continue"


def reader_report(answer: str, *, title: str = "Research Report") -> str:
    """Remove control-plane markers; audit details belong in run_audit.json."""
    lines = []
    for line in str(answer or "").splitlines():
        if STATUS_RE.search(line):
            continue
        if re.match(r"^#\s*\u7b2c\s*\d+\s*\u6267\u884c\u5468\u671f", line.strip()):
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines).strip()
    if not re.match(r"^#\s+", text):
        text = f"# {title}\n\n{text}"
    return text.rstrip() + "\n"


def write_deliverable_bundle(
    output_dir: str | Path,
    *,
    answer: str,
    title: str,
    task_key: str,
    citations: Iterable[dict[str, Any]] = (),
    audit: dict[str, Any] | None = None,
) -> DeliverableBundle:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.md"
    evidence_path = root / "evidence_appendix.md"
    audit_path = root / "run_audit.json"
    readme_path = root / "README.md"
    status = declared_status(answer)
    report_path.write_text(reader_report(answer, title=title), encoding="utf-8")

    citation_rows = [item for item in citations if isinstance(item, dict)]
    evidence_lines = [f"# {title} · Evidence Appendix", ""]
    if citation_rows:
        evidence_lines.extend([
            "| # | Source | Type | Card ID | Local Markdown |",
            "|---:|---|---|---|---|",
        ])
        for index, item in enumerate(citation_rows, start=1):
            evidence_lines.append(
                "| {index} | {title} | {page_type} | `{card_id}` | `{path}` |".format(
                    index=index,
                    title=str(item.get("title") or "Untitled").replace("|", "\\|"),
                    page_type=str(item.get("page_type") or ""),
                    card_id=str(item.get("card_id") or ""),
                    path=str(item.get("markdown_path") or "").replace("|", "\\|"),
                )
            )
    else:
        evidence_lines.append("No resolved Wiki citations were attached to this run.")
    evidence_lines.extend([
        "",
        "> Runtime cycles, tool calls, budgets, failures, and completion gates are stored in `run_audit.json`, not in the reader report.",
        "",
    ])
    evidence_path.write_text("\n".join(evidence_lines), encoding="utf-8")

    payload = dict(audit or {})
    payload.update({
        "task_key": task_key,
        "title": title,
        "declared_status": status,
        "report_path": str(report_path),
        "evidence_appendix_path": str(evidence_path),
    })
    audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme_path.write_text(
        f"# {title}\n\n"
        "Start with [report.md](report.md). It is the reader-facing deliverable.\n\n"
        "Use [evidence_appendix.md](evidence_appendix.md) for source traceability. "
        "`run_audit.json` is machine-facing execution state and is intentionally kept out of the report.\n",
        encoding="utf-8",
    )
    return DeliverableBundle(report_path, evidence_path, audit_path, readme_path, status)
