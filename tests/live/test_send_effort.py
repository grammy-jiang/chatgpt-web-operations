"""Live send with a pinned reasoning effort, checked end to end (T4,
TESTING.md section 1: marker live_send, needs CHATGPT_LIVE=send).

From 2026-09-16 to 2026-09-20 the research orchestrator pinned a send's
reasoning effort by rewriting the ``oai-last-model-config`` cookie
(``with_effort`` in its own client, ``TASK_EFFORT``). Measured on
2026-09-20 with recorded sends, that never worked: a send asking for
"standard" posted ``thinking_effort`` ``"max"``, because the page takes its
model and effort from the account's own server-side record
(``GET /backend-api/settings/user`` -> ``settings.last_used_model_config``,
read here through ``model_settings.server_config``), not from that cookie.
So for two months a table of per-step efforts silently decorated every send
with whatever the profile itself had last used, and nothing checked the two
apart. ``chatgpt_client.rewrite_send_body`` now pins effort (and model,
search, hints) by rewriting the outgoing ``f/conversation`` POST body
itself -- the one thing that actually reaches the server -- applied through
a route ``BrowserSender._open`` registers only when one of them is set.
This test is the automated check that would have caught the original bug;
it exists to keep catching it if the mechanism ever regresses.

The level asked for is never hard-coded. A test that happens to ask for the
level the account's own profile already uses would pass whether or not the
pin works -- exactly how the original bug hid for two months. So this reads
the account's current server-side default first
(``model_settings.server_config`` on ``GET /backend-api/settings/user``),
then picks a level from ``cc.EFFORTS`` that (a) differs from that default
and (b) the account's own ``GET /backend-api/models`` payload actually
offers for that model (``model_settings.effort_levels_of``), preferring
"standard", then "extended", then "min" -- never "max", the slowest level
(SKILL.md, "Reasoning effort"), which would work against "no waiting for a
long reply" below. Skips with a clear reason when no such level exists (an
account whose current default and only alternative are the same, or a
model with no configurable effort at all).

``effort_verdict`` -- pure, no session, no page -- is the check itself:
every ``thinking_effort`` that ``read_chat.turn_facts`` reports for the
resulting conversation must equal what was asked. It lives here, at module
level, rather than inside the test function, precisely so
tests/test_effort_verdict.py can import and exercise it without ever
touching the network; ``chatgpt_client``, the only network-capable name
this module uses, is imported inside the test function below, never at
module level, the same rule tests/live/test_browser_upload.py,
tests/live/minting.py and tests/live/conftest.py follow -- importing it
must never become something that happens merely by collecting this file.

Kept to a single short send so it stays inside the browser budget: one
window, no attachments, and no long wait, because "reply with the single
word OK" only ever produces a short reply. The conversation is deleted in
a ``finally`` block (``minting.delete_sandbox_chat``); the session-scoped
sandbox sweep (tests/live/conftest.py) is a backstop, not something this
relies on.

Run this test only through its opt-in live tier. See TESTING.md and
VERIFICATION.md for current execution results.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from live.minting import GuardedConversationReader, delete_sandbox_chat

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import model_settings  # noqa: E402
import read_chat  # noqa: E402

pytestmark = pytest.mark.live_send

PROMPT = "reply with the single word OK"

# Preference order for the level this test asks for: "max" is deliberately
# absent (module docstring) -- it is the slowest level, and asking for it
# would fight this test's own "no long wait" budget.
_CANDIDATE_LEVELS = ("standard", "extended", "min")


def _choose_asked_effort(
    current_effort: str, offered: Iterable[str], efforts: Iterable[str]
) -> str | None:
    """A level to ask for that differs from ``current_effort``, in
    ``_CANDIDATE_LEVELS`` order, restricted to what ``offered`` (the
    account's own models payload, for the model ``current_effort`` belongs
    to) and ``efforts`` (``cc.EFFORTS``, what the client accepts at all)
    both agree is usable. ``None`` when nothing qualifies, so the caller
    skips instead of asking for the level already in use -- which would
    prove nothing (module docstring).
    """
    offered_set, efforts_set = set(offered), set(efforts)
    for candidate in _CANDIDATE_LEVELS:
        if (
            candidate != current_effort
            and candidate in offered_set
            and candidate in efforts_set
        ):
            return candidate
    return None


def effort_verdict(turn_facts: list[dict[str, Any]], asked: str) -> tuple[bool, str]:
    """Whether every ``thinking_effort`` in ``turn_facts`` equals ``asked``.

    ``turn_facts`` is normally ``read_chat.turn_facts(conv)``'s own return
    shape: one dict per visible assistant reply, each carrying a
    ``"thinking_effort"`` key (``None`` when a reply's metadata carries no
    such field at all). Every entry is compared literally, including one
    whose effort is ``None``: a reasoning-capable model asked for a level
    is expected to record one on every visible reply, so a missing value is
    as much a failure to verify the pin as a wrong one, never something
    this quietly looks past.

    An empty ``turn_facts`` -- no visible assistant reply at all, most
    often because the turn never finished -- is never a pass either: there
    is nothing here to confirm the pin took, which is a failure to verify,
    not a success.

    Returns ``(passed, message)``; ``message`` always names ``asked`` and
    the exact values recorded, so a regression shaped like the one this
    test exists for (asked "standard", recorded "max") is legible straight
    from the assertion failure.
    """
    if not turn_facts:
        return (
            False,
            "no assistant turn recorded a thinking_effort (turn_facts is empty)",
        )
    recorded = [fact.get("thinking_effort") for fact in turn_facts]
    passed = all(value == asked for value in recorded)
    verb = "every turn matches" if passed else "not every turn matches"
    return passed, f"asked={asked!r}, recorded={recorded!r}: {verb}"


def test_a_pinned_effort_is_recorded_on_every_assistant_turn(
    live_session: Any, sandbox_id: str
) -> None:
    """Send one short prompt with a non-default effort pinned; every
    assistant turn the reply produced must record exactly that effort.

    This exercises ``rewrite_send_body`` end to end: a real scripted-browser
    send, a plain-HTTP wait for the reply, and a check of the reply's own
    metadata -- the only place that says what a send actually used (module
    docstring; SKILL.md, "Reasoning effort").
    """
    import chatgpt_client as cc

    status, settings = live_session.call(model_settings.SETTINGS)
    assert status == 200, f"GET {model_settings.SETTINGS}: HTTP {status}"
    current = model_settings.server_config(
        settings if isinstance(settings, dict) else {}
    )
    if not current["model"]:
        pytest.skip(
            f"no last_used_model_config for the web surface in "
            f"{model_settings.SETTINGS}; cannot tell what the account's "
            "current default is"
        )

    status, models = live_session.call(model_settings.MODELS)
    assert status == 200, f"GET {model_settings.MODELS}: HTTP {status}"
    offered = model_settings.effort_levels_of(
        models if isinstance(models, dict) else {}
    ).get(current["model"], [])

    asked = _choose_asked_effort(current["effort"], offered, cc.EFFORTS)
    if asked is None:
        pytest.skip(
            f"no second effort level to ask for: account default is "
            f"model={current['model']!r} effort={current['effort']!r}, "
            f"offered={sorted(offered)}"
        )

    reader = GuardedConversationReader(live_session, cc)
    known_ids = {str(c.get("id")) for c in reader.list_conversations()}
    since = time.time()

    conversation_id = ""
    try:
        with cc.BrowserSender(
            "chrome", project=sandbox_id, effort=asked, visible=False
        ) as sender:
            conversation_id = sender.send(PROMPT)

        if cc.is_provisional(conversation_id):
            conversation_id = cc.resolve_new_conversation(
                reader, known_ids, since=since
            )
        live_session.note(conversation_id)

        cc.wait_for_reply(
            reader,
            conversation_id,
            timeout=120,
            interval=5,
            max_interval=15,
            first_poll=3,
        )

        status, conv = live_session.call(f"/backend-api/conversation/{conversation_id}")
        assert status == 200, f"GET conversation/{conversation_id}: HTTP {status}"
        facts = read_chat.turn_facts(conv if isinstance(conv, dict) else {})
        passed, message = effort_verdict(facts, asked)
        assert passed, message
    finally:
        delete_sandbox_chat(live_session, conversation_id)
