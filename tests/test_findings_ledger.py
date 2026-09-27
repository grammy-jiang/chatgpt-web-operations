"""Consistency: every finding is closed by a test that exists and an atlas
entry that names it, and every recent atlas entry has a ledger id.

``tests/findings.json`` is the ledger (TESTING.md 6.4, the first rule): one
record per finding with the tests that would fail without its fix and the
phrase that finds its entry in ``references/failure-atlas.md``. This file
makes the rule mechanical:

* each ``tests`` reference resolves to a test function that exists;
* the atlas contains each entry's ``atlas`` phrase and its id;
* every "Fixed bugs" bullet in the atlas dated 2026-09-27 or later carries
  a ledger id, so a new finding cannot be written up without being listed;
* ids are unique and well formed.

"Fails with the fix removed" is recorded as ``removed_and_failed`` (the date
it was checked by hand); making that part mechanical is TESTING.md P10, the
mutation pilot.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
LEDGER = TESTS_DIR / "findings.json"
ATLAS = TESTS_DIR.parent / "references" / "failure-atlas.md"
LEDGER_ID = re.compile(r"^F-\d{4}-\d\d-\d\d-\d+$")
ID_IN_TEXT = re.compile(r"\(F-\d{4}-\d\d-\d\d-\d+\)")
DATE = re.compile(r"\b(2026-\d\d-\d\d)\b")
IDS_REQUIRED_FROM = "2026-09-27"


def ledger() -> list[dict]:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def functions_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def fixed_bugs_bullets() -> list[str]:
    """The bullets of the atlas section "Fixed bugs worth not re-introducing",
    each as one string (continuation lines joined)."""
    text = ATLAS.read_text(encoding="utf-8")
    start = text.index("## Fixed bugs worth not re-introducing")
    section = text[start:]
    bullets: list[str] = []
    for line in section.splitlines()[1:]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            bullets.append(line[2:])
        elif line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    return bullets


def test_the_ledger_has_entries_with_unique_well_formed_ids() -> None:
    entries = ledger()
    assert len(entries) >= 6
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids)), ids
    for entry in entries:
        assert LEDGER_ID.match(entry["id"]), entry["id"]
        for key in ("title", "atlas", "fix", "tests", "removed_and_failed"):
            assert key in entry, (entry["id"], key)
        assert entry["tests"], entry["id"]


@pytest.mark.parametrize("entry", ledger(), ids=lambda e: e["id"])
def test_every_ledger_test_reference_resolves(entry: dict) -> None:
    for reference in entry["tests"]:
        file_part, function = reference.split("::", 1)
        path = TESTS_DIR / file_part
        assert path.is_file(), f"{entry['id']}: {file_part} does not exist"
        assert function in functions_in(path), (
            f"{entry['id']}: {file_part} defines no {function}"
        )


@pytest.mark.parametrize("entry", ledger(), ids=lambda e: e["id"])
def test_every_ledger_entry_is_in_the_atlas_by_id_and_phrase(entry: dict) -> None:
    text = ATLAS.read_text(encoding="utf-8")
    assert f"({entry['id']})" in text, f"{entry['id']} is not in the atlas"
    assert entry["atlas"] in text, f"{entry['id']}: phrase {entry['atlas']!r} not found"


def test_every_recent_fixed_bug_in_the_atlas_carries_a_ledger_id() -> None:
    """A finding written up after the rule exists must be in the ledger."""
    known = {entry["id"] for entry in ledger()}
    offenders = []
    for bullet in fixed_bugs_bullets():
        dates = DATE.findall(bullet)
        if not dates or max(dates) < IDS_REQUIRED_FROM:
            continue
        found = ID_IN_TEXT.findall(bullet)
        if not found:
            offenders.append(bullet[:90])
            continue
        for match in found:
            assert match[1:-1] in known, f"{match} is in the atlas but not the ledger"
    assert offenders == [], offenders
