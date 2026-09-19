#!/usr/bin/env python3
"""List ChatGPT projects, and optionally the conversations inside one.

    list_projects.py [--match TEXT]
    list_projects.py --id g-p-<id> [--chats]

A project groups conversations, carries its own instructions, and keeps its
chats out of the main list. That last property is why this matters here: a
run's worker conversations belong in a project of their own rather than
scattered among the user's chats and deleted afterwards.

Projects are "snorlax gizmos" in the backend, which is why the endpoints say
gizmo. Read-only, plain HTTP.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table

SIDEBAR = "/backend-api/gizmos/snorlax/sidebar"


def project_of(item: dict[str, Any]) -> dict[str, Any]:
    """The fields worth showing, flattened out of the sidebar's wrapper."""
    gizmo = item.get("gizmo") or {}
    display = gizmo.get("display") or {}
    return {
        "id": str(gizmo.get("id") or ""),
        "name": str(display.get("name") or "(unnamed)"),
        "short_url": str(gizmo.get("short_url") or ""),
        "instructions": str(gizmo.get("instructions") or ""),
        "files": len(item.get("files") or []),
        "updated": str(gizmo.get("updated_at") or "")[:19],
    }


def projects_from(sidebar: dict[str, Any], match: str = "") -> list[dict[str, Any]]:
    """Projects whose name contains ``match``, case-insensitively."""
    needle = match.lower()
    found = [project_of(i) for i in sidebar.get("items") or []]
    return [p for p in found if not needle or needle in p["name"].lower()]


def project_url(project: dict[str, Any]) -> str:
    """Where to open this project. Composing here creates a chat inside it."""
    slug = project["short_url"] or project["id"]
    return f"https://chatgpt.com/g/{slug}/project"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", default="", help="substring of the project name")
    ap.add_argument("--id", default="", help="one project, by id")
    ap.add_argument("--chats", action="store_true", help="list its conversations")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    session = open_session()
    _status, sidebar = session.session.call(SIDEBAR)
    found = projects_from(sidebar if isinstance(sidebar, dict) else {}, args.match)

    if args.id:
        found = [p for p in found if p["id"] == args.id]
        if not found:
            print(f"no project with id {args.id}")
            return 1

    print(
        table(
            [
                (p["id"], shorten(p["name"], 34), str(p["files"]), p["updated"])
                for p in found
            ],
            ("id", "name", "files", "updated"),
        )
    )
    print(f"\n{len(found)} project(s)")

    if args.id and found:
        project = found[0]
        print(f"\nopen at: {project_url(project)}")
        if project["instructions"]:
            print(f"instructions: {shorten(project['instructions'], 200)}")
        if args.chats:
            _st, body = session.session.call(
                f"/backend-api/gizmos/{project['id']}/conversations?limit={args.limit}"
            )
            items = body.get("items") or [] if isinstance(body, dict) else []
            print()
            print(
                table(
                    [
                        (str(c.get("id", ""))[:36], shorten(c.get("title") or "", 44))
                        for c in items
                    ],
                    ("chat id", "title"),
                )
            )
            print(f"\n{len(items)} conversation(s) in {project['name']}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
