"""Tests for Stage 1 items 2-4 of ROADMAP.md.

    2. list_projects.py: paging past the sidebar's first page, and --id
       reading gizmos/<id> directly (full instructions, memory scope, files).
    3. probe_account.py: the wham/usage lines, added without ever turning
       CLEAR into NOT CLEAR.
    4. review_topic.py's seventh invariant: was the profile captured, and is
       it fresh, before the newest round.

Same house style as tests/test_commands.py: small pure functions tested over
data plus a thin main() tested over a fake session, and every test's name
and docstring say which failure it defends against.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import list_projects  # noqa: E402
import probe_account  # noqa: E402
import probe_send_gates  # noqa: E402
import profile_context  # noqa: E402
import review_topic as rt  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# shared fakes -- a ChatGPTSession stand-in used across every section below
# ---------------------------------------------------------------------------


class _Routed:
    """``session.session.call`` keyed by URL prefix; the longest match wins.

    Longest-prefix-first matching keeps ``/backend-api/gizmos/<id>`` from
    shadowing ``/backend-api/gizmos/<id>/conversations`` (a shorter prefix
    registered first would otherwise swallow both).
    """

    def __init__(self, responses: dict[str, tuple[int, dict]]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def call(self, url: str) -> tuple[int, dict]:
        self.urls.append(url)
        for prefix in sorted(self.responses, key=len, reverse=True):
            if url.startswith(prefix):
                return self.responses[prefix]
        return 404, {"error": f"unrouted: {url}"}


class _PagedSidebar:
    """Feeds fixed sidebar pages in call order, regardless of the cursor value."""

    def __init__(self, pages: list[tuple[int, dict]]) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []

    def call(self, url: str) -> tuple[int, dict]:
        self.urls.append(url)
        if not self.pages:
            return 200, {"items": [], "cursor": None}
        return self.pages.pop(0)


class _InfiniteSidebar:
    """Always answers with one item and a fresh cursor; never ends on its own."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def call(self, url: str) -> tuple[int, dict]:
        self.urls.append(url)
        return 200, {"items": [{"gizmo": {"id": "g-p-x"}}], "cursor": "again"}


class _FakeSession:
    """A ``ChatGPTSession`` stand-in: ``.session.call`` plus ``.list_conversations``."""

    def __init__(self, backend, chats: list[dict] | None = None) -> None:
        self.session = backend
        self._chats = [] if chats is None else chats

    def list_conversations(self, limit: int = 0) -> list[dict]:
        return self._chats


# ---------------------------------------------------------------------------
# list_projects -- Stage 1 item 2: paging, --id, --files
# ---------------------------------------------------------------------------


def test_sidebar_items_walks_every_page_until_the_cursor_is_null() -> None:
    """32 projects behind a 5-item page must not be read as only 5."""
    backend = _PagedSidebar(
        [
            (200, {"items": [{"gizmo": {"id": "g-p-1"}}], "cursor": "c1"}),
            (200, {"items": [{"gizmo": {"id": "g-p-2"}}], "cursor": "c2"}),
            (200, {"items": [{"gizmo": {"id": "g-p-3"}}], "cursor": None}),
        ]
    )
    items = list_projects.sidebar_items(_FakeSession(backend))
    assert [i["gizmo"]["id"] for i in items] == ["g-p-1", "g-p-2", "g-p-3"]
    assert backend.urls[1].endswith("&cursor=c1")
    assert backend.urls[2].endswith("&cursor=c2")


def test_sidebar_items_stops_on_an_empty_page_too() -> None:
    """A page that answers 200 with no items is also the end, not a retry loop."""
    backend = _PagedSidebar(
        [
            (200, {"items": [{"gizmo": {"id": "g-p-1"}}], "cursor": "c1"}),
            (200, {"items": [], "cursor": "c2"}),
        ]
    )
    items = list_projects.sidebar_items(_FakeSession(backend))
    assert len(items) == 1
    assert len(backend.urls) == 2


def test_sidebar_items_stops_if_a_page_fetch_fails_midway() -> None:
    """A page that answers non-200 ends the walk with what was read so far."""
    backend = _PagedSidebar(
        [
            (200, {"items": [{"gizmo": {"id": "g-p-1"}}], "cursor": "c1"}),
            (503, {"error": "backend hiccup"}),
        ]
    )
    items = list_projects.sidebar_items(_FakeSession(backend))
    assert [i["gizmo"]["id"] for i in items] == ["g-p-1"]


