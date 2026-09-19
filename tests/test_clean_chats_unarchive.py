"""Tests for scripts/clean_chats.py's --unarchive action (Stage 2 item M2).

--unarchive lists ARCHIVED conversations (``list_chats.query_for(limit,
archived=True)``, the same query ``list_chats.py --archived`` sends),
selects from them exactly the way --delete and --archive already do, and
--apply sends ``PATCH {"is_archived": false}`` per selected chat -- the same
conversation PATCH family ``pin_chat.py`` uses, captured 2026-09-20
alongside pin and unpin (references/endpoint-discovery.md, "Captured
2026-09-20, later"). This file owns only the --unarchive behaviour;
tests/test_commands.py's existing --delete/--archive tests are untouched
and must keep passing exactly as they are. Everything here runs over a fake
session; nothing here opens a socket (tests/conftest.py blocks it for any
test without a live marker).

Every test names the failure it defends against, the rule test_commands.py
uses.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    """Import a command module fresh by path (test_commands.py's own helper)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


clean_chats = _load("clean_chats")

ARCHIVED_CHATS = [
    {"id": "aaa", "title": "rp msgloom-topic-04 · terminology", "is_archived": True},
    {"id": "bbb", "title": "Holiday planning", "is_archived": True},
    {"id": "ccc", "title": "rp msgloom-topic-04 · screen", "is_archived": True},
]


# ---------------------------------------------------------------------------
# fixtures shared by the main()-level tests
# ---------------------------------------------------------------------------


class _Backend:
    """Fake ``session.session.call``: answers the archived listing and the
    unarchive PATCH. Every call is recorded (method, path, payload), so a
    dry run can be proven to have made only the one GET, and an --apply run
    proven to have sent exactly one PATCH per selected chat.
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
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
        if method == "GET":
            return 200, {"items": self.items}
        if method == "PATCH":
            return 200, {}
        raise AssertionError(f"unexpected method {method!r}")


class _Session:
    """Mirrors ``ChatGPTSession``'s shape for --unarchive: ``.session`` is
    the low-level transport it uses throughout. ``list_conversations``,
    ``delete`` and ``archive`` explode if reached, since --unarchive must
    use neither the default listing nor either of the other two actions.
    """

    def __init__(self, backend: _Backend) -> None:
        self.session = backend

    def list_conversations(self, limit: int = 0) -> list[dict]:
        raise AssertionError("--unarchive must not call list_conversations")

    def delete(self, chat: str) -> None:
        raise AssertionError("--unarchive must not call delete")

    def archive(self, chat: str) -> None:
        raise AssertionError("--unarchive must not call archive")


def _wire(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: _Session(backend))


def _refuse_before_a_session_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal must fire before the account is ever touched."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("open_session must not run for a refused invocation")

    monkeypatch.setattr(clean_chats, "open_session", _boom)


# ---------------------------------------------------------------------------
# the archived listing: query_for(limit, archived=True), through session.call
# ---------------------------------------------------------------------------


def test_unarchive_lists_through_the_archived_query_not_the_default_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--unarchive must read the ARCHIVED listing; the default listing
    excludes archived chats, so using it here would always find nothing."""
    backend = _Backend(ARCHIVED_CHATS)
    _wire(monkeypatch, backend)
    rc = clean_chats.main(["--match", "rp msgloom", "--unarchive"])
    assert rc == 0
    gets = [c for c in backend.calls if c[0] == "GET"]
    assert len(gets) == 1
    assert gets[0][1] == clean_chats.query_for(50, archived=True)


def test_unarchive_selects_the_same_way_delete_and_archive_do(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user's own chats share the account with the run's workers, so
    selecting the wrong ones is the failure mode this guards against."""
    backend = _Backend(ARCHIVED_CHATS)
    _wire(monkeypatch, backend)
    rc = clean_chats.main(["--match", "rp msgloom", "--unarchive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out
    assert "ccc" in out
    assert "bbb" not in out  # "Holiday planning" is the user's own chat


# ---------------------------------------------------------------------------
# dry run: reads, prints, never writes
# ---------------------------------------------------------------------------


def test_unarchive_dry_run_never_patches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default must never touch the account, however it is invoked."""
    backend = _Backend(ARCHIVED_CHATS)
    _wire(monkeypatch, backend)
    rc = clean_chats.main(["--match", "rp msgloom", "--unarchive"])
    assert rc == 0
    assert all(method == "GET" for method, _path, _payload in backend.calls)
    out = capsys.readouterr().out
    assert "dry run: 2 conversation(s) would be unarchived" in out


def test_unarchive_with_no_matches_reports_nothing_and_never_patches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(ARCHIVED_CHATS)
    _wire(monkeypatch, backend)
    rc = clean_chats.main(["--match", "no such title", "--unarchive", "--apply"])
    assert rc == 0
    assert backend.calls == [("GET", clean_chats.query_for(50, archived=True), None)]
    assert "nothing matches" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --apply sends PATCH {"is_archived": false} for exactly the matches
# ---------------------------------------------------------------------------


def test_unarchive_apply_sends_is_archived_false_for_exactly_the_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(ARCHIVED_CHATS)
    _wire(monkeypatch, backend)
    rc = clean_chats.main(["--match", "rp msgloom", "--unarchive", "--apply"])
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert [p[1] for p in patches] == [
        "/backend-api/conversation/aaa",
        "/backend-api/conversation/ccc",
    ]
    assert all(p[2] == {"is_archived": False} for p in patches)
    assert "2 conversation(s) unarchived" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the three-way exclusivity (exit 2), before a session ever opens
# ---------------------------------------------------------------------------


def test_all_three_actions_together_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = clean_chats.main(["--match", "x", "--delete", "--archive", "--unarchive"])
    assert rc == 2


def test_unarchive_with_delete_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert clean_chats.main(["--match", "x", "--delete", "--unarchive"]) == 2


def test_unarchive_with_archive_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert clean_chats.main(["--match", "x", "--archive", "--unarchive"]) == 2


def test_none_of_the_three_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old two-way check must not have quietly become "unarchive is
    optional"."""
    _refuse_before_a_session_opens(monkeypatch)
    assert clean_chats.main(["--match", "x"]) == 2


def test_unarchive_alone_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three-way refusal must not reject a lone --unarchive."""
    backend = _Backend([])
    _wire(monkeypatch, backend)
    assert clean_chats.main(["--match", "x", "--unarchive"]) == 0
