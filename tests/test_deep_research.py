"""Tests for scripts/deep_research.py (ROADMAP.md, Stage 3 item 4, "Deep
research through system_hints" and the entries that follow it, plus the
headless ``start`` measured 2026-09-20): ``start`` calling the connector
directly over MCP (no browser, no ``send_prompt.py``) is exercised
alongside the original ``--project`` send path, and everything that
happens after either one -- polling ``get_state`` and exporting the
finished report -- all plain HTTP over a fake session.

``main()`` is exercised with ``send_prompt.main`` and ``open_session``
monkeypatched on ``deep_research`` itself (the same names its own ``import
send_prompt`` and ``from _common import ... open_session`` bind), never a
real HTTP call. ``_wait_for_done`` is exercised directly, the way
``chatgpt_client.wait_for_reply`` is tested elsewhere in this suite: a
fake monotonic clock and an injected ``sleep`` that advances it, so a
timeout test costs no real time. Every test names the failure it defends
against, matching ``test_commands.py``; ``_boom`` proves a step was
skipped, the same technique ``test_send_prompt.py`` uses for refusals and
``--help``.
"""

from __future__ import annotations

import base64
import json
import sys
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import deep_research  # noqa: E402
import send_prompt  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

RUN = {
    "conversation_id": "conv-123",
    "session_id": "sess-abc",
    "message_id": "toolcall-1",
}

RUNNING_STATE = {
    "_meta": {
        "deep_research_widget_messages": [
            {
                "author": {"role": "assistant", "name": None},
                "content": {"content_type": "thoughts"},
                "metadata": {"reasoning_title": "Planning the research"},
            }
        ],
        "source_searches": [],
    },
    "content": [],
    "isError": False,
}

DONE_STATE = {
    "_meta": {
        "deep_research_widget_messages": [
            *RUNNING_STATE["_meta"]["deep_research_widget_messages"],
            {
                "author": {"role": "assistant", "name": None},
                "content": {"content_type": "text", "parts": []},
                "metadata": {
                    "reasoning_title": "Generated report on Placeholder Topic"
                },
            },
        ],
        "source_searches": [],
    },
    "content": [],
    "isError": False,
}

ERROR_RESULT = {
    "_meta": {
        "openai/http_status": 403,
        "openai/tool_error_kind": "mcp_not_allowed_in_shared_conversation",
    },
    "content": [{"type": "text", "text": "This conversation is not allowed."}],
    "isError": True,
}

# No message has a reasoning_title yet -- the first moments of a run, before
# any reasoning step has been titled.
NO_TITLES_STATE = {
    "_meta": {
        "deep_research_widget_messages": [
            {"author": {"role": "tool", "name": "web.run"}, "metadata": {}},
        ],
        "source_searches": [],
    },
    "content": [],
    "isError": False,
}

# A headless "start" MCP result (module docstring, THE MEASURED FACTS
# 2026-09-20): the new session id rides in structuredContent.
START_RESULT = {
    "isError": False,
    "structuredContent": {
        "session_id": "sess-new-1",
        "connector_settings": {"effort": "standard"},
    },
    "content": [],
    "_meta": {
        "async_task_conversation_id": "conv-async-1",
        "openai/widgetSessionId": "widget-1",
        "websocket_url": "wss://example.invalid/ws",
    },
}

# The same shape, but structuredContent carries no session_id at all --
# neither the direct read nor the find_key fallback can find one.
NO_SESSION_ID_START_RESULT = {
    "isError": False,
    "structuredContent": {"connector_settings": {"effort": "standard"}},
    "content": [],
    "_meta": {},
}


