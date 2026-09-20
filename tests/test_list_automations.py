"""Tests for scripts/list_automations.py (ROADMAP.md R4, scheduled tasks).

Same house style as tests/test_search_chats.py: a fake ``session.session``
records every call it is given and answers from a queue, ``main()`` is
exercised over that fake, and every test name says which behaviour it
defends. Fixture items are hand-written here, never recorded from the real
account: ``title`` and ``prompt`` are the user's own content, and
record_fixture.py's sanitizer has no idea how to scrub either (TESTING.md
section 1, the fixture scanner).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


list_automations = _load("list_automations")


# ---------------------------------------------------------------------------
# fixtures -- hand-written, never recorded (see module docstring)
# ---------------------------------------------------------------------------


def _components(**over: Any) -> dict[str, Any]:
    base = {
        "by_day": None,
        "by_hour": None,
        "by_minute": None,
        "by_month": None,
        "by_month_day": None,
        "by_second": None,
        "by_year_day": None,
        "frequency": None,
        "start_time": None,
    }
    base.update(over)
    return base


def _automation(
    conv_id: str = "conv-1",
    *,
    id_: str = "task-1",
    title: str = "Weekly digest",
    display_emoji: str = "",
    prompt: str = "collect the fake weekly digest",
    timing_mode: str = "exact_schedule",
    schedule: str = (
        "BEGIN:VEVENT\nDTSTART:20260830T075934Z\nRRULE:FREQ=HOURLY\nEND:VEVENT"
    ),
    schedule_components: dict[str, Any] | None = None,
    display_schedule: str | None = None,
    next_run_times: list[str] | None = None,
    last_run_time: str | None = None,
    is_enabled: bool = True,
) -> dict[str, Any]:
    """One hand-written automation, shaped like the 32 keys measured
    2026-09-21 (module docstring). Fake title and prompt throughout."""
    return {
        "id": id_,
        "title": title,
        "display_title": title,
        "display_emoji": display_emoji,
        "prompt": prompt,
        "conversation_id": conv_id,
        "timing_mode": timing_mode,
        "schedule": schedule,
        "schedule_components": (
            schedule_components if schedule_components is not None else _components()
        ),
        "display_schedule": display_schedule,
        "next_run_times": (
            next_run_times
            if next_run_times is not None
            else ["2026-09-21T08:59:34+10:00"]
        ),
        "last_run_time": last_run_time,
        "is_enabled": is_enabled,
        "executor": "cloud",
        "thread_mode": "existing_chat",
        "can_delete": True,
        "complexity": "simple",
        "content_type": "text",
        "created_by_display_name": "Fake User",
        "current_user_role": "owner",
        "default_timezone": "UTC",
        "email_enabled": False,
        "external_channel": None,
        "last_edited_at": "2026-09-01T00:00:00Z",
        "last_edited_by_display_name": "Fake User",
        "notifications_enabled": True,
        "schedule_time_of_day": None,
        "source_conversation_is_work_mode": False,
        "target_time_utc": None,
        "team_id": None,
        "updated_at": "2026-09-01T00:00:00Z",
        "webhook_triggers": [],
    }


def _resp(items: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "cursor": cursor}


class _FakeAutomationsBackend:
    """``session.session.call`` recording every call; answers from a queue."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
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
        if not self.responses:
            return 200, _resp([])
        return self.responses.pop(0)


class _FakeAutomationsSession:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.session = _FakeAutomationsBackend(responses)


# ---------------------------------------------------------------------------
# automations_url
# ---------------------------------------------------------------------------


def test_automations_url_carries_the_filter() -> None:
    assert (
        list_automations.automations_url("scheduled")
        == "/backend-api/automations?filter=scheduled"
    )
    assert (
        list_automations.automations_url("paused")
        == "/backend-api/automations?filter=paused"
    )


# ---------------------------------------------------------------------------
# next_run_label / last_run_label
# ---------------------------------------------------------------------------


def test_next_run_label_is_dash_for_an_empty_list() -> None:
    assert list_automations.next_run_label([]) == "-"


def test_next_run_label_keeps_the_item_s_own_offset() -> None:
    """2026-09-21T08:59:34+10:00 stays +10, never converted to another tz."""
    label = list_automations.next_run_label(["2026-09-21T08:59:34+10:00"])
    assert label == "2026-09-21 08:59"


