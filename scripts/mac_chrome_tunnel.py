#!/usr/bin/env python3
"""Install a restart-on-failure local SSH reverse tunnel for Mac Chrome.

Uses user-scoped launchd, never root, broad signals, SSH password prompts,
public CDP listeners, or access to Chrome credentials. The remote Pi already
restricts this key to listening on the single 127.0.0.1:9233 port.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

LABEL = "com.grammy.android-mcp.chrome-reverse-tunnel"
PORT = 9233
REMOTE = "grammy-jiang@raspberrypi.local"
LOOPBACK = "127.0.0.1"


def paths(home: Path) -> tuple[Path, Path, Path, Path]:
    return (
        home / ".ssh/android-mcp-chrome-tunnel",
        home / ".ssh/known_hosts",
        home / "Library/LaunchAgents" / f"{LABEL}.plist",
        home / "Library/Logs/AndroidMCP/chrome-reverse-tunnel.err.log",
    )


def ssh_argv(home: Path) -> list[str]:
    key, known, _, _ = paths(home)
    return [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-n",
        "-N",
        "-T",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-R",
        f"{LOOPBACK}:{PORT}:{LOOPBACK}:{PORT}",
        REMOTE,
    ]


def job_data(home: Path) -> dict[str, object]:
    _, _, _, log = paths(home)
    return {
        "Label": LABEL,
        "ProgramArguments": ssh_argv(home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 20,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(log),
        "Umask": 0o077,
    }


def trusted_files(home: Path) -> bool:
    key, known, _, _ = paths(home)
    if any(p.is_symlink() or not p.is_file() for p in (key, known)):
        return False
    try:
        key_stat = key.stat()
        known_stat = known.stat()
    except OSError:
        return False
    if (
        key_stat.st_uid != os.getuid()
        or known_stat.st_uid != os.getuid()
        or stat.S_IMODE(key_stat.st_mode) & 0o077
        or stat.S_IMODE(known_stat.st_mode) & 0o022
    ):
        return False
    try:
        host = subprocess.run(
            ["/usr/bin/ssh-keygen", "-F", "raspberrypi.local", "-f", str(known)],
            capture_output=True,
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return host.returncode == 0


def launch_target(uid: int) -> str:
    return f"gui/{uid}/{LABEL}"


def launch_loaded(uid: int) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", launch_target(uid)],
        capture_output=True,
        check=False,
        timeout=8,
    )
    return result.returncode == 0


def install(home: Path, uid: int) -> int:
    if uid == 0 or not trusted_files(home):
        print("REFUSED: dedicated SSH key or trusted host verification failed")
        return 2
    _, _, plist, log = paths(home)
    for parent in (plist.parent, log.parent):
        if parent.is_symlink():
            print("REFUSED: LaunchAgent or log directory is a symlink")
            return 2
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log.parent.chmod(0o700)
    if log.is_symlink():
        print("REFUSED: tunnel log path is a symlink")
        return 2
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)

    xml = plistlib.dumps(job_data(home), fmt=plistlib.FMT_XML)
    if plist.is_symlink():
        print("REFUSED: LaunchAgent plist is a symlink")
        return 2
    if plist.exists():
        if plist.read_bytes() != xml:
            print("REFUSED: installed LaunchAgent differs; inspect it manually")
            return 2
    else:
        # Never overwrite an existing service or other user's plist.
        fd = os.open(plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(xml)

    if launch_loaded(uid):
        print("LAUNCHD_TUNNEL_ALREADY_LOADED")
        return 0
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        capture_output=True,
        check=False,
        timeout=12,
    )
    if result.returncode != 0:
        print("LAUNCHD_BOOTSTRAP_FAILED; check macOS GUI session and logs")
        return 3
    print("LAUNCHD_TUNNEL_INSTALLED; verify reverse CDP after old SSH exits")
    return 0


def status(home: Path, uid: int) -> int:
    _, _, plist, _ = paths(home)
    installed = plist.is_file() and not plist.is_symlink()
    active = launch_loaded(uid)
    print(f"PLIST_INSTALLED={installed}")
    print(f"LAUNCHD_LOADED={active}")
    return 0 if installed and active else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status"))
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("macOS LaunchAgent only; refusing changes on other platforms")
        return 2
    home = Path.home().resolve()
    if args.action == "status":
        return status(home, os.getuid())
    return install(home, os.getuid())


if __name__ == "__main__":
    raise SystemExit(main())
