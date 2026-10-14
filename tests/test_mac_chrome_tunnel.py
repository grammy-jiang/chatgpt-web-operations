"""T0: deterministic, non-network macOS reverse CDP tunnel LaunchAgent."""

from __future__ import annotations

import plistlib
import stat
from pathlib import Path
from types import SimpleNamespace

import mac_chrome_tunnel as mac
import pytest


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    ssh = root / ".ssh"
    ssh.mkdir(parents=True)
    key = ssh / "android-mcp-chrome-tunnel"
    key.write_text("fixture-private-key-only")
    key.chmod(0o600)
    known = ssh / "known_hosts"
    known.write_text("fixture-host-key-only")
    known.chmod(0o600)
    return root


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, ...]] = []
    answers = {"host": 0, "print": 1, "bootstrap": 0}

    def mock_run(argv, **kwargs):
        args = tuple(map(str, argv))
        calls.append(args)
        assert kwargs.get("capture_output") is True
        assert kwargs.get("check") is False
        assert kwargs.get("timeout") in (8, 12)
        if argv[0].endswith("ssh-keygen"):
            return SimpleNamespace(returncode=answers["host"])
        if argv[1] == "print":
            return SimpleNamespace(returncode=answers["print"])
        if argv[1] == "bootstrap":
            return SimpleNamespace(returncode=answers["bootstrap"])
        raise AssertionError("Unexpected external command")

    monkeypatch.setattr(mac.subprocess, "run", mock_run)
    return calls, answers


def test_reverse_ssh_never_exposes_devtools_to_lan(home: Path) -> None:
    argv = mac.ssh_argv(home)
    assert argv[0] == "/usr/bin/ssh"
    assert argv[-1] == "grammy-jiang@raspberrypi.local"
    assert argv.count("-R") == 1
    assert argv[argv.index("-R") + 1] == "127.0.0.1:9233:127.0.0.1:9233"
    assert "-n" in argv and "-N" in argv and "-T" in argv
    for security in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        "ExitOnForwardFailure=yes",
        "ServerAliveInterval=20",
        "ServerAliveCountMax=2",
        "ControlMaster=no",
        "ControlPath=none",
    ):
        assert security in argv
    assert "0.0.0.0:9233" not in " ".join(argv)
    assert "PasswordAuthentication=yes" not in argv
    assert "-L" not in argv and "-D" not in argv
    assert str(home / ".ssh/android-mcp-chrome-tunnel") in argv
    assert str(home / ".ssh/known_hosts") in " ".join(argv)


def test_launchd_restarts_ssh_and_owns_nothing_else(home: Path) -> None:
    job = mac.job_data(home)
    assert set(job) == {
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "KeepAlive",
        "ThrottleInterval",
        "ProcessType",
        "StandardOutPath",
        "StandardErrorPath",
        "Umask",
    }
    assert job["Label"] == mac.LABEL
    assert job["ProgramArguments"] == mac.ssh_argv(home)
    assert job["RunAtLoad"] is True
    assert job["KeepAlive"] is True
    assert job["ThrottleInterval"] == 20
    assert job["Umask"] == 0o077
    assert job["StandardOutPath"] == "/dev/null"
    assert str(job["StandardErrorPath"]).startswith(str(home))
    assert plistlib.loads(plistlib.dumps(job)) == job
    assert mac.launch_target(501) == f"gui/501/{mac.LABEL}"


def test_installer_requires_private_owned_ssh_key(home: Path, commands) -> None:
    assert mac.trusted_files(home)
    calls, answers = commands
    assert calls[-1][0] == "/usr/bin/ssh-keygen"
    answers["host"] = 1
    assert mac.trusted_files(home) is False
    answers["host"] = 0
    key = home / ".ssh/android-mcp-chrome-tunnel"
    key.chmod(0o644)
    assert not mac.trusted_files(home)
    key.chmod(0o600)
    key.unlink()
    assert not mac.trusted_files(home)


def test_installer_rejects_untrusted_known_hosts_or_symlinks(
    home: Path,
    commands,
) -> None:
    known = home / ".ssh/known_hosts"
    known.chmod(0o666)
    assert not mac.trusted_files(home)
    known.chmod(0o600)
    known.unlink()
    assert not mac.trusted_files(home)
    known.symlink_to(home / ".ssh/android-mcp-chrome-tunnel")
    assert not mac.trusted_files(home)


