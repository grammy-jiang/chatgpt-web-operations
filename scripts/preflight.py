#!/usr/bin/env python3
"""One go/no-go verdict before a research run starts. One session, reused.

    preflight.py [--workdir DIR] [--project g-p-<id>] [--browser]
        [--json PATH] [--browser-name chrome]

Eleven real failures across eight research topics on this account (2 rate
limits, 2 host-contention failures, 2 "message was not posted", 2 HTTP read
timeouts, 1 composer that never appeared because the session was logged out
or challenged, 1 assistant that never finished, 1 reply missing a required
block), plus two conversations still stuck at status "sent" -- and the
orchestrator checked none of it before starting: it parses arguments, makes
the workdir, re-execs into Playwright and dispatches. This checks all of it
first, so it is discovered here instead of by crashing into it.

Four groups of checks, always in this order -- host, link, account, run:

    host     available memory, load average, in-flight Playwright browsers,
             Xvfb / Google Chrome / the venv's playwright, cryptography and
             dbus imports, and (with --workdir) free disk
    link     the wireless interface(s) in /proc/net/wireless -- read before
             any session opens, because a dead link must never be reported
             as a blocked account (SKILL.md, "Name the path that failed").
             When it has no interface with a link, the account and run
             groups are skipped rather than let a doomed HTTP call print
             something that reads like an account block
    account  authentication and a cheap read, the send gates
             (probe_send_gates.py), the plan window and credits
             (probe_account.py) -- one session, opened once
    run      the model and effort a send would inherit
             (model_settings.py), custom instructions and memory
             (profile_context.py), and -- with --project / --workdir -- the
             project's memory scope, conversations stuck at "sent" and
             profile-context freshness (review_topic.py)

``--browser`` adds one more check, last because everything above is cheap
and this is not: it opens a real window and confirms the composer appears,
the one failure no HTTP read can see. Opt-in: it costs one of the
``RP_MAX_BROWSERS`` slots and about twenty seconds.

Each check is a record: group, name, state ("ok" / "warn" / "block"),
detail, and an optional fix. They print grouped, then a verdict line --
"GO", "GO WITH WARNINGS (n)" or "DO NOT START (n blocking)". ``--json PATH``
writes ``{"checked_at", "verdict", "exit_code", "checks": [...]}``.

Exit 0 (GO) when nothing is warn or block, 2 (GO WITH WARNINGS) when only
warnings, 1 (DO NOT START) when anything blocks.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chatgpt_client as cc
import list_projects as lp
import model_settings as ms
import probe_account as pa
import probe_send_gates as psg
import profile_context as pc
import review_topic as rt
from _common import ensure_venv

GROUP_ORDER = ("host", "link", "account", "run", "browser")


def check(
    group: str, name: str, state: str, detail: str, fix: str | None = None
) -> dict[str, Any]:
    """One record. Every check function below returns this same shape, so
    ``main`` can print and JSON-encode them uniformly."""
    return {"group": group, "name": name, "state": state, "detail": detail, "fix": fix}


# ---------------------------------------------------------------------------
# GROUP host: no network, local reads only
# ---------------------------------------------------------------------------

LOAD_WARN_PER_CORE = 0.75
DISK_MIN_BYTES = 2 * 1024**3


def memory_check(available_mb: float, minimum_mb: float) -> dict[str, Any]:
    if available_mb < minimum_mb:
        return check(
            "host",
            "available memory",
            "block",
            f"{available_mb:.0f} MB available, need at least {minimum_mb:.0f} "
            "MB (RP_MIN_AVAILABLE_MB)",
            "close other programs, or wait for memory to free up (SKILL.md, "
            "'The browser is budgeted')",
        )
    return check(
        "host",
        "available memory",
        "ok",
        f"{available_mb:.0f} MB available (>= {minimum_mb:.0f} MB needed)",
    )


def load_check(load1: float, cpu_count: int | None) -> dict[str, Any]:
    """Warn above 0.75 per core: a 172 kB prompt fill blew its budget at load
    5 on this four-core host (SKILL.md, "Do not compete with a send for the
    CPU"); that is the failure this warns about."""
    cores = cpu_count or 1
    per_core = load1 / cores
    detail = (
        f"load average {load1:.2f} across {cores} core(s) "
        f"({per_core:.2f} per core, threshold {LOAD_WARN_PER_CORE})"
    )
    if per_core > LOAD_WARN_PER_CORE:
        return check(
            "host",
            "load average",
            "warn",
            detail,
            "wait for other CPU work to finish before sending (SKILL.md, "
            "'Do not compete with a send for the CPU')",
        )
    return check("host", "load average", "ok", detail)


def inflight_browsers_check(pids: set[int], max_browsers: int) -> dict[str, Any]:
    """A second window takes the host into swap (SKILL.md, "The browser is
    budgeted"), so any Playwright-launched Chrome already running blocks."""
    if pids:
        return check(
            "host",
            "in-flight browsers",
            "block",
            f"{len(pids)} Playwright-launched browser process(es) already "
            f"running (RP_MAX_BROWSERS={max_browsers}): {sorted(pids)}",
            "wait for the send in flight, or kill it between gates",
        )
    return check("host", "in-flight browsers", "ok", "no in-flight browser process")


def tooling_check(
    xvfb: bool, chrome: bool, playwright: bool, cryptography: bool, dbus: bool
) -> dict[str, Any]:
    missing = [
        label
        for label, present in (
            ("Xvfb", xvfb),
            ("Google Chrome", chrome),
            ("the venv's playwright import", playwright),
            ("the venv's cryptography import", cryptography),
            ("the venv's dbus import", dbus),
        )
        if not present
    ]
    if missing:
        return check(
            "host",
            "browser tooling",
            "block",
            "missing: " + ", ".join(missing),
            "bash bootstrap.sh",
        )
    return check(
        "host",
        "browser tooling",
        "ok",
        "Xvfb, Google Chrome and the venv's playwright/cryptography/dbus "
        "imports are all present",
    )


def _existing_ancestor(path: Path) -> Path:
    """``path`` itself, or the nearest parent that already exists: a new
    research topic's workdir may not be created yet, but disk usage is a
    filesystem property that does not need the exact leaf to exist."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or "/")


def disk_check(free_bytes: int, workdir: Path) -> dict[str, Any]:
    free_gb = free_bytes / 1024**3
    detail = f"{free_gb:.1f} GB free under {workdir}"
    if free_bytes < DISK_MIN_BYTES:
        return check(
            "host", "disk space", "warn", detail, f"free disk space under {workdir}"
        )
    return check("host", "disk space", "ok", detail)


def host_checks(cc: Any, workdir: Path | None) -> list[dict[str, Any]]:
    checks = [
        memory_check(cc.available_mb(), cc.MIN_AVAILABLE_MB),
        load_check(os.getloadavg()[0], os.cpu_count()),
        inflight_browsers_check(cc.scripted_browser_pids(), cc.MAX_BROWSERS),
        tooling_check(
            shutil.which("Xvfb") is not None,
            shutil.which("google-chrome") is not None
            or shutil.which("google-chrome-stable") is not None,
            importlib.util.find_spec("playwright") is not None,
            importlib.util.find_spec("cryptography") is not None,
            importlib.util.find_spec("dbus") is not None,
        ),
    ]
    if workdir is not None:
        free = shutil.disk_usage(_existing_ancestor(workdir)).free
        checks.append(disk_check(free, workdir))
    return checks


# ---------------------------------------------------------------------------
# GROUP link: no network calls, one file read -- runs before "account" so a
# dead link is never mistaken for a blocked account (see module docstring).
# ---------------------------------------------------------------------------

WIRELESS_PATH = Path("/proc/net/wireless")


def _wireless_float(field: str) -> float | None:
    try:
        return float(field.rstrip("."))
    except ValueError:
        return None


def wireless_status(text: str | None) -> dict[str, Any]:
    """Per-interface parse of ``/proc/net/wireless``. ``text`` is ``None``
    when the file does not exist (no wireless interface, or wired only).

    The three "Quality" fields are status, link and level (dBm). Status is a
    pre-mac80211 leftover: measured 2026-09-20 on this host, all three
    RTL8812AU-family interfaces (wlan0, wlan1, wlan2) read status ``0000``
    while fully associated to a 2.4/5 GHz network, so status cannot tell
    "linked" from "not linked" here. ``link`` is the field mac80211 actually
    zeroes when an interface is not associated, so that decides it; ``level``
    (dBm) is kept only to report alongside it.
    """
    if text is None:
        return {"present": False, "interfaces": {}}
    interfaces: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Inter-") or line.startswith("face"):
            continue
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        if len(fields) < 3:
            continue
        link = _wireless_float(fields[1])
        interfaces[name.strip()] = {
            "link": link,
            "level": _wireless_float(fields[2]),
            "linked": bool(link),
        }
    return {"present": True, "interfaces": interfaces}


def _interface_line(name: str, info: dict[str, Any]) -> str:
    level = "?" if info["level"] is None else f"{info['level']:.0f} dBm"
    state = "linked" if info["linked"] else "not linked"
    return f"{name} {state} (level {level})"


def wireless_check(status: dict[str, Any]) -> dict[str, Any]:
    """Block only when the file exists and lists no interface with a link;
    an absent file (no wireless hardware, or wired only) never blocks."""
    if not status["present"]:
        return check(
            "link",
            "wireless link",
            "ok",
            "no /proc/net/wireless (no wireless interface on this host, or wired only)",
        )
    interfaces = status["interfaces"]
    detail = (
        "; ".join(
            _interface_line(name, info) for name, info in sorted(interfaces.items())
        )
        or "no interface listed"
    )
    if any(info["linked"] for info in interfaces.values()):
        return check("link", "wireless link", "ok", detail)
    return check(
        "link",
        "wireless link",
        "block",
        detail,
        "reconnect the wireless interface before starting",
    )


def _read_wireless(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def link_checks(path: Path) -> list[dict[str, Any]]:
    return [wireless_check(wireless_status(_read_wireless(path)))]


# ---------------------------------------------------------------------------
# GROUP account: one session, reused by GROUP run too
# ---------------------------------------------------------------------------

ME = "/backend-api/me"
CONVERSATIONS_PROBE = "/backend-api/conversations?offset=0&limit=3&order=updated"
RATE_LIMIT_FIX = (
    "wait; the limit clears in about thirty minutes (SKILL.md, Rate limits)"
)


def open_chatgpt_session(cc: Any, browser: str) -> tuple[Any, str | None]:
    """(session, None) on success, (None, reason) on failure -- never raises,
    so a broken cookie jar becomes one block check instead of a crash."""
    try:
        return cc.ChatGPTSession(browser), None
    except Exception as exc:
        return None, str(exc)[:200]


def account_reads_check(
    me_status: int, listing_status: int, listing_count: int
) -> dict[str, Any]:
    if 429 in (me_status, listing_status):
        return check(
            "account",
            "auth and read",
            "block",
            "rate limited on the read path",
            RATE_LIMIT_FIX,
        )
    if me_status != 200:
        return check(
            "account", "auth and read", "block", f"/backend-api/me returned {me_status}"
        )
    if listing_status != 200:
        return check(
            "account",
            "auth and read",
            "block",
            f"conversations listing returned {listing_status}",
        )
    return check(
        "account",
        "auth and read",
        "ok",
        f"authenticated, /backend-api/me 200, listing returned {listing_count}",
    )


def fetch_account_reads(session: Any) -> dict[str, Any]:
    me_status, _ = session.session.call(ME)
    listing_status, listing_body = session.session.call(CONVERSATIONS_PROBE)
    count = (
        len(listing_body.get("items") or [])
        if listing_status == 200 and isinstance(listing_body, dict)
        else 0
    )
    return account_reads_check(me_status, listing_status, count)


NORMAL_GATES = {"proofofwork": True, "turnstile": True, "so": True}


def send_gates_check(gates: dict[str, bool] | None, error: str = "") -> dict[str, Any]:
    """Never blocks: ``gates`` is ``None`` when the sentinel read itself
    failed, which is a transport hiccup, not evidence the send path
    changed."""
    if gates is None:
        detail = "could not read the sentinel endpoint"
        if error:
            detail += f": {error}"
        return check("account", "send gates", "warn", detail, "probe_send_gates.py")
    if gates == NORMAL_GATES:
        return check(
            "account",
            "send gates",
            "ok",
            "proofofwork, turnstile and so are required (today's normal); a "
            "browser is needed to send",
        )
    return check(
        "account",
        "send gates",
        "warn",
        f"gate set is {gates!r}, not the usual all-required trio",
        "probe_send_gates.py, then rebuild the transport map if it changed "
        "(references/endpoint-discovery.md)",
    )


def fetch_gates(session: Any) -> dict[str, Any]:
    try:
        body = psg.fetch(session)
    except Exception as exc:
        return send_gates_check(None, error=str(exc)[:200])
    return send_gates_check(psg.gates_from(body))


def usage_check(status: int, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Informational, always ok: this window and these credits do not cover
    chat sends (probe_account.py), so a failed or odd read never blocks."""
    if status != 200 or payload is None:
        return check(
            "account", "plan and credits", "ok", f"usage: unavailable (HTTP {status})"
        )
    return check(
        "account", "plan and credits", "ok", "; ".join(pa.usage_lines(payload))
    )


def fetch_usage(session: Any) -> dict[str, Any]:
    status, payload = session.session.call(pa.USAGE)
    return usage_check(status, payload if isinstance(payload, dict) else None)


def account_checks(session: Any) -> list[dict[str, Any]]:
    """Three independent reads over one session: auth, gates, usage."""
    return [fetch_account_reads(session), fetch_gates(session), fetch_usage(session)]


# ---------------------------------------------------------------------------
# GROUP run: the same session
# ---------------------------------------------------------------------------


def model_effort_check(
    config: dict[str, str], preset: dict[str, Any] | None, total_presets: int
) -> dict[str, Any]:
    """Always ok: informational, and this is what a send inherits when
    nothing pins an explicit --effort/--model."""
    model = config.get("model") or "-"
    effort = config.get("effort") or "-"
    where = (
        f"preset {preset['title']!r} ({preset['position']} of {total_presets})"
        if preset
        else "matches no preset"
    )
    return check(
        "run",
        "model and effort",
        "ok",
        f"model={model} effort={effort} -> {where}; a send without "
        "--effort/--model inherits this",
    )


def fetch_model_effort(session: Any) -> dict[str, Any]:
    _status, models = session.session.call(ms.MODELS)
    models = models if isinstance(models, dict) else {}
    _status, settings = session.session.call(ms.SETTINGS)
    settings = settings if isinstance(settings, dict) else {}
    presets = ms.presets_of(models)
    config = ms.server_config(settings)
    preset = ms.resolve_preset(presets, config["model"], config["effort"])
    return model_effort_check(config, preset, len(presets))


def instructions_memory_check(
    ci: dict[str, Any], mem: dict[str, Any]
) -> dict[str, Any]:
    """Lengths and counts only, never the text; always ok (informational)."""
    entries = "?" if mem["entries"] is None else str(mem["entries"])
    detail = (
        ("enabled" if ci["enabled"] else "disabled")
        + f", name {len(ci['name'])} chars, role {len(ci['role'])} chars, "
        f"traits {len(ci['traits'])} chars, about {len(ci['about'])} chars; "
        f"memory {entries} entries, {mem['tokens_used']}/{mem['tokens_max']} tokens"
    )
    return check("run", "custom instructions and memory", "ok", detail)


def fetch_instructions_memory(session: Any) -> dict[str, Any]:
    _status, ci_payload = session.session.call(pc.USER_SYSTEM_MESSAGES)
    ci_payload = ci_payload if isinstance(ci_payload, dict) else {}
    _status, mem_summary = session.session.call(pc.MEMORY_SUMMARY)
    mem_summary = mem_summary if isinstance(mem_summary, dict) else {}
    _status, mem_entries = session.session.call(pc.MEMORY_ENTRIES)
    mem_entries = mem_entries if isinstance(mem_entries, dict) else {}
    ci = pc.custom_instructions_of(ci_payload)
    mem = pc.memory_of(mem_summary, mem_entries)
    return instructions_memory_check(ci, mem)


def project_check(
    project_id: str, status: int, payload: dict[str, Any] | None
) -> dict[str, Any]:
    if status != 200 or payload is None:
        return check(
            "run", "project", "block", f"project {project_id} not found (HTTP {status})"
        )
    project = lp.project_of(payload)
    scope = project["memory_scope"] or "-"
    detail = f"{project['id']} {project['name']!r}, memory scope {scope}"
    if project["memory_scope"] != "project_v2":
        return check(
            "run",
            "project",
            "warn",
            f"{detail}; worker chats can read and write the account's memory",
            f"project_settings.py {project_id} --memory project-only --apply",
        )
    return check("run", "project", "ok", detail)


def fetch_project(session: Any, project_id: str) -> dict[str, Any]:
    status, payload = session.session.call(lp.GIZMO.format(id=project_id))
    return project_check(
        project_id, status, payload if isinstance(payload, dict) else None
    )


def load_conversations(workdir: Path) -> list[dict[str, Any]]:
    """``chatgpt/conversations.json``, tolerant of it not existing yet: a
    preflight runs before a round starts, so there may be nothing to read."""
    path = workdir / "chatgpt" / "conversations.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def uncollected_check(
    workdir: Path, conversations: list[dict[str, Any]]
) -> dict[str, Any]:
    pending = [
        c for c in conversations if isinstance(c, dict) and c.get("status") == "sent"
    ]
    if not pending:
        return check(
            "run",
            "uncollected replies",
            "ok",
            "no conversation is stuck at status 'sent'",
        )
    return check(
        "run",
        "uncollected replies",
        "warn",
        f"{len(pending)} reply(ies) generated and never collected",
        f"round_status.py {workdir}",
    )


