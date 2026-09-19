"""Tier T1 smoke tests: each read command's endpoint against the real account.

Marker live_read, skipped unless CHATGPT_LIVE=read (tests/conftest.py). These
assert shape only, never the user's own data: TESTING.md section 2 says a
smoke test proves "each read command exits as documented against the real
account", not what the account contains.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_read


def test_me_identifies_the_account(live_session) -> None:
    status, body = live_session.call("/backend-api/me")
    assert status == 200
    assert "id" in body


def test_models_lists_the_power_slider_s_versions(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/models"
        "?iim=false&is_gizmo=false&supports_model_picker_upgrade_presets=true"
    )
    assert status == 200
    assert isinstance(body["versions"], list)
    assert body["versions"]


def test_snorlax_sidebar_lists_projects_with_a_cursor(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/gizmos/snorlax/sidebar?owned_only=true&limit=5"
    )
    assert status == 200
    assert "items" in body
    assert "cursor" in body


def test_memories_reports_token_usage(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/memories?include_memory_entries=false"
    )
    assert status == 200
    assert "memory_num_tokens" in body
