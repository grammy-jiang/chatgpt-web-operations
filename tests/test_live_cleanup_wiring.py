"""T0: the weekly send's cleanup path, offline (TESTING.md P12).

``tests/live/minting.delete_with_backup`` runs ``clean_chats.py --id ID
--delete --backup DIR --apply`` over a real ``ChatGPTSession`` whose
transport is the live tier's guarded session. The weekly T4 test is the
only place it runs for real, once a week; this checks the wiring on a
stand-in for the guarded session, so a broken adapter shows up in
``make test`` and not on a Sunday."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import chatgpt_client as cc
from live.minting import client_over, delete_with_backup

CHAT = "00000000-0000-4000-8000-00000000000c"
PATH = f"/backend-api/conversation/{CHAT}"


class _Guarded:
    """Answers like the guarded session: the conversation as raw text, a
    PATCH that deletes it, and 404 afterwards."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, bool]] = []
        self.deleted = False

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int = 3,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload, raw))
        if path != PATH:
            return 404, {"detail": "not here"}
        if method == "PATCH":
            self.deleted = True
            return 200, {"success": True}
        if self.deleted:
            return 404, {"detail": "deleted"}
        body = {"title": "rp-canary", "mapping": {"n": {"message": "rp-canary-1"}}}
        return 200, json.dumps(body) if raw else body


def test_the_cleanup_backs_up_then_deletes_through_the_guarded_session(
    tmp_path: Path,
) -> None:
    guarded = _Guarded()
    backup = tmp_path / "backup"

    assert delete_with_backup(guarded, cc, CHAT, backup) == 0

    (saved,) = list(backup.glob(f"*_{CHAT}.json"))
    assert "rp-canary-1" in saved.read_text(encoding="utf-8")
    methods = [(m, raw) for m, p, _payload, raw in guarded.calls if p == PATH]
    assert methods[0] == ("GET", True)  # read raw for the backup, first
    patches = [c for c in guarded.calls if c[0] == "PATCH"]
    assert [c[2] for c in patches] == [{"is_visible": False}]


def test_a_provisional_id_is_never_deleted() -> None:
    """F-2026-10-01-25: a send that never got its real id handed the page's
    own ``local-chatgpt:`` placeholder to the teardown, which PATCHed it
    and tripped the guard on top of the real failure. A placeholder names
    no server conversation; only a real id is deleted."""
    from live.minting import delete_sandbox_chat

    calls: list = []

    class Session:
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return 200, {}

    for provisional in (
        "",
        "WEB:0d4f1c2e-aaaa-4bbb-8ccc-000000000001",
        "local-chatgpt:1db76ec1-0360-4d93-96c4-b692dcd7ce77",
    ):
        delete_sandbox_chat(Session(), provisional)
    assert calls == []

    delete_sandbox_chat(Session(), "6abda84e-2418-83ec-9644-1be4b7bb082c")
    assert len(calls) == 1
    assert calls[0][1]["method"] == "PATCH"
    assert calls[0][1]["payload"] == {"is_visible": False}


def test_a_chat_already_gone_is_not_an_error(tmp_path: Path) -> None:
    guarded = _Guarded()
    guarded.deleted = True
    assert delete_with_backup(guarded, cc, CHAT, tmp_path / "b") == 0
    assert not any(c[0] == "PATCH" for c in guarded.calls)


def test_the_client_over_a_guarded_session_is_a_real_client() -> None:
    session = client_over(_Guarded(), cc)
    assert isinstance(session, cc.ChatGPTSession)
    assert session.get_conversation(CHAT)["title"] == "rp-canary"
