"""Tests for scripts/deep_research.py: the widget path (``start``,
``status``, ``fetch``) and the older MCP fallback (``export``), all
plain HTTP over a fake session (module docstring, THE EXACT SHAPES,
2026-09-20: a Deep research run is an ordinary chat that runs longer;
its widget state lives on one tool message's
``metadata.chatgpt_sdk.widget_state``, a JSON string, inside ``GET
/backend-api/conversations/<id>``).

``main()`` is exercised with ``send_prompt.main`` and ``open_session``
monkeypatched on ``deep_research`` itself (the same names its own
``import send_prompt`` and ``from _common import ... open_session``
bind), never a real HTTP call -- ``conftest.py``'s socket guard would
fail the test outright if one slipped through. ``_wait_for_status`` is
exercised directly, with a fake monotonic clock and an injected
``sleep`` that advances it, so a timeout test costs no real time
(``test_client_session.py``'s own pattern). Every test names the
failure it defends against, matching ``test_commands.py``; ``_boom``
proves a step was skipped, the same technique ``test_send_prompt.py``
uses for refusals and ``--help``.
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


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _boom(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("must not run for a refused invocation or skipped step")


def _write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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
    one thing every command in this module needs."""

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
# Widget-path fixtures: conversations and their (already parsed) states
# ---------------------------------------------------------------------------


def _conversation_with_state(
    state: dict[str, Any] | None, conversation_id: str = "conv-inline"
) -> dict[str, Any]:
    """A minimal ``GET /backend-api/conversations/<id>`` payload carrying
    ``state`` as its one widget tool message, or none at all when
    ``state`` is ``None`` -- an ordinary chat with no Deep research
    widget."""
    messages = [
        {
            "author": {"role": "user", "name": None},
            "content": {"content_type": "text", "parts": ["hi"]},
            "metadata": {},
        }
    ]
    if state is not None:
        messages.append(
            {
                "author": {"role": "tool", "name": "chatgpt_sdk_widget"},
                "content": {"content_type": "text", "parts": [""]},
                "metadata": {"chatgpt_sdk": {"widget_state": json.dumps(state)}},
            }
        )
    return {"messages": messages, "conversation_id": conversation_id}


RUNNING_STATE = {
    "status": "in_progress",
    "plan": {
        "plan_id": "plan-r",
        "version": 1,
        "title": "Research Plan: Running Topic",
        "steps": [
            {"id": "s1", "text": "Survey background sources", "status": "completed"},
            {
                "id": "s2",
                "text": "Cross-check contested claims",
                "status": "in_progress",
            },
            {"id": "s3", "text": "Draft the executive summary", "status": "pending"},
        ],
    },
    "report_message": None,
    "research_started_at": "2026-09-20T10:00:00+00:00",
    "research_stopped_at": None,
    "step_statuses_by_plan": {"plan-r": {"s1": "completed", "s2": "in_progress"}},
    "last_updated_at": "2026-09-20T10:00:45+00:00",
    "waiting_for_user_response_on_plan_until": None,
}

DONE_STATE = {
    "status": "completed",
    "plan": {
        "plan_id": "plan-d",
        "version": 1,
        "title": "Research Plan: Done Topic",
        "steps": [
            {"id": "s1", "text": "Survey background sources", "status": "completed"},
        ],
    },
    "report_message": {
        "author": {"role": "assistant", "name": None},
        "content": {"content_type": "text", "parts": ["# Report\n\nBody text."]},
        "metadata": {
            "search_result_groups": [],
            "citations": [],
            "resolved_model_slug": "gpt-5-deep-research",
        },
        "end_turn": True,
    },
    "research_started_at": "2026-09-20T11:00:00+00:00",
    "research_stopped_at": "2026-09-20T11:01:20+00:00",
    "step_statuses_by_plan": {"plan-d": {"s1": "completed"}},
    "last_updated_at": "2026-09-20T11:01:20+00:00",
    "waiting_for_user_response_on_plan_until": None,
}

WAITING_STATE = {
    "status": "in_progress",
    "plan": {
        "plan_id": "plan-w",
        "version": 1,
        "title": "Research Plan: Waiting Topic",
        "steps": [{"id": "s1", "text": "Confirm the plan", "status": "pending"}],
    },
    "report_message": None,
    "research_started_at": "2026-09-20T12:00:00+00:00",
    "research_stopped_at": None,
    "step_statuses_by_plan": {"plan-w": {"s1": "pending"}},
    "last_updated_at": "2026-09-20T12:00:05+00:00",
    "waiting_for_user_response_on_plan_until": "2026-09-20T12:05:00+00:00",
}

RUNNING_CONVERSATION = _conversation_with_state(RUNNING_STATE, "conv-running")
DONE_CONVERSATION = _conversation_with_state(DONE_STATE, "conv-done")
WAITING_CONVERSATION = _conversation_with_state(WAITING_STATE, "conv-waiting")
NO_WIDGET_CONVERSATION = _conversation_with_state(None, "conv-plain")

RUN = {"conversation_id": "conv-123"}


def _write_run(tmp_path: Path, **overrides: Any) -> Path:
    data = {**RUN, **overrides}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# find_widget_state
