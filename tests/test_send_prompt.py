"""Tests for send_prompt.py, the skill's own send entry point (ROADMAP.md,
Stage 3, "Decisions of 2026-09-20" #2: the research-pipeline orchestrator is
not modified any more, so a run or a test sends through here).

``main()`` is exercised over a fake ``BrowserSender`` and a fake
``ChatGPTSession``, never a real Playwright page or a real HTTP call:
``cc.BrowserSender`` and ``cc.ChatGPTSession`` are monkeypatched on the
shared ``chatgpt_client`` module -- the same object ``send_prompt.py``'s own
``cc = load_client()`` returns, and the same one ``_common.open_session``
calls through to -- and ``cc.resolve_new_conversation`` / ``cc.wait_for_reply``
/ ``cc.new_chat_lock`` are monkeypatched the same way. Every test names the
failure it defends against, matching ``test_commands.py``; a fake that
raises ``AssertionError`` if it is ever called (``_boom``) proves a step was
skipped, the same technique ``tests/test_stage1.py`` uses for ``--help``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import send_prompt  # noqa: E402

REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROVISIONAL_ID = f"WEB:{REAL_ID}"


def _boom(*a, **kw):
    raise AssertionError("must not run for a refused invocation or skipped step")


class _NoopCM:
    """A context manager that opens and closes nothing, for new_chat_lock."""

    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


class FakeSender:
    """A ``BrowserSender`` stand-in: plays both the constructor ``cc.BrowserSender``
    is and the context-managed object it returns, since a real send only ever
    opens one per call. ``send()`` records what it was asked to send and
    returns ``result``, or raises ``raises`` if one was configured.
    """

    def __init__(self, result: str | None = None, raises: BaseException | None = None):
        self.result = result
        self.raises = raises
        self.init_kwargs: dict | None = None
        self.calls: list[dict] = []

    def __call__(
        self,
        browser,
        *,
        project="",
        effort="",
        model="",
        visible=False,
        search=False,
        record_send_body="",
        attachments=(),
    ):
        self.init_kwargs = {
            "browser": browser,
            "project": project,
            "effort": effort,
            "model": model,
            "visible": visible,
            "search": search,
            "record_send_body": record_send_body,
            "attachments": attachments,
        }
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text: str, chat: str | None = None, name: str = "task") -> str:
        self.calls.append({"text": text, "chat": chat, "name": name})
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeSession:
    """A ``ChatGPTSession`` stand-in: ``list_conversations`` and ``rename``."""

    def __init__(self, conversations: list[dict] | None = None):
        self.conversations = list(conversations or [])
        self.renamed: list[tuple[str, str]] = []

    def list_conversations(self, limit: int = 28, offset: int = 0) -> list[dict]:
        return self.conversations

    def rename(self, chat: str, title: str) -> None:
        self.renamed.append((chat, title))


def _prompt(tmp_path: Path, text: str = "hello there") -> Path:
    path = tmp_path / "prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# a new chat: sent, resolved, waited
# ---------------------------------------------------------------------------


def test_a_new_chat_is_resolved_and_waited_for(monkeypatch, tmp_path, capsys) -> None:
    fake_sender = FakeSender(result=PROVISIONAL_ID)
    fake_session = FakeSession(conversations=[{"id": "old-1"}])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    resolve_calls = []

    def fake_resolve(session, known_ids, since=0.0, **kw):
        resolve_calls.append((session, known_ids))
        return REAL_ID

    monkeypatch.setattr(cc, "resolve_new_conversation", fake_resolve)
    wait_calls = []

    def fake_wait(session, chat, timeout=1500.0, **kw):
        wait_calls.append((session, chat, timeout))
        return "hello back"

    monkeypatch.setattr(cc, "wait_for_reply", fake_wait)

    prompt = _prompt(tmp_path)
    out_json = tmp_path / "out.json"
    rc = send_prompt.main([str(prompt), "--json", str(out_json)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "1/3 sending as a new chat" in out
    assert "2/3 resolving the new conversation id" in out
    assert "3/3 waiting for the reply" in out
    assert "hello back" in out
    assert fake_sender.calls == [
        {"text": "hello there", "chat": None, "name": "prompt"}
    ]
    assert resolve_calls == [(fake_session, {"old-1"})]
    assert wait_calls == [(fake_session, REAL_ID, 1500.0)]

    doc = json.loads(out_json.read_text())
    assert doc["conversation_id"] == REAL_ID
    assert doc["url"] == f"https://chatgpt.com/c/{REAL_ID}"
    assert doc["resolved"] is True
    assert doc["reply"] == "hello back"
    assert doc["attachments"] == []
    assert "written to" in out


def test_a_new_chat_that_is_already_real_skips_resolving(monkeypatch, tmp_path) -> None:
    """The browser can hand back a real id directly; resolve must not run
    when there is nothing provisional to resolve."""
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    rc = send_prompt.main([str(_prompt(tmp_path)), "--project", "g-p-sandbox"])

    assert rc == 0
    assert fake_sender.init_kwargs["project"] == "g-p-sandbox"


# ---------------------------------------------------------------------------
# --no-wait
# ---------------------------------------------------------------------------


def test_no_wait_skips_the_reply_and_omits_it_from_the_json(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=PROVISIONAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", lambda *a, **kw: REAL_ID)
    monkeypatch.setattr(cc, "wait_for_reply", _boom)

    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [str(_prompt(tmp_path)), "--no-wait", "--json", str(out_json)]
    )

    assert rc == 0
    doc = json.loads(out_json.read_text())
    assert "reply" not in doc
    assert doc["conversation_id"] == REAL_ID


# ---------------------------------------------------------------------------
# --chat continues an existing conversation without resolving
# ---------------------------------------------------------------------------


def test_chat_continues_an_existing_conversation_without_resolving(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", _boom)
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    rc = send_prompt.main([str(_prompt(tmp_path, "more, please")), "--chat", REAL_ID])

    assert rc == 0
    assert fake_sender.calls == [
        {"text": "more, please", "chat": REAL_ID, "name": "prompt"}
    ]


# ---------------------------------------------------------------------------
# --title renames after the id is final
# ---------------------------------------------------------------------------


def test_title_renames_the_conversation_after_it_resolves(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=PROVISIONAL_ID)
    fake_session = FakeSession()
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", lambda *a, **kw: REAL_ID)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    rc = send_prompt.main([str(_prompt(tmp_path)), "--title", "rp-test send_prompt"])

    assert rc == 0
    assert fake_session.renamed == [(REAL_ID, "rp-test send_prompt")]


# ---------------------------------------------------------------------------
# failures: exit 1, one clear line, no traceback
# ---------------------------------------------------------------------------


def test_a_provisional_id_that_never_resolves_exits_1(
    monkeypatch, tmp_path, capsys
) -> None:
    def _never_resolves(*a, **kw):
        raise cc.TransportError("the posted message did not create a conversation")

    fake_sender = FakeSender(result=PROVISIONAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _never_resolves)
    monkeypatch.setattr(cc, "wait_for_reply", _boom)  # must never be reached

    rc = send_prompt.main([str(_prompt(tmp_path))])

    assert rc == 1
    assert "send failed" in capsys.readouterr().out


def test_a_sender_failure_prints_one_line_and_exits_1(
    monkeypatch, tmp_path, capsys
) -> None:
    """``BrowserSender``'s own rate-limit handling raises; it must be caught
    and reported in one line, never a traceback."""
    fake_sender = FakeSender(
        raises=cc.TransportError("ChatGPT is rate-limiting this account", 429)
    )
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())

    rc = send_prompt.main([str(_prompt(tmp_path))])

    assert rc == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "rate-limiting" in ln]
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# bad arguments -- exit 2, and nothing is touched
# ---------------------------------------------------------------------------


def test_a_missing_prompt_file_is_refused(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    missing = tmp_path / "nope.md"
    assert send_prompt.main([str(missing)]) == 2
    assert "no such prompt file" in capsys.readouterr().out


def test_an_unknown_effort_is_refused(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    prompt = _prompt(tmp_path)
    assert send_prompt.main([str(prompt), "--effort", "ultra"]) == 2
    assert "unknown thinking effort" in capsys.readouterr().out


def test_chat_together_with_project_is_refused(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    prompt = _prompt(tmp_path)
    rc = send_prompt.main([str(prompt), "--chat", REAL_ID, "--project", "g-p-x"])
    assert rc == 2
    assert "--chat" in capsys.readouterr().out


def test_help_exits_0_without_touching_a_session(monkeypatch) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    with pytest.raises(SystemExit) as excinfo:
        send_prompt.main(["--help"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# --attach -- uploaded through the sender, as resolved absolute paths
# ---------------------------------------------------------------------------


def test_attach_files_reach_the_sender_and_the_json_document_as_absolute_paths(
    monkeypatch, tmp_path
) -> None:
    """Attachments flow through the BrowserSender constructor, in the order
    given on the command line, resolved to absolute paths -- never through
    send()'s own arguments, which stay text/chat/name only."""
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    paper = tmp_path / "paper.pdf"
    paper.write_text("not a real pdf", encoding="utf-8")
    appendix = tmp_path / "appendix.pdf"
    appendix.write_text("not a real pdf either", encoding="utf-8")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main(
        [
            str(_prompt(tmp_path)),
            "--chat",
            REAL_ID,
            "--attach",
            str(paper),
            str(appendix),
            "--json",
            str(out_json),
        ]
    )

    assert rc == 0
    resolved = [str(paper.resolve()), str(appendix.resolve())]
    assert fake_sender.init_kwargs["attachments"] == resolved
    doc = json.loads(out_json.read_text())
    assert doc["attachments"] == resolved
    assert fake_sender.calls == [
        {"text": "hello there", "chat": REAL_ID, "name": "prompt"}
    ]


