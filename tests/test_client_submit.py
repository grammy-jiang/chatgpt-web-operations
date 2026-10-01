"""T0 tests for the send path's submit robustness and its evidence
(TESTING.md section 6, P2; PLAN-2026-09-27.md B5): the fill read-back and
its keyboard fallback, the events file, and the screenshot directory.

The fixtures and wiring helpers are tests/test_client_browser.py's, so a
send here is driven exactly the way that file drives one: a fake page
where the composer, the send button and the user-turn count are declared.
The autouse fixture in tests/conftest.py points RP_EVENTS_FILE and
RP_SCREENSHOT_DIR into tmp_path, so nothing here writes to the state
directory.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402
from test_client_browser import (  # noqa: E402
    REAL_ID,
    REAL_URL,
    _page_calls,
    _wire_click_send_success,
    _wire_post_confirmed,
)


@pytest.fixture(autouse=True)
def fake_pw(monkeypatch):
    """Every test gets a fake playwright.sync_api; nothing real ever starts
    (the same fixture tests/test_client_browser.py uses)."""
    return fp.install(monkeypatch)


@pytest.fixture
def make_sender():
    """BrowserSender instances, with their owner thread pool always freed."""
    created: list[cc.BrowserSender] = []

    def _make(**kw):
        sender = cc.BrowserSender(**kw)
        created.append(sender)
        return sender

    yield _make
    for sender in created:
        sender._owner.shutdown(wait=True)


def _events() -> list[dict]:
    path = Path(os.environ["RP_EVENTS_FILE"])
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _ready_page(sender) -> fp.Page:
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)
    return page


# ---------------------------------------------------------------------------
# the fill read-back and its fallback
# ---------------------------------------------------------------------------


def test_a_fill_that_reads_back_needs_no_fallback_and_records_nothing(make_sender):
    sender = make_sender()
    page = _ready_page(sender)

    assert sender._send("hello", chat=None, name="task") == REAL_ID

    assert not _page_calls(page, "keyboard.insert_text")
    assert _events() == []


def test_a_swallowed_fill_is_retyped_through_the_keyboard_and_recorded(make_sender):
    """The 2026-09-27 shape: fill() returned, the composer held nothing.
    The send clears it, types the text (insert_text), reads it back, and
    goes on; the fallback is one event, not a failure."""
    sender = make_sender()
    page = _ready_page(sender)
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=1,
        swallow_fill=True,
        texts=fp.sequence("", "hello"),
    )

    assert sender._send("hello", chat=None, name="task") == REAL_ID

    inserts = _page_calls(page, "keyboard.insert_text")
    assert [i[2] for i in inserts] == [("hello",)]
    presses = [p[2] for p in _page_calls(page, "keyboard.press")]
    assert ("Control+A",) in presses
    assert ("Delete",) in presses
    assert presses.index(("Delete",)) < len(presses)
    events = _events()
    assert [e["kind"] for e in events] == ["fill-fallback"]
    assert events[0]["expected_chars"] == 5
    assert events[0]["shown_chars"] == 0
    assert events[0]["url"] == REAL_URL


def test_a_composer_that_never_takes_the_text_ends_the_send_with_evidence(
    make_sender,
):
    """Neither fill nor the retype is read back: the send stops before any
    click, with two events and a screenshot under RP_SCREENSHOT_DIR."""
    sender = make_sender()
    page = _ready_page(sender)
    page.set_locator(cc.COMPOSER_SELECTOR, count=1, swallow_fill=True, texts="")

    with pytest.raises(cc.TransportError, match="did not take the text"):
        sender._send("hello", chat=None, name="task")

    clicks = [
        c
        for c in page.calls
        if c[0] == "locator" and c[2] == "click" and c[1] != cc.COMPOSER_SELECTOR
    ]
    assert clicks == [], clicks
    events = _events()
    assert [e["kind"] for e in events] == ["fill-fallback", "text-not-taken"]
    shot = events[1]["screenshot"]
    assert shot.endswith("chatgpt-fill-failed.png")
    assert shot.startswith(os.environ["RP_SCREENSHOT_DIR"])
    assert Path(os.environ["RP_SCREENSHOT_DIR"]).is_dir()


def test_the_attach_path_confirms_the_cover_text_not_the_prompt(make_sender):
    """A prompt above ATTACH_ABOVE_BYTES is uploaded and the composer gets
    the short cover text; the read-back must expect the cover."""
    sender = make_sender()
    page = _ready_page(sender)
    fills: list[str] = []
    original = page.locator(cc.COMPOSER_SELECTOR)._state  # the shared state

    def watch_fill(text, timeout=None, **kw):
        fills.append(text)
        original.texts = text

    page.set_locator(cc.COMPOSER_SELECTOR, count=1)

    def fake_attach(pg, text, name):
        pass

    sender._attach_prompt = fake_attach  # the upload itself is not the subject
    page.locator(cc.COMPOSER_SELECTOR).fill = watch_fill  # type: ignore[method-assign]
    assert sender._send("x" * (sender.ATTACH_ABOVE_BYTES + 1), None, "task") == REAL_ID
    assert _events() == []


# ---------------------------------------------------------------------------
# the other events
# ---------------------------------------------------------------------------


def test_not_posted_records_an_event_with_the_screenshot_path(make_sender):
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    # USER_TURN_SELECTOR left unconfigured: count() stays 0 forever.

    with pytest.raises(cc.TransportError, match="message was not posted"):
        sender._send("hello", chat=None, name="task")

    events = _events()
    assert [e["kind"] for e in events] == ["not-posted"]
    assert events[0]["turns_before"] == 0
    assert events[0]["turns_after"] == 0
    assert events[0]["chars"] == 5
    assert events[0]["screenshot"].endswith("chatgpt-send-fail.png")


HANDSHAKE = "https://chatgpt.com/backend-api/"


def test_a_refused_handshake_with_no_message_request_is_not_a_post(make_sender):
    """2026-10-01, a fresh scripted profile: the click fired the page's own
    handshake (sentinel prepare, init), every answer was 403, no
    f/conversation request followed, and the page still showed the bubble
    at a local-chatgpt: address. The turn count alone must not make that a
    post: the send stops with the handshake named, its own event, and no
    provisional id for a conversation that does not exist
    (F-2026-10-01-23)."""
    sender = make_sender()
    page = _ready_page(sender)
    page.url = fp.sequence(
        "https://chatgpt.com/g/g-p-x/project",
        "https://chatgpt.com/g/g-p-x-s/c/local-chatgpt%3A1db76ec1-0360-4d93-96c4-b692dcd7ce77",
    )

    def refuse() -> None:
        page.emit_response(
            HANDSHAKE + "sentinel/chat-requirements/prepare", status=403, method="POST"
        )
        page.emit_response(HANDSHAKE + "conversation/init", status=403, method="POST")

    page.set_locator(cc.BrowserSender.SEND_BUTTONS[0], raises={"click": refuse})

    with pytest.raises(cc.TransportError, match="handshake was refused") as excinfo:
        sender._send("hello", chat=None, name="task")

    assert "403 on sentinel/chat-requirements/prepare" in str(excinfo.value)
    assert "403 on conversation/init" in str(excinfo.value)
    assert excinfo.value.status == 0
    events = _events()
    assert [e["kind"] for e in events] == ["handshake-refused"]
    assert events[0]["refused"] == [
        "/backend-api/sentinel/chat-requirements/prepare 403",
        "/backend-api/conversation/init 403",
    ]
    assert events[0]["turns_before"] == 0
    assert events[0]["turns_after"] == 1
    assert events[0]["chars"] == 5
    assert events[0]["screenshot"].endswith("chatgpt-send-fail.png")
    assert not _page_calls(page, "keyboard.press")
    left, record = cc.cooldown_remaining()
    assert left > 0
    assert record["kind"] == "handshake-refused"


def test_a_refused_handshake_is_forgotten_once_the_message_request_went_out(
    make_sender,
):
    """The page retries its handshake itself; a 403 followed by the
    message's own POST answered 2xx is a post, judged by that POST."""
    sender = make_sender()
    page = _ready_page(sender)

    def refuse_then_post() -> None:
        page.emit_response(HANDSHAKE + "conversation/init", status=403, method="POST")
        page.emit_request(HANDSHAKE + "f/conversation", method="POST")
        page.emit_response(HANDSHAKE + "f/conversation", status=200, method="POST")

    page.set_locator(
        cc.BrowserSender.SEND_BUTTONS[0], raises={"click": refuse_then_post}
    )

    assert sender._send("hello", chat=None, name="task") == REAL_ID
    assert _events() == []