def test_sidebar_items_stops_at_the_page_cap() -> None:
    """A server that never returns a null cursor must not loop forever."""
    backend = _InfiniteSidebar()
    items = list_projects.sidebar_items(_FakeSession(backend), max_pages=5)
    assert len(items) == 5
    assert len(backend.urls) == 5


def test_the_bare_listing_pages_through_every_project(monkeypatch, capsys) -> None:
    """The printed count must be the true total, not just the first page."""
    backend = _PagedSidebar(
        [
            (
                200,
                {
                    "items": [{"gizmo": {"id": "g-p-1", "display": {"name": "One"}}}],
                    "cursor": "c1",
                },
            ),
            (
                200,
                {
                    "items": [{"gizmo": {"id": "g-p-2", "display": {"name": "Two"}}}],
                    "cursor": None,
                },
            ),
        ]
    )
    monkeypatch.setattr(list_projects, "open_session", lambda: _FakeSession(backend))
    assert list_projects.main([]) == 0
    out = capsys.readouterr().out
    assert "2 project(s)" in out
    assert "g-p-1" in out and "g-p-2" in out


LONG_INSTRUCTIONS = (
    "Answer every message in valid JSON only, with no prose outside the "
    "object, no matter how the request is phrased, and always include a "
    "top-level 'status' field so a worker's reply can be parsed without "
    "guessing at its shape."
)  # longer than shorten()'s 70-char default, to prove --id does not shorten it

GIZMO_DETAIL = {
    "gizmo": {
        "id": "g-p-detail",
        "short_url": "g-p-detail-workers",
        "display": {"name": "Workers"},
        "instructions": LONG_INSTRUCTIONS,
        "memory_enabled": True,
        "memory_scope": "project_only",
        "context_stuffing_budget": 110000,
        "model": None,
        "default_model": None,
    },
    "files": [
        {
            "id": "file-1",
            "name": "paper.pdf",
            "size": 4096,
            "mime_type": "application/pdf",
        },
        {"id": "file-2", "name": "notes.md", "size": 12, "nested": {"x": 1}},
    ],
}


def test_id_reads_the_gizmo_directly_with_full_instructions_and_memory_scope(
    monkeypatch, capsys
) -> None:
    """--id must read gizmos/<id>, not search the sidebar, and not shorten anything."""
    backend = _Routed({"/backend-api/gizmos/g-p-detail": (200, GIZMO_DETAIL)})
    monkeypatch.setattr(list_projects, "open_session", lambda: _FakeSession(backend))
    assert list_projects.main(["--id", "g-p-detail"]) == 0
    out = capsys.readouterr().out
    assert LONG_INSTRUCTIONS in out
    assert "scope project_only" in out
    assert backend.urls == ["/backend-api/gizmos/g-p-detail"]


def test_files_flag_lists_scalar_fields_only(monkeypatch, capsys) -> None:
    """A nested field must never reach the terminal; its shape is unknown."""
    backend = _Routed({"/backend-api/gizmos/g-p-detail": (200, GIZMO_DETAIL)})
    monkeypatch.setattr(list_projects, "open_session", lambda: _FakeSession(backend))
    assert list_projects.main(["--id", "g-p-detail", "--files"]) == 0
    out = capsys.readouterr().out
    assert "file-1" in out and "paper.pdf" in out
    assert "mime_type=application/pdf" in out
    assert "nested" not in out
    assert "2 file(s)" in out


def test_id_with_chats_queries_cursor_zero(monkeypatch, capsys) -> None:
    """The real endpoint takes cursor=0; dropping it is how --chats used to drift."""
    conversations = "/backend-api/gizmos/g-p-detail/conversations"
    backend = _Routed(
        {
            "/backend-api/gizmos/g-p-detail": (200, GIZMO_DETAIL),
            conversations: (200, {"items": [{"id": "conv-1", "title": "hello"}]}),
        }
    )
    monkeypatch.setattr(list_projects, "open_session", lambda: _FakeSession(backend))
    assert list_projects.main(["--id", "g-p-detail", "--chats"]) == 0
    out = capsys.readouterr().out
    assert "conv-1" in out
    assert "1 conversation(s) in Workers" in out
    assert backend.urls[-1] == f"{conversations}?cursor=0&limit=20"


def test_an_unknown_id_prints_no_project_and_exits_1(monkeypatch, capsys) -> None:
    """A 404 from gizmos/<id> must not crash or print a stale table."""
    missing = {"/backend-api/gizmos/g-p-missing": (404, {"detail": "not found"})}
    backend = _Routed(missing)
    monkeypatch.setattr(list_projects, "open_session", lambda: _FakeSession(backend))
    assert list_projects.main(["--id", "g-p-missing"]) == 1
    assert "no project with id g-p-missing" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# profile_context -- project_of moved to list_projects.py, item 1.2(c)
