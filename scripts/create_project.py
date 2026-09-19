#!/usr/bin/env python3
"""Create a ChatGPT project over HTTP, and read back what it became.

    create_project.py NAME [--instructions FILE | --instructions-text TEXT]
        [--memory project-only|default] [--dry-run]

The browser flow captured the endpoint on 2026-09-16, driving the sidebar's
"New project" dialog and recording the call it made. The request body itself
was captured 2026-09-20 on a throwaway project, `rp-test-sandbox-2`
(references/endpoint-discovery.md, "Captured 2026-09-20, project create and
delete"): `POST /backend-api/projects` with `instructions`, `name` and
`memory_scope`. With the body known, this command creates over plain HTTP
and never imports a browser: no Playwright, no browser slot, no Xvfb.

The Create dialog offers the same two-way memory choice `project_settings.py`
writes later: "project-only" -> `memory_scope: "project_v2"`, "default" ->
`"global"`. Omitting --memory sends what the dialog itself sends by default,
`"unset"`.

Default: create it. Prints the POST body, sends it, then reads
`gizmos/<new-id>` back and checks that the instructions and memory scope it
reports match what was asked. --dry-run prints the same body and sends
nothing.

Exit 0 on --dry-run, and on a real run when the create answered 200 with a
`g-p-...` id and the read-back matched what was asked. Exit 1 when the
create does not answer 200 with such an id, the read-back cannot be
fetched, or it disagrees with what was asked. Exit 2 when NAME is empty, or
both --instructions and --instructions-text were given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session
from list_projects import project_of

PROJECTS = "/backend-api/projects"
GIZMO = "/backend-api/gizmos/{id}"

# The Create dialog's two-way memory choice, the same values
# project_settings.py writes later (references/endpoint-discovery.md,
# "Captured 2026-09-20"). Its own default, sent when nothing was chosen, is
# "unset".
MEMORY_SCOPES = {"project-only": "project_v2", "default": "global"}
DEFAULT_MEMORY_SCOPE = "unset"


def create_body(name: str, instructions: str, memory_scope: str) -> dict[str, Any]:
    """The POST body the Create dialog sends (Captured 2026-09-20)."""
    return {"instructions": instructions, "name": name, "memory_scope": memory_scope}


def created_id(resp: Any) -> str:
    """The new project's ``g-p-...`` id from a create response, or "" if none.

    Shaped like the capture: ``{"resource": {"gizmo": {"id": "g-p-...",
    ...}}, "error": None, "sharing_targets": [...]}``.
    """
    if not isinstance(resp, dict):
        return ""
    gizmo = (resp.get("resource") or {}).get("gizmo") or {}
    new_id = str(gizmo.get("id") or "")
    return new_id if new_id.startswith("g-p-") else ""


def verify(after: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
    """Mismatches between what creation asked for and the gizmos/<id> read-back."""
    return [
        f"{field}: wanted {expected!r}, read back {after.get(field)!r}"
        for field, expected in wanted.items()
        if after.get(field) != expected
    ]


def render_created(project: dict[str, Any]) -> str:
    """The id, URL, memory scope and instructions length of a new project."""
    return (
        f"{project['id']}\n"
        f"open at: {project['url']}\n"
        f"memory scope: {project['memory_scope'] or '-'}\n"
        f"instructions: {len(project['instructions'])} chars"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", help="the new project's name")
    ap.add_argument(
        "--instructions",
        default=None,
        metavar="FILE",
        help="read the new project's instructions from this file",
    )
    ap.add_argument(
        "--instructions-text",
        default=None,
        metavar="TEXT",
        help="the new project's instructions, given directly",
    )
    ap.add_argument(
        "--memory",
        choices=sorted(MEMORY_SCOPES),
        default=None,
        help="project-only or default memory; omitted sends the dialog's own default",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the body, send nothing"
    )
    args = ap.parse_args(argv)

    if not args.name.strip():
        print("refusing to create a project with an empty name")
        return 2

    given = [
        flag
        for flag, present in (
            ("--instructions", args.instructions is not None),
            ("--instructions-text", args.instructions_text is not None),
        )
        if present
    ]
    if len(given) > 1:
        print(f"choose only one of {' / '.join(given)}")
        return 2

    if args.instructions is not None:
        try:
            instructions = Path(args.instructions).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"could not read {args.instructions}: {exc}")
            return 2
    elif args.instructions_text is not None:
        instructions = args.instructions_text
    else:
        instructions = ""

    memory_scope = MEMORY_SCOPES[args.memory] if args.memory else DEFAULT_MEMORY_SCOPE
    body = create_body(args.name, instructions, memory_scope)

    print("POST body:")
    print(json.dumps(body, indent=2, ensure_ascii=False))

    if args.dry_run:
        print()
        print("dry run: nothing created")
        return 0

    session = open_session()
    status, resp = session.session.call(PROJECTS, method="POST", payload=body)
    new_id = created_id(resp) if status == 200 else ""
    if not new_id:
        print(f"\ncreate failed: HTTP {status} {str(resp)[:200]}")
        return 1

    read_status, gizmo_payload = session.session.call(GIZMO.format(id=new_id))
    if read_status != 200 or not isinstance(gizmo_payload, dict):
        print(f"\ncreated {new_id} but could not read it back: HTTP {read_status}")
        return 1

    after = project_of(gizmo_payload)
    wanted = {"instructions": instructions, "memory_scope": memory_scope}
    mismatches = verify(after, wanted)

    print()
    print(render_created(after))

    if mismatches:
        print("\nread-back disagrees:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
