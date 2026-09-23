"""Tests for health.py (TESTING.md kind "unit", tier T0).

health.py copies preflight.py's own control flow (host, link, account, run)
by calling its functions, never re-implementing them, so this file does not
re-test preflight's own checks -- test_preflight.py already does. What is
new here: the fifth "health" group (session token, sandbox, read
endpoints), the "facts" block --json writes, the health group's place in the
rendered report,
and main()'s end-to-end composition, including the link-down skip path.

Every pure verdict function is tested over plain data. The fetch_* wrappers
are tested over a fake session -- ``.session.call(path)`` returning a
canned ``(status, body)`` -- and a fake ``cc`` (chatgpt_client) namespace,
copying tests/test_preflight.py's ``_FakeSession``/``_fake_cc`` style
(never a real ChatGPTSession/BrowserSender, never a socket: conftest.py's
autouse guard fails any T0 test that tries).

``probe_send_gates.fetch`` and ``probe_cookies.read_jar`` are the two real
I/O boundaries health.py adds beyond what preflight.py already has: the
first is faked the same way test_preflight.py fakes it (a ``fake_psg``
fixture, here patching *both* preflight's and health's own ``psg``
reference); the second is exercised for real against a temporary
Chrome-shaped sqlite cookie database, because it is local-file I/O, not
network, and a real fixture is more convincing than a mock of two small
pure functions.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import health  # noqa: E402
import preflight  # noqa: E402
import probe_cookies  # noqa: E402
import probe_send_gates  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fakes and fixtures
# ---------------------------------------------------------------------------

SANDBOX = {"id": "g-p-testsandbox", "name": "rp-test-sandbox"}


class _Backend:
    """``session.session.call`` over canned responses, matched by prefix.

    Order matters: ``gizmos/<id>/conversations`` starts with the shorter
    ``gizmos/<id>`` prefix, so a response dict must list the longer,
    more specific path first or a conversations request would silently
    answer with the gizmo's own response. Every helper below that builds
    one keeps that order.
    """

    def __init__(self, responses: dict[str, tuple[int, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def call(self, path: str) -> tuple[int, Any]:
        self.calls.append(path)
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return 404, {"error": "no such fake path: " + path}


class _FakeSession:
    def __init__(self, backend: _Backend) -> None:
        self.session = backend


class _FnSession:
    """A session whose ``.session.call`` is one function: the simplest fake
    for edge cases that do not need path-based branching."""

    def __init__(self, responder: Any) -> None:
        self.session = SimpleNamespace(call=responder)


class _FakeSenderOK:
    """Stands in for chatgpt_client.BrowserSender, its public surface only
    (context manager + ``probe_composer``) -- see
    tests/test_preflight.py's own copy and
    ``test_the_fake_senders_offer_nothing_the_real_sender_does_not`` there
    for why nothing more is offered."""

    def __init__(self, browser: str) -> None:
        self.browser = browser

    def __enter__(self) -> _FakeSenderOK:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def probe_composer(self) -> str:
        return "https://chatgpt.com/"


class _FakeSenderBlocked:
    """The composer never appeared: the one failure preflight's login fix
    fits."""

    def __init__(self, browser: str) -> None:
        self.browser = browser

    def __enter__(self) -> _FakeSenderBlocked:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def probe_composer(self) -> str:
        raise RuntimeError(
            "composer did not appear at https://chatgpt.com/ (logged out or challenged)"
        )


class _FakeCS:
    """Stands in for the vendored chatgpt_session module: ``BROWSERS``,
    which fetch_session_token/facts_of reach through ``cc_mod._helpers()``,
    exactly ``probe_cookies.py``'s own ``cs = cc._helpers()`` pattern, and
    ``load_stored_session`` for the keyring side of the session-token
    horizon (chatgpt_session.py, "Session token renewal"), defaulting to
    "nothing stored" so a test that does not care about it sees the
    pre-renewal, Chrome-only behaviour."""

    def __init__(
        self,
        browsers: dict[str, tuple[str, str]],
        stored_session: dict[str, Any] | None = None,
    ) -> None:
        self.BROWSERS = browsers
        self._stored_session = stored_session

    def load_stored_session(self) -> dict[str, Any] | None:
        return self._stored_session


def _fake_cc(
    session: Any,
    *,
    available_mb: float = 8000.0,
    min_available_mb: float = 4000.0,
    pids: frozenset[int] = frozenset(),
    max_browsers: int = 1,
    sender_cls: type = _FakeSenderOK,
    browsers: dict[str, tuple[str, str]] | None = None,
    stored_session: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """A minimal chatgpt_client stand-in, a healthy host by default -- the
    same shape tests/test_preflight.py's own ``_fake_cc`` uses, plus
    ``_helpers().BROWSERS``/``load_stored_session`` for
    fetch_session_token/facts_of. The default browser entry points at a
    file that does not exist, so a test that does not care about the
    session-token check gets a deliberate, visible "block" rather than a
    silent, accidental "ok".
    """
    cs = _FakeCS(
        browsers
        if browsers is not None
        else {"chrome": ("/nonexistent/Cookies", "chrome")},
        stored_session=stored_session,
    )
    ns = SimpleNamespace(
        available_mb=lambda: available_mb,
        MIN_AVAILABLE_MB=min_available_mb,
        scripted_browser_pids=lambda: set(pids),
        MAX_BROWSERS=max_browsers,
        ChatGPTSession=lambda browser, **kwargs: session,
        BrowserSender=sender_cls,
        ensure_desktop_env_calls=[],
        _helpers=lambda: cs,
    )
    ns.ensure_desktop_env = lambda: ns.ensure_desktop_env_calls.append(True)
    return ns


class _FakeSecretsBusResult:
    def __init__(self, owner: bool = True) -> None:
        self._owner = owner

    def name_has_owner(self, name: str) -> bool:
        return self._owner

    def close(self) -> None:
        pass


def _install_fake_dbus(monkeypatch: pytest.MonkeyPatch, *, bus: Any) -> None:
    """A fake ``dbus`` module in ``sys.modules``, so ``fetch_keyring_bus``
    (called inside preflight.host_checks) never reaches the real session
    bus. Mirrors tests/test_preflight.py's own helper of the same name."""
    fake = types.ModuleType("dbus")
    fake.SessionBus = lambda: bus
    monkeypatch.setitem(sys.modules, "dbus", fake)


