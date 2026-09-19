"""Fixtures for the live tiers (TESTING.md section 1).

Nothing here touches the network at collection time: chatgpt_client is only
imported inside a fixture body, and every fixture that could reach the
account skips first unless CHATGPT_LIVE is set to match. tests/conftest.py's
marker gate is the first line of defense; tier and sandbox_id below are the
second, so a fixture used directly (not just a marked test) still refuses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from live import guard

SANDBOX_FILE = Path(__file__).resolve().parent / "sandbox.json"


def _sandbox_data() -> dict:
    return json.loads(SANDBOX_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def tier() -> str:
    """Which live tier this run is, from CHATGPT_LIVE (TESTING.md tier table)."""
    value = os.environ.get("CHATGPT_LIVE")
    if not value:
        pytest.skip("CHATGPT_LIVE is not set; run this through make live-<tier>")
    return value


@pytest.fixture(scope="session")
def sandbox_id() -> str:
    """The sandbox project's gizmo id, or skip until sandbox.json has one."""
    sid = _sandbox_data().get("id", "")
    if not sid:
        pytest.skip("tests/live/sandbox.json has no id; create rp-test-sandbox first")
    return sid


@pytest.fixture
def live_session(tier: str, sandbox_id: str) -> Any:
    """A real ChatGPTSession whose .session is wrapped in GuardedSession.

    For every tier but read, the sandbox's own name is verified first: a
    stale or wrong id in sandbox.json must never let a write tier run
    against a project that is not rp-test-sandbox.
    """
    import chatgpt_client

    session = chatgpt_client.ChatGPTSession("chrome")
    guarded = guard.GuardedSession(
        inner=session.session, tier=tier, sandbox_id=sandbox_id
    )
    session.session = guarded

    if tier != "read":
        expected = _sandbox_data()["name"]
        status, body = guarded.call(f"/backend-api/gizmos/{sandbox_id}")
        name = ((body or {}).get("gizmo") or {}).get("display", {}).get("name")
        if status != 200 or name != expected:
            pytest.fail(
                f"gizmos/{sandbox_id} display name is {name!r}, not {expected!r}; "
                "refusing to run a write tier against the wrong project"
            )
    return guarded


@pytest.fixture(scope="session", autouse=True)
def _sweep_sandbox_after_live_writes(request: Any, tier: str, sandbox_id: str) -> Any:
    """Delete every rp-test conversation left in the sandbox, in teardown.

    Session-scoped and autouse, so it runs once after the whole session even
    if a test fails partway through. Builds its own guarded session rather
    than reusing live_session, so it needs no function-scoped fixture and
    still never runs for a tier that never opens one.
    """
    yield
    if tier not in ("write", "browser", "send"):
        return
    import chatgpt_client

    session = chatgpt_client.ChatGPTSession("chrome")
    guarded = guard.GuardedSession(
        inner=session.session, tier=tier, sandbox_id=sandbox_id
    )
    status, body = guarded.call(
        f"/backend-api/gizmos/{sandbox_id}/conversations?cursor=0"
    )
    items = body.get("items", []) if status == 200 and isinstance(body, dict) else []
    for item in items:
        if isinstance(item, dict) and "id" in item:
            guarded.note(item["id"])
    stale = [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("title", "")).startswith("rp-test")
    ]
    if request.config.getoption("--keep-sandbox-chats"):
        titles = [item.get("title", "") for item in stale]
        print(f"--keep-sandbox-chats: left {len(stale)} conversation(s): {titles}")
        return
    for item in stale:
        guarded.call(
            f"/backend-api/conversation/{item['id']}",
            method="PATCH",
            payload={"is_visible": False},
        )
