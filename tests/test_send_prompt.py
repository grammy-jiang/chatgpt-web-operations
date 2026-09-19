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
REAL_ID_2 = "11111111-2222-3333-4444-555555555555"
PROVISIONAL_ID_2 = f"WEB:{REAL_ID_2}"


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
    opens one per call -- including a batch send, which must construct this
    exactly once and reuse it for every prompt (``construct_count``).

    ``send()`` records what it was asked to send. With ``results`` given, the
    Nth call consumes ``results[N-1]``: a string is returned, a
    ``BaseException`` instance is raised -- so a batch test can make one send
    among several fail without the rest. Without ``results``, every call
    returns ``result`` or raises ``raises``, as before.
    """

    def __init__(
        self,
        result: str | None = None,
        raises: BaseException | None = None,
        results: list[str | BaseException] | None = None,
    ):
        self.result = result
        self.raises = raises
        self.results = list(results) if results is not None else None
        self.init_kwargs: dict | None = None
        self.calls: list[dict] = []
        self.construct_count = 0

    def __call__(
        self,
        browser,
        *,
        project="",
        effort="",
        model="",
        visible=False,
        search=False,
        hints=(),
        record_send_body="",
        attachments=(),
    ):
        self.construct_count += 1
        self.init_kwargs = {
            "browser": browser,
            "project": project,
            "effort": effort,
            "model": model,
            "visible": visible,
            "search": search,
            "hints": hints,
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
        if self.results is not None:
            outcome = self.results[len(self.calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
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


def _prompt_file(tmp_path: Path, name: str, text: str) -> Path:
    """A second (or third, ...) prompt file, distinct from ``_prompt``'s
    fixed ``prompt.md``, for the batch-send tests."""
    path = tmp_path / name
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
        "hints": (),
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


# ---------------------------------------------------------------------------
# --system-hint -- the generic form of --search (ROADMAP.md, Stage 3 item 4)
# ---------------------------------------------------------------------------


def test_system_hint_reaches_the_sender_and_the_json_document(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [
            str(_prompt(tmp_path)),
            "--chat",
            REAL_ID,
            "--system-hint",
            "plugin:connector_openai_deep_research",
            "--json",
            str(out_json),
        ]
    )

    assert rc == 0
    assert fake_sender.init_kwargs["hints"] == (
        "plugin:connector_openai_deep_research",
    )
    doc = json.loads(out_json.read_text())
    assert doc["system_hints"] == ["plugin:connector_openai_deep_research"]


def test_repeated_system_hint_flags_are_collected_in_order(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    rc = send_prompt.main(
        [
            str(_prompt(tmp_path)),
            "--chat",
            REAL_ID,
            "--system-hint",
            "search",
            "--system-hint",
            "tasks",
        ]
    )

    assert rc == 0
    assert fake_sender.init_kwargs["hints"] == ("search", "tasks")


def test_system_hint_defaults_to_empty(monkeypatch, tmp_path) -> None:
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [str(_prompt(tmp_path)), "--chat", REAL_ID, "--json", str(out_json)]
    )

    assert rc == 0
    assert fake_sender.init_kwargs["hints"] == ()
    doc = json.loads(out_json.read_text())
    assert doc["system_hints"] == []


# ---------------------------------------------------------------------------
# more than one PROMPT_FILE -- one window, sent as new chats, waited and
# renamed only after it closes (ROADMAP.md, Stage 3, batch sends)
# ---------------------------------------------------------------------------


def test_a_single_prompt_file_still_writes_a_json_object_not_a_list(
    monkeypatch, tmp_path
) -> None:
    """Regression guard for the batch feature: exactly one PROMPT_FILE must
    keep today's single-document --json shape, not a one-item list."""
    fake_sender = FakeSender(result=REAL_ID)
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    out_json = tmp_path / "out.json"
    rc = send_prompt.main(
        [str(_prompt(tmp_path)), "--chat", REAL_ID, "--json", str(out_json)]
    )

    assert rc == 0
    doc = json.loads(out_json.read_text())
    assert isinstance(doc, dict)


def test_chat_with_more_than_one_prompt_file_is_refused(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cc, "BrowserSender", _boom)
    monkeypatch.setattr(cc, "ChatGPTSession", _boom)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2), "--chat", REAL_ID])

    assert rc == 2
    out = capsys.readouterr().out
    assert "--chat" in out
    assert "PROMPT_FILE" in out


def test_batch_opens_exactly_one_browser_sender_for_all_files(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # both already real
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2)])

    assert rc == 0
    assert fake_sender.construct_count == 1
    assert [c["text"] for c in fake_sender.calls] == ["first", "second"]
    assert [c["name"] for c in fake_sender.calls] == ["p1", "p2"]
    assert all(c["chat"] is None for c in fake_sender.calls)


