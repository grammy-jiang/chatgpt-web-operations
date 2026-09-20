"""Tier T1 smoke test: scheduled tasks ("automations", ROADMAP.md R4)
against the real account.

Marker live_read, skipped unless CHATGPT_LIVE=read (tests/conftest.py), same
pattern as tests/live/test_read_search.py. Shape only, never the user's own
data (TESTING.md section 2): a task's title and prompt are the user's own
content and must never be asserted on here, only that the documented keys
are present with the right kind of value.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_read

FILTERS = ("scheduled", "paused", "finished")
TIMING_MODES = {"condition_watch", "exact_schedule", "flexible_schedule"}


@pytest.mark.parametrize("filter_name", FILTERS)
def test_each_filter_returns_shaped_items(filter_name: str, live_session) -> None:
    status, body = live_session.call(f"/backend-api/automations?filter={filter_name}")
    assert status == 200
    assert "cursor" in body
    items = body["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item["id"], str)
        assert isinstance(item["title"], str)
        assert isinstance(item["is_enabled"], bool)
        assert item["timing_mode"] in TIMING_MODES
        assert isinstance(item["conversation_id"], str)


def test_suggested_automations_returns_a_list(live_session) -> None:
    """Documented in references/endpoint-discovery.md; no command uses it,
    but the read itself is still cheap and worth a shape check."""
    status, body = live_session.call("/backend-api/suggested_automations")
    assert status == 200
    assert isinstance(body, list)
