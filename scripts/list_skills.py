#!/usr/bin/env python3
"""List the skills and apps installed on this ChatGPT account. Read-only.

    list_skills.py [--apps] [--expect NAME ...] [--json PATH] [--browser chrome]

Skills -- the user's own uploaded skills, "hazelnuts" in the backend, never
in this command's own output -- come from:

    GET /backend-api/hazelnuts?include_permissions=true&scope=installed
    -> {"hazelnuts": [...]}

Apps and connectors, with ``--apps``, come from:

    GET /backend-api/ps/plugins/installed?limit=1000
    -> {"plugins": [...], "pagination": {"limit", "next_page_token"}}

Both captured 2026-09-21 against this account
(``references/endpoint-discovery.md``; both rows already existed there
since 2026-09-19 and are enriched by this capture). 4 skills and 16 apps
on this account that day.

Skill fields printed: name, on (``enabled``), versions
(``default_version_no``/``latest_version_no``), safety
(``safety_check_status``; seen: "blocked", "unchecked"), risk
(``safety_scan.risk_score``), labels (``safety_scan.labels``, joined; seen:
``["safeguard_evasion"]`` on one skill), files (a count -- ``files`` is a
dict of path -> ``{"start", "length"}`` byte ranges, never content, and
this command never fetches file contents either), updated (from
``updated_at``, falling back to ``last_updated_at``), checksum (the first
12 characters of ``check_sum_hash``). A skill also carries
``base_sediment_id``, ``brand_color``, ``creator_id``, ``creator_name``,
``description``, ``icon_small``, ``iconography``, ``id``, ``in_my_list``,
``permissions`` (``can_read``, ``can_write``, ``can_export``,
``can_share``, ``can_share_workspace``, ``can_enable_share``,
``can_delete``), ``sample_prompts``, ``sediment_id``, ``short_description``,
``surfaces``; none of those is in the table, but every field except
``files`` and ``sample_prompts`` is kept by ``--json``.

**What is verified and what is not.** The meaning of ``safety_check_status``
and of "default" versus "latest" version is not verified in any way here:
both print exactly as the server sends them, never interpreted or judged.
"blocked" is not assumed to mean disabled -- the account's one "blocked"
skill (``research-pipeline``) was ``enabled: true`` the same day -- and
``["safeguard_evasion"]`` is not assumed to mean anything beyond the label
ChatGPT itself attached.

``--apps`` adds a second table from ``plugins``: name
(``release.display_name``), creator (``creator_name``, "OpenAI" or the
user), scope (``GLOBAL``/``USER``), status (``status``, seen: "ENABLED"),
version (``release.version``), installed (date, from ``installed_at``). A
plugin also carries ``id``, ``connector_id``, ``canonical_app_id``,
``created_at``, ``enabled``, ``installation_policy``,
``authentication_policy``, ``disabled_reason``, ``disabled_skill_names``,
and ``release`` itself nests ``description``, ``interface`` and ``skills``;
none of that is in the table, and ``--json`` keeps every plugin field
unchanged. ``pagination.next_page_token`` was null when this was measured;
a non-null token prints "more exist" and is not followed here, because the
paging parameter is unknown -- the same caution ``list_automations.py``
takes with its own cursor.

``--expect NAME`` (repeatable) checks, after listing, that a skill with
this exact ``name`` exists and is enabled -- the check the daily health
job will use later -- and prints one report line per name, naming what
failed when it did.

``--json`` writes ``{"skills": [...], "apps": [...] with --apps}``. Every
skill record drops ``files`` (replaced by its count) and ``sample_prompts``
outright: the first is 46 file paths of the user's own skill, the second
the user's own prompt text, and neither belongs in a file this command was
not asked to hold.

Exit 0 when listed (and, with ``--apps``, the apps read too) and every
``--expect`` was met. Exit 1 when a read failed, or any ``--expect`` was
missing or disabled -- both name what failed. Exit 2 for a malformed
invocation (argparse's own default; this command adds no argument
validation of its own). Plain HTTP, GET only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session, table

HAZELNUTS = "/backend-api/hazelnuts?include_permissions=true&scope=installed"
PLUGINS_INSTALLED = "/backend-api/ps/plugins/installed?limit=1000"

SKILL_HEADERS = (
    "name",
    "on",
    "versions",
    "safety",
    "risk",
    "labels",
    "files",
    "updated",
    "checksum",
)
APP_HEADERS = ("name", "creator", "scope", "status", "version", "installed")


def files_count(item: dict[str, Any]) -> int:
    """How many paths ``files`` lists, or 0 when it is missing or not a
    dict -- never the paths themselves (module docstring)."""
    files = item.get("files")
    return len(files) if isinstance(files, dict) else 0


def versions_label(item: dict[str, Any]) -> str:
    """ "default/latest" version numbers, printed verbatim and unjudged."""
    default = item.get("default_version_no")
    latest = item.get("latest_version_no")
    default_text = "?" if default is None else str(default)
    latest_text = "?" if latest is None else str(latest)
    return f"{default_text}/{latest_text}"


def risk_label(item: dict[str, Any]) -> str:
    """``safety_scan.risk_score`` as a string, or "-" when absent."""
    risk = (item.get("safety_scan") or {}).get("risk_score")
    return str(risk) if risk is not None else "-"


def labels_label(item: dict[str, Any]) -> str:
    """``safety_scan.labels``, comma-joined; "" when there are none."""
    labels = (item.get("safety_scan") or {}).get("labels") or []
    return ",".join(str(label) for label in labels)


def checksum_label(item: dict[str, Any]) -> str:
    """The first 12 characters of ``check_sum_hash``."""
    return str(item.get("check_sum_hash") or "")[:12]


def updated_label(item: dict[str, Any]) -> str:
    """A "YYYY-MM-DD" date from ``updated_at``, falling back to
    ``last_updated_at`` (module docstring: which of the two is
    authoritative is not verified)."""
    value = item.get("updated_at") or item.get("last_updated_at") or ""
    return str(value)[:10]


def on_label(enabled: Any) -> str:
    """ "yes" / "no" for the "on" cell."""
    return "yes" if enabled else "no"


def skill_row(item: dict[str, Any]) -> tuple[str, ...]:
    """One skills-table row, in ``SKILL_HEADERS`` column order."""
    return (
        str(item.get("name") or "(unnamed)"),
        on_label(item.get("enabled")),
        versions_label(item),
        str(item.get("safety_check_status") or "-"),
        risk_label(item),
        labels_label(item),
        str(files_count(item)),
        updated_label(item),
        checksum_label(item),
    )


def app_row(item: dict[str, Any]) -> tuple[str, ...]:
    """One apps-table row, in ``APP_HEADERS`` column order."""
    release = item.get("release") or {}
    return (
        str(release.get("display_name") or item.get("name") or "(unnamed)"),
        str(item.get("creator_name") or "-"),
        str(item.get("scope") or "-"),
        str(item.get("status") or "-"),
        str(release.get("version") or "-"),
        str(item.get("installed_at") or "")[:10],
    )


def find_skill(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The hazelnut item whose ``name`` is exactly ``name``, or None."""
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def expectation_line(name: str, item: dict[str, Any] | None) -> tuple[str, bool]:
    """One ``--expect`` report line, and whether that expectation is met.

    Met means a hazelnut named ``name`` exists and its ``enabled`` is
    true; ``item`` is that raw hazelnut, or None when no skill has this
    name.
    """
    if item is None:
        return f"expect {name}: NOT installed", False
    enabled = bool(item.get("enabled"))
    state = "enabled" if enabled else "disabled"
    line = (
        f"expect {name}: installed, {state}, "
        f"safety {item.get('safety_check_status') or '-'}, "
        f"versions {versions_label(item)}"
    )
    return line, enabled