def test_batch_writes_a_json_list_with_one_document_per_prompt(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # both already real

    def fake_wait(session, chat, timeout=1500.0, **kw):
        return f"reply-{chat}"

    monkeypatch.setattr(cc, "wait_for_reply", fake_wait)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main([str(p1), str(p2), "--json", str(out_json)])

    assert rc == 0
    docs = json.loads(out_json.read_text())
    assert isinstance(docs, list)
    assert len(docs) == 2
    assert docs[0]["conversation_id"] == REAL_ID
    assert docs[1]["conversation_id"] == REAL_ID_2
    assert docs[0]["reply"] == f"reply-{REAL_ID}"
    assert docs[1]["reply"] == f"reply-{REAL_ID_2}"
    assert "error" not in docs[0]
    assert "error" not in docs[1]


def test_batch_resolves_each_provisional_id_as_today(monkeypatch, tmp_path) -> None:
    """Two new chats, both provisional: each is resolved right after its own
    send, using the listing snapshot taken just before it -- exactly the
    single-send mechanism, called twice."""
    fake_sender = FakeSender(results=[PROVISIONAL_ID, PROVISIONAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())

    resolved = [REAL_ID, REAL_ID_2]
    resolve_calls = []

    def fake_resolve(session, known_ids, since=0.0, **kw):
        resolve_calls.append(known_ids)
        return resolved[len(resolve_calls) - 1]

    monkeypatch.setattr(cc, "resolve_new_conversation", fake_resolve)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main([str(p1), str(p2), "--json", str(out_json)])

    assert rc == 0
    assert len(resolve_calls) == 2
    docs = json.loads(out_json.read_text())
    assert [d["conversation_id"] for d in docs] == [REAL_ID, REAL_ID_2]
    assert all(d["resolved"] for d in docs)


def test_batch_one_failing_send_does_not_stop_the_second(monkeypatch, tmp_path) -> None:
    boom = cc.TransportError("ChatGPT is rate-limiting this account", 429)
    fake_sender = FakeSender(results=[boom, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # never reached
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main([str(p1), str(p2), "--json", str(out_json)])

    assert rc == 1
    assert fake_sender.calls == [
        {"text": "first", "chat": None, "name": "p1"},
        {"text": "second", "chat": None, "name": "p2"},
    ]
    docs = json.loads(out_json.read_text())
    assert len(docs) == 2
    assert docs[0]["conversation_id"] == ""
    assert "rate-limiting" in docs[0]["error"]
    assert "error" not in docs[1]
    assert docs[1]["conversation_id"] == REAL_ID_2
    assert docs[1]["reply"] == "ok"


def test_batch_waits_only_after_every_send_in_the_window_finished(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(results=[PROVISIONAL_ID, PROVISIONAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())

    resolved = [REAL_ID, REAL_ID_2]
    monkeypatch.setattr(
        cc, "resolve_new_conversation", lambda *a, **kw: resolved.pop(0)
    )

    calls_at_wait_time = []
    wait_order = []

    def fake_wait(session, chat, timeout=1500.0, **kw):
        calls_at_wait_time.append(len(fake_sender.calls))
        wait_order.append(chat)
        return f"reply-{chat}"

    monkeypatch.setattr(cc, "wait_for_reply", fake_wait)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2)])

    assert rc == 0
    # Both sends (and resolves) had already happened -- the window was
    # closed -- before the first wait_for_reply was even called.
    assert calls_at_wait_time == [2, 2]
    # And the replies were collected in the same order the prompts were
    # given, not e.g. reversed or interleaved.
    assert wait_order == [REAL_ID, REAL_ID_2]


def test_batch_title_becomes_a_numbered_prefix(monkeypatch, tmp_path) -> None:
    fake_session = FakeSession()
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # both already real
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2), "--title", "rp-test batch"])

    assert rc == 0
    assert fake_session.renamed == [
        (REAL_ID, "rp-test batch 1"),
        (REAL_ID_2, "rp-test batch 2"),
    ]


def test_batch_skips_rename_for_a_prompt_that_failed(monkeypatch, tmp_path) -> None:
    boom = cc.TransportError("send blew up")
    fake_session = FakeSession()
    fake_sender = FakeSender(results=[boom, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2), "--title", "rp-test batch"])

    assert rc == 1
    assert fake_session.renamed == [(REAL_ID_2, "rp-test batch 2")]


