"""T0 tests for how a send decides that its message reached ChatGPT
(``chatgpt_client.post_evidence``; atlas F-2026-09-29-10).

Until 2026-09-29 the send counted user turns on the page and nothing else.
The events file of 2026-09-27/28 held 42 "not-posted" events, and 40 of
them were messages the server had stored: 30 new chats whose address was
already a real ``/c/<id>`` while the count read 0, and 10 follow-ups in
long chats where the count stayed level or fell. The callers sent again.
These tests drive ``BrowserSender._send`` on the fake page in exactly
those shapes, and in the shapes where the answer must stay "not posted".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import chatgpt_client as cc
import fake_playwright as fp
import pytest
from test_client_browser import REAL_ID, REAL_URL, _page_calls, _wire_click_send_success

PROJECT_PAGE = "https://chatgpt.com/g/g-p-" + "1" * 32 + "/project"
PROJECT_CHAT_URL = f"https://chatgpt.com/g/g-p-{'1' * 32}-workers/c/{REAL_ID}"
SEND_URL = "https://chatgpt.com/backend-api/f/conversation"
PREPARE_URL = SEND_URL + "/prepare"


@pytest.fixture(autouse=True)
def fake_pw(monkeypatch):
    return fp.install(monkeypatch)


@pytest.fixture
def make_sender():
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


def _clicks(page: fp.Page) -> int:
    button = cc.BrowserSender.SEND_BUTTONS[0]
    return sum(
        1
        for c in page.calls
        if c[0] == "locator" and c[1] == button and c[2] == "click"
    )


def _url_after_click(page: fp.Page, before: str, after: str):
    """The page's address: ``before`` until the send button was clicked
    ``n`` times for the ``n``-th send, then ``after``."""
    return lambda: after if _clicks(page) else before


def _answer_after_click(page: fp.Page, *answers: tuple[str, int]) -> None:
    """After the first click of each send, the page's POSTs go out and are
    answered, once, on the next wait: the order a real page shows them."""
    original = page.wait_for_timeout
    answered = {"clicks": 0}

    def wait(ms: int) -> None:
        original(ms)
        clicks = _clicks(page)
        if clicks > answered["clicks"]:
            answered["clicks"] = clicks
            for url, status in answers:
                page.emit_request(url, method="POST")
                page.emit_response(url, status=status, method="POST")

    page.wait_for_timeout = wait  # type: ignore[method-assign]


def _enter_presses(page: fp.Page) -> int:
    return [p[2] for p in _page_calls(page, "keyboard.press")].count(("Enter",))


# ---------------------------------------------------------------------------
# the pure verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"statuses": [200]}, ("request",)),
        ({"statuses": [201, 403]}, ("request",)),
        ({"statuses": [403, 429]}, ()),
        ({"new_chat": True, "url": REAL_URL}, ("url",)),
        ({"new_chat": False, "url": REAL_URL}, ()),
        ({"new_chat": True, "url": f"https://chatgpt.com/c/WEB:{REAL_ID}"}, ()),
        ({"new_chat": True, "url": PROJECT_PAGE}, ()),
        ({"turns_now": 1}, ("turn",)),
        (
            {"statuses": [200], "new_chat": True, "url": REAL_URL, "turns_now": 1},
            ("request", "url", "turn"),
        ),
    ],
)
def test_post_evidence(kw, expected) -> None:
    args = {
        "new_chat": False,
        "url": "https://chatgpt.com/",
        "turns_before": 0,
        "turns_now": 0,
        "statuses": [],
        **kw,
    }
    assert cc.post_evidence(**args) == expected


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("POST", SEND_URL, True),
        ("POST", SEND_URL + "?x=1#y", True),
        ("POST", PREPARE_URL, False),
        ("GET", SEND_URL, False),
        ("POST", "https://chatgpt.com/backend-api/conversation", False),
    ],
)
def test_is_send_post(method, url, expected) -> None:
    assert cc.BrowserSender.is_send_post(method, url) is expected


# ---------------------------------------------------------------------------
# the two shapes the events file recorded
# ---------------------------------------------------------------------------


def test_a_new_chat_whose_address_became_real_is_posted_with_no_turn_counted(
    make_sender,
) -> None:
    """The ThreadDeck shape (30 events): composed on a project page, the
    address became /g/g-p-.../c/<id>, the turn count read 0 throughout.
    Before the fix: "not posted", and the caller sent the prompt again."""
    sender = make_sender(project="g-p-" + "1" * 32)
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.set_locator(cc.USER_TURN_SELECTOR, count=0)
    page.url = _url_after_click(page, PROJECT_PAGE, PROJECT_CHAT_URL)

    assert sender._send("a long prompt", chat=None, name="task") == REAL_ID

    assert _enter_presses(page) == 0, "a posted message must not be submitted twice"
    events = _events()
    assert [e["kind"] for e in events] == ["post-unseen"]
    assert events[0]["evidence"] == ["url"]
    assert events[0]["screenshot"].endswith("chatgpt-post-unseen.png")


def test_a_follow_up_whose_request_was_answered_is_posted_though_the_count_fell(
    make_sender,
) -> None:
    """The atlas shape (10 events): a follow-up in a long chat; the count
    read 5 before and 4 after, and the page's own POST was answered 200.
    Before the fix: "not posted", and one caller sent the turn again."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.set_locator(cc.USER_TURN_SELECTOR, count=fp.sequence(5, 4))
    _answer_after_click(page, (SEND_URL, 200))

    assert sender._send("a follow-up turn", chat=REAL_ID, name="task") == REAL_ID

    assert _enter_presses(page) == 0
    events = _events()
    assert [e["kind"] for e in events] == ["post-unseen"]
    assert events[0]["evidence"] == ["request"]
    assert events[0]["post_statuses"] == [200]
    assert (events[0]["turns_before"], events[0]["turns_after"]) == (5, 4)