def _boom(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("must not run for a refused invocation or skipped step")


def _build_tiny_docx(paragraphs: list[str]) -> bytes:
    """A minimal .docx: one ``word/document.xml`` with one ``<w:p>`` per
    paragraph, no escaping applied to the caller's text (tests that need
    an entity pass it already-escaped, e.g. ``"a &amp; b"``)."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<w:document xmlns:w="
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        + "</w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


EXPORT_RESULT = {
    "_meta": {
        "content_disposition": (
            "attachment; filename=\"Report.docx\"; filename*=UTF-8''Report.docx"
        ),
        "encoded_data": base64.b64encode(
            _build_tiny_docx(["Para one.", "Para two."])
        ).decode("ascii"),
    },
    "content": [],
    "structuredContent": {"ok": True},
    "isError": False,
    "result": "",
    "meta": {},
}


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write_run(tmp_path: Path, **overrides: Any) -> Path:
    data = {**RUN, **overrides}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _conversation_with_tool_call(message_id: str = "toolcall-1") -> dict[str, Any]:
    return {
        "mapping": {
            "node-0": {
                "message": {
                    "id": "user-msg-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["research this"]},
                    "recipient": "all",
                },
                "parent": None,
            },
            "node-1": {
                "message": {
                    "id": message_id,
                    "author": {"role": "assistant", "name": None},
                    "content": {"content_type": "code"},
                    "recipient": "api_tool.call_tool",
                },
                "parent": "node-0",
            },
        },
        "current_node": "node-1",
    }


class _GetConversationSession:
    """A ``ChatGPTSession`` stand-in exposing only ``get_conversation``,
    the one method ``start`` needs."""

    def __init__(self, conversation: dict[str, Any]):
        self.conversation = conversation
        self.requested: list[str] = []

    def get_conversation(self, chat: str) -> dict[str, Any]:
        self.requested.append(chat)
        return self.conversation


class _Backend:
    """Fake ``session.session.call``: the Nth call consumes ``responses[N-1]``."""

    def __init__(self, responses: list[tuple[int, Any]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int = 3,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        return self.responses[len(self.calls) - 1]


class _Session:
    """A ``ChatGPTSession`` stand-in exposing only ``.session.call``, the
    one thing ``status`` and ``export`` need."""

    def __init__(self, backend: _Backend):
        self.session = backend


class _FakeClock:
    """A monotonic clock and a sleep that advances it, with no real delay
    (``test_client_session.py``'s own pattern)."""

    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fake_send_prompt_main(doc: dict[str, Any] | None, rc: int = 0):
    """A ``send_prompt.main`` stand-in: writes ``doc`` to the ``--json``
    path it was given (``None`` writes nothing, simulating a bug where the
    doc never lands) and records every argv it was called with."""
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(list(argv))
        if doc is not None and "--json" in argv:
            out = Path(argv[argv.index("--json") + 1])
            out.write_text(json.dumps(doc), encoding="utf-8")
        return rc

    fake_main.calls = calls
    return fake_main


# ---------------------------------------------------------------------------
# mcp_body
# ---------------------------------------------------------------------------


def test_mcp_body_shapes_the_call_mcp_payload() -> None:
    body = deep_research.mcp_body(
        "get_state", {"session_id": "sess-1"}, "conv-1", "msg-1"
    )
    assert body == {
        "app_uri": "connectors://connector_openai_deep_research",
        "method": "tools/call",
        "params": {"name": "get_state", "arguments": {"session_id": "sess-1"}},
        "conversation_id": "conv-1",
        "message_id": "msg-1",
    }


# ---------------------------------------------------------------------------
# state_summary
# ---------------------------------------------------------------------------


def test_state_summary_reports_running_when_no_title_says_generated_report() -> None:
    summary = deep_research.state_summary(RUNNING_STATE)
    assert summary == {
        "messages": 1,
        "titles": ["Planning the research"],
        "done": False,
        "error": None,
    }


def test_state_summary_reports_done_once_a_title_starts_with_generated_report() -> None:
    summary = deep_research.state_summary(DONE_STATE)
    assert summary["done"] is True
    assert summary["titles"][-1] == "Generated report on Placeholder Topic"
    assert summary["messages"] == 2


def test_state_summary_keeps_only_the_last_three_titles() -> None:
    resp = {
        "_meta": {
            "deep_research_widget_messages": [
                {"metadata": {"reasoning_title": f"Step {i}"}} for i in range(5)
            ]
        },
        "isError": False,
    }
    summary = deep_research.state_summary(resp)
    assert summary["messages"] == 5
    assert summary["titles"] == ["Step 2", "Step 3", "Step 4"]


def test_state_summary_ignores_messages_with_no_reasoning_title() -> None:
    resp = {
        "_meta": {
            "deep_research_widget_messages": [
                {"author": {"role": "tool", "name": "web.run"}, "metadata": {}},
                {"metadata": {"reasoning_title": "Only this one"}},
            ]
        },
        "isError": False,
    }
    summary = deep_research.state_summary(resp)
    assert summary["messages"] == 2
    assert summary["titles"] == ["Only this one"]


def test_state_summary_surfaces_the_tool_s_own_error_text() -> None:
    summary = deep_research.state_summary(ERROR_RESULT)
    assert summary == {
        "messages": 0,
        "titles": [],
        "done": False,
        "error": "This conversation is not allowed.",
    }


def test_state_summary_falls_back_to_the_tool_error_kind_with_no_text() -> None:
    resp = {
        "_meta": {"openai/tool_error_kind": "some_kind"},
        "content": [],
        "isError": True,
    }
    assert deep_research.state_summary(resp)["error"] == "some_kind"


def test_state_summary_falls_back_to_a_generic_label_with_nothing_at_all() -> None:
    assert deep_research.state_summary({"isError": True})["error"] == "MCP call failed"


@pytest.mark.parametrize("resp", [None, "oops", 42, []])
def test_state_summary_reports_malformed_input_instead_of_raising(resp: Any) -> None:
    summary = deep_research.state_summary(resp)
    assert summary["error"] == "malformed response"
    assert summary["done"] is False
    assert summary["messages"] == 0


def test_state_summary_reads_the_recorded_get_state_fixture() -> None:
    resp = _fixture("deep_research_state.json")
    summary = deep_research.state_summary(resp)
    assert summary == {
        "messages": 5,
        "titles": [
            "Planning the research",
            "Worked for 42 seconds",
            "Generated report on Placeholder Topic",
        ],
        "done": True,
        "error": None,
    }


# ---------------------------------------------------------------------------
# start_session_id
# ---------------------------------------------------------------------------


def test_start_session_id_reads_structured_content_directly() -> None:
    assert deep_research.start_session_id(START_RESULT) == "sess-new-1"


def test_start_session_id_reads_the_recorded_start_fixture() -> None:
    resp = _fixture("deep_research_start.json")
    assert deep_research.start_session_id(resp) == "sess-live-9f2c"


def test_start_session_id_falls_back_to_find_key_when_the_shape_moves() -> None:
    resp = {
        "isError": False,
        "result": {"deeply": {"nested": {"session_id": "sess-fallback"}}},
    }
    assert deep_research.start_session_id(resp) == "sess-fallback"


def test_start_session_id_returns_none_when_absent_from_both_places() -> None:
    assert deep_research.start_session_id(NO_SESSION_ID_START_RESULT) is None


@pytest.mark.parametrize("resp", [None, "oops", 42, [], {}])
def test_start_session_id_returns_none_for_malformed_input(resp: Any) -> None:
    assert deep_research.start_session_id(resp) is None


# ---------------------------------------------------------------------------
# tool_call_message_id
# ---------------------------------------------------------------------------


def test_tool_call_message_id_finds_the_assistant_code_message() -> None:
    conv = _conversation_with_tool_call("toolcall-99")
    assert deep_research.tool_call_message_id(conv) == "toolcall-99"


def test_tool_call_message_id_ignores_a_user_message() -> None:
    conv = {
        "mapping": {
            "n": {
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text"},
                    "recipient": "all",
                }
            }
        }
    }
    assert deep_research.tool_call_message_id(conv) is None


def test_tool_call_message_id_ignores_an_assistant_text_message() -> None:
    conv = {
        "mapping": {
            "n": {
                "message": {
                    "id": "m1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text"},
                    "recipient": "all",
                }
            }
        }
    }
    assert deep_research.tool_call_message_id(conv) is None


