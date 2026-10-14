"""T0, no real credentials: macOS cookie expiry diagnostic through local CDP."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import macos_cdp_auth
import probe_cookies
import pytest


def fake_cookie_records():
    return [
        {
            "domain": ".chatgpt.com",
            "name": "__Secure-next-auth.session-token.0",
            "value": "fixture-private-must-never-print",
            "expires": 1810000000.0,
        },
        {
            "domain": "chatgpt.com",
            "name": "cf-fake",
            "value": "private-analytics-must-never-print",
            "expires": -1,
        },
    ]


@pytest.fixture
def mac_fixture(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("CHATGPT_MACOS_CDP_MODE", "1")
    monkeypatch.setattr(macos_cdp_auth, "browser_cookies", fake_cookie_records)
    # The real session transport is independently verified with live T1
    # authentication. This T0 check must never import ssl after the socket guard.
    fake_transport = SimpleNamespace(_helpers=lambda: SimpleNamespace(os=os))
    monkeypatch.setattr(probe_cookies, "load_client", lambda: fake_transport)


def test_mac_probe_cookie_diagnostic_reports_only_metadata(
    mac_fixture,
    capsys,
    tmp_path: Path,
):
    out = tmp_path / "cookie-horizon.json"
    assert probe_cookies.main(["--json", str(out)]) == 0
    text = capsys.readouterr().out
    doc = json.loads(out.read_text())
    assert doc["readable"] == 2
    assert doc["session_readable"] is True
    assert doc["session_expires"]
    assert doc["session_days_left"] is not None
    assert "fixture-private" not in text + out.read_text()
    assert "private-analytics" not in text + out.read_text()


def test_mac_probe_cookie_row_uses_bounded_epoch_mapping(mac_fixture):
    rows = probe_cookies.mac_cdp_cookie_rows()
    assert [r[0] for r in rows] == [
        "__Secure-next-auth.session-token.0",
        "cf-fake",
    ]
    assert rows[0][3] > 0
    assert rows[1][3] == 0
    assert len(rows[0][2]) > 2


@pytest.mark.parametrize("browser", ["firefox", "chromium", "not-chrome"])
def test_mac_probe_rejects_unknown_browser_before_read(
    mac_fixture, monkeypatch, browser
):
    monkeypatch.setattr(
        macos_cdp_auth,
        "browser_cookies",
        lambda: pytest.fail("CDP should not be accessed"),
    )
    assert probe_cookies.main(["--browser", browser]) == 2


def test_mac_probe_denies_unavailable_browser_without_secret_output(
    mac_fixture,
    monkeypatch,
    capsys,
):
    def fail():
        raise RuntimeError("test-only-secret")

    monkeypatch.setattr(macos_cdp_auth, "browser_cookies", fail)
    assert probe_cookies.main([]) == 1
    assert "test-only-secret" not in capsys.readouterr().out


def test_mac_probe_denies_missing_session_cookie(mac_fixture, monkeypatch):
    monkeypatch.setattr(
        macos_cdp_auth,
        "browser_cookies",
        lambda: [
            {
                "name": "analytical",
                "domain": "chatgpt.com",
                "value": "fixture",
                "expires": -1,
            }
        ],
    )
    assert probe_cookies.main([]) == 1