# ---------------------------------------------------------------------------
# where the answer must stay "not posted"
# ---------------------------------------------------------------------------


def test_a_refused_request_is_not_a_post_and_the_event_keeps_its_status(
    make_sender,
) -> None:
    sender = make_sender(project="g-p-" + "1" * 32)
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.url = PROJECT_PAGE
    _answer_after_click(page, (SEND_URL, 403))

    with pytest.raises(cc.TransportError, match=r"^message was not posted.*\[403\]"):
        sender._send("hello", chat=None, name="task")

    assert _enter_presses(page) == 0, "the click did reach the wire"
    (event,) = _events()
    assert event["kind"] == "not-posted"
    assert event["post_requests"] == 1
    assert event["post_statuses"] == [403]


def test_the_prepare_handshake_is_not_the_send(make_sender) -> None:
    """A 200 for .../f/conversation/prepare is the composer's handshake, not
    the message. Nothing else happened, so the send still presses Enter
    once, as before, and then reports "not posted"."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.url = "https://chatgpt.com/"
    _answer_after_click(page, (PREPARE_URL, 200))

    with pytest.raises(cc.TransportError, match="^message was not posted"):
        sender._send("hello", chat=None, name="task")

    assert _enter_presses(page) == 1
    (event,) = _events()
    assert event["post_requests"] == 0
    assert event["post_statuses"] == []


def test_a_follow_up_is_not_posted_by_its_own_address(make_sender) -> None:
    """A follow-up's address is /c/<id> before anything is sent, so the
    address proves nothing there."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.set_locator(cc.USER_TURN_SELECTOR, count=3)

    with pytest.raises(cc.TransportError, match="^message was not posted"):
        sender._send("hello", chat=REAL_ID, name="task")


# ---------------------------------------------------------------------------
# the rest of the contract
# ---------------------------------------------------------------------------


def test_posted_without_an_address_returns_a_provisional_id(make_sender) -> None:
    """The server took the message and the page never showed the new chat's
    address: sending again would post twice, so the caller gets a
    provisional id to resolve from the listing, as for a WEB: address."""
    sender = make_sender(project="g-p-" + "1" * 32)
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    page.url = PROJECT_PAGE
    _answer_after_click(page, (SEND_URL, 200))

    result = sender._send("hello", chat=None, name="task")

    assert cc.is_provisional(result)
    assert result.startswith("WEB:")
    assert [e["kind"] for e in _events()] == ["post-unseen", "posted-no-id"]


def test_a_second_send_on_the_same_page_hooks_it_once_and_starts_clean(
    make_sender,
) -> None:
    """The hooks are added once per page, and the first send's 200 does
    not count for the second: each send reads only what came after its
    own click."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _answer_after_click(page, (SEND_URL, 200))
    assert sender._send("one", chat=REAL_ID, name="task") == REAL_ID

    page.wait_for_timeout = fp.Page.wait_for_timeout.__get__(page)  # no more answers
    with pytest.raises(cc.TransportError, match="^message was not posted"):
        sender._send("two", chat=REAL_ID, name="task")

    hooks = [c for c in page.calls if c[0] == "page" and c[1] == "on"]
    assert [c[2] for c in hooks] == [("request",), ("response",)]


def test_a_hook_never_raises_into_the_page(make_sender) -> None:
    """Playwright calls the hooks from its own dispatch; a malformed request
    object must be ignored, not raised."""
    sender = make_sender()

    class _Broken:
        @property
        def method(self):
            raise RuntimeError("gone")

    sender._note_send_request(_Broken())
    sender._note_send_response(object())
    assert (sender._send_requests, sender._send_statuses) == (0, [])
