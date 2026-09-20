"""Reuse a local browser's logged-in chatgpt.com session from the terminal.

Shared by the `chatgpt-refresh` and `chatgpt-chats` tools. It reads the
chatgpt.com cookies from a local Chromium/Chrome profile (decrypting them with
the OS-crypt key from the GNOME keyring) and mints a short-lived access token
via /api/auth/session, so terminal scripts can call the same backend endpoints
the web app uses. Nothing is written to disk; the durable credential stays in
the browser's own cookie/keyring store -- except the renewed session token
described next, which is this machine's own keyring, not a file.

Session token renewal: every /api/auth/session call re-issues
__Secure-next-auth.session-token.* with a fresh 90-day Max-Age (measured
2026-09-21). Session.__init__ compares whatever that call just sent
(Chrome's own jar, or an earlier renewal already in the keyring) against
what the server just re-issued, and stores the later one in this machine's
own keyring -- service org.freedesktop.secrets, attributes
{"application": "chatgpt-web-operations", "purpose":
"chatgpt-session-token"} -- whenever it is more than 60 seconds fresher.
No cron job or timer drives this: every Session built anywhere in this
skill renews it as a side effect, so the daily health check alone keeps
the token alive indefinitely, long after Chrome itself stops being asked.
Set CHATGPT_SESSION_STORE=0 to disable the keyring side entirely and use
only Chrome's own jar, as before this existed. Chrome's own cookie
database is never written by any of this -- only read, as always.

Requires running as the desktop user (session D-Bus + unlocked GNOME keyring)
with the browser logged in to chatgpt.com.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, MutableMapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, NoReturn

BASE = "https://chatgpt.com"
UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# name -> (cookie DB, GNOME-keyring "application" attribute for the OS-crypt key)
BROWSERS = {
    "chromium": (
        Path.home() / ".config" / "chromium" / "Default" / "Cookies",
        "chromium",
    ),
    "chrome": (
        Path.home() / ".config" / "google-chrome" / "Default" / "Cookies",
        "chrome",
    ),
}

# The only load-bearing cookie (measured 2026-09-20 by removing cookies from
# the jar in memory): chunked as .0, .1, ... -- a future response may carry a
# different number of chunks, so every function below keys off this prefix
# rather than assuming exactly two.
SESSION_COOKIE_PREFIX = "__Secure-next-auth.session-token"

# Chrome stores expires_utc in microseconds since 1601-01-01 (the Windows
# FILETIME epoch); 0 means a session-scoped cookie, gone when Chrome closes.
# probe_cookies.py and chatgpt_cookies.py each keep their own copy of this
# same constant; the value is the same everywhere by definition (Chrome's own
# epoch), so nothing here depends on which module's copy is used.
WEBKIT_EPOCH_DELTA_S = 11_644_473_600

# Session token renewal, kept in this machine's own keyring -- see the
# module docstring. Set to "0" to make load_stored_session/store_session
# behave as if the keyring held nothing, without ever touching the bus.
SESSION_STORE_ENV = "CHATGPT_SESSION_STORE"
SESSION_ITEM_ATTRIBUTES: dict[str, str] = {
    "application": "chatgpt-web-operations",
    "purpose": "chatgpt-session-token",
}
SESSION_ITEM_LABEL = "chatgpt.com session token (chatgpt-web-operations)"

_prog = "chatgpt"


def set_prog(name: str) -> None:
    """Set the program name used in error messages."""
    global _prog
    _prog = name


def fail(msg: str) -> NoReturn:
    print(f"{_prog}: {msg}", file=sys.stderr)
    raise SystemExit(1)


def ensure_desktop_env(environ: MutableMapping[str, str] | None = None) -> None:
    """Default the D-Bus session-bus variables cron never sets.

    A cron job's environment has no ``DBUS_SESSION_BUS_ADDRESS``: nothing
    logged this user in on that terminal, so nothing exported it. Without
    that variable, libdbus falls back to autolaunching a bus, and the
    autolaunch path needs ``$DISPLAY`` for its X11 fallback, so it dies with
    "Unable to autolaunch a dbus-daemon without a $DISPLAY for X11" -- even
    though the real per-user bus is already running and reachable, because
    ``loginctl`` linger keeps it alive with nobody logged in. Before this
    function existed, four maintenance scripts under ``~/.local/bin`` each
    exported ``XDG_RUNTIME_DIR`` or ``DBUS_SESSION_BUS_ADDRESS`` themselves
    before calling anything in this module. Defaulting both here, at the
    top of :func:`_keyring_password`, where the bus is first touched, means
    no caller has to know the bus exists, let alone how to address it.

    Each variable is defaulted independently and never overwritten once
    set, so calling this twice, or with one variable already set by the
    caller, is a no-op for that variable. ``environ`` defaults to
    ``os.environ`` (so the real process picks up the default) but takes any
    mutable string mapping, such as a plain ``dict``, so this can be unit
    tested without touching the real process environment.
    """
    if environ is None:
        environ = os.environ
    xdg_runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir is None:
        xdg_runtime_dir = f"/run/user/{os.getuid()}"
        environ["XDG_RUNTIME_DIR"] = xdg_runtime_dir
    if "DBUS_SESSION_BUS_ADDRESS" not in environ:
        environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={xdg_runtime_dir}/bus"


def _keyring_password(app: str) -> bytes:
    ensure_desktop_env()
    import dbus  # system python3 provides python-dbus

    bus = dbus.SessionBus()
    svc_obj = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
    service = dbus.Interface(svc_obj, "org.freedesktop.Secret.Service")
    _, session = service.OpenSession("plain", dbus.String("", variant_level=1))
    unlocked, locked = service.SearchItems(
        {"application": app, "xdg:schema": "chrome_libsecret_os_crypt_password_v2"}
    )
    items = list(unlocked) + list(locked)
    if locked:
        service.Unlock(locked)
    for path in items:
        item = dbus.Interface(
            bus.get_object("org.freedesktop.secrets", path),
            "org.freedesktop.Secret.Item",
        )
        try:
            return bytes(item.GetSecret(session)[2])
        except Exception:
            continue
    fail(f"'{app}' key not found in the GNOME keyring (is the keyring unlocked?)")


def _make_decryptor(app: str):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    def derive(pw: bytes) -> bytes:
        return PBKDF2HMAC(
            algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
        ).derive(pw)

    keys = {b"v10": derive(b"peanuts"), b"v11": derive(_keyring_password(app))}

    def decrypt(enc: bytes) -> str:
        key = keys.get(enc[:3])
        if key is None:
            fail(f"unexpected cookie encryption version {enc[:3]!r}")
        d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        pt = d.update(enc[3:]) + d.finalize()
        pad = pt[-1]
        pt = pt[:-pad] if 0 < pad <= 16 else pt
        for cand in (pt, pt[32:]):  # newer Chromium prepends a 32-byte domain hash
            try:
                s = cand.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if s and all(32 <= ord(c) < 127 for c in s):
                return s
        fail("could not decode a decrypted cookie value")

    return decrypt


# ---------------------------------------------------------------------------
# Session token renewal: this machine's own keyring, over raw D-Bus
# ---------------------------------------------------------------------------


def _session_store_enabled(environ: MutableMapping[str, str] | None = None) -> bool:
    """False exactly when CHATGPT_SESSION_STORE=0 -- the kill switch that
    makes load_stored_session/store_session behave as if the keyring held
    nothing, without ever opening the session bus (module docstring)."""
    if environ is None:
        environ = os.environ
    return environ.get(SESSION_STORE_ENV) != "0"


def load_stored_session() -> dict[str, Any] | None:
    """The session record ``store_session`` last saved to this machine's
    own keyring, or ``None`` when there is none yet, the store is disabled
    (``CHATGPT_SESSION_STORE=0``), or anything about reading it fails.

    Searched by ``SESSION_ITEM_ATTRIBUTES`` over the default collection --
    the same ``org.freedesktop.secrets`` service ``_keyring_password``
    already talks to for the Chrome OS-crypt key -- with a locked item
    unlocked first, exactly as there. Never raises: a missing keyring
    daemon, a collection that cannot be unlocked without a prompt, or a
    secret that is not the JSON ``store_session`` wrote are all just
    "nothing usable is stored yet" to a caller, so ``Session.__init__`` can
    call this unconditionally on every run without a guard of its own.
    """
    if not _session_store_enabled():
        return None
    try:
        ensure_desktop_env()
        import dbus

        bus = dbus.SessionBus()
        svc_obj = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
        service = dbus.Interface(svc_obj, "org.freedesktop.Secret.Service")
        _, session = service.OpenSession("plain", dbus.String("", variant_level=1))
        unlocked, locked = service.SearchItems(dict(SESSION_ITEM_ATTRIBUTES))
        items = list(unlocked) + list(locked)
        if locked:
            service.Unlock(locked)
        for path in items:
            item = dbus.Interface(
                bus.get_object("org.freedesktop.secrets", path),
                "org.freedesktop.Secret.Item",
            )
            try:
                secret = bytes(item.GetSecret(session)[2])
                record = json.loads(secret.decode("utf-8"))
            except Exception:
                continue
            if isinstance(record, dict) and isinstance(record.get("cookies"), dict):
                return record
        return None
    except Exception:
        return None


def store_session(record: dict[str, Any]) -> bool:
    """Save ``record`` (``renewed_session``'s shape) to this machine's own
    keyring, replacing any earlier copy (``CreateItem`` with ``replace``
    true). Returns ``False``, and never raises, on any failure -- a store
    that could not happen must never end a run that only needed the
    session for reading. On failure a one-line warning goes to stderr
    naming the exception type only, never a cookie or token value.
    Disabled the same way ``load_stored_session`` is
    (``CHATGPT_SESSION_STORE=0``).
    """
    if not _session_store_enabled():
        return False
    try:
        ensure_desktop_env()
        import dbus

        bus = dbus.SessionBus()
        svc_obj = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
        service = dbus.Interface(svc_obj, "org.freedesktop.Secret.Service")
        _, session = service.OpenSession("plain", dbus.String("", variant_level=1))
        collection = dbus.Interface(
            bus.get_object(
                "org.freedesktop.secrets", "/org/freedesktop/secrets/aliases/default"
            ),
            "org.freedesktop.Secret.Collection",
        )
        properties = dbus.Dictionary(
            {
                "org.freedesktop.Secret.Item.Label": SESSION_ITEM_LABEL,
                "org.freedesktop.Secret.Item.Attributes": dbus.Dictionary(
                    SESSION_ITEM_ATTRIBUTES, signature="ss"
                ),
            },
            signature="sv",
        )
        secret_bytes = json.dumps(record).encode("utf-8")
        secret = dbus.Struct(
            (session, dbus.ByteArray(b""), dbus.ByteArray(secret_bytes), "text/plain"),
            signature="oayss",
        )
        collection.CreateItem(properties, secret, True)
        return True
    except Exception as exc:
        print(
            f"{_prog}: could not store the renewed session in the keyring "
            f"({type(exc).__name__}); the next run will use Chrome's own copy",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# Session token renewal: pure functions (tested without a bus or a socket)
# ---------------------------------------------------------------------------


def chrome_session_record(
    rows: Iterable[tuple[str, str, float | None]],
) -> dict[str, Any] | None:
    """``{"cookies": {...}, "expires": epoch}`` for the
    ``__Secure-next-auth.session-token*`` chunks among ``rows`` -- ``(name,
    value, expiry epoch seconds or None)``, already converted from
    whatever Chrome-epoch form the caller's own query used: ``_cookie_pairs``
    converts its ``expires_utc`` column itself, and ``chatgpt_cookies.export``
    has already converted it by the time its cookie dicts exist. ``None``
    when no such chunk is present, since Chrome then carries no session at
    all worth comparing against a stored one.

    The record's own expiry is the chunks' minimum -- the same rule
    ``probe_cookies.session_horizon`` applies to a stored jar, because one
    chunk expiring earlier makes the whole token unusable even when
    another chunk's copy looks fresher.
    """
    cookies: dict[str, str] = {}
    epochs: list[float] = []
    for name, value, expires in rows:
        if not name.startswith(SESSION_COOKIE_PREFIX):
            continue
        cookies[name] = value
        if expires is not None:
            epochs.append(float(expires))
    if not cookies:
        return None
    return {"cookies": cookies, "expires": min(epochs) if epochs else None}


def _cookie_expiry(morsel: Any, now: float) -> float | None:
    """The Unix epoch a ``Set-Cookie`` ``Morsel``'s ``Max-Age`` or
    ``Expires`` attribute names, ``Max-Age`` preferred over ``Expires``
    when both are present (RFC 6265), or ``None`` when it carries neither
    (or the value each has is not parseable)."""
    max_age = morsel["max-age"]
    if max_age:
        try:
            return now + float(max_age)
        except ValueError:
            pass
    expires = morsel["expires"]
    if expires:
        try:
            dt = parsedate_to_datetime(expires)
        except (TypeError, ValueError, IndexError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def renewed_session(set_cookie_headers: list[str], now: float) -> dict[str, Any] | None:
    """The session-token chunks a set of raw ``Set-Cookie`` header lines
    just re-issued, and when they expire:
    ``{"cookies": {"<chunk name>": "<value>", ...}, "expires": epoch,
    "stored_at": now, "source": "api/auth/session"}``. ``None`` when none
    of the lines names a ``__Secure-next-auth.session-token*`` cookie, or
    when the one(s) that do carry neither ``Max-Age`` nor ``Expires`` (a
    record this module could never later compare for freshness is not
    worth returning).

    Measured 2026-09-21: ``GET /api/auth/session`` re-issues the token
    with ``Max-Age=7776000`` (90 days). Every chunk found is kept, not
    just ``.0``/``.1``: a future response may carry a different number of
    chunks, so this stores whatever the response gives and lets
    ``apply_session``/``apply_session_to_jar`` replace the whole set. The
    record's own ``expires`` is the earliest of the chunks that had a
    usable expiry, the same "the soonest chunk is the one that matters"
    rule ``probe_cookies.session_horizon`` applies to a stored jar.
    """
    cookies: dict[str, str] = {}
    epochs: list[float] = []
    for line in set_cookie_headers:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(line)
        except Exception:
            continue
        for name, morsel in jar.items():
            if not name.startswith(SESSION_COOKIE_PREFIX):
                continue
            cookies[name] = morsel.value
            epoch = _cookie_expiry(morsel, now)
            if epoch is not None:
                epochs.append(epoch)
    if not cookies or not epochs:
        return None
    return {
        "cookies": cookies,
        "expires": min(epochs),
        "stored_at": now,
        "source": "api/auth/session",
    }


def choose_session(
    chrome: dict[str, Any] | None, stored: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str]:
    """Which session record is fresher: ``chrome`` (this browser's own
    jar) or ``stored`` (the keyring's copy) -- both shaped
    ``{"cookies": {...}, "expires": epoch|None}`` -- and a source label,
    one of ``"chrome"``, ``"keyring"`` or ``"none"``.

    ``stored`` wins only when its ``expires`` is known and strictly later
    than ``chrome``'s (or ``chrome`` has none at all): a record whose
    expiry cannot be compared is never preferred over one that can be,
    and an exact tie keeps ``chrome`` -- it is the browser's own login,
    the one the user can always refresh by hand, so ties default to it
    rather than to a stored copy nothing renews until the next successful
    call. Either side may be ``None`` (no session found there at all);
    both ``None`` returns ``(None, "none")``.
    """
    if chrome is None and stored is None:
        return None, "none"
    if stored is None:
        return chrome, "chrome"
    if chrome is None:
        return stored, "keyring"
    chrome_exp = chrome.get("expires")
    stored_exp = stored.get("expires")
    if stored_exp is not None and (chrome_exp is None or stored_exp > chrome_exp):
        return stored, "keyring"
    return chrome, "chrome"


def apply_session(
    pairs: list[tuple[str, str]], chosen: dict[str, Any]
) -> list[tuple[str, str]]:
    """``pairs`` (name, value) with every
    ``__Secure-next-auth.session-token*`` pair replaced by ``chosen``'s
    set (``chosen["cookies"]``): a chunk ``pairs`` carries but ``chosen``
    does not is dropped, one ``chosen`` names but ``pairs`` lacks is
    appended, and every other cookie -- and every kept chunk's original
    position -- stays exactly where it was, so the header built from the
    result reads the same as before except for the token itself.
    """
    cookies = chosen.get("cookies") or {}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in pairs:
        if name.startswith(SESSION_COOKIE_PREFIX):
            if name in cookies:
                out.append((name, cookies[name]))
                seen.add(name)
            continue  # drop a chunk `chosen` does not carry
        out.append((name, value))
    for name, value in cookies.items():
        if name not in seen:
            out.append((name, value))
    return out


def apply_session_to_jar(
    jar: list[dict[str, Any]], chosen: dict[str, Any]
) -> list[dict[str, Any]]:
    """The same substitution ``apply_session`` makes to a plain Cookie
    header, made instead to a Playwright-shaped cookie jar (
    ``chatgpt_cookies.export``'s return shape: dicts with ``name``,
    ``value``, ``domain``, ``path``, ``secure``, ``httpOnly``,
    ``sameSite`` and, for a persistent cookie, ``expires``) -- one rule
    for both call sites (``chatgpt_cookies.export`` and
    ``chatgpt_client._patch_cookie_export``'s replacement), not two that
    could drift apart.

    Every existing ``__Secure-next-auth.session-token*`` entry not in
    ``chosen["cookies"]`` is dropped; every kept or added chunk gets
    ``chosen``'s value and ``chosen["expires"]``; a chunk ``chosen`` names
    that ``jar`` lacks is appended, shaped like whichever session-token
    cookie is already in ``jar`` (same domain/path/secure/httpOnly/
    sameSite), or a reasonable default when ``jar`` carries no chunk at
    all to copy from. ``jar`` is modified in place and returned, so a
    caller may use either form.
    """
    cookies = chosen.get("cookies") or {}
    expires = chosen.get("expires")
    template = next(
        (c for c in jar if str(c.get("name", "")).startswith(SESSION_COOKIE_PREFIX)),
        None,
    ) or {
        "domain": ".chatgpt.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cookie in jar:
        name = str(cookie.get("name", ""))
        if name.startswith(SESSION_COOKIE_PREFIX):
            if name not in cookies:
                continue  # drop a chunk `chosen` does not carry
            cookie["value"] = cookies[name]
            if expires is not None:
                cookie["expires"] = expires
            else:
                cookie.pop("expires", None)
            seen.add(name)
        kept.append(cookie)
    for name, value in cookies.items():
        if name in seen:
            continue
        new_cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": template.get("domain", ".chatgpt.com"),
            "path": template.get("path", "/"),
            "secure": template.get("secure", True),
            "httpOnly": template.get("httpOnly", True),
            "sameSite": template.get("sameSite", "Lax"),
        }
        if expires is not None:
            new_cookie["expires"] = expires
        kept.append(new_cookie)
    jar[:] = kept
    return jar


# ---------------------------------------------------------------------------
# The cookie jar: pairs, header, and what the jar says about Chrome's token
# ---------------------------------------------------------------------------


def _cookie_pairs(browser: str) -> tuple[list[tuple[str, str]], dict[str, Any] | None]:
    """``(name, value)`` for every chatgpt.com cookie in one browser
    profile, in SQL name order, plus what the jar itself says about
    Chrome's own copy of the session token: a
    ``{"cookies": {...}, "expires": epoch|None}`` record
    (``chrome_session_record``), or ``None`` if that is impossible (never
    happens when this returns without failing -- see below).

    ``_cookie_header`` is the joined-string form of the pairs, kept for
    compatibility; ``Session.__init__`` uses the pairs directly, so it can
    substitute a fresher session before joining them into a header
    (module docstring, "Session token renewal").
    """
    db, app = BROWSERS[browser]
    if not db.exists():
        fail(f"{browser} cookie DB not found at {db}")
    decrypt = _make_decryptor(app)
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        shutil.copy2(db, tmp.name)  # copy: the live DB is locked by the browser
        con = sqlite3.connect(tmp.name)
        rows = con.execute(
            "SELECT name, value, encrypted_value, expires_utc FROM cookies "
            "WHERE host_key LIKE '%chatgpt.com' ORDER BY name"
        ).fetchall()
        con.close()
    pairs: list[tuple[str, str]] = []
    record_rows: list[tuple[str, str, float | None]] = []
    have_session = False
    for name, value, enc, expires_utc in rows:
        text = decrypt(enc) if enc else value
        pairs.append((name, text))
        if name.startswith(SESSION_COOKIE_PREFIX):
            have_session = True
            epoch = (
                expires_utc / 1_000_000 - WEBKIT_EPOCH_DELTA_S if expires_utc else None
            )
            record_rows.append((name, text, epoch))
    if not have_session:
        fail(
            f"no ChatGPT session cookie in {browser} (is it logged in to chatgpt.com?)"
        )
    return pairs, chrome_session_record(record_rows)


def _cookie_header(browser: str) -> str:
    """Cookie header with every chatgpt.com cookie for one browser profile.

    Sending the whole jar (session token plus Cloudflare's __cf_bm / cf_clearance
    and the oai-* cookies) makes the request look like the browser; sending only
    the session token trips Cloudflare's bot check.

    A thin joined-string view of ``_cookie_pairs``, kept so any caller (or
    test) that wants the plain header on its own still gets it, unchanged
    by which session (Chrome's or the keyring's) ``Session.__init__``
    itself goes on to choose.
    """
    pairs, _chrome = _cookie_pairs(browser)
    return "; ".join(f"{name}={value}" for name, value in pairs)


def pick_browser(choice: str = "auto") -> str:
    """Return the browser to use; 'auto' picks one with a ChatGPT session."""
    if choice != "auto":
        if choice not in BROWSERS:
            fail(f"unknown browser {choice!r}; choose one of {', '.join(BROWSERS)}")
        return choice
    for name, (db, _) in BROWSERS.items():
        if not db.exists():
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
                shutil.copy2(db, tmp.name)
                con = sqlite3.connect(tmp.name)
                try:
                    row = con.execute(
                        "SELECT 1 FROM cookies WHERE host_key LIKE '%chatgpt.com' "
                        "AND name LIKE '__Secure-next-auth.session-token%' LIMIT 1"
                    ).fetchone()
                finally:
                    con.close()
            if row:
                return name
        except sqlite3.Error:
            continue
    fail("no logged-in ChatGPT session found in chromium or chrome")


class Session:
    """An authenticated ChatGPT web session driven from the terminal.

    Every session this class builds also renews the persistent session
    token (module docstring, "Session token renewal"): ``/api/auth/session``
    re-issues ``__Secure-next-auth.session-token.*`` with a fresh 90-day
    ``Max-Age`` on every call, and ``__init__`` stores whichever copy --
    Chrome's own jar or this machine's keyring -- is fresher into the
    keyring whenever the server just handed back a materially later
    expiry. No cron job or other trigger is needed for this: the daily
    health check already builds a ``Session`` once a day, and that alone
    keeps the token alive indefinitely. Set ``CHATGPT_SESSION_STORE=0`` to
    use only Chrome's own jar, as before this existed.
    """

    def __init__(self, browser: str = "auto"):
        self.browser = pick_browser(browser)
        pairs, chrome = _cookie_pairs(self.browser)
        stored = load_stored_session()
        chosen, source = choose_session(chrome, stored)
        self.session_source = source
        self.session_expires = chosen.get("expires") if chosen else None
        if chosen is not None:
            pairs = apply_session(pairs, chosen)
        self.cookie = "; ".join(f"{name}={value}" for name, value in pairs)

        status: int = 0
        data: Any = {}
        headers: Any = None
        try:
            status, text, headers = self._request("/api/auth/session")
            data = json.loads(text)
        except urllib.error.HTTPError as e:
            status, headers = e.code, e.headers
        except (urllib.error.URLError, json.JSONDecodeError):
            pass
        info: dict = data if isinstance(data, dict) else {}
        self.token = info.get("accessToken") if status == 200 else None
        self.user_id = (info.get("user") or {}).get("id")

        self.renewed_expires: float | None = None
        if headers is not None:
            renewed = renewed_session(headers.get_all("Set-Cookie") or [], time.time())
            if renewed is not None:
                self.renewed_expires = renewed["expires"]
                if self.session_expires is None or (
                    renewed["expires"] - self.session_expires > 60
                ):
                    store_session(renewed)

        if not self.token or not self.user_id:
            fail(
                f"could not authenticate (HTTP {status}); session cookie may be expired"
            )

    def _request(
        self,
        path: str,
        method: str = "GET",
        payload=None,
        timeout: float = 60,
    ) -> tuple[int, str, Any]:
        """One HTTP attempt against chatgpt.com, no retry: builds this
        session's Cookie/bearer/User-Agent headers and returns ``(status,
        response body text, response headers)`` on success. Raises
        ``urllib.error.HTTPError`` or ``URLError`` exactly as ``urlopen()``
        does on anything else -- this wraps a single ``urlopen()`` call and
        nothing more.

        The one place this module calls ``urlopen()``. ``call()`` wraps
        this with its own retry loop, exactly as it always retried;
        ``Session.__init__`` calls it directly, once, so it can read the
        ``Set-Cookie`` headers of the ``/api/auth/session`` handshake --
        headers ``call()``'s ``(status, data)`` return value never carried
        (see ``renewed_session``, module docstring "Session token
        renewal").
        """
        headers = {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Accept": "application/json",
        }
        token = getattr(self, "token", None)
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            BASE + path, headers=headers, method=method, data=body
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), r.headers

    def call(
        self,
        path: str,
        method: str = "GET",
        payload=None,
        raw: bool = False,
        retries: int = 3,
    ):
        """Call a chatgpt.com endpoint. Returns (status, parsed-json-or-text).

        Cloudflare rate-limits bursts of requests with a 403 HTML page, so
        403/429/5xx are retried with a short backoff before giving up.
        """
        last = (0, {"error": "no attempt made"})
        for attempt in range(retries):
            try:
                status, text, _headers = self._request(path, method, payload)
                return status, (text if raw else json.loads(text))
            except urllib.error.HTTPError as e:
                last = (e.code, {"error": e.read().decode()[:200]})
                if e.code not in (403, 429) and e.code < 500:
                    return last
            except urllib.error.URLError as e:
                last = (0, {"error": str(e)[:200]})
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # 2s, then 4s
        return last
