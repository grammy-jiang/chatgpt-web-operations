"""Nothing sanitize() is meant to strip may reach tests/fixtures/ on disk.

This reads the fixture files' raw text, independent of record_fixture.py's
own sanitize(), so a leak from a hand-edited fixture or a future bug in the
sanitizer is still caught (TESTING.md section 1: "a T0 test scans
tests/fixtures/ for '@', 'user-', 'org-', any 'g-p-' id outside an
allowlist, and the account's email, and fails on a hit").
"""

from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_BAD_AT = re.compile(r"@(?!example\.invalid)")
_BAD_USER_OR_ORG_ID = re.compile(r"(?:user|org)-(?!XXXXXXXX)")
_GP_ID = re.compile(r"g-p-([0-9a-fA-F]{32})(?![0-9a-fA-F])")
_GRAMMY = re.compile(r"grammy", re.IGNORECASE)


def leaks_in(text: str) -> list[str]:
    """Every reason ``text`` must not be written to a fixture, if any."""
    problems = []
    if _BAD_AT.search(text):
        problems.append("an '@' outside 'user@example.invalid'")
    if _BAD_USER_OR_ORG_ID.search(text):
        problems.append("a 'user-' or 'org-' id not replaced with XXXXXXXX")
    if any(not m.group(1).isdigit() for m in _GP_ID.finditer(text)):
        problems.append("a g-p- id whose 32 characters are not our digit placeholder")
    if _GRAMMY.search(text):
        problems.append("the account holder's name")
    return problems


def test_every_fixture_is_scrubbed_of_the_account_s_identity() -> None:
    """A fixture recorded from the real account must never carry it to disk.

    Passes trivially when tests/fixtures/ has no JSON files yet.
    """
    offenders = {
        path.name: problems
        for path in sorted(FIXTURES.glob("*.json"))
        if (problems := leaks_in(path.read_text(encoding="utf-8")))
    }
    assert not offenders, offenders
