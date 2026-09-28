"""Tier L (loopback): the skill's real HTTP path against a fake chatgpt.com on
127.0.0.1 (TESTING.md section 6, P6).

What runs for real here and nowhere else offline: ``Session._request``
through urllib and a socket, ``Session.call``'s retries, the client's
authentication ladder (production 30/90/180 s and the health policy's
5/10 s) and rate-limit ladder, the cookie header built from a Chrome jar
(a synthetic one), the renewal read from ``Set-Cookie``, every read
command's ``main()`` with its exit code, and one command started as its own
process through the system Python (the venv switch, argv, the exit code).

Waits are recorded, never slept: the ladders take the ``sleep`` their
callers pass in, and ``Session.call``'s 2/4 s pauses go through the
``time.sleep`` this file replaces. The server's stalls end on an event, so
replacing ``time.sleep`` never shortens them.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import api_contract
import chatgpt_client as cc
import chatgpt_session
import health
import pytest

from loopback.server import (
    ACCESS_TOKEN,
    AUTH,
    RENEW_MAX_AGE,
    challenge,
    cut,
    html,
    reply,
    stall,
    write_jar,
)

pytestmark = pytest.mark.loopback

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
PINS = "/backend-api/pins"
CHAT = api_contract.CHAT


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """``Session.call``'s own pauses between attempts, recorded."""
    pauses: list[float] = []
    monkeypatch.setattr(chatgpt_session.time, "sleep", pauses.append)
    return pauses


# ---------------------------------------------------------------------------
# the authentication ladders: the exit criterion of P6
# ---------------------------------------------------------------------------

POLICIES: dict[str, dict[str, Any]] = {
    "production": {},
    "health": {
        "auth_backoff": health.HEALTH_AUTH_BACKOFF,
        "request_timeout": health.HEALTH_REQUEST_TIMEOUT,
        "request_retries": health.HEALTH_REQUEST_RETRIES,
    },
}


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_the_2026_09_23_timeout_two_challenges_then_a_login(
    wired, policy: str, capsys
) -> None:
    """Two Cloudflare 403s on the handshake, then 200: the shape of the
    health timeout of 2026-09-23 (references/failure-atlas.md). Under the
    production ladder the two waits alone are the old wrapper's 120 s; the
    health policy's are 15 s. Offline, both take well under 10 s."""
    wired.script(AUTH, challenge(), challenge())
    waits: list[float] = []
    started = time.monotonic()

    session = cc.ChatGPTSession("chrome", sleep=waits.append, **POLICIES[policy])

    assert time.monotonic() - started < 10
    assert len(wired.seen(AUTH)) == 3
    assert session.session.token == ACCESS_TOKEN
    if policy == "production":
        assert waits == list(cc.ChatGPTSession.AUTH_BACKOFF[:2]) == [30.0, 90.0]
        assert sum(waits) >= 120  # 2026-09-23: killed before the third attempt
    else:
        assert waits == list(health.HEALTH_AUTH_BACKOFF) == [5.0, 10.0]
        assert sum(waits) < 180  # the wrapper's ceiling since then
    err = capsys.readouterr().err
    assert "Cloudflare challenge" in err and "expired" not in err


def test_a_stalled_handshake_is_one_failed_attempt_not_a_traceback(wired) -> None:
    """Before 2026-09-29 a handshake that timed out while its answer was read
    left ``Session.__init__`` as a bare TimeoutError, which the ladder (it
    catches the SystemExit of a failed login) never saw."""
    wired.script(AUTH, stall(5))
    waits: list[float] = []

    session = cc.ChatGPTSession(
        "chrome", sleep=waits.append, auth_backoff=(1.0,), request_timeout=0.5
    )

    assert waits == [1.0]
    assert len(wired.seen(AUTH)) == 2
    assert session.session.token == ACCESS_TOKEN


def test_a_handshake_that_never_answers_ends_the_ladder_with_a_transport_error(
    wired,
) -> None:
    wired.script(AUTH, stall(5), stall(5))
    with pytest.raises(cc.TransportError, match="could not authenticate after 1"):
        cc.ChatGPTSession(
            "chrome", sleep=lambda s: None, auth_backoff=(0.0,), request_timeout=0.3
        )


# ---------------------------------------------------------------------------
# reads that fail on the wire
# ---------------------------------------------------------------------------


def test_a_stalled_read_is_status_0_and_the_health_check_still_reports(
    wired, no_pause
) -> None:
    """F-2026-09-29-11 over a real socket: the pins read stalls past the
    health policy's timeout on both of its attempts."""
    session = cc.ChatGPTSession("chrome", request_timeout=0.5, request_retries=2)
    wired.script(PINS, stall(5), stall(5))

    row = health.fetch_read_endpoints(session)

    assert row["state"] in ("warn", "block"), row
    assert "pins" in row["detail"], row
    assert len(wired.seen(PINS)) == 2
    assert no_pause == [2]


def test_a_body_cut_short_is_read_again(wired, no_pause) -> None:
    session = cc.ChatGPTSession("chrome")
    wired.script(PINS, cut())

    status, body = session.session.call(PINS)

    assert status == 200 and isinstance(body, list)  # pins is a list
    assert len(wired.seen(PINS)) == 2


