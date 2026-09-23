#!/usr/bin/env python3
"""A daily health check for this skill's own HTTP path. Read-only.

    health.py [--browser] [--browser-name chrome] [--json PATH]
              [--skills-json PATH] [--diagnostics PATH]

This is the HTTP half of a two-part daily watcher: a cron wrapper (kept
outside this skill) calls it once a day, keeps the run history and decides
when to mail; this script never remembers a prior run and never sends mail
itself -- every fact below is only ever "as of right now".

Runs preflight's own four default groups exactly as ``preflight.py`` does,
by calling its functions rather than re-implementing them -- host, link,
account, run, in that order, over one session, with preflight's own rule
that a dead link skips account, run and (here) health rather than let a
doomed HTTP call read like a blocked account (SKILL.md, "Name the path that
failed"). Then a fifth group, "health", over the same session:

    session token   the LATER of two session-token horizons: the cookie
                     jar's own expiry, read without decrypting anything
                     (probe_cookies.read_jar / session_horizon), and this
                     machine's own keyring, which chatgpt_session.py's
                     Session.__init__ renews on every session it builds --
                     including the one this very check just opened
                     (chatgpt_session.py, "Session token renewal"). Measured
                     2026-09-20: it is the one load-bearing cookie in the
                     jar and it is issued for 90 days, so it is the one
                     thing that fails slowly enough for a once-a-day check
                     to catch before it fails outright. Before the renewal
                     existed, nothing refreshed it but the user's own
                     Chrome visiting chatgpt.com; now this check, run daily,
                     keeps it alive by itself, and Chrome is only needed
                     again after an actual logout or password change.
    sandbox         tests/live/sandbox.json's project still answers to its
                     own name -- a wrong sandbox would let a live test
                     write to a real project -- and carries no conversation
                     a live test forgot to clean up.
    read endpoints  two reads preflight.py does not make: the projects
                     sidebar and pinned items.
    skills inventory the uploaded-skills inventory, read over this same
                     authenticated session; the expected research-pipeline
                     skill must still be installed and enabled.

``--browser`` adds preflight's own composer check, last and opt-in, exactly
as ``preflight.py --browser`` does: it costs a browser slot and about
twenty seconds.

This never sends, creates, edits or deletes anything -- every call it makes
is a GET. ``--json PATH`` writes the same ``{"checked_at", "verdict",
"exit_code", "checks"}`` document ``preflight.py --json`` does, plus a
"facts" block: the cron wrapper's contract, the same keys every run, null
or empty rather than missing when a read failed, and never a cookie value.

``--diagnostics PATH`` additionally writes JSONL events while the check is
running: stage boundaries, authentication attempts/backoff, endpoint/status,
request duration and a small allow-list of response headers. It deliberately
never records cookie values, bearer tokens, request/response bodies or other
credentials, and redacts sensitive-looking keys as a second line of defence.
The health probe uses a short 5/10 s authentication backoff and 15 s request
timeout; ordinary ChatGPTSession callers keep their production 30/90/180 s
retry ladder. A monitor must report a transient failure promptly rather than
spend its entire outer timeout inside the production recovery policy.
``--skills-json PATH`` stores the redacted skills inventory fetched over that
same session, deliberately avoiding a second authentication handshake.

``preflight.py`` stays the check to run immediately before starting a
research run; this is the check that runs once a day whether or not a run
is planned, and it is not a substitute for it.

Exit 0 (GO) when nothing is warn or block, 2 (GO WITH WARNINGS) when only
warnings, 1 (DO NOT START) when anything blocks -- preflight's own
three-way split, from the same ``verdict_of``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chatgpt_client as cc
import clean_chats
import list_projects as lp
import list_skills as lsk
import model_settings as ms
import preflight
import probe_cookies
import probe_send_gates as psg
from _common import ensure_venv

SANDBOX_FILE = Path(__file__).resolve().parents[1] / "tests" / "live" / "sandbox.json"

# Documented in references/endpoint-discovery.md ("Additions seen on
# 2026-09-19") and reused as-is; not a new transport surface.
PINS = "/backend-api/pins"

SESSION_WARN_DAYS = 14
EXPECTED_SKILL = "research-pipeline"

# health.py is a diagnostic probe, not a production research run.  Production
# ChatGPTSession callers keep the long 30/90/180 s authentication ladder; a
# once-a-day watcher must instead fail fast enough to report what happened.
# Three authentication attempts can therefore consume at most about 60 s
# (3 * 15 s request timeout + 5 + 10 s backoff), leaving room inside the cron
# wrapper's overall budget for the remaining GET checks and evidence flush.
HEALTH_AUTH_BACKOFF = (5.0, 10.0)
HEALTH_REQUEST_TIMEOUT = 15.0
HEALTH_REQUEST_RETRIES = 2

SESSION_TOKEN_FIX = (
    "open chatgpt.com in this machine's own Chrome, or run any command that "
    "builds a session (this check included) -- either renews the token; "
    "reaching this warning despite that likely means the login itself needs "
    "refreshing (logout or a password change)"
)

SANDBOX_WRONG_FIX = (
    "a wrong sandbox would let live tests write to a real project; check "
    "tests/live/sandbox.json"
)
SANDBOX_DIRTY_FIX = (
    "a live test left chats behind; run the write tier once, or "
    "clean_chats.py --project {id} --apply"
)

# preflight's own group order with "health" placed between "run" and
# "browser"; passed to preflight.render, which takes the order as an argument
HEALTH_GROUP_ORDER = ("host", "link", "account", "run", "health", "browser")


def _diagnostic_sink(path_text: str):
    """Return a durable JSONL event sink, or ``None`` when not requested.

    Only metadata supplied explicitly by this module and the transport client
    is written.  Cookie values, bearer tokens and response bodies are never
    events, so this file can be retained with ordinary operational logs.
    """
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    sensitive = ("token", "cookie", "authorization", "body", "payload", "secret")

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "<redacted>"
                    if any(word in str(key).lower() for word in sensitive)
                    else scrub(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    def emit(event: dict[str, Any]) -> None:
        record = {
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 3),
            **scrub(event),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    return emit


def _diag(emit: Any, event: str, **fields: Any) -> None:
    if emit is not None:
        emit({"event": event, **fields})


def _state_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    states = ("ok", "warn", "block")
    return {state: sum(c.get("state") == state for c in checks) for state in states}


def _sandbox() -> dict[str, str]:
    """``tests/live/sandbox.json``: the one project the sandbox check, and
    the facts it contributes, are ever allowed to name."""
    return json.loads(SANDBOX_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# GROUP health, check 1: the session token's own horizon
# ---------------------------------------------------------------------------


def _read_session_horizon(
    cc_mod: Any, browser: str
) -> tuple[tuple[str, float] | None, tuple[str, float] | None]:
    """Both session-token horizons, as ``(chrome, keyring)``: Chrome's own
    jar, read without decrypting anything (``probe_cookies.read_jar`` /
    ``session_horizon``, exactly ``probe_cookies.py``'s own reasoning --
    expiry is not a secret), and this machine's own keyring
    (``cs.load_stored_session()``, converted to the same ``(date,
    days_left)`` shape). Raises on an unknown browser name, a missing
    cookie database, or a jar read failure -- that is Chrome's side; the
    keyring side never raises on its own (``load_stored_session``'s own
    contract is to fail inward to ``None``), so a keyring problem alone
    never takes this whole check down. ``fetch_session_token`` below is
    what turns a raised exception into a check instead of a crash.
    """
    cs = cc_mod._helpers()
    db, _app = cs.BROWSERS[browser]
    rows = probe_cookies.read_jar(Path(db))
    now = time.time()
    chrome = probe_cookies.session_horizon(rows, now)
    keyring = _stored_session_horizon(cs.load_stored_session(), now)
    return chrome, keyring


def _stored_session_horizon(
    stored: dict[str, Any] | None, now: float
) -> tuple[str, float] | None:
    """The keyring's stored session record in ``probe_cookies.session_horizon``'s
    own ``(date, days_left)`` shape -- ``None`` when nothing is stored yet,
    or its expiry is unknown."""
    if not stored:
        return None
    expires = stored.get("expires")
    if expires is None:
        return None
    day = datetime.fromtimestamp(expires, UTC).date().isoformat()
    return day, (expires - now) / 86400


def _later_horizon(
    chrome: tuple[str, float] | None, keyring: tuple[str, float] | None
) -> tuple[tuple[str, float], tuple[str, float] | None, str]:
    """``(winner, the other one or None, "chrome"|"keyring")`` -- the later
    of the two horizons (comparing ``days_left`` is equivalent to comparing
    expiry epochs, since both were read at the same "now"). Callers handle
    "both None" themselves; this is never called with both.
    """
    if keyring is not None and (chrome is None or keyring[1] > chrome[1]):
        return keyring, chrome, "keyring"
    return chrome, keyring, "chrome"  # type: ignore[return-value]


def session_token_check(
    chrome: tuple[str, float] | None,
    keyring: tuple[str, float] | None,
    error: str = "",
) -> dict[str, Any]:
    """Block when NEITHER the cookie jar nor the keyring holds a usable
    session token, or the LATER of the two has already expired -- both
    mean this machine cannot prove who is signed in. Warn inside the last
    two weeks of the later one, so there is time before it does. Judging
    the later of the two (chatgpt_session.py, "Session token renewal") is
    what makes this catch a renewal the keyring has but Chrome's own jar
    has not caught up to yet, and vice versa. ``SESSION_WARN_DAYS`` and the
    90-day issue window were measured 2026-09-20 (probe_cookies.py).
    """
    if chrome is None and keyring is None:
        detail = "no persistent session token in the cookie jar or the keyring"
        if error:
            detail += f": {error}"
        return preflight.check(
            "health", "session token", "block", detail, SESSION_TOKEN_FIX
        )
    winner, other, source = _later_horizon(chrome, keyring)
    expires, days_left = winner
    detail = f"expires {expires}, {days_left:.1f} days left"
    if source == "keyring" and other is not None:
        detail += (
            f" (from keyring; Chrome's copy expires {other[0]}, "
            f"{other[1]:.1f} days left)"
        )
    elif source == "keyring":
        detail += " (from keyring; Chrome's own jar has none)"
    elif keyring is None:
        detail += " (no renewed copy in the keyring yet)"
    else:
        detail += (
            f" (from Chrome; keyring's copy expires {other[0]}, "
            f"{other[1]:.1f} days left)"
        )
    if days_left <= 0:
        return preflight.check(
            "health", "session token", "block", detail, SESSION_TOKEN_FIX
        )
    if days_left < SESSION_WARN_DAYS:
        return preflight.check(
            "health", "session token", "warn", detail, SESSION_TOKEN_FIX
        )
    return preflight.check("health", "session token", "ok", detail)


def fetch_session_token(cc_mod: Any, browser: str) -> dict[str, Any]:
    """Never raises: a missing cookie DB, an unknown browser name or an
    unreadable jar becomes the same "no persistent session token" block a
    jar and keyring both empty would, since all of them mean this machine
    cannot prove who is signed in."""
    try:
        chrome, keyring = _read_session_horizon(cc_mod, browser)
    except Exception as exc:
        return session_token_check(None, None, error=str(exc)[:200])
    return session_token_check(chrome, keyring)


# ---------------------------------------------------------------------------
# GROUP health, check 2: the live-test sandbox's own identity and cleanliness
# ---------------------------------------------------------------------------


def sandbox_check(
    gizmo_status: int,
    gizmo_name: str,
    expected_id: str,
    expected_name: str,
    conv_status: int,
    conv_titles: list[str],
) -> dict[str, Any]:
    """Block on a wrong or unreadable sandbox before ever asking what is in
    it: a wrong sandbox would let a live test write to a real project
    (TESTING.md section 1). Only once its identity is confirmed does a
    leftover conversation matter, and even that is a warn, never a block --
    the live tests clean up after themselves; this only reports when they
    have not.
    """
    if gizmo_status != 200 or gizmo_name != expected_name:
        return preflight.check(
            "health",
            "sandbox",
            "block",
            f"gizmos/{expected_id} returned HTTP {gizmo_status}, display "
            f"name {gizmo_name!r} (expected {expected_name!r})",
            SANDBOX_WRONG_FIX,
        )
    if conv_status != 200:
        return preflight.check(
            "health",
            "sandbox",
            "warn",
            f"{expected_id} is {expected_name!r}, but its conversations "
            f"listing returned HTTP {conv_status}",
        )
    if conv_titles:
        return preflight.check(
            "health",
            "sandbox",
            "warn",
            f"{len(conv_titles)} conversation(s) left in the sandbox: "
            + ", ".join(conv_titles),
            SANDBOX_DIRTY_FIX.format(id=expected_id),
        )
    return preflight.check(
        "health",
        "sandbox",
        "ok",
        f"{expected_id} is {expected_name!r}, 0 conversations left",
    )


def fetch_sandbox(session: Any, sandbox: dict[str, str]) -> dict[str, Any]:
    """gizmos/<id> must be the sandbox before gizmos/<id>/conversations is
    ever read -- a wrong project's chat list is not worth reading."""
    expected_id = sandbox["id"]
    expected_name = sandbox["name"]
    status, payload = session.session.call(lp.GIZMO.format(id=expected_id))
    payload = payload if isinstance(payload, dict) else {}
    display = (payload.get("gizmo") or {}).get("display") or {}
    gizmo_name = str(display.get("name") or "")

    conv_status = 0
    conv_titles: list[str] = []
    if status == 200 and gizmo_name == expected_name:
        conv_status, body = session.session.call(
            f"{clean_chats.PROJECT_CONVERSATIONS.format(id=expected_id)}"
            "?cursor=0&limit=50"
        )
        if conv_status == 200 and isinstance(body, dict):
            conv_titles = [
                str(item.get("title") or "")
                for item in body.get("items") or []
                if isinstance(item, dict)
            ]
    return sandbox_check(
        status, gizmo_name, expected_id, expected_name, conv_status, conv_titles
    )


