#!/usr/bin/env python3
"""Delete exactly one uploaded Personal Skill from this ChatGPT account.

    delete_skill.py NAME [--id SKILL_ID] [--confirm] [--dry-run]

Resolve NAME exactly in the installed skills list. Refuse ambiguous or
malformed identities and require readable/deletable permissions. --id is
an additional exact identity guard, never an alternative selector.
Names must be 1-200 printable characters without URLs; ids must be 1-200
ASCII letters, digits, underscores, or hyphens. Unknown metadata prints ?.
Dry run is the default; --dry-run overrides --confirm. Every DELETE attempt
is followed by exactly one installed-list GET, even if DELETE fails.
Success requires HTTP 200 from DELETE and neither id nor name on read-back.
No apps, connectors, downloads, uploads, or backups are involved.

Exit 0: dry run or verified deletion. Exit 1: session, read, DELETE, or
verification failure (including malformed inventory). Exit 2: invalid
arguments or a local safety refusal. Response bodies and exception text
are never printed; only bounded identity and selected metadata are shown.
"""

from __future__ import annotations

import argparse
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from _common import ensure_venv, open_session, table
from list_skills import HAZELNUTS, on_label, versions_label

SKILL = "/backend-api/hazelnuts/{id}"


def valid_name(value: Any) -> bool:
    """Bound the audit identity without truncating or normalizing a match."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and bool(value.strip())
        and value.isprintable()
        and "://" not in value
    )


def valid_id(value: Any) -> bool:
    """Accept one opaque path segment, never a URL or an escaped path."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) is not None
    )


def installed(
    session: Any, *, retries: int | None = None
) -> list[dict[str, Any]] | None:
    """Fail closed on unreadable identities; absence must be provable."""
    try:
        status, body = session.session.call(HAZELNUTS, retries=retries)
    except (Exception, SystemExit):
        print("installed skills read failed")
        return None
    if status != 200:
        print(f"installed skills read failed: HTTP {status}")
        return None
    items = body.get("hazelnuts") if isinstance(body, dict) else None
    if not isinstance(items, list) or any(
        not isinstance(item, dict)
        or not valid_name(item.get("name"))
        or not valid_id(item.get("id"))
        for item in items
    ):
        print("installed skills read failed: malformed inventory")
        return None
    return items


def audit_row(item: dict[str, Any]) -> tuple[str, ...]:
    """Allowlist bounded metadata; never print nested records or URLs."""
    versions = {
        key: str(item[key])
        for key in ("default_version_no", "latest_version_no")
        if re.fullmatch(r"[0-9]{1,20}", str(item.get(key)))
    }
    safety = item.get("safety_check_status")
    if not isinstance(safety, str) or not re.fullmatch(r"[A-Za-z_-]{1,40}", safety):
        safety = "?"
    enabled = item.get("enabled")
    return (
        repr(item["name"]),
        item["id"],
        on_label(enabled) if isinstance(enabled, bool) else "?",
        versions_label(versions),
        safety,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("name", metavar="NAME", help="exact installed Personal Skill name")
    ap.add_argument("--id", metavar="SKILL_ID", help="additional exact id guard")
    ap.add_argument("--confirm", action="store_true", help="really delete one skill")
    ap.add_argument(
        "--dry-run", action="store_true", help="preview, even with --confirm"
    )
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)
    if not valid_name(args.name) or (args.id is not None and not valid_id(args.id)):
        print(
            "refusing: invalid name or id (expected a bounded name and one id segment)"
        )
        return 2

    # The shared bootstrap reports raw exception text. Suppress that output
    # here so authentication failures cannot expose credentials or signed URLs.
    try:
        with (
            open(os.devnull, "w") as sink,
            redirect_stdout(sink),
            redirect_stderr(sink),
        ):
            session = open_session(args.browser)
    except (Exception, SystemExit):
        print("could not open a ChatGPT session")
        return 1

    items = installed(session)
    if items is None:
        return 1
    matches = [item for item in items if item["name"] == args.name]
    if len(matches) != 1:
        print("refusing: expected exactly one installed skill with that exact name")
        return 2
    target = matches[0]
    skill_id = target["id"]
    if sum(item["id"] == skill_id for item in items) != 1:
        print("refusing: ambiguous skill id in installed inventory")
        return 2
    if args.id is not None and args.id != skill_id:
        print("refusing: --id does not match the exact-name skill")
        return 2
    permissions = target.get("permissions")
    if (
        not isinstance(permissions, dict)
        or permissions.get("can_read") is not True
        or permissions.get("can_delete") is not True
    ):
        print("refusing: skill lacks explicit read and delete permissions")
        return 2

    print(
        table([audit_row(target)], ("name", "id", "on", "versions", "safety")),
        flush=True,
    )
    if args.dry_run or not args.confirm:
        print("dry run: would DELETE this Personal Skill; add --confirm to delete")
        return 0

    status = None
    try:
        status, _body = session.session.call(
            SKILL.format(id=skill_id), method="DELETE", retries=1
        )
    except (Exception, SystemExit):
        print("DELETE request failed")
    else:
        if status != 200:
            print(f"DELETE failed: HTTP {status}")

    # A failed response does not establish whether the server applied DELETE.
    # Always observe once, without replaying either request.
    after = installed(session, retries=1)
    if after is None:
        print("deletion could not be verified: final state could not be established")
        return 1
    if any(item["id"] == skill_id or item["name"] == args.name for item in after):
        print("deletion could not be verified: skill id or name remains installed")
        return 1
    if status != 200:
        print(
            "target is absent on read-back but the DELETE response was not successful"
        )
        return 1
    print("deleted and verified: skill id and name are absent from installed skills")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main())
