"""Tests for chatgpt_session.py: cookie decryption and the terminal session.

Nothing here touches a real browser, the real GNOME keyring, or the network.
The GNOME keyring is faked by placing a stand-in ``dbus`` module in
``sys.modules`` before ``_keyring_password`` runs its ``import dbus`` (the
real python3-dbus package is importable in this venv, so without the fake it
would reach for the live session bus); cookie databases are temporary
sqlite files with the columns the module actually queries; and
``Session.call`` is driven through a fake ``urllib.request.urlopen`` with
``time.sleep`` replaced so a retry test never actually waits.

Every test names the failure it is defending against.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_session  # noqa: E402

# ---------------------------------------------------------------------------
# shared fakes and helpers
# ---------------------------------------------------------------------------


def _make_cookie_db(path: Path, rows: list[tuple[str, str, str, bytes]]) -> None:
    """A cookies table with the columns _cookie_header/pick_browser query.

    rows: (host_key, name, value, encrypted_value)
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB)"
    )
    con.executemany(
        "INSERT INTO cookies (host_key, name, value, encrypted_value) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _derive(password: bytes) -> bytes:
    """The same PBKDF2 key derivation _make_decryptor uses, for building fixtures."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
    ).derive(password)


def _encrypt(version: bytes, key: bytes, data: bytes) -> bytes:
    """Chrome's cookie encryption: AES-CBC, IV of 16 spaces, PKCS7 padding."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return version + encryptor.update(padded) + encryptor.finalize()


class _FakeItem:
    """A GNOME keyring secret item: maybe locked, maybe unreadable."""

    def __init__(self, secret: bytes | None = None, raises: bool = False) -> None:
        self.secret = secret
        self.raises = raises

    def GetSecret(self, session):  # mirrors the D-Bus method name
        if self.raises:
            raise Exception("item is still locked")
        return (None, None, self.secret)


class _FakeService:
    """org.freedesktop.Secret.Service, tracking whether Unlock ran."""

    def __init__(self, unlocked: list[str], locked: list[str]) -> None:
        self.unlocked = unlocked
        self.locked = locked
        self.unlock_calls: list[list[str]] = []

    def OpenSession(self, algorithm, plain):
        return ("plain", "session-handle")

    def SearchItems(self, criteria):
        return (self.unlocked, self.locked)

    def Unlock(self, paths):
        self.unlock_calls.append(list(paths))


class _FakeBus:
    def __init__(self, service: _FakeService, items_by_path: dict) -> None:
        self.service = service
        self.items_by_path = items_by_path

    def get_object(self, service_name, path):
        if path == "/org/freedesktop/secrets":
            return self.service
        return self.items_by_path[path]


def _install_fake_dbus(monkeypatch, bus: _FakeBus) -> None:
    """Replace sys.modules['dbus'] so _keyring_password never reaches the real bus.

    Also clears XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS: _keyring_password's
    first line is now ``ensure_desktop_env()``, which mutates the real
    ``os.environ`` (its default target) when either is absent. Starting every
    faked-bus test from "both absent", the cron case, makes the test outcome
    independent of whatever this machine's own desktop session happens to
    export; monkeypatch restores the true ambient values either way.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    fake = types.ModuleType("dbus")
    fake.SessionBus = lambda: bus
    fake.Interface = lambda obj, iface_name: obj
    fake.String = lambda s, variant_level=0: s
    monkeypatch.setitem(sys.modules, "dbus", fake)


class _FakeResponse:
    """A urlopen() context manager returning a fixed status and body."""

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    def read(self) -> bytes:
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://chatgpt.com/x", code, "msg", None, io.BytesIO(body.encode())
    )


def _session(
    cookie: str = "cookie=1", token: str | None = None
) -> chatgpt_session.Session:
    """A Session instance without running __init__: no browser, no network."""
    session = chatgpt_session.Session.__new__(chatgpt_session.Session)
    session.cookie = cookie
    if token is not None:
        session.token = token
    return session


# ---------------------------------------------------------------------------
# set_prog / fail
# ---------------------------------------------------------------------------


def test_fail_prints_the_current_prog_prefix_and_exits_1(monkeypatch, capsys) -> None:
    """The prefix must name which command failed, and set_prog must control it."""
    monkeypatch.setattr(chatgpt_session, "_prog", chatgpt_session._prog)
    chatgpt_session.set_prog("probe-cookies")
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.fail("boom")
    assert exc.value.code == 1
    assert capsys.readouterr().err == "probe-cookies: boom\n"


# ---------------------------------------------------------------------------
# ensure_desktop_env -- the D-Bus variables cron never sets
# ---------------------------------------------------------------------------


def test_ensure_desktop_env_sets_both_variables_from_the_uid_when_absent(
    monkeypatch,
) -> None:
    """A cron environment starts with neither variable; both must be derived
    from getuid(), not left for the caller to guess."""
    monkeypatch.setattr(chatgpt_session.os, "getuid", lambda: 4242)
    environ: dict[str, str] = {}
    chatgpt_session.ensure_desktop_env(environ)
    assert environ["XDG_RUNTIME_DIR"] == "/run/user/4242"
    assert environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"


def test_ensure_desktop_env_leaves_either_preset_variable_untouched(
    monkeypatch,
) -> None:
    """A value the caller already exported (its own XDG_RUNTIME_DIR, or a bus
    address pointed somewhere non-standard) must never be overwritten."""
    monkeypatch.setattr(chatgpt_session.os, "getuid", lambda: 4242)

    custom_dir = {"XDG_RUNTIME_DIR": "/custom/runtime"}
    chatgpt_session.ensure_desktop_env(custom_dir)
    assert custom_dir["XDG_RUNTIME_DIR"] == "/custom/runtime"
    assert custom_dir["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom/runtime/bus"

    custom_address = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/elsewhere/bus"}
    chatgpt_session.ensure_desktop_env(custom_address)
    assert custom_address["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/elsewhere/bus"
    assert custom_address["XDG_RUNTIME_DIR"] == "/run/user/4242"


def test_ensure_desktop_env_is_idempotent(monkeypatch) -> None:
    """A second call in the same process (_keyring_password and
    virtual_display can both run there) must change nothing the first call
    already set."""
    monkeypatch.setattr(chatgpt_session.os, "getuid", lambda: 4242)
    environ: dict[str, str] = {}
    chatgpt_session.ensure_desktop_env(environ)
    first = dict(environ)
    chatgpt_session.ensure_desktop_env(environ)
    assert environ == first


# ---------------------------------------------------------------------------
# _keyring_password -- the GNOME keyring over D-Bus, faked
# ---------------------------------------------------------------------------


def test_keyring_password_returns_the_secret_from_an_unlocked_item(
    monkeypatch,
) -> None:
    """The common case: one already-unlocked item holds the OS-crypt key."""
    service = _FakeService(unlocked=["/item1"], locked=[])
    bus = _FakeBus(service, {"/item1": _FakeItem(secret=b"unlocked-secret")})
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session._keyring_password("chrome") == b"unlocked-secret"
    assert service.unlock_calls == []


def test_keyring_password_unlocks_locked_items_before_reading_them(
    monkeypatch,
) -> None:
    """A freshly-started keyring returns items locked; Unlock must run first."""
    service = _FakeService(unlocked=[], locked=["/item1"])
    bus = _FakeBus(service, {"/item1": _FakeItem(secret=b"locked-secret")})
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session._keyring_password("chrome") == b"locked-secret"
    assert service.unlock_calls == [["/item1"]]


def test_keyring_password_tries_the_next_item_when_getsecret_raises(
    monkeypatch,
) -> None:
    """Two items can match the search; a stale or unreadable one must not be fatal."""
    service = _FakeService(unlocked=["/a", "/b"], locked=[])
    bus = _FakeBus(
        service,
        {"/a": _FakeItem(raises=True), "/b": _FakeItem(secret=b"second-secret")},
    )
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session._keyring_password("chrome") == b"second-secret"


def test_keyring_password_fails_when_no_item_is_found(monkeypatch) -> None:
    """An empty search result must exit cleanly, not raise from an empty loop."""
    service = _FakeService(unlocked=[], locked=[])
    bus = _FakeBus(service, {})
    _install_fake_dbus(monkeypatch, bus)
    with pytest.raises(SystemExit) as exc:
        chatgpt_session._keyring_password("chrome")
    assert exc.value.code == 1


def test_keyring_password_sets_the_bus_address_before_opening_the_session_bus(
    monkeypatch,
) -> None:
    """Under cron there is no DBUS_SESSION_BUS_ADDRESS yet; _keyring_password
    must set one itself -- via ensure_desktop_env() -- before calling
    dbus.SessionBus(), not assume a caller already exported it. The fake
    SessionBus records what DBUS_SESSION_BUS_ADDRESS was at the moment it was
    called, so a defaulting call placed too late (e.g. after the bus is
    opened) would be caught here even though the faked bus itself does not
    care what address it was "opened" with.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(chatgpt_session.os, "getuid", lambda: 4242)

    service = _FakeService(unlocked=["/item1"], locked=[])
    bus = _FakeBus(service, {"/item1": _FakeItem(secret=b"unlocked-secret")})
    seen_address: list[str | None] = []

    fake = types.ModuleType("dbus")

    def session_bus() -> _FakeBus:
        seen_address.append(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
        return bus

    fake.SessionBus = session_bus
    fake.Interface = lambda obj, iface_name: obj
    fake.String = lambda s, variant_level=0: s
    monkeypatch.setitem(sys.modules, "dbus", fake)

    assert chatgpt_session._keyring_password("chrome") == b"unlocked-secret"
    assert seen_address == ["unix:path=/run/user/4242/bus"]


# ---------------------------------------------------------------------------
# _make_decryptor -- real AES-CBC/PBKDF2 against known plaintexts
# ---------------------------------------------------------------------------


def test_make_decryptor_decrypts_a_v10_value(monkeypatch) -> None:
    """v10 cookies use the fixed 'peanuts' password; the keyring is never asked."""
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"unused")
    decrypt = chatgpt_session._make_decryptor("chrome")
    key = _derive(b"peanuts")
    enc = _encrypt(b"v10", key, b"hello-v10-cookie")
    assert decrypt(enc) == "hello-v10-cookie"


def test_make_decryptor_decrypts_a_v11_value_using_the_keyring_password(
    monkeypatch,
) -> None:
    """v11 cookies are keyed from the keyring password, not the v10 fallback."""
    monkeypatch.setattr(
        chatgpt_session, "_keyring_password", lambda app: b"keyring-password"
    )
    decrypt = chatgpt_session._make_decryptor("chrome")
    key = _derive(b"keyring-password")
    enc = _encrypt(b"v11", key, b"hello-v11-cookie")
    assert decrypt(enc) == "hello-v11-cookie"


def test_make_decryptor_strips_the_newer_chromium_domain_hash_prefix(
    monkeypatch,
) -> None:
    """Newer Chromium prepends 32 bytes before the value; both slices must be tried."""
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"unused")
    decrypt = chatgpt_session._make_decryptor("chrome")
    key = _derive(b"peanuts")
    enc = _encrypt(b"v10", key, (b"\x00" * 32) + b"real-plain-value")
    assert decrypt(enc) == "real-plain-value"


