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
from email.message import Message
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_session  # noqa: E402

SESSION_COOKIE = chatgpt_session.SESSION_COOKIE_PREFIX + ".0"

# ---------------------------------------------------------------------------
# shared fakes and helpers
# ---------------------------------------------------------------------------


def _make_cookie_db(
    path: Path,
    rows: list[tuple[str, str, str, bytes] | tuple[str, str, str, bytes, int]],
) -> None:
    """A cookies table with the columns _cookie_header/_cookie_pairs/
    pick_browser query, including expires_utc (Chrome's microseconds-
    since-1601 form; 0 means a session-scoped cookie).

    rows: (host_key, name, value, encrypted_value[, expires_utc]) -- a
    4-tuple defaults expires_utc to 0, so every existing caller that does
    not care about expiry needs no change.
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER)"
    )
    padded = [(*row, 0) if len(row) == 4 else row for row in rows]
    con.executemany(
        "INSERT INTO cookies (host_key, name, value, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        padded,
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
    """org.freedesktop.Secret.Service, tracking whether Unlock ran.

    ``unlocked``/``locked`` are the fixed lists ``_keyring_password``'s own
    tests set up by hand. ``register()`` is the extra surface the
    load_stored_session/store_session round-trip tests need: an item a
    ``_FakeCollection`` just created (or a test wired in directly) becomes
    visible to a later ``SearchItems`` call on this same fake service --
    modelling one shared keyring behind the Service and Collection
    interfaces, as the real Secret Service is.
    """

    def __init__(
        self, unlocked: list[str] | None = None, locked: list[str] | None = None
    ) -> None:
        self.unlocked = list(unlocked or [])
        self.locked = list(locked or [])
        self.unlock_calls: list[list[str]] = []
        self._registry: dict[str, tuple[dict[str, str], bool]] = {}

    def OpenSession(self, algorithm, plain):
        return ("plain", "session-handle")

    def SearchItems(self, criteria):
        crit = dict(criteria)
        dyn_unlocked, dyn_locked = [], []
        for path, (attrs, is_locked) in self._registry.items():
            if all(attrs.get(k) == v for k, v in crit.items()):
                (dyn_locked if is_locked else dyn_unlocked).append(path)
        return (self.unlocked + dyn_unlocked, self.locked + dyn_locked)

    def Unlock(self, paths):
        self.unlock_calls.append(list(paths))
        for path in paths:
            if path in self._registry:
                attrs, _locked = self._registry[path]
                self._registry[path] = (attrs, False)

    def register(self, path: str, attributes: dict, locked: bool = False) -> None:
        self._registry[path] = (dict(attributes), locked)


class _FakeCollection:
    """org.freedesktop.Secret.Collection: only ``CreateItem``, the one
    method ``store_session`` calls. An item it creates is added to both
    the owning bus's ``items_by_path`` (so ``GetSecret`` can find it) and
    the service's registry (so ``SearchItems`` can find it), so a
    store_session() then load_stored_session() round trip works over one
    fake exactly as it does over the real Secret Service.
    """

    def __init__(self, bus: _FakeBus, service: _FakeService) -> None:
        self.bus = bus
        self.service = service
        self.create_calls: list[tuple[dict, tuple, bool]] = []
        self._next_id = 0

    def CreateItem(self, properties, secret, replace):
        self.create_calls.append((dict(properties), tuple(secret), bool(replace)))
        attrs = dict(properties.get("org.freedesktop.Secret.Item.Attributes", {}))
        existing = next(
            (p for p, (a, _locked) in self.service._registry.items() if a == attrs),
            None,
        )
        path = existing if (existing and replace) else None
        if path is None:
            self._next_id += 1
            path = f"/item/created{self._next_id}"
        self.bus.items_by_path[path] = _FakeItem(secret=bytes(secret[2]))
        self.service.register(path, attrs, locked=False)
        return (path, "/")


class _FakeBus:
    def __init__(
        self,
        service: _FakeService,
        items_by_path: dict,
        collection: _FakeCollection | None = None,
    ) -> None:
        self.service = service
        self.items_by_path = items_by_path
        self.collection = (
            collection if collection is not None else _FakeCollection(self, service)
        )

    def get_object(self, service_name, path):
        if path == "/org/freedesktop/secrets":
            return self.service
        if path == "/org/freedesktop/secrets/aliases/default":
            return self.collection
        return self.items_by_path[path]


def _install_fake_dbus(monkeypatch, bus: _FakeBus) -> None:
    """Replace sys.modules['dbus'] so _keyring_password never reaches the real bus.

    Also clears XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS: _keyring_password's
    first line is now ``ensure_desktop_env()``, which mutates the real
    ``os.environ`` (its default target) when either is absent. Starting every
    faked-bus test from "both absent", the cron case, makes the test outcome
    independent of whatever this machine's own desktop session happens to
    export; monkeypatch restores the true ambient values either way.

    ``Dictionary``/``Struct``/``ByteArray`` are trivial passthroughs, only
    exercised by store_session (CreateItem's properties/secret arguments);
    the pre-existing ``_keyring_password`` tests never call them.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    fake = types.ModuleType("dbus")
    fake.SessionBus = lambda: bus
    fake.Interface = lambda obj, iface_name: obj
    fake.String = lambda s, variant_level=0: s
    fake.Dictionary = lambda mapping, signature=None, variant_level=0: dict(mapping)
    fake.Struct = lambda value, signature=None: tuple(value)
    fake.ByteArray = lambda data: bytes(data)
    monkeypatch.setitem(sys.modules, "dbus", fake)


class _FakeResponse:
    """A urlopen() context manager returning a fixed status, body and
    headers -- an email.message.Message, the real base class of
    http.client.HTTPMessage, so .get_all("Set-Cookie") behaves exactly as
    it would against a genuine response rather than a hand-rolled fake of
    it."""

    def __init__(
        self, status: int, text: str, headers: list[tuple[str, str]] | None = None
    ) -> None:
        self.status = status
        self._text = text
        self.headers = Message()
        for name, value in headers or []:
            self.headers[name] = value

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
    assert exc.value.detail == "boom"
    assert capsys.readouterr().err == "probe-cookies: boom\n"


def test_auth_failure_reason_distinguishes_cloudflare_challenge_from_expiry() -> None:
    headers = Message()
    headers["CF-Mitigated"] = "challenge"
    headers["Server"] = "cloudflare"
    reason = chatgpt_session._auth_failure_reason(403, headers)
    assert "Cloudflare challenge" in reason
    assert "validity is unknown" in reason
    assert "expired" not in reason


def test_auth_failure_reason_keeps_expiry_hint_for_plain_403() -> None:
    reason = chatgpt_session._auth_failure_reason(403, Message())
    assert "may be expired" in reason


def test_auth_failure_reason_does_not_call_rate_limit_cookie_expiry() -> None:
    reason = chatgpt_session._auth_failure_reason(429, Message())
    assert "rate limited" in reason
    assert "expired" not in reason


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
# chrome_session_record -- {"cookies", "expires"} from (name, value, epoch) rows
# ---------------------------------------------------------------------------


def test_chrome_session_record_keeps_only_session_token_chunks() -> None:
    """A cookie that is not part of the token must never enter the record."""
    rows = [("_cfuvid", "v", 1000.0), (SESSION_COOKIE, "tok0", 2000.0)]
    assert chatgpt_session.chrome_session_record(rows) == {
        "cookies": {SESSION_COOKIE: "tok0"},
        "expires": 2000.0,
    }


def test_chrome_session_record_expires_is_the_earliest_chunk() -> None:
    """One chunk expiring sooner ends the whole token, so it must win."""
    rows = [
        (chatgpt_session.SESSION_COOKIE_PREFIX + ".0", "a", 3000.0),
        (chatgpt_session.SESSION_COOKIE_PREFIX + ".1", "b", 1000.0),
    ]
    assert chatgpt_session.chrome_session_record(rows)["expires"] == 1000.0


def test_chrome_session_record_is_none_when_no_chunk_is_present() -> None:
    assert chatgpt_session.chrome_session_record([("_cfuvid", "v", 1000.0)]) is None


def test_chrome_session_record_expires_is_none_when_no_chunk_has_one() -> None:
    """A session-scoped chunk (Chrome's expires_utc of 0) still produces a
    record -- just one this module can never call fresher than anything
    with a real expiry (see choose_session)."""
    rows = [(SESSION_COOKIE, "tok0", None)]
    assert chatgpt_session.chrome_session_record(rows) == {
        "cookies": {SESSION_COOKIE: "tok0"},
        "expires": None,
    }


# ---------------------------------------------------------------------------
# renewed_session -- what a set of Set-Cookie lines just re-issued
# ---------------------------------------------------------------------------

NOW = 1_800_000_000.0


def test_renewed_session_prefers_max_age_added_to_now() -> None:
    lines = [f"{SESSION_COOKIE}=fresh0; Max-Age=7776000; Path=/; Secure; HttpOnly"]
    renewed = chatgpt_session.renewed_session(lines, NOW)
    assert renewed["cookies"] == {SESSION_COOKIE: "fresh0"}
    assert renewed["expires"] == NOW + 7_776_000
    assert renewed["stored_at"] == NOW
    assert renewed["source"] == "api/auth/session"


def test_renewed_session_falls_back_to_expires_when_there_is_no_max_age() -> None:
    lines = [f"{SESSION_COOKIE}=fresh0; Expires=Sat, 19 Dec 2026 22:57:49 GMT; Path=/"]
    renewed = chatgpt_session.renewed_session(lines, NOW)
    assert renewed["cookies"] == {SESSION_COOKIE: "fresh0"}
    assert renewed["expires"] == pytest.approx(1_797_721_069.0)


def test_renewed_session_is_none_when_the_token_cookie_has_neither() -> None:
    """A record this module could never compare for freshness later is not
    worth returning at all."""
    lines = [f"{SESSION_COOKIE}=fresh0; Path=/; Secure"]
    assert chatgpt_session.renewed_session(lines, NOW) is None


def test_renewed_session_is_none_without_any_token_cookie() -> None:
    lines = ["_cfuvid=xyz; Path=/", "oai-did=abc; Max-Age=100"]
    assert chatgpt_session.renewed_session(lines, NOW) is None


def test_renewed_session_keeps_every_chunk_not_just_two() -> None:
    """A future response may chunk the token differently; nothing here may
    assume exactly .0 and .1."""
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    lines = [
        f"{prefix}.0=a; Max-Age=300",
        f"{prefix}.1=b; Max-Age=100",
        f"{prefix}.2=c; Max-Age=200",
    ]
    renewed = chatgpt_session.renewed_session(lines, NOW)
    assert renewed["cookies"] == {
        f"{prefix}.0": "a",
        f"{prefix}.1": "b",
        f"{prefix}.2": "c",
    }
    assert renewed["expires"] == NOW + 100  # the earliest chunk


def test_renewed_session_ignores_a_line_that_does_not_parse() -> None:
    """One malformed Set-Cookie line must not take the rest down with it.
    "=====" is not decorative here: it is one of the few inputs that makes
    http.cookies.SimpleCookie.load itself raise (CookieError, "Illegal key
    '='"), which is the branch this defends -- a line that merely parses
    to nothing (most garbage does) never reaches that except at all."""
    lines = ["=====", f"{SESSION_COOKIE}=fresh0; Max-Age=100"]
    renewed = chatgpt_session.renewed_session(lines, NOW)
    assert renewed["cookies"] == {SESSION_COOKIE: "fresh0"}


def test_renewed_session_falls_back_to_expires_when_max_age_is_not_numeric() -> None:
    """A Max-Age present but unparseable must not be fatal -- Expires is
    still there to fall back on."""
    lines = [
        f"{SESSION_COOKIE}=fresh0; Max-Age=notanumber; "
        "Expires=Sat, 19 Dec 2026 22:57:49 GMT"
    ]
    renewed = chatgpt_session.renewed_session(lines, NOW)
    assert renewed["expires"] == pytest.approx(1_797_721_069.0)


def test_renewed_session_is_none_when_expires_does_not_parse_either() -> None:
    lines = [f"{SESSION_COOKIE}=fresh0; Expires=not-a-date-at-all"]
    assert chatgpt_session.renewed_session(lines, NOW) is None


class _FakeMorsel(dict):
    """A minimal stand-in for http.cookies.Morsel, carrying only what
    _cookie_expiry reads (``["max-age"]``, ``["expires"]``).

    Used for one test that no real Set-Cookie line can reach through
    ``renewed_session()``'s own ``SimpleCookie.load()``: that parser's
    "Special case for 'expires' attr" regex (http.cookies source) requires
    the literal ``GMT`` suffix on any value containing a space, so
    ``parsedate_to_datetime`` never actually sees a timezone-less string
    on the public path -- confirmed empirically: an Expires value without
    ``GMT`` makes ``SimpleCookie.load()`` parse the cookie as absent
    entirely, not as a cookie with an odd Expires. ``_cookie_expiry``'s own
    fallback for that case is still worth being correct about on its own
    terms, which is what this exercises directly.
    """

    def __init__(self, max_age: str = "", expires: str = "") -> None:
        super().__init__({"max-age": max_age, "expires": expires})


def test_cookie_expiry_treats_a_timezone_less_expires_as_utc() -> None:
    morsel = _FakeMorsel(expires="Sat, 19 Dec 2026 22:57:49")
    epoch = chatgpt_session._cookie_expiry(morsel, NOW)
    assert epoch == pytest.approx(1_797_721_069.0)


def test_cookie_expiry_is_none_with_neither_max_age_nor_expires() -> None:
    assert chatgpt_session._cookie_expiry(_FakeMorsel(), NOW) is None


# ---------------------------------------------------------------------------
# choose_session -- which record (chrome's jar or the keyring's) is fresher
# ---------------------------------------------------------------------------


def test_choose_session_prefers_chrome_when_it_expires_later() -> None:
    chrome = {"cookies": {"a": "c"}, "expires": NOW + 200}
    stored = {"cookies": {"a": "s"}, "expires": NOW + 100}
    assert chatgpt_session.choose_session(chrome, stored) == (chrome, "chrome")


def test_choose_session_prefers_keyring_when_it_expires_later() -> None:
    chrome = {"cookies": {"a": "c"}, "expires": NOW + 100}
    stored = {"cookies": {"a": "s"}, "expires": NOW + 200}
    assert chatgpt_session.choose_session(chrome, stored) == (stored, "keyring")


def test_choose_session_keeps_chrome_on_an_exact_tie() -> None:
    chrome = {"cookies": {"a": "c"}, "expires": NOW + 100}
    stored = {"cookies": {"a": "s"}, "expires": NOW + 100}
    assert chatgpt_session.choose_session(chrome, stored) == (chrome, "chrome")


def test_choose_session_returns_the_only_side_present() -> None:
    chrome = {"cookies": {"a": "c"}, "expires": NOW}
    stored = {"cookies": {"a": "s"}, "expires": NOW}
    assert chatgpt_session.choose_session(chrome, None) == (chrome, "chrome")
    assert chatgpt_session.choose_session(None, stored) == (stored, "keyring")


def test_choose_session_returns_none_when_both_sides_are_none() -> None:
    assert chatgpt_session.choose_session(None, None) == (None, "none")


def test_choose_session_prefers_a_known_expiry_over_an_unknown_one() -> None:
    """A record whose expiry cannot be compared must never beat one that can."""
    chrome = {"cookies": {"a": "c"}, "expires": None}
    stored = {"cookies": {"a": "s"}, "expires": NOW + 100}
    assert chatgpt_session.choose_session(chrome, stored) == (stored, "keyring")
    assert chatgpt_session.choose_session(stored, chrome) == (stored, "chrome")


# ---------------------------------------------------------------------------
# apply_session -- substitute the chosen token into a list of cookie pairs
# ---------------------------------------------------------------------------


def test_apply_session_replaces_a_matching_chunks_value() -> None:
    pairs = [(SESSION_COOKIE, "old")]
    chosen = {"cookies": {SESSION_COOKIE: "new"}}
    assert chatgpt_session.apply_session(pairs, chosen) == [(SESSION_COOKIE, "new")]


def test_apply_session_drops_a_chunk_the_chosen_set_does_not_carry() -> None:
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    pairs = [(f"{prefix}.0", "old0"), (f"{prefix}.1", "old1")]
    chosen = {"cookies": {f"{prefix}.0": "new0"}}
    assert chatgpt_session.apply_session(pairs, chosen) == [(f"{prefix}.0", "new0")]


def test_apply_session_adds_a_chunk_missing_from_pairs() -> None:
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    pairs = [(f"{prefix}.0", "old0")]
    chosen = {"cookies": {f"{prefix}.0": "new0", f"{prefix}.1": "new1"}}
    assert chatgpt_session.apply_session(pairs, chosen) == [
        (f"{prefix}.0", "new0"),
        (f"{prefix}.1", "new1"),
    ]


def test_apply_session_keeps_the_order_of_surviving_entries_stable() -> None:
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    pairs = [("x", "1"), (f"{prefix}.0", "old0"), ("y", "2"), (f"{prefix}.1", "old1")]
    chosen = {"cookies": {f"{prefix}.0": "new0"}}
    assert chatgpt_session.apply_session(pairs, chosen) == [
        ("x", "1"),
        (f"{prefix}.0", "new0"),
        ("y", "2"),
    ]


def test_apply_session_never_touches_a_non_token_cookie() -> None:
    pairs = [("oai-did", "abc"), ("_cfuvid", "xyz")]
    chosen = {"cookies": {SESSION_COOKIE: "new"}}
    out = chatgpt_session.apply_session(pairs, chosen)
    assert ("oai-did", "abc") in out
    assert ("_cfuvid", "xyz") in out
    assert (SESSION_COOKIE, "new") in out


# ---------------------------------------------------------------------------
# apply_session_to_jar -- the same substitution over Playwright-shaped dicts
# ---------------------------------------------------------------------------


def _jar_cookie(name: str, value: str, **extra) -> dict:
    base = {
        "name": name,
        "value": value,
        "domain": ".chatgpt.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    base.update(extra)
    return base


def test_apply_session_to_jar_replaces_value_and_expires_in_place() -> None:
    jar = [
        _jar_cookie(SESSION_COOKIE, "old", expires=1000.0),
        _jar_cookie("oai-did", "x"),
    ]
    chosen = {"cookies": {SESSION_COOKIE: "new"}, "expires": 2000.0}
    out = chatgpt_session.apply_session_to_jar(jar, chosen)
    assert out is jar  # modified in place and returned
    token = next(c for c in jar if c["name"] == SESSION_COOKIE)
    assert token["value"] == "new"
    assert token["expires"] == 2000.0
    assert any(c["name"] == "oai-did" for c in jar)


def test_apply_session_to_jar_drops_a_chunk_not_in_the_chosen_set() -> None:
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    jar = [_jar_cookie(f"{prefix}.0", "a"), _jar_cookie(f"{prefix}.1", "b")]
    chosen = {"cookies": {f"{prefix}.0": "new0"}, "expires": 2000.0}
    out = chatgpt_session.apply_session_to_jar(jar, chosen)
    assert [c["name"] for c in out] == [f"{prefix}.0"]


def test_apply_session_to_jar_adds_a_missing_chunk_shaped_like_an_existing_one() -> (
    None
):
    prefix = chatgpt_session.SESSION_COOKIE_PREFIX
    jar = [_jar_cookie(f"{prefix}.0", "a", domain=".chatgpt.com", path="/x")]
    chosen = {
        "cookies": {f"{prefix}.0": "new0", f"{prefix}.1": "new1"},
        "expires": 2000.0,
    }
    out = chatgpt_session.apply_session_to_jar(jar, chosen)
    added = next(c for c in out if c["name"] == f"{prefix}.1")
    assert added["value"] == "new1"
    assert added["domain"] == ".chatgpt.com"
    assert added["path"] == "/x"
    assert added["secure"] is True
    assert added["httpOnly"] is True
    assert added["expires"] == 2000.0


def test_apply_session_to_jar_uses_a_default_shape_with_no_template() -> None:
    """No chunk at all in the jar to copy from must not raise."""
    chosen = {"cookies": {SESSION_COOKIE: "new0"}, "expires": 2000.0}
    out = chatgpt_session.apply_session_to_jar([], chosen)
    assert out[0]["name"] == SESSION_COOKIE
    assert out[0]["domain"] == ".chatgpt.com"
    assert out[0]["secure"] is True


# ---------------------------------------------------------------------------
# load_stored_session / store_session -- this machine's own keyring
# ---------------------------------------------------------------------------

_TEST_RECORD = {
    "cookies": {SESSION_COOKIE: "keyring-value"},
    "expires": NOW + 1000,
    "stored_at": NOW,
    "source": "api/auth/session",
}


def test_store_then_load_round_trips_the_record(monkeypatch) -> None:
    service = _FakeService(unlocked=[], locked=[])
    bus = _FakeBus(service, {})
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session.store_session(_TEST_RECORD) is True
    assert chatgpt_session.load_stored_session() == _TEST_RECORD


def test_a_second_store_replaces_the_first_with_replace_true(monkeypatch) -> None:
    service = _FakeService(unlocked=[], locked=[])
    bus = _FakeBus(service, {})
    _install_fake_dbus(monkeypatch, bus)
    chatgpt_session.store_session(_TEST_RECORD)
    second = dict(
        _TEST_RECORD, cookies={SESSION_COOKIE: "newer-value"}, expires=NOW + 2000
    )
    assert chatgpt_session.store_session(second) is True
    assert chatgpt_session.load_stored_session() == second
    assert bus.collection.create_calls[0][2] is True  # replace=True both times
    assert bus.collection.create_calls[1][2] is True
    assert len(service._registry) == 1  # replaced, not a second item


def test_store_session_labels_and_attributes_the_item(monkeypatch) -> None:
    """The one thing that makes the item findable and deletable later."""
    service = _FakeService(unlocked=[], locked=[])
    bus = _FakeBus(service, {})
    _install_fake_dbus(monkeypatch, bus)
    chatgpt_session.store_session(_TEST_RECORD)
    properties, secret, _replace = bus.collection.create_calls[0]
    assert properties["org.freedesktop.Secret.Item.Label"] == (
        chatgpt_session.SESSION_ITEM_LABEL
    )
    assert properties["org.freedesktop.Secret.Item.Attributes"] == dict(
        chatgpt_session.SESSION_ITEM_ATTRIBUTES
    )
    assert secret[3] == "text/plain"
    assert json.loads(bytes(secret[2]).decode("utf-8")) == _TEST_RECORD


def test_load_stored_session_unlocks_a_locked_item_first(monkeypatch) -> None:
    service = _FakeService(unlocked=[], locked=[])
    secret_bytes = json.dumps(_TEST_RECORD).encode("utf-8")
    bus = _FakeBus(service, {"/item/locked": _FakeItem(secret=secret_bytes)})
    service.register(
        "/item/locked", chatgpt_session.SESSION_ITEM_ATTRIBUTES, locked=True
    )
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session.load_stored_session() == _TEST_RECORD
    assert service.unlock_calls == [["/item/locked"]]


def test_load_stored_session_returns_none_with_nothing_stored(monkeypatch) -> None:
    service = _FakeService(unlocked=[], locked=[])
    bus = _FakeBus(service, {})
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session.load_stored_session() is None


def test_load_stored_session_skips_an_item_it_cannot_parse_and_tries_the_next(
    monkeypatch,
) -> None:
    """Two items can match the search (a stale copy left over from an
    older attribute scheme, say); one that GetSecret fails on or whose
    secret is not the JSON this module wrote must not be fatal -- mirrors
    _keyring_password's own "tries the next item" behaviour."""
    service = _FakeService(unlocked=["/item/bad-raises", "/item/bad-json"], locked=[])
    bus = _FakeBus(
        service,
        {
            "/item/bad-raises": _FakeItem(raises=True),
            "/item/bad-json": _FakeItem(secret=b"not valid json {{{"),
            "/item/good": _FakeItem(secret=json.dumps(_TEST_RECORD).encode("utf-8")),
        },
    )
    service.register("/item/good", chatgpt_session.SESSION_ITEM_ATTRIBUTES)
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session.load_stored_session() == _TEST_RECORD


def test_load_stored_session_returns_none_on_a_dbus_exception(monkeypatch) -> None:
    class _Boom:
        def SessionBus(self):
            raise Exception("no session bus")

    fake = types.ModuleType("dbus")
    fake.SessionBus = _Boom().SessionBus
    monkeypatch.setitem(sys.modules, "dbus", fake)
    assert chatgpt_session.load_stored_session() is None


def test_store_session_returns_false_on_a_dbus_exception_and_warns_to_stderr(
    monkeypatch, capsys
) -> None:
    fake = types.ModuleType("dbus")

    def boom():
        raise Exception("keyring is unreachable")

    fake.SessionBus = boom
    monkeypatch.setitem(sys.modules, "dbus", fake)
    assert chatgpt_session.store_session(_TEST_RECORD) is False
    err = capsys.readouterr().err
    assert "could not store" in err
    assert "keyring-value" not in err  # never a cookie value


def test_session_store_env_0_never_touches_the_bus_for_load_or_store(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CHATGPT_SESSION_STORE", "0")

    class _Untouchable:
        def SessionBus(self):
            raise AssertionError("the bus must never be opened")

    fake = types.ModuleType("dbus")
    fake.SessionBus = _Untouchable().SessionBus
    monkeypatch.setitem(sys.modules, "dbus", fake)
    assert chatgpt_session.load_stored_session() is None
    assert chatgpt_session.store_session(_TEST_RECORD) is False


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
# _cookie_pairs -- pairs plus what the jar says about Chrome's own token
# ---------------------------------------------------------------------------


def test_cookie_pairs_returns_the_same_pairs_cookie_header_joins(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "cookies.db"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", SESSION_COOKIE, "", b"ENCBLOB", 0),
            ("chatgpt.com", "plain_cookie", "plain-value", b"", 0),
        ],
    )
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session,
        "_make_decryptor",
        lambda app: (lambda enc: f"DEC[{enc.decode()}]"),
    )
    pairs, _chrome = chatgpt_session._cookie_pairs("testbrowser")
    header = "; ".join(f"{n}={v}" for n, v in pairs)
    assert header == chatgpt_session._cookie_header("testbrowser")


def test_cookie_pairs_reports_chromes_own_expiry_from_expires_utc(
    tmp_path: Path, monkeypatch
) -> None:
    exp = 13_403_397_058_000_000  # Chrome epoch microseconds
    db = tmp_path / "cookies.db"
    _make_cookie_db(db, [("chatgpt.com", SESSION_COOKIE, "", b"ENCBLOB", exp)])
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session,
        "_make_decryptor",
        lambda app: (lambda enc: f"DEC[{enc.decode()}]"),
    )
    _pairs, chrome = chatgpt_session._cookie_pairs("testbrowser")
    assert chrome == {
        "cookies": {SESSION_COOKIE: "DEC[ENCBLOB]"},
        "expires": exp / 1_000_000 - chatgpt_session.WEBKIT_EPOCH_DELTA_S,
    }


