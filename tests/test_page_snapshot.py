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
    "html": {
        "composer": RAW_FORM,
        "turns": ["<article data-testid='conversation-turn-1'>hi</article>"],
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
    js, spec = next(c[2] for c in page.calls if c[0] == "page" and c[1] == "evaluate")
    assert "page_snapshot" in js
    assert spec == {
        "composer": list(cc.COMPOSER_SELECTORS),
        "user_turn": list(cc.USER_TURN_SELECTORS),
        "chat_turn": list(cc.CHAT_TURN_SELECTORS),
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
    assert facts["html"] == {"composer": "", "turns": []}
    assert facts["url"] == "https://chatgpt.com/x"


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


def test_snapshot_drift_treats_a_missing_group_as_empty() -> None:
    assert cc.snapshot_drift({}, {}) == []
    drift = cc.snapshot_drift({}, RAW_SNAPSHOT)
    assert "composer.found" in drift and "send_button.testid" in drift
    assert cc.snapshot_drift({"composer": "junk"}, {"composer": None}) == []


# ---------------------------------------------------------------------------
# find_composer, chat_load_failure, composer_state, text_taken
# ---------------------------------------------------------------------------


def test_find_composer_returns_the_locator_once_visible() -> None:
    page = fp.Page()
    page.set_locator(cc.COMPOSER_SELECTOR, count=1)
    composer = cc.find_composer(page, timeout_ms=5)
    assert composer.count() == 1
    waits = [c for c in page.calls if c[2] == "wait_for"]
    assert waits and waits[0][4] == {"state": "visible", "timeout": 5}


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