# ---------------------------------------------------------------------------
# GROUP health, check 3: two reads preflight.py never makes
# ---------------------------------------------------------------------------


def read_endpoints_check(sidebar_status: int, pins_status: int) -> dict[str, Any]:
    """Block on any non-200: both are plain GETs with no side effect, so
    unlike "send gates" there is nothing here worth tolerating a failure
    over -- a read that fails is exactly what this check exists to catch."""
    if 429 in (sidebar_status, pins_status):
        return preflight.check(
            "health",
            "read endpoints",
            "block",
            "rate limited on the read path",
            preflight.RATE_LIMIT_FIX,
        )
    if sidebar_status != 200 or pins_status != 200:
        return preflight.check(
            "health",
            "read endpoints",
            "block",
            f"sidebar {sidebar_status}, pins {pins_status}",
        )
    return preflight.check(
        "health",
        "read endpoints",
        "ok",
        f"sidebar {sidebar_status}, pins {pins_status}",
    )


def fetch_read_endpoints(session: Any) -> dict[str, Any]:
    sidebar_status, _ = session.session.call(f"{lp.SIDEBAR}?owned_only=true&limit=5")
    pins_status, _ = session.session.call(PINS)
    return read_endpoints_check(sidebar_status, pins_status)


def skills_inventory_check(
    status: int, payload_valid: bool, skills: list[dict[str, Any]]
) -> dict[str, Any]:
    """Verdict for the skills inventory read over the health session."""
    if status == 429:
        return preflight.check(
            "health",
            "skills inventory",
            "block",
            "rate limited on the skills read path",
            preflight.RATE_LIMIT_FIX,
        )
    if status != 200:
        return preflight.check(
            "health",
            "skills inventory",
            "block",
            f"hazelnuts endpoint returned HTTP {status}",
        )
    if not payload_valid:
        return preflight.check(
            "health",
            "skills inventory",
            "block",
            "hazelnuts endpoint returned a malformed payload",
        )
    item = lsk.find_skill(skills, EXPECTED_SKILL)
    line, met = lsk.expectation_line(EXPECTED_SKILL, item)
    detail = f"{len(skills)} installed; {line.removeprefix('expect ')}"
    return preflight.check(
        "health",
        "skills inventory",
        "ok" if met else "block",
        detail,
        None if met else f"list_skills.py --expect {EXPECTED_SKILL}",
    )


