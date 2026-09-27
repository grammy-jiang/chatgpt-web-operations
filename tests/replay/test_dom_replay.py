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

import re
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
RECORDED_COMPOSERS = [n for n in COMPOSERS if not n.endswith("-synthetic")]
WITH_SEND_BUTTON = ("composer-filled", "composer-attached")
ATTACHED = "composer-attached"
NOTICE = "chat-load-failed-synthetic"
NOTICE_URL = "https://chatgpt.com/c/x"
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


def test_the_attached_composer_names_the_file_in_its_remove_label(replay):
    """Recorded 2026-09-27 after a real upload: the chip's remove button is
    "Remove <name>" (until 2026-09-26 it was "Remove file 1: <name>") and
    the send button is enabled. The T3 upload test's ``verify()`` reads
    exactly this list; with the old prefix it was empty and T3 failed."""
    state = cc.composer_state(replay(ATTACHED))
    # The weekly T3 run records this page; its file is rp-test-browser-upload.txt,
    # renamed "(1)", "(2)" ... by ChatGPT when the name was uploaded before.
    assert len(state["remove_labels"]) == 1, state["remove_labels"]
    assert re.fullmatch(r"Remove rp-test-[\w.()-]+", state["remove_labels"][0]), state
    assert state["send_exists"] is True
    assert state["send_enabled"] is True


@pytest.mark.parametrize("name", WITH_SEND_BUTTON)
def test_a_send_button_alternative_matches_the_recorded_button(replay, name):
    """``_click_send`` tries ``SEND_BUTTONS`` in order; at least one must
    match the button a composer with text or an attachment shows."""
    page = replay(name)
    matched = [
        sel for sel in cc.BrowserSender.SEND_BUTTONS if page.locator(sel).count()
    ]
    assert matched, cc.BrowserSender.SEND_BUTTONS
    assert page.locator(matched[0]).first.is_enabled()


def test_no_send_button_alternative_matches_an_empty_composer(replay) -> None:
    """The empty home composer shows "Start Voice" where the send button
    goes; nothing in ``SEND_BUTTONS`` may take that for a send button."""
    page = replay("composer")
    assert [s for s in cc.BrowserSender.SEND_BUTTONS if page.locator(s).count()] == []


@pytest.mark.parametrize("name", RECORDED_COMPOSERS)
def test_the_upload_input_is_found_by_the_fallback_on_every_recorded_page(replay, name):
    """``UPLOAD_INPUT_SELECTOR`` is the id the page had until 2026-09-26; the
    recorded pages carry three file inputs, and only the fallback names the
    one without an image filter, which is the one a real upload used."""
    page = replay(name)
    assert page.locator(cc.UPLOAD_INPUT_SELECTOR).count() == 0
    assert page.locator(cc.UPLOAD_INPUT_FALLBACK_SELECTOR).count() == 1


def test_the_legacy_composer_still_offers_the_old_upload_input(replay) -> None:
    page = replay("composer-legacy-synthetic")
    assert page.locator(cc.UPLOAD_INPUT_SELECTOR).count() == 1


def test_find_composer_raises_the_send_s_error_on_a_page_without_one(replay):
    page = replay(NOTICE, url=NOTICE_URL)
    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        cc.find_composer(page, timeout_ms=1_000)


def test_the_chat_load_notice_is_detected_and_a_composer_page_is_clean(replay):
    notice_page = replay(NOTICE, url=NOTICE_URL)
    assert "could not load" in cc.chat_load_failure(notice_page).lower()
    assert cc.chat_load_failure(replay("composer")) == ""


def test_the_retry_button_regex_finds_the_notice_s_button(replay) -> None:
    """``_await_chat`` presses the notice's Retry button when there is one
    (``CHAT_RETRY_BUTTON_RE``); the notice fixture carries it."""
    page = replay(NOTICE, url=NOTICE_URL)
    assert page.get_by_role("button", name=cc.CHAT_RETRY_BUTTON_RE).count() == 1
    assert page.get_by_role("button", name=cc.CHAT_RETRY_BUTTON_RE).first.is_visible()


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


# ---------------------------------------------------------------------------
# every page constant, by name (tests/test_dom_coverage.py ties them here)
# ---------------------------------------------------------------------------


def test_the_matched_alternatives_are_the_named_ones(replay) -> None:
    """``page_snapshot`` reports which alternative of each selector tuple
    matched; every reported alternative must be one the client defines, so
    a fixture can never claim a selector the client does not have."""
    facts = cc.page_snapshot(replay("conversation", url=NOTICE_URL))
    assert set(facts["composer"]["matched"]) <= set(cc.COMPOSER_SELECTORS)
    assert set(facts["turns"]["matched_user"]) <= set(cc.USER_TURN_SELECTORS)
    assert set(facts["turns"]["matched_any"]) <= set(cc.CHAT_TURN_SELECTORS)
    assert facts["composer"]["matched"]
    assert facts["turns"]["matched_user"]


def test_the_joined_selectors_count_what_their_alternatives_count(replay):
    page = replay("conversation", url=NOTICE_URL)
    recorded = facts_of("conversation")["turns"]
    assert page.locator(cc.USER_TURN_SELECTOR).count() == recorded["user"]
    assert page.locator(cc.CHAT_TURN_SELECTOR).count() == recorded["any"]
    assert page.locator(cc.COMPOSER_SELECTOR).count() == 1


def test_the_notice_regex_matches_the_notice_text_on_the_page(replay) -> None:
    page = replay(NOTICE, url=NOTICE_URL)
    assert page.get_by_text(cc.CHAT_LOAD_FAILURE_RE).count() == 1
    assert page.get_by_text(cc.CHAT_LOAD_FAILURE_RE).first.is_visible()


def test_the_two_page_scripts_are_the_ones_the_functions_run(replay) -> None:
    """``composer_state`` and ``page_snapshot`` wrap ``COMPOSER_STATE_JS``
    and ``SNAPSHOT_JS``; running the scripts directly on the recorded page
    gives the same facts the functions report, so neither can drift from
    its script unnoticed."""
    page = replay(ATTACHED)
    raw_state = page.evaluate(cc.COMPOSER_STATE_JS)
    assert cc.composer_state(page) == {
        "remove_labels": [str(label) for label in raw_state["remove_labels"]],
        "send_exists": bool(raw_state["send_exists"]),
        "send_enabled": bool(raw_state["send_enabled"]),
    }
    spec = {
        "composer": list(cc.COMPOSER_SELECTORS),
        "user_turn": list(cc.USER_TURN_SELECTORS),
        "chat_turn": list(cc.CHAT_TURN_SELECTORS),
    }
    raw = page.evaluate(cc.SNAPSHOT_JS, spec)
    facts = cc.page_snapshot(page)
    assert raw["composer"] == facts["composer"]
    assert raw["send_button"] == facts["send_button"]
    assert raw["turns"] == facts["turns"]
