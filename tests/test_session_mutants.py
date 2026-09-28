"""T0 tests written for the mutants of chatgpt_session.py that the P10 pilot
found alive on 2026-09-29 (TESTING.md section 6, P10; VERIFICATION.md,
"Mutation testing pilot"). Each test names the behaviour it pins; the
survivors left without a test have a one-line reason in VERIFICATION.md."""

from __future__ import annotations

import http.client
import json
import sqlite3
import urllib.error
from typing import Any

import chatgpt_session
import pytest
from test_session import (
    AUTH_OK,
    NOW,
    SESSION_COOKIE,
    _derive,
    _encrypt,
    _FakeBus,
    _FakeItem,
    _FakeResponse,
    _FakeService,
    _http_error,
    _install_fake_dbus,
    _session,
    _wire_init,
)

PREFIX = chatgpt_session.SESSION_COOKIE_PREFIX


# -- the reason a login failed ------------------------------------------------


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (500, "HTTP 500; server error"),
        (503, "HTTP 503; server error"),
        (499, "could not authenticate (HTTP 499)"),
        (401, "HTTP 401); session cookie may be expired"),
        (403, "HTTP 403); session cookie may be expired"),
        (402, "could not authenticate (HTTP 402)"),
        (418, "could not authenticate (HTTP 418)"),
    ],
)
def test_the_login_failure_reason_by_status(status: int, words: str) -> None:
    reason = chatgpt_session._auth_failure_reason(status, None)
    assert words in reason
    if status not in (401, 403):
        assert "expired" not in reason


def test_a_header_value_is_kept_to_200_characters() -> None:
    safe = chatgpt_session._safe_response_headers({"Server": "x" * 500})
    assert safe["server"] == "x" * 200


# -- decoding a cookie: damaged values are None, never a wrong value ----------