def test_next_run_label_uses_only_the_first_entry() -> None:
    label = list_automations.next_run_label(
        ["2026-09-21T08:59:00+00:00", "2026-09-22T08:59:00+00:00"]
    )
    assert label == "2026-09-21 08:59"


def test_last_run_label_is_dash_for_none() -> None:
    assert list_automations.last_run_label(None) == "-"


def test_last_run_label_parses_a_z_suffixed_utc_string() -> None:
    assert list_automations.last_run_label("2026-09-20T12:30:00Z") == "2026-09-20 12:30"


def test_last_run_label_is_dash_for_an_unparsable_string() -> None:
    assert list_automations.last_run_label("not-a-timestamp") == "-"


# ---------------------------------------------------------------------------
# on_label
# ---------------------------------------------------------------------------


def test_on_label_yes_for_true() -> None:
    assert list_automations.on_label(True) == "yes"


@pytest.mark.parametrize("value", [False, None])
def test_on_label_no_for_false_or_none(value: Any) -> None:
    assert list_automations.on_label(value) == "no"


# ---------------------------------------------------------------------------
# rrule_line
# ---------------------------------------------------------------------------


def test_rrule_line_extracts_the_freq_value() -> None:
    schedule = "BEGIN:VEVENT\nDTSTART:20260830T075934Z\nRRULE:FREQ=HOURLY\nEND:VEVENT"
    assert list_automations.rrule_line(schedule) == "FREQ=HOURLY"


def test_rrule_line_is_empty_with_no_rrule_line() -> None:
    assert list_automations.rrule_line("BEGIN:VEVENT\nEND:VEVENT") == ""


def test_rrule_line_is_empty_for_an_empty_schedule() -> None:
    assert list_automations.rrule_line("") == ""


# ---------------------------------------------------------------------------
# schedule_label -- hourly, daily, weekly, yearly, all-null (RRULE fallback)
# ---------------------------------------------------------------------------


def test_schedule_label_prefers_display_schedule_when_set() -> None:
    label = list_automations.schedule_label(
        _components(frequency="hourly"), "Monitoring", ""
    )
    assert label == "Monitoring"


def test_schedule_label_hourly_with_a_minute() -> None:
    components = _components(frequency="hourly", by_minute="59")
    assert list_automations.schedule_label(components, None, "") == "hourly @:59"


def test_schedule_label_hourly_without_a_minute() -> None:
    components = _components(frequency="hourly")
    assert list_automations.schedule_label(components, None, "") == "hourly"


def test_schedule_label_daily() -> None:
    components = _components(frequency="daily", by_hour="9", by_minute="0")
    assert list_automations.schedule_label(components, None, "") == "daily 09:00"


def test_schedule_label_daily_tolerates_a_non_numeric_component() -> None:
    """A component that will not parse as int prints as-is rather than
    raising -- an unexpected shape must never crash the table."""
    components = _components(frequency="daily", by_hour="soon", by_minute="0")
    assert list_automations.schedule_label(components, None, "") == "daily soon:00"


def test_schedule_label_weekly_with_days() -> None:
    components = _components(frequency="weekly", by_day="MO,TH")
    assert list_automations.schedule_label(components, None, "") == "weekly mon,thu"


def test_schedule_label_weekly_without_days() -> None:
    components = _components(frequency="weekly")
    assert list_automations.schedule_label(components, None, "") == "weekly"


def test_schedule_label_yearly() -> None:
    components = _components(frequency="yearly", by_month="6", by_month_day="23")
    assert list_automations.schedule_label(components, None, "") == "yearly 06-23"


def test_schedule_label_unrecognised_frequency_prints_verbatim() -> None:
    components = _components(frequency="monthly")
    assert list_automations.schedule_label(components, None, "") == "monthly"


def test_schedule_label_falls_back_to_the_rrule_line_when_all_null() -> None:
    schedule = "BEGIN:VEVENT\nDTSTART:20260830T075934Z\nRRULE:FREQ=HOURLY\nEND:VEVENT"
    assert (
        list_automations.schedule_label(_components(), None, schedule) == "FREQ=HOURLY"
    )


def test_schedule_label_is_dash_when_nothing_is_available() -> None:
    assert list_automations.schedule_label(_components(), None, "") == "-"


