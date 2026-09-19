"""Tests for scripts/project_settings.py (Stage 2 items M3 and M4).

Project settings' PATCH always sends the whole record -- name, instructions,
emoji, theme and, when memory changed, memory_scope
(references/endpoint-discovery.md, "Captured 2026-09-20") -- so most of what
is worth proving here is the round trip: the body a dry run *would* send
matches the capture exactly, a dry run never sends it, and --apply's
read-back is actually checked rather than assumed. Everything runs over a
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


project_settings = _load("project_settings")


# ---------------------------------------------------------------------------
# fixtures shared by the main()-level tests
# ---------------------------------------------------------------------------

GIZMO_PAYLOAD = {
    "gizmo": {
        "id": "g-p-sandbox123",
        "short_url": "g-p-sandbox123-rp-test-sandbox",
        "display": {"name": "rp-test-sandbox", "emoji": None, "theme": None},
        "instructions": "Old instructions.",
        "memory_enabled": True,
        "memory_scope": "global",
        "context_stuffing_budget": 110000,
        "model": None,
        "default_model": None,
    },
    "files": [],
}


class _Backend:
    """Fake ``session.session.call``.

    GET always returns the current gizmo state; a successful PATCH updates
    that state the way the capture describes, so a read-back after it sees
    the change -- the same round trip the real endpoint makes. Every call
    is recorded (method, path, payload), so a dry run can be proven to have
    made none but GETs, and an --apply run proven to have sent exactly one
    PATCH with a given body.
    """

    def __init__(
        self,
        gizmo: dict[str, Any],
        *,
        patch_status: int = 200,
        apply_patch: bool = True,
    ) -> None:
        self.gizmo = json.loads(json.dumps(gizmo))  # a private, mutable copy
        self.patch_status = patch_status
        self.apply_patch = apply_patch
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
            return 200, self.gizmo
        if method == "PATCH":
            if self.patch_status == 200 and self.apply_patch:
                gizmo = self.gizmo["gizmo"]
                display = gizmo.setdefault("display", {})
                display["name"] = payload.get("name")
                display["emoji"] = payload.get("emoji")
                display["theme"] = payload.get("theme")
                gizmo["instructions"] = payload.get("instructions")
                if "memory_scope" in payload:
                    scope = payload["memory_scope"]
                    gizmo["memory_scope"] = scope
                    gizmo["memory_enabled"] = scope == "global"
            return self.patch_status, {
                "resource": {"gizmo": self.gizmo["gizmo"]},
                "error": None,
                "sharing_targets": [],
            }
        raise AssertionError(f"unexpected method {method!r}")


class _Session:
    def __init__(self, backend: _Backend) -> None:
        self.session = backend


def _wire(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    """Wire a fake backend in as the session ``main`` will open.

    ``backend`` only needs to be shaped like ``_Backend``: something with a
    ``.call(path, method=, payload=, raw=, retries=)``. The read-back
    failure tests below pass their own smaller fakes rather than ``_Backend``
    itself.
    """
    monkeypatch.setattr(
        project_settings, "open_session", lambda *a, **k: _Session(backend)
    )


def _refuse_before_a_session_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal must fire before the account is ever touched."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("open_session must not run for a refused invocation")

    monkeypatch.setattr(project_settings, "open_session", _boom)


# ---------------------------------------------------------------------------
# patch_body -- the pure function behind every send
# ---------------------------------------------------------------------------


def test_patch_body_keeps_existing_instructions_when_none_are_given() -> None:
    body = project_settings.patch_body(GIZMO_PAYLOAD)
    assert body["instructions"] == "Old instructions."
    assert "memory_scope" not in body


def test_patch_body_uses_the_given_instructions_instead_of_the_existing_ones() -> None:
    body = project_settings.patch_body(GIZMO_PAYLOAD, instructions="New.")
    assert body["instructions"] == "New."


def test_patch_body_includes_memory_scope_only_when_memory_was_given() -> None:
    """The page never sends memory_scope on an instructions-only save."""
    assert "memory_scope" not in project_settings.patch_body(GIZMO_PAYLOAD)
    with_memory = project_settings.patch_body(GIZMO_PAYLOAD, memory="global")
    assert with_memory["memory_scope"] == "global"


def test_emoji_and_theme_pass_through_unchanged() -> None:
    """This command has no --emoji or --theme flag; it must never clear them."""
    gizmo_payload = {
        "gizmo": {
            "id": "g-p-x",
            "display": {"name": "x", "emoji": "smile", "theme": "dark"},
            "instructions": "Keep me.",
        },
        "files": [],
    }
    body = project_settings.patch_body(gizmo_payload, instructions="New.")
    assert body["name"] == "x"
    assert body["emoji"] == "smile"
    assert body["theme"] == "dark"


def test_patch_body_copes_with_a_gizmo_missing_its_display() -> None:
    body = project_settings.patch_body({"gizmo": {"instructions": "x"}})
    assert body["name"] is None
    assert body["emoji"] is None
    assert body["theme"] is None


# ---------------------------------------------------------------------------
# verify -- the pure function behind --apply's exit code
# ---------------------------------------------------------------------------


def test_verify_reports_no_mismatches_when_the_read_back_matches() -> None:
    before = {"instructions": "Old.", "memory_scope": "global", "memory_enabled": True}
    after = {
        "instructions": "New.",
        "memory_scope": "project_v2",
        "memory_enabled": False,
    }
    wanted = {"instructions": "New.", "memory_scope": "project_v2"}
    assert project_settings.verify(before, after, wanted) == []


def test_verify_reports_a_field_that_did_not_take() -> None:
    """A PATCH that answers 200 but silently no-ops must still be caught."""
    before = {"instructions": "Old."}
    after = {"instructions": "Old."}
    wanted = {"instructions": "New."}
    mismatches = project_settings.verify(before, after, wanted)
    assert len(mismatches) == 1
    assert "wanted 'New.'" in mismatches[0]
    assert "read back 'Old.'" in mismatches[0]
    assert "was 'Old.'" in mismatches[0]


def test_verify_catches_memory_enabled_disagreeing_with_memory_scope() -> None:
    """memory_scope can match while the effect it is supposed to have did not."""
    before = {"memory_scope": "global", "memory_enabled": True}
    after = {"memory_scope": "project_v2", "memory_enabled": True}  # should be False
    wanted = {"memory_scope": "project_v2"}
    mismatches = project_settings.verify(before, after, wanted)
    assert len(mismatches) == 1
    assert "memory_enabled" in mismatches[0]


def test_verify_only_checks_fields_that_were_actually_wanted() -> None:
    """name/emoji/theme are echoed back unchanged; verify must not opine on them."""
    before = {"instructions": "Old.", "name": "before-name"}
    after = {"instructions": "New.", "name": "a-completely-different-name"}
    wanted = {"instructions": "New."}
    assert project_settings.verify(before, after, wanted) == []


# ---------------------------------------------------------------------------
# render helpers
# ---------------------------------------------------------------------------


def test_render_current_shows_name_instructions_length_and_memory_scope() -> None:
    project = {
        "id": "g-p-x",
        "name": "x",
        "instructions": "1234567890",
        "memory_scope": "global",
    }
    text = project_settings.render_current(project)
    assert "g-p-x" in text
    assert "10 chars" in text
    assert "global" in text


def test_render_current_shows_a_dash_when_memory_scope_is_unknown() -> None:
    project = {"id": "g-p-x", "name": "x", "instructions": "", "memory_scope": None}
    assert "memory scope -" in project_settings.render_current(project)


def test_render_change_shows_one_line_per_changed_field() -> None:
    before = {"instructions": "Old.", "memory_scope": "global"}
    after = {"instructions": "New.", "memory_scope": "project_v2"}
    wanted = {"instructions": "New.", "memory_scope": "project_v2"}
    lines = project_settings.render_change(before, after, wanted).splitlines()
    assert len(lines) == 2
    assert "'Old.' -> 'New.'" in lines[0]


# ---------------------------------------------------------------------------
# main -- refusals (exit 2), all before a session ever opens
# ---------------------------------------------------------------------------


def test_an_id_that_is_not_a_project_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert project_settings.main(["not-a-project", "--instructions-text", "x"]) == 2


def test_nothing_to_change_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare run must never be mistaken for a no-op success."""
    _refuse_before_a_session_opens(monkeypatch)
    assert project_settings.main(["g-p-sandbox123"]) == 2
    assert project_settings.main(["g-p-sandbox123", "--apply"]) == 2