@pytest.mark.parametrize(
    "conv", [{}, {"mapping": "not-a-dict"}, "not-a-dict", None, {"mapping": {}}]
)
def test_tool_call_message_id_returns_none_for_malformed_input(conv: Any) -> None:
    assert deep_research.tool_call_message_id(conv) is None


# ---------------------------------------------------------------------------
# parse_disposition
# ---------------------------------------------------------------------------


def test_parse_disposition_prefers_the_rfc5987_form_and_percent_decodes_it() -> None:
    header = (
        'attachment; filename="Report.docx"; '
        "filename*=UTF-8''Deep%20Research%20Report.docx"
    )
    assert deep_research.parse_disposition(header) == "Deep Research Report.docx"


def test_parse_disposition_falls_back_to_the_plain_form() -> None:
    assert deep_research.parse_disposition('attachment; filename="Report.docx"') == (
        "Report.docx"
    )


@pytest.mark.parametrize("header", ["attachment", "", None])
def test_parse_disposition_returns_empty_string_when_no_filename_is_present(
    header: Any,
) -> None:
    assert deep_research.parse_disposition(header) == ""


# ---------------------------------------------------------------------------
# decode_export
# ---------------------------------------------------------------------------


def test_decode_export_reads_the_filename_and_the_exact_bytes() -> None:
    data = _build_tiny_docx(["One.", "Two."])
    resp = {
        "_meta": {
            "content_disposition": 'attachment; filename="x.docx"',
            "encoded_data": base64.b64encode(data).decode("ascii"),
        }
    }
    filename, content = deep_research.decode_export(resp)
    assert filename == "x.docx"
    assert content == data