def redacted_skill(item: dict[str, Any]) -> dict[str, Any]:
    """One hazelnut item for ``--json``: every field verbatim except
    ``files`` (replaced by its count) and ``sample_prompts`` (dropped) --
    module docstring, and the HARD RULE against printing a skill's file
    list or the user's own prompt text."""
    out = {k: v for k, v in item.items() if k not in ("files", "sample_prompts")}
    out["files"] = files_count(item)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apps", action="store_true", help="also list installed apps and connectors"
    )
    ap.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="NAME",
        help="check a skill with this name is installed and enabled (repeatable)",
    )
    ap.add_argument(
        "--json", default="", metavar="PATH", help="write the raw items here"
    )
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    session = open_session(args.browser)

    status, body = session.session.call(HAZELNUTS)
    if status != 200 or not isinstance(body, dict):
        print(f"hazelnuts read failed: HTTP {status} {str(body)[:200]}")
        return 1
    skills = list(body.get("hazelnuts") or [])

    print(table([skill_row(s) for s in skills], SKILL_HEADERS))
    print(f"\n{len(skills)} skill(s)")

    apps: list[dict[str, Any]] = []
    apps_failed = False
    if args.apps:
        status, body = session.session.call(PLUGINS_INSTALLED)
        if status != 200 or not isinstance(body, dict):
            print(f"\nplugins read failed: HTTP {status} {str(body)[:200]}")
            apps_failed = True
        else:
            apps = list(body.get("plugins") or [])
            print()
            print(table([app_row(a) for a in apps], APP_HEADERS))
            print(f"\n{len(apps)} app(s)")
            if (body.get("pagination") or {}).get("next_page_token"):
                print("more exist (next_page_token returned, not followed)")

    unmet = 0
    if args.expect:
        print()
        for name in args.expect:
            line, met = expectation_line(name, find_skill(skills, name))
            print(line)
            if not met:
                unmet += 1

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        doc: dict[str, Any] = {"skills": [redacted_skill(s) for s in skills]}
        if args.apps:
            doc["apps"] = apps
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwritten to {out}")

    return 1 if (apps_failed or unmet) else 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
