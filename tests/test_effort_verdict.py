"""Offline tests for ``effort_verdict`` (T0): the pure check
tests/live/test_send_effort.py uses to confirm a send's pinned reasoning
effort actually reached every assistant turn (that module's own docstring
has the full WHY -- the 2026-09-16 to 2026-09-20 regression this guards
against).

``effort_verdict`` lives in tests/live/test_send_effort.py itself, at
module level, precisely so it can be imported here without touching the
network: ``chatgpt_client``, the only network-capable name that module
uses, is imported inside its live test function, never at module import
time. Its arguably cleanest long-term home is scripts/read_chat.py, next to
``turn_facts`` and ``format_turn_fact`` -- it is a general-purpose check
over ``turn_facts``'s own output shape, not something inherently tied to
the live/browser test machinery -- but this change's file ownership was
tests/live/test_send_effort.py and this file only, so it was left where it
is rather than moved.

Every test builds its own ``turn_facts``-shaped input by hand: a list of
plain dicts each carrying a ``"thinking_effort"`` key, the one field
``effort_verdict`` reads (the other three keys ``read_chat.turn_facts``
reports -- ``resolved_model_slug``, ``has_search``, ``has_citations`` --
are irrelevant to it and left out here).
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from live.test_send_effort import effort_verdict  # noqa: E402


def _fact(effort: str | None) -> dict[str, object]:
    """A minimal turn_facts entry carrying only the field effort_verdict
    reads."""
    return {"thinking_effort": effort}


def test_all_matching_efforts_pass() -> None:
    facts = [_fact("standard"), _fact("standard"), _fact("standard")]
    passed, message = effort_verdict(facts, "standard")
    assert passed is True
    assert "standard" in message


def test_one_mismatch_fails_the_regression_shape() -> None:
    """Asked "standard", recorded "max": the exact 2026-09-20 regression
    shape (tests/live/test_send_effort.py's module docstring) -- what this
    test exists to keep catching."""
    facts = [_fact("max")]
    passed, message = effort_verdict(facts, "standard")
    assert passed is False
    assert "asked='standard'" in message
    assert "'max'" in message


def test_an_empty_list_is_not_a_pass() -> None:
    """No effort recorded at all: nothing here confirms the pin took, so
    this must never read as a pass."""
    passed, message = effort_verdict([], "standard")
    assert passed is False
    assert "empty" in message


def test_mixed_levels_fail_and_name_every_recorded_value() -> None:
    facts = [_fact("standard"), _fact("max"), _fact("standard")]
    passed, message = effort_verdict(facts, "standard")
    assert passed is False
    assert "'standard'" in message
    assert "'max'" in message