def test_a_page_instead_of_json_is_read_again_but_a_write_is_sent_once(
    wired, no_pause
) -> None:
    session = cc.ChatGPTSession("chrome")
    wired.script(PINS, html())
    status, body = session.session.call(PINS)
    assert status == 200 and isinstance(body, list)  # pins is a list
    assert len(wired.seen(PINS)) == 2

    path = f"/backend-api/conversation/{CHAT}"
    wired.script(path, html())
    status, body = session.session.call(
        path, method="PATCH", payload={"is_visible": False}
    )
    assert status == 200
    assert body["error"].startswith("the answer was not JSON")
    assert [r.method for r in wired.seen(path)] == ["PATCH"]


def test_429_takes_the_session_s_retries_then_the_client_s_ladder(
    wired, no_pause
) -> None:
    session = cc.ChatGPTSession("chrome")
    slow = reply(429, {"detail": "Too many requests"})
    wired.script(PINS, slow, slow, slow)
    waits: list[float] = []

    data = session._call(PINS, sleep=waits.append)

    assert isinstance(data, list)  # pins is a list
    assert no_pause == [2, 4]  # Session.call: three attempts
    assert waits == [cc.ChatGPTSession.RATE_LIMIT_BACKOFF[0]]  # then the client
    assert len(wired.seen(PINS)) == 4


def test_424_is_retried_by_the_refresh_command_not_by_the_session(
    wired, monkeypatch
) -> None:
    import refresh_connector as rc

    waits: list[float] = []
    monkeypatch.setattr(rc, "SLEEP", waits.append)
    link = "link_" + "a" * 32
    wired.script(
        rc.REFRESH,
        reply(424, {"detail": "tunnel not reachable"}),
        reply(424, {}),
        reply(200, {"actions": [{"name": "read_file"}]}),
    )

    assert rc.main(["--link-id", link]) == 0

    assert waits == [rc.RETRY_424_DELAY_S] * 2
    sent = wired.seen(rc.REFRESH)
    assert len(sent) == 3
    assert json.loads(sent[0].body) == {"link_id": link}


# ---------------------------------------------------------------------------
# what goes on the wire
# ---------------------------------------------------------------------------


def test_the_cookie_header_is_the_jar_and_the_bearer_is_the_handshake_s(
    wired,
) -> None:
    session = cc.ChatGPTSession("chrome")
    session.session.call(PINS)

    login = wired.seen(AUTH)[0].headers
    pairs = dict(part.split("=", 1) for part in login["cookie"].split("; "))
    assert pairs["__Secure-next-auth.session-token"] == "loopback-session"
    assert pairs["cf_clearance"] == "loopback-clearance"  # v10, decrypted
    assert pairs["_dd_s"] == ""  # an empty analytics cookie is kept, not fatal
    assert "elsewhere" not in pairs  # another site's cookie is never sent
    assert "authorization" not in login
    read = wired.seen(PINS)[0].headers
    assert read["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_the_handshake_s_set_cookie_renews_the_horizon(wired) -> None:
    session = cc.ChatGPTSession("chrome")
    renewed = session.session.renewed_expires
    assert renewed is not None
    assert abs(renewed - (time.time() + RENEW_MAX_AGE)) < 120


# ---------------------------------------------------------------------------
# every read command, then one as its own process
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", api_contract.SCENARIOS, ids=lambda s: s.id)
def test_every_read_command_runs_over_real_http(
    wired, scenario: api_contract.Scenario, capsys
) -> None:
    """The contract scenarios of P5, with nothing faked between the command
    and the socket: each builds its own session through ``open_session``."""
    wired.prefer = dict(scenario.prefer)
    module = importlib.import_module(scenario.module)
    argv = [
        api_contract.PLACEHOLDERS[a](wired.shapes)
        if a in api_contract.PLACEHOLDERS
        else a
        for a in scenario.argv
    ]
    try:
        code = module.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    output = capsys.readouterr()
    assert code in scenario.exits, (output.out + output.err)[-2000:]
    assert wired.unrecorded == []
    assert wired.seen(AUTH), "the command did not build its own session"


def test_list_chats_runs_as_its_own_process(server, tmp_path: Path) -> None:
    """``/usr/bin/python3 scripts/list_chats.py`` as the owner types it: the
    venv switch, argv, the exit code. The process gets a HOME with only the
    synthetic jar, the loopback address, no stored token and no bus."""
    home = tmp_path / "home"
    write_jar(home / ".config" / "google-chrome" / "Default" / "Cookies")
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        chatgpt_session.BASE_ENV: server.url,
        chatgpt_session.SESSION_STORE_ENV: "0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus",
    }
    result = subprocess.run(
        ["/usr/bin/python3", str(SCRIPTS / "list_chats.py"), "--limit", "3"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    paths = [r.path.split("?", 1)[0] for r in server.requests]
    assert paths[:2] == [AUTH, "/backend-api/conversations"]
    assert "loopback-session" in server.seen(AUTH)[0].headers["cookie"]