def test_installer_refuses_root_or_missing_host_key(
    home: Path, commands, capsys
) -> None:
    _, answers = commands
    assert mac.install(home, 0) == 2
    answers["host"] = 1
    assert mac.install(home, 501) == 2
    assert "REFUSED" in capsys.readouterr().out
    assert not mac.paths(home)[2].exists()


def test_installer_creates_private_launchagent_and_bootstraps_user_domain(
    home: Path,
    commands,
) -> None:
    calls, _ = commands
    assert mac.install(home, 501) == 0
    _, _, plist, log = mac.paths(home)
    assert plist.is_file()
    assert log.is_file()
    assert stat.S_IMODE(plist.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert plistlib.loads(plist.read_bytes()) == mac.job_data(home)
    assert any(
        x[:3] == ("/bin/launchctl", "bootstrap", "gui/501") and x[-1] == str(plist)
        for x in calls
    )


def test_idempotent_existing_plist_loaded_does_not_bootstrap_twice(
    home: Path,
    commands,
) -> None:
    calls, answers = commands
    assert mac.install(home, 501) == 0
    answers["print"] = 0
    old = len(calls)
    assert mac.install(home, 501) == 0
    assert not any("bootstrap" in x for x in calls[old:])


def test_existing_different_launchagent_refuses_overwrite(home: Path, commands) -> None:
    assert mac.install(home, 501) == 0
    plist = mac.paths(home)[2]
    plist.write_bytes(b"manually edited service")
    assert mac.install(home, 501) == 2
    assert plist.read_bytes() == b"manually edited service"


@pytest.mark.parametrize(
    "relative_path",
    [
        "Library/LaunchAgents",
        "Library/Logs/AndroidMCP",
    ],
)
def test_symlink_launchagent_or_log_folder_is_refused(
    home: Path,
    commands,
    relative_path: str,
) -> None:
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(home / ".ssh", target_is_directory=True)
    assert mac.install(home, 501) == 2


def test_symlinked_log_is_refused(home: Path, commands) -> None:
    _, _, _, log = mac.paths(home)
    log.parent.mkdir(parents=True)
    log.symlink_to(home / ".ssh/known_hosts")
    assert mac.install(home, 501) == 2


def test_symlinked_plist_is_refused(home: Path, commands) -> None:
    _, _, plist, _ = mac.paths(home)
    plist.parent.mkdir(parents=True)
    plist.symlink_to(home / ".ssh/known_hosts")
    assert mac.install(home, 501) == 2


def test_launchctl_failure_does_not_report_success(home: Path, commands) -> None:
    _, answers = commands
    answers["bootstrap"] = 1
    assert mac.install(home, 501) == 3
    assert mac.paths(home)[2].is_file()


def test_status_requires_our_plist_and_launchd_loaded(home: Path, commands) -> None:
    _, answers = commands
    assert mac.status(home, 501) == 1
    assert mac.install(home, 501) == 0
    assert mac.status(home, 501) == 1
    answers["print"] = 0
    assert mac.status(home, 501) == 0


def test_mac_cli_main_refuses_linux_even_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mac.sys, "platform", "linux")
    assert mac.main(["install"]) == 2


def test_mac_cli_main_dispatches_status_and_install(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> None:
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.Path, "home", lambda: home)
    monkeypatch.setattr(mac.os, "getuid", lambda: 501)
    observed = []
    monkeypatch.setattr(
        mac,
        "status",
        lambda h, u: observed.append(("status", h, u)) or 4,
    )
    monkeypatch.setattr(
        mac,
        "install",
        lambda h, u: observed.append(("install", h, u)) or 5,
    )
    assert mac.main(["status"]) == 4
    assert mac.main(["install"]) == 5
    assert observed == [
        ("status", home.resolve(), 501),
        ("install", home.resolve(), 501),
    ]


@pytest.mark.parametrize("error_type", [OSError, mac.subprocess.TimeoutExpired])
def test_host_key_inspection_faults_never_enable_service(
    home: Path, commands, monkeypatch, error_type
) -> None:
    def deny(*args, **kwargs):
        if error_type is OSError:
            raise OSError("fixture ssh-keygen error")
        raise mac.subprocess.TimeoutExpired(["ssh-keygen"], 1)

    monkeypatch.setattr(mac.subprocess, "run", deny)
    assert not mac.trusted_files(home)
    assert mac.install(home, 501) == 2


def test_installer_rejects_different_ssh_key_owner(home: Path, commands, monkeypatch):
    real = mac.os.getuid()
    monkeypatch.setattr(mac.os, "getuid", lambda: real + 42)
    assert not mac.trusted_files(home)
