"""Live round trip for pin, unpin, archive and unarchive (T4, TESTING.md
section 1; ROADMAP.md Stage 2 item 3, M2).

Exercises the same pure function and endpoint constant the commands send
through -- ``pin_chat.patch_body`` and ``pin_chat.CONVERSATION`` -- against
the real sandbox project, over the guarded session, so this is the actual
``PATCH .../conversation/<id>`` the commands make, not a re-implementation
of it. Every path stays inside what tests/live/guard.py allows a write
tier: ``/backend-api/gizmos/<sandbox_id>`` and a ``conversation/<id>`` the
guard has itself seen in ``gizmos/<sandbox_id>/conversations`` -- which is
why the ``test_chat`` fixture below registers the chat it mints with
``live_session.note`` before yielding it. ``GET /backend-api/pins`` and
``GET /backend-api/conversation/<id>`` are unrestricted reads (guard.py
allows every GET), so pin and unpin are checked against ``pins`` itself,
the more authoritative record (SKILL.md: pinned chats "appear in GET
/backend-api/pins ... and carry is_starred true in listings"), with
``pin_chat.verify`` re-checked against the conversation's own
``is_starred`` alongside it.

This test mints the chat it acts on. It used to look for a sandbox
conversation left behind by an earlier send and skip when it found none --
which was always, because the session-end sweep empties the sandbox, so
the test never ran once between the day it was written and 2026-09-20.
A test that depends on state another run happened to leave is a test that
does not run. Minting a chat means one send, so this belongs to the send
tier (T4) and not to the write tier it used to claim.
Everything is restored in a ``finally`` block to unpinned and unarchived,
however far the round trip got, the same unconditional-restore shape
tests/live/test_write_project_settings.py uses for the sandbox's
instructions.

Not run by this agent (see the skill's HARD RULES); collect-only proves it
is wired up without touching the account:

    .venv/bin/python -m pytest tests/live/test_send_chat_flags.py \\
        --collect-only -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from live.minting import delete_sandbox_chat, mint_sandbox_chat

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pin_chat  # noqa: E402

pytestmark = pytest.mark.live_send

# Cosmetic only: nothing looks a chat up by title (ChatGPT renames a new
# chat itself once the first reply lands). It exists so a leftover in the
# sandbox says where it came from.
TEST_CHAT_TITLE = "rp-test chat flags"
PROMPT = "reply with the single word OK"


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
def test_chat(request: Any, live_session: Any, sandbox_id: str) -> Any:
    """One throwaway sandbox chat, minted here and deleted in teardown.

    ``minting.mint_sandbox_chat`` sends one short prompt into the sandbox
    project, resolves the real conversation id, and registers it with the
    guard's ``note`` -- that registration is the only reason the PATCH
    calls below are allowed at all (tests/live/guard.py permits a non-GET
    on ``conversation/<id>`` only for an id the guard has itself seen).

    Teardown deletes it, unless ``--keep-sandbox-chats`` was passed, which
    exists so a failure can be looked at in the web UI; the session-scoped
    sweep in tests/live/conftest.py honours the same flag and is the
    backstop behind this one.
    """
    import chatgpt_client as cc

    chat_id = mint_sandbox_chat(
        cc, live_session, sandbox_id, PROMPT, title=TEST_CHAT_TITLE
    )
    try:
        yield {"id": chat_id, "title": TEST_CHAT_TITLE}
    finally:
        if request.config.getoption("--keep-sandbox-chats"):
            print(f"--keep-sandbox-chats: left conversation {chat_id}")
        else:
            delete_sandbox_chat(live_session, chat_id)


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
