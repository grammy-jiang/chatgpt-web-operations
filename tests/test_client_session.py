"""Tests for the stateful half of chatgpt_client.py: cookies, sessions, locks.

Cookie decryption and the ``_helpers``/``_patch_*`` machinery, the browser
memory/slot budget, ``ChatGPTSession``, ``wait_for_reply``, and the desktop/
display helpers. The pure parsing and transcript functions live in
``test_client_core.py`` instead. ``BrowserSender`` (roughly line 1038 onward)
is covered by ``test_client_browser.py``.

Every test names the failure it defends against, matching ``test_commands.py``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402


class _FakeClock:
    """A monotonic clock and a sleep that advances it, with no real delay."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------------------
# _tolerant_decryptor -- one unreadable cookie must never end the session
# ---------------------------------------------------------------------------


def _pbkdf2_key(password: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
    ).derive(password)


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad_len = block - (len(data) % block)
    return data + bytes([pad_len]) * pad_len


def _encrypt(plaintext: bytes, version: bytes, password: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _pbkdf2_key(password)
    engine = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    padded = _pkcs7_pad(plaintext)
    return version + engine.update(padded) + engine.finalize()


_KEYRING_PW = b"gnome-keyring-secret"
_FAKE_CS = types.SimpleNamespace(_keyring_password=lambda app: _KEYRING_PW)
# Deliberately not valid UTF-8 (0xff/0xfe are never a valid lead byte), so it
# stands in for Chromium's 32-byte domain hash: the decoder must reject it as
# text and fall back to the bytes after it.
_HASH32 = b"\xff\xfe" * 16


def test_v10_decrypts_an_ordinary_value_with_the_fixed_password() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(b"abc123", b"v10", b"peanuts")
    assert decrypt(enc) == "abc123"


def test_v11_uses_the_keyring_derived_password() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(b"xyz789", b"v11", _KEYRING_PW)
    assert decrypt(enc) == "xyz789"


def test_an_unexpected_version_prefix_is_unreadable() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    with pytest.raises(cc.UnreadableCookie, match="unexpected encryption version"):
        decrypt(b"v99" + b"0123456789abcdef")


def test_zero_length_ciphertext_is_unreadable() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    with pytest.raises(cc.UnreadableCookie, match="empty ciphertext"):
        decrypt(b"v10")


def test_a_tab_character_is_tolerated() -> None:
    """The strict decryptor chokes on this; the tolerant one must not."""
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(b"a\tb", b"v10", b"peanuts")
    assert decrypt(enc) == "a\tb"


def test_non_ascii_utf8_is_tolerated() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt("café☕".encode(), b"v10", b"peanuts")
    assert decrypt(enc) == "café☕"


def test_a_genuine_control_character_is_still_rejected() -> None:
    """Tolerance is deliberately narrow: control bytes other than tab stay out."""
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(b"\x01bad", b"v10", b"peanuts")
    with pytest.raises(cc.UnreadableCookie, match="not decodable text"):
        decrypt(enc)


def test_a_bare_empty_plaintext_with_no_hash_prefix_is_rejected() -> None:
    """Random bytes from a torn DB copy must not pass as an empty cookie."""
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(b"", b"v10", b"peanuts")
    with pytest.raises(cc.UnreadableCookie, match="not decodable text"):
        decrypt(enc)


def test_a_32_byte_hash_only_value_decodes_as_the_empty_string() -> None:
    """Newer Chromium's real 'empty cookie' shape: the hash and nothing else."""
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(_HASH32, b"v10", b"peanuts")
    assert decrypt(enc) == ""


def test_a_hashed_short_value_strips_the_32_byte_prefix() -> None:
    decrypt = cc._tolerant_decryptor(_FAKE_CS, "chrome")
    enc = _encrypt(_HASH32 + b"sess-value", b"v10", b"peanuts")
    assert decrypt(enc) == "sess-value"


# ---------------------------------------------------------------------------
# _skip_unreadable -- only UnreadableCookie is swallowed
# ---------------------------------------------------------------------------


def test_no_encrypted_value_returns_the_plaintext_column() -> None:
    assert cc._skip_unreadable(lambda enc: "should not run", "plain", b"") == "plain"
    assert cc._skip_unreadable(lambda enc: "should not run", "plain", None) == "plain"


def test_a_readable_encrypted_value_is_decrypted() -> None:
    assert cc._skip_unreadable(lambda enc: "decoded", "ignored", b"data") == "decoded"


def test_an_unreadable_cookie_is_dropped_not_raised() -> None:
    def boom(enc: bytes) -> str:
        raise cc.UnreadableCookie("nope")

    assert cc._skip_unreadable(boom, "ignored", b"data") is None


def test_only_unreadablecookie_is_swallowed() -> None:
    def boom(enc: bytes) -> str:
        raise ValueError("something else entirely")

    with pytest.raises(ValueError, match="something else"):
        cc._skip_unreadable(boom, "ignored", b"data")


# ---------------------------------------------------------------------------
# _patch_cookie_header / _patch_cookie_export -- over a real sqlite DB
# ---------------------------------------------------------------------------


def _make_cookie_db(path: Path, rows: list[tuple]) -> None:
    """rows: host_key, name, value, encrypted_value, path, expires_utc,
    is_secure, is_httponly, samesite -- the columns both patched queries read.
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, expires_utc INTEGER, "
        "is_secure INTEGER, is_httponly INTEGER, samesite INTEGER)"
    )
    con.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _fake_cs(db: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        BROWSERS={"chrome": (db, "chrome")},
        _keyring_password=lambda app: _KEYRING_PW,
    )


def test_cookie_header_raises_when_the_db_file_is_missing(tmp_path: Path) -> None:
    cs = _fake_cs(tmp_path / "missing.db")
    cc._patch_cookie_header(cs)
    with pytest.raises(cc.TransportError, match="cookie DB not found"):
        cs._cookie_header("chrome")


def test_cookie_header_skips_one_unreadable_cookie_but_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """The whole point: an analytics cookie must not mask a working session."""
    db = tmp_path / "Cookies"
    session_enc = _encrypt(b"sess-abc", b"v10", b"peanuts")
    bad_enc = b"v99" + b"0123456789abcdef"
    _make_cookie_db(
        db,
        [
            ("chatgpt.com", "oai-did", "plain-value", b"", "/", 0, 1, 0, 1),
            (
                "chatgpt.com",
                "__Secure-next-auth.session-token.0",
                "",
                session_enc,
                "/",
                0,
                1,
                1,
                1,
            ),
            ("chatgpt.com", "_dd_s", "", bad_enc, "/", 0, 1, 0, 1),
        ],
    )
    cs = _fake_cs(db)
    cc._patch_cookie_header(cs)
    pairs = cs._cookie_header("chrome").split("; ")
    assert "oai-did=plain-value" in pairs
    assert "__Secure-next-auth.session-token.0=sess-abc" in pairs
    assert not any(p.startswith("_dd_s=") for p in pairs)


def test_cookie_header_raises_when_no_session_cookie_is_readable(
    tmp_path: Path,
) -> None:
    db = tmp_path / "Cookies"
    bad_enc = b"v99" + b"0123456789abcdef"
    _make_cookie_db(db, [("chatgpt.com", "_dd_s", "", bad_enc, "/", 0, 1, 0, 1)])
    cs = _fake_cs(db)
    cc._patch_cookie_header(cs)
    with pytest.raises(cc.TransportError, match="no readable ChatGPT session cookie"):
        cs._cookie_header("chrome")


def test_patch_cookie_header_is_idempotent() -> None:
    cs = types.SimpleNamespace(BROWSERS={}, _keyring_password=lambda app: b"pw")
    cc._patch_cookie_header(cs)
    first = cs._cookie_header
    cc._patch_cookie_header(cs)
    assert cs._cookie_header is first


def test_cookie_export_raises_when_the_db_file_is_missing(tmp_path: Path) -> None:
    cs = _fake_cs(tmp_path / "missing.db")
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    with pytest.raises(cc.TransportError, match="cookie DB not found"):
        mod.export("chrome")


def test_cookie_export_returns_playwright_shaped_dicts(tmp_path: Path) -> None:
    db = tmp_path / "Cookies"
    session_enc = _encrypt(b"sess-abc", b"v10", b"peanuts")
    exp = 13_360_000_000_000_000
    _make_cookie_db(
        db,
        [
            (
                "chatgpt.com",
                "__Secure-next-auth.session-token.0",
                "",
                session_enc,
                "/",
                exp,
                1,
                1,
                1,
            ),
        ],
    )
    cs = _fake_cs(db)
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    out = mod.export("chrome")
    assert len(out) == 1
    cookie = out[0]
    assert cookie["name"] == "__Secure-next-auth.session-token.0"
    assert cookie["value"] == "sess-abc"
    assert cookie["domain"] == "chatgpt.com"
    assert cookie["path"] == "/"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is True
    assert cookie["sameSite"] == "Lax"
    assert cookie["expires"] == exp / 1_000_000 - mod._EPOCH_DELTA_S


def test_cookie_export_omits_expires_when_there_is_none(tmp_path: Path) -> None:
    db = tmp_path / "Cookies"
    session_enc = _encrypt(b"sess-abc", b"v10", b"peanuts")
    _make_cookie_db(
        db,
        [
            (
                "chatgpt.com",
                "__Secure-next-auth.session-token.0",
                "",
                session_enc,
                "/",
                0,
                1,
                1,
                1,
            ),
        ],
    )
    cs = _fake_cs(db)
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    assert "expires" not in mod.export("chrome")[0]


@pytest.mark.parametrize(
    ("samesite_code", "expected"), [(0, "None"), (1, "Lax"), (2, "Strict"), (9, "Lax")]
)
def test_cookie_export_maps_the_samesite_code(
    tmp_path: Path, samesite_code: int, expected: str
) -> None:
    db = tmp_path / "Cookies"
    session_enc = _encrypt(b"sess-abc", b"v10", b"peanuts")
    _make_cookie_db(
        db,
        [
            (
                "chatgpt.com",
                "__Secure-next-auth.session-token.0",
                "",
                session_enc,
                "/",
                0,
                1,
                1,
                samesite_code,
            ),
        ],
    )
    cs = _fake_cs(db)
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    assert mod.export("chrome")[0]["sameSite"] == expected


def test_cookie_export_drops_one_unreadable_cookie_but_keeps_the_rest(
    tmp_path: Path,
) -> None:
    db = tmp_path / "Cookies"
    session_enc = _encrypt(b"sess-abc", b"v10", b"peanuts")
    bad_enc = b"v99" + b"0123456789abcdef"
    _make_cookie_db(
        db,
        [
            (
                "chatgpt.com",
                "__Secure-next-auth.session-token.0",
                "",
                session_enc,
                "/",
                0,
                1,
                1,
                1,
            ),
            ("chatgpt.com", "_dd_s", "", bad_enc, "/", 0, 1, 0, 1),
        ],
    )
    cs = _fake_cs(db)
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    out = mod.export("chrome")
    assert [c["name"] for c in out] == ["__Secure-next-auth.session-token.0"]


def test_cookie_export_raises_when_no_session_cookie_survives(tmp_path: Path) -> None:
    db = tmp_path / "Cookies"
    bad_enc = b"v99" + b"0123456789abcdef"
    _make_cookie_db(db, [("chatgpt.com", "_dd_s", "", bad_enc, "/", 0, 1, 0, 1)])
    cs = _fake_cs(db)
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=11_644_473_600)
    cc._patch_cookie_export(cs, mod)
    with pytest.raises(cc.TransportError, match="no readable ChatGPT session cookie"):
        mod.export("chrome")


def test_patch_cookie_export_is_idempotent() -> None:
    cs = types.SimpleNamespace(BROWSERS={}, _keyring_password=lambda app: b"pw")
    mod = types.SimpleNamespace(_EPOCH_DELTA_S=0)
    cc._patch_cookie_export(cs, mod)
    first = mod.export
    cc._patch_cookie_export(cs, mod)
    assert mod.export is first


# ---------------------------------------------------------------------------
# _helpers -- imports chatgpt_session from HELPERS and patches it once
# ---------------------------------------------------------------------------


_SESSION_STUB = (
    "MARKER = {marker!r}\n"
    "BROWSERS = {{}}\n"
    "\n"
    "def _keyring_password(app):\n"
    "    return b'unused'\n"
)
_COOKIES_STUB = "MARKER = {marker!r}\n"


@pytest.fixture
def stub_helpers_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HELPERS dir with stub siblings, and a guarantee that sys.modules and
    sys.path come back exactly as they were, whatever the test does.

    The marker is the tmp_path itself, so a leftover module from an earlier
    test (a cleanup bug) would be caught by the next test asserting its own
    marker rather than silently reusing a stale stub.
    """
    (tmp_path / "chatgpt_session.py").write_text(
        _SESSION_STUB.format(marker=str(tmp_path)), encoding="utf-8"
    )
    (tmp_path / "chatgpt_cookies.py").write_text(
        _COOKIES_STUB.format(marker=str(tmp_path)), encoding="utf-8"
    )
    monkeypatch.setattr(cc, "HELPERS", tmp_path)
    saved = {
        name: sys.modules.pop(name, None)
        for name in ("chatgpt_session", "chatgpt_cookies")
    }
    path_was_absent = str(tmp_path) not in sys.path
    try:
        yield tmp_path
    finally:
        for name, mod in saved.items():
            sys.modules.pop(name, None)
            if mod is not None:
                sys.modules[name] = mod
        if path_was_absent:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(tmp_path))


