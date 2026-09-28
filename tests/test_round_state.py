"""T0 tests for round_state.skipped_papers, written for the two mutants the
P10 pilot found alive (TESTING.md section 6, P10; VERIFICATION.md)."""

from __future__ import annotations

import json
from pathlib import Path

import round_state


def _skipped(tmp_path: Path, data: object) -> dict[str, str]:
    path = tmp_path / "analysis" / "skipped.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return round_state.skipped_papers(tmp_path)


def test_a_skipped_paper_without_a_reason_gets_the_default_one(tmp_path) -> None:
    got = _skipped(
        tmp_path,
        [{"paper_id": "2401.00001", "reason": ""}, {"paper_id": "2401.00002"}],
    )
    assert got == {"2401.00001": "no reason given", "2401.00002": "no reason given"}


def test_malformed_entries_are_ignored_not_fatal(tmp_path) -> None:
    got = _skipped(
        tmp_path,
        [
            "2401.00003",  # not an object
            {"reason": "no id"},  # no paper_id
            {"paper_id": "", "reason": "empty id"},
            {"paper_id": "2401.00004", "reason": "scanned PDF"},
        ],
    )
    assert got == {"2401.00004": "scanned PDF"}