@pytest.mark.parametrize("resp", [{}, {"_meta": {}}, {"_meta": None}])
def test_decode_export_handles_missing_fields_without_raising(resp: Any) -> None:
    assert deep_research.decode_export(resp) == ("", b"")


def test_decode_export_reads_the_recorded_export_fixture() -> None:
    resp = _fixture("deep_research_export.json")
    filename, content = deep_research.decode_export(resp)
    assert filename == "Deep Research Report.docx"
    assert len(content) == 1066


# ---------------------------------------------------------------------------
# docx_text
# ---------------------------------------------------------------------------


def test_docx_text_extracts_two_paragraphs_tags_stripped_entities_unescaped() -> None:
    data = _build_tiny_docx(["Hello world.", "Second paragraph &amp; more."])
    text = deep_research.docx_text(data)
    assert text.splitlines() == ["Hello world.", "Second paragraph & more."]


def test_docx_text_reads_the_recorded_export_fixture_s_report_text() -> None:
    _filename, content = deep_research.decode_export(
        _fixture("deep_research_export.json")
    )
    text = deep_research.docx_text(content)
    assert text.splitlines() == [
        "Deep Research Report: Placeholder Topic",
        "This is a synthetic report body used only for testing docx_text "
        "extraction & parsing.",
        "Sources: [1] Example Source A. [2] Example Source B.",
    ]


# ---------------------------------------------------------------------------
# _load_run
# ---------------------------------------------------------------------------


def test_load_run_reads_a_valid_file(tmp_path: Path) -> None:
    path = _write_run(tmp_path)
    run = deep_research._load_run(str(path))
    assert run == RUN


def test_load_run_reports_a_missing_file(tmp_path: Path, capsys: Any) -> None:
    assert deep_research._load_run(str(tmp_path / "nope.json")) is None
    assert "could not read" in capsys.readouterr().out