def fetch_skills_inventory(
    session: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and redact skills once over the already-authenticated session."""
    status, body = session.session.call(lsk.HAZELNUTS)
    raw_skills = body.get("hazelnuts") if isinstance(body, dict) else None
    payload_valid = isinstance(raw_skills, list)
    skills = [item for item in (raw_skills or []) if isinstance(item, dict)]
    check = skills_inventory_check(status, payload_valid, skills)
    return check, {
        "available": status == 200 and payload_valid,
        "status": status,
        "skills": [lsk.redacted_skill(item) for item in skills],
    }


def _write_skills_json(path_text: str, checked_at: str, doc: dict[str, Any]) -> None:
    """Persist only list_skills.py's already-redacted inventory shape."""
    if not path_text:
        return
    out = Path(path_text).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"checked_at": checked_at, **doc}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def health_checks(
    cc_mod: Any, session: Any, browser: str, sandbox: dict[str, str]
) -> list[dict[str, Any]]:
    """The fifth group, run only once a session has already opened: the
    session token's own horizon, the sandbox's identity, and two reads
    preflight.py never makes."""
    return [
        fetch_session_token(cc_mod, browser),
        fetch_sandbox(session, sandbox),
        fetch_read_endpoints(session),
    ]


# ---------------------------------------------------------------------------
# The --json "facts" block: a stable contract for the cron wrapper
# ---------------------------------------------------------------------------


