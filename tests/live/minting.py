"""Minting and deleting a throwaway sandbox conversation (T4 helpers).

A live test that acts on a conversation needs a conversation to act on.
Two ways exist, and only one of them works:

* look for one an earlier run left in the sandbox. This is what
  tests/live/test_send_chat_flags.py did until 2026-09-20, while it was
  still named test_write_chat_flags.py, and it never ran once: the
  session-scoped sweep in tests/live/conftest.py deletes every
  conversation in the sandbox in teardown, so the chat the test looked
  for was gone before the next run started and the test skipped, every
  time, with a reason that read like a setup note rather than a defect.
* make one. That is this module. It costs one short send, which is why a
  test that uses it belongs to the send tier (T4) and not to the write
  tier (T2).

``GuardedConversationReader`` was tests/live/test_send_effort.py's private
``_GuardedConversationReader``; it moved here unchanged when a second test
needed the same adapter, so there is one copy, not two.

Everything here goes through the guarded session (tests/live/guard.py), so
the sandbox fence still holds: the new id is registered with ``note`` the
moment it resolves, and that registration is the only reason a PATCH on it
is allowed at all.

No network at import time: ``chatgpt_client`` is never imported here. The
caller passes the module in as ``cc``, which is also what lets
tests/test_harness.py exercise ``mint_sandbox_chat`` offline with a fake.
"""

from __future__ import annotations

import time
from typing import Any

CONVERSATION = "/backend-api/conversation/{id}"


class GuardedConversationReader:
    """Adapts a guarded ``live_session``'s ``.call`` to the
    ``get_conversation`` / ``list_conversations`` shape
    ``chatgpt_client.wait_for_reply`` and
    ``chatgpt_client.resolve_new_conversation`` expect from a
    ``ChatGPTSession`` -- both call only these two methods, so this is
    enough to reuse them unchanged over the same guarded plain-HTTP session
    the rest of a live test uses, instead of opening a second, unguarded
    one.
    """

    def __init__(self, guarded: Any, cc: Any) -> None:
        self._guarded = guarded
        self._cc = cc

    def get_conversation(self, chat: str) -> dict[str, Any]:
        status, body = self._guarded.call(CONVERSATION.format(id=chat))
        if status != 200 or not isinstance(body, dict):
            raise self._cc.TransportError(
                f"GET conversation/{chat}: HTTP {status}", status
            )
        return body

    def list_conversations(self, limit: int = 28, offset: int = 0) -> list[dict]:
        status, body = self._guarded.call(
            f"/backend-api/conversations?offset={offset}&limit={limit}&order=updated"
        )
        if status != 200 or not isinstance(body, dict):
            return []
        return list(body.get("items", []))


def mint_sandbox_chat(
    cc: Any,
    live_session: Any,
    sandbox_id: str,
    prompt: str,
    title: str = "",
) -> str:
    """Send ``prompt`` into the sandbox project; return the new chat's id.

    The same three steps a real send makes (scripts/send_prompt.py): open
    one headless window on the sandbox project, send, and resolve the
    provisional ``WEB:`` id the page hands back into the real one by
    looking for the conversation that appeared since the listing taken
    just before. ``since`` is that moment, which is what keeps a
    conversation another run opened earlier from being mistaken for this
    one.

    The resolved id is registered with ``live_session.note`` before this
    returns, so the caller may PATCH it. ``title`` is cosmetic and
    optional: it only makes a leftover legible to a human reading the
    sandbox, and ChatGPT may overwrite it with an auto-title once the
    first reply lands, so nothing should ever look a chat up by it.

    This does not wait for the reply. A caller that needs the reply waits
    for it itself (``cc.wait_for_reply``); a caller that only needs a
    conversation to exist, such as the pin and archive round trip, does
    not have to pay for one.
    """
    reader = GuardedConversationReader(live_session, cc)
    known_ids = {str(c.get("id")) for c in reader.list_conversations()}
    since = time.time()
    with cc.BrowserSender("chrome", project=sandbox_id, visible=False) as sender:
        conversation_id = sender.send(prompt)
    if cc.is_provisional(conversation_id):
        conversation_id = cc.resolve_new_conversation(reader, known_ids, since=since)
    live_session.note(conversation_id)
    if title:
        live_session.call(
            CONVERSATION.format(id=conversation_id),
            method="PATCH",
            payload={"title": title},
        )
    return conversation_id


def delete_sandbox_chat(live_session: Any, conversation_id: str) -> None:
    """Delete one sandbox chat, the way the web UI does it.

    ``PATCH {"is_visible": false}`` (scripts/clean_chats.py). Safe to call
    with an empty or still-provisional id, so a ``finally`` block can call
    it without first working out how far the send got. The sandbox sweep
    in tests/live/conftest.py is a backstop for this, not a replacement:
    a test cleans up after itself.
    """
    if not conversation_id or conversation_id.startswith("WEB:"):
        return
    live_session.call(
        CONVERSATION.format(id=conversation_id),
        method="PATCH",
        payload={"is_visible": False},
    )
