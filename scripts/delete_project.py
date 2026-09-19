#!/usr/bin/env python3
"""Delete a ChatGPT project over HTTP, after refusing anything unsafe.

    delete_project.py g-p-<id> --expect-name NAME [--force] [--apply]

Captured 2026-09-20 on a throwaway project, deleted in the same minute
(references/endpoint-discovery.md, "Captured 2026-09-20, project create and
delete"): `DELETE /backend-api/gizmos/<g-p-id>` with no body removes the
project, and `GET /backend-api/gizmos/<id>` then answers 404. Deleting a
project deletes every chat inside it, and it cannot be undone.

Three refusals happen before anything is deleted, all exit 2: an id that
does not start with "g-p-"; a project whose current name (read from
`gizmos/<id>`) does not equal --expect-name, so a typo'd id can never delete
the wrong project; and, unless --force is given, a name that does not start
with "rp-test", so a real project is never one flag away from being gone.
This command never reads tests/live/sandbox.json -- the rp-test-prefix rule
is what stands between a mistyped id and a real project, not a hard-coded
exception for one id.

Dry run by default: prints the project's name and how many chats
`gizmos/<id>/conversations` reports inside it, then exits 0 without sending
anything. --apply sends the DELETE, then reads `gizmos/<id>` again and
requires 404.

Exit 0 on a dry run, and on --apply when the post-delete read-back answered
404. Exit 1 when the initial read fails, or the post-delete read-back is
anything but 404. Exit 2 for the three refusals above.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session
from list_projects import project_of

GIZMO = "/backend-api/gizmos/{id}"
CONVERSATIONS = "/backend-api/gizmos/{id}/conversations?cursor=0&limit=50"


def refusal(project: dict[str, Any], expect_name: str, force: bool) -> str | None:
    """Why this project must not be deleted, or None if it is safe to.

    Checked in order: the name must match --expect-name exactly (a typo'd id
    must never delete the wrong project), then, unless --force was given,
    the name must start with "rp-test" (deleting a project deletes every
    chat in it and cannot be undone).
    """
    if project["name"] != expect_name:
        return (
            f"{project['id']} is named {project['name']!r}, not "
            f"{expect_name!r} -- a typo'd id must never delete the wrong "
            "project"
        )
    if not project["name"].startswith("rp-test") and not force:
        return (
            f"{project['name']!r} does not start with 'rp-test'; deleting "
            "a project deletes every chat in it and cannot be undone. Pass "
            "--force to delete it anyway."
        )
    return None


def chat_count(conversations_payload: Any) -> int:
    """How many chats a gizmos/<id>/conversations page reports."""
    items = (
        conversations_payload.get("items")
        if isinstance(conversations_payload, dict)
        else None
    )
    return len(items or [])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_id", metavar="g-p-ID", help="the project to delete")
    ap.add_argument(
        "--expect-name",
        required=True,
        metavar="NAME",
        help="the project's exact current name; a mismatch refuses",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="allow deleting a project whose name does not start with rp-test",
    )
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    args = ap.parse_args(argv)

    if not args.project_id.startswith("g-p-"):
        print(f"{args.project_id!r} is not a project id (it must start with 'g-p-')")
        return 2

    session = open_session()
    status, payload = session.session.call(GIZMO.format(id=args.project_id))
    if status != 200 or not isinstance(payload, dict):
        print(f"no project with id {args.project_id} (gizmos read: HTTP {status})")
        return 1

    project = project_of(payload)
    reason = refusal(project, args.expect_name, args.force)
    if reason:
        print(f"refusing: {reason}")
        return 2

    _conv_status, conv_body = session.session.call(
        CONVERSATIONS.format(id=args.project_id)
    )
    chats = chat_count(conv_body)

    print(f"{project['id']}  {project['name']}")
    print(f"chats inside: {chats}")

    if not args.apply:
        print()
        print("dry run: would DELETE this project and every chat in it; add --apply")
        return 0

    delete_status, delete_resp = session.session.call(
        GIZMO.format(id=args.project_id), method="DELETE", payload=None
    )
    read_status, _after = session.session.call(GIZMO.format(id=args.project_id))

    print(f"\nDELETE -> HTTP {delete_status}  {str(delete_resp)[:200]}")

    if read_status != 404:
        print(f"read-back -> HTTP {read_status}, wanted 404: not verified")
        return 1

    print(f"read-back -> HTTP 404: {args.project_id} is gone")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