def _raw_v10(plain: bytes) -> bytes:
    """v10 ciphertext of ``plain`` exactly as given: no padding added."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    assert len(plain) % 16 == 0
    enc = Cipher(algorithms.AES(_derive(b"peanuts")), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(plain) + enc.finalize()


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: b"k")
    return chatgpt_session._make_decryptor("chrome").plain


def test_a_domain_hash_before_an_empty_value_decodes_to_the_empty_value(plain):
    """Newer Chromium prepends a 32-byte hash of the domain; with an empty
    value (an analytics cookie) the plaintext is exactly 32 bytes."""
    assert plain(_encrypt(b"v10", _derive(b"peanuts"), b"\xff" * 32)) == ""


def test_a_plaintext_ending_in_a_zero_byte_is_damaged_not_empty(plain) -> None:
    assert plain(_raw_v10(b"abcdefghijklmno\x00")) is None


def test_a_plaintext_ending_in_17_is_damaged_not_cut(plain) -> None:
    """48 bytes, the last 0x11: no padding reads 17. (At exactly 32 bytes the
    same value is a domain hash before an empty value, and "" is right.)"""
    assert plain(_raw_v10(b"a" * 47 + b"\x11")) is None


# -- the keyring's own copy ---------------------------------------------------


def test_load_and_store_set_up_the_desktop_environment_first(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        chatgpt_session, "ensure_desktop_env", lambda: calls.append("env")
    )
    service = _FakeService(unlocked=[], locked=[])
    _install_fake_dbus(monkeypatch, _FakeBus(service, {}))
    chatgpt_session.load_stored_session()
    chatgpt_session.store_session(
        {"cookies": {SESSION_COOKIE: "v"}, "expires": NOW, "stored_at": NOW}
    )
    assert calls == ["env", "env"]


def test_a_stored_item_without_a_cookies_map_is_skipped(monkeypatch) -> None:
    good = {"cookies": {SESSION_COOKIE: "v"}, "expires": NOW}
    service = _FakeService(unlocked=["/item/no-cookies", "/item/good"], locked=[])
    bus = _FakeBus(
        service,
        {
            "/item/no-cookies": _FakeItem(secret=json.dumps({"expires": NOW}).encode()),
            "/item/good": _FakeItem(secret=json.dumps(good).encode()),
        },
    )
    _install_fake_dbus(monkeypatch, bus)
    assert chatgpt_session.load_stored_session() == good


# -- shaping the jar the scripted browser receives ----------------------------


def test_a_new_chunk_without_a_template_is_secure_and_http_only() -> None:
    jar: list[dict[str, Any]] = []
    chatgpt_session.apply_session_to_jar(
        jar, {"cookies": {f"{PREFIX}.0": "v"}, "expires": NOW}
    )
    (cookie,) = jar
    assert (cookie["secure"], cookie["httpOnly"]) == (True, True)


def test_a_new_chunk_takes_secure_defaults_the_template_lacks() -> None:
    jar = [{"name": f"{PREFIX}.0", "value": "old", "domain": ".chatgpt.com"}]
    chatgpt_session.apply_session_to_jar(
        jar, {"cookies": {f"{PREFIX}.0": "a", f"{PREFIX}.1": "b"}, "expires": NOW}
    )
    added = next(c for c in jar if c["name"] == f"{PREFIX}.1")
    assert (added["secure"], added["httpOnly"]) == (True, True)


def test_a_record_without_an_expiry_removes_the_old_one() -> None:
    jar = [{"name": f"{PREFIX}.0", "value": "old", "expires": 123.0}]
    chatgpt_session.apply_session_to_jar(
        jar, {"cookies": {f"{PREFIX}.0": "new"}, "expires": None}
    )
    assert jar == [{"name": f"{PREFIX}.0", "value": "new"}]


def test_a_webkit_expiry_converts_to_the_exact_epoch_second(tmp_path, monkeypatch):
    """2030-01-01T00:00:00Z is 1893456000 s after 1970 and
    (1893456000 + 11644473600) * 10**6 microseconds after 1601."""
    db = tmp_path / "Cookies"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER)"
    )
    webkit = (1_893_456_000 + 11_644_473_600) * 1_000_000
    con.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
        (".chatgpt.com", f"{PREFIX}.0", "tok", b"", webkit),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(chatgpt_session, "BROWSERS", {"chrome": (db, "chrome")})
    _pairs, record = chatgpt_session._cookie_pairs("chrome")
    assert record["expires"] == 1_893_456_000.0


# -- building a session -------------------------------------------------------


@pytest.mark.parametrize(("later_by", "stored"), [(60, False), (61, True)])
def test_a_renewal_is_stored_only_when_more_than_a_minute_later(
    monkeypatch, later_by: int, stored: bool
) -> None:
    max_age = 7_776_000
    chrome = {"cookies": {SESSION_COOKIE: "c"}, "expires": NOW + max_age - later_by}
    store_calls = _wire_init(
        monkeypatch,
        AUTH_OK,
        pairs=[(SESSION_COOKIE, "c")],
        chrome=chrome,
        set_cookie_headers=[f"{SESSION_COOKIE}=r; Max-Age={max_age}; Path=/"],
    )
    monkeypatch.setattr(chatgpt_session.time, "time", lambda: NOW)
    chatgpt_session.Session()
    assert bool(store_calls) is stored


def test_a_session_built_with_the_defaults_tries_a_read_three_times(monkeypatch):
    _wire_init(monkeypatch, AUTH_OK)
    session = chatgpt_session.Session()
    attempts: list[int] = []

    def slow(req, timeout=60):
        attempts.append(1)
        raise _http_error(429, "slow down")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", slow)
    monkeypatch.setattr(chatgpt_session.time, "sleep", lambda s: None)
    assert session.call("/x")[0] == 429
    assert len(attempts) == 3


def test_a_call_with_no_attempts_says_so() -> None:
    assert _session().call("/x", retries=0) == (0, {"error": "no attempt made"})


# -- what an error excerpt keeps ----------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _http_error(404, "b" * 500),
        urllib.error.URLError("u" * 500),
        TimeoutError("t" * 500),
    ],
)
def test_an_error_excerpt_is_200_characters(monkeypatch, error) -> None:
    def fail(req, timeout=60):
        raise error

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fail)
    _status, data = _session().call("/x", retries=1)
    assert len(data["error"]) == 200


# -- the diagnostic events a run leaves behind --------------------------------


class _Clock:
    """``time.monotonic`` for one request: its start, then its end."""

    def __init__(self, *values: float) -> None:
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _diag_session(events: list[dict[str, Any]]) -> chatgpt_session.Session:
    session = _session()
    session._diagnostic = events.append
    return session


@pytest.mark.parametrize(
    ("raise_or_answer", "outcome", "status"),
    [
        (_FakeResponse(200, "{}"), "success", 200),
        (_http_error(404, "gone"), "http_error", 404),
        (urllib.error.URLError("refused"), "url_error", 0),
        (TimeoutError("timed out"), "exception", 0),
    ],
)
def test_each_attempt_event_carries_its_outcome_status_and_elapsed_time(
    monkeypatch, raise_or_answer, outcome, status
) -> None:
    def answer(req, timeout=60):
        if isinstance(raise_or_answer, Exception):
            raise raise_or_answer
        return raise_or_answer

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", answer)
    monkeypatch.setattr(chatgpt_session.time, "monotonic", _Clock(10.0, 10.12345))
    events: list[dict[str, Any]] = []
    _diag_session(events).call("/x", retries=1)
    (event,) = [e for e in events if e["event"] == "http_attempt"]
    assert (event["outcome"], event["status"]) == (outcome, status)
    assert event["elapsed_ms"] == 123.5  # 0.12345 s, to one decimal of a ms
    if outcome == "exception":
        assert event["error_type"] == "TimeoutError"


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("e" * 400), OSError("o" * 400)]
)
def test_an_attempt_event_keeps_300_characters_of_the_error(monkeypatch, error) -> None:
    def fail(req, timeout=60):
        raise error

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fail)
    events: list[dict[str, Any]] = []
    _diag_session(events).call("/x", retries=1)
    (event,) = [e for e in events if e["event"] == "http_attempt"]
    assert len(event["error"]) == 300


def test_retry_events_count_attempts_from_one(monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_session.time, "sleep", lambda s: None)

    def fail(req, timeout=60):
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(chatgpt_session.urllib.request, "urlopen", fail)
    events: list[dict[str, Any]] = []
    _diag_session(events).call("/x", retries=3)
    retries = [e for e in events if e["event"] == "http_retry"]
    assert [(e["attempt"], e["max_attempts"]) for e in retries] == [(1, 3), (2, 3)]


@pytest.mark.parametrize("with_records", [False, True])
def test_a_session_build_leaves_its_four_events(monkeypatch, with_records) -> None:
    chrome = (
        {"cookies": {SESSION_COOKIE: "c"}, "expires": NOW} if with_records else None
    )
    stored = (
        {"cookies": {SESSION_COOKIE: "k"}, "expires": NOW} if with_records else None
    )
    _wire_init(monkeypatch, AUTH_OK, chrome=chrome, stored=stored)
    events: list[dict[str, Any]] = []
    chatgpt_session.Session(diagnostic=events.append)
    assert [e["event"] for e in events] == [
        "session_start",
        "session_cookie_source",
        "http_attempt",
        "auth_result",
    ]
    source = events[1]
    assert (source["chrome_candidate"], source["keyring_candidate"]) == (
        with_records,
        with_records,
    )
    assert events[3]["authenticated"] is True
