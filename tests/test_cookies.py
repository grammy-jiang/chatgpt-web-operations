"""Tests for chatgpt_cookies.py: exporting the cookie jar as Playwright JSON.

Same offline techniques as test_session.py: a temporary sqlite cookie
database stands in for Chrome's real one, and chatgpt_session's decryption
and browser lookup are replaced with fakes, so nothing here touches a
keyring, a browser profile, or the network.

Every test names the failure it is defending against.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_cookies  # noqa: E402
import chatgpt_session as cs  # noqa: E402

SESSION_COOKIE = "__Secure-next-auth.session-token.0"

# ---------------------------------------------------------------------------
# shared fakes and helpers
# ---------------------------------------------------------------------------


def _make_cookie_db(path: Path, rows: list[tuple]) -> None:
    """A cookies table with the full column set export() queries.

    rows: (host_key, name, value, encrypted_value, path, expires_utc,
    is_secure, is_httponly, samesite)
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies ("
        "host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
        "path TEXT, expires_utc INTEGER, is_secure INTEGER, "
        "is_httponly INTEGER, samesite INTEGER)"
    )
    con.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def _wire_db(monkeypatch, db: Path, decrypt=lambda enc: f"DEC[{enc.decode()}]") -> None:
    """Point BROWSERS at the temp db and replace the real decryptor with a fake."""
    monkeypatch.setitem(cs.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(cs, "_make_decryptor", lambda app: decrypt)


@pytest.fixture(autouse=True)
def _no_stored_session(monkeypatch):
    """export() now also applies chatgpt_session's session-token renewal
    choice (chatgpt_session.py, "Session token renewal"): after 2026-09-21
    it calls cs.load_stored_session() itself. A T0 test must never reach
    the real keyring (tests/conftest.py's network guard does not cover
    D-Bus), and a test that is not about the substitution should see no
    behaviour change at all -- with nothing "stored", choose_session always
    keeps Chrome's own record, so apply_session_to_jar substitutes each
    cookie's already-current value back onto itself.
    """
    monkeypatch.setattr(cs, "load_stored_session", lambda: None)


# ---------------------------------------------------------------------------
# export() -- field by field
# ---------------------------------------------------------------------------


def test_export_converts_chrome_epoch_expiry_and_omits_it_for_session_cookies(
    tmp_path: Path, monkeypatch
) -> None:
    """expires_utc is microseconds since 1601; a 0 (session cookie) must add no key."""
    db = tmp_path / "cookies.db"
    exp = 13_403_397_058_000_000
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "s", b"", "/", 0, 1, 1, 1),
            ("chatgpt.com", "persistent", "p", b"", "/", exp, 0, 0, 1),
        ],
    )
    _wire_db(monkeypatch, db)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert "expires" not in out[SESSION_COOKIE]
    assert out["persistent"]["expires"] == exp / 1_000_000 - 11_644_473_600


def test_export_maps_samesite_integers_and_defaults_unknown_to_lax(
    tmp_path: Path, monkeypatch
) -> None:
    """An undocumented samesite integer must degrade to Lax, not raise a KeyError."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "s", b"", "/", 0, 0, 0, 1),
            ("chatgpt.com", "none_site", "v", b"", "/", 0, 0, 0, 0),
            ("chatgpt.com", "strict_site", "v", b"", "/", 0, 0, 0, 2),
            ("chatgpt.com", "weird_site", "v", b"", "/", 0, 0, 0, 99),
        ],
    )
    _wire_db(monkeypatch, db)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out["none_site"]["sameSite"] == "None"
    assert out["strict_site"]["sameSite"] == "Strict"
    assert out["weird_site"]["sameSite"] == "Lax"


def test_export_defaults_an_empty_path_to_slash_but_keeps_an_explicit_one(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty path column must not produce a cookie with no usable path."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "s", b"", "/", 0, 0, 0, 1),
            ("chatgpt.com", "no_path", "v", b"", "", 0, 0, 0, 1),
            ("chatgpt.com", "chat_path", "v", b"", "/chat", 0, 0, 0, 1),
        ],
    )
    _wire_db(monkeypatch, db)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out["no_path"]["path"] == "/"
    assert out["chat_path"]["path"] == "/chat"


def test_export_reports_secure_and_httponly_as_real_booleans(
    tmp_path: Path, monkeypatch
) -> None:
    """Chrome stores these as 0/1 integers; a Playwright context needs real bools."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "s", b"", "/", 0, 1, 1, 1),
            ("chatgpt.com", "open_cookie", "v", b"", "/", 0, 0, 0, 1),
        ],
    )
    _wire_db(monkeypatch, db)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out[SESSION_COOKIE]["secure"] is True
    assert out[SESSION_COOKIE]["httpOnly"] is True
    assert out["open_cookie"]["secure"] is False
    assert out["open_cookie"]["httpOnly"] is False


