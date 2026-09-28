"""T0 tests for project_settings.py's --name and --show (PLAN-2026-09-27 A2):
what binnacle's chatgpt-project get-instructions / set-instructions did."""

from __future__ import annotations

from typing import Any

import project_settings as ps
import pytest

GP_A = "g-p-" + "a" * 32
GP_B = "g-p-" + "b" * 32
SIDEBAR = "/backend-api/gizmos/snorlax/sidebar"


def _gizmo(gid: str, name: str, instructions: str) -> dict[str, Any]:
    return {
        "gizmo": {
            "id": gid,
            "display": {"name": name, "emoji": None, "theme": None},
            "instructions": instructions,
            "memory_scope": "global",
            "memory_enabled": True,
        },
        "files": [],
    }


class _Inner:
    def __init__(self) -> None:
        self.store = {
            GP_A: _gizmo(GP_A, "Raspberry Pi 5", "line one\nline two\n"),
            GP_B: _gizmo(GP_B, "Raspberry Pi 5 benchmarks", ""),
        }
        self.calls: list[tuple[str, str, Any]] = []

    def call(self, path: str, method: str = "GET", payload: Any = None, **kw: Any):
        self.calls.append((method, path, payload))
        if path.startswith(SIDEBAR):
            return 200, {"items": list(self.store.values()), "cursor": None}
        gid = path.rsplit("/", 1)[-1]
        if method == "PATCH":
            self.store[gid]["gizmo"]["instructions"] = payload["instructions"]
            return 200, {"resource": {}}
        if gid in self.store:
            return 200, self.store[gid]
        return 404, {"detail": "no"}


class _Session:
    def __init__(self) -> None:
        self.session = _Inner()


@pytest.fixture
def session(monkeypatch) -> _Session:
    made = _Session()
    monkeypatch.setattr(ps, "open_session", lambda *a, **k: made)
    return made


def test_show_prints_the_instructions_exactly_plus_print_s_newline(session, capsys):
    """binnacle's harness strips exactly one trailing newline and compares."""
    assert ps.main(["--name", "raspberry pi 5", "--show"]) == 0
    assert capsys.readouterr().out == "line one\nline two\n\n"
    assert ps.main([GP_B, "--show"]) == 0
    assert capsys.readouterr().out == "\n"
    assert not any(c[0] == "PATCH" for c in session.session.calls)


def test_name_prefers_an_exact_name_then_a_unique_substring(session, capsys) -> None:
    assert ps.main(["--name", "BENCHMARKS", "--show"]) == 0
    assert capsys.readouterr().out == "\n"
    assert ps.main(["--name", "raspberry", "--show"]) == 2
    assert "ambiguous" in capsys.readouterr().out
    assert ps.main(["--name", "nothing", "--show"]) == 2
    assert "no project matches 'nothing'" in capsys.readouterr().out


def test_set_by_name_patches_the_resolved_project_and_reads_it_back(session) -> None:
    code = ps.main(
        ["--name", "Raspberry Pi 5", "--instructions-text", "new", "--apply"]
    )
    assert code == 0
    assert session.session.store[GP_A]["gizmo"]["instructions"] == "new"
    assert ("PATCH", f"/backend-api/projects/{GP_A}") in [
        c[:2] for c in session.session.calls
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["--show"],
        [GP_A, "--name", "x", "--show"],
        ["--name", "x", "--show", "--instructions-text", "y"],
        ["--name", "x"],
    ],
)
def test_bad_combinations_are_refused_before_a_session(monkeypatch, argv) -> None:
    monkeypatch.setattr(ps, "open_session", lambda *a, **k: pytest.fail("no session"))
    assert ps.main(argv) == 2


def test_the_browser_option_reaches_the_session(monkeypatch) -> None:
    seen: list[str] = []

    def opener(browser: str = "chrome") -> _Session:
        seen.append(browser)
        return _Session()

    monkeypatch.setattr(ps, "open_session", opener)
    assert ps.main(["--browser", "chromium", GP_A, "--show"]) == 0
    assert seen == ["chromium"]