def test_helpers_imports_the_configured_module_and_patches_it(
    stub_helpers_dir: Path,
) -> None:
    module = cc._helpers()
    assert module.__name__ == "chatgpt_session"
    assert str(stub_helpers_dir) == module.MARKER
    assert module._rp_tolerant is True
    assert str(stub_helpers_dir) in sys.path


def test_helpers_does_not_duplicate_an_already_present_path(
    stub_helpers_dir: Path,
) -> None:
    sys.path.insert(0, str(stub_helpers_dir))
    count_before = sys.path.count(str(stub_helpers_dir))
    cc._helpers()
    assert sys.path.count(str(stub_helpers_dir)) == count_before


def test_helpers_returns_a_fresh_stub_each_test_proving_cleanup_worked(
    stub_helpers_dir: Path,
) -> None:
    """If the previous test's stub leaked, this marker would not match."""
    module = cc._helpers()
    assert str(stub_helpers_dir) == module.MARKER


# ---------------------------------------------------------------------------
# available_mb -- MemAvailable from /proc/meminfo, never fatal
# ---------------------------------------------------------------------------


def test_available_mb_converts_kib_to_mb(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(
        read_text=lambda: "MemTotal:  16384000 kB\nMemAvailable:   2048000 kB\n"
    )
    monkeypatch.setattr(cc, "Path", lambda p: fake)
    assert cc.available_mb() == pytest.approx(2000.0)


def test_available_mb_is_infinite_when_meminfo_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown must never block a run; only a real low reading should."""

    class _Boom:
        def read_text(self) -> str:
            raise OSError("no such file")

    monkeypatch.setattr(cc, "Path", lambda p: _Boom())
    assert cc.available_mb() == float("inf")


def test_available_mb_is_infinite_when_the_field_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = types.SimpleNamespace(read_text=lambda: "MemTotal: 100 kB\n")
    monkeypatch.setattr(cc, "Path", lambda p: fake)
    assert cc.available_mb() == float("inf")


# ---------------------------------------------------------------------------
# scripted_browser_pids -- Playwright's Chrome only, never the user's own
# ---------------------------------------------------------------------------


class _FakeCmdlineFile:
    def __init__(self, data: bytes, raise_oserror: bool) -> None:
        self._data = data
        self._raise = raise_oserror

    def read_bytes(self) -> bytes:
        if self._raise:
            raise OSError("process gone")
        return self._data


class _FakeProcEntry:
    def __init__(
        self, name: str, cmdline: bytes = b"", raise_oserror: bool = False
    ) -> None:
        self.name = name
        self._cmdline = cmdline
        self._raise = raise_oserror

    def __truediv__(self, other: str) -> _FakeCmdlineFile:
        assert other == "cmdline"
        return _FakeCmdlineFile(self._cmdline, self._raise)


class _FakeProcRoot:
    def __init__(self, entries: list[_FakeProcEntry]) -> None:
        self._entries = entries

    def iterdir(self) -> list[_FakeProcEntry]:
        return list(self._entries)


def test_scripted_browser_pids_of_an_empty_proc_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cc, "Path", lambda p: _FakeProcRoot([]))
    assert cc.scripted_browser_pids() == set()


def test_scripted_browser_pids_matches_only_playwrights_chrome_or_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _FakeProcEntry("111", b"/opt/google/chrome\x00--flag\x00...playwright...\x00"),
        _FakeProcEntry("222", b"/usr/lib/chromium\x00playwright-marker\x00"),
        _FakeProcEntry("333", b"/opt/google/chrome\x00--user-data-dir=/home/x\x00"),
        _FakeProcEntry("444", b"playwright\x00/usr/bin/firefox\x00"),
        _FakeProcEntry("self", b"unused"),
        _FakeProcEntry("555", b"", raise_oserror=True),
        # a shell whose script text mentions both words is not a browser:
        # this exact shape held the in-flight block for an hour, 2026-09-21
        _FakeProcEntry("666", b"/bin/bash\x00-c\x00case chrome in *playwright*\x00"),
        # Playwright's headless shell counts; a browser with no marker is the
        # user's own and never counts
        _FakeProcEntry("777", b"/x/chrome-headless-shell\x00--playwright-x\x00"),
        _FakeProcEntry("888", b"/opt/google/chrome/chrome\x00--type=renderer\x00"),
    ]
    monkeypatch.setattr(cc, "Path", lambda p: _FakeProcRoot(entries))
    assert cc.scripted_browser_pids() == {111, 222, 777}


# ---------------------------------------------------------------------------
# browser_slot -- a cross-process advisory lock plus a memory floor
# ---------------------------------------------------------------------------


@pytest.fixture
def slot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "slots"
    monkeypatch.setattr(cc, "SLOT_DIR", d)
    return d


def _generous_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "MAX_BROWSERS", 1)
    monkeypatch.setattr(cc, "MIN_AVAILABLE_MB", 100.0)
    monkeypatch.setattr(cc, "available_mb", lambda: 999_999.0)


def test_a_free_slot_is_acquired_without_waiting(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generous_budget(monkeypatch)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with cc.browser_slot(sleep=clock.sleep) as slot:
        assert slot == 0
    assert clock.calls == []


def test_the_slot_is_released_on_exit_so_a_second_acquire_succeeds(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generous_budget(monkeypatch)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with cc.browser_slot(sleep=clock.sleep) as first:
        assert first == 0
    with cc.browser_slot(sleep=clock.sleep) as second:
        assert second == 0


def test_a_slot_busy_at_first_is_acquired_once_it_frees(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generous_budget(monkeypatch)
    slot_dir.mkdir(parents=True)
    held = (slot_dir / "slot0.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)

    def fake_sleep(seconds: float) -> None:
        clock.calls.append(seconds)
        clock.now += seconds
        if seconds == 5 and not held.closed:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

    with cc.browser_slot(sleep=fake_sleep, wait=60) as slot:
        assert slot == 0
    assert 5 in clock.calls


def test_a_lock_left_by_a_dead_process_is_reclaimed(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock is released by the kernel when the holder's fd table is torn
    down, so closing without an explicit unlock stands in for the process
    dying, not merely finishing cleanly (covered by the previous test)."""
    _generous_budget(monkeypatch)
    slot_dir.mkdir(parents=True)
    held = (slot_dir / "slot0.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)

    def fake_sleep(seconds: float) -> None:
        clock.calls.append(seconds)
        clock.now += seconds
        if not held.closed:
            held.close()

    with cc.browser_slot(sleep=fake_sleep, wait=60) as slot:
        assert slot == 0


def test_every_slot_busy_past_the_wait_deadline_raises(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generous_budget(monkeypatch)
    slot_dir.mkdir(parents=True)
    held = (slot_dir / "slot0.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    try:
        with (
            pytest.raises(cc.TransportError, match="busy"),
            cc.browser_slot(sleep=clock.sleep, wait=12),
        ):
            pass
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert clock.calls == [5, 5, 5]


def test_low_memory_waits_then_raises_after_the_deadline(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "MAX_BROWSERS", 1)
    monkeypatch.setattr(cc, "MIN_AVAILABLE_MB", 4000.0)
    monkeypatch.setattr(cc, "available_mb", lambda: 100.0)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with (
        pytest.raises(cc.TransportError, match="MB available"),
        cc.browser_slot(sleep=clock.sleep, wait=20),
    ):
        pass
    assert clock.calls == [15, 15]


def test_memory_recovering_mid_wait_yields_the_slot(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "MAX_BROWSERS", 1)
    monkeypatch.setattr(cc, "MIN_AVAILABLE_MB", 4000.0)
    readings = iter([100.0, 100.0, 5000.0])
    monkeypatch.setattr(cc, "available_mb", lambda: next(readings))
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with cc.browser_slot(sleep=clock.sleep, wait=60) as slot:
        assert slot == 0
    assert clock.calls == [15, 15]


# ---------------------------------------------------------------------------
# new_chat_lock -- best-effort serialisation, never worth aborting a send
# ---------------------------------------------------------------------------


def test_new_chat_lock_acquired_immediately_yields_true(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with cc.new_chat_lock(sleep=clock.sleep) as taken:
        assert taken is True
    assert clock.calls == []


def test_new_chat_lock_busy_the_whole_wait_yields_false_without_raising(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot_dir.mkdir(parents=True)
    held = (slot_dir / "newchat.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    try:
        with cc.new_chat_lock(wait=5, sleep=clock.sleep) as taken:
            assert taken is False
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    assert clock.calls == [2, 2, 2]


def test_new_chat_lock_freed_mid_wait_is_acquired(
    slot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot_dir.mkdir(parents=True)
    held = (slot_dir / "newchat.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)

    def fake_sleep(seconds: float) -> None:
        clock.calls.append(seconds)
        clock.now += seconds
        if not held.closed:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

    with cc.new_chat_lock(wait=30, sleep=fake_sleep) as taken:
        assert taken is True
    assert clock.calls == [2]


# ---------------------------------------------------------------------------
# ChatGPTSession -- __init__ retries, _call's ladder, and the PATCH bodies
# ---------------------------------------------------------------------------


class _FakeCS:
    """Stands in for the ``chatgpt_session`` module ``_helpers()`` returns."""

    def __init__(self, session_results: list) -> None:
        self.session_results = list(session_results)
        self.set_prog_calls: list[str] = []
        self.session_calls: list[str] = []

    def set_prog(self, name: str) -> None:
        self.set_prog_calls.append(name)

    def Session(self, browser: str) -> object:
        self.session_calls.append(browser)
        result = self.session_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _make_session(
    monkeypatch: pytest.MonkeyPatch, fake_cs: _FakeCS, sleep=lambda s: None
) -> cc.ChatGPTSession:
    monkeypatch.setattr(cc, "_helpers", lambda: fake_cs)
    return cc.ChatGPTSession(browser="chrome", sleep=sleep)


def test_init_authenticates_once_when_the_first_attempt_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object()
    fake_cs = _FakeCS([backend])
    session = _make_session(monkeypatch, fake_cs)
    assert session.session is backend
    assert session.browser == "chrome"
    assert fake_cs.set_prog_calls == ["chatgpt-client"]
    assert fake_cs.session_calls == ["chrome"]


def test_init_retries_a_systemexit_with_the_auth_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient 403 at startup must not kill a resumed run outright."""
    backend = object()
    fake_cs = _FakeCS([SystemExit("no1"), SystemExit("no2"), backend])
    sleeps: list[float] = []
    session = _make_session(monkeypatch, fake_cs, sleep=sleeps.append)
    assert session.session is backend
    assert sleeps == [30.0, 90.0]


def test_init_gives_up_after_exhausting_the_auth_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits = [SystemExit("a"), SystemExit("b"), SystemExit("c"), SystemExit("d")]
    fake_cs = _FakeCS(exits)
    sleeps: list[float] = []
    with pytest.raises(cc.TransportError, match="could not authenticate after 3"):
        _make_session(monkeypatch, fake_cs, sleep=sleeps.append)
    assert sleeps == [30.0, 90.0, 180.0]


class _FakeBackend:
    """The lower-level ``session.session.call`` the real ``Session`` exposes."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    def call(self, path: str, method: str = "GET", payload=None, retries: int = 3):
        self.calls.append((path, method, payload))
        return self.responses.pop(0)


def test_call_returns_the_data_on_a_200(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend([(200, {"ok": True})])
    session = _make_session(monkeypatch, _FakeCS([backend]))
    assert session._call("/x") == {"ok": True}
    assert backend.calls == [("/x", "GET", None)]


def test_call_retries_a_429_with_the_rate_limit_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend([(429, "slow down"), (200, {"ok": True})])
    sleeps: list[float] = []
    session = _make_session(monkeypatch, _FakeCS([backend]))
    assert session._call("/x", sleep=sleeps.append) == {"ok": True}
    assert sleeps == [60.0]


def test_call_raises_after_exhausting_the_rate_limit_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend([(500, "e")] * 4)
    sleeps: list[float] = []
    session = _make_session(monkeypatch, _FakeCS([backend]))
    with pytest.raises(cc.TransportError) as exc_info:
        session._call("/x", sleep=sleeps.append)
    assert exc_info.value.status == 500
    assert sleeps == [60.0, 150.0, 300.0]


def test_call_does_not_retry_a_non_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend([(404, "nope")])
    sleeps: list[float] = []
    session = _make_session(monkeypatch, _FakeCS([backend]))
    with pytest.raises(cc.TransportError, match="HTTP 404"):
        session._call("/x", sleep=sleeps.append)
    assert sleeps == []
    assert len(backend.calls) == 1


def test_call_rebuilds_the_session_once_on_a_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run outlives its bearer token; a 403 mid-run must not be fatal."""
    backend1 = _FakeBackend([(403, "expired")])
    backend2 = _FakeBackend([(200, {"ok": True})])
    fake_cs = _FakeCS([backend1, backend2])
    session = _make_session(monkeypatch, fake_cs)
    assert session._call("/x") == {"ok": True}
    assert fake_cs.session_calls == ["chrome", "chrome"]


def test_call_raises_when_reauthentication_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend1 = _FakeBackend([(403, "expired")])
    fake_cs = _FakeCS([backend1, SystemExit("still logged out")])
    session = _make_session(monkeypatch, fake_cs)
    with pytest.raises(cc.TransportError, match="re-authentication failed"):
        session._call("/x")


def test_call_only_reauthenticates_once_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second 403 right after a fresh session means really logged out."""
    backend1 = _FakeBackend([(403, "expired")])
    backend2 = _FakeBackend([(403, "still expired")])
    fake_cs = _FakeCS([backend1, backend2])
    session = _make_session(monkeypatch, fake_cs)
    with pytest.raises(cc.TransportError, match="HTTP 403"):
        session._call("/x")
    assert fake_cs.session_calls == ["chrome", "chrome"]


def _spy_call(session: cc.ChatGPTSession, result: object) -> list[tuple]:
    calls: list[tuple] = []

    def fake(path: str, method: str = "GET", payload=None, sleep=None):
        calls.append((path, method, payload))
        return result

    session._call = fake  # type: ignore[method-assign]
    return calls


def test_get_conversation_reads_the_conversation_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    calls = _spy_call(session, {"id": "c1"})
    url = "https://chatgpt.com/c/6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    assert session.get_conversation(url) == {"id": "c1"}
    assert calls == [
        ("/backend-api/conversation/6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8", "GET", None)
    ]


def test_get_conversation_rejects_a_non_object_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    _spy_call(session, ["not", "a", "dict"])
    with pytest.raises(cc.TransportError, match="not an object"):
        session.get_conversation("c1")


def test_list_conversations_builds_the_query_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    calls = _spy_call(session, {"items": [{"id": "a"}, {"id": "b"}]})
    assert session.list_conversations(limit=5, offset=10) == [{"id": "a"}, {"id": "b"}]
    assert calls == [
        ("/backend-api/conversations?offset=10&limit=5&order=updated", "GET", None)
    ]


def test_list_conversations_of_a_non_dict_payload_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    _spy_call(session, "unexpected")
    assert session.list_conversations() == []


def test_rename_patches_the_title(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    calls = _spy_call(session, None)
    session.rename("c1", "new title")
    assert calls == [("/backend-api/conversation/c1", "PATCH", {"title": "new title"})]


def test_archive_patches_is_archived_true(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session(monkeypatch, _FakeCS([object()]))
    calls = _spy_call(session, None)
    session.archive("c1")
    assert calls == [("/backend-api/conversation/c1", "PATCH", {"is_archived": True})]


def test_delete_patches_is_visible_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same call the web app's delete button makes; must not archive instead."""
    session = _make_session(monkeypatch, _FakeCS([object()]))
    calls = _spy_call(session, None)
    session.delete("c1")
    assert calls == [("/backend-api/conversation/c1", "PATCH", {"is_visible": False})]


# ---------------------------------------------------------------------------
# wait_for_reply -- poll until the tail is a stable, finished assistant reply
# ---------------------------------------------------------------------------


def _wmsg(
    node_id: str,
    role: str,
    text: str = "",
    *,
    status: str = "finished_successfully",
) -> dict:
    return {
        "id": node_id,
        "author": {"role": role},
        "content": {"content_type": "text", "parts": [text] if text else []},
        "recipient": "all",
        "status": status,
    }


def _wconv(*msgs: dict) -> dict:
    mapping = {}
    parent = None
    for m in msgs:
        mapping[m["id"]] = {"message": m, "parent": parent}
        parent = m["id"]
    return {"mapping": mapping, "current_node": parent}


_RUNNING = _wconv(
    _wmsg("1", "user", "q"), _wmsg("2", "assistant", "partial", status="in_progress")
)
_FINISHED = _wconv(_wmsg("1", "user", "q"), _wmsg("2", "assistant", "the answer"))
_NO_USER_TURN_FINISHED = _wconv(_wmsg("1", "assistant", "the answer"))


class _PollSession:
    def __init__(self, convs: list[dict], fail_first: int = 0, fail_exc=None) -> None:
        self.convs = list(convs)
        self.fail_first = fail_first
        self.fail_exc = fail_exc or cc.TransportError("temp", 500)
        self.calls = 0

    def get_conversation(self, chat: str) -> dict:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise self.fail_exc
        idx = min(self.calls - self.fail_first, len(self.convs)) - 1
        return self.convs[idx]


def test_a_running_turn_then_two_matching_finished_polls_returns_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the check that stops waiting once a reply is truly settled."""
    session = _PollSession([_RUNNING, _FINISHED, _FINISHED])
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    got = cc.wait_for_reply(session, "c1", timeout=1000, interval=10, sleep=clock.sleep)
    assert got == "the answer"
    assert session.calls == 3


def test_a_turn_that_never_finishes_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _PollSession([_RUNNING])
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with pytest.raises(TimeoutError, match="did not finish within"):
        cc.wait_for_reply(session, "c1", timeout=25, interval=10, sleep=clock.sleep)


def test_min_user_turns_blocks_a_reply_from_before_this_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished-looking tail with no user turn yet must not be collected."""
    session = _PollSession([_NO_USER_TURN_FINISHED, _NO_USER_TURN_FINISHED])
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with pytest.raises(TimeoutError):
        cc.wait_for_reply(
            session, "c1", timeout=15, interval=10, min_user_turns=1, sleep=clock.sleep
        )


def test_a_transient_poll_failure_is_retried_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll that fails is not a step that failed; the reply is still there."""
    session = _PollSession([_FINISHED, _FINISHED], fail_first=1)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    got = cc.wait_for_reply(session, "c1", timeout=1000, interval=10, sleep=clock.sleep)
    assert got == "the answer"


def test_a_poll_failure_that_persists_past_the_deadline_is_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = cc.TransportError("still down", 500)
    session = _PollSession([_FINISHED], fail_first=999, fail_exc=boom)
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    with pytest.raises(cc.TransportError, match="still down"):
        cc.wait_for_reply(session, "c1", timeout=15, interval=10, sleep=clock.sleep)


def test_the_polling_cadence_grows_then_shortens_to_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """first_poll, then a grown interval, then a short confirm -- in order."""
    session = _PollSession([_RUNNING, _FINISHED, _FINISHED])
    clock = _FakeClock()
    monkeypatch.setattr(cc.time, "monotonic", clock.monotonic)
    got = cc.wait_for_reply(
        session,
        "c1",
        timeout=1000,
        interval=60,
        max_interval=150,
        confirm_interval=5,
        first_poll=20,
        sleep=clock.sleep,
    )
    assert got == "the answer"
    assert clock.calls == [20, 60, 5]


# ---------------------------------------------------------------------------
# ensure_desktop_env / reexec_with_playwright
# ---------------------------------------------------------------------------


def test_ensure_desktop_env_sets_defaults_from_the_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(cc.os, "getuid", lambda: 4242)
    cc.ensure_desktop_env()
    assert os.environ["XDG_RUNTIME_DIR"] == "/run/user/4242"
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"


def test_ensure_desktop_env_never_goes_through_the_patching_loader(
    monkeypatch,
) -> None:
    """``_helpers()`` patches chatgpt_session's cookie reader as a side
    effect. A defaulting helper that reached the session module through it
    patched that module for every later test in the process, and three
    tests in tests/test_session.py failed only in a full run (2026-09-20)."""

    def boom() -> None:
        raise AssertionError("ensure_desktop_env must not call _helpers()")

    monkeypatch.setattr(cc, "_helpers", boom)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    cc.ensure_desktop_env()
    assert "DBUS_SESSION_BUS_ADDRESS" in os.environ


def test_ensure_desktop_env_does_not_override_an_existing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
    monkeypatch.setattr(cc.os, "getuid", lambda: 4242)
    cc.ensure_desktop_env()
    assert os.environ["XDG_RUNTIME_DIR"] == "/custom/runtime"


def test_reexec_returns_immediately_when_playwright_already_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This venv has playwright installed, so this is the ordinary path."""
    calls: list[tuple] = []
    monkeypatch.setattr(cc.os, "execve", lambda *a: calls.append(a))
    cc.reexec_with_playwright()
    assert calls == []


def test_reexec_raises_when_playwright_missing_and_no_venv_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setattr(cc, "PW_PYTHON", str(tmp_path / "does-not-exist" / "python"))
    monkeypatch.delenv("CHATGPT_SEND_REEXEC", raising=False)
    with pytest.raises(SystemExit, match="playwright missing"):
        cc.reexec_with_playwright()


def test_reexec_raises_when_already_re_execed_even_if_the_venv_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CHATGPT_SEND_REEXEC means this is the second attempt; looping is a bug."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    venv_python = tmp_path / "python"
    venv_python.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cc, "PW_PYTHON", str(venv_python))
    monkeypatch.setenv("CHATGPT_SEND_REEXEC", "1")
    with pytest.raises(SystemExit, match="playwright missing"):
        cc.reexec_with_playwright()


def test_reexec_execves_into_the_playwright_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    venv_python = tmp_path / "python"
    venv_python.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cc, "PW_PYTHON", str(venv_python))
    monkeypatch.delenv("CHATGPT_SEND_REEXEC", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(cc.os, "execve", lambda *a: calls.append(a))
    cc.reexec_with_playwright(["--flag", "x"])
    assert len(calls) == 1
    exe, argv, env = calls[0]
    assert exe == str(venv_python)
    assert argv[0] == str(venv_python)
    assert argv[1] == os.path.abspath(sys.argv[0])
    assert argv[2:] == ["--flag", "x"]
    assert env["CHATGPT_SEND_REEXEC"] == "1"


def test_reexec_defaults_argv_to_sys_argv_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    venv_python = tmp_path / "python"
    venv_python.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cc, "PW_PYTHON", str(venv_python))
    monkeypatch.delenv("CHATGPT_SEND_REEXEC", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(cc.os, "execve", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["prog", "a", "b"])
    cc.reexec_with_playwright()
    _, argv, _ = calls[0]
    assert argv[2:] == ["a", "b"]


# ---------------------------------------------------------------------------
# virtual_display -- Xvfb by default, the real display when visible=True
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_display_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registers all four env vars with monkeypatch so it restores them,
    even though the code under test sets some of them directly."""
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakePopen:
    def __init__(self) -> None:
        self.terminated = False
        self.waited_timeout: float | None = None
        self.wait_raises: Exception | None = None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> None:
        self.waited_timeout = timeout
        if self.wait_raises:
            raise self.wait_raises


def _exists_false_once_then_true():
    """False for the free-display-number pick, True from then on (the
    socket 'appearing'), so the wait loop never actually spins."""
    state = {"n": 0}

    def fake(path: str) -> bool:
        state["n"] += 1
        return state["n"] > 1

    return fake


def test_visible_true_sets_display_zero_without_starting_xvfb(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None, tmp_path: Path
) -> None:
    which_calls: list[str] = []
    monkeypatch.setattr(
        cc.shutil, "which", lambda name: which_calls.append(name) or "/usr/bin/Xvfb"
    )
    popen_calls: list[tuple] = []
    monkeypatch.setattr(
        cc.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k))
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "wayland-0").touch()
    with cc.virtual_display(visible=True):
        assert os.environ["DISPLAY"] == ":0"
        assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert which_calls == []  # visible=True short-circuits before shutil.which
    assert popen_calls == []
    assert "DISPLAY" not in os.environ
    assert "WAYLAND_DISPLAY" not in os.environ


def test_visible_true_does_not_clobber_an_existing_display(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    monkeypatch.setenv("DISPLAY", ":42")
    with cc.virtual_display(visible=True):
        assert os.environ["DISPLAY"] == ":42"
    assert os.environ["DISPLAY"] == ":42"


def test_xvfb_is_started_and_terminated_on_a_headless_host(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/Xvfb")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setattr(cc.os.path, "exists", _exists_false_once_then_true())
    fake_proc = _FakePopen()
    popen_calls: list[tuple] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return fake_proc

    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    with cc.virtual_display(visible=False):
        assert os.environ["DISPLAY"] == ":99"
        assert "WAYLAND_DISPLAY" not in os.environ

    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert fake_proc.terminated is True
    assert fake_proc.waited_timeout == 5
    argv = popen_calls[0][0]
    assert argv[0] == "/usr/bin/Xvfb"
    assert argv[1] == ":99"


def test_xvfb_wait_survives_a_terminate_timeout(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/Xvfb")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setattr(cc.os.path, "exists", _exists_false_once_then_true())
    import subprocess as sp

    fake_proc = _FakePopen()
    fake_proc.wait_raises = sp.TimeoutExpired(cmd="Xvfb", timeout=5)
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: fake_proc)
    with cc.virtual_display(visible=False):
        pass
    assert fake_proc.terminated is True


def _exists_sequence(*results: bool):
    """Scripted answers for os.path.exists; True once the script runs out."""
    it = iter(results)

    def fake(path: str) -> bool:
        return next(it, True)

    return fake


def test_xvfb_picks_the_next_free_display_number_and_waits_for_its_socket(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    """:99 already taken, and the socket takes one extra poll to appear."""
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/Xvfb")
    sleeps: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        cc.os.path, "exists", _exists_sequence(True, False, False, True)
    )
    fake_proc = _FakePopen()
    popen_calls: list[tuple] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return fake_proc

    monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
    with cc.virtual_display(visible=False):
        assert os.environ["DISPLAY"] == ":100"
    assert popen_calls[0][0][1] == ":100"
    assert 0.05 in sleeps


def test_xvfb_missing_falls_back_to_the_onscreen_display(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    popen_calls: list[tuple] = []
    monkeypatch.setattr(
        cc.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k))
    )
    with cc.virtual_display(visible=False):
        assert os.environ["DISPLAY"] == ":0"
    assert popen_calls == []


def test_an_exception_inside_the_block_still_restores_env_and_stops_xvfb(
    monkeypatch: pytest.MonkeyPatch, clean_display_env: None
) -> None:
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/Xvfb")
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setattr(cc.os.path, "exists", _exists_false_once_then_true())
    fake_proc = _FakePopen()
    monkeypatch.setattr(cc.subprocess, "Popen", lambda *a, **k: fake_proc)
    with pytest.raises(RuntimeError, match="boom"), cc.virtual_display(visible=False):
        raise RuntimeError("boom")
    assert fake_proc.terminated is True
    assert "DISPLAY" not in os.environ
