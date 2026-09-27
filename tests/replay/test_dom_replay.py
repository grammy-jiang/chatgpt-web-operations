"""Tier R: the client's page functions against recorded ChatGPT pages in a
real headless Chrome (TESTING.md section 6, P1; marker ``replay``).

What this proves that the fake-page tests cannot: ``COMPOSER_SELECTOR``,
``COMPOSER_STATE_JS``, the turn selectors and the chat-load notice regex are
run by a real browser against the DOM ChatGPT actually served, as recorded
by the daily browser check and promoted into ``tests/fixtures/dom``. When
ChatGPT changes the page, the daily check records the new page and reports
the drift; once promoted, these tests fail offline until the client matches
it -- the composer change of 2026-09-26 would have failed here the morning
it shipped.

Every fixture is loaded by name, never by list comprehension over an empty
directory: a missing fixture fails ``test_the_recorded_composer_fixture_exists``
rather than parametrizing nothing and reading as a pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from replay.conftest import FIXTURES, facts_of, fixture_names

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402

pytestmark = pytest.mark.replay

COMPOSERS = fixture_names("composer")
PROBE_TEXT = "replay probe: the recorded composer takes text"


def test_the_recorded_composer_fixture_exists() -> None:
    """``composer.html`` is the daily check's promoted recording; the
    synthetic legacy page is a second generation, not a substitute."""
    assert (FIXTURES / "composer.html").is_file(), sorted(
        p.name for p in FIXTURES.iterdir()
    )
    assert "composer-legacy-synthetic" in COMPOSERS


@pytest.mark.parametrize("name", COMPOSERS)
def test_find_composer_matches_exactly_one_element(replay, name: str) -> None:
    page = replay(name)
    composer = cc.find_composer(page, timeout_ms=5_000)
    assert composer.count() == 1
    facts = cc.page_snapshot(page)
    assert facts["composer"]["found"] is True
    assert facts["composer"]["matched"] == facts_of(name)["composer"]["matched"]


@pytest.mark.parametrize("name", COMPOSERS)
def test_composer_state_agrees_with_the_recorded_send_button(replay, name: str):
    """Two scripts read the send button (COMPOSER_STATE_JS for uploads,
    SNAPSHOT_JS for the record); on the same DOM they must agree."""
    page = replay(name)
    state = cc.composer_state(page)
    recorded = facts_of(name)["send_button"]
    assert state["send_exists"] == recorded["found"]
    if recorded["found"]:
        assert state["send_enabled"] == (not recorded["disabled"])


@pytest.mark.parametrize("name", COMPOSERS)
def test_a_fill_is_read_back_from_the_recorded_composer(replay, name: str) -> None:
    """The send's read-back check (``text_taken``) on a real contenteditable:
    what ``fill`` put in is what ``inner_text`` shows."""
    page = replay(name)
    composer = cc.find_composer(page, timeout_ms=5_000)
    composer.fill(PROBE_TEXT)
    assert cc.text_taken(PROBE_TEXT, composer.inner_text())


def test_find_composer_raises_the_send_s_error_on_a_page_without_one(replay):
    page = replay("chat-load-failed-synthetic", url="https://chatgpt.com/c/x")
    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        cc.find_composer(page, timeout_ms=1_000)


def test_the_chat_load_notice_is_detected_and_a_composer_page_is_clean(replay):
    notice_page = replay("chat-load-failed-synthetic", url="https://chatgpt.com/c/x")
    assert "could not load" in cc.chat_load_failure(notice_page).lower()
    assert cc.chat_load_failure(replay("composer")) == ""


def test_a_snapshot_of_a_recorded_page_keeps_what_the_selectors_need(replay):
    """Snapshotting the recorded page again and loading that second document
    gives the same composer and send-button facts: the sanitizer keeps every
    attribute a selector reads and its output is stable (idempotent)."""
    first = cc.page_snapshot(replay("composer"))
    again = cc.page_snapshot(replay.html(cc.snapshot_document(first)))
    assert cc.snapshot_drift(again, first) == []
    assert cc.sanitize_html(first["html"]["composer"]) == first["html"]["composer"]


def test_turn_selectors_count_the_recorded_conversation_when_one_is_recorded(
    replay,
) -> None:
    """A conversation page is recorded by the weekly send (TESTING.md P3),
    which owns a chat for a minute. Until the first one is promoted this
    test proves the selectors on the synthetic notice page only (no turns),
    and says so."""
    names = fixture_names("conversation")
    if not names:
        facts = cc.page_snapshot(replay("chat-load-failed-synthetic"))
        assert facts["turns"] == {
            "user": 0,
            "any": 0,
            "articles": 0,
            "matched_user": [],
            "matched_any": [],
        }
        pytest.fail(
            "no conversation fixture is recorded yet: run make live-send once "
            "(tests/live/test_send_cli_roundtrip.py records it), then "
            "make refresh-dom-fixtures FROM=<its dom dir>"
        )
    for name in names:
        facts = cc.page_snapshot(replay(name, url="https://chatgpt.com/c/x"))
        recorded = facts_of(name)["turns"]
        assert facts["turns"]["user"] == recorded["user"] > 0
        assert facts["turns"]["any"] == recorded["any"] > 0
        assert facts["turns"]["matched_user"] == recorded["matched_user"]