def test_make_decryptor_strips_a_full_block_of_pkcs7_padding(monkeypatch) -> None:
    """A plaintext that is already block-aligned still carries a padding byte of 16."""
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"unused")
    decrypt = chatgpt_session._make_decryptor("chrome")
    key = _derive(b"peanuts")
    enc = _encrypt(b"v10", key, b"0123456789ABCDEF")  # exactly one 16-byte block
    assert decrypt(enc) == "0123456789ABCDEF"


def test_make_decryptor_fails_on_a_non_printable_result(monkeypatch, capsys) -> None:
    """Garbage plaintext (wrong key, corrupt row) must not be handed back silently."""
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"unused")
    decrypt = chatgpt_session._make_decryptor("chrome")
    key = _derive(b"peanuts")
    enc = _encrypt(b"v10", key, b"\x80\x81\x82\x83")  # not valid UTF-8
    with pytest.raises(SystemExit) as exc:
        decrypt(enc)
    assert exc.value.code == 1
    assert "could not decode a decrypted cookie value" in capsys.readouterr().err


def test_make_decryptor_fails_on_an_unknown_version_prefix(monkeypatch, capsys) -> None:
    """A cookie encryption scheme this module has never seen must not be guessed at."""
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"unused")
    decrypt = chatgpt_session._make_decryptor("chrome")
    with pytest.raises(SystemExit) as exc:
        decrypt(b"v99" + b"\x00" * 16)
    assert exc.value.code == 1
    assert "unexpected cookie encryption version" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _cookie_header -- one Cookie header per browser profile