def profile_context_freshness_check(workdir: Path) -> dict[str, Any]:
    fix = f"profile_context.py --json {workdir}/chatgpt/profile_context.json"
    found = rt.newest_profile_context(workdir)
    if found is None:
        return check(
            "run",
            "profile context",
            "warn",
            "no chatgpt/profile_context*.json recorded",
            fix,
        )
    path, when = found
    started = rt.newest_round_start(workdir)
    if started is not None and when < started:
        return check(
            "run",
            "profile context",
            "warn",
            f"{path.name} (captured_at {when.isoformat()}) is older than the "
            f"newest round's start ({started.isoformat()})",
            fix,
        )
    return check(
        "run", "profile context", "ok", f"{path.name}, captured_at {when.isoformat()}"
    )


def run_checks(
    session: Any, project_id: str, workdir: Path | None
) -> list[dict[str, Any]]:
    """What a send would inherit, plus what --project / --workdir add."""
    checks = [fetch_model_effort(session), fetch_instructions_memory(session)]
    if project_id:
        checks.append(fetch_project(session, project_id))
    if workdir is not None:
        checks.append(uncollected_check(workdir, load_conversations(workdir)))
        checks.append(profile_context_freshness_check(workdir))
    return checks


# ---------------------------------------------------------------------------
# GROUP browser: opt-in (--browser), last because it is not cheap
# ---------------------------------------------------------------------------


