"""Tests for scripts/create_project.py (Stage 2 item M5, create over HTTP).

The request body was captured 2026-09-20 on a throwaway project, deleted in
the same minute (references/endpoint-discovery.md, "Captured 2026-09-20,
project create and delete"): `POST /backend-api/projects` with
`instructions`, `name` and `memory_scope`, answering with the new project's
id nested under `resource.gizmo.id`. With the body known, creation needs no
browser -- this file never touches a real one, and the module under test
never imports Playwright. Everything here runs over a fake session; nothing
here opens a socket (tests/conftest.py blocks it for any test without a live
marker).

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


create_project = _load("create_project")


# ---------------------------------------------------------------------------
# fixtures shared by the main()-level tests
# ---------------------------------------------------------------------------


def _gizmo_payload(
    gizmo_id: str, name: str, instructions: str, memory_scope: str
) -> dict[str, Any]:
    """A gizmos/<id> read-back shaped like a project this body would create."""
    return {
        "gizmo": {
            "id": gizmo_id,
            "short_url": f"{gizmo_id}-{name.lower().replace(' ', '-')}",
            "display": {"name": name},
            "instructions": instructions,
            "memory_enabled": memory_scope == "global",
            "memory_scope": memory_scope,
        },
        "files": [],
    }


class _Backend:
    """Fake ``session.session.call`` for create_project.py.

    A successful POST is remembered, and the following GET gizmos/<id>
    echoes it back -- the same round trip the real endpoint makes.
    ``mismatch`` overrides one field of that read-back, for the one test
    that needs the create to have silently not taken.
    """

    def __init__(
        self,
        *,
        create_status: int = 200,
        new_id: str = "g-p-newproject",
        read_status: int = 200,
        mismatch: dict[str, Any] | None = None,
    ) -> None:
        self.create_status = create_status
        self.new_id = new_id
        self.read_status = read_status
        self.mismatch = mismatch or {}
        self.calls: list[tuple[str, str, Any]] = []
        self._created: dict[str, Any] | None = None

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int = 3,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        if method == "POST":
            if self.create_status != 200:
                return self.create_status, {"error": "boom"}
            self._created = _gizmo_payload(
                self.new_id,
                payload["name"],
                payload["instructions"],
                payload["memory_scope"],
            )
            return self.create_status, {
                "resource": {"gizmo": {"id": self.new_id}},
                "error": None,
                "sharing_targets": [],
            }
        if method == "GET":
            assert self._created is not None, "GET before a successful POST"
            gizmo = json.loads(json.dumps(self._created))
            for field, value in self.mismatch.items():
                gizmo["gizmo"][field] = value
            return self.read_status, gizmo
        raise AssertionError(f"unexpected method {method!r}")


class _Session:
    def __init__(self, backend: Any) -> None:
        self.session = backend


def _wire(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    monkeypatch.setattr(
        create_project, "open_session", lambda *a, **k: _Session(backend)
    )


def _refuse_before_a_session_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal, or a dry run, must fire before the account is ever touched."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("open_session must not run for this invocation")

    monkeypatch.setattr(create_project, "open_session", _boom)


# ---------------------------------------------------------------------------
# create_body / created_id / verify / render_created -- the pure functions
# ---------------------------------------------------------------------------


def test_create_body_carries_instructions_name_and_memory_scope() -> None:
    body = create_project.create_body("msgloom workers", "rules", "project_v2")
    assert body == {
        "instructions": "rules",
        "name": "msgloom workers",
        "memory_scope": "project_v2",
    }


def test_created_id_reads_the_captured_response_shape() -> None:
    resp = {
        "resource": {"gizmo": {"id": "g-p-abc123"}},
        "error": None,
        "sharing_targets": [],
    }
    assert create_project.created_id(resp) == "g-p-abc123"


def test_created_id_is_empty_without_a_g_p_prefixed_id() -> None:
    assert (
        create_project.created_id({"resource": {"gizmo": {"id": "not-a-project"}}})
        == ""
    )
    assert create_project.created_id({"resource": {}}) == ""
    assert create_project.created_id({}) == ""
    assert create_project.created_id(None) == ""
    assert create_project.created_id("nope") == ""


def test_verify_reports_no_mismatches_when_the_read_back_matches() -> None:
    after = {"instructions": "x", "memory_scope": "global"}
    wanted = {"instructions": "x", "memory_scope": "global"}
    assert create_project.verify(after, wanted) == []


def test_verify_reports_a_field_that_disagrees() -> None:
    after = {"instructions": "x", "memory_scope": "unset"}
    wanted = {"instructions": "x", "memory_scope": "project_v2"}
    mismatches = create_project.verify(after, wanted)
    assert len(mismatches) == 1
    assert "memory_scope" in mismatches[0]
    assert "wanted 'project_v2'" in mismatches[0]
    assert "read back 'unset'" in mismatches[0]


def test_render_created_shows_id_url_memory_scope_and_instructions_length() -> None:
    project = {
        "id": "g-p-x",
        "url": "https://chatgpt.com/g/g-p-x/project",
        "memory_scope": "global",
        "instructions": "12345",
    }
    text = create_project.render_created(project)
    assert "g-p-x" in text
    assert "https://chatgpt.com/g/g-p-x/project" in text
    assert "global" in text
    assert "5 chars" in text


def test_render_created_shows_a_dash_when_memory_scope_is_unknown() -> None:
    project = {"id": "g-p-x", "url": "u", "memory_scope": None, "instructions": ""}
    assert "memory scope: -" in create_project.render_created(project)


# ---------------------------------------------------------------------------
# main -- refusals (exit 2), before a session ever opens
# ---------------------------------------------------------------------------


def test_an_empty_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert create_project.main([""]) == 2


def test_a_whitespace_only_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert create_project.main(["   "]) == 2


def test_instructions_and_instructions_text_together_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = create_project.main(
        ["name", "--instructions", "x", "--instructions-text", "y"]
    )
    assert rc == 2


def test_a_missing_instructions_file_is_refused_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    missing = tmp_path / "nope.txt"
    rc = create_project.main(["name", "--instructions", str(missing)])
    assert rc == 2


def test_help_exits_0_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--help must never authenticate; argparse's own exit must win the race."""
    _refuse_before_a_session_opens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        create_project.main(["--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# main -- --dry-run: prints the body, sends nothing
# ---------------------------------------------------------------------------


def test_dry_run_sends_nothing(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = create_project.main(["msgloom workers", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run: nothing created" in out
    assert '"name": "msgloom workers"' in out
    assert '"instructions": ""' in out
    assert '"memory_scope": "unset"' in out


def test_dry_run_with_instructions_text_and_memory_shows_the_full_body(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = create_project.main(
        [
            "msgloom workers",
            "--instructions-text",
            "rules",
            "--memory",
            "project-only",
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"instructions": "rules"' in out
    assert '"memory_scope": "project_v2"' in out


def test_instructions_from_a_file_are_read_into_the_dry_run_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    path = tmp_path / "instructions.txt"
    path.write_text("Instructions from a file.", encoding="utf-8")
    rc = create_project.main(
        ["msgloom workers", "--instructions", str(path), "--dry-run"]
    )
    assert rc == 0
    assert '"instructions": "Instructions from a file."' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main -- the body for each memory choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(None, "unset"), ("project-only", "project_v2"), ("default", "global")],
)
def test_the_post_body_carries_the_right_memory_scope_for_each_choice(
    monkeypatch: pytest.MonkeyPatch, flag: str | None, expected: str
) -> None:
    backend = _Backend()
    _wire(monkeypatch, backend)
    args = ["msgloom workers"]
    if flag:
        args += ["--memory", flag]
    rc = create_project.main(args)
    assert rc == 0
    posts = [c for c in backend.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == {
        "instructions": "",
        "name": "msgloom workers",
        "memory_scope": expected,
    }


def test_instructions_from_a_file_are_sent_verbatim_on_a_real_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "instructions.txt"
    path.write_text("Instructions from a file.", encoding="utf-8")
    backend = _Backend()
    _wire(monkeypatch, backend)
    rc = create_project.main(["msgloom workers", "--instructions", str(path)])
    assert rc == 0
    posts = [c for c in backend.calls if c[0] == "POST"]
    assert posts[0][2]["instructions"] == "Instructions from a file."


# ---------------------------------------------------------------------------
# main -- a real create round-trips and reports what it made
# ---------------------------------------------------------------------------


def test_a_real_create_prints_id_url_memory_scope_and_instructions_length(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(new_id="g-p-newproject")
    _wire(monkeypatch, backend)
    rc = create_project.main(["msgloom workers", "--instructions-text", "rules"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "g-p-newproject" in out
    assert "https://chatgpt.com/g/g-p-newproject-msgloom-workers/project" in out
    assert "memory scope: unset" in out
    assert "instructions: 5 chars" in out
    posts = [c for c in backend.calls if c[0] == "POST"]
    gets = [c for c in backend.calls if c[0] == "GET"]
    assert len(posts) == 1
    assert len(gets) == 1
    assert gets[0][1] == "/backend-api/gizmos/g-p-newproject"


# ---------------------------------------------------------------------------
# main -- failure paths (exit 1)
# ---------------------------------------------------------------------------


def test_a_non_200_create_fails_the_exit_code_without_a_read_back(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(create_status=500)
    _wire(monkeypatch, backend)
    rc = create_project.main(["msgloom workers"])
    assert rc == 1
    assert [c[0] for c in backend.calls] == ["POST"]
    assert "create failed: HTTP 500" in capsys.readouterr().out


def test_a_create_response_without_a_g_p_id_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class _NoIdBackend:
        def call(
            self,
            path: str,
            method: str = "GET",
            payload: Any = None,
            raw: bool = False,
            retries: int = 3,
        ) -> tuple[int, Any]:
            if method == "POST":
                return 200, {"resource": {"gizmo": {}}, "error": None}
            raise AssertionError("must not read back without an id")

    _wire(monkeypatch, _NoIdBackend())
    rc = create_project.main(["msgloom workers"])
    assert rc == 1
    assert "create failed: HTTP 200" in capsys.readouterr().out


def test_a_read_back_get_that_fails_after_a_successful_create_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    backend = _Backend(read_status=500)
    _wire(monkeypatch, backend)
    rc = create_project.main(["msgloom workers"])
    assert rc == 1
    assert "could not read it back: HTTP 500" in capsys.readouterr().out


def test_a_read_back_mismatch_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A create that answers 200 is not proof of anything by itself."""
    backend = _Backend(mismatch={"instructions": "something else"})
    _wire(monkeypatch, backend)
    rc = create_project.main(["msgloom workers", "--instructions-text", "rules"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "read-back disagrees" in out
    assert "instructions" in out
    assert "'rules'" in out
    assert "'something else'" in out