# ---------------------------------------------------------------------------


def test_cookie_header_decrypts_encrypted_values_and_keeps_plain_ones(
    tmp_path: Path, monkeypatch
) -> None:
    """An encrypted cell must go through the decryptor; a plain one must not."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", "__Secure-next-auth.session-token.0", "", b"ENCBLOB"),
            ("chatgpt.com", "plain_cookie", "plain-value", b""),
        ],
    )
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session,
        "_make_decryptor",
        lambda app: (lambda enc: f"DEC[{enc.decode()}]"),
    )
    header = chatgpt_session._cookie_header("testbrowser")
    assert "__Secure-next-auth.session-token.0=DEC[ENCBLOB]" in header
    assert "plain_cookie=plain-value" in header


def test_cookie_header_lists_cookies_in_the_sql_order_by_name(
    tmp_path: Path, monkeypatch
) -> None:
    """The query sorts by name; insertion order would build the wrong header."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", "zzz_last", "z", b""),
            ("chatgpt.com", "__Secure-next-auth.session-token.0", "s", b""),
            ("chatgpt.com", "mmm_middle", "m", b""),
        ],
    )
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session, "_make_decryptor", lambda app: (lambda enc: "")
    )
    header = chatgpt_session._cookie_header("testbrowser")
    names = [pair.split("=", 1)[0] for pair in header.split("; ")]
    assert names == sorted(names)
    assert names[-1] == "zzz_last"