def _empty_facts(sandbox: dict[str, str]) -> dict[str, Any]:
    """The full shape with every key present but null or empty -- what
    ``--json`` writes when the link is down and no session ever opens, and
    the starting point ``facts_of`` fills in once one does. The wrapper
    this is built for relies on the shape never changing key by key."""
    sandbox_id = sandbox["id"]
    return {
        "session_expires": None,
        "session_days_left": None,
        "session_source": None,
        "chrome_session_expires": None,
        "keyring_session_expires": None,
        "model": "",
        "effort": "",
        "preset": None,
        "presets": 0,
        "sandbox_id": sandbox_id,
        "sandbox_name": sandbox["name"],
        "sandbox_conversations": 0,
        "gates": None,
        "endpoints": {
            "gizmos/snorlax/sidebar": 0,
            "pins": 0,
            "hazelnuts": 0,
            f"gizmos/{sandbox_id}": 0,
            f"gizmos/{sandbox_id}/conversations": 0,
        },
    }


def _model_effort_facts(session: Any) -> dict[str, Any]:
    """model/effort/preset/presets for the facts block. Independent of
    preflight's own "model and effort" check, which measures the same
    thing but folds it into a sentence for a person to read; a machine
    contract re-derives the structured value from ``model_settings``
    rather than parsing that sentence back apart."""
    _status, models = session.session.call(ms.MODELS)
    models = models if isinstance(models, dict) else {}
    _status, settings = session.session.call(ms.SETTINGS)
    settings = settings if isinstance(settings, dict) else {}
    presets = ms.presets_of(models)
    config = ms.server_config(settings)
    preset = ms.resolve_preset(presets, config["model"], config["effort"])
    return {
        "model": config["model"],
        "effort": config["effort"],
        "preset": preset["title"] if preset else None,
        "presets": len(presets),
    }


