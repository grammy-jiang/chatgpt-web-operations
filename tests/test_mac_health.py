"""T0 macOS health gate: real CDP shapes, no socket or credential material."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import health
import probe_cookies
import pytest


class _FakeClient:
    MIN_AVAILABLE_MB = 4000
    MAX_BROWSERS = 2
    CDP_MODE = "http://127.0.0.1:9233"
    DEDICATED_URL = "http://127.0.0.1:9233"
    os = os

    @staticmethod
    def available_mb() -> float:
        return 16_000

    @staticmethod
    def scripted_browser_pids() -> list[int]:
        return []

    @staticmethod
    def dedicated_browser_version(_url: str):
        return {"Browser": "Chrome/155.0.8059.80"}


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setenv("CHATGPT_MACOS_CDP_MODE", "1")
    monkeypatch.setattr(
        os,
        "getloadavg",
        lambda: (1.0, 1.0, 1.0),
    )


def test_mac_runtime_does_not_require_linux_xvfb_dbus_or_keyring(mac):
    rows = health.mac_host_checks(_FakeClient)
    assert [r["name"] for r in rows] == [
        "available memory",
        "load average",
        "in-flight browsers",
        "dedicated browser",
    ]
    assert all(r["state"] == "ok" for r in rows)
    assert all("keyring" not in r["name"] for r in rows)


def test_mac_cdp_true_version_shape_counts_as_connected(mac, monkeypatch):
    import macos_cdp_auth

    monkeypatch.setattr(macos_cdp_auth, "endpoint", lambda: "http://127.0.0.1:9233")
    [result] = health.mac_link_checks(_FakeClient)
    assert result["state"] == "ok"
    assert "CDP" in result["name"]
    assert "ws://" not in result["detail"]


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"Browser": "other/155"},
        {"Browser": None},
        {"Browser": 155},
        {"Browser": "Chrome Canary/155"},
        "",
        None,
        12,
        ["Chrome/155"],
    ],
)
def test_mac_cdp_invalid_version_is_blocked(mac, monkeypatch, reply):
    import macos_cdp_auth

    monkeypatch.setattr(macos_cdp_auth, "endpoint", lambda: "http://127.0.0.1:9233")
    fake = SimpleNamespace(dedicated_browser_version=lambda *_: reply)
    [result] = health.mac_link_checks(fake)
    assert result["state"] == "block"
    assert "unreachable" in result["detail"]


@pytest.mark.parametrize("error", [OSError("secret"), RuntimeError("secret")])
def test_mac_cdp_failed_probe_blocks_without_logging_exception(
    mac,
    monkeypatch,
    error,
):
    import macos_cdp_auth

    monkeypatch.setattr(macos_cdp_auth, "endpoint", lambda: "http://127.0.0.1:9233")

    def fail(*_):
        raise error

    [result] = health.mac_link_checks(SimpleNamespace(dedicated_browser_version=fail))
    assert result["state"] == "block"
    assert "secret" not in result["detail"]


def test_mac_session_expiry_reads_browser_only_not_linux_cookie_db(mac, monkeypatch):
    valid_until = time.time() + 30 * 86400
    exp = int((valid_until + probe_cookies.WEBKIT_EPOCH_DELTA_S) * 1_000_000)
    seen = []

    def fake_rows():
        seen.append("mac_cdp")
        return [("__Secure-next-auth.session-token.0", ".chatgpt.com", b"", exp)]

    monkeypatch.setattr(health.probe_cookies, "mac_cdp_cookie_rows", fake_rows)
    client = SimpleNamespace(_helpers=lambda: SimpleNamespace(os=os))
    browser_horizon, keyring_horizon = health._read_session_horizon(client, "chrome")
    assert seen == ["mac_cdp"]
    assert browser_horizon is not None
    assert 29 < browser_horizon[1] <= 31
    assert keyring_horizon is None


def test_mac_cookie_probe_failure_blocks_and_masks_sensitive_message(mac, monkeypatch):
    def fail():
        raise RuntimeError("TEST_DO_NOT_LEAK_AUTH_TOKEN")

    monkeypatch.setattr(health.probe_cookies, "mac_cdp_cookie_rows", fail)
    client = SimpleNamespace(
        _helpers=lambda: SimpleNamespace(os=os),
        os=os,
    )
    result = health.fetch_session_token(client, "chrome")
    assert result["state"] == "block"
    assert "RuntimeError" in result["detail"]
    assert "TEST_DO_NOT_LEAK_AUTH_TOKEN" not in str(result)


def test_mac_cookie_wrong_browser_blocks(mac, monkeypatch):
    import macos_cdp_auth

    monkeypatch.setattr(macos_cdp_auth, "endpoint", lambda: "http://127.0.0.1:9233")
    client = SimpleNamespace(
        _helpers=lambda: SimpleNamespace(os=os),
        os=os,
    )
    outcome = health.fetch_session_token(client, "firefox")
    assert outcome["state"] == "block"
    assert "ValueError" in outcome["detail"]