def test_cookie_header_fails_when_no_session_cookie_is_present(
    tmp_path: Path, monkeypatch
) -> None:
    """A jar full of unrelated cookies must not be mistaken for a login."""
    db = tmp_path / "cookies.db"
    _make_cookie_db(db, [("chatgpt.com", "_cfuvid", "v", b"")])
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session, "_make_decryptor", lambda app: (lambda enc: "")
    )
    with pytest.raises(SystemExit) as exc:
        chatgpt_session._cookie_header("testbrowser")
    assert exc.value.code == 1


def test_cookie_header_fails_when_the_db_file_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """No browser profile at all must be a clean failure, not an sqlite traceback."""
    monkeypatch.setitem(
        chatgpt_session.BROWSERS, "testbrowser", (tmp_path / "missing.db", "testapp")
    )
    with pytest.raises(SystemExit) as exc:
        chatgpt_session._cookie_header("testbrowser")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# pick_browser
# ---------------------------------------------------------------------------


def test_pick_browser_returns_an_explicit_choice_unchanged() -> None:
    """A caller-supplied browser name must never be second-guessed."""
    assert chatgpt_session.pick_browser("chrome") == "chrome"
    assert chatgpt_session.pick_browser("chromium") == "chromium"


def test_pick_browser_auto_picks_the_first_browser_with_a_session_cookie(
    tmp_path: Path, monkeypatch
) -> None:
    """Auto must skip a missing profile and one with no session before picking."""
    no_session_db = tmp_path / "no_session.db"
    _make_cookie_db(no_session_db, [("chatgpt.com", "other", "v", b"")])
    has_session_db = tmp_path / "has_session.db"
    _make_cookie_db(
        has_session_db,
        [("chatgpt.com", "__Secure-next-auth.session-token.0", "v", b"")],
    )
    monkeypatch.setattr(
        chatgpt_session,
        "BROWSERS",
        {
            "missing": (tmp_path / "does-not-exist.db", "app1"),
            "no-session": (no_session_db, "app2"),
            "has-session": (has_session_db, "app3"),
        },
    )
    assert chatgpt_session.pick_browser("auto") == "has-session"


