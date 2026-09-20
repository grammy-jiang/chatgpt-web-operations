"""Tests for the ``main()`` paths and remaining branches of the read commands.

``tests/test_commands.py`` already covers the pure functions each command is
built from. What is missing is the thin wiring around them: argument
parsing, the live session, and the process-level plumbing in ``_common.py``.
This file (and its sibling ``test_command_mains_2.py``) close that gap for
``_common.py``, ``round_state.py``, ``probe_cookies.py`` and
``model_settings.py``.

Every test names the failure it defends against, the same rule
``test_commands.py`` uses. Nothing here touches the network, the keyring or
a real cookie database: fakes and temporary sqlite files stand in for
Chrome's own.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common as common  # noqa: E402
import model_settings  # noqa: E402
import probe_cookies  # noqa: E402
import round_state  # noqa: E402

# ---------------------------------------------------------------------------
# _common.ensure_venv -- four branches, none of which may ever loop or exec
# for real inside a test
# ---------------------------------------------------------------------------


def test_ensure_venv_is_a_no_op_once_the_reexec_flag_is_set(monkeypatch) -> None:
    """A second re-exec would loop forever; the flag must short-circuit it."""
    monkeypatch.setenv(common.REEXEC_FLAG, "1")
    calls: list[tuple] = []
    monkeypatch.setattr(os, "execve", lambda *a: calls.append(a))
    common.ensure_venv()
    assert calls == []


def test_ensure_venv_is_a_no_op_when_already_running_inside_the_venv(
    monkeypatch, tmp_path: Path
) -> None:
    """Every normal run: the venv's own python started the command already."""
    monkeypatch.delenv(common.REEXEC_FLAG, raising=False)
    fake_venv = tmp_path / "venv"
    fake_venv.mkdir()
    monkeypatch.setattr(common, "VENV", fake_venv)
    monkeypatch.setattr(sys, "prefix", str(fake_venv))
    calls: list[tuple] = []
    monkeypatch.setattr(os, "execve", lambda *a: calls.append(a))
    common.ensure_venv()
    assert calls == []


def test_ensure_venv_exits_with_the_bootstrap_hint_when_the_venv_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    """A missing venv must say how to create it, not fail some other way."""
    monkeypatch.delenv(common.REEXEC_FLAG, raising=False)
    fake_venv = tmp_path / "venv"  # deliberately never created
    monkeypatch.setattr(common, "VENV", fake_venv)
    monkeypatch.setattr(common, "VENV_PYTHON", fake_venv / "bin" / "python")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit) as excinfo:
        common.ensure_venv()
    message = str(excinfo.value)
    assert "no virtual environment" in message
    assert "bootstrap.sh" in message


def test_ensure_venv_re_execs_under_the_venv_python_with_flag_and_full_argv(
    monkeypatch, tmp_path: Path
) -> None:
    """The re-exec must carry the venv python, the script path and argv."""
    monkeypatch.delenv(common.REEXEC_FLAG, raising=False)
    fake_venv = tmp_path / "venv"
    fake_python = fake_venv / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("")  # only .exists() is ever checked
    monkeypatch.setattr(common, "VENV", fake_venv)
    monkeypatch.setattr(common, "VENV_PYTHON", fake_python)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(sys, "argv", ["cmd.py", "--flag", "x"])
    monkeypatch.setenv("KEEP_ME", "1")
    calls: list[tuple] = []
    monkeypatch.setattr(os, "execve", lambda *a: calls.append(a))

    common.ensure_venv()

    assert len(calls) == 1
    path, argv, env = calls[0]
    assert path == str(fake_python)
    assert argv == [str(fake_python), os.path.abspath("cmd.py"), "--flag", "x"]
    assert env[common.REEXEC_FLAG] == "1"
    assert env["KEEP_ME"] == "1"  # the rest of the environment must survive


# ---------------------------------------------------------------------------
# _common.load_client -- the sys.path insertion the bundled client needs
# ---------------------------------------------------------------------------


def test_load_client_imports_the_bundled_client_module() -> None:
    module = common.load_client()
    assert module.__name__ == "chatgpt_client"
    assert hasattr(module, "ChatGPTSession")


def test_load_client_inserts_the_scripts_directory_when_it_is_missing(
    monkeypatch,
) -> None:
    """The bundled client must import even before anything else adds the path."""
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(SCRIPTS)])
    assert str(SCRIPTS) not in sys.path
    common.load_client()
    assert str(SCRIPTS) in sys.path