def test_schedule_label_tolerates_a_none_components_dict() -> None:
    assert list_automations.schedule_label(None, None, "") == "-"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# title_label / prompt_label
# ---------------------------------------------------------------------------


def test_title_label_adds_the_emoji() -> None:
    item = _automation(title="Weekly digest", display_emoji="\U0001f4c5")
    assert list_automations.title_label(item) == "\U0001f4c5 Weekly digest"


def test_title_label_without_an_emoji() -> None:
    item = _automation(title="Weekly digest", display_emoji="")
    assert list_automations.title_label(item) == "Weekly digest"


def test_title_label_falls_back_to_untitled() -> None:
    item = _automation(title="")
    item["title"] = None
    assert list_automations.title_label(item) == "(untitled)"


def test_title_label_is_shortened() -> None:
    item = _automation(title="x" * 100, display_emoji="")
    label = list_automations.title_label(item)
    assert len(label) <= list_automations.TITLE_WIDTH
    assert label.endswith("…")


def test_prompt_label_is_shortened_and_empty_when_absent() -> None:
    item = _automation(prompt="y" * 100)
    label = list_automations.prompt_label(item)
    assert len(label) <= list_automations.PROMPT_WIDTH
    item2 = _automation(prompt="")
    item2["prompt"] = None
    assert list_automations.prompt_label(item2) == ""


# ---------------------------------------------------------------------------
# headers_for / row_for
# ---------------------------------------------------------------------------


def test_headers_for_base_columns() -> None:
    headers = list_automations.headers_for(with_state=False, show_prompts=False)
    assert headers == (
        "next run",
        "last run",
        "mode",
        "schedule",
        "on",
        "title",
        "conversation",
    )


def test_headers_for_adds_state_and_prompt() -> None:
    headers = list_automations.headers_for(with_state=True, show_prompts=True)
    assert headers[0] == "state"
    assert headers[-1] == "prompt"
    assert len(headers) == 9


def test_row_for_without_state_or_prompt() -> None:
    item = _automation(
        "conv-9",
        title="Weekly digest",
        display_emoji="",
        timing_mode="exact_schedule",
        is_enabled=True,
        next_run_times=["2026-09-21T08:59:00+00:00"],
        last_run_time="2026-09-20T08:59:00Z",
        schedule_components=_components(frequency="hourly", by_minute="59"),
    )
    row = list_automations.row_for(item)
    assert row == (
        "2026-09-21 08:59",
        "2026-09-20 08:59",
        "exact_schedule",
        "hourly @:59",
        "yes",
        "Weekly digest",
        "conv-9",
    )


def test_row_for_with_state_prepends_it() -> None:
    item = _automation("conv-1")
    row = list_automations.row_for(item, with_state=True, state="paused")
    assert row[0] == "paused"
    assert len(row) == 8


def test_row_for_with_prompt_appends_it() -> None:
    item = _automation("conv-1", prompt="fake prompt text")
    row = list_automations.row_for(item, show_prompt=True)
    assert row[-1] == "fake prompt text"
    assert len(row) == 8


# ---------------------------------------------------------------------------
# main() -- one filter
# ---------------------------------------------------------------------------


def test_main_default_filter_is_scheduled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_automation("conv-1", title="Alpha")]
    session = _FakeAutomationsSession([(200, _resp(items))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main([]) == 0
    assert session.session.calls == [
        ("GET", "/backend-api/automations?filter=scheduled", None)
    ]
    out = capsys.readouterr().out
    assert "1 automation(s) (scheduled)" in out
    assert "Alpha" in out


def test_main_explicit_filter_paused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([(200, _resp([]))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "paused"]) == 0
    assert session.session.calls == [
        ("GET", "/backend-api/automations?filter=paused", None)
    ]
    assert "0 automation(s) (paused)" in capsys.readouterr().out


def test_main_explicit_filter_finished(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([(200, _resp([]))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "finished"]) == 0
    assert session.session.calls == [
        ("GET", "/backend-api/automations?filter=finished", None)
    ]


def test_main_forwards_the_browser_flag_to_open_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}
    session = _FakeAutomationsSession([(200, _resp([]))])

    def _open(browser: str = "chrome") -> Any:
        seen["browser"] = browser
        return session

    monkeypatch.setattr(list_automations, "open_session", _open)
    list_automations.main(["--browser", "chromium"])
    assert seen["browser"] == "chromium"


# ---------------------------------------------------------------------------
# main() -- --filter all
# ---------------------------------------------------------------------------


def test_main_filter_all_calls_the_three_filters_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession(
        [
            (200, _resp([_automation("s1", title="Sched one")])),
            (200, _resp([_automation("p1", title="Paused one")])),
            (200, _resp([_automation("f1", title="Finished one")])),
        ]
    )
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "all"]) == 0
    assert [c[1] for c in session.session.calls] == [
        "/backend-api/automations?filter=scheduled",
        "/backend-api/automations?filter=paused",
        "/backend-api/automations?filter=finished",
    ]
    out = capsys.readouterr().out
    assert "state" in out  # the added column header
    assert "scheduled" in out and "paused" in out and "finished" in out
    assert "3 automation(s) (all)" in out