def test_a_handshake_refused_with_429_is_the_rate_limit(make_sender):
    sender = make_sender()
    page = _ready_page(sender)

    def refuse() -> None:
        page.emit_response(
            HANDSHAKE + "f/conversation/prepare", status=429, method="POST"
        )

    page.set_locator(cc.BrowserSender.SEND_BUTTONS[0], raises={"click": refuse})

    with pytest.raises(cc.TransportError, match="429 on f/conversation/prepare") as exc:
        sender._send("hello", chat=None, name="task")
    assert exc.value.status == 429
    assert [e["kind"] for e in _events()] == ["handshake-refused"]


def test_is_handshake_post_names_the_endpoint_and_nothing_else() -> None:
    bs = cc.BrowserSender
    assert bs.is_handshake_post("POST", HANDSHAKE + "conversation/init?x=1") == (
        "/backend-api/conversation/init"
    )
    assert bs.is_handshake_post("POST", HANDSHAKE + "f/conversation/prepare") == (
        "/backend-api/f/conversation/prepare"
    )
    assert bs.is_handshake_post("POST", HANDSHAKE + "f/conversation") == ""
    assert bs.is_handshake_post("GET", HANDSHAKE + "conversation/init") == ""


def test_a_missing_composer_records_an_event(make_sender):
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.url = "https://chatgpt.com/"
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        count=0,
        raises={"wait_for": fp.PlaywrightTimeoutError("t")},
    )

    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        sender._send("hi", chat=None, name="task")

    events = _events()
    assert [e["kind"] for e in events] == ["composer-missing"]
    assert events[0]["screenshot"].endswith("chatgpt-composer-missing.png")