def browser_composer_check(cc: Any, browser: str) -> dict[str, Any]:
    """Opens a window and confirms the composer appears.

    Opt-in only: costs one of ``cc.MAX_BROWSERS`` slots and about twenty
    seconds, so it runs last, after every cheap check. Reuses
    ``BrowserSender``'s own composer wait, which raises exactly the "logged
    out or challenged" failure (references/failure-atlas.md) when the
    composer never appears within 60 s.
    """
    try:
        with cc.BrowserSender(browser) as sender:
            sender._composer()
    except Exception as exc:
        return check(
            "browser",
            "composer",
            "block",
            str(exc)[:200],
            "log in to chatgpt.com from this machine's own browser, or clear "
            "any Cloudflare challenge, then re-run with --browser",
        )
    return check(
        "browser",
        "composer",
        "ok",
        "the composer appeared; a send would not be blocked by a logged-out "
        "or challenged session",
    )


# ---------------------------------------------------------------------------
# Verdict and rendering
# ---------------------------------------------------------------------------


def verdict_of(checks: list[dict[str, Any]]) -> tuple[str, int]:
    blocks = sum(1 for c in checks if c["state"] == "block")
    warns = sum(1 for c in checks if c["state"] == "warn")
    if blocks:
        return f"DO NOT START ({blocks} blocking)", 1
    if warns:
        return f"GO WITH WARNINGS ({warns})", 2
    return "GO", 0


