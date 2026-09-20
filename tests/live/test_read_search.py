"""Tier T1 smoke test: global chat search (ROADMAP.md R6) against the real
account.

Marker live_read, skipped unless CHATGPT_LIVE=read (tests/conftest.py), same
pattern as tests/live/test_read_smoke.py. Shape only, never the user's own
data: TESTING.md section 2 says a smoke test proves "each read command exits
as documented against the real account", not what the account contains --
titles and snippets are the user's own content and must never be asserted on
here. The POST this test makes is a read (guard.py's READ_POSTS), so it runs
under tier read like every other T1 test.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.live_read

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def test_a_common_word_returns_conversation_shaped_hits(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/global/search",
        method="POST",
        payload={
            "query": "the",
            "limit": 3,
            "source_requests": [{"type": "conversation"}],
        },
    )
    assert status == 200
    items = body["items"]
    assert isinstance(items, list)
    for item in items:
        payload = item["payload"]
        assert UUID_RE.match(payload["conversation_id"])
        assert item["match_kind"] in {"title", "content"}


def test_a_nonsense_token_returns_no_hits(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/global/search",
        method="POST",
        payload={
            "query": "zqxjkv-no-such-thing",
            "limit": 3,
            "source_requests": [{"type": "conversation"}],
        },
    )
    assert status == 200
    assert body["items"] == []
    assert body["cursor"] is None