def test_a_chat_that_never_loads_records_an_event_after_the_retries(
    make_sender, monkeypatch
):
    monkeypatch.setattr(cc, "CHAT_LOAD_RETRIES", 0)
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        fp.key_for_text(cc.CHAT_LOAD_FAILURE_RE),
        count=1,
        texts="Could not load this ChatGPT conversation.",
    )

    with pytest.raises(cc.TransportError, match="did not load"):
        sender._await_chat(REAL_URL)

    events = _events()
    assert [e["kind"] for e in events] == ["chat-load-failed"]
    assert events[0]["chat"] == REAL_ID
    assert events[0]["loads"] == 1
    assert events[0]["notice"].startswith("Could not load")
    assert events[0]["screenshot"].endswith("chatgpt-chat-load-failed.png")


def test_a_generic_browser_failure_records_a_send_error(make_sender, monkeypatch):
    sender = make_sender()
    page = fp.Page()
    sender.page = page

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sender, "_send", boom)
    with pytest.raises(cc.TransportError, match="browser send failed"):
        sender.send("hi")

    events = _events()
    assert [e["kind"] for e in events] == ["send-error"]
    assert events[0]["error"] == "boom"
    assert events[0]["screenshot"].endswith("chatgpt-send-error.png")


# ---------------------------------------------------------------------------
# record_event and the screenshot helper
# ---------------------------------------------------------------------------


def test_record_event_appends_json_lines_and_never_raises(tmp_path, monkeypatch):
    target = tmp_path / "deep" / "events.jsonl"
    monkeypatch.setenv("RP_EVENTS_FILE", str(target))
    assert cc.record_event("not-posted", url="u", n=1) == str(target)
    assert cc.record_event("fill-fallback", where=Path("p")) == str(target)
    lines = [json.loads(line) for line in target.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["not-posted", "fill-fallback"]
    assert lines[0]["url"] == "u" and lines[0]["n"] == 1
    assert lines[1]["where"] == "p"
    for line in lines:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line["ts"])
        assert line["pid"] == os.getpid()

    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("RP_EVENTS_FILE", str(blocker / "events.jsonl"))
    assert cc.record_event("send-error", error="x") == ""


def test_events_file_defaults_to_the_state_directory(monkeypatch):
    monkeypatch.delenv("RP_EVENTS_FILE", raising=False)
    assert cc.events_file() == cc.STATE_DIR / "events.jsonl"


def test_screenshot_dir_comes_from_the_argument_the_environment_or_the_state(
    make_sender, monkeypatch, tmp_path
):
    monkeypatch.setenv("RP_SCREENSHOT_DIR", str(tmp_path / "env"))
    assert make_sender().screenshot_dir == tmp_path / "env"
    assert make_sender(screenshot_dir=str(tmp_path / "arg")).screenshot_dir == (
        tmp_path / "arg"
    )
    monkeypatch.delenv("RP_SCREENSHOT_DIR")
    assert make_sender().screenshot_dir == cc.STATE_DIR / "screenshots"


def test_the_screenshot_helper_returns_the_path_and_swallows_failures(
    make_sender, tmp_path
):
    sender = make_sender(screenshot_dir=str(tmp_path / "shots"))
    page = fp.Page()
    sender.page = page

    path = sender._screenshot("x.png")

    assert path == str(tmp_path / "shots" / "x.png")
    assert (tmp_path / "shots").is_dir()
    assert _page_calls(page, "screenshot")[0][3]["path"] == path

    def no_display(**kw):
        raise RuntimeError("no display")

    page.screenshot = no_display  # type: ignore[method-assign]
    assert sender._screenshot("y.png") == ""


def test_every_event_kind_the_client_records_is_named_in_event_kinds() -> None:
    source = Path(cc.__file__).read_text(encoding="utf-8")
    recorded = set(re.findall(r'record_event\(\s*"([a-z-]+)"', source))
    assert recorded == set(cc.EVENT_KINDS)