def render(checks: list[dict[str, Any]]) -> str:
    """Grouped, human-read report: one line per check, an indented fix below
    any that has one. ``--json`` carries the same information for machines."""
    lines: list[str] = []
    for group in GROUP_ORDER:
        rows = [c for c in checks if c["group"] == group]
        if not rows:
            continue
        lines.append(f"== {group} ==")
        for c in rows:
            lines.append(f"  [{c['state']:5s}] {c['name']:<28s} {c['detail']}")
            if c["fix"]:
                lines.append(f"           fix: {c['fix']}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workdir",
        default="",
        metavar="DIR",
        help="a research topic's working directory: also checks free disk, "
        "conversations stuck at 'sent' and profile-context freshness",
    )
    ap.add_argument(
        "--project",
        default="",
        metavar="g-p-ID",
        help="also confirm this project exists and check its memory scope",
    )
    ap.add_argument(
        "--browser",
        action="store_true",
        help="also open a window and confirm the composer appears; opt-in, "
        "costs a browser slot and about twenty seconds, and runs last",
    )
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write the full verdict document here",
    )
    ap.add_argument(
        "--browser-name",
        default="chrome",
        metavar="NAME",
        help="which browser's cookies to use (default chrome)",
    )
    args = ap.parse_args(argv)
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")

    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else None
    checks: list[dict[str, Any]] = []
    checks += host_checks(cc, workdir)

    link = link_checks(WIRELESS_PATH)
    checks += link
    if any(c["state"] == "block" for c in link):
        checks.append(
            check(
                "account",
                "skipped",
                "warn",
                "wireless link is down; account and run checks were skipped "
                "so a dead link is never reported as a blocked account",
            )
        )
    else:
        session, error = open_chatgpt_session(cc, args.browser_name)
        if error is not None:
            checks.append(
                check(
                    "account",
                    "auth and read",
                    "block",
                    f"could not authenticate: {error}",
                )
            )
        else:
            checks += account_checks(session)
            checks += run_checks(session, args.project, workdir)
            if args.browser:
                checks.append(browser_composer_check(cc, args.browser_name))

    print(render(checks))
    verdict, exit_code = verdict_of(checks)
    print(verdict)

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "checked_at": checked_at,
            "verdict": verdict,
            "exit_code": exit_code,
            "checks": checks,
        }
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwritten to {out}")
    return exit_code


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
