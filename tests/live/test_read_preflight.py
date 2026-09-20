"""Tier T1: preflight's account and run groups against the real account,
and the whole command end to end (marker live_read, CHATGPT_LIVE=read).

preflight.py had 31 functions and every one an offline test, and it was in
no live tier at all. So its account and run checks had never once been run
against the real account by the suite, and its --browser check, tested
against a fake kinder than the real BrowserSender, had never worked
(references/failure-atlas.md, 2026-09-20). The pure verdict functions are
proved offline; what this file proves is the composition: the real
``Session.call`` feeding the real verdict functions over the guarded
session, and ``main()`` producing the documented exit code and document.

Shape only, never the user's data (TESTING.md section 2): a check's name,
its state, and the fields its detail must carry. The one value asserted
outright is the sandbox project's memory scope, which the tests own.

The --browser group opens a window and is T3 territory; it is not
exercised here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import preflight  # noqa: E402

pytestmark = pytest.mark.live_read

STATES = {"ok", "warn", "block"}

# The ten checks a bare `preflight.py` makes, in GROUP_ORDER.
DEFAULT_NAMES = [
    "available memory",
    "load average",
    "in-flight browsers",
    "browser tooling",
    "keyring bus",
    "wireless link",
    "auth and read",
    "send gates",
    "plan and credits",
    "model and effort",
    "custom instructions and memory",
]


def _as_session(live_session: Any) -> SimpleNamespace:
    """preflight's fetch functions take a ChatGPTSession and call
    ``.session.call``; the guarded ``live_session`` is that inner object."""
    return SimpleNamespace(session=live_session)


def test_account_group_reads_the_real_account(live_session: Any) -> None:
    checks = preflight.account_checks(_as_session(live_session))
    assert [c["name"] for c in checks] == [
        "auth and read",
        "send gates",
        "plan and credits",
    ]
    assert all(c["state"] in STATES for c in checks)
    auth = checks[0]
    assert auth["state"] == "ok", auth["detail"]
    assert "/backend-api/me 200" in auth["detail"]


def test_run_group_resolves_what_a_send_inherits(live_session: Any) -> None:
    checks = preflight.run_checks(_as_session(live_session), "", None)
    assert [c["name"] for c in checks] == [
        "model and effort",
        "custom instructions and memory",
    ]
    model = checks[0]
    assert model["state"] == "ok"
    assert "model=" in model["detail"] and "effort=" in model["detail"]
    # The account's own record must land on a preset the slider offers; if
    # this ever fails, the presets changed under us (SKILL.md, "Reasoning
    # effort") and model_settings.py is the place to look.
    assert "matches no preset" not in model["detail"], model["detail"]
    memory = checks[1]
    assert "memory" in memory["detail"] and "tokens" in memory["detail"]


def test_run_group_finds_the_sandbox_with_project_only_memory(
    live_session: Any, sandbox_id: str
) -> None:
    checks = preflight.run_checks(_as_session(live_session), sandbox_id, None)
    project = next(c for c in checks if c["name"] == "project")
    assert project["state"] == "ok", project["detail"]
    assert "project_v2" in project["detail"]


def test_the_whole_command_exits_as_documented(
    live_session: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """``main()`` over the guarded session: the host and link groups read the
    real host, the account and run groups read the real account, and the
    document written by --json agrees with the exit code."""
    monkeypatch.setattr(
        preflight,
        "open_chatgpt_session",
        lambda cc, browser: (_as_session(live_session), None),
    )
    out = tmp_path / "preflight.json"
    code = preflight.main(["--json", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))

    assert code in (0, 1, 2)
    assert doc["exit_code"] == code
    assert doc["verdict"] == preflight.verdict_of(doc["checks"])[0]
    assert [c["name"] for c in doc["checks"]] == DEFAULT_NAMES
    # A host may legitimately block right now (memory, a send in flight);
    # against a working account the other groups must not.
    outside_host = [c for c in doc["checks"] if c["group"] != "host"]
    assert all(c["state"] != "block" for c in outside_host), outside_host