def test_load_run_reports_invalid_json(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert deep_research._load_run(str(path)) is None
    assert "not valid JSON" in capsys.readouterr().out


def test_load_run_reports_a_json_value_that_is_not_an_object(
    tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert deep_research._load_run(str(path)) is None
    assert "not a JSON object" in capsys.readouterr().out


def test_load_run_reports_which_keys_are_missing(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"conversation_id": "c"}), encoding="utf-8")
    assert deep_research._load_run(str(path)) is None
    out = capsys.readouterr().out
    assert "session_id" in out
    assert "message_id" in out


# ---------------------------------------------------------------------------
# _wait_for_done
# ---------------------------------------------------------------------------


def test_wait_for_done_returns_0_immediately_when_already_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, DONE_STATE)])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == []
    assert len(backend.calls) == 1


def test_wait_for_done_polls_once_then_reports_done(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, RUNNING_STATE), (200, DONE_STATE)])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == [60.0]
    out = capsys.readouterr().out
    assert out.count("RUNNING") == 1
    assert out.count("DONE") == 1


def test_wait_for_done_prints_a_summary_only_when_it_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, RUNNING_STATE), (200, RUNNING_STATE), (200, DONE_STATE)])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert capsys.readouterr().out.count("messages:") == 2


def test_wait_for_done_backs_off_120s_on_a_429_then_tries_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(429, {"error": "slow down"}), (200, DONE_STATE)])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == [120.0]


def test_wait_for_done_exits_1_on_an_mcp_error(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, ERROR_RESULT)])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 1


def test_wait_for_done_exits_1_on_a_non_429_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(500, {"error": "boom"})])
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 1


def test_wait_for_done_times_out_and_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, RUNNING_STATE)] * 5)
    rc = deep_research._wait_for_done(
        _Session(backend), RUN, timeout=90.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 3


# ---------------------------------------------------------------------------
# main "status"
# ---------------------------------------------------------------------------


def test_status_done_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    assert deep_research.main(["status", "--run", str(run_path)]) == 0


def test_status_names_the_carrier_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    deep_research.main(["status", "--run", str(run_path)])
    out = capsys.readouterr().out
    assert f"carrier conversation: {RUN['conversation_id']}" in out


def test_status_running_exits_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, RUNNING_STATE)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    assert deep_research.main(["status", "--run", str(run_path)]) == 3


def test_status_mcp_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, ERROR_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 1
    assert "MCP error" in capsys.readouterr().out


def test_status_http_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(500, {"error": "boom"})])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 1
    assert "get_state failed" in capsys.readouterr().out


def test_status_prints_none_yet_when_no_reasoning_title_exists_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, NO_TITLES_STATE)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 3
    assert "last reasoning titles: (none yet)" in capsys.readouterr().out


def test_status_missing_run_file_exits_2_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    rc = deep_research.main(["status", "--run", str(tmp_path / "missing.json")])
    assert rc == 2


def test_status_run_without_conversation_id_exits_2_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    run_path = _write_run(tmp_path, conversation_id="")
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 2
    assert "conversation_id" in capsys.readouterr().out


def test_status_wait_dispatches_with_the_given_timeout_and_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: object())
    calls = []
    monkeypatch.setattr(
        deep_research,
        "_wait_for_done",
        lambda session, run, *, timeout, interval: calls.append((timeout, interval))
        or 0,
    )
    rc = deep_research.main(
        [
            "status",
            "--run",
            str(run_path),
            "--wait",
            "--timeout",
            "42",
            "--interval",
            "90",
        ]
    )
    assert rc == 0
    assert calls == [(42.0, 90.0)]


def test_status_wait_enforces_the_60s_minimum_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: object())
    calls = []
    monkeypatch.setattr(
        deep_research,
        "_wait_for_done",
        lambda session, run, *, timeout, interval: calls.append(interval) or 0,
    )
    deep_research.main(["status", "--run", str(run_path), "--wait", "--interval", "5"])
    assert calls == [60.0]
    assert "is below the minimum" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main "export"
# ---------------------------------------------------------------------------