def test_load_client_inserts_the_scripts_directory_at_most_once(monkeypatch) -> None:
    """A duplicated sys.path entry is harmless but is a sign the guard broke."""
    monkeypatch.setattr(sys, "path", [str(SCRIPTS), *sys.path])
    before = len(sys.path)
    common.load_client()
    assert len(sys.path) == before


# ---------------------------------------------------------------------------
# _common.open_session -- success returns the session, failure names why
# ---------------------------------------------------------------------------


def test_open_session_returns_the_session_the_client_constructs(monkeypatch) -> None:
    class _FakeClientModule:
        def ChatGPTSession(self, browser: str) -> str:
            assert browser == "chrome"
            return "a-live-session"

    monkeypatch.setattr(common, "load_client", lambda: _FakeClientModule())
    assert common.open_session("chrome") == "a-live-session"


def test_open_session_exits_1_and_names_the_reason_it_could_not_open(
    monkeypatch, capsys
) -> None:
    """A failed session must say why, not just disappear into a stack trace."""

    class _FakeClientModule:
        def ChatGPTSession(self, browser: str):
            raise RuntimeError("no readable session cookie")

    monkeypatch.setattr(common, "load_client", lambda: _FakeClientModule())
    with pytest.raises(SystemExit) as excinfo:
        common.open_session()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "could not open a session" in out
    assert "no readable session cookie" in out


# ---------------------------------------------------------------------------
# round_state -- the two branches not reached through round_status's own
# existing tests: a missing JSONL file and a corrupted skipped.json
# ---------------------------------------------------------------------------


def test_read_jsonl_returns_empty_for_a_file_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    """A round's screened.jsonl may not exist yet; that is not a parse error."""
    assert round_state.read_jsonl(tmp_path / "screened.jsonl") == []


def test_skipped_papers_survives_a_corrupted_skipped_json(tmp_path: Path) -> None:
    """A half-written skipped.json must not crash the status check."""
    run_dir = tmp_path / "run"
    (run_dir / "analysis").mkdir(parents=True)
    (run_dir / "analysis" / "skipped.json").write_text("{not valid json")
    assert round_state.skipped_papers(run_dir) == {}


# ---------------------------------------------------------------------------
# shared: a temporary Chrome-shaped cookie database
# ---------------------------------------------------------------------------