def test_cookie_pairs_fails_when_no_session_cookie_is_present(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "cookies.db"
    _make_cookie_db(db, [("chatgpt.com", "_cfuvid", "v", b"", 0)])
    monkeypatch.setitem(chatgpt_session.BROWSERS, "testbrowser", (db, "testapp"))
    monkeypatch.setattr(
        chatgpt_session, "_make_decryptor", lambda app: (lambda enc: "")
    )
    with pytest.raises(SystemExit):
        chatgpt_session._cookie_pairs("testbrowser")


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


def test_http_diagnostics_record_safe_request_metadata_not_credentials(
    monkeypatch,
) -> None:
    events: list[dict] = []

    def fake_urlopen(req, timeout=60):
        assert timeout == 12.0
        return _FakeResponse(
            200,
            '{"private_body":"do-not-log"}',
            [
                ("CF-Ray", "ray-123"),
                ("X-Request-ID", "request-456"),
                ("Set-Cookie", "session=do-not-log"),
            ],
        )

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    session = _session(cookie="cookie=do-not-log", token="bearer-do-not-log")
    session._diagnostic = events.append
    session.request_timeout = 12.0
    status, _data = session.call("/backend-api/me", retries=1)

    assert status == 200
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "http_attempt"
    assert event["path"] == "/backend-api/me"
    assert event["status"] == 200
    assert event["outcome"] == "success"
    assert event["cf_ray"] == "ray-123"
    assert event["request_id"] == "request-456"
    serialized = json.dumps(events)
    assert "do-not-log" not in serialized
    assert "Set-Cookie" not in serialized


def test_http_diagnostics_record_each_403_retry_and_backoff(monkeypatch) -> None:
    events: list[dict] = []
    sleeps: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", sleeps.append)
    responses = iter([_http_error(403, "blocked"), _FakeResponse(200, "{}")])

    def fake_urlopen(req, timeout=60):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    session = _session()
    session._diagnostic = events.append
    status, _data = session.call("/backend-api/me", retries=2)

    assert status == 200
    assert sleeps == [2]
    assert [(e["event"], e.get("status")) for e in events] == [
        ("http_attempt", 403),
        ("http_retry", 403),
        ("http_attempt", 200),
    ]
    assert events[1]["backoff_s"] == 2
    assert events[0]["response_bytes"] == len(b"blocked")
    expected_hash = __import__("hashlib").sha256(b"blocked").hexdigest()
    assert events[0]["response_sha256"] == expected_hash
    assert "blocked" not in json.dumps(events)


def test_http_error_fingerprint_preserves_call_error_body(monkeypatch) -> None:
    events: list[dict] = []

    def fake_urlopen(req, timeout=60):
        raise _http_error(404, "not found diagnostic body")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    session = _session()
    session._diagnostic = events.append
    status, data = session.call("/missing", retries=1)

    assert status == 404
    assert data == {"error": "not found diagnostic body"}
    assert events[0]["response_bytes"] == len(b"not found diagnostic body")
    assert "not found diagnostic body" not in json.dumps(events)


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


def _wire_init(
    monkeypatch,
    json_body,
    *,
    pairs=(("cookie", "abc"),),
    chrome=None,
    stored=None,
    set_cookie_headers=(),
) -> list[dict]:
    """Wire Session.__init__'s collaborators: pick_browser, _cookie_pairs
    (the pairs plus Chrome's own session record), load_stored_session, a
    recording store_session, and urlopen's /api/auth/session reply
    (optionally carrying Set-Cookie headers, for the renewal tests).
    Returns the list store_session's calls are recorded into.

    Every existing caller that passes only ``json_body`` keeps the exact
    pre-renewal behaviour: chrome and stored both default to None, so
    choose_session picks neither and pairs (and therefore session.cookie)
    pass through unchanged.
    """
    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(
        chatgpt_session, "_cookie_pairs", lambda browser: (list(pairs), chrome)
    )
    monkeypatch.setattr(chatgpt_session, "load_stored_session", lambda: stored)
    store_calls: list[dict] = []

    def fake_store(record):
        store_calls.append(record)
        return True

    monkeypatch.setattr(chatgpt_session, "store_session", fake_store)

    def fake_urlopen(req, timeout=60):
        header_pairs = [("Set-Cookie", line) for line in set_cookie_headers]
        return _FakeResponse(200, json.dumps(json_body), headers=header_pairs)

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    return store_calls


def test_session_init_succeeds_and_stores_token_and_user_id(monkeypatch) -> None:
    """A normal handshake must populate browser, cookie, token and user_id."""
    _wire_init(monkeypatch, {"accessToken": "tok123", "user": {"id": "user-1"}})
    session = chatgpt_session.Session()
    assert session.browser == "fake-browser"
    assert session.cookie == "cookie=abc"
    assert session.token == "tok123"
    assert session.user_id == "user-1"
    assert session.session_source == "none"
    assert session.session_expires is None
    assert session.renewed_expires is None


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


# ---------------------------------------------------------------------------
# Session.__init__ -- session token renewal end to end
# ---------------------------------------------------------------------------

AUTH_OK = {"accessToken": "tok123", "user": {"id": "user-1"}}


def test_session_init_stores_a_renewed_session_when_it_is_fresher(
    monkeypatch,
) -> None:
    """A Set-Cookie the handshake carries, materially fresher than what was
    just sent, must be stored to the keyring."""
    chrome = {"cookies": {SESSION_COOKIE: "chrome-tok"}, "expires": NOW}
    store_calls = _wire_init(
        monkeypatch,
        AUTH_OK,
        pairs=[(SESSION_COOKIE, "chrome-tok")],
        chrome=chrome,
        set_cookie_headers=[f"{SESSION_COOKIE}=renewed-tok; Max-Age=7776000; Path=/"],
    )
    monkeypatch.setattr(chatgpt_session.time, "time", lambda: NOW)
    session = chatgpt_session.Session()
    assert session.session_source == "chrome"
    assert session.session_expires == NOW
    assert session.renewed_expires == NOW + 7_776_000
    assert len(store_calls) == 1
    assert store_calls[0]["cookies"] == {SESSION_COOKIE: "renewed-tok"}
    assert store_calls[0]["expires"] == NOW + 7_776_000


def test_session_init_does_not_store_when_the_renewal_is_not_meaningfully_later(
    monkeypatch,
) -> None:
    """A re-issue that is not later than what was already sent (or later by
    60 s or less) must not trigger a write to the keyring."""
    chrome = {"cookies": {SESSION_COOKIE: "chrome-tok"}, "expires": NOW + 7_776_000}
    store_calls = _wire_init(
        monkeypatch,
        AUTH_OK,
        pairs=[(SESSION_COOKIE, "chrome-tok")],
        chrome=chrome,
        set_cookie_headers=[f"{SESSION_COOKIE}=chrome-tok; Max-Age=7776000; Path=/"],
    )
    monkeypatch.setattr(chatgpt_session.time, "time", lambda: NOW)
    session = chatgpt_session.Session()
    assert session.renewed_expires == NOW + 7_776_000  # observed
    assert store_calls == []  # but not later than session_expires by more than 60s


def test_session_init_sends_the_keyring_copy_when_it_is_fresher(monkeypatch) -> None:
    """choose_session must run before the handshake: the Cookie header the
    server sees is built from whichever record is fresher, never always
    Chrome's. Fake, synthetic values throughout -- never a real token."""
    chrome = {"cookies": {SESSION_COOKIE: "chrome-tok"}, "expires": NOW}
    stored = {"cookies": {SESSION_COOKIE: "keyring-tok"}, "expires": NOW + 5000}
    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout=60):
        seen["cookie_header"] = req.get_header("Cookie")
        return _FakeResponse(200, json.dumps(AUTH_OK))

    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(
        chatgpt_session,
        "_cookie_pairs",
        lambda browser: ([(SESSION_COOKIE, "chrome-tok"), ("other", "x")], chrome),
    )
    monkeypatch.setattr(chatgpt_session, "load_stored_session", lambda: stored)
    monkeypatch.setattr(chatgpt_session, "store_session", lambda record: True)
    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)

    session = chatgpt_session.Session()
    assert session.session_source == "keyring"
    header = seen["cookie_header"]
    assert f"{SESSION_COOKIE}=keyring-tok" in header
    assert "other=x" in header
    assert "chrome-tok" not in header  # the stale chrome value must not be sent