def _gates_facts(session: Any) -> dict[str, bool] | None:
    """None if the sentinel read itself failed -- the same tolerance
    ``preflight.fetch_gates`` has; a transport hiccup is not evidence the
    send path changed."""
    try:
        body = psg.fetch(session)
    except Exception:
        return None
    return psg.gates_from(body)


def facts_of(
    cc_mod: Any, session: Any, browser: str, sandbox: dict[str, str]
) -> dict[str, Any]:
    """Everything ``--json``'s "facts" block needs. Shape and counts only,
    same as every check above -- never the user's data, never a cookie
    value (TESTING.md section 2)."""
    facts = _empty_facts(sandbox)
    sandbox_id = sandbox["id"]

    try:
        chrome, keyring = _read_session_horizon(cc_mod, browser)
    except Exception:
        chrome, keyring = None, None
    if chrome is not None:
        facts["chrome_session_expires"] = chrome[0]
    if keyring is not None:
        facts["keyring_session_expires"] = keyring[0]
    if chrome is not None or keyring is not None:
        winner, _other, source = _later_horizon(chrome, keyring)
        facts["session_expires"], days_left = winner
        facts["session_days_left"] = round(days_left, 2)
        facts["session_source"] = source

    facts.update(_model_effort_facts(session))
    facts["gates"] = _gates_facts(session)

    sidebar_status, _ = session.session.call(f"{lp.SIDEBAR}?owned_only=true&limit=5")
    facts["endpoints"]["gizmos/snorlax/sidebar"] = sidebar_status
    pins_status, _ = session.session.call(PINS)
    facts["endpoints"]["pins"] = pins_status

    gizmo_status, payload = session.session.call(lp.GIZMO.format(id=sandbox_id))
    facts["endpoints"][f"gizmos/{sandbox_id}"] = gizmo_status
    payload = payload if isinstance(payload, dict) else {}
    display = (payload.get("gizmo") or {}).get("display") or {}
    gizmo_name = str(display.get("name") or "")
    if gizmo_status == 200 and gizmo_name == sandbox["name"]:
        conv_status, body = session.session.call(
            f"{clean_chats.PROJECT_CONVERSATIONS.format(id=sandbox_id)}"
            "?cursor=0&limit=50"
        )
        facts["endpoints"][f"gizmos/{sandbox_id}/conversations"] = conv_status
        if conv_status == 200 and isinstance(body, dict):
            facts["sandbox_conversations"] = len(body.get("items") or [])
    return facts


