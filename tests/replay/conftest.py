"""Tier R fixtures: a real headless Chrome showing recorded pages, no network
(TESTING.md section 6, P1).

A recorded page is ``tests/fixtures/dom/<name>.html``, written by
``BrowserSender.snapshot_page`` and promoted by ``make refresh-dom-fixtures``.
``replay(name)`` serves it at a chatgpt.com URL through a Playwright route
and aborts every other request, so the page never touches the network:
interception happens before name resolution (checked 2026-09-27 against an
``.invalid`` host). ``tests/conftest.py`` additionally refuses any
non-loopback resolution or connection from the Python side.

The browser is the host's Google Chrome (``channel="chrome"``), the same
binary a real send uses; headless, so no Xvfb is needed. When Chrome is
missing the launch fails, and the tier fails with it: a tier that cannot
run must fail, never skip (TESTING.md section 6.4).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dom"
REPLAY_ORIGIN = "https://chatgpt.com/"
LAUNCH_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-extensions",
    "--no-first-run",
    "--mute-audio",
)


def fixture_names(prefix: str) -> list[str]:
    """The stems of ``tests/fixtures/dom/<prefix>*.html``, sorted."""
    return sorted(path.stem for path in FIXTURES.glob(f"{prefix}*.html"))


def facts_of(name: str) -> dict[str, Any]:
    """The JSON facts recorded beside ``<name>.html``."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def replay_browser() -> Any:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True, args=list(LAUNCH_ARGS)
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def replay(replay_browser: Any) -> Any:
    """``replay(name, url=...)`` -> a page showing the recorded fixture at
    ``url`` (default the chatgpt.com home page); ``replay.html(markup)``
    shows arbitrary markup the same way. Every context is closed after the
    test."""
    contexts: list[Any] = []

    def _show(markup: str, url: str) -> Any:
        context = replay_browser.new_context()
        contexts.append(context)
        page = context.new_page()

        def handle(route: Any, request: Any) -> None:
            if request.url.startswith(REPLAY_ORIGIN):
                route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=markup
                )
            else:
                route.abort()

        page.route("**/*", handle)
        page.goto(url, wait_until="domcontentloaded")
        return page

    def load(name: str, url: str = REPLAY_ORIGIN) -> Any:
        markup = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
        return _show(markup, url)

    load.html = lambda markup, url=REPLAY_ORIGIN: _show(markup, url)  # type: ignore[attr-defined]
    yield load
    for context in contexts:
        context.close()


ReplayLoader = Callable[..., Any]
