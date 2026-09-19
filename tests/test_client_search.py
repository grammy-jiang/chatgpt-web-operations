"""Tests for Stage 3 item 2 (B2): ``BrowserSender(search=...)`` /
``_enable_search`` and ``BrowserSender(record_send_body=...)`` /
``_record_request`` (ROADMAP.md, Stage 3, "Decisions of 2026-09-20" #4).

Nothing here touches a real browser: ``fake_playwright.install()`` puts a
fake ``playwright`` / ``playwright.sync_api`` module pair in ``sys.modules``,
exactly as ``tests/test_client_browser.py`` does. The ``fake_pw`` and
``enter_env`` fixtures, and the small helpers around them, are duplicated
from that file (and from ``tests/test_client_model.py``, which duplicates
them the same way) rather than shared, since nothing outside this file may
be edited to add a fixture.

Every test names the failure or behaviour it defends, matching
``test_commands.py``.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REAL_URL = f"https://chatgpt.com/c/{REAL_ID}"
USER_TURN_SELECTOR = '[data-message-author-role="user"]'
SEARCH_ITEM_KEY = fp.key_for_text(cc.BrowserSender.SEARCH_ITEM_TEXT)


# ---------------------------------------------------------------------------
# fixtures and small shared helpers (duplicated, see module docstring)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_pw(monkeypatch):
    """Every test gets a fake playwright.sync_api; nothing real ever starts."""
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


@pytest.fixture
def enter_env(monkeypatch, tmp_path):
    """Everything BrowserSender.__enter__/_open touch besides Playwright."""
    monkeypatch.setattr(cc, "virtual_display", _noop_cm)
    monkeypatch.setattr(cc, "browser_slot", _noop_cm)
    monkeypatch.setattr(cc, "new_chat_lock", _noop_cm)
    monkeypatch.setattr(cc, "available_mb", lambda: 9999.0)
    monkeypatch.setattr(cc, "PROFILE_DIR", tmp_path / "profile")

    fake_cs = types.SimpleNamespace(pick_browser=lambda choice: "chrome")
    monkeypatch.setattr(cc, "_helpers", lambda: fake_cs)

    cookies_mod = types.ModuleType("chatgpt_cookies")
    cookies_mod._rp_tolerant = True  # short-circuits _patch_cookie_export
    cookies_mod.canned = [_session_cookie()]
    cookies_mod.export = lambda browser: cookies_mod.canned
    monkeypatch.setitem(sys.modules, "chatgpt_cookies", cookies_mod)

    return types.SimpleNamespace(cs=fake_cs, cookies=cookies_mod)


def _noop_cm(*a, **kw):
    return _NoopCM()


class _NoopCM:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _session_cookie(value: str = "tok") -> dict:
    return {
        "name": "__Secure-next-auth.session-token.0",
        "value": value,
        "domain": "chatgpt.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }


def _calls(page: fp.Page, method: str, selector: str | None = None) -> list[tuple]:
    return [
        c
        for c in page.calls
        if c[0] == "locator"
        and c[2] == method
        and (selector is None or c[1] == selector)
    ]


def _wire_click_send_success(page: fp.Page) -> None:
    page.set_locator(
        cc.BrowserSender.SEND_BUTTONS[0], count=1, visible=True, enabled=True
    )


def _wire_post_confirmed(page: fp.Page) -> None:
    """The user-turn count reads 0 once (the baseline) then 1 forever."""
    page.set_locator(USER_TURN_SELECTOR, count=fp.sequence(0, 1))


def _wire_web_search_visible(page: fp.Page) -> None:
    page.set_locator(SEARCH_ITEM_KEY, count=1, visible=True)


# ---------------------------------------------------------------------------
# _enable_search -- the "+" menu, its retry, and its failure
# ---------------------------------------------------------------------------


def test_enable_search_clicks_plus_then_the_web_search_item_on_the_first_try(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    _wire_web_search_visible(page)

    sender._enable_search(page)

    plus_clicks = _calls(page, "click", cc.BrowserSender.PLUS_BUTTON)
    item_clicks = _calls(page, "click", SEARCH_ITEM_KEY)
    assert len(plus_clicks) == 1
    assert len(item_clicks) == 1
    # the "+" click precedes the item click, in page.calls order
    all_clicks = [c for c in page.calls if c[0] == "locator" and c[2] == "click"]
    assert [c[1] for c in all_clicks] == [cc.BrowserSender.PLUS_BUTTON, SEARCH_ITEM_KEY]


def test_enable_search_retries_the_plus_click_once_when_the_popup_does_not_open(
    make_sender,
) -> None:
    """UI fact (2026-09-20): the first click right after a page load was
    swallowed twice; a second click opened it. count() reads 0 (not open)
    on the first check, then 1 (open) on the second."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(SEARCH_ITEM_KEY, count=fp.sequence(0, 1), visible=True)

    sender._enable_search(page)

    assert len(_calls(page, "click", cc.BrowserSender.PLUS_BUTTON)) == 2
    assert len(_calls(page, "click", SEARCH_ITEM_KEY)) == 1