# ---------------------------------------------------------------------------
# main() -- bad filter, before any call
# ---------------------------------------------------------------------------


def test_main_bad_filter_exits_2_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "bogus"]) == 2
    assert session.session.calls == []
    assert "bogus" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- a failed read
# ---------------------------------------------------------------------------


def test_main_a_failed_read_exits_1_and_names_the_filter_and_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([(500, {"error": "boom"})])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main([]) == 1
    out = capsys.readouterr().out
    assert "filter 'scheduled' failed" in out
    assert "500" in out


def test_main_filter_all_a_later_failure_keeps_earlier_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession(
        [
            (200, _resp([_automation("s1", title="Sched one")])),
            (500, {"error": "boom"}),
        ]
    )
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "all"]) == 1
    out = capsys.readouterr().out
    assert "Sched one" in out
    assert "filter 'paused' failed" in out
    assert "500" in out


def test_main_treats_a_non_dict_200_response_as_a_failed_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([(200, None)])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main([]) == 1
    assert "filter 'scheduled' failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- cursor non-null
# ---------------------------------------------------------------------------


def test_main_a_non_null_cursor_prints_more_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession(
        [(200, _resp([_automation("s1")], cursor="opaque"))]
    )
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main([]) == 0
    out = capsys.readouterr().out
    assert "more exist (cursor returned, not followed)" in out


def test_main_filter_all_names_which_filter_had_more(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession(
        [
            (200, _resp([_automation("s1")], cursor=None)),
            (200, _resp([_automation("p1")], cursor="opaque")),
            (200, _resp([_automation("f1")], cursor=None)),
        ]
    )
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--filter", "all"]) == 0
    out = capsys.readouterr().out
    assert "more exist (cursor returned, not followed): paused" in out


def test_main_a_null_cursor_prints_no_more_exist_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeAutomationsSession([(200, _resp([_automation("s1")], cursor=None))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    list_automations.main([])
    assert "more exist" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- --prompts
# ---------------------------------------------------------------------------


def test_main_prompts_flag_adds_the_prompt_column(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_automation("conv-1", prompt="fake prompt visible now")]
    session = _FakeAutomationsSession([(200, _resp(items))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    assert list_automations.main(["--prompts"]) == 0
    out = capsys.readouterr().out
    assert "prompt" in out
    assert "fake prompt visible now" in out


def test_main_without_prompts_flag_never_prints_the_prompt_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_automation("conv-1", prompt="fake prompt hidden now")]
    session = _FakeAutomationsSession([(200, _resp(items))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    list_automations.main([])
    assert "fake prompt hidden now" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- --json
# ---------------------------------------------------------------------------


def test_main_json_writes_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [_automation("conv-1")]
    session = _FakeAutomationsSession([(200, _resp(items))])
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "automations.json"
    assert list_automations.main(["--json", str(out_path)]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(doc) == {"filter", "items"}
    assert doc["filter"] == "scheduled"
    assert doc["items"] == items


def test_main_json_filter_all_concatenates_in_fetch_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s_items = [_automation("s1")]
    p_items = [_automation("p1")]
    f_items = [_automation("f1")]
    session = _FakeAutomationsSession(
        [(200, _resp(s_items)), (200, _resp(p_items)), (200, _resp(f_items))]
    )
    monkeypatch.setattr(list_automations, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "automations.json"
    list_automations.main(["--filter", "all", "--json", str(out_path)])
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["filter"] == "all"
    assert [i["conversation_id"] for i in doc["items"]] == ["s1", "p1", "f1"]