# ---------------------------------------------------------------------------


def test_profile_context_still_imports_project_of_from_list_projects() -> None:
    """The move must not fork the reader into two different functions."""
    assert profile_context.project_of is list_projects.project_of


def test_profile_context_project_document_is_unchanged_by_the_move(monkeypatch) -> None:
    """collect() must still read gizmos/<id> through the now-imported project_of."""
    reads = {
        "/backend-api/user_system_messages": (200, {}),
        "/backend-api/memories?include_memory_entries=false": (200, {}),
        "/backend-api/memories?include_memory_entries=true": (200, {"memories": []}),
        "/backend-api/settings/user": (200, {}),
        "/backend-api/models": (200, {}),
        "/backend-api/gizmos/g-p-moved": (
            200,
            {
                "gizmo": {
                    "id": "g-p-moved",
                    "short_url": "g-p-moved-x",
                    "display": {"name": "Moved"},
                    "instructions": "still here",
                    "memory_enabled": True,
                    "memory_scope": "global",
                },
                "files": [{"id": "f1", "name": "a.txt", "extra": {"x": 1}}],
            },
        ),
    }
    monkeypatch.setattr(profile_context, "profile_config", lambda cc, browser: {})
    session = _FakeSession(_Routed(reads))
    doc = profile_context.collect(session, None, "chrome", "g-p-moved")
    assert doc["errors"] == []
    assert doc["project"]["instructions"] == "still here"
    assert doc["project"]["memory_scope"] == "global"
    assert doc["project"]["files"] == [{"id": "f1", "name": "a.txt"}]
    assert doc["project"]["url"] == "https://chatgpt.com/g/g-p-moved-x/project"


# ---------------------------------------------------------------------------
# probe_account -- Stage 1 item 3: wham/usage lines
# ---------------------------------------------------------------------------


def _expected_resets(epoch: int) -> str:
    """The same local-time conversion usage_lines is expected to perform."""
    when = datetime.fromtimestamp(epoch, UTC).astimezone()
    return when.isoformat(timespec="seconds")


def test_usage_lines_from_the_fixture() -> None:
    """The real (sanitized) wham/usage shape: three lines, none about sends."""
    payload = json.loads((FIXTURES / "wham_usage.json").read_text(encoding="utf-8"))
    lines = probe_account.usage_lines(payload)
    assert lines == [
        f"plan pro: weekly window 0% used, resets {_expected_resets(1790434491)}",
        "credits: balance 102.8469730000, 2 reset credit(s)",
        "This window and these credits do not cover chat sends.",
    ]


def test_usage_lines_survives_missing_fields() -> None:
    """A reshaped or empty payload must print '?' fields, never raise."""
    assert probe_account.usage_lines({}) == [
        "plan ?: weekly window ?% used, resets ?",
        "credits: balance ?, ? reset credit(s)",
        "This window and these credits do not cover chat sends.",
    ]


def test_main_reports_usage_after_clear(monkeypatch, capsys) -> None:
    """A healthy account gets the plan window and credits after CLEAR."""
    payload = json.loads((FIXTURES / "wham_usage.json").read_text(encoding="utf-8"))
    backend = _Routed(
        {"/backend-api/me": (200, {}), "/backend-api/wham/usage": (200, payload)}
    )
    session = _FakeSession(backend, chats=[{"id": "c1"}, {"id": "c2"}])
    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", lambda browser: session)
    assert probe_account.main([]) == 0
    out = capsys.readouterr().out
    assert "CLEAR: authenticated, /backend-api/me 200, listing returned 2" in out
    assert "plan pro:" in out
    assert "do not cover chat sends" in out


def test_main_keeps_exit_0_when_usage_is_unavailable(monkeypatch, capsys) -> None:
    """A failed wham/usage read must not turn CLEAR into NOT CLEAR."""
    backend = _Routed(
        {"/backend-api/me": (200, {}), "/backend-api/wham/usage": (503, {})}
    )
    session = _FakeSession(backend)
    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", lambda browser: session)
    assert probe_account.main([]) == 0
    out = capsys.readouterr().out
    assert "usage: unavailable (HTTP 503)" in out
    assert "NOT CLEAR" not in out


def test_main_reports_not_clear_when_authentication_fails(monkeypatch, capsys) -> None:
    """An expired cookie must be reported, not raised past main()."""

    def _boom(browser: str) -> None:
        raise RuntimeError("expired session cookie")

    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", _boom)
    assert probe_account.main([]) == 1
    assert "NOT CLEAR: could not authenticate" in capsys.readouterr().out


