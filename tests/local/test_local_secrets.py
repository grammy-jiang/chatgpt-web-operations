"""Tier TL (``live_local``): this machine's own secrets, read the way a
session reads them, with no network (TESTING.md section 6, P7).

Chrome's cookie database is opened from a copy, read-only; the session
cookie decrypts through the real ``_keyring_password()`` over D-Bus and real
AES; ``probe_cookies.py`` runs; the keyring's own session record is read;
and the pure choice between the two copies runs on the real records. The
Python socket guard of T0 stays on (``tests/conftest.py``); D-Bus goes
through libdbus, not Python sockets.

Nothing here prints, logs or asserts on a value. Every assertion compares a
name, a length, a count or a time, so a failure message carries no secret.
Daily from the wrapper, before ``health.py``: when this fails, the session
cannot open either, and this says which step broke.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import chatgpt_session
import pytest

pytestmark = pytest.mark.live_local

PREFIX = chatgpt_session.SESSION_COOKIE_PREFIX


def _versions(db: Path, tmp_path: Path) -> dict[str, int]:
    """How many chatgpt.com values each encryption version holds, from a
    copy of the database: names of versions only, never a value."""
    copy = tmp_path / "Cookies"
    shutil.copy2(db, copy)
    con = sqlite3.connect(copy)
    rows = con.execute(
        "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%chatgpt.com'"
    ).fetchall()
    con.close()
    counts: dict[str, int] = {}
    for (enc,) in rows:
        version = bytes(enc[:3]).decode("ascii", "replace") if enc else "plain"
        counts[version] = counts.get(version, 0) + 1
    return counts


def test_the_chrome_jar_opens_from_a_copy_and_the_session_cookie_decrypts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _app = chatgpt_session.BROWSERS["chrome"]
    assert db.is_file(), "no Chrome cookie database for this user"
    versions = _versions(db, tmp_path)
    assert versions.get("v11", 0) > 0, f"no keyring-encrypted value: {versions}"

    real = chatgpt_session._keyring_password
    calls: list[str] = []

    def counting(app: str) -> Any:
        calls.append(app)
        return real(app)

    monkeypatch.setattr(chatgpt_session, "_keyring_password", counting)
    pairs, record = chatgpt_session._cookie_pairs("chrome")

    assert calls == ["chrome"], "the keyring key was not read exactly once"
    names = [name for name, _value in pairs]
    tokens = [value for name, value in pairs if name.startswith(PREFIX)]
    assert tokens, f"no session token among {len(names)} cookies"
    # The token is chunked (.0, .1, ...) and the last chunk may be short; a
    # real one is several kilobytes in all.
    assert sum(len(value) for value in tokens) > 1000, "the session token is short"
    assert record is not None and record["expires"], "no expiry on Chrome's copy"
    days = (record["expires"] - time.time()) / 86_400
    assert days > 0, f"Chrome's session token expired {-days:.1f} days ago"


def test_probe_cookies_exits_0_and_prints_no_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import probe_cookies

    code = probe_cookies.main(["--browser", "chrome"])
    out = capsys.readouterr().out
    assert code == 0, "probe_cookies.py could not read the session cookie"
    pairs, _record = chatgpt_session._cookie_pairs("chrome")
    leaked = sorted({name for name, value in pairs if len(value) >= 8 and value in out})
    assert leaked == [], f"values printed for: {leaked}"


def test_the_keyring_holds_a_session_record_with_an_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chatgpt_session.SESSION_STORE_ENV, raising=False)
    record = chatgpt_session.load_stored_session()
    assert record is not None, "no session record in this machine's keyring"
    assert sorted(record) >= ["cookies", "expires"], sorted(record)
    assert any(name.startswith(PREFIX) for name in record["cookies"]), sorted(
        record["cookies"]
    )
    days = (float(record["expires"]) - time.time()) / 86_400
    assert days > 0, f"the keyring's session record expired {-days:.1f} days ago"


def test_the_later_of_the_two_copies_is_the_one_a_session_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chatgpt_session.SESSION_STORE_ENV, raising=False)
    _pairs, chrome = chatgpt_session._cookie_pairs("chrome")
    stored = chatgpt_session.load_stored_session()
    chosen, source = chatgpt_session.choose_session(chrome, stored)
    assert chosen is not None and source in ("chrome", "keyring"), source
    later = max(
        (r for r in (chrome, stored) if r and r.get("expires")),
        key=lambda r: float(r["expires"]),
    )
    assert float(chosen["expires"]) == float(later["expires"]), source
