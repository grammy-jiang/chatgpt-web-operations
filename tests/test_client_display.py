"""T0 tests for where the scripted window goes and how it is recognized
(2026-09-28, the owner's complaint: a Chrome window in front of everything,
taking the focus while they dictate).

Three facts are defended here: the launch pins Chrome to X11 so the Xvfb
display is the only place the window can be drawn; the sender checks after
launch that the window is not on the desktop and records an event when it
is; and the processes this skill starts are recognized by their arguments,
including the shared-profile window that carried no "playwright" marker and
so escaped the in-flight check and the lifetime watchdog until today.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402
import preflight  # noqa: E402

PERSISTENT_ARGV = [
    "/opt/google/chrome/chrome",
    "--disable-background-networking",
    "--window-size=1000,800",
    "--user-data-dir=/tmp/rp-browser-profile",
    "--remote-debugging-pipe",
    "about:blank",
]
TEMP_PROFILE_ARGV = [
    "/opt/google/chrome/chrome",
    "--user-data-dir=/tmp/playwright_chromiumdev_profile-abc123",
    "about:blank",
]
USERS_OWN_ARGV = [
    "/opt/google/chrome/chrome",
    "--user-data-dir=/home/someone/.config/google-chrome",
    "https://chatgpt.com/",
]


def _events() -> list[dict]:
    path = Path(os.environ["RP_EVENTS_FILE"])
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# the launch pins Chrome to X11
# ---------------------------------------------------------------------------


def test_launch_args_pin_chrome_to_the_x11_display() -> None:
    assert "--ozone-platform=x11" in cc.BrowserSender.LAUNCH_ARGS


def test_desktop_env_keys_cover_every_way_to_reach_the_compositor() -> None:
    assert set(cc.DESKTOP_ENV_KEYS) == {
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "GDK_BACKEND",
        "XDG_SESSION_TYPE",
    }


# ---------------------------------------------------------------------------
# recognizing our own processes
# ---------------------------------------------------------------------------


def test_scripted_markers_name_the_shared_profile_when_one_is_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cc, "PROFILE_DIR", Path("/tmp/rp-browser-profile"))
    assert cc.scripted_markers() == (
        cc.PW_MARKER,
        "--user-data-dir=/tmp/rp-browser-profile",
    )
    monkeypatch.setattr(cc, "PROFILE_DIR", None)
    assert cc.scripted_markers() == (cc.PW_MARKER,)


def test_the_shared_profile_window_counts_as_scripted(monkeypatch) -> None:
    """Until 2026-09-28 it did not: no "playwright" in its arguments, so the
    default send window was invisible to preflight and to the watchdog."""
    monkeypatch.setattr(cc, "PROFILE_DIR", Path("/tmp/rp-browser-profile"))
    assert cc.is_scripted_browser(PERSISTENT_ARGV) is True
    assert cc.is_scripted_browser(TEMP_PROFILE_ARGV) is True
    assert cc.is_scripted_browser(USERS_OWN_ARGV) is False
    assert cc.is_scripted_browser([]) is False
    assert cc.is_scripted_browser(["/usr/bin/bash", "-c", "playwright chrome"]) is False


def test_is_xvfb_matches_the_server_binary_only() -> None:
    assert cc.is_xvfb(["/usr/bin/Xvfb", ":99", "-screen", "0", "1280x1024x24"])
    assert not cc.is_xvfb(["/usr/bin/xvfb-run", "Xvfb"])
    assert not cc.is_xvfb([])


def test_process_scans_use_the_argv_predicates(monkeypatch) -> None:
    monkeypatch.setattr(cc, "PROFILE_DIR", Path("/tmp/rp-browser-profile"))
    monkeypatch.setattr(
        cc,
        "_process_argvs",
        lambda: iter(
            [
                (10, PERSISTENT_ARGV),
                (11, USERS_OWN_ARGV),
                (12, ["/usr/bin/Xvfb", ":99"]),
                (13, ["/usr/bin/Xvfb", ":100"]),
                (14, TEMP_PROFILE_ARGV),
            ]
        ),
    )
    assert cc.scripted_browser_pids() == {10, 14}
    assert cc.xvfb_pids() == {12, 13}


# ---------------------------------------------------------------------------
# the check after launch
# ---------------------------------------------------------------------------


@pytest.fixture
def sender(monkeypatch):
    fp.install(monkeypatch)
    made = cc.BrowserSender()
    made.page = fp.Page()
    made._own_pids = {501, 502}
    yield made
    made._owner.shutdown(wait=True)


def test_a_window_on_its_xvfb_display_records_nothing(sender, monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":105")
    monkeypatch.setattr(cc, "compositor_clients", lambda pids: [])
    sender._desktop_check()
    assert _events() == []


def test_a_window_talking_to_the_compositor_is_recorded(sender, monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":105")
    monkeypatch.setattr(cc, "compositor_clients", lambda pids: [502])
    sender._desktop_check()
    events = _events()
    assert [e["kind"] for e in events] == ["window-on-desktop"]
    assert events[0]["wayland_pids"] == [502]
    assert events[0]["display"] == ":105"


def test_a_window_on_the_desktop_display_is_recorded(sender, monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cc, "compositor_clients", lambda pids: [])
    sender._desktop_check()
    assert [e["kind"] for e in _events()] == ["window-on-desktop"]


def test_a_visible_window_is_what_the_owner_asked_for(monkeypatch) -> None:
    fp.install(monkeypatch)
    sender = cc.BrowserSender(visible=True)
    try:
        sender._own_pids = {7}
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(cc, "compositor_clients", lambda pids: [7])
        sender._desktop_check()
        assert _events() == []
    finally:
        sender._owner.shutdown(wait=True)


SS_LINES = """\
u_str ESTAB 0 0 @abc/bus/labwc/system 12667 * 0 users:(("labwc",pid=1186,fd=12))
u_str ESTAB 0 0 /run/user/1000/wayland-0 12959 * 0 users:(("labwc",pid=1186,fd=62))
u_str ESTAB 0 0 /run/user/1000/wayland-0 30001 * 30002 users:(("labwc",pid=1186,fd=70))
u_str ESTAB 0 0 /run/user/1000/wayland-0 30003 * 30004 users:(("labwc",pid=1186,fd=71))
u_str ESTAB 0 0 /tmp/.X11-unix/X99 40001 * 40002 users:(("Xvfb",pid=1332491,fd=8))
u_str ESTAB 0 0 * 50001 * 50002 users:(("chrome",pid=1332511,fd=30))
"""


def test_wayland_peer_inodes_reads_the_compositor_s_accepted_connections() -> None:
    """Only the compositor's own side of a connection names the socket
    path; its peer inode is the client's socket. Listening sockets (peer 0),
    the X server's connections and unnamed sockets do not count."""
    assert cc.wayland_peer_inodes(SS_LINES) == {30002, 30004}
    assert cc.wayland_peer_inodes("") == set()
    assert cc.wayland_peer_inodes("garbage * notanumber /x/wayland-0") == set()


