"""Tier T4: the scripted end-to-end send through the real command entry
point (TESTING.md section 6, P3; marker live_send, CHATGPT_LIVE=send).

Three facts about one real send made by ``send_prompt.main`` -- the same
function ``python3 scripts/send_prompt.py`` runs -- into the sandbox
project:

1. it posted, with one small attachment uploaded through the composer (the
   upload path is part of a real send since 2026-09-28): exit 0 and a real,
   resolved conversation id in the ``--json`` record;
2. the reply arrived: the record's ``reply`` and ``read_chat.main([id,
   "--text"])`` both carry the nonce the prompt asked for;
3. it is cleaned up by ``clean_chats.py`` itself (``--id ID --delete
   --backup DIR --apply``, TESTING.md P12): the backup holds the
   conversation with the nonce, and ``GET conversation/<id>`` answers 404
   afterwards. If the command fails, the chat is still deleted directly.

The HTTP half runs through the guarded session: ``open_session`` in both
commands is replaced by a ``GuardedConversationReader`` over
``live_session``, which is exactly the shape they use (``list_conversations``
before the send, ``get_conversation`` to resolve, wait and read). The
browser half is the real ``BrowserSender``, untouched. ``RP_NEWCHAT_FAST``
is cleared so the default path runs: session, listing, lock, send, resolve.

With ``RP_SNAPSHOT_DIR`` set it also records two DOM fixtures for tier R
(TESTING.md P1): ``conversation`` (the chat page with its turns) and
``composer-filled`` (the sandbox composer with text in it, so the send
button exists; never clicked). ``make refresh-dom-fixtures FROM=<dir>``
with ``--name`` promotes them.

Weekly from cron (``~/.local/bin/chatgpt-ops-send-check.sh``, which runs the T3
upload dry run first, recording ``composer-attached``), and by hand
after any change to ``BrowserSender`` or ``send_prompt.py``. One send per
run; the T4 cap counts it.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

from live.minting import (
    GuardedConversationReader,
    delete_sandbox_chat,
    delete_with_backup,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import read_chat  # noqa: E402
import send_prompt  # noqa: E402

pytestmark = pytest.mark.live_send

NONCE_PREFIX = "rp-canary-"
FILLED_PROBE = "rp-test probe: filled for a DOM snapshot, never sent"


def test_send_prompt_main_posts_gets_a_reply_and_is_cleaned_up(
    live_session: Any,
    sandbox_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import chatgpt_client as cc

    nonce = f"{NONCE_PREFIX}{secrets.token_hex(4)}"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(
        f"Reply with exactly this token and nothing else: {nonce}\n", encoding="utf-8"
    )
    record = tmp_path / "send.json"
    attachment = tmp_path / "rp-test-attachment.md"
    attachment.write_text(
        "rp-test attachment for the weekly send; nothing to act on\n", encoding="utf-8"
    )
    reader = GuardedConversationReader(live_session, cc)
    monkeypatch.setattr(send_prompt, "open_session", lambda *a, **k: reader)
    monkeypatch.setattr(read_chat, "open_session", lambda *a, **k: reader)
    monkeypatch.delenv("RP_NEWCHAT_FAST", raising=False)

    conversation_id = ""
    backup_dir = tmp_path / "backup"
    cleaned: int | None = None
    try:
        rc = send_prompt.main(
            [
                str(prompt),
                "--project",
                sandbox_id,
                "--json",
                str(record),
                "--timeout",
                "240",
                "--attach",
                str(attachment),
            ]
        )
        printed = capsys.readouterr().out
        assert rc == 0, printed[-800:]

        # 1. posted
        doc = json.loads(record.read_text(encoding="utf-8"))
        conversation_id = str(doc.get("conversation_id") or "")
        assert conversation_id and not cc.is_provisional(conversation_id), doc
        assert doc.get("resolved") is True, doc
        live_session.note(conversation_id)

        # 2. the reply arrived, in the record and through read_chat
        assert nonce in (doc.get("reply") or ""), doc.get("reply")
        assert read_chat.main([conversation_id, "--text"]) == 0
        assert nonce in capsys.readouterr().out

        # tier R fixtures, when asked for
        snapshot_dir = os.environ.get("RP_SNAPSHOT_DIR")
        if snapshot_dir:
            with cc.BrowserSender(
                "chrome", project=sandbox_id, visible=False
            ) as sender:
                facts = sender.snapshot_page(
                    snapshot_dir, "conversation", chat=conversation_id
                )
                assert facts["turns"]["user"] >= 1, facts["turns"]
                sender.fill_composer(FILLED_PROBE)
                filled = sender.snapshot_page(snapshot_dir, "composer-filled")
                assert filled["composer"]["found"] is True, filled["composer"]
    finally:
        if conversation_id and not cc.is_provisional(conversation_id):
            cleaned = delete_with_backup(live_session, cc, conversation_id, backup_dir)
        if cleaned != 0:
            delete_sandbox_chat(live_session, conversation_id)  # never leave it

    # 3. cleaned up by clean_chats.py: backed up, deleted, 404
    if conversation_id:
        assert cleaned == 0, "clean_chats.py --id --delete --backup --apply failed"
        saved = list(backup_dir.glob(f"*_{conversation_id}.json"))
        assert len(saved) == 1, [p.name for p in saved]
        assert nonce in saved[0].read_text(encoding="utf-8"), "backup lacks the nonce"
        status, _body = live_session.call(
            f"/backend-api/conversation/{conversation_id}"
        )
        assert status == 404, f"conversation/{conversation_id} still answers {status}"