def test_instructions_and_instructions_text_together_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions", "x", "--instructions-text", "y"]
    )
    assert rc == 2


def test_clear_instructions_and_instructions_text_together_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = project_settings.main(
        ["g-p-sandbox123", "--clear-instructions", "--instructions-text", "y"]
    )
    assert rc == 2


def test_a_missing_instructions_file_is_refused_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    missing = tmp_path / "nope.txt"
    rc = project_settings.main(["g-p-sandbox123", "--instructions", str(missing)])
    assert rc == 2


def test_help_exits_0_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--help must never authenticate; argparse's own exit must win the race."""
    _refuse_before_a_session_opens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        project_settings.main(["--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# main -- reading the project
# ---------------------------------------------------------------------------


def test_a_project_that_cannot_be_read_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    rc = project_settings.main(["g-p-gone", "--instructions-text", "x"])
    assert rc == 1
    assert "no project with id g-p-gone" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main -- the dry run: reads, decides, never writes
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_patch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default must never touch the account, however it is invoked."""
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(["g-p-sandbox123", "--instructions-text", "New."])
    assert rc == 0
    assert all(method == "GET" for method, _path, _payload in backend.calls)
    out = capsys.readouterr().out
    assert "dry run: nothing changed; add --apply" in out
    assert '"instructions": "New."' in out  # the body it would send


def test_dry_run_with_a_memory_change_reports_the_captured_body_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    assert project_settings.main(["g-p-sandbox123", "--memory", "project-only"]) == 0
    assert all(method == "GET" for method, _path, _payload in backend.calls)


# ---------------------------------------------------------------------------
# main -- --apply, the three captured body shapes
# ---------------------------------------------------------------------------


def test_apply_sets_instructions_and_verifies_the_read_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions-text", "New instructions.", "--apply"]
    )
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1] == "/backend-api/projects/g-p-sandbox123"
    assert patches[0][2] == {
        "name": "rp-test-sandbox",
        "instructions": "New instructions.",
        "emoji": None,
        "theme": None,
    }
    assert "changed:" in capsys.readouterr().out


def test_apply_switches_to_project_only_memory_and_verifies_memory_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        ["g-p-sandbox123", "--memory", "project-only", "--apply"]
    )
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2] == {
        "name": "rp-test-sandbox",
        "instructions": "Old instructions.",
        "emoji": None,
        "theme": None,
        "memory_scope": "project_v2",
    }
    assert backend.gizmo["gizmo"]["memory_enabled"] is False


def test_apply_switches_to_default_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    gizmo = json.loads(json.dumps(GIZMO_PAYLOAD))
    gizmo["gizmo"]["memory_scope"] = "project_v2"
    gizmo["gizmo"]["memory_enabled"] = False
    backend = _Backend(gizmo)
    _wire(monkeypatch, backend)
    rc = project_settings.main(["g-p-sandbox123", "--memory", "default", "--apply"])
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2]["memory_scope"] == "global"
    assert backend.gizmo["gizmo"]["memory_enabled"] is True


def test_apply_can_change_instructions_and_memory_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        [
            "g-p-sandbox123",
            "--instructions-text",
            "Both at once.",
            "--memory",
            "project-only",
            "--apply",
        ]
    )
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2]["instructions"] == "Both at once."
    assert patches[0][2]["memory_scope"] == "project_v2"


def test_clear_instructions_sends_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(["g-p-sandbox123", "--clear-instructions", "--apply"])
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2]["instructions"] == ""


def test_instructions_from_a_file_are_read_and_sent_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "instructions.txt"
    path.write_text("Instructions from a file.", encoding="utf-8")
    backend = _Backend(GIZMO_PAYLOAD)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions", str(path), "--apply"]
    )
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2]["instructions"] == "Instructions from a file."


# ---------------------------------------------------------------------------
# main -- --apply failure paths (exit 1)
# ---------------------------------------------------------------------------


def test_a_read_back_that_disagrees_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PATCH that answers 200 is not proof of anything by itself."""
    backend = _Backend(GIZMO_PAYLOAD, apply_patch=False)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions-text", "New instructions.", "--apply"]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "read-back disagrees" in out
    assert "New instructions." in out
    assert "Old instructions." in out


def test_a_non_200_patch_fails_the_exit_code_without_a_second_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(GIZMO_PAYLOAD, patch_status=500)
    _wire(monkeypatch, backend)
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions-text", "New instructions.", "--apply"]
    )
    assert rc == 1
    methods = [c[0] for c in backend.calls]
    assert methods == ["GET", "PATCH"]  # never spends a read-back on a failed PATCH
    assert "PATCH failed: HTTP 500" in capsys.readouterr().out


def test_a_read_back_get_that_fails_after_a_successful_patch_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful PATCH proves nothing if the confirming read cannot happen."""

    class _FlakyBackend:
        def __init__(self) -> None:
            self.gets = 0

        def call(
            self,
            path: str,
            method: str = "GET",
            payload: Any = None,
            raw: bool = False,
            retries: int = 3,
        ) -> tuple[int, Any]:
            if method == "PATCH":
                return 200, {"resource": {"gizmo": {}}, "error": None}
            self.gets += 1
            if self.gets == 1:
                return 200, GIZMO_PAYLOAD
            return 500, {"error": "boom"}

    _wire(monkeypatch, _FlakyBackend())
    rc = project_settings.main(
        ["g-p-sandbox123", "--instructions-text", "New instructions.", "--apply"]
    )
    assert rc == 1
    assert "read-back disagrees" in capsys.readouterr().out
