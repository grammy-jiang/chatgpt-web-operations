"""Tests for scripts/delete_project.py (Stage 2 item M5, delete over HTTP).

Captured 2026-09-20 on a throwaway project, deleted in the same minute
(references/endpoint-discovery.md, "Captured 2026-09-20, project create and
delete"): `DELETE /backend-api/gizmos/<g-p-id>` with no body removes the
project, and the following `GET /backend-api/gizmos/<id>` answers 404. This
command never reads tests/live/sandbox.json; the safety net is the
--expect-name match plus the rp-test-prefix rule, both proved here as pure
functions before any test touches ``main()``. Everything here runs over a
fake session; nothing here opens a socket (tests/conftest.py blocks it for
any test without a live marker).

Every test names the failure it defends against, the rule test_commands.py
uses.
"""

from __future__ import annotations

import importlib.util
import json
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


delete_project = _load("delete_project")

GIZMO_PAYLOAD = {
    "gizmo": {
        "id": "g-p-throwaway123",
        "short_url": "g-p-throwaway123-rp-test-lifecycle",
        "display": {"name": "rp-test lifecycle"},
        "instructions": "",
        "memory_enabled": False,
        "memory_scope": "project_v2",
    },
    "files": [],
}


# ---------------------------------------------------------------------------
# fixtures shared by the main()-level tests
# ---------------------------------------------------------------------------


class _Backend:
    """Fake ``session.session.call`` for delete_project.py.

    GET gizmos/<id> returns the current project, and GET
    gizmos/<id>/conversations returns ``conversations``. A DELETE marks the
    project gone, so the next gizmos/<id> GET answers 404 -- the same round
    trip the real endpoint makes -- unless ``stays`` keeps it alive, for the
    one test that proves the read-back is actually checked.
    """

    def __init__(
        self,
        gizmo: dict[str, Any],
        *,
        conversations: dict[str, Any] | None = None,
        stays: bool = False,
    ) -> None:
        self.gizmo = json.loads(json.dumps(gizmo))
        self.conversations = (
            conversations if conversations is not None else {"items": []}
        )
        self.stays = stays
        self.deleted = False
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
        if method == "GET" and "/conversations" in path:
            return 200, self.conversations
        if method == "GET":
            if self.deleted and not self.stays:
                return 404, {"detail": "This project can't be found."}
            return 200, self.gizmo
        if method == "DELETE":
            self.deleted = True
            return 200, {}
        raise AssertionError(f"unexpected method {method!r}")


class _Session:
    def __init__(self, backend: Any) -> None:
        self.session = backend