def test_pick_browser_auto_skips_a_database_that_sqlite_cannot_open(
    tmp_path: Path, monkeypatch
) -> None:
    """A locked or corrupt cookie DB must be skipped, not crash the whole probe."""
    broken_db = tmp_path / "broken.db"
    broken_db.write_text("not a sqlite database")
    has_session_db = tmp_path / "has_session.db"
    _make_cookie_db(
        has_session_db,
        [("chatgpt.com", "__Secure-next-auth.session-token.0", "v", b"")],
    )
    monkeypatch.setattr(
        chatgpt_session,
        "BROWSERS",
        {"broken": (broken_db, "app1"), "has-session": (has_session_db, "app2")},
    )
    assert chatgpt_session.pick_browser("auto") == "has-session"


def test_pick_browser_auto_fails_when_nothing_is_found(
    tmp_path: Path, monkeypatch
) -> None:
    """No logged-in browser at all must be a clean failure."""
    no_session_db = tmp_path / "no_session.db"
    _make_cookie_db(no_session_db, [("chatgpt.com", "other", "v", b"")])
    monkeypatch.setattr(
        chatgpt_session,
        "BROWSERS",
        {
            "missing": (tmp_path / "does-not-exist.db", "app1"),
            "no-session": (no_session_db, "app2"),
        },
    )
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.pick_browser("auto")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Session.call -- retries, headers, raw vs JSON
# ---------------------------------------------------------------------------


def test_call_returns_200_status_and_parsed_json(monkeypatch) -> None:
    """The default path must parse the body as JSON."""
    monkeypatch.setattr(
        chatgpt_session.urllib.request,
        "urlopen",
        lambda req, timeout=60: _FakeResponse(200, json.dumps({"a": 1})),
    )
    status, data = _session().call("/x")
    assert (status, data) == (200, {"a": 1})


def test_call_raw_true_returns_text_without_json_parsing(monkeypatch) -> None:
    """raw=True must hand back the body untouched, even when it is not valid JSON."""
    monkeypatch.setattr(
        chatgpt_session.urllib.request,
        "urlopen",
        lambda req, timeout=60: _FakeResponse(200, "not-json-at-all{{{"),
    )
    status, data = _session().call("/x", raw=True)
    assert (status, data) == (200, "not-json-at-all{{{")


def test_call_with_payload_sends_json_content_type_and_body(monkeypatch) -> None:
    """A payload must be serialised to JSON and announced with a Content-Type."""
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["req"] = req
        return _FakeResponse(200, "{}")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    _session().call("/x", method="POST", payload={"k": "v"})
    req = captured["req"]
    assert req.get_header("Content-type") == "application/json"
    assert req.data == json.dumps({"k": "v"}).encode()


def test_call_omits_authorization_header_until_a_token_is_set(monkeypatch) -> None:
    """A pre-auth call (fetching the session itself) must not send a stale token."""
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["req"] = req
        return _FakeResponse(200, "{}")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    _session().call("/x")
    assert captured["req"].get_header("Authorization") is None


def test_call_sends_bearer_authorization_once_token_is_set(monkeypatch) -> None:
    """Every call after the handshake must carry the bearer token."""
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["req"] = req
        return _FakeResponse(200, "{}")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    _session(token="tok-xyz").call("/x")
    assert captured["req"].get_header("Authorization") == "Bearer tok-xyz"


def test_call_retries_a_403_then_succeeds_with_backoff_2_then_4(monkeypatch) -> None:
    """Cloudflare's transient 403 must be retried, not treated as a hard failure."""
    sleeps: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", sleeps.append)
    responses = iter(
        [
            _http_error(403, "first"),
            _http_error(403, "second"),
            _FakeResponse(200, json.dumps({"ok": True})),
        ]
    )

    def fake_urlopen(req, timeout=60):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    status, data = _session().call("/x")
    assert (status, data) == (200, {"ok": True})
    assert sleeps == [2, 4]


