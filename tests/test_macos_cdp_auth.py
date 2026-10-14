"""Mac-only localhost CDP cookie bridge must remain fail-closed and private."""

from __future__ import annotations

import sys

import pytest
from macos_cdp_auth import _eligible, endpoint


@pytest.mark.parametrize("raw", ["", "auto", "off"])
def test_missing_explicit_local_cdp_is_not_a_fallback(monkeypatch, raw: str) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("RP_BROWSER_CDP", raw)
    assert endpoint() is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://127.0.0.1:9233",
        "http://192.168.50.197:9233",
        "http://chatgpt.com:9233",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://someone@127.0.0.1:9233",
        "http://127.0.0.1:9233/path",
        "http://127.0.0.1:9233/?q=1",
        "http://127.0.0.1:9233/#fragment",
        "file:///tmp/user-cookie-file",
    ],
)
def test_external_or_ambiguous_cdp_rejected(monkeypatch, raw: str) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("RP_BROWSER_CDP", raw)
    with pytest.raises(ValueError):
        endpoint()


@pytest.mark.parametrize(
    "raw",
    ["http://127.0.0.1:9233", "http://localhost:9233", "http://[::1]:9233"],
)
def test_explicit_loopback_allowed(monkeypatch, raw: str) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("RP_BROWSER_CDP", raw)
    assert endpoint() == raw


def test_non_mac_never_uses_cdp_credentials(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("RP_BROWSER_CDP", "http://127.0.0.1:9233")
    assert endpoint() is None


def test_only_chatgpt_request_scoped_cookie_values_eligible() -> None:
    assert _eligible({"domain": ".chatgpt.com", "name": "session", "value": "dummy"})
    assert _eligible({"domain": "chatgpt.com", "name": "session", "value": "dummy"})
    for cookie in [
        {"domain": ".other.example", "name": "session", "value": "dummy"},
        {"domain": ".openai.com", "name": "session", "value": "dummy"},
        {"domain": ".chatgpt.com", "name": "", "value": "dummy"},
        {"domain": ".chatgpt.com", "name": "session", "value": 10},
        {"domain": ".chatgpt.com", "name": "long", "value": "x" * 16385},
    ]:
        assert not _eligible(cookie)


# macOS production CDP cookie path (no filesystem, no network, no credentials).
class _CookieContext:
    def __init__(self, cookie_rows):
        self.cookie_rows = cookie_rows
        self.urls = []

    def cookies(self, urls):
        self.urls = urls
        return self.cookie_rows


class _CookieBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


class _MacCDPManager:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        return self.fake

    def __exit__(self, *_):
        return False


def _install_fake_cdp(monkeypatch, fake_browser=None, *, error=None):
    import sys
    from types import ModuleType

    import macos_cdp_auth as mod

    seen = []

    class FakeChromium:
        def connect_over_cdp(self, endpoint_url, timeout):
            seen.append((endpoint_url, timeout))
            if error is not None:
                raise error
            return fake_browser

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(mod, "endpoint", lambda: "http://127.0.0.1:9233")
    # T0 forbids even localhost sockets. Avoid importing the real Playwright
    # package while that guard is active: its asyncio/ssl import needs socket.
    fake_module = ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _MacCDPManager(FakePlaywright())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return seen


def test_mac_browser_cookies_are_selected_scoped_sorted_and_close(monkeypatch):
    from macos_cdp_auth import browser_cookies

    rows = [
        {"domain": ".not-chatgpt.example", "name": "ignore", "value": "discard"},
        {"domain": ".chatgpt.com", "name": "zzz", "value": "fixture-three"},
        {
            "domain": ".chatgpt.com",
            "name": "__Secure-next-auth.session-token.0",
            "value": "fixture-token",
        },
        {"domain": "chatgpt.com", "name": "aaa", "value": "fixture-one"},
    ]
    context = _CookieContext(rows)
    browser = _CookieBrowser([context])
    seen = _install_fake_cdp(monkeypatch, browser)
    values = browser_cookies()
    assert seen == [("http://127.0.0.1:9233", 12000)]
    assert context.urls == ["https://chatgpt.com/"]
    assert [x["name"] for x in values] == [
        "__Secure-next-auth.session-token.0",
        "aaa",
        "zzz",
    ]
    assert browser.closed is True
    assert all(c["domain"] in (".chatgpt.com", "chatgpt.com") for c in values)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"domain": ".chatgpt.com", "name": "analytics", "value": "dummy"}],
        [
            {
                "domain": ".elsewhere.example",
                "name": "__Secure-next-auth.session-token",
                "value": "dummy",
            }
        ],
    ],
)
def test_mac_browser_cookies_without_chat_session_fail_closed(monkeypatch, rows):
    from macos_cdp_auth import browser_cookies

    browser = _CookieBrowser([_CookieContext(rows)])
    _install_fake_cdp(monkeypatch, browser)
    with pytest.raises(RuntimeError, match="not logged in"):
        browser_cookies()
    assert browser.closed is True


@pytest.mark.parametrize("contexts", [[], [_CookieContext([]), _CookieContext([])]])
def test_mac_browser_requires_one_dedicated_context(monkeypatch, contexts):
    from macos_cdp_auth import browser_cookies

    browser = _CookieBrowser(contexts)
    _install_fake_cdp(monkeypatch, browser)
    with pytest.raises(RuntimeError, match="one dedicated"):
        browser_cookies()
    assert browser.closed is True


def test_mac_browser_refuses_missing_local_cdp_even_when_chrome_library_exists(
    monkeypatch,
):
    import macos_cdp_auth as mod

    monkeypatch.setattr(mod, "endpoint", lambda: None)
    with pytest.raises(RuntimeError, match="explicit|required"):
        mod.browser_cookies()


def test_mac_browser_connector_failure_does_not_swallow_error(monkeypatch):
    from macos_cdp_auth import browser_cookies

    calls = _install_fake_cdp(
        monkeypatch, error=ConnectionRefusedError("synthetic CDP failure")
    )
    with pytest.raises(ConnectionRefusedError):
        browser_cookies()
    assert calls == [("http://127.0.0.1:9233", 12000)]


def test_mac_cookie_filter_rejects_wrong_record_types():
    import macos_cdp_auth as mod

    for raw in (
        {},
        {"domain": 20, "name": "x", "value": "y"},
        {"domain": "chatgpt.com", "name": 40, "value": "y"},
        {"domain": ".chatgpt.com", "name": "z" * 257, "value": "y"},
        {"domain": ".chatgpt.com", "name": "z", "value": None},
        {"domain": ".chatgpt.com", "name": "z", "value": "y" * 16385},
    ):
        assert mod._eligible(raw) is False
