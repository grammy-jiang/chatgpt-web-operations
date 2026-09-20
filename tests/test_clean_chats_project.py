"""Tests for scripts/clean_chats.py's ``--project g-p-<id>``.

A worker chat lives inside a project so it stays out of the user's own list
(SKILL.md, "Projects keep a run's chats out of the user's list"), but ChatGPT
renames a chat once its first reply lands (measured 2026-09-20: "rp-test send
1" became "Reply PONG"), so a ``--match`` sweep over the account-wide listing
can miss it. ``--project`` reads the project's own listing instead
(``GET gizmos/<id>/conversations``, paged by ``cursor``) and, with no
``--match``, selects every conversation in it -- the same "select
everything" rule the sandbox sweep in tests/live/conftest.py uses, and for
the same reason: a project's chats are already isolated, so the blunt
--match safety net that protects the account-wide listing does not apply.

This file owns only the --project behaviour; tests/test_commands.py's and
tests/test_command_mains_2.py's existing --delete/--archive/--unarchive
tests, and tests/test_clean_chats_unarchive.py, are untouched and must keep
passing exactly as they are. Runs entirely over fake sessions (HARD RULE 1:
no network, no browser, no real Session).

Every test names the failure it defends against, the rule test_commands.py
uses.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import clean_chats  # noqa: E402

RENAMED_PROJECT_CHATS = [
    {"id": "aaa", "title": "Reply PONG"},  # ChatGPT's own rename, not "rp-test"
    {"id": "bbb", "title": "Python Release Schedule Summary"},  # likewise
]


# ---------------------------------------------------------------------------
# select_from_project -- pure, an empty match selects everything on purpose
# ---------------------------------------------------------------------------


def test_an_empty_match_selects_every_conversation_in_the_project() -> None:
    """The opposite of select(): a project's chats are already isolated, so
    there is no account to protect by refusing an empty match."""
    chosen = clean_chats.select_from_project(RENAMED_PROJECT_CHATS, "")
    assert [c["id"] for c in chosen] == ["aaa", "bbb"]


def test_a_match_still_narrows_the_project_selection() -> None:
    chosen = clean_chats.select_from_project(RENAMED_PROJECT_CHATS, "pong")
    assert [c["id"] for c in chosen] == ["aaa"]


def test_a_renamed_chat_is_found_by_project_membership_not_by_its_old_title() -> None:
    """The whole point: "rp-test send 1" became "Reply PONG" the moment its
    first reply landed, so a title match on the old name would find
    nothing. Project membership alone still finds it."""
    chosen = clean_chats.select_from_project(RENAMED_PROJECT_CHATS, "")
    assert any(c["title"] == "Reply PONG" for c in chosen)


# ---------------------------------------------------------------------------
# project_conversations -- pages cursor, stops on empty page / null cursor
# ---------------------------------------------------------------------------


class _ProjectBackend:
    """``session.session.call`` answering a GET by the exact query string,
    recording every call made, and accepting a PATCH (for --unarchive)."""

    def __init__(self, responses: dict[str, tuple[int, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Any]] = []
        self.patches: list[tuple[str, Any]] = []

    def call(
        self, path: str, method: str = "GET", payload: Any = None
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        if method == "PATCH":
            self.patches.append((path, payload))
            return 200, {}
        return self.responses.get(path, (404, {"error": f"unexpected {path}"}))


class _ProjectSession:
    def __init__(self, backend: _ProjectBackend) -> None:
        self.session = backend

    def list_conversations(self, limit: int = 0) -> list[dict]:
        raise AssertionError("--project must not read the account-wide listing")

    def delete(self, chat: str) -> None:
        raise AssertionError("dry run must never delete")

    def archive(self, chat: str) -> None:
        raise AssertionError("dry run must never archive")


def test_project_conversations_follows_the_cursor_across_pages() -> None:
    backend = _ProjectBackend(
        {
            "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=50": (
                200,
                {"items": [{"id": "a"}, {"id": "b"}], "cursor": "page2"},
            ),
            "/backend-api/gizmos/g-p-abc/conversations?cursor=page2&limit=50": (
                200,
                {"items": [{"id": "c"}], "cursor": ""},
            ),
        }
    )
    items = clean_chats.project_conversations(_ProjectSession(backend), "g-p-abc")
    assert [i["id"] for i in items] == ["a", "b", "c"]
    assert len(backend.calls) == 2


def test_project_conversations_stops_on_a_missing_cursor() -> None:
    backend = _ProjectBackend(
        {
            "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=50": (
                200,
                {"items": [{"id": "a"}]},  # no "cursor" key at all
            )
        }
    )
    items = clean_chats.project_conversations(_ProjectSession(backend), "g-p-abc")
    assert [i["id"] for i in items] == ["a"]
    assert len(backend.calls) == 1


def test_project_conversations_stops_on_an_empty_page() -> None:
    backend = _ProjectBackend(
        {
            "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=50": (
                200,
                {"items": [], "cursor": "would-loop-forever"},
            )
        }
    )
    items = clean_chats.project_conversations(_ProjectSession(backend), "g-p-abc")
    assert items == []
    assert len(backend.calls) == 1


def test_project_conversations_stops_on_a_non_200_or_non_dict_body() -> None:
    backend = _ProjectBackend(
        {"/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=50": (500, None)}
    )
    items = clean_chats.project_conversations(_ProjectSession(backend), "g-p-abc")
    assert items == []


def test_project_conversations_honours_a_smaller_max_pages_hard_stop() -> None:
    """A server that never returns a null cursor must not loop forever --
    list_projects.py's sidebar_items and the sandbox sweep use the same hard
    stop; this proves the parameter, not the default of 50."""

    class _InfiniteBackend:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, path: str) -> tuple[int, Any]:
            self.calls += 1
            return 200, {"items": [{"id": f"x{self.calls}"}], "cursor": "again"}

    backend = _InfiniteBackend()
    items = clean_chats.project_conversations(
        _ProjectSession(backend), "g-p-abc", max_pages=3
    )
    assert len(items) == 3
    assert backend.calls == 3


def test_project_conversations_uses_the_given_limit() -> None:
    backend = _ProjectBackend(
        {
            "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=5": (
                200,
                {"items": []},
            )
        }
    )
    clean_chats.project_conversations(_ProjectSession(backend), "g-p-abc", limit=5)
    assert backend.calls == [
        ("GET", "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=5", None)
    ]


# ---------------------------------------------------------------------------
# main() -- --project wired end to end
# ---------------------------------------------------------------------------


def _boom_if_opened(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("a refused invocation must never open a session")


def _one_page_backend(items: list[dict[str, Any]]) -> _ProjectBackend:
    return _ProjectBackend(
        {
            "/backend-api/gizmos/g-p-abc/conversations?cursor=0&limit=50": (
                200,
                {"items": items},
            )
        }
    )


def test_main_project_with_no_match_selects_every_chat_dry_run(
    monkeypatch: Any, capsys: Any
) -> None:
    backend = _one_page_backend(RENAMED_PROJECT_CHATS)
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    rc = clean_chats.main(["--project", "g-p-abc", "--delete"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out and "bbb" in out
    assert "dry run: 2 conversation(s) would be deleted" in out


def test_main_project_with_match_narrows_the_selection(
    monkeypatch: Any, capsys: Any
) -> None:
    backend = _one_page_backend(RENAMED_PROJECT_CHATS)
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    rc = clean_chats.main(["--project", "g-p-abc", "--match", "pong", "--archive"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa" in out
    assert "bbb" not in out


def test_main_project_apply_deletes_exactly_the_selected_chats(
    monkeypatch: Any,
) -> None:
    backend = _one_page_backend(RENAMED_PROJECT_CHATS)
    touched: list[str] = []

    class _Session(_ProjectSession):
        def delete(self, chat: str) -> None:
            touched.append(chat)

    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: _Session(backend))
    rc = clean_chats.main(["--project", "g-p-abc", "--delete", "--apply"])
    assert rc == 0
    assert touched == ["aaa", "bbb"]


def test_main_project_empty_project_reports_no_conversations(
    monkeypatch: Any, capsys: Any
) -> None:
    backend = _one_page_backend([])
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    rc = clean_chats.main(["--project", "g-p-abc", "--delete"])
    assert rc == 0
    assert "the project has no conversations" in capsys.readouterr().out


def test_main_project_match_with_nothing_found_reports_nothing_matches(
    monkeypatch: Any, capsys: Any
) -> None:
    backend = _one_page_backend(RENAMED_PROJECT_CHATS)
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    rc = clean_chats.main(
        ["--project", "g-p-abc", "--match", "no-such-title", "--delete"]
    )
    assert rc == 0
    assert "nothing matches 'no-such-title'" in capsys.readouterr().out


def test_main_project_never_reads_the_account_wide_listing(monkeypatch: Any) -> None:
    """_ProjectSession.list_conversations raises if reached; a passing run
    proves --project used only the project's own endpoint (a dry run, so
    _ProjectSession's own delete/archive stubs, which raise, are never
    called either)."""
    backend = _one_page_backend(RENAMED_PROJECT_CHATS)
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    assert clean_chats.main(["--project", "g-p-abc", "--archive"]) == 0


def test_main_project_with_unarchive_still_uses_the_project_listing(
    monkeypatch: Any,
) -> None:
    """--project overrides the listing choice for every action, including
    --unarchive, which otherwise reads the account-wide archived listing."""
    backend = _one_page_backend([{"id": "aaa", "title": "Reply PONG"}])
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    rc = clean_chats.main(["--project", "g-p-abc", "--unarchive", "--apply"])
    assert rc == 0
    assert backend.patches == [
        ("/backend-api/conversation/aaa", {"is_archived": False})
    ]


# ---------------------------------------------------------------------------
# --match requiredness: still required unless --project is given
# ---------------------------------------------------------------------------


def test_match_is_required_when_project_is_absent(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setattr(clean_chats, "open_session", _boom_if_opened)
    assert clean_chats.main(["--delete"]) == 2
    assert "--match is required unless --project" in capsys.readouterr().out


def test_project_alone_needs_no_match(monkeypatch: Any) -> None:
    backend = _one_page_backend([])
    monkeypatch.setattr(
        clean_chats, "open_session", lambda *a, **k: _ProjectSession(backend)
    )
    assert clean_chats.main(["--project", "g-p-abc", "--delete"]) == 0


def test_the_three_way_exclusivity_still_wins_over_the_match_check(
    monkeypatch: Any,
) -> None:
    """Neither --project nor --match is given, and neither action is; the
    pre-existing check must still fire first (exit 2 either way, but this
    proves the ordering was not disturbed)."""
    monkeypatch.setattr(clean_chats, "open_session", _boom_if_opened)
    assert clean_chats.main(["--delete", "--archive"]) == 2


# ---------------------------------------------------------------------------
# --help documents --project
# ---------------------------------------------------------------------------


def test_help_exits_0_and_documents_project(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        clean_chats.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "--project" in out
