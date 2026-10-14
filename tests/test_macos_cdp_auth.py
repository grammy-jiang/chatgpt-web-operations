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
