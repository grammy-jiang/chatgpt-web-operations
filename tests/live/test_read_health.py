"""Tier T1: health.py's own "health" group against the real account, and
the whole command end to end (marker live_read, CHATGPT_LIVE=read).

Mirrors tests/live/test_read_preflight.py: every pure verdict function and
every fetch wrapper is proved offline against fakes (tests/test_health.py);
what this file proves is the composition -- the real ``Session.call``
feeding health's own verdict functions over the guarded session, and
``main()`` producing the documented exit code and the "facts" document's
real shape.

Shape only, never the user's data (TESTING.md section 2). The one state
this file requires exactly is "sandbox": these tests own that project, so
it must read ok, never warn (a leftover chat) or block (a wrong project) --
a failure here means some other live test left the sandbox dirty, or
sandbox.json itself is stale.
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

import health  # noqa: E402
import preflight  # noqa: E402

pytestmark = pytest.mark.live_read

STATES = {"ok", "warn", "block"}

# The fourteen checks a bare `health.py` makes with a working link and
# account: preflight's own eleven (DEFAULT_NAMES in
# tests/live/test_read_preflight.py), then health's own three.
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
    "session token",
    "sandbox",
    "read endpoints",
]


def _as_session(live_session: Any) -> SimpleNamespace:
    """health's fetch functions take a ChatGPTSession-like object and call
    ``.session.call``; the guarded ``live_session`` is that inner object."""
    return SimpleNamespace(session=live_session)


def test_health_group_reads_the_real_account(
    live_session: Any, sandbox_id: str
) -> None:
    sandbox = health._sandbox()
    assert sandbox["id"] == sandbox_id

    checks = health.health_checks(
        health.cc, _as_session(live_session), "chrome", sandbox
    )
    assert [c["name"] for c in checks] == ["session token", "sandbox", "read endpoints"]
    assert all(c["state"] in STATES for c in checks)

    session_token, sandbox_check, endpoints = checks
    assert session_token["state"] in {"ok", "warn"}, session_token["detail"]
    assert "expires" in session_token["detail"]

    assert sandbox_check["state"] == "ok", sandbox_check["detail"]
    assert sandbox["name"] in sandbox_check["detail"]

    assert endpoints["state"] == "ok", endpoints["detail"]
    assert "sidebar 200" in endpoints["detail"]
    assert "pins 200" in endpoints["detail"]


def test_facts_of_reads_the_real_account(live_session: Any, sandbox_id: str) -> None:
    sandbox = health._sandbox()
    facts = health.facts_of(health.cc, _as_session(live_session), "chrome", sandbox)

    assert facts["sandbox_id"] == sandbox_id
    assert facts["sandbox_name"] == sandbox["name"]
    assert facts["sandbox_conversations"] == 0

    # The account's own model/effort record must resolve to a real preset,
    # the same invariant tests/live/test_read_preflight.py checks for
    # preflight's "model and effort" check (SKILL.md, "Reasoning effort").
    assert facts["model"]
    assert facts["presets"] > 0
    assert facts["preset"] is not None, facts

    if facts["session_expires"] is not None:
        assert facts["session_days_left"] is not None
        assert facts["session_days_left"] > 0

    assert facts["gates"] is None or set(facts["gates"]) <= {
        "proofofwork",
        "turnstile",
        "so",
        "arkose",
    }

    endpoints = facts["endpoints"]
    assert endpoints["gizmos/snorlax/sidebar"] == 200
    assert endpoints["pins"] == 200
    assert endpoints[f"gizmos/{sandbox_id}"] == 200
    assert endpoints[f"gizmos/{sandbox_id}/conversations"] == 200


def test_the_whole_command_exits_as_documented(
    live_session: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """``main()`` over the guarded session: the host and link groups read
    the real host, the account, run and health groups read the real
    account, and the document written by --json agrees with the exit
    code."""
    monkeypatch.setattr(
        preflight,
        "open_chatgpt_session",
        lambda cc, browser: (_as_session(live_session), None),
    )
    out = tmp_path / "health.json"
    code = health.main(["--json", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))

    assert code in (0, 1, 2)
    assert doc["exit_code"] == code
    assert doc["verdict"] == preflight.verdict_of(doc["checks"])[0]
    assert [c["name"] for c in doc["checks"]] == DEFAULT_NAMES

    # A host may legitimately block right now (memory, a send in flight);
    # against a working account and a clean sandbox every other group must
    # not -- the same tolerance test_read_preflight.py applies, extended to
    # the new "health" group.
    outside_host = [c for c in doc["checks"] if c["group"] != "host"]
    assert all(c["state"] != "block" for c in outside_host), outside_host

    sandbox_check = next(c for c in doc["checks"] if c["name"] == "sandbox")
    assert sandbox_check["state"] == "ok", sandbox_check["detail"]

    facts = doc["facts"]
    assert facts["sandbox_conversations"] == 0
    assert facts["endpoints"]["pins"] == 200
    # Never the user's data: only shape, counts and statuses appear above;
    # nothing here ever asserts a cookie value or a conversation's content.