def test_session_init_fails_cleanly_on_an_http_error_from_the_handshake(
    monkeypatch,
) -> None:
    """A 403 (an expired cookie, most likely) must still reach fail(), not
    an unhandled HTTPError -- and never attempt a renewal off it."""
    store_calls: list[dict] = []
    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(
        chatgpt_session, "_cookie_pairs", lambda browser: ([("cookie", "abc")], None)
    )
    monkeypatch.setattr(chatgpt_session, "load_stored_session", lambda: None)
    monkeypatch.setattr(
        chatgpt_session, "store_session", lambda record: store_calls.append(record)
    )

    def fake_urlopen(req, timeout=60):
        raise _http_error(403, "forbidden")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.Session()
    assert exc.value.code == 1
    assert store_calls == []


def test_session_init_reports_cloudflare_challenge_without_blaming_cookie(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(
        chatgpt_session, "_cookie_pairs", lambda browser: ([("cookie", "abc")], None)
    )
    monkeypatch.setattr(chatgpt_session, "load_stored_session", lambda: None)

    headers = Message()
    headers["CF-Mitigated"] = "challenge"
    headers["Server"] = "cloudflare"

    def fake_urlopen(req, timeout=60):
        raise urllib.error.HTTPError(
            "https://chatgpt.com/api/auth/session",
            403,
            "Forbidden",
            headers,
            io.BytesIO(b"challenge page"),
        )

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.Session()
    assert exc.value.code == 1
    assert "Cloudflare challenge" in exc.value.detail
    err = capsys.readouterr().err
    assert "Cloudflare challenge" in err
    assert "cookie may be expired" not in err


def test_session_init_fails_cleanly_on_a_transport_failure(monkeypatch) -> None:
    """A DNS failure or refused connection (URLError, no response at all)
    must also reach fail(), with no headers to even look at for a renewal."""
    monkeypatch.setattr(
        chatgpt_session, "pick_browser", lambda choice="auto": "fake-browser"
    )
    monkeypatch.setattr(
        chatgpt_session, "_cookie_pairs", lambda browser: ([("cookie", "abc")], None)
    )
    monkeypatch.setattr(chatgpt_session, "load_stored_session", lambda: None)

    def fake_urlopen(req, timeout=60):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        chatgpt_session.Session()
    assert exc.value.code == 1


def test_pick_browser_rejects_an_unknown_name_with_a_clear_message(capsys) -> None:
    """An unknown browser used to surface later as a raw KeyError."""
    with pytest.raises(SystemExit):
        chatgpt_session.pick_browser("firefox")
    assert "unknown browser 'firefox'" in capsys.readouterr().err
