#!/usr/bin/env python3
"""Set a project's instructions and memory scope.

    project_settings.py g-p-<id> [--apply]
        [--instructions FILE | --instructions-text TEXT | --clear-instructions]
        [--memory project-only|default]

Project settings (a project's "..." menu) always saves its whole record in
one PATCH, never a single field: name, instructions, emoji, theme and, when
memory changed, memory_scope (references/endpoint-discovery.md, "Captured
2026-09-20"). This command reads the project first with GET gizmos/<id>,
then resends that body with only the requested field(s) changed, matching
what the page itself does.

Dry run by default: prints the project's name, its current instructions
length and memory scope, then the exact body a send would use, and never
calls the PATCH. --apply sends it, reads gizmos/<id> again, and checks
that every field the caller asked to change reads back as requested --
including, for a memory change, the memory_enabled the capture says
follows from it.

Exit 0 on a dry run, and on --apply when the PATCH answered 200 and the
read-back agreed. Exit 1 when the PATCH does not answer 200, or a changed
field's read-back disagrees with what was sent (both values are printed).
Exit 2 when the id is not a project id, when more than one of
--instructions / --instructions-text / --clear-instructions was given, or
when nothing was asked to change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session
from list_projects import project_of

GIZMO = "/backend-api/gizmos/{id}"
PROJECT = "/backend-api/projects/{id}"

# Project settings' two-way memory choice; both directions captured
# 2026-09-20 (references/endpoint-discovery.md, "Captured 2026-09-20") in
# the same PATCH the instructions use: the body's memory_scope field.
MEMORY_SCOPES = {"project-only": "project_v2", "default": "global"}

# What a PATCH's memory_scope reads back as in gizmos/<id>, captured the
# same day: "project_v2" always pairs with memory_enabled false, "global"
# always with true.
MEMORY_ENABLED_FOR_SCOPE = {"project_v2": False, "global": True}


def patch_body(
    gizmo_payload: dict[str, Any],
    instructions: str | None = None,
    memory: str | None = None,
) -> dict[str, Any]:
    """The PATCH body: the whole record, with only what changed different.

    The page always sends name, instructions, emoji and theme together --
    never one field alone -- and adds memory_scope only when memory itself
    changed. Name, emoji and theme are read from ``gizmo.display`` and
    passed through exactly as they came, so a caller who only changes
    instructions can never accidentally clear them.
    """
    gizmo = gizmo_payload.get("gizmo") or {}
    display = gizmo.get("display") or {}
    body: dict[str, Any] = {
        "name": display.get("name"),
        "instructions": (
            instructions if instructions is not None else gizmo.get("instructions")
        ),
        "emoji": display.get("emoji"),
        "theme": display.get("theme"),
    }
    if memory is not None:
        body["memory_scope"] = memory
    return body


def verify(
    before: dict[str, Any], after: dict[str, Any], wanted: dict[str, Any]
) -> list[str]:
    """Mismatches between what --apply asked for and the next gizmos/<id> read.

    ``before``, ``after`` and ``wanted`` share one flat shape --
    ``list_projects.project_of``'s -- so this never has to know about the
    raw gizmo/display nesting. ``wanted`` carries only the field(s) the
    caller actually asked to change (``instructions``, ``memory_scope``);
    ``before`` is carried through only to make a mismatch legible, showing
    what the field was and not only what it should have become.

    A changed memory_scope carries one more, derived check: the capture
    says a PATCH's memory_scope always reads back paired with one
    memory_enabled value, never the other. Disagreeing there means the
    isolation (or its opposite) the caller asked for did not actually take
    effect, even when memory_scope itself matches.
    """
    mismatches = [
        f"{field}: wanted {expected!r}, read back {after.get(field)!r} "
        f"(was {before.get(field)!r})"
        for field, expected in wanted.items()
        if after.get(field) != expected
    ]
    if "memory_scope" in wanted:
        expected_enabled = MEMORY_ENABLED_FOR_SCOPE.get(wanted["memory_scope"])
        actual_enabled = after.get("memory_enabled")
        if actual_enabled != expected_enabled:
            mismatches.append(
                f"memory_enabled: wanted {expected_enabled!r} (implied by "
                f"memory_scope {wanted['memory_scope']!r}), "
                f"read back {actual_enabled!r}"
            )
    return mismatches


def render_current(project: dict[str, Any]) -> str:
    """The project's name, current instructions length and memory scope."""
    return (
        f"{project['id']}  {project['name']}\n"
        f"instructions: {len(project['instructions'])} chars, "
        f"memory scope {project['memory_scope'] or '-'}"
    )


def render_change(
    before: dict[str, Any], after: dict[str, Any], wanted: dict[str, Any]
) -> str:
    """One line per field --apply asked to change: its before and after value."""
    return "\n".join(
        f"{field}: {before.get(field)!r} -> {after.get(field)!r}" for field in wanted
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_id", metavar="g-p-ID", help="the project to change")
    ap.add_argument(
        "--instructions",
        default=None,
        metavar="FILE",
        help="read the new instructions from this file",
    )
    ap.add_argument(
        "--instructions-text",
        default=None,
        metavar="TEXT",
        help="the new instructions, given directly",
    )
    ap.add_argument(
        "--clear-instructions",
        action="store_true",
        help="set the instructions to empty",
    )
    ap.add_argument(
        "--memory",
        choices=sorted(MEMORY_SCOPES),
        default=None,
        help="project-only or default memory",
    )
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    args = ap.parse_args(argv)

    if not args.project_id.startswith("g-p-"):
        print(f"{args.project_id!r} is not a project id (it must start with 'g-p-')")
        return 2

    given = [
        name
        for name, present in (
            ("--instructions", args.instructions is not None),
            ("--instructions-text", args.instructions_text is not None),
            ("--clear-instructions", args.clear_instructions),
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
    elif args.clear_instructions:
        instructions = ""
    else:
        instructions = None

    memory = MEMORY_SCOPES[args.memory] if args.memory else None

    if instructions is None and memory is None:
        print(
            "nothing to change: pass --instructions, --instructions-text, "
            "--clear-instructions or --memory"
        )
        return 2

    session = open_session()
    status, gizmo_payload = session.session.call(GIZMO.format(id=args.project_id))
    if status != 200 or not isinstance(gizmo_payload, dict):
        print(f"no project with id {args.project_id} (gizmos read: HTTP {status})")
        return 1

    before = project_of(gizmo_payload)
    body = patch_body(gizmo_payload, instructions=instructions, memory=memory)

    print(render_current(before))
    print()
    print("PATCH body:")
    print(json.dumps(body, indent=2, ensure_ascii=False))

    if not args.apply:
        print()
        print("dry run: nothing changed; add --apply")
        return 0

    patch_status, patch_resp = session.session.call(
        PROJECT.format(id=args.project_id), method="PATCH", payload=body
    )
    if patch_status != 200:
        print(f"\nPATCH failed: HTTP {patch_status} {str(patch_resp)[:200]}")
        return 1

    read_status, gizmo_after = session.session.call(GIZMO.format(id=args.project_id))
    after = (
        project_of(gizmo_after)
        if read_status == 200 and isinstance(gizmo_after, dict)
        else {}
    )

    wanted: dict[str, Any] = {}
    if instructions is not None:
        wanted["instructions"] = instructions
    if memory is not None:
        wanted["memory_scope"] = memory

    mismatches = verify(before, after, wanted)
    if mismatches:
        print("\nread-back disagrees:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1

    print("\nchanged:")
    print(render_change(before, after, wanted))
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
