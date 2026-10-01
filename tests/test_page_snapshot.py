"""T0 tests for the page-level functions of chatgpt_client (TESTING.md
section 6, P1): the sanitizer, the snapshot, the drift comparison, the
composer/notice/state readers, the read-back rule, and
``BrowserSender.snapshot_page`` over the fake page.

The replay tier (tests/replay) runs the same functions in a real Chrome on
recorded pages; this file proves their decisions over data and their
behaviour when a page returns nothing usable.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

RAW_FORM = (
    '<FORM class="w-full" data-type="unified-composer" style="color:red">'
    '<div id="prompt-textarea" contenteditable="true" role="textbox"'
    ' aria-label="New chat in rp-test-sandbox" title="a title">'
    '<p data-placeholder="Ask anything">the user typed this<br class="x"></p></div>'
    '<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>'
    '<img src="https://x/avatar.png" alt="someone">'
    '<a href="https://chatgpt.com/g/g-p-abc/project">link text</a>'
    "<script>alert(1)</script><style>.a{}</style>"
    '<button data-testid="send-button" aria-label="Send prompt" type="button"'
    " disabled>Send</button>"
    '<input id="upload-files" type="file" value="v" tabindex="-1">'
    "<!-- a comment --></FORM>"
)

RAW_SURFACE = (
    '<div role="group" aria-label="Composer mode">'
    '<button type="button" aria-pressed="true" data-state="closed">Chat</button>'
    '<button type="button" aria-pressed="false" data-state="closed">Work</button>'
    "</div>"
)

RAW_SNAPSHOT = {
    "url": "https://chatgpt.com/",
    "composer": {
        "found": True,
        "count": 1,
        "matched": [cc.COMPOSER_SELECTORS[1]],
        "tag": "div",
        "id": None,
        "aria_label": None,
        "in_form": True,
    },
    "send_button": {
        "found": True,
        "testid": "send-button",
        "aria_label": "Send prompt",
        "disabled": True,
    },
    "turns": {
        "user": 0,
        "any": 0,
        "articles": 0,
        "matched_user": [],
        "matched_any": [],
    },
    "surface": {"found": True, "buttons": 2, "pressed": ["true", "false"]},
    # 2026-10-01 evening: the facts beyond the composer (page_facts_compact)
    "composers": {"total": 1, "visible": 1, "hidden": 0},
    "uploads": {
        "inputs": 3,
        "primary_found": False,
        "any_file": 1,
        "accepts": ["", "image/*", "image/*,video/*"],
    },
    "controls": {"attach_menu": True, "picker": True, "remove_chips": 0},
    "page": {"title_kind": "chatgpt", "challenge": False, "login": False},
    "html": {
        "composer": RAW_FORM,
        "turns": ["<article data-testid='conversation-turn-1'>hi</article>"],
        "surface": RAW_SURFACE,
    },
}


# ---------------------------------------------------------------------------
# sanitize_html
# ---------------------------------------------------------------------------


def test_sanitize_drops_text_urls_scripts_and_media_but_keeps_selector_attributes():
    out = cc.sanitize_html(RAW_FORM)
    for gone in (
        "the user typed",
        "link text",
        "Send</button>",
        "href",
        "src=",
        "alt=",
        "title=",
        "style",
        "<svg",
        "<path",
        "<script",
        "alert",
        "<img",
        "comment",
        'value="v"',
    ):
        assert gone not in out, (gone, out)
    stand_in = '<br data-snapshot-text="">'
    assert f"<a>{stand_in}</a>" in out  # the element stays; href and text do not
    assert '<form class="w-full" data-type="unified-composer">' in out
    assert (
        '<div id="prompt-textarea" contenteditable="true" role="textbox"'
        ' aria-label="New chat in rp-test-sandbox">'
    ) in out
    assert f'<p data-placeholder="Ask anything">{stand_in}<br class="x"></p>' in out
    assert (
        '<button data-testid="send-button" aria-label="Send prompt" type="button"'
        f' disabled="">{stand_in}</button>'
    ) in out
    assert '<input id="upload-files" type="file" tabindex="-1">' in out
    assert out.endswith("</form>")


def test_sanitize_is_idempotent_and_leaves_no_text_node() -> None:
    once = cc.sanitize_html(RAW_FORM)
    assert cc.sanitize_html(once) == once
    assert not re.search(r">\s*[^<\s][^<]*<", once)


def test_sanitize_replaces_each_run_of_text_with_one_stand_in() -> None:
    """A prompt in the composer's paragraphs must not survive, but the
    paragraph must keep a line of height, or Playwright calls the recorded
    composer invisible (found on the first filled-composer recording)."""
    out = cc.sanitize_html(
        "<div><p>one two</p><p>  </p><p>three<b>four</b>five</p></div>"
    )
    assert out == (
        '<div><p><br data-snapshot-text=""></p><p></p>'
        '<p><br data-snapshot-text=""><b><br data-snapshot-text=""></b>'
        '<br data-snapshot-text=""></p></div>'
    )
    assert cc.sanitize_html("tail text") == '<br data-snapshot-text="">'
    assert cc.sanitize_html("<svg>text inside svg</svg>") == ""


def test_sanitize_truncates_long_values_and_escapes_quotes() -> None:
    out = cc.sanitize_html('<div class="' + "a" * 500 + '" data-x=\'say "hi"\'></div>')
    assert 'class="' + "a" * 200 + '"' in out
    assert "a" * 201 not in out
    assert 'data-x="say &quot;hi&quot;"' in out


def test_sanitize_handles_void_elements_and_empty_input() -> None:
    assert cc.sanitize_html("<div><br/><hr><input type=text></div>") == (
        '<div><br><hr><input type="text"></div>'
    )
    assert cc.sanitize_html("") == ""
    assert cc.sanitize_html(None) == ""  # type: ignore[arg-type]


def test_sanitize_drops_a_whole_svg_subtree_including_nested_tags() -> None:
    out = cc.sanitize_html(
        '<button><svg><g><path d="x"/></g></svg><span></span></button>'
    )
    assert out == "<button><span></span></button>"


# ---------------------------------------------------------------------------
# page_snapshot, snapshot_document, snapshot_drift
# ---------------------------------------------------------------------------


def test_page_snapshot_sanitizes_the_markup_and_keeps_the_facts() -> None:
    page = fp.Page()
    page.set_evaluate_result("page_snapshot", json.loads(json.dumps(RAW_SNAPSHOT)))
    facts = cc.page_snapshot(page)
    assert facts["composer"]["matched"] == [cc.COMPOSER_SELECTORS[1]]
    assert facts["send_button"]["testid"] == "send-button"
    assert "the user typed" not in facts["html"]["composer"]
    assert facts["html"]["composer"].startswith('<form class="w-full"')
    stand_in = '<br data-snapshot-text="">'
    assert facts["html"]["turns"] == [
        f'<article data-testid="conversation-turn-1">{stand_in}</article>'
    ]
    # The Chat/Work toggle group (2026-10-01): its facts, and its markup with
    # the buttons' text gone and the pressed state kept.
    assert facts["surface"] == {
        "found": True,
        "buttons": 2,
        "pressed": ["true", "false"],
    }
    assert "Chat" not in facts["html"]["surface"]
    assert facts["html"]["surface"].count('aria-pressed="true"') == 1
    assert facts["html"]["surface"].count('aria-pressed="false"') == 1
    js, spec = next(c[2] for c in page.calls if c[0] == "page" and c[1] == "evaluate")
    assert "page_snapshot" in js
    assert spec == {
        "composer": list(cc.COMPOSER_SELECTORS),
        "user_turn": list(cc.USER_TURN_SELECTORS),
        "chat_turn": list(cc.CHAT_TURN_SELECTORS),
        "surface_group": cc.SURFACE_GROUP_SELECTOR,
        "upload_primary": cc.UPLOAD_INPUT_SELECTOR,
        "attach_menu": cc.ATTACH_MENU_SELECTOR,
        "picker": cc.PICKER_SELECTOR,
        "challenge": cc.CHALLENGE_SELECTOR,
        "login": cc.LOGIN_SELECTOR,
    }
    # the page facts of 2026-10-01 evening come through as recorded
    assert facts["composers"] == {"total": 1, "visible": 1, "hidden": 0}
    assert facts["uploads"]["inputs"] == 3 and facts["uploads"]["any_file"] == 1
    assert facts["controls"] == {"attach_menu": True, "picker": True, "remove_chips": 0}
    assert facts["page"] == {
        "title_kind": "chatgpt",
        "challenge": False,
        "login": False,
    }


@pytest.mark.parametrize("raw", [None, "nonsense", [], {"html": "not a dict"}])
def test_page_snapshot_makes_an_empty_record_from_an_unusable_page(raw) -> None:
    """Evidence that cannot be gathered is an empty record, never an error."""
    page = fp.Page()
    page.url = "https://chatgpt.com/x"
    page.set_evaluate_result("page_snapshot", raw)
    facts = cc.page_snapshot(page)
    assert facts["composer"] == {}
    assert facts["send_button"] == {}
    assert facts["turns"] == {}
    assert facts["surface"] == {}
    for group in ("composers", "uploads", "controls", "page"):
        assert facts[group] == {}, group
    assert facts["html"] == {"composer": "", "turns": [], "surface": ""}
    assert facts["url"] == "https://chatgpt.com/x"
    assert cc.page_facts_compact(facts) == {}
    assert cc.page_facts_summary(facts) == ""


def test_page_facts_compact_and_summary_name_what_a_page_without_a_composer_is():
    """The owner's ask of 2026-10-01 evening: when the composer does not
    appear, say what the page is instead -- a challenge, a login page, a
    page with a hidden composer, a page that lost its controls."""
    facts = json.loads(json.dumps(RAW_SNAPSHOT))
    assert cc.page_facts_compact(facts) == {
        "challenge": False,
        "login": False,
        "composers": 1,
        "hidden": 0,
        "toggle": True,
        "attach_menu": True,
        "picker": True,
        "title": "chatgpt",
    }
    facts["page"] = {"title_kind": "other", "challenge": True, "login": False}
    facts["composers"] = {"total": 2, "visible": 0, "hidden": 2}
    facts["controls"] = {"attach_menu": False, "picker": False, "remove_chips": 0}
    facts["surface"] = {"found": False}
    assert cc.page_facts_summary(facts) == (
        "page: challenge=yes, login=no, composers=2 (2 hidden), toggle=no, "
        "attach-menu=no, picker=no, title=other"
    )
    # a page that answered with some groups but no page facts still counts
    assert cc.page_facts_compact({"surface": {"found": True}})["toggle"] is True
    assert cc.page_facts_compact({"composer": {"found": True}}) == {}


def test_snapshot_drift_skips_facts_the_fixture_never_recorded_and_names_them():
    """A fixture promoted before a fact joined SNAPSHOT_DRIFT_KEYS lacks
    it: that is not drift (the page did not change, the keys did), it is an
    unproven fact -- named by snapshot_unproven until a re-recording is
    promoted. A fixture that has the fact still compares it."""
    fresh = json.loads(json.dumps(RAW_SNAPSHOT))
    older = {
        k: v
        for k, v in json.loads(json.dumps(RAW_SNAPSHOT)).items()
        if k not in ("composers", "uploads", "controls", "page")
    }
    assert cc.snapshot_drift(fresh, older) == []
    unproven = cc.snapshot_unproven(older)
    assert "composers.visible" in unproven and "page.challenge" in unproven
    assert "composer.found" not in unproven
    assert cc.snapshot_unproven(RAW_SNAPSHOT) == []
    current = json.loads(json.dumps(RAW_SNAPSHOT))
    fresh["composers"]["hidden"] = 1
    fresh["uploads"]["primary_found"] = True
    fresh["page"]["login"] = True
    assert cc.snapshot_drift(fresh, current) == [
        "composers.hidden",
        "uploads.primary_found",
        "page.login",
    ]


def test_find_composer_s_message_carries_the_page_facts(monkeypatch) -> None:
    """The three mornings of "logged out or challenged" (2026-09-29 to
    10-01) now end with what the page is: no challenge, no login route, how
    many composers the selector matched and whether the controls are there."""
    fp.install(monkeypatch)
    page = fp.Page()
    page.url = "https://chatgpt.com/"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    raw = json.loads(json.dumps(RAW_SNAPSHOT))
    raw["composer"] = {"found": False, "count": 0, "matched": []}
    raw["composers"] = {"total": 0, "visible": 0, "hidden": 0}
    raw["controls"] = {"attach_menu": False, "picker": False, "remove_chips": 0}
    page.set_evaluate_result("page_snapshot", raw)
    with pytest.raises(cc.TransportError) as excinfo:
        cc.find_composer(page, timeout_ms=5)
    message = str(excinfo.value)
    assert "(logged out or challenged)" in message
    assert message.endswith(
        "[page: challenge=no, login=no, composers=0 (0 hidden), toggle=yes, "
        "attach-menu=no, picker=no, title=chatgpt]"
    )
    # a page that gives no facts keeps the plain message
    plain = fp.Page()
    plain.url = "https://chatgpt.com/"
    plain.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    with pytest.raises(cc.TransportError) as plain_exc:
        cc.find_composer(plain, timeout_ms=5)
    assert "[page:" not in str(plain_exc.value)


def test_snapshot_document_wraps_turns_and_composer_without_text_nodes() -> None:
    doc = cc.snapshot_document(
        {
            "html": {
                "composer": '<form id="f"></form>',
                "turns": ['<article id="t"></article>', 7],
            }
        }
    )
    assert doc.startswith("<!doctype html>")
    assert '<article id="t"></article>\n<form id="f"></form>' in doc
    assert not re.search(r">\s*[^<\s][^<]*<", doc)
    assert cc.snapshot_document({}).count("<main>") == 1
    # The Chat/Work toggle group sits between the turns and the composer's
    # form, the way the page lays them out; absent, it leaves no blank line.
    with_surface = cc.snapshot_document(
        {
            "html": {
                "composer": '<form id="f"></form>',
                "turns": ['<article id="t"></article>'],
                "surface": '<div role="group" id="s"></div>',
            }
        }
    )
    assert (
        '<article id="t"></article>\n<div role="group" id="s"></div>\n'
        '<form id="f"></form>'
    ) in with_surface


def test_snapshot_drift_names_only_the_facts_the_selectors_depend_on() -> None:
    fixture = json.loads(json.dumps(RAW_SNAPSHOT))
    fresh = json.loads(json.dumps(RAW_SNAPSHOT))
    fresh["url"] = "https://chatgpt.com/other"
    fresh["send_button"]["disabled"] = False
    fresh["turns"]["user"] = 3
    assert cc.snapshot_drift(fresh, fixture) == []
    fresh["composer"]["matched"] = ["#prompt-textarea"]
    fresh["send_button"]["aria_label"] = "Send"
    assert cc.snapshot_drift(fresh, fixture) == [
        "composer.matched",
        "send_button.aria_label",
    ]


def test_snapshot_drift_names_the_composer_label_and_the_toggle_group() -> None:
    """Since 2026-10-01 a send would behave differently on a page whose
    composer says "Work with ChatGPT" (the Work surface) or that has lost
    the Chat/Work toggle, so both are drift."""
    fixture = json.loads(json.dumps(RAW_SNAPSHOT))
    fresh = json.loads(json.dumps(RAW_SNAPSHOT))
    fresh["composer"]["aria_label"] = "Work with ChatGPT"
    fresh["surface"]["found"] = False
    fresh["surface"]["pressed"] = ["false", "true"]  # not compared: churns
    assert cc.snapshot_drift(fresh, fixture) == ["composer.aria_label", "surface.found"]


def test_snapshot_drift_treats_a_missing_group_as_empty() -> None:
    assert cc.snapshot_drift({}, {}) == []
    drift = cc.snapshot_drift({}, RAW_SNAPSHOT)
    assert "composer.found" in drift and "send_button.testid" in drift
    assert cc.snapshot_drift({"composer": "junk"}, {"composer": None}) == []


# ---------------------------------------------------------------------------
# find_composer, chat_load_failure, composer_state, text_taken
# ---------------------------------------------------------------------------


VISIBLE_COMPOSER = (
    f"{cc.COMPOSER_SELECTOR} >> filter({sorted({'visible': True}.items())!r})"
)


def test_find_composer_returns_the_locator_once_visible() -> None:
    page = fp.Page()
    page.set_locator(cc.COMPOSER_SELECTOR, count=1)
    composer = cc.find_composer(page, timeout_ms=5)
    assert composer.selector == cc.COMPOSER_SELECTOR
    assert composer.count() == 1
    waits = [c for c in page.calls if c[2] == "wait_for"]
    # The wait is on the first visible match (2026-10-01: a chat page's first
    # match is a hidden one); the strict locator is what comes back.
    assert waits and waits[0][1] == VISIBLE_COMPOSER
    assert waits[0][4] == {"state": "visible", "timeout": 5}


def test_find_composer_waits_out_a_chat_page_s_second_composer(monkeypatch) -> None:
    """2026-10-01, through the dedicated window: a chat page matched two
    composers for a moment ("strict mode violation ... resolved to 2
    elements") and one eight seconds later. The wait is on the first match;
    the strict locator is returned once only one is left."""
    monkeypatch.setattr(cc, "COMPOSER_SETTLE_SECONDS", 5.0)
    page = fp.Page()
    page.set_locator(cc.COMPOSER_SELECTOR, count=fp.sequence(2, 2, 1))
    composer = cc.find_composer(page, timeout_ms=5)
    assert composer.selector == cc.COMPOSER_SELECTOR
    assert composer.count() == 1
    waits = [c for c in page.calls if c[2] == "wait_for"]
    assert waits[0][1] == VISIBLE_COMPOSER


def test_find_composer_settles_for_the_visible_composer_when_two_stay(monkeypatch):
    monkeypatch.setattr(cc, "COMPOSER_SETTLE_SECONDS", 0.05)
    page = fp.Page()
    page.set_locator(cc.COMPOSER_SELECTOR, count=2)
    page.set_locator(VISIBLE_COMPOSER, count=1)
    assert cc.find_composer(page, timeout_ms=5).selector == VISIBLE_COMPOSER
    page.set_locator(VISIBLE_COMPOSER, count=2)
    first = cc.find_composer(page, timeout_ms=5)
    assert first.selector == VISIBLE_COMPOSER
    assert first._index == 0  # the first visible one drives the send


def test_find_composer_on_a_chat_page_names_the_chat_not_the_window(monkeypatch):
    """2026-10-01 13:38: a chat page whose composer never rendered was taken
    for a refused window and started the cool-down for everyone. On a /c/
    page the reason is the chat's."""
    fp.install(monkeypatch)
    page = fp.Page()
    page.url = "https://chatgpt.com/g/g-p-x-s/c/6abdd50c-d21c-83ec-82d4-a5c8e4580113"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    page.set_locator(cc.LOGGED_IN_SELECTOR, count=14)
    with pytest.raises(cc.TransportError, match="this chat's composer never rendered"):
        cc.find_composer(page, timeout_ms=5)


def test_find_composer_raises_the_send_s_logged_out_error_on_a_timeout(
    monkeypatch,
) -> None:
    fp.install(monkeypatch)
    page = fp.Page()
    page.url = "https://chatgpt.com/"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        cc.find_composer(page, timeout_ms=5)


def test_find_composer_names_the_cloudflare_challenge_when_its_widget_is_up(
    monkeypatch,
) -> None:
    """The three-day ALERT of 2026-09-29 to 2026-10-01 read "logged out or
    challenged" while the page was Cloudflare's "Verify you are human"
    interstitial; the message now says which, and names the profile as
    the thing to change, never the challenge."""
    fp.install(monkeypatch)
    page = fp.Page()
    page.url = "https://chatgpt.com/"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    page.set_locator(cc.CHALLENGE_SELECTOR, count=1)
    with pytest.raises(cc.TransportError, match="Verify you are human") as excinfo:
        cc.find_composer(page, timeout_ms=5)
    assert cc.CHALLENGE_REASON in str(excinfo.value)
    assert "not the account" in str(excinfo.value)
    assert "logged out" not in str(excinfo.value)


def test_find_composer_names_the_refused_requests_on_a_logged_in_page(monkeypatch):
    """2026-10-01 10:33: the sidebar listed conversations, the main pane
    stayed blank, no challenge widget; the page's own API requests were
    refused. Neither "logged out" nor "challenged" was true."""
    fp.install(monkeypatch)
    page = fp.Page()
    page.url = "https://chatgpt.com/g/g-p-x/project"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    page.set_locator(cc.LOGGED_IN_SELECTOR, count=14)
    with pytest.raises(cc.TransportError, match="requests are being refused") as exc:
        cc.find_composer(page, timeout_ms=5)
    assert cc.REFUSED_REASON in str(exc.value)
    assert "logged out" not in str(exc.value)
    assert cc.page_logged_in(page) is True
    assert cc.page_logged_in(fp.Page()) is False


def test_challenge_page_reads_the_widget_or_the_notice_and_survives_a_dead_page():
    assert cc.challenge_page(fp.Page()) is False
    by_notice = fp.Page()
    by_notice.set_locator(fp.key_for_text(cc.CHALLENGE_TEXT_RE), count=1)
    assert cc.challenge_page(by_notice) is True
    by_widget = fp.Page()
    by_widget.set_locator(cc.CHALLENGE_SELECTOR, count=1)
    assert cc.challenge_page(by_widget) is True

    def gone() -> int:
        raise RuntimeError("target closed")

    dead = fp.Page()
    dead.set_locator(cc.CHALLENGE_SELECTOR, count=gone)
    dead.set_locator(fp.key_for_text(cc.CHALLENGE_TEXT_RE), count=gone)
    assert cc.challenge_page(dead) is False


def test_chat_load_failure_reads_the_notice_and_is_empty_without_it() -> None:
    page = fp.Page()
    key = fp.key_for_text(cc.CHAT_LOAD_FAILURE_RE)
    page.set_locator(key, count=1, texts="Could not load this\nChatGPT conversation.")
    assert cc.chat_load_failure(page) == "Could not load this ChatGPT conversation."
    assert cc.chat_load_failure(fp.Page()) == ""


def test_chat_load_failure_survives_an_unreadable_notice() -> None:
    page = fp.Page()
    key = fp.key_for_text(cc.CHAT_LOAD_FAILURE_RE)
    page.set_locator(key, count=1, raises={"inner_text": RuntimeError("detached")})
    assert cc.chat_load_failure(page) == ""


def test_composer_state_has_a_fixed_shape_whatever_the_page_returned() -> None:
    page = fp.Page()
    page.set_evaluate_result(
        "remove_labels",
        {
            "remove_labels": ["Remove file a.pdf", 3],
            "send_exists": 1,
            "send_enabled": 0,
        },
    )
    assert cc.composer_state(page) == {
        "remove_labels": ["Remove file a.pdf", "3"],
        "send_exists": True,
        "send_enabled": False,
    }
    assert cc.composer_state(fp.Page()) == {
        "remove_labels": [],
        "send_exists": False,
        "send_enabled": False,
    }


@pytest.mark.parametrize(
    ("expected", "actual", "taken"),
    [
        ("hello world", "hello\nworld", True),
        ("hello world", "hello\u00a0 world ", True),
        ("", "anything at all", True),
        ("abc" * 100, "abc" * 89, False),
        ("abc" * 100, "abc" * 90, True),
        ("hello world", "hello there", False),
        ("hello", "", False),
        ("x" * 300 + "tail", "x" * 300 + "other", True),
    ],
)
def test_text_taken_ignores_whitespace_and_needs_prefix_and_length(
    expected, actual, taken
) -> None:
    assert cc.text_taken(expected, actual) is taken


# ---------------------------------------------------------------------------
# BrowserSender.snapshot_page over the fake page
# ---------------------------------------------------------------------------


@pytest.fixture
def sender(monkeypatch):
    fp.install(monkeypatch)
    made = cc.BrowserSender()
    made.page = fp.Page()
    made.page.url = "https://chatgpt.com/"
    made.page.set_evaluate_result("page_snapshot", json.loads(json.dumps(RAW_SNAPSHOT)))
    yield made
    made._owner.shutdown(wait=True)


def test_snapshot_page_writes_the_document_and_the_facts(sender, tmp_path) -> None:
    facts = sender.snapshot_page(tmp_path / "dom")
    assert facts["browser"] == "chrome"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", facts["recorded_at"])
    html = (tmp_path / "dom" / "composer.html").read_text(encoding="utf-8")
    record = json.loads(
        (tmp_path / "dom" / "composer.json").read_text(encoding="utf-8")
    )
    assert "<form" in html and "the user typed" not in html
    assert "html" not in record
    assert record["composer"]["matched"] == facts["composer"]["matched"]
    assert record["recorded_at"] == facts["recorded_at"]
    # The window was already on a page: nothing was navigated.
    assert sender.page.last_goto_url is None


def test_snapshot_page_loads_the_new_chat_page_when_the_window_is_blank(
    sender, tmp_path
) -> None:
    sender.page.url = "about:blank"
    sender.page.set_locator(cc.COMPOSER_SELECTOR, count=1)
    sender.snapshot_page(tmp_path)
    assert sender.page.last_goto_url == "https://chatgpt.com/"


def test_snapshot_page_loads_a_chat_when_one_is_named(sender, tmp_path) -> None:
    sender.page.set_locator(cc.COMPOSER_SELECTOR, count=1)
    sender.page.set_locator(cc.CHAT_TURN_SELECTOR, count=2)
    sender.snapshot_page(tmp_path, "conversation", chat=REAL_ID)
    assert sender.page.last_goto_url == f"https://chatgpt.com/c/{REAL_ID}"
    assert (tmp_path / "conversation.html").is_file()
    assert (tmp_path / "conversation.json").is_file()


def test_snapshot_page_wraps_an_unexpected_failure_as_a_transport_error(
    sender, tmp_path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(cc.TransportError, match="page snapshot failed"):
        sender.snapshot_page(blocker / "dom")


def test_snapshot_page_reraises_a_transport_error_unchanged(
    sender, tmp_path, monkeypatch
):
    fp.install(monkeypatch)
    sender.page.url = "about:blank"
    sender.page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )
    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        sender.snapshot_page(tmp_path)


def test_the_selector_unions_are_the_joined_alternatives() -> None:
    """The tuples are the single source; the joined strings the client has
    always used must stay byte-identical to what a send matched before."""
    assert cc.COMPOSER_SELECTOR == (
        '#prompt-textarea, div.ProseMirror[contenteditable="true"][role="textbox"]'
    )
    assert cc.USER_TURN_SELECTOR == (
        '[data-message-author-role="user"], [data-user-message-bubble="true"]'
    )
    assert cc.CHAT_TURN_SELECTOR == (
        '[data-message-author-role], [data-user-message-bubble="true"], '
        'article[data-testid^="conversation-turn"]'
    )


def test_sanitize_drops_the_at_sign_class_tokens_the_hygiene_rule_refuses() -> None:
    """The Chat/Work toggle's buttons (2026-10-01) carry Tailwind
    container-query classes such as ``@max-xs/home-mode-toggle:px-2.5``;
    the first promotion of a page with the toggle was refused for the "@"
    (``tests/test_fixture_hygiene.py``). The token goes, the rest stays."""
    out = cc.sanitize_html(
        '<button class="rounded-full @max-xs/home-mode-toggle:px-2.5 px-10"'
        ' aria-pressed="true">Chat</button>'
    )
    stand_in = '<br data-snapshot-text="">'
    assert out == (
        f'<button class="rounded-full px-10" aria-pressed="true">{stand_in}</button>'
    )
    assert "@" not in out
    assert cc.sanitize_html('<div class="@container"></div>') == '<div class=""></div>'


def test_sanitize_keeps_the_accept_attribute_a_selector_reads() -> None:
    """UPLOAD_INPUT_FALLBACK_SELECTOR is ``input[type="file"]:not([accept*="image"])``;
    with ``accept`` dropped, every file input on a recorded page matched it
    (tier R, 2026-09-28)."""
    out = cc.sanitize_html(
        '<input type="file" accept="image/*" class="hidden">'
        '<input type="file" class="hidden">'
    )
    assert out == (
        '<input type="file" accept="image/*" class="hidden">'
        '<input type="file" class="hidden">'
    )