def _cookie_db(path: Path, rows: list[tuple]) -> Path:
    """A sqlite file with the one table and columns probe_cookies.read_jar
    reads. A row may omit ``expires_utc``; it is then 0, a session cookie."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (name TEXT, host_key TEXT, encrypted_value BLOB, "
        "expires_utc INTEGER)"
    )
    con.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?)",
        [row if len(row) == 4 else (*row, 0) for row in rows],
    )
    con.commit()
    con.close()
    return path


class _FakeCookieHelpers:
    """Stands in for ``chatgpt_session``: only ``BROWSERS`` is ever read."""

    def __init__(self, db_path: Path, app: str = "chrome-app") -> None:
        self.BROWSERS = {"chrome": (str(db_path), app)}


class _FakeCookieClient:
    """Stands in for ``chatgpt_client``, scoped to what probe_cookies.main uses."""

    def __init__(self, db_path: Path, decrypt) -> None:
        self._cs = _FakeCookieHelpers(db_path)
        self._decrypt = decrypt

    def _helpers(self):
        return self._cs

    def _tolerant_decryptor(self, cs, app):
        assert cs is self._cs
        return self._decrypt


# ---------------------------------------------------------------------------
# probe_cookies.read_jar -- real sqlite, so the SQL itself is exercised
# ---------------------------------------------------------------------------


def test_read_jar_reads_every_chatgpt_cookie_ordered_by_name(tmp_path: Path) -> None:
    db = _cookie_db(
        tmp_path / "Cookies",
        [
            ("_dd_s", "chatgpt.com", b"enc-analytics"),
            ("__Secure-next-auth.session-token.0", "chatgpt.com", b"enc-session"),
            ("unrelated", "example.com", b"enc-other"),  # a different host
        ],
    )
    rows = probe_cookies.read_jar(db)
    assert rows == [
        ("__Secure-next-auth.session-token.0", "chatgpt.com", b"enc-session", 0),
        ("_dd_s", "chatgpt.com", b"enc-analytics", 0),
    ]


def test_read_jar_copies_the_database_rather_than_opening_it_in_place(
    tmp_path: Path,
) -> None:
    """The live browser locks its Cookies file; read_jar must work on a copy."""
    db = _cookie_db(tmp_path / "Cookies", [("a", "chatgpt.com", b"x")])
    before = db.read_bytes()
    probe_cookies.read_jar(db)
    assert db.read_bytes() == before  # untouched, and still openable afterwards
    assert probe_cookies.read_jar(db) == [("a", "chatgpt.com", b"x", 0)]


def test_read_jar_keeps_the_expiry_and_reads_a_null_as_zero(tmp_path: Path) -> None:
    db = _cookie_db(
        tmp_path / "Cookies",
        [
            ("a", "chatgpt.com", b"x", 13_400_000_000_000_000),
            ("b", "chatgpt.com", b"y", None),
        ],
    )
    assert probe_cookies.read_jar(db) == [
        ("a", "chatgpt.com", b"x", 13_400_000_000_000_000),
        ("b", "chatgpt.com", b"y", 0),
    ]


# ---------------------------------------------------------------------------
# probe_cookies.main
# ---------------------------------------------------------------------------


def test_main_exits_0_when_the_session_cookie_decrypts(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    db = _cookie_db(
        tmp_path / "Cookies",
        [("__Secure-next-auth.session-token.0", "chatgpt.com", b"session-blob")],
    )
    monkeypatch.setattr(
        probe_cookies,
        "load_client",
        lambda: _FakeCookieClient(db, decrypt=lambda enc: enc.decode()),
    )
    assert probe_cookies.main([]) == 0
    out = capsys.readouterr().out
    assert "The session cookie is readable" in out


def test_main_reports_an_unreadable_analytics_cookie_without_failing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The whole point: one bad analytics cookie must not end the session."""
    db = _cookie_db(
        tmp_path / "Cookies",
        [
            ("__Secure-next-auth.session-token.0", "chatgpt.com", b"session-blob"),
            ("_dd_s", "chatgpt.com", b"bad-blob"),
        ],
    )

    def decrypt(enc: bytes) -> str:
        if enc == b"bad-blob":
            raise ValueError("bad padding")
        return enc.decode()

    monkeypatch.setattr(
        probe_cookies, "load_client", lambda: _FakeCookieClient(db, decrypt=decrypt)
    )
    assert probe_cookies.main([]) == 0
    out = capsys.readouterr().out
    assert "unreadable:" in out
    assert "1 readable, 1 unreadable" in out