# ---------------------------------------------------------------------------


def test_find_widget_state_reads_the_completed_fixture() -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    state = deep_research.find_widget_state(conv)
    assert state is not None
    assert state["status"] == "completed"
    assert state["plan"]["title"] == "Research Plan: Placeholder Topic"


def test_find_widget_state_reads_the_running_fixture() -> None:
    conv = _fixture("deep_research_conversation_running.json")
    state = deep_research.find_widget_state(conv)
    assert state is not None
    assert state["status"] == "in_progress"


def test_find_widget_state_returns_none_for_a_conversation_with_no_widget() -> None:
    conv = _fixture("deep_research_conversation_no_widget.json")
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_returns_none_for_malformed_widget_json() -> None:
    conv = _fixture("deep_research_conversation_malformed.json")
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_returns_none_when_messages_is_missing() -> None:
    assert deep_research.find_widget_state({"title": "no messages key"}) is None


def test_find_widget_state_returns_none_when_messages_is_not_a_list() -> None:
    assert deep_research.find_widget_state({"messages": "oops"}) is None


@pytest.mark.parametrize("conv", [None, "oops", 42, [], {"messages": None}])
def test_find_widget_state_tolerates_malformed_input_instead_of_raising(
    conv: Any,
) -> None:
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_skips_a_non_dict_message() -> None:
    conv = {"messages": ["not a dict", {"author": {"role": "tool"}}]}
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_skips_a_tool_message_with_unrelated_metadata() -> None:
    conv = {
        "messages": [
            {
                "author": {"role": "tool", "name": "web.run"},
                "metadata": {"search_result_groups": []},
            }
        ]
    }
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_skips_a_non_string_widget_state() -> None:
    conv = {
        "messages": [
            {
                "author": {"role": "tool"},
                "metadata": {"chatgpt_sdk": {"widget_state": {"already": "a dict"}}},
            }
        ]
    }
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_skips_a_widget_state_that_parses_to_a_non_object() -> None:
    conv = {
        "messages": [
            {
                "author": {"role": "tool"},
                "metadata": {"chatgpt_sdk": {"widget_state": "[1, 2, 3]"}},
            }
        ]
    }
    assert deep_research.find_widget_state(conv) is None


def test_find_widget_state_the_last_of_two_carriers_wins() -> None:
    conv = {
        "messages": [
            {
                "author": {"role": "tool"},
                "metadata": {
                    "chatgpt_sdk": {"widget_state": json.dumps({"status": "first"})}
                },
            },
            {
                "author": {"role": "tool"},
                "metadata": {
                    "chatgpt_sdk": {"widget_state": json.dumps({"status": "second"})}
                },
            },
        ]
    }
    assert deep_research.find_widget_state(conv) == {"status": "second"}


# ---------------------------------------------------------------------------
# state_verdict
# ---------------------------------------------------------------------------


def test_state_verdict_done_reads_the_completed_state() -> None:
    verdict = deep_research.state_verdict(DONE_STATE)
    assert verdict["status"] == "completed"
    assert verdict["done"] is True
    assert verdict["waiting"] is False
    assert verdict["started_at"] == "2026-09-20T11:00:00+00:00"
    assert verdict["stopped_at"] == "2026-09-20T11:01:20+00:00"
    assert verdict["title"] == "Research Plan: Done Topic"
    assert verdict["steps"] == [
        {"id": "s1", "text": "Survey background sources", "status": "completed"}
    ]


def test_state_verdict_running_is_not_done_and_not_waiting() -> None:
    verdict = deep_research.state_verdict(RUNNING_STATE)
    assert verdict["done"] is False
    assert verdict["waiting"] is False
    assert verdict["stopped_at"] is None
    assert len(verdict["steps"]) == 3
    assert verdict["steps"][1]["status"] == "in_progress"
    assert verdict["steps"][2]["status"] == "pending"


def test_state_verdict_waiting_is_true_when_the_field_is_set() -> None:
    verdict = deep_research.state_verdict(WAITING_STATE)
    assert verdict["done"] is False
    assert verdict["waiting"] is True


def test_state_verdict_keeps_a_steps_own_reason_when_present() -> None:
    state = {
        "status": "in_progress",
        "plan": {
            "title": "T",
            "steps": [
                {
                    "id": "s1",
                    "text": "Skip a dead end",
                    "status": "skipped",
                    "reason": "source unreachable",
                }
            ],
        },
    }
    verdict = deep_research.state_verdict(state)
    assert verdict["steps"] == [
        {
            "id": "s1",
            "text": "Skip a dead end",
            "status": "skipped",
            "reason": "source unreachable",
        }
    ]


def test_state_verdict_omits_reason_when_the_step_carries_none() -> None:
    state = {"plan": {"steps": [{"id": "s1", "text": "T", "status": "pending"}]}}
    verdict = deep_research.state_verdict(state)
    assert "reason" not in verdict["steps"][0]


def test_state_verdict_skips_a_non_dict_step() -> None:
    state = {
        "plan": {"steps": ["not a dict", {"id": "s1", "text": "T", "status": "x"}]}
    }
    verdict = deep_research.state_verdict(state)
    assert len(verdict["steps"]) == 1


