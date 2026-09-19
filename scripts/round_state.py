"""What a research round admitted, read, and wrote off, from its run directory.

The helpers ``round_status.py`` needs, copied verbatim from the research-pipeline
orchestrator (``.github/scripts/chatgpt_research.py`` at 00b5e1a0: ``base_id``,
``read_jsonl``, ``paper_id_of``, ``ANALYSIS_SUFFIX``, ``admitted_ids``,
``analysed_ids``, ``skipped_papers``, ``accounted_ids``) so this skill does not
import that checkout. They define what "accounted for"
means; keep them in step with the orchestrator, or the gate here passes rounds
the run itself would reject. See VENDORED.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def base_id(pid: str) -> str:
    return re.sub(r"v\d+$", "", pid.strip())


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def paper_id_of(row: dict) -> str:
    return base_id(
        str(row.get("arxiv_id") or row.get("paper_id") or row.get("id") or "")
    )


ANALYSIS_SUFFIX = "_analysis.json"


def admitted_ids(run_dir: Path) -> set[str]:
    """Papers this round admitted, so must read."""
    return {
        pid
        for r in read_jsonl(run_dir / "screen" / "screened.jsonl")
        if (pid := paper_id_of(r))
    }


def analysed_ids(run_dir: Path) -> set[str]:
    """Papers this round has an analysis for, carried ones included."""
    return {
        f.name[: -len(ANALYSIS_SUFFIX)]
        for f in (run_dir / "analysis").glob(f"*{ANALYSIS_SUFFIX}")
    }


def skipped_papers(run_dir: Path) -> dict[str, str]:
    """Papers a worker declared unreadable, mapped to the reason it gave.

    ``analysis/skipped.json`` is the system's own way of saying "this one
    cannot be read", and ``make_shards.py`` honours it, so a skipped paper is
    never re-sharded. A completeness check that ignores it demands an analysis
    that will never arrive and blocks the round for ever.
    """
    path = run_dir / "analysis" / "skipped.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        str(item["paper_id"]): str(item.get("reason") or "no reason given")
        for item in (data if isinstance(data, list) else [])
        if isinstance(item, dict) and item.get("paper_id")
    }


def accounted_ids(run_dir: Path) -> set[str]:
    """Papers that are read or explicitly written off, so nothing is pending.

    Distinct from ``analysed_ids``: a skipped paper is accounted for but is
    not evidence, so it settles the completeness check and must never count
    towards the new papers a round contributed.
    """
    return analysed_ids(run_dir) | set(skipped_papers(run_dir))
