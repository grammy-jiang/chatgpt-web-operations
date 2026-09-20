"""Tests for scripts/search_chats.py (ROADMAP.md R6, global content search).

Same house style as tests/test_command_mains_2.py: a fake ``session.session``
records every call it is given and answers from a queue, ``main()`` is
exercised over that fake, and every test name says which failure or
behaviour it defends. Fixture items are hand-written here, never recorded
from the real account: a snippet is the user's own content, and
record_fixture.py's sanitizer has no idea how to scrub one (TESTING.md
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


search_chats = _load("search_chats")


# ---------------------------------------------------------------------------
# fixtures -- hand-written, never recorded (see module docstring)
# ---------------------------------------------------------------------------


def _item(
    conv_id: str,
    match_kind: str = "content",
    title: str = "Some worker chat",
    snippet: str | None = "a snippet with the word in it",
    *,
    archived: bool = False,
    starred: bool | None = False,
    update_time: float = 1758000000.0,
) -> dict[str, Any]:
    return {
        "id": f"conversation:{conv_id}",
        "source_type": "conversation",
        "source_key": "conversation",
        "title": title,
        "snippet": snippet,
        "update_time": update_time,
        "match_kind": match_kind,
        "payload": {
            "kind": "conversation",
            "conversation_id": conv_id,
            "message_id": "msg-1",
            "is_archived": archived,
            "is_starred": starred,
        },
    }


def _ok_status(has_more: bool = False) -> dict[str, Any]:
    return {
        "source_type": "conversation",
        "source_key": "conversation",
        "status": "ok",
        "has_more": has_more,
        "duration_ms": 812.4,
        "error_code": None,
    }


def _resp(
    items: list[dict[str, Any]],
    cursor: str | None = None,
    partial: bool = False,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "items": items,
        "cursor": cursor,
        "partial_results": partial,
        "source_statuses": statuses if statuses is not None else [_ok_status()],
    }


class _FakeSearchBackend:
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


class _FakeSearchSession:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.session = _FakeSearchBackend(responses)


class _InfiniteSearchBackend:
    """Always answers with one hit and a fresh cursor; never ends on its own."""

    def __init__(self) -> None:
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
        return 200, _resp(
            [_item("inf")], cursor="again", statuses=[_ok_status(has_more=True)]
        )


class _InfiniteSearchSession:
    def __init__(self) -> None:
        self.session = _InfiniteSearchBackend()


# ---------------------------------------------------------------------------
# body_for
# ---------------------------------------------------------------------------


def test_body_for_asks_for_conversation_sources_only() -> None:
    body = search_chats.body_for("worker", 20)
    assert body == {
        "query": "worker",
        "limit": 20,
        "source_requests": [{"type": "conversation"}],
    }


def test_body_for_never_asks_for_project_or_library_sources() -> None:
    """HARD RULE: this command must never send a project or library source."""
    body = search_chats.body_for("worker", 20, cursor="anything")
    kinds = {sr["type"] for sr in body["source_requests"]}
    assert kinds == {"conversation"}


@pytest.mark.parametrize("cursor", [None, ""])
def test_body_for_omits_the_cursor_when_there_is_none(cursor: str | None) -> None:
    assert "cursor" not in search_chats.body_for("worker", 20, cursor=cursor)


def test_body_for_adds_the_cursor_when_one_is_given() -> None:
    body = search_chats.body_for("worker", 20, cursor="3")
    assert body["cursor"] == "3"


# ---------------------------------------------------------------------------
# flags_of
# ---------------------------------------------------------------------------


def test_flags_of_reports_archived_and_pinned() -> None:
    payload = {"is_archived": True, "is_starred": True}
    assert search_chats.flags_of(payload) == "archived pinned"


def test_flags_of_is_empty_when_neither_flag_is_set() -> None:
    assert search_chats.flags_of({}) == ""
    assert search_chats.flags_of({"is_starred": None, "is_archived": False}) == ""


def test_flags_of_reports_archived_alone() -> None:
    assert search_chats.flags_of({"is_archived": True}) == "archived"


# ---------------------------------------------------------------------------
# rows_for
# ---------------------------------------------------------------------------


def test_rows_for_builds_one_row_per_item_in_column_order() -> None:
    items = [
        _item(
            "conv-1",
            "content",
            "Worker chat",
            "found the word right here",
            update_time=1758000000.0,
        )
    ]
    rows = search_chats.rows_for(items)
    assert len(rows) == 1
    updated, match, flags, conv_id, title, snippet = rows[0]
    assert match == "content"
    assert flags == ""
    assert conv_id == "conv-1"
    assert title == "Worker chat"
    assert snippet == "found the word right here"
    assert len(updated) == 10 and updated.count("-") == 2  # YYYY-MM-DD


def test_rows_for_a_title_match_has_an_empty_snippet_cell_not_none() -> None:
    items = [_item("conv-2", "title", "Exact title match", snippet=None)]
    rows = search_chats.rows_for(items)
    assert rows[0][1] == "title"
    assert rows[0][5] == ""


def test_rows_for_collapses_newlines_in_the_snippet() -> None:
    items = [_item("conv-3", "content", "t", "line one\nline two\nline three")]
    rows = search_chats.rows_for(items)
    assert rows[0][5] == "line one line two line three"


def test_rows_for_flags_archived_and_pinned_from_the_payload() -> None:
    items = [_item("conv-4", "content", "t", "s", archived=True, starred=True)]
    rows = search_chats.rows_for(items)
    assert rows[0][2] == "archived pinned"


def test_rows_for_survives_a_missing_update_time() -> None:
    item = _item("conv-5")
    item["update_time"] = None
    rows = search_chats.rows_for([item])
    assert rows[0][0] == "?"


def test_rows_for_survives_a_non_dict_payload() -> None:
    item = _item("conv-6")
    item["payload"] = "not a dict"
    rows = search_chats.rows_for([item])
    assert rows[0][2] == ""
    assert rows[0][3] == ""


# ---------------------------------------------------------------------------
# page_status
# ---------------------------------------------------------------------------


def test_page_status_is_empty_for_a_clean_ok_page() -> None:
    assert search_chats.page_status(_resp([], statuses=[_ok_status()])) == []


def test_page_status_warns_on_partial_results() -> None:
    warnings = search_chats.page_status(_resp([], partial=True))
    assert len(warnings) == 1
    assert "partial_results" in warnings[0]


def test_page_status_warns_and_names_the_error_code_for_a_bad_source() -> None:
    status = {
        "source_type": "conversation",
        "source_key": "conversation",
        "status": "error",
        "has_more": False,
        "duration_ms": 5.0,
        "error_code": "timeout",
    }
    warnings = search_chats.page_status(_resp([], statuses=[status]))
    assert len(warnings) == 1
    assert "timeout" in warnings[0]
    assert "conversation" in warnings[0]


def test_page_status_tolerates_a_malformed_source_statuses_list() -> None:
    assert search_chats.page_status({"source_statuses": ["not-a-dict", None]}) == []


def test_page_status_can_warn_twice_in_one_page() -> None:
    bad = {
        "source_type": "conversation",
        "source_key": "conversation",
        "status": "error",
        "has_more": False,
        "duration_ms": 5.0,
        "error_code": "timeout",
    }
    warnings = search_chats.page_status(_resp([], partial=True, statuses=[bad]))
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# main() -- one page, hits
# ---------------------------------------------------------------------------


def test_main_one_page_of_hits_exits_0_with_the_summary_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_item("conv-1", "content", "Worker chat", "found it")]
    session = _FakeSearchSession([(200, _resp(items, statuses=[_ok_status()]))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker"]) == 0
    out = capsys.readouterr().out
    assert "1 hit(s) on 1 page(s); no more" in out
    assert "Worker chat" in out


def test_main_forwards_the_browser_flag_to_open_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, str] = {}
    session = _FakeSearchSession([(200, _resp([]))])

    def _open(browser: str = "chrome") -> Any:
        seen["browser"] = browser
        return session

    monkeypatch.setattr(search_chats, "open_session", _open)
    search_chats.main(["worker", "--browser", "chromium"])
    assert seen["browser"] == "chromium"


# ---------------------------------------------------------------------------
# main() -- paging
# ---------------------------------------------------------------------------


def test_main_follows_the_cursor_across_two_pages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    page1 = _resp([_item("a", title="Alpha")], cursor="3", statuses=[_ok_status(True)])
    page2 = _resp([_item("b", title="Bravo")], cursor=None, statuses=[_ok_status()])
    session = _FakeSearchSession([(200, page1), (200, page2)])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--pages", "2"]) == 0
    out = capsys.readouterr().out
    assert "2 hit(s) on 2 page(s); no more" in out
    assert "Alpha" in out and "Bravo" in out
    first_payload = session.session.calls[0][2]
    second_payload = session.session.calls[1][2]
    assert "cursor" not in first_payload
    assert second_payload["cursor"] == "3"


def test_main_stops_early_when_a_page_s_cursor_is_null(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--pages 5 but the first page already has cursor null: only one call."""
    session = _FakeSearchSession([(200, _resp([_item("a")], cursor=None))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--pages", "5"]) == 0
    assert len(session.session.calls) == 1


def test_main_hard_stops_at_max_pages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server that never returns a null cursor must not loop forever."""
    session = _InfiniteSearchSession()
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--pages", "999"]) == 0
    assert len(session.session.calls) == search_chats.MAX_PAGES
    out = capsys.readouterr().out
    assert f"on {search_chats.MAX_PAGES} page(s); more available" in out


# ---------------------------------------------------------------------------
# main() -- no hits, and a failed page
# ---------------------------------------------------------------------------


def test_main_no_hits_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSearchSession([(200, _resp([], cursor=None))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["zqxjkv-no-such-thing"]) == 1
    out = capsys.readouterr().out
    assert "0 hit(s) on 1 page(s); no more" in out


def test_main_a_failed_second_page_exits_1_but_keeps_page_one_s_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    page1 = _resp([_item("a", title="Alpha")], cursor="3", statuses=[_ok_status(True)])
    session = _FakeSearchSession([(200, page1), (500, {"error": "boom"})])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--pages", "2"]) == 1
    out = capsys.readouterr().out
    assert "Alpha" in out
    assert "page 2 failed" in out
    assert "500" in out


def test_main_treats_a_non_dict_200_response_as_a_failed_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSearchSession([(200, None)])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker"]) == 1
    assert "page 1 failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- --limit validation, before any call
# ---------------------------------------------------------------------------


def test_main_limit_41_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSearchSession([])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--limit", "41"]) == 2
    assert session.session.calls == []
    assert "40" in capsys.readouterr().out


def test_main_limit_exactly_40_is_allowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSearchSession([(200, _resp([_item("a")]))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker", "--limit", "40"]) == 0
    assert session.session.calls[0][2]["limit"] == 40


# ---------------------------------------------------------------------------
# main() -- --json
# ---------------------------------------------------------------------------


def test_main_json_writes_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [_item("a")]
    session = _FakeSearchSession([(200, _resp(items, statuses=[_ok_status()]))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "hits.json"
    assert search_chats.main(["worker", "--json", str(out_path)]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(doc) == {"query", "limit", "pages", "items", "has_more"}
    assert doc["query"] == "worker"
    assert doc["limit"] == search_chats.DEFAULT_LIMIT
    assert doc["pages"] == 1
    assert doc["has_more"] is False
    assert doc["items"] == items


def test_main_json_records_has_more_true_and_the_page_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page1 = _resp([_item("a")], cursor="3", statuses=[_ok_status(True)])
    page2 = _resp([_item("b")], cursor=None, statuses=[_ok_status(True)])
    session = _FakeSearchSession([(200, page1), (200, page2)])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "hits.json"
    search_chats.main(["worker", "--pages", "2", "--json", str(out_path)])
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["pages"] == 2
    assert doc["has_more"] is True
    assert len(doc["items"]) == 2


# ---------------------------------------------------------------------------
# main() -- degraded pages: partial_results and a bad source status
# ---------------------------------------------------------------------------


def test_main_prints_a_warning_line_for_partial_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resp = _resp([_item("a")], partial=True)
    session = _FakeSearchSession([(200, resp)])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker"]) == 0
    out = capsys.readouterr().out
    assert "warning" in out
    assert "partial_results" in out


def test_main_prints_a_warning_line_naming_the_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_status = {
        "source_type": "conversation",
        "source_key": "conversation",
        "status": "error",
        "has_more": False,
        "duration_ms": 1.0,
        "error_code": "timeout",
    }
    resp = _resp([], statuses=[bad_status])
    session = _FakeSearchSession([(200, resp)])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    search_chats.main(["worker"])
    out = capsys.readouterr().out
    assert "warning" in out
    assert "timeout" in out


# ---------------------------------------------------------------------------
# main() -- snippet rendering, end to end
# ---------------------------------------------------------------------------


def test_main_collapses_newlines_in_a_snippet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _item("a", "content", "t", "line one\nline two")
    session = _FakeSearchSession([(200, _resp([item]))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker"]) == 0
    out = capsys.readouterr().out
    assert "line one line two" in out
    assert "line one\nline two" not in out


def test_main_a_title_match_shows_no_literal_none_snippet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _item("a", "title", "Exact Title Match", snippet=None)
    session = _FakeSearchSession([(200, _resp([item]))])
    monkeypatch.setattr(search_chats, "open_session", lambda *a, **k: session)
    assert search_chats.main(["worker"]) == 0
    out = capsys.readouterr().out
    assert "Exact Title Match" in out
    assert "None" not in out
