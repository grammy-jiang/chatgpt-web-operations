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

# The sandbox sweep's own page walk. A server that never returns a null
# cursor must not turn it into an infinite loop -- MAX_SWEEP_PAGES is a hard
# stop, the same shape as list_projects.py's MAX_SIDEBAR_PAGES.
SWEEP_PAGE_LIMIT = 50
MAX_SWEEP_PAGES = 50

# The T4 send cap (TESTING.md section 1). Every T4 test makes exactly one
# send, so the cap is counted in tests. It is a stop against a suite that
# grows a send storm by accident, not a quota to tune per run: the default
# leaves headroom above the tests that exist, and going past it fails the
# run loudly rather than skipping tests. A cap that skips instead would
# repeat the defect tests/live/test_send_chat_flags.py had: a test that
# quietly does not run reads as a pass.
DEFAULT_LIVE_SENDS = 4
SENDS_VAR = "CHATGPT_LIVE_SENDS"


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


@pytest.fixture(scope="session")
def live_transport(tier: str) -> Any:
    """Authenticate once per tier, avoiding one session renewal per test."""
    import chatgpt_client

    return chatgpt_client.ChatGPTSession("chrome").session


@pytest.fixture
def live_session(tier: str, sandbox_id: str, live_transport: Any) -> Any:
    """A real ChatGPTSession whose .session is wrapped in GuardedSession.

    For every tier but read, the sandbox's own name is verified first: a
    stale or wrong id in sandbox.json must never let a write tier run
    against a project that is not rp-test-sandbox.
    """
    guarded = guard.GuardedSession(
        inner=live_transport, tier=tier, sandbox_id=sandbox_id
    )

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


def sweep_targets(listing_pages: Any) -> list[tuple[str, str]]:
    """Every conversation ``(id, title)`` across ``listing_pages``, unfiltered.

    ``listing_pages`` is the raw body of each page already fetched from
    ``GET gizmos/<sandbox_id>/conversations`` (``{"items": [...], "cursor":
    ...}``), in the order ``_sweep_sandbox_after_live_writes`` walked them.
    That walk needs a live call per page, so it happens there, not here;
    this function only decides which of the already-fetched items are
    sweep targets -- the pure half of the sweep, unit-tested in
    tests/test_harness.py without a network call.

    Every item with an id is a target, whatever its title. Before
    2026-09-20 the sweep kept only titles starting with "rp-test", but
    ChatGPT renames a chat itself once the first reply lands -- measured
    the same day: "rp-test send 1" became "Reply PONG" and "rp-test send
    5" became "Python Release Schedule Summary" -- so a title-based filter
    let a renamed chat escape the sweep and it had to be deleted by hand.
    The sandbox project is used by nothing but these tests (TESTING.md
    §1), so nothing found in it needs a title check to be safe to delete.
    """
    targets: list[tuple[str, str]] = []
    for page in listing_pages:
        items = page.get("items") if isinstance(page, dict) else None
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                targets.append((str(item["id"]), str(item.get("title", ""))))
    return targets


@pytest.fixture(scope="session", autouse=True)
def _sweep_sandbox_after_live_writes(request: Any, tier: str, sandbox_id: str) -> Any:
    """Delete every conversation left in the sandbox, in teardown.

    Session-scoped and autouse, so it runs once after the whole session even
    if a test fails partway through. Builds its own guarded session rather
    than reusing live_session, so it needs no function-scoped fixture and
    still never runs for a tier that never opens one.

    ``guarded.refresh()`` runs first so every id already in the sandbox is
    known to the guard before any PATCH is attempted (tests/live/guard.py
    allows a non-GET on ``conversation/<id>`` only for an id the guard has
    already seen). The sweep then pages through
    ``gizmos/<sandbox_id>/conversations`` itself, starting at
    ``cursor=0&limit=50`` and following the returned ``cursor`` for as long
    as one comes back, noting every id it sees the same way -- a sandbox
    with more than one page of conversations must not leave the later pages
    unswept. ``sweep_targets`` then decides what to delete from the pages
    collected: every conversation, titled "rp-test ..." or not (TESTING.md
    §1).
    """
    yield
    if tier not in ("write", "browser", "send"):
        return
    import chatgpt_client

    session = chatgpt_client.ChatGPTSession("chrome")
    guarded = guard.GuardedSession(
        inner=session.session, tier=tier, sandbox_id=sandbox_id
    )
    guarded.refresh()

    pages: list[dict] = []
    cursor = "0"
    for _ in range(MAX_SWEEP_PAGES):
        status, body = guarded.call(
            f"/backend-api/gizmos/{sandbox_id}/conversations"
            f"?cursor={cursor}&limit={SWEEP_PAGE_LIMIT}"
        )
        if status != 200 or not isinstance(body, dict):
            break
        items = body.get("items") or []
        if not items:
            break
        pages.append(body)
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                guarded.note(item["id"])
        cursor = body.get("cursor") or ""
        if not cursor:
            break

    targets = sweep_targets(pages)
    if request.config.getoption("--keep-sandbox-chats"):
        titles = [title for _id, title in targets]
        print(f"--keep-sandbox-chats: left {len(targets)} conversation(s): {titles}")
        return
    for conversation_id, _title in targets:
        guarded.call(
            f"/backend-api/conversation/{conversation_id}",
            method="PATCH",
            payload={"is_visible": False},
        )
    titles = [title for _id, title in targets]
    print(f"sandbox sweep: deleted {len(targets)} conversation(s): {titles}")


class SendBudget:
    """How many sends a T4 run has left. Pure; no network, no pytest."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    def spend(self, what: str) -> str:
        """Count one send and return "" , or the reason it is refused."""
        self.spent += 1
        if self.spent > self.limit:
            return (
                f"{what} would be send {self.spent} of this run, over the "
                f"cap of {self.limit} ({SENDS_VAR}); refusing to send"
            )
        return ""


def budget_limit(environ: Any) -> int:
    """``CHATGPT_LIVE_SENDS`` as an int, or the default.

    A value that is not a positive number is the default too: a typo in an
    environment variable must not quietly lift a safety cap.
    """
    raw = str(environ.get(SENDS_VAR, "")).strip()
    if not raw.isdigit() or int(raw) < 1:
        return DEFAULT_LIVE_SENDS
    return int(raw)


@pytest.fixture(scope="session")
def send_budget() -> SendBudget:
    """One budget for the whole run, shared by every T4 test."""
    return SendBudget(budget_limit(os.environ))


@pytest.fixture(autouse=True)
def _spend_one_send(request: Any, tier: str, send_budget: SendBudget) -> None:
    """Count this test's one send before it runs, and fail if it is over.

    Autouse and marker-gated, so a T4 test cannot forget to be counted and
    a T1 to T3 test is never touched. It runs before the test body, which
    is the only place a refusal can still stop the send from happening.
    """
    if tier != "send" or request.node.get_closest_marker("live_send") is None:
        return
    refusal = send_budget.spend(request.node.name)
    if refusal:
        pytest.fail(refusal)
