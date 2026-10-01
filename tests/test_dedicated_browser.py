"""dedicated_browser.py (SKILL.md, "Sending through the dedicated browser
window"): the launch line, the environment a desktop window needs, the
status and stop paths, the foreground run for systemd, and the user unit,
all over fakes -- no Chrome, no port, no systemctl."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dedicated_browser as db  # noqa: E402

ALIVE = {"Browser": "Chrome/154.0.8037.92", "Protocol-Version": "1.3"}


@pytest.fixture
def quiet_log(monkeypatch, tmp_path: Path) -> Path:
    log = tmp_path / "state" / "dedicated-browser.log"
    monkeypatch.setattr(db, "LOG_FILE", log)
    return log


def _args(tmp_path: Path, **extra) -> SimpleNamespace:
    base = {
        "port": 9222,
        "profile": tmp_path / "profile",
        "json": None,
        "install": False,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def test_chrome_args_name_the_profile_the_port_and_the_start_page(tmp_path):
    args = db.chrome_args(9222, tmp_path / "p")
    assert args[1] == f"--user-data-dir={tmp_path / 'p'}"
    assert "--remote-debugging-port=9222" in args
    assert "--no-first-run" in args
    assert args[-1] == "https://chatgpt.com/"


def test_chrome_binary_falls_back_to_the_bare_name(monkeypatch) -> None:
    monkeypatch.setattr(db.shutil, "which", lambda name: None)
    assert db.chrome_binary() == "google-chrome"
    monkeypatch.setattr(db.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert db.chrome_binary() == "/usr/bin/google-chrome"


def test_desktop_env_defaults_what_cron_lacks_and_keeps_what_is_set() -> None:
    env = db.desktop_env({"HOME": "/home/x", "WAYLAND_DISPLAY": "wayland-1"})
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["XDG_SESSION_TYPE"] == "wayland"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
    assert env["HOME"] == "/home/x"


def test_version_and_tabs_read_the_endpoint_and_survive_a_dead_one(monkeypatch):
    monkeypatch.setattr(db.cc, "dedicated_browser_version", lambda url: ALIVE)
    assert db.version(9222) == ALIVE

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    pages = [
        {"type": "page", "url": "https://chatgpt.com/", "title": "ChatGPT"},
        {"type": "browser_ui", "url": "chrome://x"},
        {"type": "page", "url": "https://example.invalid/"},
    ]
    monkeypatch.setattr(
        db.urllib.request,
        "urlopen",
        lambda url, timeout=0: Resp(json.dumps(pages).encode()),
    )
    assert [t["url"] for t in db.tabs(9222)] == [
        "https://chatgpt.com/",
        "https://example.invalid/",
    ]

    def boom(url, timeout=0):
        raise OSError("refused")

    monkeypatch.setattr(db.urllib.request, "urlopen", boom)
    assert db.tabs(9222) == []


def test_wait_for_polls_until_the_state_or_the_deadline(monkeypatch) -> None:
    answers = iter([None, None, ALIVE])
    monkeypatch.setattr(db, "version", lambda port: next(answers, ALIVE))
    slept: list[float] = []
    assert db.wait_for(9222, up=True, seconds=10, sleep=slept.append) is True
    assert slept == [1.0, 1.0]
    monkeypatch.setattr(db, "version", lambda port: ALIVE)
    clock = iter([0.0, 0.5, 1.5, 2.5])
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))
    assert db.wait_for(9222, up=False, seconds=1, sleep=lambda s: None) is False


def test_profile_pids_name_the_browser_process_only(monkeypatch, tmp_path) -> None:
    profile = tmp_path / "p"
    marker = f"--user-data-dir={profile}"
    monkeypatch.setattr(
        db.cc,
        "_process_argvs",
        lambda: iter(
            [
                (10, ["/opt/google/chrome/chrome"]),  # the owner's own Chrome
                (
                    20,
                    ["/usr/bin/google-chrome", marker, "--remote-debugging-port=9222"],
                ),
                (
                    21,
                    [
                        "/opt/google/chrome/chrome",
                        "--type=renderer",
                        marker,
                        "--remote-debugging-port=9222",
                    ],
                ),
                (30, ["/usr/bin/google-chrome", marker]),  # no port: not ours
            ]
        ),
    )
    assert db.profile_pids(profile) == [20]


def test_launch_starts_chrome_detached_with_the_desktop_env(
    monkeypatch, tmp_path, quiet_log
):
    calls: list[tuple] = []

    class Proc:
        def wait(self):
            return 0

    def popen(args, **kw):
        calls.append((args, kw))
        return Proc()

    monkeypatch.setattr(db.subprocess, "Popen", popen)
    db.launch(9222, tmp_path / "profile", detached=True)
    args, kw = calls[0]
    assert args[1] == f"--user-data-dir={tmp_path / 'profile'}"
    assert kw["start_new_session"] is True
    assert kw["env"]["XDG_RUNTIME_DIR"]
    assert (tmp_path / "profile").is_dir()
    assert quiet_log.parent.is_dir()


def test_start_reports_a_running_browser_without_launching(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(db, "version", lambda port: ALIVE)
    monkeypatch.setattr(db, "launch", lambda *a, **k: pytest.fail("launched"))
    assert db.cmd_start(_args(tmp_path)) == 0
    assert "already running: Chrome/154.0.8037.92" in capsys.readouterr().out


def test_start_launches_waits_and_reports(monkeypatch, tmp_path, capsys) -> None:
    answers = iter([None, None, ALIVE, ALIVE])
    monkeypatch.setattr(db, "version", lambda port: next(answers, ALIVE))
    launched: list[tuple] = []
    monkeypatch.setattr(
        db,
        "launch",
        lambda port, profile, detached: launched.append((port, profile, detached)),
    )
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    assert db.cmd_start(_args(tmp_path)) == 0
    assert launched == [(9222, tmp_path / "profile", True)]
    out = capsys.readouterr().out
    assert "started: Chrome/154.0.8037.92" in out
    assert "log in to chatgpt.com" in out


def test_start_fails_when_nothing_answers_in_time(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(db, "version", lambda port: None)
    monkeypatch.setattr(db, "launch", lambda *a, **k: None)
    monkeypatch.setattr(db, "START_WAIT_SECONDS", 0.0)
    assert db.cmd_start(_args(tmp_path)) == 1
    assert "nothing answers" in capsys.readouterr().err


def test_run_waits_for_a_hand_started_instance_then_supervises_its_own(
    monkeypatch, tmp_path
):
    answers = iter([ALIVE, ALIVE, None])
    monkeypatch.setattr(db, "version", lambda port: next(answers, None))
    slept: list[float] = []

    class Proc:
        def wait(self):
            return 7

    launched: list[bool] = []

    def launch(port, profile, detached):
        launched.append(detached)
        return Proc()

    monkeypatch.setattr(db, "launch", launch)
    assert db.cmd_run(_args(tmp_path), sleep=slept.append) == 7
    assert slept == [db.RUN_POLL_SECONDS, db.RUN_POLL_SECONDS]
    assert launched == [False]


def test_status_reports_tabs_and_writes_json(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(db, "version", lambda port: ALIVE)
    monkeypatch.setattr(
        db,
        "tabs",
        lambda port: [
            {"type": "page", "url": "https://chatgpt.com/"},
            {"type": "page", "url": "https://example.invalid/"},
        ],
    )
    out_json = tmp_path / "status.json"
    assert db.cmd_status(_args(tmp_path, json=str(out_json))) == 0
    record = json.loads(out_json.read_text())
    assert record["running"] is True
    assert record["tabs"] == 2
    assert record["chatgpt_tabs"] == 1
    assert "2 tab(s), 1 on chatgpt.com" in capsys.readouterr().out


def test_status_is_one_when_nothing_answers(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(db, "version", lambda port: None)
    assert db.cmd_status(_args(tmp_path)) == 1
    assert "not running" in capsys.readouterr().out


def test_stop_ends_the_profile_s_browser_and_waits_for_the_port(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(db, "profile_pids", lambda profile: [20, 21])
    answers = iter([ALIVE, None])
    monkeypatch.setattr(db, "version", lambda port: next(answers, None))
    killed: list[tuple] = []

    def kill(pid, sig):
        killed.append((pid, sig))
        if pid == 21:
            raise OSError("gone")

    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    assert db.cmd_stop(_args(tmp_path), kill=kill) == 0
    assert [pid for pid, _ in killed] == [20, 21]
    assert "stopped 2 process(es)" in capsys.readouterr().out


def test_stop_is_fine_with_nothing_running_and_fails_when_the_port_stays(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(db, "profile_pids", lambda profile: [])
    monkeypatch.setattr(db, "version", lambda port: None)
    assert db.cmd_stop(_args(tmp_path)) == 0
    assert "not running" in capsys.readouterr().out
    monkeypatch.setattr(db, "profile_pids", lambda profile: [20])
    monkeypatch.setattr(db, "version", lambda port: ALIVE)
    monkeypatch.setattr(db, "STOP_WAIT_SECONDS", 0.0)
    assert db.cmd_stop(_args(tmp_path), kill=lambda pid, sig: None) == 1
    assert "still answering" in capsys.readouterr().err


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(
    monkeypatch, tmp_path, capsys
):
    """2026-10-01: one SIGTERM left the hand-started window up past 15 s;
    the login lives on disk, so a kill loses nothing."""
    monkeypatch.setattr(db, "profile_pids", lambda profile: [20])
    answers = iter([ALIVE, None])  # after SIGTERM: still up; after SIGKILL: gone
    monkeypatch.setattr(db, "version", lambda port: next(answers, None))
    monkeypatch.setattr(db, "STOP_WAIT_SECONDS", 0.0)
    signals: list = []
    assert db.cmd_stop(_args(tmp_path), kill=lambda pid, sig: signals.append(sig)) == 0
    assert signals == [db.signal.SIGTERM, db.signal.SIGKILL]
    assert "killed 1 process(es)" in capsys.readouterr().out


def test_unit_text_names_the_script_the_port_and_the_profile(tmp_path) -> None:
    text = db.unit_text(9222, tmp_path / "p")
    assert "ExecStart=/usr/bin/python3 " in text
    assert "dedicated_browser.py run --port 9222 --profile " in text
    assert "Restart=always" in text
    assert "WantedBy=default.target" in text


def test_unit_prints_or_installs_and_enables(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(db, "UNIT_DIR", tmp_path / "systemd")
    assert db.cmd_unit(_args(tmp_path)) == 0
    assert "[Service]" in capsys.readouterr().out
    commands: list[list[str]] = []

    def run(command, **kw):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert db.cmd_unit(_args(tmp_path, install=True), run=run) == 0
    assert (tmp_path / "systemd" / db.UNIT_NAME).read_text().startswith("[Unit]")
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", db.UNIT_NAME],
    ]

    def failing(command, **kw):
        return subprocess.CompletedProcess(command, 1, "", "no manager")

    assert db.cmd_unit(_args(tmp_path, install=True), run=failing) == 1
    assert "daemon-reload failed: no manager" in capsys.readouterr().err


def test_main_dispatches_and_parses_the_options(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(db, "version", lambda port: None)
    out_json = tmp_path / "s.json"
    assert db.main(["status", "--port", "9333", "--json", str(out_json)]) == 1
    assert json.loads(out_json.read_text())["endpoint"] == "http://127.0.0.1:9333"
    with pytest.raises(SystemExit) as exc:
        db.main(["bogus"])
    assert exc.value.code == 2


def test_profile_extensions_lists_web_store_extensions_only(tmp_path) -> None:
    """F-2026-10-01-32: the dedicated profile should carry none; a synced
    duplicate-tab blocker bounced the home page. Only Web Store entries
    count (Chrome's own components are not the owner's doing)."""
    prefs = tmp_path / "profile" / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        "aaaa": {
                            "from_webstore": True,
                            "manifest": {"name": "Clutter Free"},
                        },
                        "bbbb": {
                            "from_webstore": False,
                            "manifest": {"name": "Chrome PDF Viewer"},
                        },
                        "cccc": {
                            "from_webstore": True,
                            "manifest": {"name": "AdBlock"},
                        },
                        "dddd": "junk",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert db.profile_extensions(tmp_path / "profile") == ["AdBlock", "Clutter Free"]
    assert db.profile_extensions(tmp_path / "missing") == []
    prefs.write_text("not json", encoding="utf-8")
    assert db.profile_extensions(tmp_path / "profile") == []


def test_status_names_the_profile_s_extensions(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(db, "version", lambda port: ALIVE)
    monkeypatch.setattr(db, "tabs", lambda port: [])
    monkeypatch.setattr(
        db, "profile_extensions", lambda profile: ["AdBlock", "Clutter Free"]
    )
    record = tmp_path / "status.json"
    assert db.cmd_status(_args(tmp_path, json=str(record))) == 0
    out = capsys.readouterr().out
    assert "2 Web Store extension(s) in the profile (AdBlock, Clutter Free)" in out
    assert "sign it out of Chrome" in out
    assert json.loads(record.read_text())["extensions"] == ["AdBlock", "Clutter Free"]
