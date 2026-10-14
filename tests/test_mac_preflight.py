"""T0, socket-free macOS ChatGPT Skill readiness and fail-closed checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import mac_preflight as mac
import pytest


class _HTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"Browser":"Chrome/155.0.8059.80"}'


class _Composer:
    def __init__(self, *, fail=False):
        self.first = self
        self.fail = fail
        self.waits = 0

    def wait_for(self, *, timeout):
        assert timeout == 18000
        self.waits += 1
        if self.fail:
            raise RuntimeError("synthetic composer unavailable")

    def count(self):
        return 1


class _Page:
    def __init__(self, *, composer=None):
        self.url = "https://chatgpt.com/"
        self.composer = composer or _Composer()
        self.closed = False
        self.gotos = []

    def goto(self, url, *, wait_until, timeout):
        self.gotos.append((url, wait_until, timeout))

    def get_by_role(self, role):
        assert role == "textbox"
        return self.composer

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class _Browser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


class _Playwright:
    def __init__(self, browser=None, error=None):
        self.browser = browser
        self.error = error
        self.endpoints = []
        self.chromium = self

    def connect_over_cdp(self, url, *, timeout):
        self.endpoints.append((url, timeout))
        if self.error:
            raise self.error
        return self.browser


class _Manager:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_):
        return False


def _fake_browser(monkeypatch, *, browser=None, error=None):
    fake = _Playwright(browser=browser, error=error)
    module = ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _Manager(fake)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return fake


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(mac, "endpoint", lambda: "http://127.0.0.1:9233")
    requests = []

    def urlopen(url, *, timeout):
        requests.append((url, timeout))
        return _HTTPResponse()

    monkeypatch.setattr(mac.urllib.request, "urlopen", urlopen)
    import chatgpt_session

    monkeypatch.setattr(
        chatgpt_session,
        "Session",
        lambda **kwargs: SimpleNamespace(token="synthetic", user_id="synthetic"),
    )
    return requests


def test_mac_readonly_preflight_no_browser_go_json_is_sanitized(
    ready,
    capsys,
    tmp_path: Path,
):
    report = tmp_path / "preflight.json"
    assert mac.main(["--json", str(report)]) == 0
    output = capsys.readouterr().out
    assert "GO" in output
    assert "Authenticated HTTP session" in output
    assert "synthetic" not in output
    assert ready == [("http://127.0.0.1:9233/json/version", 5)]
    obj = json.loads(report.read_text())
    assert obj["verdict"] == "GO"
    assert obj["credential_values_saved"] is False
    assert len(obj["checks"]) == 4
    assert report.stat().st_mode & 0o077 == 0
    assert "synthetic" not in report.read_text()


def test_mac_preflight_real_browser_control_path_closes_only_new_tab(
    ready,
    monkeypatch,
    capsys,
):
    page = _Page()
    browser = _Browser([_Context(page)])
    fake = _fake_browser(monkeypatch, browser=browser)
    assert mac.main(["--browser"]) == 0
    assert page.closed and browser.closed
    assert page.gotos == [("https://chatgpt.com/", "domcontentloaded", 45000)]
    assert page.composer.waits == 1
    assert fake.endpoints == [("http://127.0.0.1:9233", 12000)]
    assert "Chat Mode composer" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad_flag",
    [
        ["--browser-name", "firefox"],
        ["--project", "bad-id"],
        ["--project", "g-p-x"],
    ],
)
def test_mac_preflight_invalid_selection_blocks(ready, bad_flag, capsys):
    assert mac.main(bad_flag) == 1
    assert "DO NOT START" in capsys.readouterr().out


def test_mac_preflight_disk_budget_enforced(ready, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(mac.shutil, "disk_usage", lambda _: SimpleNamespace(free=12))
    assert mac.main(["--workdir", str(tmp_path)]) == 1


def test_mac_preflight_invalid_cdp_blocks_without_touching_account(monkeypatch, capsys):
    monkeypatch.setattr(mac, "endpoint", lambda: None)
    assert mac.main(["--browser"]) == 1
    text = capsys.readouterr().out
    assert "Chrome CDP" in text and "DO NOT START" in text
    assert "ChatGPT auth" not in text


def test_mac_preflight_invalid_cdp_exception_is_reported_as_type_only(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        mac, "endpoint", lambda: (_ for _ in ()).throw(ValueError("synthetic secret"))
    )
    assert mac.main([]) == 1
    assert "ValueError" in capsys.readouterr().out
    assert "synthetic secret" not in capsys.readouterr().out


def test_mac_preflight_browser_http_error_denied(monkeypatch, capsys):
    monkeypatch.setattr(mac, "endpoint", lambda: "http://127.0.0.1:9233")
    monkeypatch.setattr(
        mac.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError("fixture")),
    )
    assert mac.main(["--browser"]) == 1
    assert "ConnectionRefusedError" in capsys.readouterr().out


def test_mac_preflight_auth_failure_is_denied_without_credential_log(
    ready, monkeypatch, capsys
):
    import chatgpt_session

    def deny(**_):
        raise RuntimeError("fixture-private-secret")

    monkeypatch.setattr(chatgpt_session, "Session", deny)
    assert mac.main([]) == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "fixture-private-secret" not in output


@pytest.mark.parametrize("contexts", [[], [object(), object()]])
def test_mac_preflight_wrong_context_count_denied_and_disconnects(
    ready,
    monkeypatch,
    contexts,
):
    browser = _Browser(contexts)
    _fake_browser(monkeypatch, browser=browser)
    assert mac.main(["--browser"]) == 1
    assert browser.closed


def test_mac_preflight_missing_composer_denied_and_cleans_up(ready, monkeypatch):
    page = _Page(composer=_Composer(fail=True))
    browser = _Browser([_Context(page)])
    _fake_browser(monkeypatch, browser=browser)
    assert mac.main(["--browser"]) == 1
    assert page.closed and browser.closed


def test_mac_preflight_cdp_connection_failure_denied(ready, monkeypatch):
    client = _fake_browser(monkeypatch, error=ConnectionRefusedError("fake"))
    assert mac.main(["--browser"]) == 1
    assert client.endpoints


def test_mac_preflight_refuses_output_in_nonexistent_directory(ready, tmp_path: Path):
    with pytest.raises(SystemExit, match="directory"):
        mac.main(["--json", str(tmp_path / "does-not-exist" / "result.json")])


def test_mac_preflight_dispatch_only_with_explicit_opt_in(monkeypatch):
    import preflight

    monkeypatch.setattr(preflight.sys, "platform", "darwin")
    monkeypatch.setenv("CHATGPT_MACOS_CDP_MODE", "1")
    monkeypatch.setattr(mac, "main", lambda args: 17 if args == ["--browser"] else 18)
    assert preflight.main(["--browser"]) == 17
    assert preflight.main(["--other"]) == 18
