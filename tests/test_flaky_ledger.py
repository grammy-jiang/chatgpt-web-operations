"""T0 tests for the flaky-test rules (TESTING.md section 6, P9): the
quarantine marker in tests/conftest.py and the ledger in tests/FLAKY.md."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import conftest
import pytest

TESTS = Path(__file__).resolve().parent
FLAKY = TESTS / "FLAKY.md"
ROW = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \| (.+?) \|.*\| ([^|]+) \|$")
QUARANTINE_USE = re.compile(r"@pytest\.mark\.quarantine\(.*?\)\s*\n(?:@.*\n)*def (\w+)")


class _Marker:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Item:
    def __init__(self, marker: _Marker | None) -> None:
        self.marker = marker
        self.added: list[Any] = []

    def get_closest_marker(self, name: str) -> _Marker | None:
        return self.marker if name == "quarantine" else None

    def add_marker(self, marker: Any) -> None:
        self.added.append(marker)


DAY = date(2026, 9, 29)


@pytest.mark.parametrize(
    ("until", "expected"),
    [
        ("2026-09-30", "active"),
        ("2026-09-29", "active"),  # the last day counts
        ("2026-09-28", "expired"),
        ("next week", "invalid"),
        ("", "invalid"),
    ],
)
def test_quarantine_state(until: str, expected: str) -> None:
    assert conftest.quarantine_state(until, DAY) == expected


def test_an_active_quarantine_runs_as_a_non_strict_xfail() -> None:
    item = _Item(_Marker(until="2026-10-01", reason="races the clock"))
    assert conftest.apply_quarantine(item, DAY) is None
    (marker,) = item.added
    assert marker.name == "xfail"
    assert "races the clock" in marker.kwargs["reason"]
    assert not marker.kwargs.get("strict", False)


@pytest.mark.parametrize(
    ("marker", "words"),
    [
        (_Marker(until="2026-09-01", reason="old"), "ended on 2026-09-01"),
        (_Marker(until="soon", reason="x"), "not an ISO date"),
        (_Marker(until="2026-10-01"), "needs a reason"),
    ],
)
def test_an_expired_or_malformed_quarantine_is_a_failure(marker, words) -> None:
    item = _Item(marker)
    problem = conftest.apply_quarantine(item, DAY)
    assert problem and words in problem
    assert item.added == []


def test_an_unmarked_test_is_left_alone() -> None:
    item = _Item(None)
    assert conftest.apply_quarantine(item, DAY) is None
    assert item.added == []


def _rows() -> list[tuple[date, str, str]]:
    rows = []
    for line in FLAKY.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match:
            seen, test, deadline = match.groups()
            rows.append((date.fromisoformat(seen), test, deadline.strip()))
    return rows


def test_the_ledger_exists_and_every_open_row_has_a_one_week_deadline() -> None:
    assert FLAKY.is_file()
    for seen, test, deadline in _rows():
        if deadline.startswith("closed "):
            date.fromisoformat(deadline.removeprefix("closed "))
            continue
        due = date.fromisoformat(deadline)
        assert seen <= due <= seen + timedelta(days=7), (test, seen, due)


def test_every_quarantine_in_the_suite_has_a_ledger_row() -> None:
    ledger = FLAKY.read_text(encoding="utf-8")
    missing = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if path == Path(__file__):
            continue
        for name in QUARANTINE_USE.findall(path.read_text(encoding="utf-8")):
            if name not in ledger:
                missing.append(f"{path.relative_to(TESTS)}::{name}")
    assert missing == [], f"quarantined without a row in tests/FLAKY.md: {missing}"


def test_the_ledger_pattern_finds_a_quarantine() -> None:
    sample = (
        '@pytest.mark.quarantine(until="2026-10-01", reason="x")\n'
        "@pytest.mark.parametrize('a', [1])\n"
        "def test_sample(a):\n"
    )
    assert QUARANTINE_USE.findall(sample) == ["test_sample"]