def test_main_json_reports_counts_and_the_session_horizon(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """The daily health check reads this file for one number, the session
    token's days left; the text output says the same in words."""
    # 2026-12-19T10:59:49Z as Chrome stores it: microseconds since 1601.
    dec_19 = int(datetime(2026, 12, 19, 10, 59, 49, tzinfo=UTC).timestamp())
    expires = (dec_19 + probe_cookies.WEBKIT_EPOCH_DELTA_S) * 1_000_000
    db = _cookie_db(
        tmp_path / "Cookies",
        [
            ("__Secure-next-auth.session-token.0", "chatgpt.com", b"s0", expires),
            ("__Secure-next-auth.session-token.1", "chatgpt.com", b"s1", expires),
            ("_dd_s", "chatgpt.com", b"bad-blob", 0),
        ],
    )

    def decrypt(enc: bytes) -> str:
        if enc == b"bad-blob":
            raise ValueError("bad padding")
        return enc.decode()

    monkeypatch.setattr(
        probe_cookies, "load_client", lambda: _FakeCookieClient(db, decrypt=decrypt)
    )
    monkeypatch.setattr(probe_cookies.time, "time", lambda: dec_19 - 90 * 86400)
    out_path = tmp_path / "cookies.json"

    assert probe_cookies.main(["--json", str(out_path)]) == 0

    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc == {
        "readable": 2,
        "unreadable": 1,
        "session_readable": True,
        "session_expires": "2026-12-19",
        "session_days_left": 90.0,
    }
    out = capsys.readouterr().out
    assert "session token expires 2026-12-19 (90.0 days left)" in out
    assert "2026-12-19 (90.0 d)" in out  # the expires column


def test_main_exits_1_when_the_session_cookie_itself_is_unreadable(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """An analytics cookie decrypting fine must not hide a dead session cookie."""
    db = _cookie_db(tmp_path / "Cookies", [("_dd_s", "chatgpt.com", b"ok-blob")])
    monkeypatch.setattr(
        probe_cookies,
        "load_client",
        lambda: _FakeCookieClient(db, decrypt=lambda enc: enc.decode()),
    )
    assert probe_cookies.main([]) == 1
    assert "NOT readable" in capsys.readouterr().out


def test_main_exits_1_when_there_is_no_cookie_database_for_the_browser(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    missing = tmp_path / "does-not-exist" / "Cookies"
    monkeypatch.setattr(
        probe_cookies, "load_client", lambda: _FakeCookieClient(missing, decrypt=str)
    )
    assert probe_cookies.main([]) == 1
    assert "no cookie DB for chrome" in capsys.readouterr().out


def test_main_never_prints_a_decrypted_cookie_value(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A secret leaking into a log is worse than the bug this command finds."""
    secret = "super-secret-session-value"
    db = _cookie_db(
        tmp_path / "Cookies",
        [("__Secure-next-auth.session-token.0", "chatgpt.com", secret.encode())],
    )
    monkeypatch.setattr(
        probe_cookies,
        "load_client",
        lambda: _FakeCookieClient(db, decrypt=lambda enc: enc.decode()),
    )
    probe_cookies.main([])
    assert secret not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# model_settings.presets_of -- the one branch test_commands.py leaves out
# ---------------------------------------------------------------------------


def test_presets_of_is_empty_when_the_version_is_not_in_the_payload() -> None:
    """No matching version must read as "no presets", never as a KeyError."""
    models = {"versions": [{"id": "5.5", "intelligence_presets": [{"id": 0}]}]}
    assert model_settings.presets_of(models, "latest") == []
    assert model_settings.presets_of({}, "latest") == []


# ---------------------------------------------------------------------------
# model_settings.main
# ---------------------------------------------------------------------------

_MODELS_PAYLOAD = {
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
    "models": [
        {
            "slug": "gpt-5-6-thinking",
            "configurable_thinking_effort": True,
            "thinking_efforts": [{"thinking_effort": "max"}],
        }
    ],
}


def _settings_payload(model: str, effort: str) -> dict:
    """A minimal settings/user payload with one web (model, effort) pair."""
    return {
        "settings": {
            "last_used_model_config": {
                "slugs": {"web": model},
                "juices": {"web": {model: effort} if effort else {}},
            }
        }
    }


class _FakeModelsBackend:
    """``session.session.call`` over two payloads, matched by path prefix."""

    def __init__(self, models: dict, settings: dict) -> None:
        self.models = models
        self.settings = settings

    def call(self, path: str) -> tuple[int, dict]:
        if path.startswith("/backend-api/settings/user"):
            return 200, self.settings
        return 200, self.models


class _FakeModelsSession:
    def __init__(self, models: dict, settings: dict) -> None:
        self.session = _FakeModelsBackend(models, settings)


class _FakeModelsClient:
    EFFORTS = ("min", "standard", "extended", "max")


def _wire_model_settings(monkeypatch, *, settings: dict) -> None:
    monkeypatch.setattr(model_settings, "load_client", lambda: _FakeModelsClient())
    monkeypatch.setattr(
        model_settings,
        "open_session",
        lambda *a, **k: _FakeModelsSession(_MODELS_PAYLOAD, settings),
    )


def test_main_exits_0_and_names_the_preset_when_the_server_record_resolves(
    monkeypatch, capsys
) -> None:
    _wire_model_settings(
        monkeypatch, settings=_settings_payload("gpt-5-6-thinking", "max")
    )
    assert model_settings.main([]) == 0
    out = capsys.readouterr().out
    assert "preset 'Extra High'" in out
    assert "send_prompt.py --effort/--model rewrite it in flight" in out


def test_main_exits_1_when_the_server_has_no_last_used_model_config(
    monkeypatch, capsys
) -> None:
    _wire_model_settings(monkeypatch, settings={"settings": {}})
    assert model_settings.main([]) == 1
    assert "no last_used_model_config" in capsys.readouterr().out


def test_main_exits_1_when_the_server_record_matches_no_known_preset(
    monkeypatch, capsys
) -> None:
    """A model/effort pair the slider does not offer must be reported, not guessed."""
    _wire_model_settings(
        monkeypatch, settings=_settings_payload("gpt-5-6-thinking", "ultra")
    )
    assert model_settings.main([]) == 1
    assert "matches no preset" in capsys.readouterr().out
