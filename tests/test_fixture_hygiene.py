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

    Covers the recorded payloads in tests/fixtures/ and the recorded API
    shapes in tests/fixtures/http/ (TESTING.md section 6, P5).
    """
    offenders = {
        str(path.relative_to(FIXTURES)): problems
        for path in sorted([*FIXTURES.glob("*.json"), *FIXTURES.glob("http/*.json")])
        if (problems := leaks_in(path.read_text(encoding="utf-8")))
    }
    assert not offenders, offenders


# --- DOM snapshots (tests/fixtures/dom, TESTING.md section 6 P1) -----------
#
# A recorded page keeps attributes only (chatgpt_client.sanitize_html): no
# text node, no URL, no script. The markup rule for ids is narrower than the
# JSON one on purpose: ``data-user-message-bubble`` is a ChatGPT attribute,
# not an id, so only ``user-``/``org-`` followed by an id-length token counts.

DOM_FIXTURES = FIXTURES / "dom"
_BAD_ID_IN_MARKUP = re.compile(r"(?:user|org)-[A-Za-z0-9]{12,}")
_TEXT_NODE = re.compile(r">\s*[^<\s][^<]*<")
_URL_IN_MARKUP = re.compile(r"""(?:href|src|srcset)\s*=|https?://""", re.IGNORECASE)
_SCRIPT_IN_MARKUP = re.compile(r"<script\b", re.IGNORECASE)


def leaks_in_markup(text: str, *, synthetic: bool = False) -> list[str]:
    """Every reason recorded markup must not be committed. A hand-written
    (``synthetic``) fixture may carry text, because the notice fixture's
    text is its whole point; a recorded one may not."""
    problems = []
    if _BAD_AT.search(text):
        problems.append("an '@' outside 'user@example.invalid'")
    if _BAD_ID_IN_MARKUP.search(text):
        problems.append("a 'user-' or 'org-' id")
    if any(not m.group(1).isdigit() for m in _GP_ID.finditer(text)):
        problems.append("a g-p- id whose 32 characters are not our digit placeholder")
    if _GRAMMY.search(text):
        problems.append("the account holder's name")
    if _URL_IN_MARKUP.search(text):
        problems.append("a URL or an href/src attribute")
    if _SCRIPT_IN_MARKUP.search(text):
        problems.append("a script element")
    if not synthetic and _TEXT_NODE.search(text):
        problems.append("a text node (recorded markup keeps attributes only)")
    return problems


def leaks_in_facts(text: str) -> list[str]:
    """The rules for the JSON facts beside a recorded page: ``leaks_in`` with
    the markup id rule, because the facts quote selectors such as
    ``[data-user-message-bubble="true"]``."""
    problems = [p for p in leaks_in(text) if not p.startswith("a 'user-' or 'org-'")]
    if _BAD_ID_IN_MARKUP.search(text):
        problems.append("a 'user-' or 'org-' id")
    return problems


def test_every_dom_fixture_is_attributes_only_and_scrubbed() -> None:
    """Recorded pages carry no text, URL, script or identity; the JSON facts
    beside them carry no identity either (the URL is the one field that may
    name a page, and it is a chatgpt.com path with placeholder ids)."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(DOM_FIXTURES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        synthetic = path.stem.endswith("-synthetic")
        if problems := leaks_in_markup(text, synthetic=synthetic):
            offenders[path.name] = problems
    for path in sorted(DOM_FIXTURES.glob("*.json")):
        if problems := leaks_in_facts(path.read_text(encoding="utf-8")):
            offenders[path.name] = problems
    assert not offenders, offenders


def test_the_markup_rule_catches_text_urls_scripts_and_ids() -> None:
    assert leaks_in_markup('<div class="x"></div>') == []
    assert "a text node" in " ".join(leaks_in_markup("<div>hello</div>"))
    assert leaks_in_markup("<div>hello</div>", synthetic=True) == []
    assert "URL" in " ".join(leaks_in_markup('<a href="https://x.y"></a>'))
    assert "script" in " ".join(leaks_in_markup("<script></script>"))
    assert "id" in " ".join(leaks_in_markup('<div data-x="user-abcdefghijklmnop">'))
    assert leaks_in_markup('<div data-user-message-bubble="true"></div>') == []
    assert (
        leaks_in_facts('{"matched_user": ["[data-user-message-bubble=\\"true\\"]"]}')
        == []
    )
    assert leaks_in_facts('{"x": "user-abcdefghijklmnop"}') == [
        "a 'user-' or 'org-' id"
    ]
    assert "name" in " ".join(leaks_in_facts('{"x": "Grammy"}'))
