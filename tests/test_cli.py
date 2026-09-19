"""CLI tests (TESTING.md, kind "CLI"): every command has argparse and
``--help`` exits 0 without ever reaching a session -- catching the bug where
``probe_send_gates.py`` had no argparse at all and ``--help`` ran the live
probe instead (Stage 1 item 3, see ``tests/test_stage1.py``).

Commands are discovered, not listed by hand: every ``scripts/*.py`` file
except the bundled library modules (``_common.py``, ``round_state.py``,
``chatgpt_client.py``, ``chatgpt_session.py``, ``chatgpt_cookies.py``) is
imported, and a module whose ``main`` takes exactly one parameter named
``argv`` is added to the parametrized ``--help`` check. A module with a
different ``main`` signature -- ``round_status.py`` (``workdir: Path``) and
``review_topic.py`` (``folders: list[str]``, no argparse) -- is a special
case and is covered separately or not at all, never by assuming it behaves
like the rest. This means a new command dropped into ``scripts/`` following
the ``main(argv)`` convention is picked up here automatically.
"""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

LIBRARY_MODULES = {
    "_common.py",
    "round_state.py",
    "chatgpt_client.py",
    "chatgpt_session.py",
    "chatgpt_cookies.py",
}


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _argv_modules() -> list[tuple[str, Any]]:
    """(name, module) for every scripts/*.py command whose main(argv) takes
    exactly one parameter named "argv"."""
    found = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name in LIBRARY_MODULES:
            continue
        module = _import(path.stem)
        main = getattr(module, "main", None)
        if main is None:
            continue
        if list(inspect.signature(main).parameters) == ["argv"]:
            found.append((path.stem, module))
    return found


ARGV_MODULES = _argv_modules()


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("--help must exit before touching a session")


def _forbid_session(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Make every session-opening entry point this module might have fail
    loudly if it is ever reached, so a passing test proves --help never
    reaches it (argparse's own SystemExit must win the race)."""
    if hasattr(module, "open_session"):
        monkeypatch.setattr(module, "open_session", _boom)
    if hasattr(module, "load_client"):
        monkeypatch.setattr(module, "load_client", _boom)
    if hasattr(module, "cc"):
        monkeypatch.setattr(module.cc, "ChatGPTSession", _boom, raising=False)


# ---------------------------------------------------------------------------
# every main(argv) command: --help exits 0 and prints usage, no session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "module"), ARGV_MODULES, ids=[name for name, _ in ARGV_MODULES]
)
def test_help_exits_0_and_prints_usage_without_a_session(
    name: str, module: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _forbid_session(monkeypatch, module)
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0, f"{name} --help must exit 0"
    out = capsys.readouterr().out
    assert "usage:" in out, f"{name} --help must print a usage line"


def test_discovery_found_every_expected_argv_command() -> None:
    """A stale exclusion list would silently stop covering a command."""
    names = {name for name, _ in ARGV_MODULES}
    assert names >= {
        "clean_chats",
        "create_project",
        "discover_endpoints",
        "list_chats",
        "list_projects",
        "measure_window",
        "model_settings",
        "probe_account",
        "probe_cookies",
        "probe_send_gates",
        "profile_context",
        "read_chat",
    }
    # The two special cases must never slip into the generic loop.
    assert "round_status" not in names
    assert "review_topic" not in names


# ---------------------------------------------------------------------------
# chatgpt_cookies.py -- main() takes no argv parameter; it reads sys.argv
# ---------------------------------------------------------------------------


def test_chatgpt_cookies_help_exits_0_via_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    module = _import("chatgpt_cookies")
    monkeypatch.setattr(module, "export", _boom)
    monkeypatch.setattr(sys, "argv", ["chatgpt_cookies.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# round_status.py -- parse_args(argv) -> Path (main keeps taking a Path)
# ---------------------------------------------------------------------------


def test_round_status_parse_args_help_exits_0_and_prints_usage(capsys: Any) -> None:
    round_status = _import("round_status")
    with pytest.raises(SystemExit) as exc:
        round_status.parse_args(["--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_round_status_parse_args_resolves_the_workdir_argument(tmp_path: Path) -> None:
    round_status = _import("round_status")
    target = tmp_path / "topic"
    assert round_status.parse_args([str(target)]) == target.resolve()


def test_round_status_parse_args_requires_exactly_one_workdir() -> None:
    round_status = _import("round_status")
    with pytest.raises(SystemExit) as exc:
        round_status.parse_args([])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# subprocess: python3 scripts/probe_send_gates.py --help from any checkout
# ---------------------------------------------------------------------------


def test_probe_send_gates_help_via_subprocess_with_reexec_flag_set() -> None:
    """CHATGPT_WEB_OPS_REEXEC=1 makes ensure_venv a no-op, so this runs the
    same everywhere a plain "python3 scripts/probe_send_gates.py --help"
    would need the skill's own venv."""
    env = dict(os.environ, CHATGPT_WEB_OPS_REEXEC="1")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe_send_gates.py"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout


# ---------------------------------------------------------------------------
# _common.ensure_venv -- the flag the subprocess test above relies on
# ---------------------------------------------------------------------------


def test_ensure_venv_returns_without_reexec_when_the_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = _import("_common")
    monkeypatch.setenv(common.REEXEC_FLAG, "1")
    monkeypatch.setattr(os, "execve", _boom)
    common.ensure_venv()  # must return quietly; a call to os.execve would raise