def test_batch_no_wait_still_renames(monkeypatch, tmp_path) -> None:
    fake_session = FakeSession()
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", _boom)  # must not run

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main(
        [
            str(p1),
            str(p2),
            "--no-wait",
            "--title",
            "rp-test batch",
            "--json",
            str(out_json),
        ]
    )

    assert rc == 0
    assert fake_session.renamed == [
        (REAL_ID, "rp-test batch 1"),
        (REAL_ID_2, "rp-test batch 2"),
    ]
    docs = json.loads(out_json.read_text())
    assert all("reply" not in d for d in docs)


def test_batch_warns_when_the_estimate_exceeds_the_browser_budget(
    monkeypatch, tmp_path, capsys
) -> None:
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")
    monkeypatch.setattr(cc, "BROWSER_MAX_SECONDS", 100.0)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2)])

    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "warning" in out
    assert "budget" in out


def test_batch_does_not_warn_comfortably_under_the_browser_budget(
    monkeypatch, tmp_path, capsys
) -> None:
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")

    rc = send_prompt.main([str(p1), str(p2)])

    assert rc == 0
    assert "warning" not in capsys.readouterr().out.lower()


def test_batch_attachments_reach_the_shared_sender_once(monkeypatch, tmp_path) -> None:
    """--attach is validated once and handed to the one shared sender, not
    re-validated or re-resolved per prompt."""
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    paper = tmp_path / "paper.pdf"
    paper.write_text("not a real pdf", encoding="utf-8")
    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main(
        [str(p1), str(p2), "--attach", str(paper), "--json", str(out_json)]
    )

    assert rc == 0
    assert fake_sender.construct_count == 1
    assert fake_sender.init_kwargs["attachments"] == [str(paper.resolve())]
    docs = json.loads(out_json.read_text())
    assert docs[0]["attachments"] == [str(paper.resolve())]
    assert docs[1]["attachments"] == [str(paper.resolve())]


def test_batch_records_the_same_error_for_every_prompt_when_the_window_never_opens(
    monkeypatch, tmp_path, capsys
) -> None:
    """The window itself (or the lock, or the session) fails before any
    prompt could be attempted: nothing was sent, so every prompt's document
    carries the one error, and nothing raises past main()."""

    class BoomOnEnter(FakeSender):
        def __enter__(self):
            raise cc.TransportError("could not open the browser window")

    fake_sender = BoomOnEnter(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)
    monkeypatch.setattr(cc, "wait_for_reply", _boom)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main([str(p1), str(p2), "--json", str(out_json)])

    assert rc == 1
    assert "send failed" in capsys.readouterr().out
    docs = json.loads(out_json.read_text())
    assert len(docs) == 2
    assert all("could not open the browser window" in d["error"] for d in docs)
    assert all(d["conversation_id"] == "" for d in docs)


def test_batch_a_failing_wait_does_not_stop_the_second_reply(
    monkeypatch, tmp_path
) -> None:
    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: FakeSession())
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # both already real

    def fake_wait(session, chat, timeout=1500.0, **kw):
        if chat == REAL_ID:
            raise TimeoutError("assistant did not finish within 1500 s")
        return "second reply"

    monkeypatch.setattr(cc, "wait_for_reply", fake_wait)

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main([str(p1), str(p2), "--json", str(out_json)])

    assert rc == 1
    docs = json.loads(out_json.read_text())
    assert "did not finish" in docs[0]["error"]
    assert docs[0]["reply"] is None
    assert "error" not in docs[1]
    assert docs[1]["reply"] == "second reply"


def test_batch_a_failing_rename_does_not_stop_the_second(monkeypatch, tmp_path) -> None:
    fake_session = FakeSession()

    def fake_rename(chat: str, title: str) -> None:
        if chat == REAL_ID:
            raise cc.TransportError("rename failed")
        fake_session.renamed.append((chat, title))

    fake_session.rename = fake_rename  # type: ignore[method-assign]

    fake_sender = FakeSender(results=[REAL_ID, REAL_ID_2])
    monkeypatch.setattr(cc, "BrowserSender", fake_sender)
    monkeypatch.setattr(cc, "ChatGPTSession", lambda browser: fake_session)
    monkeypatch.setattr(cc, "new_chat_lock", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(cc, "resolve_new_conversation", _boom)  # both already real
    monkeypatch.setattr(cc, "wait_for_reply", lambda *a, **kw: "ok")

    p1 = _prompt_file(tmp_path, "p1.md", "first")
    p2 = _prompt_file(tmp_path, "p2.md", "second")
    out_json = tmp_path / "out.json"

    rc = send_prompt.main(
        [str(p1), str(p2), "--title", "rp-test batch", "--json", str(out_json)]
    )

    assert rc == 1
    docs = json.loads(out_json.read_text())
    assert "rename failed" in docs[0]["error"]
    assert "error" not in docs[1]
    assert fake_session.renamed == [(REAL_ID_2, "rp-test batch 2")]
