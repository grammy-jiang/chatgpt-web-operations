#!/usr/bin/env python3
"""List ChatGPT projects, and optionally the conversations inside one.

    list_projects.py [--match TEXT]
    list_projects.py --id g-p-<id> [--chats] [--files]

A project groups conversations, carries its own instructions, and keeps its
chats out of the main list. That last property is why this matters here: a
run's worker conversations belong in a project of their own rather than
scattered among the user's chats and deleted afterwards.

Projects are "snorlax gizmos" in the backend, which is why the endpoints say
gizmo. Read-only, plain HTTP.

The bare listing walks every page of the sidebar -- it returns 5 items and a
``cursor`` unless told otherwise -- so the printed count is the true total,
not just the first page. ``--id`` reads the project directly from
``gizmos/<id>`` instead of searching the sidebar, which is the only place
the full (unshortened) instructions and the memory scope are available;
``--files`` then lists its files, keeping scalar fields only since a file
record's full shape has not been seen on a real project yet.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table

SIDEBAR = "/backend-api/gizmos/snorlax/sidebar"
GIZMO = "/backend-api/gizmos/{id}"
SIDEBAR_PAGE_LIMIT = 50
MAX_SIDEBAR_PAGES = 50  # a server that never returns a null cursor stops here

SCALAR = (str, int, float, bool, type(None))


def sidebar_entry(item: dict[str, Any]) -> dict[str, Any]:
    """The fields worth showing, flattened out of one sidebar item."""
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


def sidebar_items(
    session: Any,
    *,
    limit: int = SIDEBAR_PAGE_LIMIT,
    max_pages: int = MAX_SIDEBAR_PAGES,
) -> list[dict[str, Any]]:
    """Every raw sidebar item (``{"gizmo": ..., "files": ...}``), all pages.

    ``GET gizmos/snorlax/sidebar?owned_only=true&limit=<n>`` returns
    ``{"items": [...], "cursor": "<opaque>" or null}``; the next page is the
    same URL plus ``&cursor=<cursor>``. Walking stops at a null or missing
    cursor, an empty page, or ``max_pages`` pages read -- the last one is a
    hard cap so a server that never returns a null cursor cannot loop this
    forever.
    """
    items: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        url = f"{SIDEBAR}?owned_only=true&limit={limit}"
        if cursor:
            url += f"&cursor={cursor}"
        status, body = session.session.call(url)
        if status != 200 or not isinstance(body, dict):
            break
        page = body.get("items") or []
        if not page:
            break
        items.extend(page)
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    return items


def projects_from(sidebar: dict[str, Any], match: str = "") -> list[dict[str, Any]]:
    """Projects whose name contains ``match``, case-insensitively."""
    needle = match.lower()
    found = [sidebar_entry(i) for i in sidebar.get("items") or []]
    return [p for p in found if not needle or needle in p["name"].lower()]


def project_url(project: dict[str, Any]) -> str:
    """Where to open this project. Composing here creates a chat inside it."""
    slug = project["short_url"] or project["id"]
    return f"https://chatgpt.com/g/{slug}/project"


def project_of(payload: dict[str, Any]) -> dict[str, Any]:
    """Instructions, files and memory scope of one project, from ``gizmos/<id>``.

    File records keep their scalar fields only; their shape has not been seen
    on a project with files yet, and nested blobs would bloat the document.
    """
    gizmo = payload.get("gizmo") or {}
    display = gizmo.get("display") or {}
    project = {
        "id": str(gizmo.get("id") or ""),
        "name": str(display.get("name") or "(unnamed)"),
        "short_url": str(gizmo.get("short_url") or ""),
        "instructions": str(gizmo.get("instructions") or ""),
        "files": [
            {k: v for k, v in f.items() if isinstance(v, SCALAR)}
            for f in payload.get("files") or []
            if isinstance(f, dict)
        ],
        "memory_enabled": gizmo.get("memory_enabled"),
        "memory_scope": gizmo.get("memory_scope"),
        "context_stuffing_budget": gizmo.get("context_stuffing_budget"),
        "model": gizmo.get("model") or gizmo.get("default_model"),
    }
    project["url"] = project_url(project)
    return project


def file_row(f: dict[str, Any]) -> tuple[str, str, str]:
    """One project file for ``--files``: id, name, then any other scalar field."""
    rest = ", ".join(
        f"{k}={v}" for k, v in sorted(f.items()) if k not in ("id", "name")
    )
    return (str(f.get("id", "")), str(f.get("name", "")), rest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", default="", help="substring of the project name")
    ap.add_argument(
        "--id", default="", help="one project, by id (reads gizmos/<id> directly)"
    )
    ap.add_argument("--chats", action="store_true", help="list its conversations")
    ap.add_argument("--files", action="store_true", help="list its files (with --id)")
    ap.add_argument("--limit", type=int, default=20, help="conversations to list")
    args = ap.parse_args(argv)

    session = open_session()

    if args.id:
        status, payload = session.session.call(GIZMO.format(id=args.id))
        if status != 200 or not isinstance(payload, dict):
            print(f"no project with id {args.id}")
            return 1
        project = project_of(payload)
        print(f"{project['id']}  {project['name']}")
        print(f"open at: {project['url']}")
        print(
            "memory: "
            + ("enabled" if project["memory_enabled"] else "disabled")
            + f", scope {project['memory_scope'] or '-'}"
        )
        print(f"instructions: {project['instructions'] or '(none)'}")

        if args.files:
            print()
            print(
                table(
                    [file_row(f) for f in project["files"]],
                    ("id", "name", "other fields"),
                )
            )
            print(f"\n{len(project['files'])} file(s)")

        if args.chats:
            _st, body = session.session.call(
                f"/backend-api/gizmos/{project['id']}/conversations"
                f"?cursor=0&limit={args.limit}"
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

    found = projects_from({"items": sidebar_items(session)}, args.match)
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
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
