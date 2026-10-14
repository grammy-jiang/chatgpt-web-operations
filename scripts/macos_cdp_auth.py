"""macOS: read the owner's dedicated Chrome cookies through local CDP.

No Keychain or Chrome database is opened. Cookie values remain in memory:
never log, print, write or transfer them between hosts.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import urlsplit

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def endpoint() -> str | None:
    """Mac-only explicit localhost browser binding. Never silently fall back."""
    if sys.platform != "darwin":
        return None
    raw = os.environ.get("RP_BROWSER_CDP", "").strip()
    if not raw or raw in {"auto", "off"}:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid macOS local CDP endpoint") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_HOSTS
        or not 1 <= (port or 0) <= 65535
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("macOS CDP must be explicit localhost HTTP with a port")
    return raw.rstrip("/")


def _eligible(cookie: dict[str, Any]) -> bool:
    """Only the browser's chatgpt.com request cookies; no other domains."""
    domain = cookie.get("domain")
    name = cookie.get("name")
    value = cookie.get("value")
    return (
        isinstance(domain, str)
        and (domain == "chatgpt.com" or domain == ".chatgpt.com")
        and isinstance(name, str)
        and len(name) <= 256
        and isinstance(value, str)
        and len(value) <= 16384
        and bool(name)
    )


def browser_cookies() -> list[dict[str, Any]]:
    """Retrieve browser-scoped ChatGPT cookie records with CDP, never disk."""
    url = endpoint()
    if url is None:
        raise RuntimeError("Explicit macOS Chrome CDP endpoint is required")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(url, timeout=12000)
        try:
            contexts = list(browser.contexts)
            if len(contexts) != 1:
                raise RuntimeError("Expected exactly one dedicated Chrome context")
            records: list[dict[str, Any]] = contexts[0].cookies(
                ["https://chatgpt.com/"]
            )
            filtered = [dict(c) for c in records if _eligible(c)]
            if not any(
                str(c["name"]).startswith("__Secure-next-auth.session-token")
                for c in filtered
            ):
                raise RuntimeError("Dedicated macOS Chrome is not logged in to ChatGPT")
            return sorted(filtered, key=lambda c: str(c["name"]))
        finally:
            # With CDP, close() disconnects our client but does not stop Chrome.
            browser.close()
