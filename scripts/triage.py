#!/usr/bin/env python3
"""Classify a failed scheduled check before anything expensive runs. Offline.

    triage.py --run DIR [--repro JSON] [--summary TEXT] [--drift-lines FILE]
              [--registry PATH] [--json PATH] [--repeat-threshold N] [--dry-run]
    triage.py --ok RUN_ID [--registry PATH]

Layer 1 of the owner's design of 2026-10-01 evening: rules, no model. The
daily and weekly cron wrappers (``~/.local/bin/chatgpt-ops-check.sh``,
``chatgpt-ops-send-check.sh``) call this on a WARN or ALERT, after
reproducing the failed check the same way (``chatgpt-ops-repro.sh``, a
read-only ``preflight --browser`` through the dedicated window), and run
the repair agent (``chatgpt-ops-heal.sh``, a large model) only on an
``escalate`` decision. Three mornings of 2026-09-29 to 10-01 each cost one
agent run and one mail for one Cloudflare refusal; this is what counts them
as one incident.

The run's evidence directory is read, never written: ``result.txt``,
``health.json`` (preflight/health's checks and facts), ``dom/*.json`` (the
page recordings, compared with ``tests/fixtures/dom`` through
``chatgpt_client.snapshot_drift``), ``dom/send_body.shape.json`` (compared
with ``tests/fixtures/send``), and ``--drift-lines`` (the weekly wrapper's
own drift lines). It is reduced to a **signature** (what failed and how,
without timestamps or ids), a **class**, a **streak** (how many failing runs
in a row carried this signature, kept in ``--registry``; ``--ok`` records a
clean run and ends every streak) and a **decision**:

    digest           known transient or host condition, below the repeat
                     threshold: one line in the digest, no agent
    escalate         a page change, an API change, a session problem or an
                     unknown shape: the agent runs now
    escalate-repeat  the same signature on REPEAT_THRESHOLD failing runs in
                     a row: not transient, whatever it looks like

Classes, in the order the rules are tried: ``page-change`` (a recorded page
differs from its fixture in a fact the selectors read, or a composer that did
not appear on a page that shows neither Cloudflare's challenge nor a login
route -- the page facts a composer-missing message carries since
2026-10-01 evening), ``api-change`` (the api shapes check warns, or the
send body lost a field the client pins), ``session`` (a login route on the
page, or the session token check blocks), ``known-transient`` (the
challenge widget; read-path 403/429/"rate limited"; a cool-down; a busy
browser slot; the page's own requests refused), ``host`` (memory, load,
disk, in-flight browsers, displays, the keyring bus, a bounced tab), and
``unknown`` for the rest.

The reproduction (``--repro``: preflight's ``--json`` document) is the
owner's rule: a failure reproduced the same way is understood better and is
real; one that does not reproduce is probably transient. So a reproduction
whose composer check passes turns any class below the threshold into
``digest`` (with the reason recorded), and a reproduction that fails the same
way turns an ``unknown`` or ``page-change`` into ``escalate`` even on its
first day. A known transient that reproduces (the challenge is still up) is
still a digest line until it repeats: an agent cannot answer a challenge.

Exit 0 for ``digest``, 2 for either ``escalate``, 1 when the run cannot be
read. ``--json`` (default ``<run>/triage.json``) carries the same record for
the wrapper and the heal evidence. Nothing here reaches the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from _common import ensure_venv

SKILL_DIR = Path(__file__).resolve().parents[1]
DOM_FIXTURES = SKILL_DIR / "tests" / "fixtures" / "dom"
DEFAULT_REGISTRY = Path("~/.local/state/chatgpt-ops/triage.json").expanduser()
REPEAT_THRESHOLD = 2

CLASSES = (
    "page-change",
    "api-change",
    "session",
    "known-transient",
    "host",
    "unknown",
)
ESCALATE_CLASSES = frozenset({"page-change", "api-change", "session", "unknown"})
HOST_CHECKS = frozenset(
    {
        "available memory",
        "load average",
        "free disk",
        "in-flight browsers",
        "virtual displays",
        "browser tooling",
        "keyring bus",
        "browser cool-down",
        "dedicated browser",
        "wireless link",
    }
)
PAGE_FACTS_RE = re.compile(r"\[page: (?P<body>[^\]]*)\]")
COMPOSERS_RE = re.compile(r"composers=(\d+) \((\d+) hidden\)")
TRANSIENT_HTTP_RE = re.compile(r"\b(403|429)\b|rate limited|cool-down", re.I)


# ---------------------------------------------------------------------------
# reading the run
# ---------------------------------------------------------------------------


def read_result(run: Path) -> dict[str, str]:
    """``result.txt`` as a dict (``key=value`` lines), or {}."""
    path = run / "result.txt"
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip()
    except OSError:
        return {}
    return out


def read_health(run: Path) -> dict[str, Any]:
    """``health.json`` (preflight's document: verdict, checks, facts), or {}."""
    try:
        data = json.loads((run / "health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def failed_checks(health: dict[str, Any]) -> list[dict[str, Any]]:
    """The checks that are not ok, in report order."""
    return [
        c
        for c in health.get("checks") or []
        if isinstance(c, dict) and c.get("state") in ("warn", "block")
    ]


def composer_reason(detail: str) -> str:
    """What a composer check's detail says the page was: ``ok``, ``drift``,
    ``challenge``, ``refused``, ``logged-out-or-challenged``, ``chat-page``,
    ``bounced``, ``work-surface``, ``cooldown``, ``busy`` or ``other``."""
    text = detail.lower()
    if "differs from the fixture" in text:
        return "drift"
    if "the composer appeared" in text:
        return "ok"
    if "verify you are human" in text:
        return "challenge"
    if "requests are being refused" in text:
        return "refused"
    if "not on chatgpt.com" in text:
        return "bounced"
    if "this chat's composer" in text:
        return "chat-page"
    if "work surface" in text:
        return "work-surface"
    if "cool-down" in text:
        return "cooldown"
    if "slot" in text and ("busy" in text or "taken" in text):
        return "busy"
    if "logged out or challenged" in text:
        return "logged-out-or-challenged"
    return "other"


def page_facts(detail: str) -> dict[str, Any]:
    """The ``[page: ...]`` facts a composer-missing message ends with
    (``chatgpt_client.page_facts_summary``), or {} when it carries none."""
    match = PAGE_FACTS_RE.search(detail)
    if not match:
        return {}
    body = match.group("body")
    flags = dict(
        part.split("=", 1)
        for part in body.split(", ")
        if "=" in part and "(" not in part
    )
    counts = COMPOSERS_RE.search(body)
    return {
        "challenge": flags.get("challenge") == "yes",
        "login": flags.get("login") == "yes",
        "composers": int(counts.group(1)) if counts else 0,
        "hidden": int(counts.group(2)) if counts else 0,
        "toggle": flags.get("toggle") == "yes",
        "attach_menu": flags.get("attach-menu") == "yes",
        "picker": flags.get("picker") == "yes",
    }


def dom_drift(run: Path, fixtures: Path = DOM_FIXTURES) -> dict[str, list[str]]:
    """Per recorded page under ``run/dom``, the facts that differ from the
    committed fixture of the same name (``chatgpt_client.snapshot_drift``);
    pages without drift or without a fixture are left out."""
    import chatgpt_client as cc

    out: dict[str, list[str]] = {}
    for fresh in sorted((run / "dom").glob("*.json")) if (run / "dom").is_dir() else []:
        committed = fixtures / fresh.name
        if not committed.is_file():
            continue
        try:
            drift = cc.snapshot_drift(
                json.loads(fresh.read_text(encoding="utf-8")),
                json.loads(committed.read_text(encoding="utf-8")),
            )
        except (OSError, ValueError, TypeError):
            continue
        if drift:
            out[fresh.stem] = drift
    return out


def send_body_drift(run: Path) -> list[str]:
    """The breaking lines of the recorded send body against the committed
    shape (``api_shapes.compare_send_body``), or []."""
    import api_shapes

    fresh = run / "dom" / f"{api_shapes.SEND_BODY_NAME}{api_shapes.SHAPE_SUFFIX}"
    committed = api_shapes.load_send_body_shape()
    if not fresh.is_file() or committed is None:
        return []
    try:
        shape = json.loads(fresh.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return api_shapes.compare_send_body(shape, committed)["breaking"]


def read_lines(path: str | None) -> list[str]:
    """The non-empty lines of ``path``, or [] (``--drift-lines``)."""
    if not path:
        return []
    try:
        return [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return []


def gather(run: Path, summary: str = "", drift_lines: list[str] | None = None) -> dict:
    """Everything the rules read, from the run directory and the caller."""
    health = read_health(run)
    failed = failed_checks(health)
    composer = next(
        (
            c
            for c in failed
            if c.get("name") == "composer" and c.get("group") == "browser"
        ),
        None,
    )
    composer_detail = str(composer.get("detail") or "") if composer else ""
    details = [str(c.get("detail") or "") for c in failed]
    lines = list(drift_lines or [])
    return {
        "result": read_result(run),
        "summary": summary or read_result(run).get("summary", ""),
        "failed": [
            {"group": c.get("group"), "name": c.get("name"), "state": c.get("state")}
            for c in failed
        ],
        "composer_reason": composer_reason(composer_detail) if composer else "",
        "page_facts": page_facts(composer_detail),
        "http": sorted(
            {m.group(0).lower() for d in details for m in TRANSIENT_HTTP_RE.finditer(d)}
        ),
        "api_shapes_warn": any(
            c.get("name") == "api shapes" and c.get("state") == "warn" for c in failed
        ),
        "session_block": any(
            c.get("name") == "session token" and c.get("state") == "block"
            for c in failed
        ),
        "dom_drift": dom_drift(run),
        "send_body_breaking": send_body_drift(run)
        + [line for line in lines if line.startswith("send_body:")],
        "drift_lines": [line for line in lines if not line.startswith("send_body:")],
    }


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def classify(evidence: dict[str, Any]) -> tuple[str, list[str]]:
    """The class and the reasons, first rule that fits."""
    reasons: list[str] = []
    facts = evidence["page_facts"]
    reason = evidence["composer_reason"]
    if evidence["dom_drift"] or reason == "drift" or evidence["drift_lines"]:
        where = (
            ", ".join(f"{n}: {', '.join(k)}" for n, k in evidence["dom_drift"].items())
            or "; ".join(evidence["drift_lines"])
            or "the composer check"
        )
        reasons.append(f"a recorded page differs from its fixture ({where})")
        return "page-change", reasons
    if evidence["api_shapes_warn"] or evidence["send_body_breaking"]:
        reasons.append(
            "a shape the skill reads changed: "
            + (
                "; ".join(evidence["send_body_breaking"])
                or "the api shapes check warns"
            )
        )
        return "api-change", reasons
    if facts.get("login") or evidence["session_block"]:
        reasons.append("the page shows a login route or the session token check blocks")
        return "session", reasons
    if reason == "challenge":
        reasons.append("Cloudflare's challenge widget is on the page")
        return "known-transient", reasons
    if facts and not facts.get("challenge") and not facts.get("login"):
        reasons.append(
            "the composer did not appear on a page with no challenge and no login "
            f"route ({facts['composers']} composer(s), {facts['hidden']} hidden, "
            f"toggle={'yes' if facts['toggle'] else 'no'}, "
            f"attach-menu={'yes' if facts['attach_menu'] else 'no'}, "
            f"picker={'yes' if facts['picker'] else 'no'})"
        )
        return "page-change", reasons
    if reason in ("refused", "cooldown", "busy") or evidence["http"]:
        reasons.append(
            "the read path or the page's own requests were refused "
            f"({', '.join(evidence['http']) or reason})"
        )
        return "known-transient", reasons
    if reason == "bounced":
        reasons.append("the tab was sent off chatgpt.com (an extension in the profile)")
        return "host", reasons
    failed_names = {c["name"] for c in evidence["failed"]}
    if failed_names and failed_names <= HOST_CHECKS:
        reasons.append(f"only host checks failed ({', '.join(sorted(failed_names))})")
        return "host", reasons
    if reason == "logged-out-or-challenged":
        reasons.append(
            "the composer did not appear and the page gave no facts (a client "
            "from before 2026-10-01 evening, or a page that did not answer)"
        )
        return "unknown", reasons
    reasons.append(
        "no rule fits: "
        + (evidence["summary"] or ", ".join(sorted(failed_names)) or "-")
    )
    return "unknown", reasons


def signature(evidence: dict[str, Any], cls: str) -> str:
    """A stable name for this failure: no timestamps, ids or counts that
    churn, so the same failure tomorrow carries the same signature."""
    facts = evidence["page_facts"]
    flags = (
        "".join(
            k[0] if facts.get(k) else "-"
            for k in ("challenge", "login", "toggle", "attach_menu", "picker")
        )
        + f"c{facts.get('composers', 0)}h{facts.get('hidden', 0)}"
        if facts
        else "-"
    )
    checks = ";".join(sorted(f"{c['name']}:{c['state']}" for c in evidence["failed"]))
    drift = ";".join(
        f"{n}={'+'.join(k)}" for n, k in sorted(evidence["dom_drift"].items())
    ) or ";".join(evidence["drift_lines"])
    shapes = ";".join(evidence["send_body_breaking"]) + (
        ";api-shapes" if evidence["api_shapes_warn"] else ""
    )
    return (
        f"{cls}|checks={checks or '-'}|composer={evidence['composer_reason'] or '-'}"
        f"|facts={flags}|drift={drift or '-'}|shapes={shapes or '-'}"
        f"|http={','.join(evidence['http']) or '-'}"
    )


def repro_summary(path: str | None) -> dict[str, Any]:
    """What the reproduction found (preflight's ``--json``): whether it ran,
    its composer check's state and detail, and its verdict."""
    if not path:
        return {"ran": False}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ran": False}
    composer = next(
        (
            c
            for c in doc.get("checks") or []
            if isinstance(c, dict) and c.get("name") == "composer"
        ),
        None,
    )
    return {
        "ran": True,
        "verdict": doc.get("verdict"),
        "composer": composer.get("state") if composer else None,
        "detail": str(composer.get("detail") or "")[:300] if composer else "",
        "reason": composer_reason(str(composer.get("detail") or ""))
        if composer
        else "",
    }


def decide(
    cls: str, streak: int, repro: dict[str, Any], threshold: int = REPEAT_THRESHOLD
) -> tuple[str, list[str]]:
    """The decision and why, from the class, the streak and the reproduction."""
    reasons: list[str] = []
    if streak >= threshold:
        reasons.append(
            f"the same failure on {streak} failing runs in a row (by signature, "
            "or the browser composer check blocking every time)"
        )
        return "escalate-repeat", reasons
    if repro.get("ran"):
        if repro.get("composer") == "ok":
            reasons.append(
                "the reproduction found the composer: the failure did not repeat"
            )
            return "digest", reasons
        if repro.get("reason") == "drift":
            reasons.append(
                "the reproduction found the composer, but its page differs from "
                "the fixture in a fact the selectors read: a page change"
            )
            return "escalate", reasons
        if cls in ("page-change", "unknown"):
            how = repro.get("reason") or "block"
            reasons.append(f"the reproduction failed the same way ({how})")
            return "escalate", reasons
        reasons.append(
            f"the reproduction failed too ({repro.get('reason') or 'block'}); "
            "a known transient repeats until it repeats enough"
        )
    if cls in ESCALATE_CLASSES:
        reasons.append(f"class {cls} is investigated at once")
        return "escalate", reasons
    reasons.append(f"class {cls}, first occurrence(s): the digest carries it")
    return "digest", reasons


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def load_registry(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"signatures": {}, "last": None}
    if not isinstance(data, dict):
        return {"signatures": {}, "last": None}
    data.setdefault("signatures", {})
    data.setdefault("last", None)
    return data


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(registry, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def record(
    registry: dict[str, Any],
    run_id: str,
    sig: str,
    cls: str,
    now: float,
    browser_failed: bool = False,
) -> tuple[int, int]:
    """Note this failing run under ``sig``; return ``(streak,
    browser_streak)``: consecutive failing runs with exactly this signature,
    and consecutive failing runs whose browser composer check blocked,
    whatever else failed beside it (a 403 on the read path one morning and
    not the next must not restart the count). An ``--ok`` run resets both."""
    last = registry.get("last") or {}
    streak = int(last.get("streak") or 0) + 1 if last.get("signature") == sig else 1
    browser_streak = int(last.get("browser_streak") or 0) + 1 if browser_failed else 0
    entry = registry["signatures"].setdefault(
        sig, {"class": cls, "first": now, "count": 0, "runs": []}
    )
    entry.update({"class": cls, "last": now, "count": int(entry.get("count", 0)) + 1})
    entry["runs"] = ([*entry.get("runs", []), run_id])[-5:]
    registry["last"] = {
        "run_id": run_id,
        "signature": sig,
        "streak": streak,
        "browser_streak": browser_streak,
        "at": now,
    }
    return streak, browser_streak


def record_ok(registry: dict[str, Any], run_id: str, now: float) -> None:
    registry["last"] = {
        "run_id": run_id,
        "signature": "ok",
        "streak": 0,
        "browser_streak": 0,
        "at": now,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def triage(
    run: Path,
    *,
    repro_path: str | None = None,
    summary: str = "",
    drift_lines: list[str] | None = None,
    registry_path: Path = DEFAULT_REGISTRY,
    threshold: int = REPEAT_THRESHOLD,
    dry_run: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """The whole decision for one run, registry updated unless ``dry_run``."""
    now = time.time() if now is None else now
    evidence = gather(run, summary, drift_lines)
    cls, why = classify(evidence)
    sig = signature(evidence, cls)
    registry = load_registry(registry_path)
    run_id = evidence["result"].get("run_id") or run.name
    browser_failed = evidence["composer_reason"] not in ("", "ok")
    streak, browser_streak = record(registry, run_id, sig, cls, now, browser_failed)
    if not dry_run:
        save_registry(registry_path, registry)
    repro = repro_summary(repro_path)
    decision, because = decide(cls, max(streak, browser_streak), repro, threshold)
    return {
        "run_id": run_id,
        "class": cls,
        "signature": sig,
        "streak": streak,
        "browser_streak": browser_streak,
        "decision": decision,
        "reasons": why + because,
        "repro": repro,
        "evidence": {k: v for k, v in evidence.items() if k != "result"},
        "triaged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }


def render(record_: dict[str, Any]) -> str:
    repro = record_["repro"]
    repro_line = (
        f"reproduction: composer {repro.get('composer') or '?'}"
        + (f" ({repro.get('reason')})" if repro.get("reason") else "")
        if repro.get("ran")
        else "reproduction: not run"
    )
    return "\n".join(
        [
            f"Triage: {record_['decision']} -- class {record_['class']}, "
            f"streak {record_['streak']} (browser {record_['browser_streak']})",
            f"  signature: {record_['signature']}",
            f"  {repro_line}",
            *(f"  - {reason}" for reason in record_["reasons"]),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", metavar="DIR", help="the failed run's evidence directory")
    ap.add_argument(
        "--ok", metavar="RUN_ID", help="record a clean run; ends every streak"
    )
    ap.add_argument(
        "--repro", metavar="JSON", help="preflight --json of the reproduction"
    )
    ap.add_argument("--summary", default="", help="the wrapper's one-line summary")
    ap.add_argument(
        "--drift-lines", metavar="FILE", help="the weekly wrapper's drift lines"
    )
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), metavar="PATH")
    ap.add_argument("--json", dest="json_path", metavar="PATH", help="write the record")
    ap.add_argument("--repeat-threshold", type=int, default=REPEAT_THRESHOLD)
    ap.add_argument("--dry-run", action="store_true", help="do not write the registry")
    args = ap.parse_args(argv)

    registry_path = Path(args.registry).expanduser()
    if args.ok:
        registry = load_registry(registry_path)
        record_ok(registry, args.ok, time.time())
        if not args.dry_run:
            save_registry(registry_path, registry)
        print(f"Triage: ok run {args.ok} recorded; streaks reset")
        return 0
    if not args.run:
        print("give --run DIR or --ok RUN_ID")
        return 1
    run = Path(args.run).expanduser()
    if not run.is_dir():
        print(f"not a run directory: {run}")
        return 1
    record_ = triage(
        run,
        repro_path=args.repro,
        summary=args.summary,
        drift_lines=read_lines(args.drift_lines),
        registry_path=registry_path,
        threshold=args.repeat_threshold,
        dry_run=args.dry_run,
    )
    target = Path(args.json_path) if args.json_path else run / "triage.json"
    try:
        target.write_text(json.dumps(record_, indent=1, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"triage record not written to {target}: {exc}")
    print(render(record_))
    return 0 if record_["decision"] == "digest" else 2


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