def _wire(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(
        delete_project, "open_session", lambda *a, **k: _Session(backend)
    )


def _refuse_before_a_session_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("open_session must not run for a refused invocation")

    monkeypatch.setattr(delete_project, "open_session", _boom)


# ---------------------------------------------------------------------------
# refusal -- the pure function behind every exit-2 policy check
# ---------------------------------------------------------------------------


def test_refusal_is_none_when_the_name_matches_and_starts_with_rp_test() -> None:
    project = {"id": "g-p-x", "name": "rp-test lifecycle"}
    assert delete_project.refusal(project, "rp-test lifecycle", False) is None


def test_refusal_flags_a_name_mismatch_even_with_force() -> None:
    """A typo'd id must never delete the wrong project, --force or not."""
    project = {"id": "g-p-x", "name": "rp-test lifecycle"}
    reason = delete_project.refusal(project, "wrong name", True)
    assert reason is not None
    assert "not 'wrong name'" in reason


def test_refusal_flags_a_non_rp_test_name_without_force() -> None:
    project = {"id": "g-p-x", "name": "My Real Project"}
    reason = delete_project.refusal(project, "My Real Project", False)
    assert reason is not None
    assert "rp-test" in reason
    assert "cannot be undone" in reason


def test_refusal_allows_a_non_rp_test_name_with_force() -> None:
    project = {"id": "g-p-x", "name": "My Real Project"}
    assert delete_project.refusal(project, "My Real Project", True) is None


def test_refusal_allows_a_name_that_merely_starts_with_rp_test() -> None:
    project = {"id": "g-p-x", "name": "rp-test-sandbox"}
    assert delete_project.refusal(project, "rp-test-sandbox", False) is None


# ---------------------------------------------------------------------------
# chat_count
# ---------------------------------------------------------------------------


def test_chat_count_counts_the_items_list() -> None:
    assert delete_project.chat_count({"items": [{"id": "1"}, {"id": "2"}]}) == 2


def test_chat_count_is_zero_for_an_empty_or_malformed_payload() -> None:
    assert delete_project.chat_count({}) == 0
    assert delete_project.chat_count({"items": None}) == 0
    assert delete_project.chat_count(None) == 0
    assert delete_project.chat_count("nope") == 0


# ---------------------------------------------------------------------------
# main -- refusals (exit 2)
# ---------------------------------------------------------------------------


def test_an_id_that_is_not_a_project_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = delete_project.main(["not-a-project", "--expect-name", "x"])
    assert rc == 2


def test_a_name_mismatch_is_refused_before_any_conversations_read(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = delete_project.main(["g-p-throwaway123", "--expect-name", "wrong name"])
    assert rc == 2
    assert [c[0] for c in backend.calls] == ["GET"]
    assert not backend.deleted
    out = capsys.readouterr().out
    assert "refusing" in out
    assert "not 'wrong name'" in out


def test_a_non_rp_test_name_is_refused_without_force(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    gizmo = json.loads(json.dumps(GIZMO_PAYLOAD))
    gizmo["gizmo"]["display"]["name"] = "My Real Project"
    backend = _Backend(gizmo)
    _wire(monkeypatch, backend)
    rc = delete_project.main(["g-p-throwaway123", "--expect-name", "My Real Project"])
    assert rc == 2
    assert not backend.deleted
    assert "rp-test" in capsys.readouterr().out


def test_a_non_rp_test_name_is_allowed_to_proceed_with_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gizmo = json.loads(json.dumps(GIZMO_PAYLOAD))
    gizmo["gizmo"]["display"]["name"] = "My Real Project"
    backend = _Backend(gizmo)
    _wire(monkeypatch, backend)
    rc = delete_project.main(
        ["g-p-throwaway123", "--expect-name", "My Real Project", "--force"]
    )
    assert rc == 0  # allowed past the refusal; still a dry run, nothing deleted
    assert not backend.deleted


def test_help_exits_0_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        delete_project.main(["--help"])
    assert exc.value.code == 0


def test_expect_name_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        delete_project.main(["g-p-throwaway123"])
    assert exc.value.code == 2  # argparse's own exit for a missing required flag


# ---------------------------------------------------------------------------
# main -- reading the project
# ---------------------------------------------------------------------------


def test_a_project_that_cannot_be_read_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class _MissingBackend:
        def call(
            self,
            path: str,
            method: str = "GET",
            payload: Any = None,
            raw: bool = False,
            retries: int = 3,
        ) -> tuple[int, Any]:
            return 404, {"error": "no such gizmo"}

    _wire(monkeypatch, _MissingBackend())
    rc = delete_project.main(["g-p-gone", "--expect-name", "x"])
    assert rc == 1
    assert "no project with id g-p-gone" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main -- the dry run: reads, decides, never writes
# ---------------------------------------------------------------------------


def test_dry_run_counts_chats_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(
        GIZMO_PAYLOAD, conversations={"items": [{"id": "c1"}, {"id": "c2"}]}
    )
    _wire(monkeypatch, backend)
    rc = delete_project.main(["g-p-throwaway123", "--expect-name", "rp-test lifecycle"])
    assert rc == 0
    assert not backend.deleted
    assert all(method == "GET" for method, _p, _pl in backend.calls)
    out = capsys.readouterr().out
    assert "rp-test lifecycle" in out
    assert "chats inside: 2" in out
    assert "dry run" in out
    assert "add --apply" in out


# ---------------------------------------------------------------------------
# main -- --apply
# ---------------------------------------------------------------------------


def test_apply_sends_delete_with_no_payload_then_verifies_404(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = delete_project.main(
        ["g-p-throwaway123", "--expect-name", "rp-test lifecycle", "--apply"]
    )
    assert rc == 0
    deletes = [c for c in backend.calls if c[0] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][1] == "/backend-api/gizmos/g-p-throwaway123"
    assert deletes[0][2] is None  # payload=None for DELETE, per the session API
    out = capsys.readouterr().out
    assert "HTTP 404" in out
    assert "is gone" in out


def test_apply_with_a_read_back_that_does_not_confirm_404_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A DELETE that answers 200 is not proof of anything by itself."""
    backend = _Backend(GIZMO_PAYLOAD, stays=True)
    _wire(monkeypatch, backend)
    rc = delete_project.main(
        ["g-p-throwaway123", "--expect-name", "rp-test lifecycle", "--apply"]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "not verified" in out
    assert "wanted 404" in out