def test_enable_search_raises_naming_the_step_after_two_failed_tries(
    make_sender,
) -> None:
    """references/endpoint-discovery.md, "Seen on 2026-09-20": the popup
    carries no role="menu", so "open" is judged only by the "Web search"
    text; when that text never appears, _enable_search must give up with a
    clear error rather than hang, and never click an item that was never
    found."""
    sender = make_sender()
    page = fp.Page()
    # SEARCH_ITEM_KEY left unconfigured: count() stays 0 forever.

    with pytest.raises(RuntimeError, match="_enable_search") as excinfo:
        sender._enable_search(page)

    assert "Web search" in str(excinfo.value)
    assert len(_calls(page, "click", cc.BrowserSender.PLUS_BUTTON)) == 2
    assert _calls(page, "click", SEARCH_ITEM_KEY) == []


# ---------------------------------------------------------------------------
# _send -- search=True enables it before the fill; search=False never touches it
# ---------------------------------------------------------------------------


def test_send_enables_search_before_filling_the_composer(make_sender) -> None:
    sender = make_sender(search=True)
    page = fp.Page()
    sender.page = page
    _wire_web_search_visible(page)
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert len(_calls(page, "click", cc.BrowserSender.PLUS_BUTTON)) == 1
    assert len(_calls(page, "click", SEARCH_ITEM_KEY)) == 1
    # "Web search" is clicked before the composer is filled.
    item_click_index = next(
        i
        for i, c in enumerate(page.calls)
        if c[0] == "locator" and c[2] == "click" and c[1] == SEARCH_ITEM_KEY
    )
    fill_index = next(
        i
        for i, c in enumerate(page.calls)
        if c[0] == "locator" and c[2] == "fill" and c[1] == "#prompt-textarea"
    )
    assert item_click_index < fill_index


def test_send_never_touches_the_plus_button_when_search_is_false(make_sender) -> None:
    """search defaults to False; a send must never open the "+" menu at
    all, not even to check it."""
    sender = make_sender()  # search=False
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert _calls(page, "click", cc.BrowserSender.PLUS_BUTTON) == []
    assert _calls(page, "click", SEARCH_ITEM_KEY) == []


def test_send_wraps_an_enable_search_failure_as_a_failed_send(make_sender) -> None:
    """_enable_search's RuntimeError is not a TransportError; send()'s
    existing generic exception handling must still turn it into one
    (SKILL.md-style contract: the caller never sees a bare RuntimeError)."""
    sender = make_sender(search=True)
    page = fp.Page()
    sender.page = page
    # SEARCH_ITEM_KEY left unconfigured: _enable_search raises.

    with pytest.raises(cc.TransportError) as excinfo:
        sender.send("hello")

    assert excinfo.value.status == 0
    assert "browser send failed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# record_send_body -- the request listener attached in _open
# ---------------------------------------------------------------------------


def test_record_send_body_writes_the_f_conversation_post_body(
    enter_env, fake_pw, tmp_path
) -> None:
    # A nested, not-yet-existing directory: _record_request must create it.
    target = tmp_path / "sub" / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation",
            method="POST",
            post_data='{"system_hints": ["search"]}',
        )

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc == {
            "method": "POST",
            "url": "https://chatgpt.com/backend-api/f/conversation",
            "post_data": '{"system_hints": ["search"]}',
        }
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_ignores_the_prepare_handshake(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation/prepare",
            method="POST",
            post_data="irrelevant",
        )

        assert not target.exists()
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_ignores_gets(enter_env, fake_pw, tmp_path) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation", method="GET"
        )

        assert not target.exists()
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_off_by_default_attaches_no_listener(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender()  # record_send_body=""
    try:
        sender._open()

        assert sender.page._event_handlers.get("request", []) == []
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)