def test_state_verdict_reads_the_completed_fixture_end_to_end() -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    state = deep_research.find_widget_state(conv)
    verdict = deep_research.state_verdict(state)
    assert verdict["done"] is True
    assert len(verdict["steps"]) == 3


@pytest.mark.parametrize(
    "state", [None, "oops", 42, [], {}, {"plan": "oops"}, {"plan": {"steps": "oops"}}]
)
def test_state_verdict_tolerates_malformed_input_instead_of_raising(state: Any) -> None:
    verdict = deep_research.state_verdict(state)
    assert verdict == {
        "status": None,
        "done": False,
        "waiting": False,
        "started_at": None,
        "stopped_at": None,
        "steps": [],
        "title": None,
    }


# ---------------------------------------------------------------------------
# report_markdown
# ---------------------------------------------------------------------------


def test_report_markdown_reads_the_completed_fixture_verbatim() -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    state = deep_research.find_widget_state(conv)
    report = deep_research.report_markdown(state)
    assert report == (
        "# Deep Research Report: Placeholder Topic\n"
        "\n"
        "## Executive summary\n"
        "\n"
        "This is a **synthetic** report used only to test `fetch`. See "
        "[Example Source A](https://a.example.invalid/one) for background."
    )
    assert not report.endswith("\n")


def test_report_markdown_is_none_while_running() -> None:
    assert deep_research.report_markdown(RUNNING_STATE) is None


def test_report_markdown_keeps_a_trailing_blank_line_exactly() -> None:
    state = {
        "report_message": {"content": {"content_type": "text", "parts": ["Body.\n\n"]}}
    }
    assert deep_research.report_markdown(state) == "Body.\n\n"


def test_report_markdown_keeps_an_empty_report_as_an_empty_string() -> None:
    state = {"report_message": {"content": {"content_type": "text", "parts": [""]}}}
    assert deep_research.report_markdown(state) == ""


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"report_message": None},
        {"report_message": "oops"},
        {"report_message": {}},
        {"report_message": {"content": None}},
        {"report_message": {"content": "oops"}},
        {"report_message": {"content": {}}},
        {"report_message": {"content": {"parts": "oops"}}},
        {"report_message": {"content": {"parts": []}}},
        {"report_message": {"content": {"parts": [123]}}},
    ],
)
def test_report_markdown_handles_malformed_shapes_without_raising(state: Any) -> None:
    assert deep_research.report_markdown(state) is None


@pytest.mark.parametrize("state", [None, "oops", 42, []])
def test_report_markdown_tolerates_a_non_dict_state(state: Any) -> None:
    assert deep_research.report_markdown(state) is None


# ---------------------------------------------------------------------------
# sources_lines
# ---------------------------------------------------------------------------


def test_sources_lines_reads_the_completed_fixture_deduplicated() -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    state = deep_research.find_widget_state(conv)
    lines = deep_research.sources_lines(state)
    assert lines == [
        "a.example.invalid | Example Source A | https://a.example.invalid/one",
        "b.example.invalid | Example Source B | https://b.example.invalid/two",
    ]


def test_sources_lines_is_empty_while_running() -> None:
    assert deep_research.sources_lines(RUNNING_STATE) == []


def test_sources_lines_dedups_the_exact_triple_not_just_the_url() -> None:
    state = {
        "report_message": {
            "metadata": {
                "search_result_groups": [
                    {
                        "domain": "x.example.invalid",
                        "entries": [
                            {
                                "title": "First Title",
                                "url": "https://x.example.invalid/p",
                            },
                            {
                                "title": "Second Title",
                                "url": "https://x.example.invalid/p",
                            },
                        ],
                    }
                ]
            }
        }
    }
    lines = deep_research.sources_lines(state)
    assert lines == [
        "x.example.invalid | First Title | https://x.example.invalid/p",
        "x.example.invalid | Second Title | https://x.example.invalid/p",
    ]


def test_sources_lines_skips_a_non_dict_group_and_a_non_dict_entry() -> None:
    state = {
        "report_message": {
            "metadata": {
                "search_result_groups": [
                    "not a dict",
                    {
                        "domain": "d",
                        "entries": ["not a dict", {"title": "T", "url": "u"}],
                    },
                ]
            }
        }
    }
    assert deep_research.sources_lines(state) == ["d | T | u"]


def test_sources_lines_skips_a_group_whose_entries_is_not_a_list() -> None:
    state = {
        "report_message": {
            "metadata": {"search_result_groups": [{"domain": "d", "entries": "oops"}]}
        }
    }
    assert deep_research.sources_lines(state) == []


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"report_message": None},
        {"report_message": {}},
        {"report_message": {"metadata": None}},
        {"report_message": {"metadata": {}}},
        {"report_message": {"metadata": {"search_result_groups": "oops"}}},
    ],
)
def test_sources_lines_handles_malformed_shapes_without_raising(state: Any) -> None:
    assert deep_research.sources_lines(state) == []


@pytest.mark.parametrize("state", [None, "oops", 42, []])
def test_sources_lines_tolerates_a_non_dict_state(state: Any) -> None:
    assert deep_research.sources_lines(state) == []