@pytest.fixture
def clean_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiet, four-core, fully-tooled host with a reachable keyring bus,
    for tests that are not about the host group itself."""
    monkeypatch.setattr(preflight.os, "getloadavg", lambda: (0.5,))
    monkeypatch.setattr(preflight.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: object())
    _install_fake_dbus(monkeypatch, bus=_FakeSecretsBusResult(owner=True))


REAL_WIRELESS_SAMPLE = (
    "Inter-| sta-|   Quality        |   Discarded packets               "
    "| Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc "
    "| beacon | 22\n"
    " wlan1: 0000   62.  -48.  -256        0      0      0      0      0"
    "        0\n"
)

DISCONNECTED_WIRELESS_SAMPLE = (
    "Inter-| sta-|   Quality        |   Discarded packets               "
    "| Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc "
    "| beacon | 22\n"
    " wlan0: 0000    0.    0.    0.       0      0      0      0        0"
    "        0\n"
)


@pytest.fixture
def linked_wireless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "wireless"
    p.write_text(REAL_WIRELESS_SAMPLE)
    monkeypatch.setattr(preflight, "WIRELESS_PATH", p)
    return p


@pytest.fixture
def dead_wireless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "wireless"
    p.write_text(DISCONNECTED_WIRELESS_SAMPLE)
    monkeypatch.setattr(preflight, "WIRELESS_PATH", p)
    return p


@pytest.fixture
def fake_psg(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replaces *both* preflight's and health's own ``probe_send_gates``
    reference -- preflight.account_checks uses the first, health.facts_of's
    gates fact uses the second -- so neither ever reaches the real network
    fetch ``probe_send_gates.fetch`` makes with raw ``urllib``."""
    ns = SimpleNamespace(
        fetch=lambda session: {
            "proofofwork": {"required": True},
            "turnstile": {"required": True},
            "so": {"required": True},
        },
        gates_from=probe_send_gates.gates_from,
    )
    monkeypatch.setattr(preflight, "psg", ns)
    monkeypatch.setattr(health, "psg", ns)
    return ns


@pytest.fixture
def sandbox_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Points health.SANDBOX_FILE at a throwaway sandbox.json, so tests
    never depend on the real tests/live/sandbox.json's exact id."""
    path = tmp_path / "sandbox.json"
    path.write_text(json.dumps(SANDBOX))
    monkeypatch.setattr(health, "SANDBOX_FILE", path)
    return SANDBOX


def _webkit_expiry(days_from_now: float) -> int:
    """A Chrome ``expires_utc`` value ``days_from_now`` days from now."""
    epoch = time.time() + days_from_now * 86400
    return int((epoch + probe_cookies.WEBKIT_EPOCH_DELTA_S) * 1_000_000)