# ---------------------------------------------------------------------------
# Rendering: preflight's own render(), with "health" given a place in it
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--browser",
        action="store_true",
        help="also open a window and confirm the composer appears "
        "(preflight.browser_composer_check); opt-in, costs a browser slot "
        "and about twenty seconds, and runs last",
    )
    ap.add_argument(
        "--browser-name",
        default="chrome",
        metavar="NAME",
        help="which browser's cookies to use (default chrome)",
    )
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write the verdict document, with a 'facts' block for the cron "
        "wrapper, here",
    )
    ap.add_argument(
        "--skills-json",
        default="",
        metavar="PATH",
        help="write the redacted skills inventory read over this same session",
    )
    ap.add_argument(
        "--diagnostics",
        default="",
        metavar="PATH",
        help="append structured, credential-free transport/stage events as JSONL",
    )
    args = ap.parse_args(argv)
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")
    diagnostic = _diagnostic_sink(args.diagnostics)
    _diag(
        diagnostic,
        "health_run_start",
        browser=args.browser_name,
        browser_check=args.browser,
        auth_backoff_s=list(HEALTH_AUTH_BACKOFF),
        request_timeout_s=HEALTH_REQUEST_TIMEOUT,
        request_retries=HEALTH_REQUEST_RETRIES,
    )
    sandbox = _sandbox()

    checks: list[dict[str, Any]] = []
    _diag(diagnostic, "stage_start", stage="host")
    host = preflight.host_checks(cc, None)
    checks += host
    _diag(diagnostic, "stage_end", stage="host", **_state_counts(host))

    _diag(diagnostic, "stage_start", stage="link")
    link = preflight.link_checks(preflight.WIRELESS_PATH)
    checks += link
    _diag(diagnostic, "stage_end", stage="link", **_state_counts(link))

    facts = _empty_facts(sandbox)
    skills_doc: dict[str, Any] = {"available": False, "status": 0, "skills": []}

    if any(c["state"] == "block" for c in link):
        checks.append(
            preflight.check(
                "account",
                "skipped",
                "warn",
                "wireless link is down; account, run and health checks "
                "were skipped so a dead link is never reported as a "
                "blocked account",
            )
        )
    else:
        _diag(diagnostic, "stage_start", stage="auth")
        session, error = preflight.open_chatgpt_session(
            cc,
            args.browser_name,
            auth_backoff=HEALTH_AUTH_BACKOFF,
            diagnostic=diagnostic,
            request_timeout=HEALTH_REQUEST_TIMEOUT,
            request_retries=HEALTH_REQUEST_RETRIES,
        )
        _diag(
            diagnostic,
            "stage_end",
            stage="auth",
            success=error is None,
            error=error,
        )
        if error is not None:
            checks.append(
                preflight.check(
                    "account",
                    "auth and read",
                    "block",
                    f"could not authenticate: {error}",
                )
            )
        else:
            _diag(diagnostic, "stage_start", stage="account")
            account = preflight.account_checks(session)
            checks += account
            _diag(diagnostic, "stage_end", stage="account", **_state_counts(account))

            _diag(diagnostic, "stage_start", stage="run")
            run = preflight.run_checks(session, "", None)
            checks += run
            _diag(diagnostic, "stage_end", stage="run", **_state_counts(run))

            _diag(diagnostic, "stage_start", stage="health")
            health_rows = health_checks(cc, session, args.browser_name, sandbox)
            checks += health_rows
            _diag(
                diagnostic,
                "stage_end",
                stage="health",
                **_state_counts(health_rows),
            )

            _diag(diagnostic, "stage_start", stage="skills")
            skill_check, skills_doc = fetch_skills_inventory(session)
            checks.append(skill_check)
            _write_skills_json(args.skills_json, checked_at, skills_doc)
            _diag(
                diagnostic,
                "stage_end",
                stage="skills",
                status=skills_doc["status"],
                available=skills_doc["available"],
                count=len(skills_doc["skills"]),
                **_state_counts([skill_check]),
            )

            facts = facts_of(cc, session, args.browser_name, sandbox)
            facts["endpoints"]["hazelnuts"] = skills_doc["status"]
            if args.browser:
                _diag(diagnostic, "stage_start", stage="browser")
                browser_row = preflight.browser_composer_check(cc, args.browser_name)
                checks.append(browser_row)
                _diag(
                    diagnostic,
                    "stage_end",
                    stage="browser",
                    **_state_counts([browser_row]),
                )

    if args.skills_json and not Path(args.skills_json).expanduser().exists():
        _write_skills_json(args.skills_json, checked_at, skills_doc)

    print(preflight.render(checks, order=HEALTH_GROUP_ORDER))
    verdict, exit_code = preflight.verdict_of(checks)
    print(verdict)
    _diag(
        diagnostic,
        "health_run_end",
        verdict=verdict,
        exit_code=exit_code,
        **_state_counts(checks),
    )

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "checked_at": checked_at,
            "verdict": verdict,
            "exit_code": exit_code,
            "checks": checks,
            "facts": facts,
        }
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwritten to {out}")
    return exit_code


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
