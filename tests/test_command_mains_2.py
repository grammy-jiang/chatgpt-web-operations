"""Tests for the ``main()`` paths and remaining branches of the read commands.

Continues ``test_command_mains.py`` (split at ~600 lines) for
``list_chats.py``, ``read_chat.py``, ``round_status.py`` and
``clean_chats.py``. See that file's module docstring for the rules this one
follows too: no network, small fakes, one named failure per test.

``if __name__ == "__main__":`` blocks are excluded by ``.coveragerc`` and are
not covered here; they only re-exec into the venv and call ``main()``, both
already exercised directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import clean_chats  # noqa: E402
import list_chats  # noqa: E402
import read_chat  # noqa: E402
import round_status  # noqa: E402

# ---------------------------------------------------------------------------
# list_chats.main
# ---------------------------------------------------------------------------

_CHATS = [
    {"id": "aaa", "title": "rp msgloom-topic-04 · terminology", "update_time": "t1"},
    {"id": "bbb", "title": "Holiday planning", "update_time": "t2"},
    {"id": "ccc", "title": "rp msgloom-topic-04 · screen", "update_time": "t3"},
]


class _FakeChatsBackend:
    """``session.session.call`` recording the query it was asked for."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[str] = []

    def call(self, path: str) -> tuple[int, dict]:
        self.calls.append(path)
        return 200, {"items": self.items}


class _FakeChatsSession:
    def __init__(self, items: list[dict]) -> None:
        self.session = _FakeChatsBackend(items)


def test_main_lists_every_chat_by_default(monkeypatch, capsys) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main([]) == 0
    out = capsys.readouterr().out
    assert "3 of 3 listed conversation(s)" in out
    assert "&is_archived" not in session.session.calls[0]


def test_main_match_flag_filters_the_listed_chats(monkeypatch, capsys) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main(["--match", "rp msgloom"]) == 0
    assert "2 of 3 listed conversation(s)" in capsys.readouterr().out


def test_main_pinned_flag_is_forwarded_to_the_query(monkeypatch, capsys) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main(["--pinned"]) == 0
    assert session.session.calls[0].endswith("&is_starred=true")


def test_main_archived_flag_is_forwarded_to_the_query(monkeypatch, capsys) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main(["--archived"]) == 0
    assert session.session.calls[0].endswith("&is_archived=true")


def test_main_no_project_chats_flag_is_forwarded_to_the_query(
    monkeypatch, capsys
) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main(["--no-project-chats"]) == 0
    assert session.session.calls[0].endswith("&hide_snorlax=true")


def test_main_limit_flag_is_forwarded_to_the_query(monkeypatch, capsys) -> None:
    session = _FakeChatsSession(_CHATS)
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main(["--limit", "5"]) == 0
    assert "limit=5" in session.session.calls[0]


def test_main_reports_nothing_when_the_account_has_no_chats(
    monkeypatch, capsys
) -> None:
    session = _FakeChatsSession([])
    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: session)
    assert list_chats.main([]) == 0
    out = capsys.readouterr().out
    assert "(nothing)" in out
    assert "0 of 0 listed conversation(s)" in out


def test_main_survives_a_body_that_is_not_a_dict(monkeypatch, capsys) -> None:
    """A malformed listing response must not crash the report."""

    class _OddBackend:
        def call(self, path: str) -> tuple[int, Any]:
            return 200, None

    class _OddSession:
        session = _OddBackend()

    monkeypatch.setattr(list_chats, "open_session", lambda *a, **k: _OddSession())
    assert list_chats.main([]) == 0
    assert "0 of 0 listed conversation(s)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# read_chat.main
# ---------------------------------------------------------------------------


def _msg(role: str, text: str = "") -> dict:
    return {
        "author": {"role": role},
        "content": {"content_type": "text"},
        "recipient": "all",
        "status": "finished_successfully",
        "text": text,
    }


class _FakeReadClient:
    """The client helpers read_chat.main uses, over a simple message list."""

    def __init__(self, messages: list[dict], final: bool, reply: str = "") -> None:
        self.messages = messages
        self.final = final
        self.reply = reply

    def chat_id(self, chat: str) -> str:
        return chat.rsplit("/", 1)[-1]

    def tail_signature(self, _conv: dict) -> tuple[str, int, str, bool]:
        return ("node", len(self.messages), "text", self.final)

    def assistant_text_messages(self, _conv: dict) -> list[dict]:
        return [m for m in self.messages if m["author"]["role"] == "assistant"]

    def chain(self, _conv: dict) -> list[dict]:
        return self.messages

    def message_text(self, msg: dict) -> str:
        return msg.get("text", "")

    def latest_reply(self, _conv: dict) -> str:
        return self.reply


