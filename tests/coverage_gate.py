#!/usr/bin/env python3
"""Fail the build if any scripts/*.py module is under its coverage bar.

Reads the JSON report ``pytest --cov-report=json`` writes (TESTING.md section
3: core modules at 95% or more, every other module at 90% or more, measured
per module and never as an average) and prints one aligned table. A module
present on disk but absent from the report is treated as 0%, so a module
nobody imports cannot pass by being invisible to coverage.

    coverage_gate.py [path-to-coverage.json]   # default: <repo>/coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

CORE = {
    "chatgpt_client.py",
    "chatgpt_session.py",
    "chatgpt_cookies.py",
    "_common.py",
    "round_state.py",
}
CORE_BAR = 95.0
OTHER_BAR = 90.0

Row = tuple[str, int, float, float, str]


def bar_for(module: str) -> float:
    """The line-coverage bar ``module`` must clear (TESTING.md section 3)."""
    return CORE_BAR if module in CORE else OTHER_BAR


def verdicts(report: dict, modules_on_disk: list[str]) -> list[Row]:
    """One row per module on disk: statements, percent, its bar, OK/LOW.

    Iterating ``modules_on_disk`` rather than ``report["files"]`` is the
    point: a module coverage never measured gets a 0% row instead of being
    skipped, so it cannot pass by absence.
    """
    files = report.get("files", {})
    rows: list[Row] = []
    for module in sorted(modules_on_disk):
        bar = bar_for(module)
        entry = files.get(f"scripts/{module}")
        summary = entry.get("summary", {}) if entry else {}
        statements = summary.get("num_statements", 0)
        percent = summary.get("percent_covered", 0.0)
        status = "OK" if percent >= bar else "LOW"
        rows.append((module, statements, percent, bar, status))
    return rows


def render(rows: list[Row]) -> str:
    """A plain aligned table: module, statements, percent, bar, OK/LOW."""
    header = ("module", "statements", "percent", "bar", "")
    body = [
        (m, str(s), f"{p:.1f}%", f"{b:.1f}%", status) for m, s, p, b, status in rows
    ]
    widths = [
        max(len(header[i]), *(len(r[i]) for r in body)) if body else len(header[i])
        for i in range(len(header))
    ]
    lines = ["  ".join(c.ljust(w) for c, w in zip(header, widths, strict=True))]
    lines += [
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in body
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    report_path = Path(argv[0]) if argv else REPO_ROOT / "coverage.json"
    if not report_path.is_file():
        print(f"coverage_gate: no report at {report_path}; run `make test` first")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    modules_on_disk = [p.name for p in SCRIPTS.glob("*.py")]
    rows = verdicts(report, modules_on_disk)
    print(render(rows))
    return 1 if any(status == "LOW" for *_rest, status in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