# ---------------------------------------------------------------------------
# _get_widget_state
# ---------------------------------------------------------------------------


def test_get_widget_state_returns_the_state_on_200() -> None:
    backend = _Backend([(200, DONE_CONVERSATION)])
    http_status, state, error = deep_research._get_widget_state(
        _Session(backend), "conv-done"
    )
    assert http_status == 200
    assert state == DONE_STATE
    assert error is None
    assert backend.calls == [("GET", "/backend-api/conversations/conv-done", None)]


def test_get_widget_state_reports_no_widget_state() -> None:
    backend = _Backend([(200, NO_WIDGET_CONVERSATION)])
    http_status, state, error = deep_research._get_widget_state(
        _Session(backend), "conv-plain"
    )
    assert http_status == 200
    assert state is None
    assert "not started the page way" in error


def test_get_widget_state_reports_an_http_failure() -> None:
    backend = _Backend([(500, {"error": "boom"})])
    http_status, state, error = deep_research._get_widget_state(
        _Session(backend), "conv-x"
    )
    assert http_status == 500
    assert state is None
    assert "HTTP 500" in error


# ---------------------------------------------------------------------------
# _wait_for_status
# ---------------------------------------------------------------------------


def test_wait_for_status_returns_0_immediately_when_already_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, DONE_CONVERSATION)])
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-done", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == []
    assert len(backend.calls) == 1


def test_wait_for_status_polls_once_then_reports_done(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, RUNNING_CONVERSATION), (200, DONE_CONVERSATION)])
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == [60.0]
    out = capsys.readouterr().out
    assert out.count("RUNNING") == 1
    assert out.count("DONE") == 1


def test_wait_for_status_keeps_polling_through_waiting_then_done(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend(
        [
            (200, WAITING_CONVERSATION),
            (200, RUNNING_CONVERSATION),
            (200, DONE_CONVERSATION),
        ]
    )
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "WAITING FOR PLAN CONFIRMATION" in out
    assert "RUNNING" in out
    assert "DONE" in out


def test_wait_for_status_prints_a_status_block_only_when_it_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend(
        [
            (200, RUNNING_CONVERSATION),
            (200, RUNNING_CONVERSATION),
            (200, DONE_CONVERSATION),
        ]
    )
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert capsys.readouterr().out.count("plan:") == 2


def test_wait_for_status_backs_off_120s_on_a_429_then_tries_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(429, {"error": "slow down"}), (200, DONE_CONVERSATION)])
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 0
    assert clock.sleeps == [120.0]


