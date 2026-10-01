#!/usr/bin/env python3
"""The dedicated browser window a send attaches to.

    dedicated_browser.py start  [--port N] [--profile DIR]
    dedicated_browser.py run    [--port N] [--profile DIR]   # foreground (systemd)
    dedicated_browser.py status [--port N] [--json PATH]
    dedicated_browser.py stop   [--port N] [--profile DIR]
    dedicated_browser.py unit   [--install]

Since 2026-10-01 a send attaches over CDP to a Chrome the owner launched
normally -- its own profile directory, a debugging port on 127.0.0.1, logged
in to chatgpt.com once by hand -- and drives one tab in it, its own kept
tab by default (``RP_BROWSER_TAB``; SKILL.md,
"Sending through the dedicated browser window"; the client's ``cdp_endpoint``
and ``BrowserSender._open_attached``). The scripted Xvfb window stays as
the fallback when nothing answers at the port. Only what needs a browser
goes this way; every read and every other write stays HTTP.

``start`` launches that Chrome detached when nothing answers at the port
and waits until it does; ``run`` keeps it in the foreground so a systemd
user unit can supervise it (and, when another instance already holds the
port, waits for that one to go away before launching, so the unit never
fights a hand-started window); ``status`` prints the version and the
chatgpt.com tabs; ``stop`` ends the instance that holds this profile;
``unit`` prints the user unit, ``--install`` writes and enables it. Chrome
136 and later ignore the debugging switches on the default profile, which
is why this is a separate profile and never the owner's own Chrome.

Exit codes: 0 done, 1 the browser did not start, answer or stop, 2 bad
arguments. Nothing here reads or prints a cookie; the login lives in the
profile directory alone.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from _common import ensure_venv

ensure_venv()

import chatgpt_client as cc  # noqa: E402

START_PAGE = "https://chatgpt.com/"
LOG_FILE = cc.STATE_DIR / "dedicated-browser.log"
UNIT_NAME = "chatgpt-dedicated-browser.service"
UNIT_DIR = Path("~/.config/systemd/user").expanduser()
START_WAIT_SECONDS = 30.0
STOP_WAIT_SECONDS = 15.0
RUN_POLL_SECONDS = 30.0


def chrome_binary() -> str:
    """The Google Chrome binary: the same one a scripted window uses."""
    return (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or "google-chrome"
    )


def chrome_args(port: int, profile: Path) -> list[str]:
    """The launch line: a non-default profile (Chrome 136+ ignores the
    debugging switches on the default one), the local debugging port, no
    first-run prompts, no extensions, one window on chatgpt.com.

    ``--disable-extensions`` since 2026-10-01 evening: a Google login in the
    window signed Chrome in and synced the owner's extensions into the
    profile, and a duplicate-tab blocker among them bounced the diagnostics'
    tabs (F-2026-10-01-32). This window runs none of them; they stay
    installed until the owner removes them, and ``status`` says both."""
    return [
        chrome_binary(),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--window-size=1100,900",
        "--new-window",
        START_PAGE,
    ]


def desktop_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a desktop window needs, defaulted the way a cron job
    or a user unit lacks them: the Wayland socket, the runtime directory and
    the session bus. Never overwrites a variable that is set."""
    env = dict(os.environ if environ is None else environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_SESSION_TYPE", "wayland")
    env.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
    )
    return env


def endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def profile_extensions(profile: Path) -> list[str]:
    """Names of the Web Store extensions installed in ``profile`` (its
    ``Default/Preferences``), or []. The dedicated profile should have none:
    on 2026-10-01 a Google login in its window signed Chrome in as well and
    synced the owner's 33 extensions into it, and a duplicate-tab blocker
    among them bounced every new tab opened on an address the owner's own
    tab held (F-2026-10-01-32). Read-only, local, never a value of the
    owner's."""
    prefs = profile / "Default" / "Preferences"
    try:
        settings = json.loads(prefs.read_text(encoding="utf-8"))
        entries = settings.get("extensions", {}).get("settings", {})
    except (OSError, ValueError, AttributeError):
        return []
    names = []
    for value in entries.values():
        if not isinstance(value, dict) or not value.get("from_webstore"):
            continue
        name = (value.get("manifest") or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(names)


def version(port: int) -> dict[str, Any] | None:
    """``/json/version`` of the browser at ``port``, or None."""
    return cc.dedicated_browser_version(endpoint(port))


def tabs(port: int, timeout: float = 3.0) -> list[dict[str, Any]]:
    """The pages the browser at ``port`` has open (``/json/list``), or []."""
    try:
        with urllib.request.urlopen(
            f"{endpoint(port)}/json/list", timeout=timeout
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    return [t for t in data if isinstance(t, dict) and t.get("type") == "page"]


def wait_for(port: int, *, up: bool, seconds: float, sleep: Any = time.sleep) -> bool:
    """Wait until the browser at ``port`` answers (``up``) or stops
    answering (not ``up``), at most ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        if bool(version(port)) == up:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(1.0)


def browser_processes(profile: Path) -> list[tuple[int, list[str]]]:
    """``(pid, argv)`` of the Chrome browser process(es) started on
    ``profile``: the ones whose arguments name its directory and the
    debugging port, never the owner's own Chrome (a different directory, no
    such switch) and never a renderer or other helper (``--type=``)."""
    marker = f"--user-data-dir={profile}"
    found: list[tuple[int, list[str]]] = []
    for pid, argv in cc._process_argvs():
        tokens = argv_tokens(argv)
        ours = marker in tokens and any(
            a.startswith("--remote-debugging-port=") for a in tokens
        )
        helper = any(a.startswith("--type=") for a in tokens)
        if ours and not helper:
            found.append((pid, tokens))
    return found


def argv_tokens(argv: list[str]) -> list[str]:
    """``argv`` as switches. A running Chrome rewrites its own
    ``/proc/<pid>/cmdline`` into one space-joined string (measured
    2026-10-01 16:30: ``['/opt/google/chrome/chrome --user-data-dir=...
    --remote-debugging-port=9222 ...', '']``), which ``stop`` and ``status``
    then failed to recognise as ours; an element that carries switches
    inside is split on whitespace, a plain element is kept whole."""
    return [t for a in argv for t in (a.split() if " --" in a else [a])]


def profile_pids(profile: Path) -> list[int]:
    """PIDs of the Chrome browser process(es) started on ``profile``."""
    return [pid for pid, _argv in browser_processes(profile)]


def extensions_disabled(profile: Path) -> bool | None:
    """Whether the browser running on ``profile`` was started with
    ``--disable-extensions``; None when none of ours runs. A window started
    before 2026-10-01 evening runs the profile's extensions until it is
    restarted, which is what ``status`` tells apart."""
    processes = browser_processes(profile)
    if not processes:
        return None
    return all("--disable-extensions" in argv for _pid, argv in processes)


def launch(port: int, profile: Path, *, detached: bool) -> subprocess.Popen[bytes]:
    """Start Chrome on ``profile`` with its output appended to LOG_FILE."""
    profile.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab")  # noqa: SIM115  # Chrome inherits the descriptor
    return subprocess.Popen(
        chrome_args(port, profile),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=detached,
        env=desktop_env(),
    )


def cmd_start(args: argparse.Namespace) -> int:
    info = version(args.port)
    if info:
        print(f"already running: {info.get('Browser', '?')} at {endpoint(args.port)}")
        return 0
    launch(args.port, args.profile, detached=True)
    if not wait_for(args.port, up=True, seconds=START_WAIT_SECONDS):
        print(
            f"nothing answers at {endpoint(args.port)} after "
            f"{START_WAIT_SECONDS:.0f} s; see {LOG_FILE}",
            file=sys.stderr,
        )
        return 1
    info = version(args.port) or {}
    print(f"started: {info.get('Browser', '?')} at {endpoint(args.port)}")
    print(f"profile: {args.profile}")
    print("if this profile is new, log in to chatgpt.com in its window once")
    return 0


def cmd_run(args: argparse.Namespace, sleep: Any = time.sleep) -> int:
    """Foreground, for the systemd unit: supervise one Chrome on the
    profile. An instance that already holds the port (a hand-started one)
    is left alone until it goes away; then this starts its own and returns
    its exit status, so the unit's Restart policy brings the next one."""
    while version(args.port):
        sleep(RUN_POLL_SECONDS)
    proc = launch(args.port, args.profile, detached=False)
    return proc.wait()


def cmd_status(args: argparse.Namespace) -> int:
    info = version(args.port)
    pages = tabs(args.port) if info else []
    chatgpt_tabs = [t for t in pages if "chatgpt.com" in str(t.get("url", ""))]
    extensions = profile_extensions(args.profile)
    disabled = extensions_disabled(args.profile) if info else None
    tab = tab_in_use(pages)
    record = {
        "endpoint": endpoint(args.port),
        "running": bool(info),
        "browser": (info or {}).get("Browser"),
        "tabs": len(pages),
        "chatgpt_tabs": len(chatgpt_tabs),
        "mode": cc.CDP_MODE,
        "extensions": extensions,
        "extensions_disabled": disabled,
        "tab_mode": cc.TAB_MODE,
        "tab": tab,
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
    if not info:
        print(f"not running: nothing answers at {record['endpoint']}")
        return 1
    print(
        f"running: {record['browser']} at {record['endpoint']}; "
        f"{record['tabs']} tab(s), {record['chatgpt_tabs']} on chatgpt.com; "
        f"RP_BROWSER_CDP={record['mode']}"
    )
    print(f"RP_BROWSER_TAB={record['tab_mode']}: {tab['detail']}")
    if extensions:
        names = f"{', '.join(extensions[:4])}{', ...' if len(extensions) > 4 else ''}"
        runs = (
            "none of them runs in this window (--disable-extensions)"
            if disabled
            else "this window runs them: restart it (dedicated_browser.py stop; "
            "the unit starts it again with --disable-extensions)"
        )
        print(
            f"{len(extensions)} Web Store extension(s) in the profile ({names}); "
            f"{runs}. To remove them for good, sign it out of Chrome (not out of "
            "chatgpt.com) first, or sync brings them back and removing a synced "
            "extension removes it from the owner's own Chrome too (F-2026-10-01-32)"
        )
    return 0


def tab_in_use(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Which tab of the window a send would drive, from the open targets
    and ``RP_BROWSER_TAB`` (``chatgpt_client.tab_mode_parts``): for "url"
    the first page whose address starts with the prefix, for "own" the
    remembered tab when it is still open; ``open`` False with the reason
    when the next send would make a tab of its own."""
    kind, prefix = cc.tab_mode_parts()
    open_pages = [p for p in pages if p.get("type") == "page"]
    if kind == "new":
        return {
            "kind": kind,
            "open": False,
            "url": "",
            "detail": "one tab per send, closed at its end",
        }
    if kind == "url":
        for p in open_pages:
            if str(p.get("url", "")).startswith(prefix):
                return {
                    "kind": kind,
                    "open": True,
                    "url": str(p["url"]),
                    "detail": f"drives the open tab at {p['url']}",
                }
        return {
            "kind": kind,
            "open": False,
            "url": "",
            "detail": (
                f"no open tab starts with {prefix}; the next send uses the "
                "skill's own tab"
            ),
        }
    record = cc.tab_record()
    wanted = str(record.get("target_id") or "")
    for p in open_pages:
        if wanted and p.get("id") == wanted:
            return {
                "kind": kind,
                "open": True,
                "url": str(p.get("url", "")),
                "detail": f"the skill's own tab is open at {p.get('url', '')}",
            }
    if wanted:
        return {
            "kind": kind,
            "open": False,
            "url": "",
            "detail": "the skill's own tab is gone; the next send makes one",
        }
    return {
        "kind": kind,
        "open": False,
        "url": "",
        "detail": "no tab of its own yet; the first send makes one and keeps it",
    }


def cmd_stop(args: argparse.Namespace, kill: Any = os.kill) -> int:
    """SIGTERM the profile's browser and wait for the port to close; a
    Chrome that is still answering after STOP_WAIT_SECONDS gets SIGKILL
    (measured 2026-10-01: one SIGTERM left the window up past 15 s, a second
    ended it; the login lives on disk, so a kill loses nothing)."""
    pids = profile_pids(args.profile)
    if not pids and not version(args.port):
        print("not running")
        return 0
    for sig, label in ((signal.SIGTERM, "stopped"), (signal.SIGKILL, "killed")):
        for pid in pids:
            with contextlib.suppress(OSError):
                kill(pid, sig)
        if wait_for(args.port, up=False, seconds=STOP_WAIT_SECONDS):
            print(f"{label} {len(pids)} process(es) on {args.profile}")
            return 0
    print(
        f"still answering at {endpoint(args.port)} after SIGTERM and SIGKILL",
        file=sys.stderr,
    )
    return 1


def unit_text(port: int, profile: Path) -> str:
    script = Path(__file__).resolve()
    return (
        "[Unit]\n"
        "Description=ChatGPT dedicated browser window (chatgpt-web-operations; "
        f"CDP on 127.0.0.1:{port})\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart=/usr/bin/python3 {script} run --port {port} --profile {profile}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "KillMode=control-group\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def cmd_unit(args: argparse.Namespace, run: Any = subprocess.run) -> int:
    text = unit_text(args.port, args.profile)
    if not args.install:
        print(text, end="")
        return 0
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    path = UNIT_DIR / UNIT_NAME
    path.write_text(text, encoding="utf-8")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", UNIT_NAME],
    ):
        result = run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(
                f"{' '.join(command)} failed: {result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return 1
    print(f"installed and enabled {path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("start", "run", "status", "stop", "unit"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=cc.DEDICATED_PORT)
        p.add_argument("--profile", type=Path, default=cc.DEDICATED_PROFILE_DIR)
        if name == "status":
            p.add_argument("--json", help="write the status record to PATH")
        if name == "unit":
            p.add_argument(
                "--install", action="store_true", help="write and enable the user unit"
            )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handlers = {
        "start": cmd_start,
        "run": cmd_run,
        "status": cmd_status,
        "stop": cmd_stop,
        "unit": cmd_unit,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
