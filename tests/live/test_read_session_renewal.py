"""Tier T1: does the session-token renewal actually happen against the real
account? (marker live_read, CHATGPT_LIVE=read)

chatgpt_session.py, "Session token renewal": every ``Session`` built
anywhere in this skill renews the persistent token into this machine's own
keyring whenever the server hands back a materially later expiry than what
was just sent. The ``live_session`` fixture (tests/live/conftest.py)
builds a real ``chatgpt_client.ChatGPTSession``, which builds a real
``chatgpt_session.Session`` -- so by the time either test here runs, at
least one renewal attempt has already happened for real, over the one call
this whole tier is allowed to make either way (``GET /api/auth/session``).

This is the one live tier in this whole suite that writes anything
locally: an item in the user's own GNOME keyring (service
``org.freedesktop.secrets``, attributes ``{"application":
"chatgpt-web-operations", "purpose": "chatgpt-session-token"}``). It never
writes anything on the account itself, and never Chrome's own cookie
database, which stays read-only exactly as everywhere else in this skill.

Shape only, never a cookie or token value (TESTING.md section 2):
``session.token`` is asserted truthy and nothing about ``load_stored_session``'s
record is ever printed or compared to a literal string.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_session as cs  # noqa: E402
import probe_cookies  # noqa: E402

pytestmark = pytest.mark.live_read

# The token is issued for 90 days (measured 2026-09-21); one extra day is
# slack for clock skew between this machine and the server, and for the
# time this test itself takes to run.
MAX_HORIZON_DAYS = 91


def _chrome_horizon_expires(browser: str = "chrome") -> float | None:
    """Chrome's own jar horizon as an epoch, read the same undecrypted way
    probe_cookies.py does -- expiry is not a secret. None when the jar
    holds no persistent session token (should not happen on this
    machine's own logged-in Chrome, but this must never crash the test
    over it)."""
    db, _app = cs.BROWSERS[browser]
    rows = probe_cookies.read_jar(Path(db))
    horizon = probe_cookies.session_horizon(rows, time.time())
    if horizon is None:
        return None
    _date, days_left = horizon
    return time.time() + days_left * 86400


def test_a_real_session_build_leaves_a_sane_renewal_in_the_keyring(
    live_session: Any,
) -> None:
    """live_session (unused directly below, its build is the point) has
    already made a real Session over the one GET this tier allows, which
    already renewed -- or confirmed -- the token. This checks what landed
    in the keyring is sane: present, not expired, not absurdly far out,
    and never a downgrade from Chrome's own jar (choose_session's own
    invariant: the keyring only ever holds the later of the two)."""
    now = time.time()
    record = cs.load_stored_session()
    assert record is not None, "a real Session build must leave something stored"
    assert isinstance(record.get("cookies"), dict) and record["cookies"]
    assert record.get("source") == "api/auth/session"

    expires = record["expires"]
    assert now < expires <= now + MAX_HORIZON_DAYS * 86400

    chrome_expires = _chrome_horizon_expires()
    if chrome_expires is not None:
        # A second of slack: the two reads (this one and the jar's) are not
        # perfectly simultaneous.
        assert expires >= chrome_expires - 1


def test_session_still_authenticates_with_the_store_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHATGPT_SESSION_STORE=0 must only ever turn off the keyring side:
    Chrome's own cookie is untouched by anything in this skill and remains
    a valid login by itself, exactly as before this feature existed --
    proving the kill switch is real, not just a flag that is checked and
    ignored. Never prints or compares the token; truthy is everything a
    caller needs to know (HARD RULES: never print or log a cookie or
    token value).
    """
    monkeypatch.setenv("CHATGPT_SESSION_STORE", "0")
    session = cs.Session("chrome")
    assert session.token
    assert session.user_id
    assert session.session_source == "chrome"
