"""Tests for scripts/pin_chat.py (Stage 2 item M2, pin/unpin).

Pin and unpin are the same conversation PATCH family the client already
uses for rename, archive and delete: ``{"is_starred": true}`` /
``{"is_starred": false}``, both captured 2026-09-20 and each verified with a
read afterward (references/endpoint-discovery.md, "Captured 2026-09-20,
later"). There is no ``starred()`` method on ``ChatGPTSession``, so
``--apply`` calls ``session.session.call`` directly; the initial read (used
for the dry run, and as the record ``--apply`` prints before it changes
anything) goes through ``session.get_conversation``, the same call
``read_chat.py`` already makes. Everything here runs over a fake session;
nothing here opens a socket (tests/conftest.py blocks it for any test
without a live marker).

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

import chatgpt_client as cc  # noqa: E402


def _load(name: str):
    """Import a command module fresh by path (test_commands.py's own helper)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pin_chat = _load("pin_chat")

REAL_ID = "6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
CONV = {
    "id": REAL_ID,
    "title": "rp-test send 1",
    "is_starred": False,
    "is_archived": False,
}


# ---------------------------------------------------------------------------
# fixtures shared by the main()-level tests
# ---------------------------------------------------------------------------


class _Backend:
    """Fake ``session.session.call``.

    GET always returns the current conversation state; a successful PATCH
    updates ``is_starred`` the way the capture describes, so a read-back
    after it sees the change -- the same round trip the real endpoint makes.
    Every call is recorded (method, path, payload), so a dry run can be
    proven to have made only the one GET behind ``get_conversation``, and an
    --apply run proven to have sent exactly one PATCH with a given body.
    """

    def __init__(
        self,
        conv: dict[str, Any],
        *,
        patch_status: int = 200,
        apply_patch: bool = True,
    ) -> None:
        self.conv = json.loads(json.dumps(conv))  # a private, mutable copy
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
            return 200, self.conv
        if method == "PATCH":
            if self.patch_status == 200 and self.apply_patch:
                self.conv["is_starred"] = payload["is_starred"]
            return self.patch_status, self.conv
        raise AssertionError(f"unexpected method {method!r}")


class _Session:
    """Mirrors ``ChatGPTSession``'s shape: ``.session`` is the low-level
    transport ``--apply`` uses directly for the PATCH, ``.get_conversation``
    is the high-level read the dry run (and the pre-PATCH record) uses.
    """

    def __init__(self, backend: _Backend) -> None:
        self.session = backend

    def get_conversation(self, chat: str) -> dict[str, Any]:
        status, body = self.session.call(f"/backend-api/conversation/{chat}")
        if status != 200:
            raise cc.TransportError(f"GET conversation {chat} -> HTTP {status}")
        return body


def _wire(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
    """Wire a fake backend in as the session ``main`` will open.

    ``backend`` only needs to be shaped like ``_Backend``: something with a
    ``.call(path, method=, payload=, raw=, retries=)``.
    """
    monkeypatch.setattr(pin_chat, "open_session", lambda *a, **k: _Session(backend))


def _refuse_before_a_session_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal must fire before the account is ever touched."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("open_session must not run for a refused invocation")

    monkeypatch.setattr(pin_chat, "open_session", _boom)


# ---------------------------------------------------------------------------
# patch_body / verify -- the pure functions behind every send
# ---------------------------------------------------------------------------


def test_patch_body_pin_sends_is_starred_true() -> None:
    assert pin_chat.patch_body(True) == {"is_starred": True}


def test_patch_body_unpin_sends_is_starred_false() -> None:
    """The captured unpin body: {"is_starred": false}."""
    assert pin_chat.patch_body(False) == {"is_starred": False}


def test_verify_reports_no_mismatch_when_the_read_back_matches() -> None:
    assert pin_chat.verify({"is_starred": True}, True) == []


def test_verify_reports_a_mismatch_when_the_read_back_disagrees() -> None:
    """A PATCH that answers 200 is not proof of anything by itself."""
    mismatches = pin_chat.verify({"is_starred": False}, True)
    assert len(mismatches) == 1
    assert "wanted True" in mismatches[0]
    assert "read back False" in mismatches[0]


def test_verify_treats_a_missing_is_starred_as_a_mismatch() -> None:
    """A failed re-read degrades to {}; that must not read as a false match."""
    assert pin_chat.verify({}, True) != []


# ---------------------------------------------------------------------------
# main -- refusals (exit 2), all before a session ever opens
# ---------------------------------------------------------------------------


def test_a_bad_id_is_refused_before_a_session_opens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    rc = pin_chat.main(["not-a-real-id"])
    assert rc == 2
    assert "not-a-real-id" in capsys.readouterr().out


def test_an_empty_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse_before_a_session_opens(monkeypatch)
    assert pin_chat.main([""]) == 2


def test_a_provisional_web_id_is_refused_not_sent_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new chat's optimistic WEB: id is not yet known to the backend."""
    _refuse_before_a_session_opens(monkeypatch)
    assert pin_chat.main([f"WEB:{REAL_ID}"]) == 2


def test_help_exits_0_without_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--help must never authenticate; argparse's own exit must win the race."""
    _refuse_before_a_session_opens(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        pin_chat.main(["--help"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# main -- a /c/ URL resolves the same as a bare id
# ---------------------------------------------------------------------------


def test_a_c_url_is_accepted_the_same_as_a_bare_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(CONV)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([f"https://chatgpt.com/c/{REAL_ID}"])
    assert rc == 0
    gets = [c for c in backend.calls if c[0] == "GET"]
    assert gets[0][1] == f"/backend-api/conversation/{REAL_ID}"


# ---------------------------------------------------------------------------
# main -- the dry run: reads, prints, never writes
# ---------------------------------------------------------------------------


def test_dry_run_never_patches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default must never touch the account, however it is invoked."""
    backend = _Backend(CONV)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID])
    assert rc == 0
    assert len(backend.calls) == 1
    assert backend.calls[0][0] == "GET"
    out = capsys.readouterr().out
    assert "rp-test send 1" in out
    assert "current is_starred: False" in out
    assert '"is_starred": true' in out  # the body it would send
    assert "dry run: would pin" in out


def test_dry_run_with_unpin_shows_the_unpin_body_and_verb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    starred = json.loads(json.dumps(CONV))
    starred["is_starred"] = True
    backend = _Backend(starred)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID, "--unpin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "current is_starred: True" in out
    assert '"is_starred": false' in out
    assert "dry run: would unpin" in out


# ---------------------------------------------------------------------------
# main -- --apply, verified
# ---------------------------------------------------------------------------


def test_apply_pins_and_verifies_the_read_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(CONV)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID, "--apply"])
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1] == f"/backend-api/conversation/{REAL_ID}"
    assert patches[0][2] == {"is_starred": True}
    out = capsys.readouterr().out
    assert "pinned" in out
    assert "is_starred now True" in out


def test_apply_unpins_and_sends_the_captured_unpin_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    starred = json.loads(json.dumps(CONV))
    starred["is_starred"] = True
    backend = _Backend(starred)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID, "--unpin", "--apply"])
    assert rc == 0
    patches = [c for c in backend.calls if c[0] == "PATCH"]
    assert patches[0][2] == {"is_starred": False}
    assert "unpinned" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main -- --apply failure paths (exit 1)
# ---------------------------------------------------------------------------


def test_a_non_200_patch_fails_the_exit_code_without_a_second_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = _Backend(CONV, patch_status=500)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID, "--apply"])
    assert rc == 1
    methods = [c[0] for c in backend.calls]
    assert methods == ["GET", "PATCH"]  # never spends a read-back on a failed PATCH
    assert "PATCH failed: HTTP 500" in capsys.readouterr().out


def test_a_read_back_that_disagrees_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PATCH that answers 200 is not proof of anything by itself."""
    backend = _Backend(CONV, apply_patch=False)
    _wire(monkeypatch, backend)
    rc = pin_chat.main([REAL_ID, "--apply"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "read-back disagrees" in out
    assert "wanted True" in out


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
                return 200, {}
            self.gets += 1
            if self.gets == 1:
                return 200, CONV
            return 500, {"error": "boom"}

    _wire(monkeypatch, _FlakyBackend())
    rc = pin_chat.main([REAL_ID, "--apply"])
    assert rc == 1
    assert "read-back disagrees" in capsys.readouterr().out


def test_a_failed_initial_read_is_not_silently_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial read has no exit code of its own (TESTING.md, "kinds of
    tests": a failure must never look like success); read_chat.py sets the
    precedent of letting a TransportError here propagate rather than
    inventing one."""

    class _AlwaysFails:
        def call(
            self,
            path: str,
            method: str = "GET",
            payload: Any = None,
            raw: bool = False,
            retries: int = 3,
        ) -> tuple[int, Any]:
            return 404, {"error": "no such conversation"}

    _wire(monkeypatch, _AlwaysFails())
    with pytest.raises(cc.TransportError):
        pin_chat.main([REAL_ID])