def test_a_missing_attachment_file_is_refused_before_any_session_opens(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    def _boom_open_session(*a, **kw):
        raise AssertionError("open_session must not run for a refused invocation")

    monkeypatch.setattr(send_prompt, "open_session", _boom_open_session)

    missing = tmp_path / "nope.pdf"
    rc = send_prompt.main([str(_prompt(tmp_path)), "--attach", str(missing)])

    assert rc == 2
    assert "no such attachment file" in capsys.readouterr().out


def test_a_missing_attachment_among_several_is_refused_too(
    monkeypatch, tmp_path, capsys
) -> None:
    """The first bad path stops the whole send, whatever position it is in."""
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    present = tmp_path / "present.pdf"
    present.write_text("here", encoding="utf-8")
    missing = tmp_path / "nope.pdf"

    rc = send_prompt.main(
        [str(_prompt(tmp_path)), "--attach", str(present), str(missing)]
    )

    assert rc == 2
    assert "no such attachment file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# effort / model / visible reach the sender unchanged
# ---------------------------------------------------------------------------


def test_effort_model_and_visible_reach_the_sender(monkeypatch, tmp_path) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    rc = send_prompt.main(
        [
            str(_prompt(tmp_path)),
            "--chat",
            REAL_ID,
            "--effort",
            "max",
            "--model",
            "gpt-6-pro",
            "--visible",
            "--browser",
            "chrome",
        ]
    )

    assert rc == 0
    assert fake_sender.init_kwargs == {
        "browser": "chrome",
        "project": "",
        "effort": "max",
        "model": "gpt-6-pro",
        "visible": True,
        "search": False,
        "record_send_body": "",
        "attachments": [],
    }


# ---------------------------------------------------------------------------
# --search / --record-send-body -- reach the sender and the --json document
# ---------------------------------------------------------------------------


def test_search_and_record_send_body_reach_the_sender_and_the_json_document(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    body_path = tmp_path / "send-body.json"
    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [
            str(_prompt(tmp_path)),
            "--chat",
            REAL_ID,
            "--search",
            "--record-send-body",
            str(body_path),
            "--json",
            str(out_json),
        ]
    )

    assert rc == 0
    assert fake_sender.init_kwargs["search"] is True
    assert fake_sender.init_kwargs["record_send_body"] == str(body_path)
    doc = json.loads(out_json.read_text())
    assert doc["search"] is True
    assert doc["send_body_file"] == str(body_path)


def test_search_and_record_send_body_default_off_in_the_json_document(
    monkeypatch, tmp_path
) -> None:
    """Neither flag given: search is False and send_body_file is null, not
    an empty string -- so a reader can tell "not recorded" from "recorded
    at an empty path"."""
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [str(_prompt(tmp_path)), "--chat", REAL_ID, "--json", str(out_json)]
    )

    assert rc == 0
    assert fake_sender.init_kwargs["search"] is False
    assert fake_sender.init_kwargs["record_send_body"] == ""
    doc = json.loads(out_json.read_text())
    assert doc["search"] is False
    assert doc["send_body_file"] is None