def test_export_writes_the_file_and_prints_filename_and_byte_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE), (200, EXPORT_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.docx"

    rc = deep_research.main(["export", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 0
    expected = base64.b64decode(EXPORT_RESULT["_meta"]["encoded_data"])
    assert out_path.read_bytes() == expected
    out = capsys.readouterr().out
    assert "Report.docx" in out
    assert f"{len(expected)} bytes" in out
    assert [c[1] for c in backend.calls] == [deep_research.CALL_MCP] * 2
    assert backend.calls[0][2]["params"]["name"] == "get_state"
    assert backend.calls[1][2]["params"]["name"] == "export"
    assert backend.calls[1][2]["params"]["arguments"] == {
        "session_id": "sess-abc",
        "export_type": "docx",
    }


def test_export_with_text_writes_plain_text_and_prints_character_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE), (200, EXPORT_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.docx"
    text_path = tmp_path / "report.md"

    rc = deep_research.main(
        [
            "export",
            "--run",
            str(run_path),
            "--out",
            str(out_path),
            "--text",
            str(text_path),
        ]
    )

    assert rc == 0
    assert text_path.read_text(encoding="utf-8").splitlines() == [
        "Para one.",
        "Para two.",
    ]
    out = capsys.readouterr().out
    assert "characters ->" in out


def test_export_text_with_type_pdf_is_refused_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    run_path = _write_run(tmp_path)
    rc = deep_research.main(
        [
            "export",
            "--run",
            str(run_path),
            "--out",
            str(tmp_path / "r.pdf"),
            "--type",
            "pdf",
            "--text",
            str(tmp_path / "r.md"),
        ]
    )
    assert rc == 2


def test_export_missing_run_file_exits_2_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    rc = deep_research.main(
        [
            "export",
            "--run",
            str(tmp_path / "missing.json"),
            "--out",
            str(tmp_path / "r.docx"),
        ]
    )
    assert rc == 2


def test_export_run_without_conversation_id_exits_2_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    run_path = _write_run(tmp_path, conversation_id="")
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 2
    assert "conversation_id" in capsys.readouterr().out


def test_export_refuses_when_not_done_and_never_calls_export_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, RUNNING_STATE)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 3
    assert "--force" in capsys.readouterr().out
    assert len(backend.calls) == 1


def test_export_force_skips_the_get_state_check_entirely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, EXPORT_RESULT)])  # one response: get_state must not fire
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.docx"

    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(out_path), "--force"]
    )

    assert rc == 0
    assert len(backend.calls) == 1
    assert backend.calls[0][2]["params"]["name"] == "export"


def test_export_get_state_http_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(500, {"error": "boom"})])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 1


def test_export_get_state_mcp_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, ERROR_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 1


def test_export_call_http_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE), (500, {"error": "boom"})])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 1


def test_export_call_mcp_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(200, DONE_STATE), (200, ERROR_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 1
    assert "MCP error" in capsys.readouterr().out


def test_export_type_pdf_is_forwarded_to_the_mcp_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    pdf_result = {
        "_meta": {
            "content_disposition": 'attachment; filename="Report.pdf"',
            "encoded_data": base64.b64encode(b"%PDF-1.4 placeholder").decode("ascii"),
        },
        "isError": False,
    }
    backend = _Backend([(200, DONE_STATE), (200, pdf_result)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )

    rc = deep_research.main(
        [
            "export",
            "--run",
            str(run_path),
            "--out",
            str(tmp_path / "r.pdf"),
            "--type",
            "pdf",
        ]
    )

    assert rc == 0
    assert backend.calls[1][2]["params"]["arguments"]["export_type"] == "pdf"


# ---------------------------------------------------------------------------
# main "start"
# ---------------------------------------------------------------------------


def test_start_writes_run_json_with_the_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Research the thing.", encoding="utf-8")
    run_path = tmp_path / "run.json"

    fake_main = _fake_send_prompt_main(
        {"conversation_id": "conv-123", "session_id": "sess-abc"}
    )
    monkeypatch.setattr(send_prompt, "main", fake_main)
    fake_session = _GetConversationSession(_conversation_with_tool_call("toolcall-1"))
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: fake_session)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-sandbox",
            "--title",
            "rp-test deep research",
            "--run",
            str(run_path),
        ]
    )

    assert rc == 0
    run_doc = json.loads(run_path.read_text())
    assert run_doc["conversation_id"] == "conv-123"
    assert run_doc["session_id"] == "sess-abc"
    assert run_doc["message_id"] == "toolcall-1"
    assert run_doc["prompt_file"] == str(prompt)
    assert run_doc["title"] == "rp-test deep research"
    assert run_doc["mode"] == "send"
    assert "started_at" in run_doc
    assert fake_session.requested == ["conv-123"]

    argv = fake_main.calls[0]
    assert argv[0] == str(prompt)
    assert argv[argv.index("--project") + 1] == "g-p-sandbox"
    assert argv[argv.index("--system-hint") + 1] == deep_research.DEEP_RESEARCH_HINT
    assert "--no-wait" in argv
    assert argv[argv.index("--record-send-body") + 1] == f"{run_path}.body.json"
    assert argv[argv.index("--json") + 1] == f"{run_path}.send.json"
    assert argv[argv.index("--title") + 1] == "rp-test deep research"
    assert "--effort" not in argv
    assert "written to" in capsys.readouterr().out


