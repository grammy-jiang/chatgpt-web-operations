"""T0 tests for clean_chats.py's exact-id selection, the test-chat ledger and
backups (PLAN-2026-09-27 A2): what binnacle's chatgpt-chats did, now here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import clean_chats as ccm
import pytest

A = "6ab9cc0a-b708-83ec-9337-52553608281f"
B = "6ab94a01-340c-83ec-9551-1b0d592aca12"
URL_A = f"https://chatgpt.com/g/g-p-x/c/{A}"


class _Session:
    """A ChatGPTSession stand-in: raw reads by id, PATCH through delete/
    archive, and the inner call for unarchive."""

    def __init__(self, texts: dict[str, Any], fail: set[str] | None = None) -> None:
        self.texts = texts  # id -> JSON text, or an int status
        self.fail = fail or set()
        self.calls: list[tuple[str, str, Any]] = []
        self.session = self

    def call(self, path: str, method: str = "GET", payload: Any = None, **kw: Any):
        self.calls.append((method, path, payload))
        chat = path.rsplit("/", 1)[-1]
        if method == "PATCH":
            return (500, "no") if chat in self.fail else (200, {"success": True})
        reply = self.texts.get(chat, 404)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return reply, '{"detail": "x"}'
        return 200, reply

    def delete(self, chat: str) -> None:
        self.calls.append(("DELETE-PATCH", chat, None))
        if chat in self.fail:
            raise RuntimeError("PATCH conversation -> HTTP 500")

    def archive(self, chat: str) -> None:
        self.calls.append(("ARCHIVE-PATCH", chat, None))
        if chat in self.fail:
            raise RuntimeError("PATCH conversation -> HTTP 500")


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "chatgpt-chats" / "test-chats.json"
    monkeypatch.setenv(ccm.LEDGER_ENV, str(path))
    return path


def _wire(monkeypatch, session: _Session) -> _Session:
    monkeypatch.setattr(ccm, "open_session", lambda *a, **k: session)
    return session


def _conv(title: str) -> str:
    return json.dumps({"title": title, "mapping": {}})


def test_parse_chat_id_takes_an_id_or_a_url_and_rejects_the_rest() -> None:
    assert ccm.parse_chat_id(URL_A) == A
    assert ccm.parse_chat_id(A.upper()) == A
    assert ccm.parse_chat_id("not an id") == ""


def test_the_default_ledger_is_the_one_binnacle_kept(monkeypatch) -> None:
    monkeypatch.delenv(ccm.LEDGER_ENV, raising=False)
    assert ccm.ledger_path() == (
        Path.home() / ".local" / "share" / "chatgpt-chats" / "test-chats.json"
    )


def test_track_and_untrack_edit_only_the_ledger(ledger, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ccm, "open_session", lambda *a, **k: pytest.fail("no session"))
    assert ccm.main(["--track", URL_A, "--note", "write test"]) == 0
    assert ccm.main(["--track", A]) == 0
    assert "already tracked" in capsys.readouterr().out
    chats = json.loads(ledger.read_text())["chats"]
    assert [(c["id"], c["note"]) for c in chats] == [(A, "write test")]
    assert ccm.main(["--untrack", A]) == 0
    assert json.loads(ledger.read_text())["chats"] == []
    assert ccm.main(["--untrack", A]) == 1
    assert ccm.main(["--track", "nope"]) == 2


def test_a_corrupt_ledger_is_reported_not_overwritten(ledger, monkeypatch) -> None:
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"chats": "not a list"}')
    assert ccm.main(["--track", A]) == 1
    assert ccm.main(["--tracked", "--delete"]) == 1
    assert ledger.read_text() == '{"chats": "not a list"}'


def test_id_dry_run_reads_each_chat_and_touches_nothing(ledger, monkeypatch, capsys):
    session = _wire(monkeypatch, _Session({A: _conv("probe"), B: 404}))
    assert ccm.main(["--id", URL_A, "--id", B, "--id", A, "--delete"]) == 0
    out = capsys.readouterr().out
    assert "probe" in out and "(already gone)" in out
    assert "dry run: 1 conversation(s) would be deleted" in out
    assert [c[0] for c in session.calls] == ["GET", "GET"]  # A once, B once


def test_id_delete_backs_up_first_then_deletes(ledger, monkeypatch, tmp_path) -> None:
    session = _wire(monkeypatch, _Session({A: _conv("probe")}))
    backups = tmp_path / "backups"
    assert ccm.main(["--id", A, "--delete", "--backup", str(backups), "--apply"]) == 0
    saved = list(backups.glob(f"*_{A}.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["title"] == "probe"
    assert ("DELETE-PATCH", A, None) in session.calls


def test_a_failed_backup_leaves_the_chat_untouched(ledger, monkeypatch, tmp_path):
    session = _wire(monkeypatch, _Session({A: _conv("probe")}))
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code = ccm.main(["--id", A, "--delete", "--backup", str(blocker / "d"), "--apply"])
    assert code == 1
    assert not any(c[0] == "DELETE-PATCH" for c in session.calls)


def test_tracked_delete_clears_what_it_deleted_or_found_gone(ledger, monkeypatch):
    for chat in (A, B):
        assert ccm.main(["--track", chat]) == 0
    session = _wire(monkeypatch, _Session({A: _conv("probe"), B: 404}))
    assert ccm.main(["--tracked", "--delete", "--apply"]) == 0
    assert json.loads(ledger.read_text())["chats"] == []
    assert ("DELETE-PATCH", A, None) in session.calls


def test_a_failed_delete_keeps_the_chat_tracked_and_exits_1(ledger, monkeypatch):
    assert ccm.main(["--track", A]) == 0
    _wire(monkeypatch, _Session({A: _conv("probe")}, fail={A}))
    assert ccm.main(["--tracked", "--delete", "--apply"]) == 1
    assert [c["id"] for c in json.loads(ledger.read_text())["chats"]] == [A]


def test_an_unreadable_chat_is_a_failure_in_a_dry_run_and_an_apply(ledger, monkeypatch):
    _wire(monkeypatch, _Session({A: 500}))
    assert ccm.main(["--id", A, "--archive"]) == 1
    _wire(monkeypatch, _Session({A: RuntimeError("read timed out")}))
    assert ccm.main(["--id", A, "--archive", "--apply"]) == 1


def test_archive_and_unarchive_by_id(ledger, monkeypatch) -> None:
    session = _wire(monkeypatch, _Session({A: _conv("x")}))
    assert ccm.main(["--id", A, "--archive", "--apply"]) == 0
    assert ("ARCHIVE-PATCH", A, None) in session.calls
    session = _wire(monkeypatch, _Session({A: _conv("x")}))
    assert ccm.main(["--id", A, "--unarchive", "--apply"]) == 0
    assert ("PATCH", f"/backend-api/conversation/{A}", {"is_archived": False}) in (
        session.calls
    )
    session = _wire(monkeypatch, _Session({A: _conv("x")}, fail={A}))
    assert ccm.main(["--id", A, "--unarchive", "--apply"]) == 1


def test_no_tracked_chats_is_nothing_to_do(ledger, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ccm, "open_session", lambda *a, **k: pytest.fail("no session"))
    assert ccm.main(["--tracked", "--delete"]) == 0
    assert "no tracked chats" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--id", A, "--match", "x", "--delete"],
        ["--id", A, "--project", "g-p-x", "--delete"],
        ["--id", A, "--tracked", "--delete"],
        ["--tracked", "--match", "x", "--delete"],
        ["--id", "not-an-id", "--delete"],
    ],
)
def test_selectors_that_do_not_combine_are_refused(ledger, monkeypatch, argv) -> None:
    monkeypatch.setattr(ccm, "open_session", lambda *a, **k: pytest.fail("no session"))
    assert ccm.main(argv) == 2


def test_a_match_delete_with_backup_reads_each_chat_first(
    ledger, monkeypatch, tmp_path
) -> None:
    """--backup works with the old selectors too: the chat is read raw, saved,
    then deleted; one that cannot be read is left alone."""
    session = _Session({A: _conv("rp probe"), B: 500})
    session.list_conversations = lambda limit=50: [  # type: ignore[attr-defined]
        {"id": A, "title": "rp probe"},
        {"id": B, "title": "rp other"},
    ]
    _wire(monkeypatch, session)
    backups = tmp_path / "b"
    code = ccm.main(["--match", "rp", "--delete", "--backup", str(backups), "--apply"])
    assert code == 1
    assert len(list(backups.glob("*.json"))) == 1
    assert ("DELETE-PATCH", A, None) in session.calls
    assert ("DELETE-PATCH", B, None) not in session.calls


def test_title_of_tolerates_what_is_not_a_conversation() -> None:
    assert ccm.title_of("not json") == ""
    assert ccm.title_of("[1, 2]") == ""
    assert ccm.title_of('{"title": null}') == ""


def test_the_browser_option_reaches_the_session(ledger, monkeypatch) -> None:
    seen: list[str] = []

    def opener(browser: str = "chrome") -> _Session:
        seen.append(browser)
        return _Session({A: _conv("x")})

    monkeypatch.setattr(ccm, "open_session", opener)
    assert ccm.main(["--browser", "chromium", "--id", A, "--archive"]) == 0
    assert seen == ["chromium"]