class _FakeConvSession:
    def __init__(self, conv: dict) -> None:
        self.conv = conv

    def get_conversation(self, chat_id: str) -> dict:
        assert chat_id == "chat-123"
        return self.conv


def _wire_read_chat(
    monkeypatch, client: _FakeReadClient, conv: dict | None = None
) -> None:
    monkeypatch.setattr(read_chat, "load_client", lambda: client)
    monkeypatch.setattr(
        read_chat,
        "open_session",
        lambda *a, **k: _FakeConvSession(conv or {"title": "t"}),
    )


def test_text_flag_prints_the_latest_reply_and_exits_0_when_finished(
    monkeypatch, capsys
) -> None:
    client = _FakeReadClient(
        [_msg("user"), _msg("assistant", "done")], final=True, reply="the answer"
    )
    _wire_read_chat(monkeypatch, client)
    assert read_chat.main(["chat-123", "--text"]) == 0
    out = capsys.readouterr().out
    assert "the answer" in out
    assert "turn finished: yes, ready to collect" in out


def test_text_flag_says_no_visible_reply_yet_when_there_is_none(
    monkeypatch, capsys
) -> None:
    """An empty reply must read as "nothing yet", not print a blank line."""
    client = _FakeReadClient([_msg("user")], final=False, reply="")
    _wire_read_chat(monkeypatch, client)
    assert read_chat.main(["chat-123", "--text"]) == 1
    assert "(no visible reply yet)" in capsys.readouterr().out


def test_default_output_is_the_message_table_and_exits_1_when_not_finished(
    monkeypatch, capsys
) -> None:
    client = _FakeReadClient([_msg("user"), _msg("assistant", "partial")], final=False)
    _wire_read_chat(monkeypatch, client)
    assert read_chat.main(["chat-123"]) == 1
    out = capsys.readouterr().out
    assert "turn finished: no" in out
    assert "partial" in out  # the table, not the --text path


def test_default_output_message_table_exits_0_when_finished(
    monkeypatch, capsys
) -> None:
    client = _FakeReadClient([_msg("user"), _msg("assistant", "done")], final=True)
    _wire_read_chat(monkeypatch, client)
    assert read_chat.main(["chat-123", "--tail", "2"]) == 0
    assert "turn finished: yes" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# round_status.main -- branches test_commands.py's _workdir scenarios miss
# ---------------------------------------------------------------------------


def _workdir(
    tmp_path: Path,
    admitted: list[str],
    analysed: list[str],
    round_no: int = 1,
    run_id: str = "abc123",
) -> Path:
    """The on-disk layout round_status.main reads, rebuilt from the module."""
    work = tmp_path / "work"
    run = work / "runs" / run_id
    (run / "screen").mkdir(parents=True)
    (run / "analysis").mkdir(parents=True)
    (work / "chatgpt").mkdir(parents=True)
    (work / "workflow_state.json").write_text(
        json.dumps({"run_id": run_id, "round": round_no, "max_rounds": 4})
    )
    (work / "chatgpt" / "conversations.json").write_text("[]")
    (run / "screen" / "screened.jsonl").write_text(
        "".join(json.dumps({"paper_id": p}) + "\n" for p in admitted)
    )
    for pid in analysed:
        (run / "analysis" / f"{pid}_analysis.json").write_text("{}")
    return work


class _FakeTransportError(Exception):
    """Stands in for chatgpt_client.TransportError."""


class _FakeStatusClient:
    """Stands in for chatgpt_client, scoped to what round_status.main reads."""

    TransportError = _FakeTransportError

    def __init__(
        self, conversations: dict[str, dict], unreadable: set[str] = frozenset()
    ) -> None:
        self._conversations = conversations
        self._unreadable = unreadable

    def ChatGPTSession(self, browser: str) -> _FakeStatusSession:
        assert browser == "chrome"
        return _FakeStatusSession(self._conversations, self._unreadable)

    @staticmethod
    def tail_signature(conv: dict) -> tuple:
        return conv["_tail"]

    @staticmethod
    def assistant_text_messages(conv: dict) -> list:
        return conv["_replies"]


class _FakeStatusSession:
    def __init__(self, conversations: dict[str, dict], unreadable: set[str]) -> None:
        self._conversations = conversations
        self._unreadable = unreadable

    def get_conversation(self, chat_id: str) -> dict:
        if chat_id in self._unreadable:
            raise _FakeTransportError("boom")
        return self._conversations[chat_id]