def test_wait_keeps_polling_while_the_widget_has_not_attached_yet(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """ChatGPT attaches the widget a little after the send, not instantly.

    Measured 2026-09-20: a wait started right after `start` found no widget
    for the first minute and the report was ready at 180 s. Failing on the
    first poll, as this did, made `status --wait` unusable right after a
    send.
    """
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend(
        [
            (200, NO_WIDGET_CONVERSATION),
            (200, NO_WIDGET_CONVERSATION),
            (200, DONE_CONVERSATION),
        ]
    )
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "no widget state yet" in out
    assert "DONE" in out


def test_wait_gives_up_when_no_widget_ever_attaches(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A research started the older way never attaches one; say so rather
    than polling to the end of time."""
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, NO_WIDGET_CONVERSATION)] * 40)
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=120.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 1
    assert "not started the page way" in capsys.readouterr().out


def test_wait_for_status_exits_1_on_a_non_429_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(500, {"error": "boom"})])
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=1800.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 1


def test_wait_for_status_times_out_and_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(deep_research.time, "monotonic", clock.monotonic)
    backend = _Backend([(200, RUNNING_CONVERSATION)] * 5)
    rc = deep_research._wait_for_status(
        _Session(backend), "conv-x", timeout=90.0, interval=60.0, sleep=clock.sleep
    )
    assert rc == 3


# ---------------------------------------------------------------------------
# main "start"
# ---------------------------------------------------------------------------


def test_start_sends_with_the_hint_and_writes_run_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Research the thing.", encoding="utf-8")
    run_path = tmp_path / "run.json"
    monkeypatch.setattr(deep_research, "open_session", _boom)

    fake_main = _fake_send_prompt_main({"conversation_id": "conv-123"})
    monkeypatch.setattr(send_prompt, "main", fake_main)

    rc = deep_research.main(
        [
            "start",
            str(prompt),
            "--project",
            "g-p-sandbox",
            "--title",
            "rp-test deep research",
            "--effort",
            "max",
            "--run",
            str(run_path),
        ]
    )

    assert rc == 0
    run_doc = json.loads(run_path.read_text())
    assert run_doc == {
        "conversation_id": "conv-123",
        "started_at": run_doc["started_at"],
        "prompt_file": str(prompt),
        "title": "rp-test deep research",
        "mode": "widget",
    }

    argv = fake_main.calls[0]
    assert argv[0] == str(prompt)
    assert argv[argv.index("--project") + 1] == "g-p-sandbox"
    assert argv[argv.index("--system-hint") + 1] == (
        "plugin:connector_openai_deep_research"
    )
    assert "--no-wait" in argv
    assert argv[argv.index("--json") + 1] == f"{run_path}.send.json"
    assert argv[argv.index("--title") + 1] == "rp-test deep research"
    assert argv[argv.index("--effort") + 1] == "max"
    assert "written to" in capsys.readouterr().out


def test_start_omits_title_and_effort_when_blank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(deep_research, "open_session", _boom)
    fake_main = _fake_send_prompt_main({"conversation_id": "conv-1"})
    monkeypatch.setattr(send_prompt, "main", fake_main)

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

    assert rc == 0
    argv = fake_main.calls[0]
    assert "--title" not in argv
    assert "--effort" not in argv
    run_doc = json.loads((tmp_path / "run.json").read_text())
    assert run_doc["title"] == ""


def test_start_no_mcp_call_is_ever_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """open_session (the only door to an MCP call) is never opened by
    start -- the module docstring's "no MCP call is made at all"."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(deep_research, "open_session", _boom)
    monkeypatch.setattr(
        send_prompt, "main", _fake_send_prompt_main({"conversation_id": "conv-1"})
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
    assert rc == 0


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
    assert "could not read" in capsys.readouterr().out


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


def test_start_send_record_without_conversation_id_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(
        send_prompt, "main", _fake_send_prompt_main({"session_id": "s"})
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
    assert "conversation_id" in capsys.readouterr().out


def test_start_requires_run(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("hi", encoding="utf-8")
    with pytest.raises(SystemExit):
        deep_research.main(["start", str(prompt), "--project", "g-p-x"])


def test_start_requires_project(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("hi", encoding="utf-8")
    with pytest.raises(SystemExit):
        deep_research.main(["start", str(prompt), "--run", str(tmp_path / "run.json")])


# ---------------------------------------------------------------------------
# main "status"
# ---------------------------------------------------------------------------


def test_status_done_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-done")
    backend = _Backend([(200, DONE_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    assert deep_research.main(["status", "--run", str(run_path)]) == 0


def test_status_prints_the_conversation_plan_and_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-done")
    backend = _Backend([(200, DONE_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    deep_research.main(["status", "--run", str(run_path)])
    out = capsys.readouterr().out
    assert "conversation: conv-done" in out
    assert "Research Plan: Done Topic" in out
    assert "[completed] Survey background sources" in out
    assert "started: 2026-09-20T11:00:00+00:00" in out
    assert "stopped: 2026-09-20T11:01:20+00:00" in out
    assert "DONE" in out


def test_status_running_exits_3_and_prints_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-running")
    backend = _Backend([(200, RUNNING_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 3
    out = capsys.readouterr().out
    assert "RUNNING" in out
    assert "DONE" not in out


def test_status_waiting_exits_3_and_prints_waiting_for_plan_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-waiting")
    backend = _Backend([(200, WAITING_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 3
    assert "WAITING FOR PLAN CONFIRMATION" in capsys.readouterr().out


def test_status_no_widget_state_exits_1_and_says_so_plainly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-plain")
    backend = _Backend([(200, NO_WIDGET_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 1
    assert "not started the page way" in capsys.readouterr().out


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
    assert "could not read the conversation" in capsys.readouterr().out


def test_status_no_steps_yet_prints_the_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    state = {"status": "in_progress", "plan": {"steps": []}}
    conv = _conversation_with_state(state, "conv-nosteps")
    run_path = _write_run(tmp_path, conversation_id="conv-nosteps")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    deep_research.main(["status", "--run", str(run_path)])
    out = capsys.readouterr().out
    assert "(no steps yet)" in out
    assert "plan: (untitled)" in out


def test_status_prints_a_steps_own_reason_when_it_carries_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    state = {
        "status": "in_progress",
        "plan": {
            "title": "T",
            "steps": [
                {
                    "id": "s1",
                    "text": "Skip a dead end",
                    "status": "skipped",
                    "reason": "source unreachable",
                }
            ],
        },
    }
    conv = _conversation_with_state(state, "conv-reason")
    run_path = _write_run(tmp_path, conversation_id="conv-reason")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    deep_research.main(["status", "--run", str(run_path)])
    out = capsys.readouterr().out
    assert "[skipped] Skip a dead end -- source unreachable" in out


def test_status_missing_run_file_exits_1_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    rc = deep_research.main(["status", "--run", str(tmp_path / "missing.json")])
    assert rc == 1


def test_status_run_without_conversation_id_exits_1_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    run_path = _write_run(tmp_path, conversation_id="")
    rc = deep_research.main(["status", "--run", str(run_path)])
    assert rc == 1
    assert "conversation_id" in capsys.readouterr().out


def test_status_wait_dispatches_with_the_given_timeout_and_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: object())
    calls = []
    monkeypatch.setattr(
        deep_research,
        "_wait_for_status",
        lambda session, conversation_id, *, timeout, interval: calls.append(
            (conversation_id, timeout, interval)
        )
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
    assert calls == [("conv-123", 42.0, 90.0)]


def test_status_wait_enforces_the_60s_minimum_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path)
    monkeypatch.setattr(deep_research, "open_session", lambda *a, **kw: object())
    calls = []
    monkeypatch.setattr(
        deep_research,
        "_wait_for_status",
        lambda session, conversation_id, *, timeout, interval: calls.append(interval)
        or 0,
    )
    deep_research.main(["status", "--run", str(run_path), "--wait", "--interval", "5"])
    assert calls == [60.0]
    assert "is below the minimum" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main "fetch"
# ---------------------------------------------------------------------------


def test_fetch_writes_the_report_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    expected_report = deep_research.report_markdown(
        deep_research.find_widget_state(conv)
    )
    run_path = _write_run(tmp_path, conversation_id="conv-completed-1")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.md"

    rc = deep_research.main(["fetch", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 0
    written = out_path.read_text(encoding="utf-8")
    assert written == expected_report
    assert not written.endswith("\n")  # the fixture's report has no trailing newline
    out = capsys.readouterr().out
    assert f"{len(expected_report)} characters ->" in out


def test_fetch_preserves_a_trailing_blank_line_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {
        "status": "completed",
        "plan": {"title": "T", "steps": []},
        "report_message": {
            "content": {"content_type": "text", "parts": ["Body text.\n\n"]}
        },
    }
    conv = _conversation_with_state(state, "conv-trailing")
    run_path = _write_run(tmp_path, conversation_id="conv-trailing")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.md"

    rc = deep_research.main(["fetch", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 0
    assert out_path.read_text(encoding="utf-8") == "Body text.\n\n"


def test_fetch_writes_an_empty_report_as_a_zero_byte_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    state = {
        "status": "completed",
        "plan": {"title": "T", "steps": []},
        "report_message": {"content": {"content_type": "text", "parts": [""]}},
    }
    conv = _conversation_with_state(state, "conv-empty")
    run_path = _write_run(tmp_path, conversation_id="conv-empty")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.md"

    rc = deep_research.main(["fetch", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 0
    assert out_path.read_bytes() == b""
    assert "0 characters ->" in capsys.readouterr().out


def test_fetch_writes_sources_deduplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    conv = _fixture("deep_research_conversation_completed.json")
    run_path = _write_run(tmp_path, conversation_id="conv-completed-1")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.md"
    sources_path = tmp_path / "sources.txt"

    rc = deep_research.main(
        [
            "fetch",
            "--run",
            str(run_path),
            "--out",
            str(out_path),
            "--sources",
            str(sources_path),
        ]
    )

    assert rc == 0
    lines = sources_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "a.example.invalid | Example Source A | https://a.example.invalid/one",
        "b.example.invalid | Example Source B | https://b.example.invalid/two",
    ]
    assert "2 source(s) ->" in capsys.readouterr().out


def test_fetch_writes_an_empty_sources_file_when_there_are_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-done")
    backend = _Backend([(200, DONE_CONVERSATION)])  # report_message has no groups
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    sources_path = tmp_path / "sources.txt"

    rc = deep_research.main(
        [
            "fetch",
            "--run",
            str(run_path),
            "--out",
            str(tmp_path / "r.md"),
            "--sources",
            str(sources_path),
        ]
    )

    assert rc == 0
    assert sources_path.read_bytes() == b""


def test_fetch_writes_the_whole_widget_state_as_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-done")
    backend = _Backend([(200, DONE_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    json_path = tmp_path / "state.json"

    rc = deep_research.main(
        [
            "fetch",
            "--run",
            str(run_path),
            "--out",
            str(tmp_path / "r.md"),
            "--json",
            str(json_path),
        ]
    )

    assert rc == 0
    assert json.loads(json_path.read_text(encoding="utf-8")) == DONE_STATE


def test_fetch_refuses_while_running_and_never_writes_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-running")
    backend = _Backend([(200, RUNNING_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.md"

    rc = deep_research.main(["fetch", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 3
    assert "not finished yet" in capsys.readouterr().out
    assert not out_path.exists()


def test_fetch_refuses_while_waiting_with_its_own_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-waiting")
    backend = _Backend([(200, WAITING_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["fetch", "--run", str(run_path), "--out", str(tmp_path / "r.md")]
    )
    assert rc == 3
    assert "waiting for plan confirmation" in capsys.readouterr().out


def test_fetch_done_but_no_report_text_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    state = {
        "status": "completed",
        "plan": {"title": "T", "steps": []},
    }  # no report_message
    conv = _conversation_with_state(state, "conv-noreport")
    run_path = _write_run(tmp_path, conversation_id="conv-noreport")
    backend = _Backend([(200, conv)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["fetch", "--run", str(run_path), "--out", str(tmp_path / "r.md")]
    )
    assert rc == 1
    assert "no report text" in capsys.readouterr().out


def test_fetch_no_widget_state_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_run(tmp_path, conversation_id="conv-plain")
    backend = _Backend([(200, NO_WIDGET_CONVERSATION)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["fetch", "--run", str(run_path), "--out", str(tmp_path / "r.md")]
    )
    assert rc == 1
    assert "not started the page way" in capsys.readouterr().out


def test_fetch_http_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_run(tmp_path)
    backend = _Backend([(500, {"error": "boom"})])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    rc = deep_research.main(
        ["fetch", "--run", str(run_path), "--out", str(tmp_path / "r.md")]
    )
    assert rc == 1


def test_fetch_missing_run_file_exits_1_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    rc = deep_research.main(
        [
            "fetch",
            "--run",
            str(tmp_path / "missing.json"),
            "--out",
            str(tmp_path / "r.md"),
        ]
    )
    assert rc == 1


def test_fetch_run_without_conversation_id_exits_1_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(deep_research, "open_session", _boom)
    run_path = _write_run(tmp_path, conversation_id="")
    rc = deep_research.main(
        ["fetch", "--run", str(run_path), "--out", str(tmp_path / "r.md")]
    )
    assert rc == 1
    assert "conversation_id" in capsys.readouterr().out


def test_fetch_requires_run_and_out(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["fetch", "--run", str(tmp_path / "run.json")])


# ---------------------------------------------------------------------------
# export fallback: mcp_body / state_summary / run_session_arg / run_message_id
# ---------------------------------------------------------------------------

EXPORT_RUN = {
    "conversation_id": "conv-123",
    "session_id": "sess-abc",
    "message_id": "toolcall-1",
}

RUNNING_MCP_STATE = {
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

DONE_MCP_STATE = {
    "_meta": {
        "deep_research_widget_messages": [
            *RUNNING_MCP_STATE["_meta"]["deep_research_widget_messages"],
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

NO_TITLES_MCP_STATE = {
    "_meta": {
        "deep_research_widget_messages": [
            {"author": {"role": "tool", "name": "web.run"}, "metadata": {}},
        ],
        "source_searches": [],
    },
    "content": [],
    "isError": False,
}


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


def _write_export_run(tmp_path: Path, **overrides: Any) -> Path:
    data = {**EXPORT_RUN, **overrides}
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


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


def test_state_summary_reports_running_when_no_title_says_generated_report() -> None:
    summary = deep_research.state_summary(RUNNING_MCP_STATE)
    assert summary == {
        "messages": 1,
        "titles": ["Planning the research"],
        "done": False,
        "error": None,
    }


def test_state_summary_reports_done_once_a_title_starts_with_generated_report() -> None:
    summary = deep_research.state_summary(DONE_MCP_STATE)
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


def test_run_session_arg_uses_the_recorded_session_id_when_present() -> None:
    assert deep_research.run_session_arg(EXPORT_RUN) == "sess-abc"


def test_run_session_arg_falls_back_to_the_conversation_id_when_absent() -> None:
    assert deep_research.run_session_arg({"conversation_id": "conv-only"}) == (
        "conv-only"
    )


def test_run_session_arg_falls_back_when_session_id_is_present_but_empty() -> None:
    run = {"conversation_id": "conv-1", "session_id": ""}
    assert deep_research.run_session_arg(run) == "conv-1"


def test_run_message_id_uses_the_recorded_message_id_when_present() -> None:
    assert deep_research.run_message_id(EXPORT_RUN) == "toolcall-1"


def test_run_message_id_mints_a_fresh_uuid4_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(deep_research.uuid, "uuid4", lambda: fixed_uuid)
    assert deep_research.run_message_id({"conversation_id": "c"}) == str(fixed_uuid)


def test_run_message_id_falls_back_when_message_id_is_present_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_uuid = uuid.UUID("00000000-0000-0000-0000-000000000002")
    monkeypatch.setattr(deep_research.uuid, "uuid4", lambda: fixed_uuid)
    run = {"conversation_id": "c", "message_id": ""}
    assert deep_research.run_message_id(run) == str(fixed_uuid)


# ---------------------------------------------------------------------------
# export fallback: parse_disposition / decode_export / docx_text
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
# _load_json_object / _load_run
# ---------------------------------------------------------------------------


def test_load_json_object_reads_a_valid_file_with_no_required_keys(
    tmp_path: Path,
) -> None:
    path = _write_json(tmp_path / "x.json", {"a": 1})
    assert deep_research._load_json_object(str(path), ()) == {"a": 1}


def test_load_json_object_reads_a_valid_file_with_its_required_keys_present(
    tmp_path: Path,
) -> None:
    path = _write_json(tmp_path / "x.json", {"a": 1, "b": 2})
    assert deep_research._load_json_object(str(path), ("a", "b")) == {"a": 1, "b": 2}


def test_load_json_object_reports_a_missing_file(tmp_path: Path, capsys: Any) -> None:
    assert deep_research._load_json_object(str(tmp_path / "nope.json"), ()) is None
    assert "could not read" in capsys.readouterr().out


def test_load_json_object_reports_invalid_json(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert deep_research._load_json_object(str(path), ()) is None
    assert "not valid JSON" in capsys.readouterr().out


def test_load_json_object_reports_a_json_value_that_is_not_an_object(
    tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert deep_research._load_json_object(str(path), ()) is None
    assert "not a JSON object" in capsys.readouterr().out


def test_load_json_object_reports_which_required_keys_are_missing(
    tmp_path: Path, capsys: Any
) -> None:
    path = _write_json(tmp_path / "run.json", {"a": "x"})
    assert deep_research._load_json_object(str(path), ("a", "b")) is None
    assert "b" in capsys.readouterr().out


def test_load_json_object_treats_an_empty_value_as_missing(
    tmp_path: Path, capsys: Any
) -> None:
    path = _write_json(tmp_path / "run.json", {"a": ""})
    assert deep_research._load_json_object(str(path), ("a",)) is None
    assert "a" in capsys.readouterr().out


def test_load_run_reads_a_valid_file(tmp_path: Path) -> None:
    path = _write_export_run(tmp_path)
    run = deep_research._load_run(str(path))
    assert run == EXPORT_RUN


def test_load_run_accepts_conversation_id_alone(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "run.json", {"conversation_id": "conv-only"})
    assert deep_research._load_run(str(path)) == {"conversation_id": "conv-only"}


def test_load_run_reports_a_missing_conversation_id(
    tmp_path: Path, capsys: Any
) -> None:
    path = _write_json(tmp_path / "run.json", {"session_id": "s", "message_id": "m"})
    assert deep_research._load_run(str(path)) is None
    assert "conversation_id" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main "export" (the fallback path)
# ---------------------------------------------------------------------------


def test_export_writes_the_file_and_prints_filename_and_byte_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_export_run(tmp_path)
    backend = _Backend([(200, DONE_MCP_STATE), (200, EXPORT_RESULT)])
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
    run_path = _write_export_run(tmp_path)
    backend = _Backend([(200, DONE_MCP_STATE), (200, EXPORT_RESULT)])
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
    run_path = _write_export_run(tmp_path)
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
    run_path = _write_export_run(tmp_path, conversation_id="")
    rc = deep_research.main(
        ["export", "--run", str(run_path), "--out", str(tmp_path / "r.docx")]
    )
    assert rc == 2
    assert "conversation_id" in capsys.readouterr().out


def test_export_works_from_a_run_with_only_conversation_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_path = _write_json(tmp_path / "run.json", {"conversation_id": "conv-only"})
    backend = _Backend([(200, DONE_MCP_STATE), (200, EXPORT_RESULT)])
    monkeypatch.setattr(
        deep_research, "open_session", lambda *a, **kw: _Session(backend)
    )
    out_path = tmp_path / "report.docx"

    rc = deep_research.main(["export", "--run", str(run_path), "--out", str(out_path)])

    assert rc == 0
    get_state_call, export_call = (c[2] for c in backend.calls)
    assert get_state_call["conversation_id"] == "conv-only"
    assert get_state_call["params"]["arguments"]["session_id"] == "conv-only"
    assert export_call["params"]["arguments"]["session_id"] == "conv-only"
    # one message_id minted for the whole command, reused across both calls.
    assert get_state_call["message_id"] == export_call["message_id"]


def test_export_refuses_when_not_done_and_never_calls_export_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    run_path = _write_export_run(tmp_path)
    backend = _Backend([(200, RUNNING_MCP_STATE)])
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
    run_path = _write_export_run(tmp_path)
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
    run_path = _write_export_run(tmp_path)
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
    run_path = _write_export_run(tmp_path)
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
    run_path = _write_export_run(tmp_path)
    backend = _Backend([(200, DONE_MCP_STATE), (500, {"error": "boom"})])
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
    run_path = _write_export_run(tmp_path)
    backend = _Backend([(200, DONE_MCP_STATE), (200, ERROR_RESULT)])
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
    run_path = _write_export_run(tmp_path)
    pdf_result = {
        "_meta": {
            "content_disposition": 'attachment; filename="Report.pdf"',
            "encoded_data": base64.b64encode(b"%PDF-1.4 placeholder").decode("ascii"),
        },
        "isError": False,
    }
    backend = _Backend([(200, DONE_MCP_STATE), (200, pdf_result)])
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


def test_export_requires_run_and_out(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["export", "--run", str(tmp_path / "run.json")])


# ---------------------------------------------------------------------------
# --help, and the required arguments each subcommand claims to enforce
# ---------------------------------------------------------------------------


def test_no_command_at_all_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        deep_research.main([])
    assert exc.value.code != 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["start", "--help"],
        ["status", "--help"],
        ["fetch", "--help"],
        ["export", "--help"],
    ],
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


def test_module_help_states_the_widget_facts(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "ordinary chat that runs longer" in out
    assert "widget_state" in out or "widget state" in out
    assert "no MCP call is made at all" in out


def test_module_help_states_the_exact_conversations_endpoint(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    # Not a single contiguous substring check: argparse's own textwrap
    # rewraps this long docstring and may break a hyphenated compound
    # like "backend-api" across the line boundary it chooses, which is a
    # display artifact, not a documentation gap.
    assert "plural" in out
    assert "conversations" in out
    assert "no" in out and "messages" in out and "suffix" in out


@pytest.mark.parametrize(
    "argv", [["start", "--help"], ["status", "--help"], ["fetch", "--help"]]
)
def test_each_widget_subcommand_help_repeats_the_widget_note(
    argv: list[str], capsys: Any
) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(argv)
    out = " ".join(capsys.readouterr().out.split())
    assert "ordinary chat that runs longer" in out
    assert "export" in out


def test_export_help_states_it_is_the_fallback(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["export", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Fallback only" in out
    assert "MCP start tool" in out


def test_start_help_mentions_the_system_hint(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        deep_research.main(["start", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "plugin:connector_openai_deep_research" in out
    assert "--project" in out