def test_socket_inodes_reads_the_fd_links_of_a_process(tmp_path) -> None:
    fd = tmp_path / "77" / "fd"
    fd.mkdir(parents=True)
    (fd / "3").symlink_to("socket:[30002]")
    (fd / "4").symlink_to("pipe:[123]")
    (fd / "5").symlink_to("/dev/null")
    (fd / "6").symlink_to("socket:[555]")
    assert cc.socket_inodes(77, proc=tmp_path) == {30002, 555}
    assert cc.socket_inodes(78, proc=tmp_path) == set()


def test_compositor_clients_matches_peer_inodes_to_our_processes(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=SS_LINES, stderr="")

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cc, "socket_inodes", lambda pid, proc=None: {30002} if pid == 9 else {1}
    )
    assert cc.compositor_clients([8, 9, 10]) == [9]


def test_compositor_clients_is_empty_without_ss(monkeypatch) -> None:
    """No ``ss``: nothing is recorded rather than a guess."""

    def boom(*a, **k):
        raise FileNotFoundError("ss")

    monkeypatch.setattr(cc.subprocess, "run", boom)
    assert cc.compositor_clients([1, 2]) == []


# ---------------------------------------------------------------------------
# preflight: leaked virtual displays
# ---------------------------------------------------------------------------


def test_virtual_displays_check_is_ok_up_to_one_more_than_the_browser_cap():
    assert preflight.virtual_displays_check(set(), 1)["state"] == "ok"
    assert preflight.virtual_displays_check({1, 2}, 1)["state"] == "ok"


def test_virtual_displays_check_warns_and_names_the_fix_when_displays_leaked():
    c = preflight.virtual_displays_check(set(range(100, 130)), 1)
    assert c["state"] == "warn"
    assert "30 Xvfb" in c["detail"]
    assert "leaked" in c["detail"]
    assert c["fix"] == "when no send is in flight: pkill -x Xvfb"
