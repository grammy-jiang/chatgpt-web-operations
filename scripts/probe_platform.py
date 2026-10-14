#!/usr/bin/env python3
"""Is the dedicated browser window still logged in to platform.openai.com?
Read-only.

    probe_platform.py [--json PATH|-] [--port N] [--profile DIR]
                      [--timeout SECONDS] [--warn-days N]

platform.openai.com is a separate login from chatgpt.com. The chatgpt.com
session renews itself on every authenticated request (``chatgpt_session.py``,
"Session token renewal"); the Platform one does not. Its session lives in
auth.openai.com's cookies (``oai-client-auth-info``, about 30 days, and the
unified-session manifest, about 400 days; there is no platform-specific
cookie), and when it goes stale the site sends the tab to ``/login`` (an
account chooser) and, after the account is chosen, to
``auth.openai.com/mfa-challenge`` ("Check your authenticator app"). Only the
owner can answer that. This probe exists so the owner hears about it from the
morning mail, not from a Platform step that fails mid-task (owner's approval,
2026-10-12).

What it does, and nothing more:

1. Attach to the dedicated browser (``dedicated_browser.py``; CDP on
   127.0.0.1:9222) under the same cross-process browser slot every window
   takes (``chatgpt_client.browser_slot``: ``RP_MAX_BROWSERS``,
   ``RP_BROWSER_WAIT_SECONDS``, the memory floor). A busy slot is waited for
   and then reported, never forced. No dedicated browser means ERROR: the
   scripted Xvfb window carries no Platform login, so there is no fallback.
2. Open ONE new tab on https://platform.openai.com/ through the browser's
   HTTP endpoint (``PUT /json/new``) and poll ``/json/list`` until the tab's
   address has not changed for ``SETTLE_SECONDS`` (at least
   ``MIN_WAIT_SECONDS`` after the open, at most ``--timeout``).
3. Classify the final address and title (``classify``): VALID (the Platform
   app, not ``/login``, not auth.openai.com), LOGIN_REQUIRED (``/login`` or
   an auth.openai.com log-in or chooser page), MFA_REQUIRED
   (auth.openai.com/mfa-challenge or any verify / two-factor page), ERROR
   (a timeout, a network or load failure, a Cloudflare challenge, anything
   else).
4. Close the tab (``/json/close``), also on SIGTERM.
5. Read, from a COPY of the dedicated profile's cookie DB, the expiry of the
   auth.openai.com session cookies: the query selects ``host_key``, ``name``
   and ``expires_utc`` and nothing else, so no value is ever read.

The HTTP endpoints above are the whole surface: there is no page script, no
input event, no click, no typing, no account selection, and the API-keys page
is never opened. Nothing is printed or stored but the state, the final
address as host and path (no query string), a short reason and the days left.

``--json PATH`` writes the document (``-`` prints it instead of the summary):
``checked_at``, ``state``, ``detail``, ``final_url``, ``tab_closed``,
``auth_days`` (``oai_client_auth_info``, ``unified_session``),
``platform_auth_days`` (the soonest, whole days), ``severity`` (``ok`` or
``warn``; a Platform problem is never more), ``finding``, ``log_fields``
(the cron wrapper's ``check.log`` fields) and ``elapsed_s``.

Exit 0 VALID with more than ``--warn-days`` left, 2 LOGIN_REQUIRED,
MFA_REQUIRED or VALID close to expiry, 1 ERROR.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import chatgpt_client as cc
import probe_cookies
from _common import ensure_venv

PLATFORM_URL = "https://platform.openai.com/"
PLATFORM_HOST = "platform.openai.com"
AUTH_HOSTS = ("auth.openai.com", "auth0.openai.com")
STATES = ("VALID", "LOGIN_REQUIRED", "MFA_REQUIRED", "ERROR")

# A Platform problem is a WARN at most, and this many days before the auth
# cookie's expiry is early enough for the owner to log in at leisure.
WARN_DAYS = 5
OWNER_ACTION = (
    "owner action: log in to platform.openai.com in the dedicated Chrome (MFA)"
)

# The tab settles when its address has stayed the same this long, but never
# before MIN_WAIT_SECONDS: the app's first address is "/" and the redirect to
# /login (stale) or to the dashboard (valid) is the app's own script.
SETTLE_SECONDS = 6.0
MIN_WAIT_SECONDS = 10.0
NAV_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 5.0

# Cookie name -> the document's key. Both spellings of the unified-session
# manifest exist in the jar (measured 2026-10-12: ``__Host-`` on
# auth.openai.com, plain on .auth.openai.com); the earlier one counts.
AUTH_COOKIES = {
    "oai-client-auth-info": "oai_client_auth_info",
    "__Host-unified_session_manifest": "unified_session",
    "unified_session_manifest": "unified_session",
}
COOKIE_QUERY = (
    "SELECT host_key, name, expires_utc FROM cookies "
    "WHERE host_key LIKE '%auth.openai.com' AND name IN ({marks})"
)

MFA_PATH = re.compile(
    r"(?:^|[/_-])(?:mfa|verify|verification|two-factor|2fa|otp|totp|authenticator)"
    r"(?:$|[/_-])"
)
MFA_TITLE = ("authenticator", "verify your", "verification", "two-factor", "2fa")
LOGIN_TITLE = ("log in", "login", "sign in", "choose an account", "welcome back")
CHALLENGE_TITLE = ("just a moment", "attention required", "verify you are human")
LOGIN_PATHS = ("/login", "/log-in", "/signup", "/sign-up")

# The tab this process has open, so a SIGTERM (the wrapper's ``timeout``)
# can close it from outside before the process ends.
_open_tab: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Classification: pure functions over the final address and title
# ---------------------------------------------------------------------------


def short_url(url: str) -> str:
    """``host/path`` with no scheme, query string or fragment; the scheme is
    kept for a non-web page (``chrome-error://chromewebdata/``)."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if parts.scheme in ("http", "https"):
        return f"{host}{path}"
    return (url or "").split("?", 1)[0].split("#", 1)[0]


def classify(url: str, title: str = "") -> tuple[str, str]:
    """``(state, reason)`` for a settled tab at ``url`` titled ``title``.

    The address decides first; the title only separates what the address
    cannot (a Cloudflare challenge served on the Platform host, or a log-in
    page that kept the Platform address)."""
    parts = urlsplit(url or "")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    path = (parts.path or "/").lower()
    low_title = (title or "").lower()
    where = short_url(url)
    if scheme not in ("http", "https"):
        page = where or "no page"
        return "ERROR", f"the tab ended on {page}: a network or load failure"
    if any(marker in low_title for marker in CHALLENGE_TITLE):
        return "ERROR", f"a Cloudflare challenge at {where}, not a login page"
    is_auth = host in AUTH_HOSTS or host.endswith(".auth.openai.com")
    if is_auth or host == PLATFORM_HOST:
        if MFA_PATH.search(path) or any(m in low_title for m in MFA_TITLE):
            return "MFA_REQUIRED", f"a second-factor page at {where}"
        if is_auth:
            return "LOGIN_REQUIRED", f"the auth.openai.com log-in at {where}"
        if path.startswith(LOGIN_PATHS):
            return "LOGIN_REQUIRED", f"the Platform log-in or chooser at {where}"
        if path.startswith("/auth/"):
            return "ERROR", f"the tab stopped in the sign-in callback at {where}"
        if any(m in low_title for m in LOGIN_TITLE):
            return "LOGIN_REQUIRED", f"a log-in page at {where} (by its title)"
        return "VALID", f"the Platform app loaded at {where}"
    return "ERROR", f"the tab ended on another site: {where}"


def verdict(
    state: str, days: int | None, detail: str = "", warn_days: int = WARN_DAYS
) -> tuple[str, str]:
    """``(severity, finding)``: ``ok`` or ``warn``, never more. A Platform
    problem is the owner's to fix by hand, so it never raises an ALERT and
    never reaches the repair loop; the finding names the state and the
    owner's action."""
    soon = days is not None and days <= warn_days
    expiry = f"; the auth cookies expire in {days} d" if soon else ""
    if state == "VALID":
        if soon:
            return "warn", (
                f"platform.openai.com: VALID, but the auth cookies expire in "
                f"{days} d; {OWNER_ACTION} before then"
            )
        return "ok", ""
    if state in ("LOGIN_REQUIRED", "MFA_REQUIRED"):
        return "warn", f"platform.openai.com: {state}{expiry}; {OWNER_ACTION}"
    reason = f" ({detail})" if detail else ""
    return "warn", f"platform.openai.com probe: ERROR{reason}{expiry}"


def log_fields(state: str, days: int | None) -> str:
    """The cron wrapper's ``check.log`` fields: ``platform=<STATE>
    platform_auth_days=<N>``, ``-`` for an unknown number."""
    shown = "-" if days is None else str(days)
    named = state if state in STATES else "ERROR"
    return f"platform={named} platform_auth_days={shown}"


# ---------------------------------------------------------------------------
# Cookie metadata: names and expiry only, from a copy
# ---------------------------------------------------------------------------


def cookie_db(profile: Path) -> Path:
    """The dedicated profile's cookie database."""
    return Path(profile) / "Default" / "Cookies"


def read_auth_cookies(
    db: Path, connect: Callable[[str], Any] = sqlite3.connect
) -> list[tuple[str, str, int]]:
    """``(host_key, name, expires_utc)`` of the auth.openai.com session
    cookies, from a copy of ``db`` (the live file is locked by the browser).
    The query names three columns; no value column is ever read."""
    names = tuple(AUTH_COOKIES)
    query = COOKIE_QUERY.format(marks=", ".join("?" * len(names)))
    with tempfile.TemporaryDirectory(prefix="probe-platform-") as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copyfile(db, copy)
        con = connect(str(copy))
        try:
            rows = con.execute(query, names).fetchall()
        finally:
            con.close()
    return [(str(host), str(name), int(exp or 0)) for host, name, exp in rows]


def auth_days(rows: list[tuple[str, str, int]], now: float) -> dict[str, float | None]:
    """Days left per document key (``oai_client_auth_info``,
    ``unified_session``), one decimal; the earliest row wins; None for a
    missing or session-scoped cookie."""
    out: dict[str, float | None] = dict.fromkeys(sorted(set(AUTH_COOKIES.values())))
    for _host, name, expires_utc in rows:
        key = AUTH_COOKIES.get(name)
        epoch = probe_cookies.expiry_epoch(expires_utc)
        if key is None or epoch is None:
            continue
        days = round((epoch - now) / 86400, 1)
        current = out[key]
        if current is None or days < current:
            out[key] = days
    return out


def soonest_days(days: dict[str, float | None]) -> int | None:
    """The whole days left on the soonest auth cookie (rounded down), or
    None when none is known."""
    known = [d for d in days.values() if d is not None]
    return math.floor(min(known)) if known else None


# ---------------------------------------------------------------------------
# The tab, over the dedicated browser's HTTP endpoint
# ---------------------------------------------------------------------------


def _cdp(
    endpoint: str,
    path: str,
    method: str = "GET",
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Any:
    """One request to the browser's debugging endpoint; the parsed JSON body,
    or the raw text when it is not JSON (``/json/close`` answers in text)."""
    request = urllib.request.Request(f"{endpoint.rstrip('/')}{path}", method=method)
    with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8", "replace")
    stripped = body.strip()
    return json.loads(stripped) if stripped[:1] in ("{", "[") else stripped


def open_tab(endpoint: str, opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    """Open one tab on PLATFORM_URL; its target id. Chrome 111 and later
    want PUT here."""
    target = _cdp(endpoint, f"/json/new?{PLATFORM_URL}", "PUT", opener)
    target_id = str(target.get("id") or "") if isinstance(target, dict) else ""
    if not target_id:
        raise RuntimeError("the browser opened no tab (no target id in /json/new)")
    return target_id


def read_target(
    endpoint: str, target_id: str, opener: Callable[..., Any] = urllib.request.urlopen
) -> dict[str, Any] | None:
    """The tab's entry in ``/json/list`` (its ``url`` and ``title``), or None
    when it is gone."""
    pages = _cdp(endpoint, "/json/list", "GET", opener)
    for page in pages if isinstance(pages, list) else []:
        if isinstance(page, dict) and page.get("id") == target_id:
            return page
    return None


def close_tab(
    endpoint: str, target_id: str, opener: Callable[..., Any] = urllib.request.urlopen
) -> bool:
    """Close the tab; two attempts, as ``BrowserSender.close_tab_from_outside``
    makes (a loaded browser can miss one)."""
    for _attempt in range(2):
        try:
            _cdp(endpoint, f"/json/close/{target_id}", "GET", opener)
            return True
        except Exception:
            continue
    return False


def settle(
    read: Callable[[], dict[str, Any] | None],
    *,
    timeout: float = NAV_TIMEOUT_SECONDS,
    min_wait: float = MIN_WAIT_SECONDS,
    stable_for: float = SETTLE_SECONDS,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any] | None, bool]:
    """Poll the tab until its address has stayed the same for
    ``stable_for`` seconds and at least ``min_wait`` have passed; ``(last
    entry, settled)``. ``(None, False)`` when the tab went away first,
    ``(last, False)`` when ``timeout`` ran out."""
    start = clock()
    last: dict[str, Any] | None = None
    last_url: str | None = None
    since = start
    while True:
        entry = read()
        now = clock()
        if entry is None:
            return None, False
        url = str(entry.get("url") or "")
        if url != last_url:
            last_url, since = url, now
        last = entry
        loaded = url not in ("", "about:blank")
        if loaded and now - start >= min_wait and now - since >= stable_for:
            return last, True
        if now - start >= timeout:
            return last, False
        sleep(poll)


def navigate(
    endpoint: str,
    *,
    timeout: float = NAV_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Open the tab, wait for it to settle, close it, classify. The tab is
    closed whatever happened in between."""
    target_id = open_tab(endpoint, opener)
    _open_tab.update(endpoint=endpoint, target_id=target_id)
    try:
        last, settled = settle(
            lambda: read_target(endpoint, target_id, opener),
            timeout=timeout,
            clock=clock,
            sleep=sleep,
        )
    finally:
        closed = close_tab(endpoint, target_id, opener)
        _open_tab.clear()
    if last is None:
        return {
            "state": "ERROR",
            "detail": "the tab went away before it settled",
            "final_url": "",
            "tab_closed": closed,
        }
    url = str(last.get("url") or "")
    if not settled:
        return {
            "state": "ERROR",
            "detail": (
                f"the page did not settle within {timeout:.0f} s "
                f"(last at {short_url(url) or '-'})"
            ),
            "final_url": short_url(url),
            "tab_closed": closed,
        }
    state, detail = classify(url, str(last.get("title") or ""))
    return {
        "state": state,
        "detail": detail,
        "final_url": short_url(url),
        "tab_closed": closed,
    }


def _close_on_sigterm(signum: int, frame: Any) -> None:
    """Close the open tab from outside, then leave through SystemExit, so a
    ``timeout``-killed probe never leaves its tab behind (F-2026-10-01-29)."""
    if _open_tab:
        close_tab(_open_tab["endpoint"], _open_tab["target_id"])
        _open_tab.clear()
    raise SystemExit(128 + int(signum))


# ---------------------------------------------------------------------------
# The whole probe
# ---------------------------------------------------------------------------


def probe(
    *,
    endpoint: str | None = None,
    profile: Path | None = None,
    timeout: float = NAV_TIMEOUT_SECONDS,
    warn_days: int = WARN_DAYS,
    slot: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
    alive: Callable[[str], Any] | None = None,
    nav: Callable[..., dict[str, Any]] | None = None,
    read_cookies: Callable[[Path], list[tuple[str, str, int]]] = read_auth_cookies,
    now: Callable[[], float] = time.time,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The document ``--json`` writes. Every failure is ERROR in the
    document, never an exception out of here."""
    endpoint = endpoint or cc.DEDICATED_URL
    profile = Path(profile or cc.DEDICATED_PROFILE_DIR)
    slot = slot or cc.browser_slot
    alive = alive or cc.dedicated_browser_version
    nav = nav or navigate
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    started = clock()
    result: dict[str, Any] = {"final_url": "", "tab_closed": None}
    try:
        if not alive(endpoint):
            result.update(
                state="ERROR",
                detail=(
                    f"the dedicated browser does not answer at {endpoint}; "
                    "dedicated_browser.py status"
                ),
            )
        else:
            with slot():
                result.update(nav(endpoint, timeout=timeout))
    except Exception as exc:
        result.update(state="ERROR", detail=f"{type(exc).__name__}: {exc}"[:240])
    try:
        days = auth_days(read_cookies(cookie_db(profile)), now())
        cookie_error = ""
    except Exception as exc:
        days = auth_days([], now())
        cookie_error = f"{type(exc).__name__}: {exc}"[:160]
    soonest = soonest_days(days)
    severity, finding = verdict(result["state"], soonest, result["detail"], warn_days)
    doc = {
        "checked_at": checked_at,
        "state": result["state"],
        "detail": result["detail"],
        "final_url": result["final_url"],
        "tab_closed": result["tab_closed"],
        "auth_days": days,
        "platform_auth_days": soonest,
        "severity": severity,
        "finding": finding,
        "log_fields": log_fields(result["state"], soonest),
        "elapsed_s": round(clock() - started, 1),
    }
    if cookie_error:
        doc["cookie_error"] = cookie_error
    return doc


def summary_lines(doc: dict[str, Any]) -> list[str]:
    """What the command prints: the state first, then the reason and the
    finding when there is one."""
    days = doc["auth_days"]

    def shown(key: str) -> str:
        return "-" if days.get(key) is None else f"{days[key]:.1f} d"

    lines = [
        f"platform: {doc['state']} at {doc['final_url'] or '-'}; auth cookies "
        f"oai-client-auth-info {shown('oai_client_auth_info')}, unified session "
        f"{shown('unified_session')}",
        f"  {doc['detail']}",
    ]
    if doc["finding"]:
        lines.append(f"  {doc['finding']}")
    if doc.get("cookie_error"):
        lines.append(f"  cookie DB not read: {doc['cookie_error']}")
    return lines


def exit_code(doc: dict[str, Any]) -> int:
    """0 ok, 2 a warn the owner can act on, 1 ERROR."""
    if doc["state"] == "ERROR":
        return 1
    return 0 if doc["severity"] == "ok" else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write the document here; - prints it instead of the summary",
    )
    ap.add_argument("--port", type=int, default=cc.DEDICATED_PORT)
    ap.add_argument("--profile", type=Path, default=cc.DEDICATED_PROFILE_DIR)
    ap.add_argument(
        "--timeout",
        type=float,
        default=NAV_TIMEOUT_SECONDS,
        help="seconds the tab may take to settle (default %(default).0f)",
    )
    ap.add_argument(
        "--warn-days",
        type=int,
        default=WARN_DAYS,
        help="warn when the auth cookies have this many days or fewer left",
    )
    args = ap.parse_args(argv)
    previous = None
    with contextlib.suppress(ValueError):  # not the main thread
        previous = signal.signal(signal.SIGTERM, _close_on_sigterm)
    try:
        doc = probe(
            endpoint=f"http://127.0.0.1:{args.port}",
            profile=args.profile,
            timeout=args.timeout,
            warn_days=args.warn_days,
        )
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    text = json.dumps(doc, indent=2) + "\n"
    if args.json == "-":
        print(text, end="")
    else:
        print("\n".join(summary_lines(doc)))
        if args.json:
            out = Path(args.json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
    return exit_code(doc)


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