def _sent(chat: str, job: str, round_no: int = 1) -> dict:
    return {"round": round_no, "status": "sent", "chat": chat, "job": job}


def test_a_finished_pending_reply_is_reported_ready_to_collect(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The check that stops a 30-minute wait for a reply already written."""
    work = _workdir(tmp_path, ["a"], ["a"])
    (work / "chatgpt" / "conversations.json").write_text(
        json.dumps([_sent("chat-1", "job-1")])
    )
    conv = {"_tail": ("node", 3, "text", True), "_replies": [{"x": 1}]}
    monkeypatch.setattr(round_status, "cc", _FakeStatusClient({"chat-1": conv}))
    monkeypatch.setattr(
        round_status,
        "open_session",
        lambda *a, **k: round_status.cc.ChatGPTSession("chrome"),
    )
    assert round_status.main(work) == 0
    out = capsys.readouterr().out
    assert "1 conversation(s) sent but not collected" in out
    assert "REPLY READY to collect" in out


def test_a_pending_reply_still_running_is_reported_as_such(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    work = _workdir(tmp_path, ["a"], ["a"])
    (work / "chatgpt" / "conversations.json").write_text(
        json.dumps([_sent("chat-1", "job-1")])
    )
    conv = {"_tail": ("node", 1, "text", False), "_replies": []}
    monkeypatch.setattr(round_status, "cc", _FakeStatusClient({"chat-1": conv}))
    monkeypatch.setattr(
        round_status,
        "open_session",
        lambda *a, **k: round_status.cc.ChatGPTSession("chrome"),
    )
    assert round_status.main(work) == 0
    assert "still running" in capsys.readouterr().out


def test_an_unreadable_pending_conversation_is_reported_not_raised(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A dead chat id must not crash the status check; it is expected traffic."""
    work = _workdir(tmp_path, ["a"], ["a"])
    (work / "chatgpt" / "conversations.json").write_text(
        json.dumps([_sent("chat-1", "job-1")])
    )
    monkeypatch.setattr(
        round_status, "cc", _FakeStatusClient({}, unreadable={"chat-1"})
    )
    monkeypatch.setattr(
        round_status,
        "open_session",
        lambda *a, **k: round_status.cc.ChatGPTSession("chrome"),
    )
    assert round_status.main(work) == 0
    out = capsys.readouterr().out
    assert "job-1" in out
    assert "unreadable: boom" in out


def test_a_sent_conversation_from_a_different_round_is_not_pending(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Only this round's in-flight sends are outstanding; an old one is not."""
    work = _workdir(tmp_path, ["a"], ["a"], round_no=2)
    (work / "chatgpt" / "conversations.json").write_text(
        json.dumps([_sent("chat-1", "job-1", round_no=1)])
    )
    monkeypatch.setattr(round_status, "cc", _FakeStatusClient({}))
    monkeypatch.setattr(
        round_status,
        "open_session",
        lambda *a, **k: round_status.cc.ChatGPTSession("chrome"),
    )
    assert round_status.main(work) == 0
    assert "sent but not collected" not in capsys.readouterr().out


def test_a_run_directory_that_was_never_created_is_nothing_admitted_yet(
    tmp_path: Path, capsys
) -> None:
    """round_state's own file checks must not crash on a run dir that is absent."""
    work = tmp_path / "work"
    (work / "chatgpt").mkdir(parents=True)
    (work / "workflow_state.json").write_text(
        json.dumps({"run_id": "ghost", "round": 1, "max_rounds": 4})
    )
    (work / "chatgpt" / "conversations.json").write_text("[]")
    assert round_status.main(work) == 0
    assert "admitted nothing yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# clean_chats.main -- the one remaining branch: nothing to act on
# ---------------------------------------------------------------------------

_CLEAN_CHATS = [
    {"id": "aaa", "title": "rp msgloom-topic-04 · terminology"},
    {"id": "bbb", "title": "Holiday planning"},
]


def test_main_reports_nothing_matches_and_exits_0_without_touching_anything(
    monkeypatch, capsys
) -> None:
    """An empty selection must say so and stop, not print an empty apply."""

    class _Session:
        def list_conversations(self, limit: int = 0) -> list[dict]:
            return _CLEAN_CHATS

        def delete(self, chat: str) -> None:
            raise AssertionError("nothing matched; delete must never be called")

    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: _Session())
    assert clean_chats.main(["--match", "no-such-title-anywhere", "--delete"]) == 0
    out = capsys.readouterr().out
    assert "nothing matches 'no-such-title-anywhere'" in out
