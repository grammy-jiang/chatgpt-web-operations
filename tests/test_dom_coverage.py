"""Consistency: everything the client asks Playwright to find on the page is
a named constant, and every such constant is exercised by tier R against a
recorded page, or excused here by name with a reason.

Why (TESTING.md section 6.4, after 2026-09-27): two DOM-reading scripts in
the client disagreed about the same send button for a day, and the upload
chip's label changed under a check nobody ran, because a selector could
live as a bare string in one method, proven by nothing but a fake page. A
named constant can be listed; a listed constant can be required to appear
in ``tests/replay``, where a real Chrome runs it against the DOM ChatGPT
served; and a constant that cannot be proven that way has to say so.

The rule is textual on purpose: a replay test that names the constant is
the evidence, and the excuse table is the only other exit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
CLIENT = SKILL_DIR / "scripts" / "chatgpt_client.py"
REPLAY_DIR = SKILL_DIR / "tests" / "replay"

PAGE_CONSTANT_RE = re.compile(r".*_(SELECTOR|SELECTORS|JS|RE)$")
CLASS_CONSTANTS = {"SEND_BUTTONS", "RATE_LIMIT_MODAL"}
# Playwright page methods whose first argument (or ``name=``) selects
# something on the page. ``get_by_role``'s first argument is the role, a
# vocabulary word, not a selector; its ``name=`` is checked instead.
LOCATOR_METHODS = {
    "locator",
    "query_selector",
    "query_selector_all",
    "wait_for_selector",
    "get_by_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_test_id",
    "get_by_title",
}

# Named like page constants, but they parse reply text, not the page. Listed
# here so the rule stays exact: a new *_RE that reads the page must be proven
# or excused, and a text pattern must be named here to be left out.
NOT_PAGE_FACING = {
    "BLOCK_RE": "parses a reply's fenced blocks, never the page",
    "FENCE_RE": "parses a reply's code fences, never the page",
}

EXCUSED = {
    "UPLOAD_BUSY_SELECTOR": (
        "a transient state while an upload runs; no recording holds it"
    ),
    "DIALOG_SELECTOR": (
        "no dialog has been recorded: the announcement and rate-limit dialogs "
        "appear on the account's own schedule"
    ),
    "RATE_LIMIT_MODAL": "the rate-limit notice has not been recorded either",
    "CHAT_SURFACE_RADIO_RE": (
        "the Chat/Work toggle is absent on every recorded page, and a send "
        "leaves it alone when absent"
    ),
}


def _tree() -> ast.Module:
    return ast.parse(CLIENT.read_text(encoding="utf-8"))


def page_constants() -> list[str]:
    """Module-level names ending in _SELECTOR(S)/_JS/_RE, plus the sender's
    class-level selector attributes."""
    names: list[str] = []
    for node in _tree().body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and PAGE_CONSTANT_RE.match(target.id)
                    and target.id not in NOT_PAGE_FACING
                ):
                    names.append(target.id)
        if isinstance(node, ast.ClassDef) and node.name == "BrowserSender":
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and (
                            target.id in CLASS_CONSTANTS
                            or PAGE_CONSTANT_RE.match(target.id)
                        ):
                            names.append(target.id)
    return sorted(set(names))


def bare_locator_literals() -> list[str]:
    """``<line>: <method>(<literal>)`` for every locator call in the client
    whose selector is a string literal instead of a named constant."""
    found: list[str] = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        candidates: list[ast.expr] = []
        if method in LOCATOR_METHODS and node.args:
            candidates.append(node.args[0])
        if method == "get_by_role":
            candidates.extend(kw.value for kw in node.keywords if kw.arg == "name")
        for arg in candidates:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(f"{node.lineno}: {method}({arg.value!r})")
    return found


def replay_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(REPLAY_DIR.glob("*.py"))
    )


def test_no_locator_in_the_client_takes_a_bare_string_literal() -> None:
    assert bare_locator_literals() == []


def test_the_client_has_the_page_constants_this_rule_expects() -> None:
    """A floor, so a rename cannot make the rule vacuous."""
    names = page_constants()
    for expected in (
        "COMPOSER_SELECTOR",
        "USER_TURN_SELECTOR",
        "CHAT_TURN_SELECTOR",
        "COMPOSER_STATE_JS",
        "SNAPSHOT_JS",
        "CHAT_LOAD_FAILURE_RE",
        "CHAT_RETRY_BUTTON_RE",
        "UPLOAD_INPUT_FALLBACK_SELECTOR",
        "SEND_BUTTONS",
    ):
        assert expected in names, names


@pytest.mark.parametrize("name", page_constants())
def test_every_page_constant_is_exercised_by_tier_r_or_excused(name: str) -> None:
    if name in EXCUSED:
        assert name not in replay_source(), (
            f"{name} is exercised by tests/replay; drop its excuse"
        )
        return
    assert name in replay_source(), (
        f"{name} is read from the page but no test under tests/replay names it; "
        "add one against a recorded fixture, or excuse it in EXCUSED with a reason"
    )


def test_every_excuse_names_a_real_constant() -> None:
    stale = sorted(set(EXCUSED) - set(page_constants()))
    assert stale == [], stale


def test_every_not_page_facing_name_exists_and_is_a_regex() -> None:
    """A stale entry here would silently widen the exemption."""
    assigned = {
        target.id
        for node in _tree().body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for name in NOT_PAGE_FACING:
        assert name in assigned, name
        assert name.endswith("_RE"), name