def test_start_forwards_effort_without_a_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    fake_main = _fake_send_prompt_main(
        {"conversation_id": "conv-1", "session_id": "sess-1"}
    )
    monkeypatch.setattr(send_prompt, "main", fake_main)
    fake_session = _GetConversationSession(_conversation_with_tool_call())
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: fake_session)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--effort",
            "max",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 0
    argv = fake_main.calls[0]
    assert argv[argv.index("--effort") + 1] == "max"
    assert "--title" not in argv


def test_start_missing_prompt_file_exits_2_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(send_prompt, "main", _boom)
    monkeypatch.setattr(deep_research, "open_session", _boom)
    rc = deep_research.main(
        [
            "start",
            str(tmp_path / "nope.md"),
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )
    assert rc == 2


def test_start_propagates_a_send_prompt_failure_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    run_path = tmp_path / "run.json"
    monkeypatch.setattr(send_prompt, "main", lambda argv: 1)
    monkeypatch.setattr(deep_research, "open_session", _boom)

    rc = deep_research.main(
        ["start", str(prompt), "--project", "g-p-x", "--run", str(run_path)]
    )

    assert rc == 1
    assert not run_path.exists()


def test_start_propagates_a_bad_argument_exit_code_from_send_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(send_prompt, "main", lambda argv: 2)
    monkeypatch.setattr(deep_research, "open_session", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--effort",
            "not-a-level",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 2


def test_start_missing_session_id_exits_1_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(
        send_prompt,
        "main",
        _fake_send_prompt_main({"conversation_id": "conv-1", "session_id": None}),
    )
    monkeypatch.setattr(deep_research, "open_session", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "the stream carried no session id" in capsys.readouterr().out


def test_start_no_tool_call_message_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(
        send_prompt,
        "main",
        _fake_send_prompt_main({"conversation_id": "conv-1", "session_id": "sess-1"}),
    )
    empty_conversation = {"mapping": {}, "current_node": None}
    monkeypatch.setattr(
        deep_research,
        "open_session",
        lambda *a, **kw: _GetConversationSession(empty_conversation),
    )

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "no tool-call message found" in capsys.readouterr().out


def test_start_reports_a_send_record_that_was_never_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(send_prompt, "main", lambda argv: 0)  # never writes --json
    monkeypatch.setattr(deep_research, "open_session", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "could not read the send record" in capsys.readouterr().out


def test_start_reports_a_send_record_that_is_not_valid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")

    def fake_main(argv: list[str]) -> int:
        Path(argv[argv.index("--json") + 1]).write_text("{not json", encoding="utf-8")
        return 0

    monkeypatch.setattr(send_prompt, "main", fake_main)
    monkeypatch.setattr(deep_research, "open_session", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main "start" -- headless (--conversation), the default path
# ---------------------------------------------------------------------------


def test_start_headless_writes_run_json_from_a_fake_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Research the thing.", encoding="utf-8")
    run_path = tmp_path / "run.json"
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(deep_research.uuid, "uuid4", lambda: fixed_uuid)
    backend = _Backend([(200, START_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    monkeypatch.setattr(send_prompt, "main", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--conversation",
            "conv-carrier-1",
            "--run",
            str(run_path),
            "--title",
            "headless test",
        ]
    )

    assert rc == 0
    run_doc = json.loads(run_path.read_text())
    assert run_doc == {
        "conversation_id": "conv-carrier-1",
        "session_id": "sess-new-1",
        "message_id": str(fixed_uuid),
        "started_at": run_doc["started_at"],
        "prompt_file": str(prompt),
        "title": "headless test",
        "mode": "headless",
    }
    assert backend.calls == [
        (
            "POST",
            deep_research.CALL_MCP,
            {
                "app_uri": deep_research.APP_URI,
                "method": "tools/call",
                "params": {
                    "name": "start",
                    "arguments": {"user_query": "Research the thing."},
                },
                "conversation_id": "conv-carrier-1",
                "message_id": str(fixed_uuid),
            },
        )
    ]
    assert "written to" in capsys.readouterr().out


def test_start_headless_isError_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    backend = _Backend([(200, ERROR_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    monkeypatch.setattr(send_prompt, "main", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--conversation",
            "conv-1",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "This conversation is not allowed." in capsys.readouterr().out


def test_start_headless_http_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    backend = _Backend([(500, {"error": "boom"})])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    monkeypatch.setattr(send_prompt, "main", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--conversation",
            "conv-1",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "start failed" in capsys.readouterr().out


def test_start_headless_no_session_id_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    backend = _Backend([(200, NO_SESSION_ID_START_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    monkeypatch.setattr(send_prompt, "main", _boom)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--conversation",
            "conv-1",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 1
    assert "no session id" in capsys.readouterr().out


def test_start_both_conversation_and_project_exits_2_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    monkeypatch.setattr(send_prompt, "main", _boom)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--conversation",
            "conv-1",
            "--project",
            "g-p-x",
            "--run",
            str(tmp_path / "run.json"),
        ]
    )

    assert rc == 2
    assert "never both" in capsys.readouterr().out


def test_start_neither_conversation_nor_project_exits_2_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    monkeypatch.setattr(send_prompt, "main", _boom)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")

    rc = deep_research.main(["start", str(prompt), "--run", str(tmp_path / "run.json")])

    assert rc == 2
    assert "give --conversation" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --help, and the required arguments each subcommand claims to enforce
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["--help"], ["start", "--help"], ["status", "--help"], ["export", "--help"]],
)
def test_help_exits_0_and_never_opens_a_session(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    monkeypatch.setattr(send_prompt, "main", _boom)
    with pytest.raises(SystemExit) as exc:
        deep_research.main(argv)
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_module_help_states_the_carrier_conversation_facts(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "carrier conversation" in out
    assert "one research" in out
    assert "never added to or changed" in out


@pytest.mark.parametrize(
    "argv", [["start", "--help"], ["status", "--help"], ["export", "--help"]]
)
def test_each_subcommand_help_states_the_carrier_conversation_facts(
    argv: list[str], capsys: Any
) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(argv)
    out = " ".join(capsys.readouterr().out.split())
    assert "carrier conversation" in out
    assert "one research runs at a time per conversation" in out
    assert "conversation's own messages are never added to or changed" in out


def test_start_requires_run(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("hi", encoding="utf-8")
    with pytest.raises(SystemExit):
        deep_research.main(["start", str(prompt), "--conversation", "conv-1"])


def test_status_requires_run() -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["status"])


def test_export_requires_run_and_out(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["export", "--run", str(tmp_path / "run.json")])


def test_no_command_at_all_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        deep_research.main([])
    assert exc.value.code != 0