def test_export_decrypts_encrypted_values_and_keeps_plain_ones_as_is(
    tmp_path: Path, monkeypatch
) -> None:
    """An encrypted cell must be decrypted; a plain one must not be run through it."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "", b"ENCBLOB", "/", 0, 0, 0, 1),
            ("chatgpt.com", "plain_cookie", "plain-value", b"", "/", 0, 0, 0, 1),
        ],
    )
    _wire_db(monkeypatch, db)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out[SESSION_COOKIE]["value"] == "DEC[ENCBLOB]"
    assert out["plain_cookie"]["value"] == "plain-value"


# ---------------------------------------------------------------------------
# export() -- session token renewal substitution
# ---------------------------------------------------------------------------


def test_export_replaces_the_token_with_the_keyrings_copy_when_it_is_fresher(
    tmp_path: Path, monkeypatch
) -> None:
    """A stored session that outlives Chrome's own jar must end up in the
    exported jar: value and expires both come from the keyring, not from
    Chrome's own (still valid, just staler) cookie."""
    db = tmp_path / "cookies.db"
    exp = 13_403_397_058_000_000  # Chrome epoch microseconds
    _make_cookie_db(
        db, [("chatgpt.com", SESSION_COOKIE, "", b"ENCBLOB", "/", exp, 1, 1, 1)]
    )
    _wire_db(monkeypatch, db)
    chrome_expires = exp / 1_000_000 - 11_644_473_600
    stored = {
        "cookies": {SESSION_COOKIE: "keyring-value"},
        "expires": chrome_expires + 1000,
    }
    monkeypatch.setattr(cs, "load_stored_session", lambda: stored)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out[SESSION_COOKIE]["value"] == "keyring-value"
    assert out[SESSION_COOKIE]["expires"] == chrome_expires + 1000
    # shape besides value/expires must survive untouched
    assert out[SESSION_COOKIE]["domain"] == "chatgpt.com"
    assert out[SESSION_COOKIE]["secure"] is True


def test_export_keeps_chromes_own_token_when_it_is_fresher(
    tmp_path: Path, monkeypatch
) -> None:
    """A stored session that is not fresher must leave the exported jar
    exactly as Chrome's own jar has it -- the substitution must never make
    the jar worse."""
    db = tmp_path / "cookies.db"
    exp = 13_403_397_058_000_000
    _make_cookie_db(
        db, [("chatgpt.com", SESSION_COOKIE, "", b"ENCBLOB", "/", exp, 1, 1, 1)]
    )
    _wire_db(monkeypatch, db)
    chrome_expires = exp / 1_000_000 - 11_644_473_600
    stored = {
        "cookies": {SESSION_COOKIE: "stale-keyring-value"},
        "expires": chrome_expires - 1000,
    }
    monkeypatch.setattr(cs, "load_stored_session", lambda: stored)
    out = {c["name"]: c for c in chatgpt_cookies.export("testbrowser")}
    assert out[SESSION_COOKIE]["value"] == "DEC[ENCBLOB]"
    assert out[SESSION_COOKIE]["expires"] == chrome_expires


def test_export_fails_when_the_db_file_is_missing(tmp_path: Path, monkeypatch) -> None:
    """No cookie DB at all must be a clean failure, not an unhandled exception."""
    monkeypatch.setitem(cs.BROWSERS, "testbrowser", (tmp_path / "missing.db", "app"))
    with pytest.raises(SystemExit) as exc:
        chatgpt_cookies.export("testbrowser")
    assert exc.value.code == 1


def test_export_fails_when_no_row_has_a_session_cookie(
    tmp_path: Path, monkeypatch
) -> None:
    """A jar of analytics cookies without a login must not be exported as one."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(db, [("chatgpt.com", "_cfuvid", "v", b"", "/", 0, 0, 0, 1)])
    _wire_db(monkeypatch, db)
    with pytest.raises(SystemExit) as exc:
        chatgpt_cookies.export("testbrowser")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# main() -- argparse wiring and JSON on stdout
# ---------------------------------------------------------------------------


def test_main_prints_the_exported_cookies_as_json(monkeypatch, capsys) -> None:
    """The CLI's whole job is to print export()'s result as JSON, once, to stdout."""
    monkeypatch.setattr(sys, "argv", ["chatgpt_cookies.py", "--browser", "chrome"])
    # Snapshot _prog so monkeypatch restores it: main() calls the real set_prog().
    monkeypatch.setattr(cs, "_prog", cs._prog)
    seen = {}

    def fake_pick_browser(choice):
        seen["choice"] = choice
        return "chrome-picked"

    def fake_export(browser):
        seen["browser"] = browser
        return [{"name": "x", "value": "y"}]

    monkeypatch.setattr(cs, "pick_browser", fake_pick_browser)
    monkeypatch.setattr(chatgpt_cookies, "export", fake_export)
    chatgpt_cookies.main()
    out = capsys.readouterr().out
    assert json.loads(out) == [{"name": "x", "value": "y"}]
    assert seen == {"choice": "chrome", "browser": "chrome-picked"}


def test_main_defaults_the_browser_choice_to_auto(monkeypatch, capsys) -> None:
    """Running with no --browser flag must resolve like a person typing nothing."""
    monkeypatch.setattr(sys, "argv", ["chatgpt_cookies.py"])
    monkeypatch.setattr(cs, "_prog", cs._prog)
    seen = {}

    def fake_pick_browser(choice):
        seen["choice"] = choice
        return "auto-picked"

    monkeypatch.setattr(cs, "pick_browser", fake_pick_browser)
    monkeypatch.setattr(chatgpt_cookies, "export", lambda browser: [])
    chatgpt_cookies.main()
    assert seen["choice"] == "auto"