def test_main_reports_not_clear_when_me_fails(monkeypatch, capsys) -> None:
    """A non-200 /me is the account-level failure this command probes for."""
    backend = _Routed({"/backend-api/me": (401, {})})
    session = _FakeSession(backend)
    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", lambda browser: session)
    assert probe_account.main([]) == 1
    assert "NOT CLEAR: /backend-api/me returned 401" in capsys.readouterr().out


def test_main_reports_not_clear_when_listing_fails(monkeypatch, capsys) -> None:
    """A listing exception after a good /me must still fail clearly."""

    class _Boom(_FakeSession):
        def list_conversations(self, limit: int = 0) -> list[dict]:
            raise RuntimeError("listing failed")

    backend = _Routed({"/backend-api/me": (200, {})})
    fake = _Boom(backend)
    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", lambda browser: fake)
    assert probe_account.main([]) == 1
    assert "NOT CLEAR: listing failed" in capsys.readouterr().out


def test_probe_account_help_exits_0_without_creating_a_session(monkeypatch) -> None:
    """--help must never authenticate; argparse's own exit must win the race."""

    def _boom(browser: str) -> None:
        raise AssertionError("ChatGPTSession must not be built for --help")

    monkeypatch.setattr(probe_account.cc, "ChatGPTSession", _boom)
    with pytest.raises(SystemExit) as exc:
        probe_account.main(["--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# probe_send_gates -- Stage 1 item 3: --help must not run the probe
# ---------------------------------------------------------------------------


def test_probe_send_gates_help_exits_0_without_opening_a_session(monkeypatch) -> None:
    """Before this fix, --help had no argparse and ran the live probe instead."""

    def _boom(browser: str = "chrome"):
        raise AssertionError("open_session must not run for --help")

    monkeypatch.setattr(probe_send_gates, "open_session", _boom)
    with pytest.raises(SystemExit) as exc:
        probe_send_gates.main(["--help"])
    assert exc.value.code == 0


def test_fetch_posts_to_the_sentinel_and_parses_the_json_reply(monkeypatch) -> None:
    """fetch() itself (unchanged by the argparse fix) must still be exercised.

    Only urlopen is faked, so every line that builds the request -- cookie,
    bearer token, user agent -- actually runs; nothing here touches a socket.
    """

    class _Helpers:
        UA = "test-agent"

    class _FakeClient:
        @staticmethod
        def _helpers():
            return _Helpers()

    monkeypatch.setattr(probe_send_gates, "load_client", lambda: _FakeClient)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"persona": "chatgpt-paid"}'

    seen: dict[str, str] = {}

    def _fake_urlopen(request, timeout: int = 60):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    class _Inner:
        cookie = "sess=abc"
        token = "tok-123"

    class _Session:
        session = _Inner()

    assert probe_send_gates.fetch(_Session()) == {"persona": "chatgpt-paid"}
    assert seen == {"method": "POST", "url": probe_send_gates.SENTINEL}


def test_probe_send_gates_reports_the_sentinel_http_error(monkeypatch, capsys) -> None:
    """The existing HTTPError handling must survive the argparse change."""
    monkeypatch.setattr(
        probe_send_gates, "open_session", lambda browser="chrome": object()
    )

    def _boom(session):
        raise urllib.error.HTTPError(
            "https://chatgpt.com/x", 500, "boom", None, io.BytesIO(b"server exploded")
        )

    monkeypatch.setattr(probe_send_gates, "fetch", _boom)
    assert probe_send_gates.main([]) == 1
    assert "sentinel returned 500" in capsys.readouterr().out


def test_probe_send_gates_blocks_when_a_gate_is_required(monkeypatch, capsys) -> None:
    """The exit code (1, browser needed) must be unchanged by the argparse fix."""
    monkeypatch.setattr(
        probe_send_gates, "open_session", lambda browser="chrome": object()
    )
    monkeypatch.setattr(
        probe_send_gates,
        "fetch",
        lambda session: {"turnstile": {"required": True}, "persona": "chatgpt-paid"},
    )
    assert probe_send_gates.main([]) == 1
    assert "A browser is needed to send: turnstile" in capsys.readouterr().out


def test_probe_send_gates_passes_when_no_gate_is_required(monkeypatch, capsys) -> None:
    """The exit code (0, no browser needed) must be unchanged by the argparse fix."""
    monkeypatch.setattr(
        probe_send_gates, "open_session", lambda browser="chrome": object()
    )
    monkeypatch.setattr(
        probe_send_gates,
        "fetch",
        lambda session: {"turnstile": {"required": False}, "persona": "chatgpt-paid"},
    )
    assert probe_send_gates.main([]) == 0
    assert "No browser-only control is in force" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# review_topic -- Stage 1 item 4: the seventh, warn-only invariant
# ---------------------------------------------------------------------------


def _clean_round(run: Path) -> None:
    """A one-paper round with nothing wrong, so invariant 7 is isolated."""
    (run / "search").mkdir(parents=True, exist_ok=True)
    (run / "screen").mkdir(parents=True, exist_ok=True)
    (run / "analysis").mkdir(parents=True, exist_ok=True)
    (run / "round_context.json").write_text(json.dumps({"round": 1}))
    (run / "search" / "candidates.jsonl").write_text(json.dumps({"id": "c0"}) + "\n")
    (run / "screen" / "cheap_scores.jsonl").write_text(json.dumps({"id": "c0"}) + "\n")
    (run / "screen" / "shortlist.json").write_text(json.dumps([{"id": "s0"}]))
    (run / "screen" / "admission.json").write_text(
        json.dumps({"decisions": [{"paper_id": "p1", "decision": "ADMIT"}]})
    )
    (run / "screen" / "screened.jsonl").write_text(json.dumps({"id": "p1"}) + "\n")
    (run / "analysis" / "p1_analysis.json").write_text("{}")


def test_invariant_7_warns_when_no_profile_context_file_exists(
    tmp_path: Path, capsys
) -> None:
    """A topic that never ran profile_context.py must say so, not stay silent."""
    _clean_round(tmp_path / "runs" / "r1")
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "profile context: none recorded" in out
    assert "profile_context.py --json" in out
    assert str(tmp_path) in out


def test_invariant_7_needs_no_ledger_to_report_a_capture(
    tmp_path: Path, capsys
) -> None:
    """No conversations.json yet must not crash or fabricate staleness."""
    _clean_round(tmp_path / "runs" / "r1")
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-19T00:00:00+00:00"})
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "captured_at 2026-09-19T00:00:00+00:00" in out
    assert "older than" not in out


def test_invariant_7_is_silent_when_the_capture_is_fresh(
    tmp_path: Path, capsys
) -> None:
    """A capture taken at or after the newest round's start must not warn."""
    _clean_round(tmp_path / "runs" / "r1")
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "conversations.json").write_text(
        json.dumps(
            [
                {"round": 1, "status": "sent"},  # no sent_at: skipped, not a crash
                {"round": 1, "status": "done", "sent_at": "2026-09-17T00:00:00+00:00"},
            ]
        )
    )
    (chat / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-19T00:00:00+00:00"})
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    expect = "profile context: profile_context.json, captured_at "
    assert expect + "2026-09-19T00:00:00+00:00" in out
    assert "older than the newest round" not in out


def test_invariant_7_warns_when_the_capture_predates_the_newest_round(
    tmp_path: Path, capsys
) -> None:
    """A capture from before a later round even started must warn.

    Warn-only: the exit code must stay whatever the first six invariants
    say, which is 0 here since the round itself is clean.
    """
    _clean_round(tmp_path / "runs" / "r1")
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "conversations.json").write_text(
        json.dumps(
            [{"round": 1, "status": "done", "sent_at": "2026-09-17T00:00:00+00:00"}]
        )
    )
    (chat / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-10T00:00:00+00:00"})
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    expect = "profile context: profile_context.json, captured_at "
    assert expect + "2026-09-10T00:00:00+00:00" in out
    assert "older than the newest round's start" in out


def test_invariant_7_picks_the_newest_capture_by_captured_at(
    tmp_path: Path, capsys
) -> None:
    """A capture that merely sorts late by filename must not win on that alone."""
    _clean_round(tmp_path / "runs" / "r1")
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "profile_context.a-sorts-before-the-real-one.json").write_text(
        json.dumps({"captured_at": "2026-09-01T00:00:00+00:00"})
    )
    (chat / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-19T00:00:00+00:00"})
    )
    assert rt.main([str(tmp_path)]) == 0
    assert "captured_at 2026-09-19T00:00:00+00:00" in capsys.readouterr().out


def test_invariant_7_ignores_a_capture_file_with_no_captured_at(
    tmp_path: Path, capsys
) -> None:
    """A malformed capture (no captured_at) must not crash or count as recorded."""
    _clean_round(tmp_path / "runs" / "r1")
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "profile_context.json").write_text(json.dumps({"model": {}}))
    assert rt.main([str(tmp_path)]) == 0
    assert "profile context: none recorded" in capsys.readouterr().out