def test_call_exhausts_retries_on_429_and_returns_the_last_error(monkeypatch) -> None:
    """After all retries a 429 must report the most recent attempt, not the first."""
    sleeps: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", sleeps.append)
    bodies = iter(["first", "second", "third"])

    def fake_urlopen(req, timeout=60):
        raise _http_error(429, next(bodies))

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    status, data = _session().call("/x")
    assert (status, data) == (429, {"error": "third"})
    assert sleeps == [2, 4]


def test_call_retries_a_500_then_succeeds(monkeypatch) -> None:
    """A server error must be retried the same as a 403, not returned immediately."""
    sleeps: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", sleeps.append)
    responses = iter([_http_error(500, "server error"), _FakeResponse(200, "{}")])

    def fake_urlopen(req, timeout=60):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    status, _data = _session().call("/x")
    assert status == 200
    assert sleeps == [2]


def test_call_returns_a_non_retryable_4xx_immediately(monkeypatch) -> None:
    """A plain 404 is not Cloudflare's rate limit; retrying it would waste time."""
    sleeps: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", sleeps.append)
    calls: list[int] = []

    def fake_urlopen(req, timeout=60):
        calls.append(1)
        raise _http_error(404, "not found")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    status, data = _session().call("/x")
    assert (status, data) == (404, {"error": "not found"})
    assert len(calls) == 1
    assert sleeps == []


def test_call_urlerror_returns_zero_status_without_raising(monkeypatch) -> None:
    """A DNS failure or refused connection must come back as data, not an exception."""

    def fake_urlopen(req, timeout=60):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    status, data = _session().call("/x", retries=1)
    assert status == 0
    assert "connection refused" in data["error"]


# ---------------------------------------------------------------------------
# Session.__init__ -- browser, cookie and the /api/auth/session handshake
# ---------------------------------------------------------------------------


def _wire_init(monkeypatch, json_body) -> None:
    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(chatgpt_session, "_cookie_header", lambda browser: "cookie=abc")
    monkeypatch.setattr(
        chatgpt_session.urllib.request,
        "urlopen",
        lambda req, timeout=60: _FakeResponse(200, json.dumps(json_body)),
    )


def test_session_init_succeeds_and_stores_token_and_user_id(monkeypatch) -> None:
    """A normal handshake must populate browser, cookie, token and user_id."""
    _wire_init(monkeypatch, {"accessToken": "tok123", "user": {"id": "user-1"}})
    session = chatgpt_session.Session()
    assert session.browser == "fake-browser"
    assert session.cookie == "cookie=abc"
    assert session.token == "tok123"
    assert session.user_id == "user-1"


@pytest.mark.parametrize(
    "body",
    [
        {"user": {"id": "user-1"}},
        {"accessToken": "tok123"},
        {"accessToken": "tok123", "user": {}},
        {"accessToken": "tok123", "user": None},
    ],
    ids=["no-token", "no-user", "user-without-id", "null-user"],
)
def test_session_init_fails_when_the_handshake_is_incomplete(monkeypatch, body) -> None:
    """A half-populated auth reply must not produce a half-authed session."""
    _wire_init(monkeypatch, body)
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.Session()
    assert exc.value.code == 1


def test_session_init_fails_when_the_auth_reply_is_not_a_json_object(
    monkeypatch,
) -> None:
    """A non-dict reply (e.g. an HTML error page parsed as text) must not crash init."""
    _wire_init(monkeypatch, [1, 2, 3])
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.Session()
    assert exc.value.code == 1


def test_pick_browser_rejects_an_unknown_name_with_a_clear_message(capsys) -> None:
    """An unknown browser used to surface later as a raw KeyError."""
    with pytest.raises(SystemExit):
        chatgpt_session.pick_browser("firefox")
    assert "unknown browser 'firefox'" in capsys.readouterr().err
