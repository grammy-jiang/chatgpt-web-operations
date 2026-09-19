"""Live round trip for pin, unpin, archive and unarchive (T2, TESTING.md
section 1; ROADMAP.md Stage 2 item 3, M2).

Exercises the same pure function and endpoint constant the commands send
through -- ``pin_chat.patch_body`` and ``pin_chat.CONVERSATION`` -- against
the real sandbox project, over the guarded session, so this is the actual
``PATCH .../conversation/<id>`` the commands make, not a re-implementation
of it. Every path stays inside what tests/live/guard.py allows for a T2
test: ``/backend-api/gizmos/<sandbox_id>`` and a ``conversation/<id>`` the
guard has itself seen in ``gizmos/<sandbox_id>/conversations`` -- which is
why ``live_session.refresh()`` runs before any PATCH here, in the
``test_chat`` fixture below. ``GET /backend-api/pins`` and
``GET /backend-api/conversation/<id>`` are unrestricted reads (guard.py
allows every GET), so pin and unpin are checked against ``pins`` itself,
the more authoritative record (SKILL.md: pinned chats "appear in GET
/backend-api/pins ... and carry is_starred true in listings"), with
``pin_chat.verify`` re-checked against the conversation's own
``is_starred`` alongside it.

The chat this acts on is the sandbox conversation titled "rp-test send 1".
Creating one is send_prompt.py's job, not this test's, so a sandbox without
one skips with a clear reason instead of failing or creating it here.
Everything is restored in a ``finally`` block to unpinned and unarchived,
however far the round trip got, the same unconditional-restore shape
tests/live/test_write_project_settings.py uses for the sandbox's
instructions.

Not run by this agent (see the skill's HARD RULES); collect-only proves it
is wired up without touching the account:

    .venv/bin/python -m pytest tests/live/test_write_chat_flags.py \\
        --collect-only -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pin_chat  # noqa: E402

pytestmark = pytest.mark.live_write

TEST_CHAT_TITLE = "rp-test send 1"


def _patch(live_session: Any, chat_id: str, body: dict[str, Any]) -> None:
    """The command's own PATCH, inline: same path, same call."""
    status, _resp = live_session.call(
        pin_chat.CONVERSATION.format(id=chat_id), method="PATCH", payload=body
    )
    assert status == 200, f"PATCH conversation/{chat_id} {body}: HTTP {status}"


def _conversation(live_session: Any, chat_id: str) -> dict[str, Any]:
    status, body = live_session.call(pin_chat.CONVERSATION.format(id=chat_id))
    assert status == 200, f"GET conversation/{chat_id}: HTTP {status}"
    return body


def _pinned_ids(live_session: Any) -> set[str]:
    """Every conversation id ``GET /backend-api/pins`` currently lists."""
    status, body = live_session.call("/backend-api/pins")
    assert status == 200, f"GET pins: HTTP {status}"
    entries = body if isinstance(body, list) else []
    return {
        str((entry.get("item") or {}).get("id"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("item_type") == "conversation"
    }


@pytest.fixture
def test_chat(live_session: Any, sandbox_id: str) -> dict[str, Any]:
    """The sandbox conversation titled "rp-test send 1", or a clear skip.

    ``live_session.refresh()`` reads gizmos/<sandbox_id>/conversations and
    records every id it sees there in the guard's ``known`` set -- the only
    ids tests/live/guard.py will ever let a T2 test PATCH. Calling it here,
    before either round trip runs, is what makes the PATCH calls below
    allowed at all.
    """
    status, body = live_session.refresh()
    assert status == 200, f"gizmos/{sandbox_id}/conversations: HTTP {status}"
    items = body.get("items", []) if isinstance(body, dict) else []
    for item in items:
        if isinstance(item, dict) and item.get("title") == TEST_CHAT_TITLE:
            return item
    pytest.skip(
        f"no sandbox conversation titled {TEST_CHAT_TITLE!r}; send a prompt "
        f"into the sandbox project first, e.g. send_prompt.py --project "
        f"g-p-<sandbox> --title {TEST_CHAT_TITLE!r} <prompt file>"
    )


def test_pin_unpin_archive_unarchive_round_trip_on_the_sandbox_chat(
    live_session: Any, sandbox_id: str, test_chat: dict[str, Any]
) -> None:
    """pin -> pins contains the id -> unpin -> it does not;
    archive -> is_archived true -> unarchive -> false."""
    chat_id = test_chat["id"]
    try:
        _patch(live_session, chat_id, pin_chat.patch_body(True))
        assert chat_id in _pinned_ids(live_session), (
            "pin did not appear in /backend-api/pins"
        )
        mismatches = pin_chat.verify(_conversation(live_session, chat_id), True)
        assert mismatches == [], mismatches

        _patch(live_session, chat_id, pin_chat.patch_body(False))
        assert chat_id not in _pinned_ids(live_session), (
            "unpin left the chat in /backend-api/pins"
        )
        mismatches = pin_chat.verify(_conversation(live_session, chat_id), False)
        assert mismatches == [], mismatches

        _patch(live_session, chat_id, {"is_archived": True})
        assert _conversation(live_session, chat_id)["is_archived"] is True

        _patch(live_session, chat_id, {"is_archived": False})
        assert _conversation(live_session, chat_id)["is_archived"] is False
    finally:
        _patch(live_session, chat_id, pin_chat.patch_body(False))
        _patch(live_session, chat_id, {"is_archived": False})
        assert chat_id not in _pinned_ids(live_session), (
            "sandbox chat NOT restored: still pinned"
        )
        assert _conversation(live_session, chat_id)["is_archived"] is False, (
            "sandbox chat NOT restored: still archived"
        )