def _make_cookie_db(path: Path, rows: list[tuple[str, str, bytes, int]]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (name TEXT, host_key TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    con.executemany(
        "INSERT INTO cookies (name, host_key, encrypted_value, expires_utc) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


@pytest.fixture
def cookie_db(tmp_path: Path) -> Path:
    """A real Chrome-shaped cookie database with one session-token row, 45
    days from expiry -- lets fetch_session_token/facts_of exercise the
    real probe_cookies.read_jar/session_horizon path end to end instead of
    a mock of it."""
    path = tmp_path / "Cookies"
    _make_cookie_db(
        path,
        [
            (
                "__Secure-next-auth.session-token.0",
                ".chatgpt.com",
                b"opaque-blob",
                _webkit_expiry(45.0),
            )
        ],
    )
    return path


MODELS_PAYLOAD = {
    "versions": [
        {
            "id": "latest",
            "intelligence_presets": [
                {"id": 0, "title": "Instant", "model_slug": "gpt-5-6-instant"},
                {
                    "id": 6,
                    "title": "Extra High",
                    "model_slug": "gpt-5-6-thinking",
                    "thinking_effort": "max",
                },
            ],
        }
    ],
    "models": [{"slug": "gpt-5-6-instant", "configurable_thinking_effort": False}],
}

SETTINGS_PAYLOAD = {
    "settings": {
        "last_used_model_config": {
            "slugs": {"web": "gpt-5-6-thinking"},
            "juices": {"web": {"gpt-5-6-thinking": "max"}},
        }
    }
}


def _gizmo_payload(name: str) -> dict[str, Any]:
    return {"gizmo": {"display": {"name": name}}}


def _conversations_payload(titles: list[str]) -> dict[str, Any]:
    return {"items": [{"title": t} for t in titles]}


def _happy_backend(
    sandbox: dict[str, str], extra: dict[str, tuple[int, Any]] | None = None
) -> _Backend:
    """Every endpoint preflight's account/run groups and health's own group
    touch, all answering 200 -- the longer gizmos/<id>/conversations prefix
    is listed before the shorter gizmos/<id> one (see ``_Backend``)."""
    responses: dict[str, tuple[int, Any]] = {
        preflight.ME: (200, {}),
        preflight.CONVERSATIONS_PROBE: (200, {"items": []}),
        preflight.pa.USAGE: (200, {}),
        preflight.ms.MODELS: (200, MODELS_PAYLOAD),
        preflight.ms.SETTINGS: (200, SETTINGS_PAYLOAD),
        preflight.pc.USER_SYSTEM_MESSAGES: (200, {}),
        preflight.pc.MEMORY_SUMMARY: (200, {}),
        preflight.pc.MEMORY_ENTRIES: (200, {}),
        health.clean_chats.PROJECT_CONVERSATIONS.format(id=sandbox["id"]): (
            200,
            _conversations_payload([]),
        ),
        health.lp.GIZMO.format(id=sandbox["id"]): (
            200,
            _gizmo_payload(sandbox["name"]),
        ),
        health.lp.SIDEBAR: (200, {}),
        health.PINS: (200, {}),
    }
    if extra:
        responses.update(extra)
    return _Backend(responses)


# ---------------------------------------------------------------------------
# GROUP health, check 1: session_token_check / fetch_session_token
# ---------------------------------------------------------------------------


def test_session_token_check_blocks_when_neither_side_has_a_token_at_all() -> None:
    """No persistent session token anywhere, and no read error either --
    the jar was read fine, it simply holds none, and the keyring holds
    nothing either (probe_cookies.session_horizon's own ``None`` case,
    load_stored_session's own ``None`` case)."""
    c = health.session_token_check(None, None)
    assert c["group"] == "health"
    assert c["state"] == "block"
    assert c["detail"] == "no persistent session token in the cookie jar or the keyring"
    assert c["fix"] == health.SESSION_TOKEN_FIX


def test_session_token_check_appends_the_read_error_when_given() -> None:
    c = health.session_token_check(None, None, error="no such table: cookies")
    assert c["state"] == "block"
    assert c["detail"].endswith(": no such table: cookies")


def test_session_token_check_blocks_when_already_expired() -> None:
    """Chrome-only (no keyring copy at all) -- the pre-renewal shape."""
    c = health.session_token_check(("2020-01-01", -3.0), None)
    assert c["state"] == "block"
    assert c["detail"] == (
        "expires 2020-01-01, -3.0 days left (no renewed copy in the keyring yet)"
    )


def test_session_token_check_blocks_exactly_at_zero_days_left() -> None:
    c = health.session_token_check(("2026-09-21", 0.0), None)
    assert c["state"] == "block"


def test_session_token_check_warns_inside_the_two_week_window() -> None:
    c = health.session_token_check(("2026-10-01", 5.2), None)
    assert c["state"] == "warn"
    assert c["detail"] == (
        "expires 2026-10-01, 5.2 days left (no renewed copy in the keyring yet)"
    )
    assert c["fix"] == health.SESSION_TOKEN_FIX


def test_session_token_check_is_ok_at_exactly_the_warn_threshold() -> None:
    """SESSION_WARN_DAYS itself must not warn: the warn test is strictly
    "<", so 14.0 days left is still ok."""
    c = health.session_token_check(
        ("2026-10-05", float(health.SESSION_WARN_DAYS)), None
    )
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_session_token_check_is_ok_well_before_expiry() -> None:
    c = health.session_token_check(("2026-12-01", 60.0), None)
    assert c["state"] == "ok"
    assert c["detail"] == (
        "expires 2026-12-01, 60.0 days left (no renewed copy in the keyring yet)"
    )


def test_session_token_check_judges_the_keyring_when_it_is_later() -> None:
    """A renewal the keyring has but Chrome's own jar has not caught up to
    yet must win -- both the verdict and the detail line."""
    chrome = ("2026-09-25", 4.0)  # inside the warn window on its own
    keyring = ("2026-12-19", 89.5)
    c = health.session_token_check(chrome, keyring)
    assert c["state"] == "ok"
    assert c["detail"] == (
        "expires 2026-12-19, 89.5 days left (from keyring; Chrome's copy "
        "expires 2026-09-25, 4.0 days left)"
    )


def test_session_token_check_judges_chrome_when_it_is_later_than_a_stale_keyring() -> (
    None
):
    """The keyring can also be the stale one -- e.g. right after the user's
    own Chrome refreshed its cookie by hand, before the next run catches
    the keyring up."""
    chrome = ("2026-12-19", 89.5)
    keyring = ("2026-09-25", 4.0)
    c = health.session_token_check(chrome, keyring)
    assert c["state"] == "ok"
    assert c["detail"] == (
        "expires 2026-12-19, 89.5 days left (from Chrome; keyring's copy "
        "expires 2026-09-25, 4.0 days left)"
    )


def test_session_token_check_keeps_chrome_on_an_exact_tie() -> None:
    same = ("2026-12-19", 89.5)
    c = health.session_token_check(same, same)
    assert "from Chrome" in c["detail"]


def test_read_session_horizon_reads_a_real_jar_through_probe_cookies(
    cookie_db: Path,
) -> None:
    """Proves the integration, not just the mock: a real sqlite cookie jar
    with a session-token row produces the same (date, days-left) shape
    probe_cookies.py itself would print, as the first of the two
    horizons; with nothing in the (fake) keyring, the second is None."""
    cc_mod = _fake_cc(None, browsers={"chrome": (str(cookie_db), "chrome")})
    chrome, keyring = health._read_session_horizon(cc_mod, "chrome")
    assert chrome is not None
    _date, days_left = chrome
    assert days_left == pytest.approx(45.0, abs=0.01)
    assert keyring is None


def test_read_session_horizon_reads_the_keyrings_stored_copy_too(
    cookie_db: Path,
) -> None:
    stored = {"cookies": {"x": "y"}, "expires": time.time() + 90 * 86400}
    cc_mod = _fake_cc(
        None,
        browsers={"chrome": (str(cookie_db), "chrome")},
        stored_session=stored,
    )
    _chrome, keyring = health._read_session_horizon(cc_mod, "chrome")
    assert keyring is not None
    _date, days_left = keyring
    assert days_left == pytest.approx(90.0, abs=0.01)


def test_read_session_horizon_raises_for_an_unknown_browser_name() -> None:
    cc_mod = _fake_cc(None, browsers={"chrome": ("/nonexistent", "chrome")})
    with pytest.raises(KeyError):
        health._read_session_horizon(cc_mod, "firefox")


def test_stored_session_horizon_is_none_with_nothing_stored() -> None:
    assert health._stored_session_horizon(None, time.time()) is None


def test_stored_session_horizon_is_none_when_expires_is_missing() -> None:
    assert health._stored_session_horizon({"cookies": {}}, time.time()) is None


def test_stored_session_horizon_matches_probe_cookies_shape() -> None:
    now = time.time()
    horizon = health._stored_session_horizon(
        {"cookies": {}, "expires": now + 10 * 86400}, now
    )
    assert horizon is not None
    date, days_left = horizon
    assert days_left == pytest.approx(10.0, abs=0.01)
    assert len(date) == 10  # YYYY-MM-DD


def test_fetch_session_token_is_ok_over_a_real_healthy_jar(cookie_db: Path) -> None:
    cc_mod = _fake_cc(None, browsers={"chrome": (str(cookie_db), "chrome")})
    c = health.fetch_session_token(cc_mod, "chrome")
    assert c["name"] == "session token"
    assert c["state"] == "ok"


def test_fetch_session_token_never_raises_on_a_missing_cookie_database() -> None:
    """The exact failure a fresh or wiped Chrome profile produces: the
    database file simply is not there yet. This must become a check, not
    an uncaught exception that would crash the whole run."""
    cc_mod = _fake_cc(None, browsers={"chrome": ("/nonexistent/Cookies", "chrome")})
    c = health.fetch_session_token(cc_mod, "chrome")
    assert c["state"] == "block"
    assert "no persistent session token" in c["detail"]


def test_fetch_session_token_truncates_a_long_error_to_200_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(cc_mod: Any, browser: str) -> None:
        raise RuntimeError("x" * 500)

    monkeypatch.setattr(health, "_read_session_horizon", boom)
    c = health.fetch_session_token(_fake_cc(None), "chrome")
    assert c["detail"].endswith("x" * 200)
    assert not c["detail"].endswith("x" * 201)


# ---------------------------------------------------------------------------
# GROUP health, check 2: sandbox_check / fetch_sandbox
# ---------------------------------------------------------------------------


def test_sandbox_check_blocks_on_a_non_200_gizmo_read() -> None:
    c = health.sandbox_check(404, "", "g-p-x", "rp-test-sandbox", 0, [])
    assert c["group"] == "health"
    assert c["state"] == "block"
    assert "404" in c["detail"]
    assert c["fix"] == health.SANDBOX_WRONG_FIX


def test_sandbox_check_blocks_on_a_name_that_does_not_match() -> None:
    """The one failure this check exists to catch: a stale or wrong id in
    sandbox.json pointing at a real project (TESTING.md section 1)."""
    c = health.sandbox_check(
        200, "some other project", "g-p-x", "rp-test-sandbox", 0, []
    )
    assert c["state"] == "block"
    assert "some other project" in c["detail"]
    assert "rp-test-sandbox" in c["detail"]


def test_sandbox_check_warns_when_the_conversations_read_itself_fails() -> None:
    c = health.sandbox_check(
        200, "rp-test-sandbox", "g-p-x", "rp-test-sandbox", 500, []
    )
    assert c["state"] == "warn"
    assert "500" in c["detail"]


def test_sandbox_check_warns_and_names_the_titles_when_chats_are_left() -> None:
    c = health.sandbox_check(
        200,
        "rp-test-sandbox",
        "g-p-x",
        "rp-test-sandbox",
        200,
        ["rp-test send 1", "Reply"],
    )
    assert c["state"] == "warn"
    assert "2 conversation(s)" in c["detail"]
    assert "rp-test send 1" in c["detail"]
    assert "Reply" in c["detail"]
    assert (
        c["fix"] == "a live test left chats behind; run the write tier once, or "
        "clean_chats.py --project g-p-x --apply"
    )


def test_sandbox_check_is_ok_when_identity_matches_and_nothing_is_left() -> None:
    c = health.sandbox_check(
        200, "rp-test-sandbox", "g-p-x", "rp-test-sandbox", 200, []
    )
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_fetch_sandbox_never_reads_conversations_when_the_gizmo_is_wrong() -> None:
    """Guards the "block before ever asking what is in it" rule: a wrong
    project's own conversations must never be fetched or reported."""
    backend = _Backend({health.lp.GIZMO.format(id=SANDBOX["id"]): (404, {})})
    session = _FakeSession(backend)
    c = health.fetch_sandbox(session, SANDBOX)
    assert c["state"] == "block"
    assert not any("conversations" in call for call in backend.calls)


def test_fetch_sandbox_is_ok_when_the_sandbox_is_clean(sandbox_file: dict) -> None:
    backend = _happy_backend(SANDBOX)
    session = _FakeSession(backend)
    c = health.fetch_sandbox(session, SANDBOX)
    assert c["state"] == "ok"


def test_fetch_sandbox_warns_with_titles_when_chats_were_left(
    sandbox_file: dict,
) -> None:
    backend = _happy_backend(
        SANDBOX,
        {
            health.clean_chats.PROJECT_CONVERSATIONS.format(id=SANDBOX["id"]): (
                200,
                _conversations_payload(["leftover chat"]),
            )
        },
    )
    session = _FakeSession(backend)
    c = health.fetch_sandbox(session, SANDBOX)
    assert c["state"] == "warn"
    assert "leftover chat" in c["detail"]


def test_fetch_sandbox_ignores_a_non_dict_conversations_body() -> None:
    backend = _Backend(
        {
            health.clean_chats.PROJECT_CONVERSATIONS.format(id=SANDBOX["id"]): (
                200,
                "not-a-dict",
            ),
            health.lp.GIZMO.format(id=SANDBOX["id"]): (
                200,
                _gizmo_payload(SANDBOX["name"]),
            ),
        }
    )
    session = _FakeSession(backend)
    c = health.fetch_sandbox(session, SANDBOX)
    assert c["state"] == "ok"  # tolerated as "no titles found", not a crash


# ---------------------------------------------------------------------------
# GROUP health, check 3: read_endpoints_check / fetch_read_endpoints
# ---------------------------------------------------------------------------


def test_read_endpoints_check_blocks_on_a_429_on_the_sidebar() -> None:
    c = health.read_endpoints_check(429, 200)
    assert c["state"] == "block"
    assert c["detail"] == "rate limited on the read path"
    assert c["fix"] == preflight.RATE_LIMIT_FIX


def test_read_endpoints_check_blocks_on_a_429_on_pins() -> None:
    c = health.read_endpoints_check(200, 429)
    assert c["state"] == "block"
    assert c["detail"] == "rate limited on the read path"


def test_read_endpoints_check_blocks_on_a_non_429_failure() -> None:
    c = health.read_endpoints_check(200, 500)
    assert c["state"] == "block"
    assert c["detail"] == "sidebar 200, pins 500"
    assert c["fix"] is None


def test_read_endpoints_check_is_ok_when_both_succeed() -> None:
    c = health.read_endpoints_check(200, 200)
    assert c["state"] == "ok"
    assert c["detail"] == "sidebar 200, pins 200"


def test_fetch_read_endpoints_reads_sidebar_and_pins() -> None:
    backend = _Backend({health.lp.SIDEBAR: (200, {}), health.PINS: (200, {})})
    c = health.fetch_read_endpoints(_FakeSession(backend))
    assert c["state"] == "ok"
    assert any(call.startswith(health.PINS) for call in backend.calls)


# ---------------------------------------------------------------------------
# health_checks()
# ---------------------------------------------------------------------------


def test_health_checks_returns_the_three_in_order(
    sandbox_file: dict, cookie_db: Path
) -> None:
    cc_mod = _fake_cc(None, browsers={"chrome": (str(cookie_db), "chrome")})
    backend = _happy_backend(SANDBOX)
    session = _FakeSession(backend)
    checks = health.health_checks(cc_mod, session, "chrome", SANDBOX)
    assert [c["name"] for c in checks] == ["session token", "sandbox", "read endpoints"]
    assert all(c["group"] == "health" for c in checks)
    assert all(c["state"] == "ok" for c in checks)


# ---------------------------------------------------------------------------
# The --json "facts" block
# ---------------------------------------------------------------------------


def test_empty_facts_has_the_full_documented_shape() -> None:
    facts = health._empty_facts(SANDBOX)
    assert facts == {
        "session_expires": None,
        "session_days_left": None,
        "session_source": None,
        "chrome_session_expires": None,
        "keyring_session_expires": None,
        "model": "",
        "effort": "",
        "preset": None,
        "presets": 0,
        "sandbox_id": "g-p-testsandbox",
        "sandbox_name": "rp-test-sandbox",
        "sandbox_conversations": 0,
        "gates": None,
        "endpoints": {
            "gizmos/snorlax/sidebar": 0,
            "pins": 0,
            "gizmos/g-p-testsandbox": 0,
            "gizmos/g-p-testsandbox/conversations": 0,
        },
    }


def test_model_effort_facts_resolves_the_servers_own_record() -> None:
    backend = _Backend(
        {
            preflight.ms.MODELS: (200, MODELS_PAYLOAD),
            preflight.ms.SETTINGS: (200, SETTINGS_PAYLOAD),
        }
    )
    facts = health._model_effort_facts(_FakeSession(backend))
    assert facts == {
        "model": "gpt-5-6-thinking",
        "effort": "max",
        "preset": "Extra High",
        "presets": 2,
    }


def test_model_effort_facts_preset_is_none_without_a_server_record() -> None:
    backend = _Backend(
        {preflight.ms.MODELS: (200, MODELS_PAYLOAD), preflight.ms.SETTINGS: (200, {})}
    )
    facts = health._model_effort_facts(_FakeSession(backend))
    assert facts["model"] == ""
    assert facts["preset"] is None
    assert facts["presets"] == 2


def test_gates_facts_returns_the_gate_dict_on_success(
    fake_psg: SimpleNamespace,
) -> None:
    facts = health._gates_facts(_FakeSession(_Backend({})))
    assert facts == {"proofofwork": True, "turnstile": True, "so": True}


def test_gates_facts_is_none_when_the_sentinel_fetch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(session: Any) -> dict:
        raise RuntimeError("network is down")

    monkeypatch.setattr(health, "psg", SimpleNamespace(fetch=boom))
    assert health._gates_facts(_FakeSession(_Backend({}))) is None


def test_facts_of_reflects_every_fetch_on_a_clean_sandbox(
    sandbox_file: dict, cookie_db: Path, fake_psg: SimpleNamespace
) -> None:
    cc_mod = _fake_cc(None, browsers={"chrome": (str(cookie_db), "chrome")})
    session = _FakeSession(_happy_backend(SANDBOX))
    facts = health.facts_of(cc_mod, session, "chrome", SANDBOX)
    assert facts["session_expires"] is not None
    assert facts["session_days_left"] == pytest.approx(45.0, abs=0.01)
    assert facts["session_source"] == "chrome"
    assert facts["chrome_session_expires"] == facts["session_expires"]
    assert facts["keyring_session_expires"] is None  # nothing stored in this test
    assert facts["model"] == "gpt-5-6-thinking"
    assert facts["effort"] == "max"
    assert facts["preset"] == "Extra High"
    assert facts["presets"] == 2
    assert facts["sandbox_id"] == "g-p-testsandbox"
    assert facts["sandbox_name"] == "rp-test-sandbox"
    assert facts["sandbox_conversations"] == 0
    assert facts["gates"] == {"proofofwork": True, "turnstile": True, "so": True}
    assert facts["endpoints"] == {
        "gizmos/snorlax/sidebar": 200,
        "pins": 200,
        "gizmos/g-p-testsandbox": 200,
        "gizmos/g-p-testsandbox/conversations": 200,
    }


def test_facts_of_reports_the_keyring_when_it_is_fresher(
    sandbox_file: dict, cookie_db: Path, fake_psg: SimpleNamespace
) -> None:
    stored = {"cookies": {"x": "y"}, "expires": time.time() + 90 * 86400}
    cc_mod = _fake_cc(
        None, browsers={"chrome": (str(cookie_db), "chrome")}, stored_session=stored
    )
    session = _FakeSession(_happy_backend(SANDBOX))
    facts = health.facts_of(cc_mod, session, "chrome", SANDBOX)
    assert facts["session_source"] == "keyring"
    assert facts["session_days_left"] == pytest.approx(90.0, abs=0.01)
    assert facts["chrome_session_expires"] is not None
    assert facts["keyring_session_expires"] is not None
    assert facts["session_expires"] == facts["keyring_session_expires"]


def test_facts_of_leaves_session_fields_null_when_the_jar_is_unreadable(
    sandbox_file: dict, fake_psg: SimpleNamespace
) -> None:
    cc_mod = _fake_cc(None, browsers={"chrome": ("/nonexistent/Cookies", "chrome")})
    session = _FakeSession(_happy_backend(SANDBOX))
    facts = health.facts_of(cc_mod, session, "chrome", SANDBOX)
    assert facts["session_expires"] is None
    assert facts["session_days_left"] is None
    assert facts["session_source"] is None
    assert facts["chrome_session_expires"] is None
    assert facts["keyring_session_expires"] is None


def test_facts_of_never_reads_conversations_when_the_gizmo_is_wrong(
    cookie_db: Path, fake_psg: SimpleNamespace
) -> None:
    """The conversations endpoint's own status stays at the empty-facts
    default (0) when the gizmo read never matched -- it was never called,
    the same rule fetch_sandbox enforces for the check."""
    cc_mod = _fake_cc(None, browsers={"chrome": (str(cookie_db), "chrome")})
    backend = _Backend(
        {
            health.lp.GIZMO.format(id=SANDBOX["id"]): (
                200,
                _gizmo_payload("someone else"),
            ),
            preflight.ms.MODELS: (200, MODELS_PAYLOAD),
            preflight.ms.SETTINGS: (200, SETTINGS_PAYLOAD),
            health.lp.SIDEBAR: (200, {}),
            health.PINS: (200, {}),
        }
    )
    session = _FakeSession(backend)
    facts = health.facts_of(cc_mod, session, "chrome", SANDBOX)
    assert facts["endpoints"]["gizmos/g-p-testsandbox/conversations"] == 0
    assert facts["sandbox_conversations"] == 0


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_health_group_renders_between_run_and_browser() -> None:
    """preflight.render takes the group order as an argument, so the health
    group has a place without touching preflight's own GROUP_ORDER."""
    checks = [
        preflight.check("run", "x", "ok", "d"),
        preflight.check("health", "y", "ok", "d"),
        preflight.check("browser", "z", "ok", "d"),
    ]
    text = preflight.render(checks, order=health.HEALTH_GROUP_ORDER)
    assert (
        text.index("== run ==")
        < text.index("== health ==")
        < text.index("== browser ==")
    )
    assert preflight.GROUP_ORDER == ("host", "link", "account", "run", "browser")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_help_exits_0_and_prints_usage(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        health.main(["--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


DEFAULT_HEALTH_NAMES = [
    "available memory",
    "load average",
    "in-flight browsers",
    "browser tooling",
    "keyring bus",
    "wireless link",
    "auth and read",
    "send gates",
    "plan and credits",
    "model and effort",
    "custom instructions and memory",
    "session token",
    "sandbox",
    "read endpoints",
]


def test_main_go_on_a_fully_clean_run(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend(SANDBOX))
    monkeypatch.setattr(
        health, "cc", _fake_cc(session, browsers={"chrome": (str(cookie_db), "chrome")})
    )
    code = health.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip().splitlines()[-1] == "GO"


def test_main_writes_the_documented_json_shape(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend(SANDBOX))
    monkeypatch.setattr(
        health, "cc", _fake_cc(session, browsers={"chrome": (str(cookie_db), "chrome")})
    )
    out_path = tmp_path / "out" / "health.json"
    code = health.main(["--json", str(out_path)])
    doc = json.loads(out_path.read_text())

    assert set(doc.keys()) == {"checked_at", "verdict", "exit_code", "checks", "facts"}
    assert doc["exit_code"] == code == 0
    assert doc["verdict"] == "GO"
    assert [c["name"] for c in doc["checks"]] == DEFAULT_HEALTH_NAMES

    facts = doc["facts"]
    assert set(facts.keys()) == {
        "session_expires",
        "session_days_left",
        "session_source",
        "chrome_session_expires",
        "keyring_session_expires",
        "model",
        "effort",
        "preset",
        "presets",
        "sandbox_id",
        "sandbox_name",
        "sandbox_conversations",
        "gates",
        "endpoints",
    }
    assert facts["sandbox_id"] == "g-p-testsandbox"
    assert facts["sandbox_conversations"] == 0
    assert set(facts["endpoints"].keys()) == {
        "gizmos/snorlax/sidebar",
        "pins",
        "gizmos/g-p-testsandbox",
        "gizmos/g-p-testsandbox/conversations",
    }


def test_main_go_with_warnings_when_the_session_token_is_near_expiry(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    near_expiry = tmp_path / "Cookies"
    _make_cookie_db(
        near_expiry,
        [
            (
                "__Secure-next-auth.session-token.0",
                ".chatgpt.com",
                b"opaque",
                _webkit_expiry(5.0),
            )
        ],
    )
    session = _FakeSession(_happy_backend(SANDBOX))
    monkeypatch.setattr(
        health,
        "cc",
        _fake_cc(session, browsers={"chrome": (str(near_expiry), "chrome")}),
    )
    code = health.main([])
    out = capsys.readouterr().out
    assert code == 2
    assert out.strip().splitlines()[-1].startswith("GO WITH WARNINGS")


def test_main_do_not_start_when_the_sandbox_is_the_wrong_project(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _happy_backend(
        SANDBOX,
        {
            health.lp.GIZMO.format(id=SANDBOX["id"]): (
                200,
                _gizmo_payload("a real project"),
            )
        },
    )
    session = _FakeSession(backend)
    monkeypatch.setattr(
        health, "cc", _fake_cc(session, browsers={"chrome": (str(cookie_db), "chrome")})
    )
    code = health.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert out.strip().splitlines()[-1].startswith("DO NOT START")
    assert "a real project" in out


def test_main_facts_show_a_dirty_sandbox_as_a_warning_not_a_block(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _happy_backend(
        SANDBOX,
        {
            health.clean_chats.PROJECT_CONVERSATIONS.format(id=SANDBOX["id"]): (
                200,
                _conversations_payload(["leftover"]),
            )
        },
    )
    session = _FakeSession(backend)
    monkeypatch.setattr(
        health, "cc", _fake_cc(session, browsers={"chrome": (str(cookie_db), "chrome")})
    )
    out_path = tmp_path / "health.json"
    code = health.main(["--json", str(out_path)])
    doc = json.loads(out_path.read_text())
    assert code == 2
    assert doc["facts"]["sandbox_conversations"] == 1
    sandbox_check = next(c for c in doc["checks"] if c["name"] == "sandbox")
    assert sandbox_check["state"] == "warn"


def test_diagnostic_sink_writes_timestamped_jsonl_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    out = tmp_path / "events.jsonl"
    emit = health._diagnostic_sink(str(out))
    assert emit is not None
    emit(
        {
            "event": "probe",
            "path": "/api/auth/session",
            "status": 403,
            "cookie": "secret-cookie",
            "nested": {"Authorization": "Bearer secret-token", "safe": 1},
        }
    )
    row = json.loads(out.read_text())
    assert row["event"] == "probe"
    assert row["path"] == "/api/auth/session"
    assert row["status"] == 403
    assert row["cookie"] == "<redacted>"
    assert row["nested"]["Authorization"] == "<redacted>"
    assert row["nested"]["safe"] == 1
    assert "secret-cookie" not in out.read_text()
    assert "secret-token" not in out.read_text()
    assert row["at"].endswith("+00:00")


def test_main_uses_the_bounded_health_session_policy(
    clean_host: None,
    linked_wireless: Path,
    sandbox_file: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def spy_open(
        cc_mod: Any, browser: str, **session_options: Any
    ) -> tuple[Any, str | None]:
        seen.update(session_options)
        return None, "intentional test stop"

    monkeypatch.setattr(preflight, "open_chatgpt_session", spy_open)
    monkeypatch.setattr(health, "cc", _fake_cc(None))
    code = health.main([])
    assert code == 1
    assert seen["auth_backoff"] == health.HEALTH_AUTH_BACKOFF
    assert seen["request_timeout"] == health.HEALTH_REQUEST_TIMEOUT
    assert seen["request_retries"] == health.HEALTH_REQUEST_RETRIES
    assert seen["diagnostic"] is None


def test_main_skips_health_when_the_link_is_dead(
    clean_host: None,
    dead_wireless: Path,
    sandbox_file: dict,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The house rule (preflight.py's own): a dead link must never be
    reported as a blocked account, and the same now applies to the health
    group, which needs a session exactly as much as account/run do."""
    calls: list[str] = []

    def spy_open(
        cc_mod: Any, browser: str, **session_options: Any
    ) -> tuple[Any, str | None]:
        calls.append(browser)
        return _FakeSession(_happy_backend(SANDBOX)), None

    monkeypatch.setattr(preflight, "open_chatgpt_session", spy_open)
    monkeypatch.setattr(health, "cc", _fake_cc(None))
    code = health.main([])
    out = capsys.readouterr().out
    assert calls == [], (
        "open_chatgpt_session must never be called when the link is dead"
    )
    assert code == 1
    assert "skipped" in out
    assert "session token" not in out
    assert "sandbox" not in out
    assert "read endpoints" not in out


def test_main_json_facts_stay_empty_when_the_link_is_dead(
    clean_host: None,
    dead_wireless: Path,
    sandbox_file: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "cc", _fake_cc(None))
    out_path = tmp_path / "health.json"
    health.main(["--json", str(out_path)])
    doc = json.loads(out_path.read_text())
    assert doc["facts"] == health._empty_facts(SANDBOX)


def test_main_blocks_on_auth_and_read_when_the_session_cannot_open(
    clean_host: None,
    linked_wireless: Path,
    sandbox_file: dict,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_open(
        cc_mod: Any, browser: str, **session_options: Any
    ) -> tuple[Any, str | None]:
        return None, "no cookie database"

    monkeypatch.setattr(preflight, "open_chatgpt_session", failing_open)
    monkeypatch.setattr(health, "cc", _fake_cc(None))
    code = health.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "could not authenticate: no cookie database" in out
    assert "session token" not in out


def test_main_runs_the_browser_check_last_when_opted_in(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend(SANDBOX))
    monkeypatch.setattr(
        health,
        "cc",
        _fake_cc(
            session,
            browsers={"chrome": (str(cookie_db), "chrome")},
            sender_cls=_FakeSenderOK,
        ),
    )
    out_path = tmp_path / "health.json"
    code = health.main(["--browser", "--json", str(out_path)])
    doc = json.loads(out_path.read_text())
    assert code == 0
    assert doc["checks"][-1]["group"] == "browser"
    assert doc["checks"][-1]["state"] == "ok"
    assert [c["name"] for c in doc["checks"][:-1]] == DEFAULT_HEALTH_NAMES


def test_main_records_a_blocked_browser_check(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    sandbox_file: dict,
    cookie_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend(SANDBOX))
    monkeypatch.setattr(
        health,
        "cc",
        _fake_cc(
            session,
            browsers={"chrome": (str(cookie_db), "chrome")},
            sender_cls=_FakeSenderBlocked,
        ),
    )
    out_path = tmp_path / "health.json"
    code = health.main(["--browser", "--json", str(out_path)])
    doc = json.loads(out_path.read_text())
    assert code == 1
    assert doc["checks"][-1]["group"] == "browser"
    assert doc["checks"][-1]["state"] == "block"
