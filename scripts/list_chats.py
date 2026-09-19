#!/usr/bin/env python3
"""List recent ChatGPT conversations. Read-only, plain HTTP.

    list_chats.py [--limit N] [--match TEXT] [--archived] [--pinned]
                  [--no-project-chats]

``--match`` filters on the title, case-insensitively, which is how you find
the worker conversations a run left behind. By default project chats are
included, because that is where the workers live; ``--no-project-chats``
shows the list the way the sidebar does. What the UI calls *pinned* is
``is_starred`` in the API.

The last column flags each chat: ``project`` when it lives inside one,
``pinned``, ``archived``, ``temporary``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table

CONVERSATIONS = "/backend-api/conversations"


def query_for(
    limit: int = 20,
    offset: int = 0,
    archived: bool = False,
    pinned: bool = False,
    hide_project_chats: bool = False,
) -> str:
    """The listing URL for one view. Only the flags asked for are sent.

    The page itself sends ``is_archived=false&is_starred=false`` for the main
    list and ``hide_snorlax=true`` to keep project chats out of it.
    """
    query = f"{CONVERSATIONS}?offset={offset}&limit={limit}&order=updated"
    if archived:
        query += "&is_archived=true"
    if pinned:
        query += "&is_starred=true"
    if hide_project_chats:
        query += "&hide_snorlax=true"
    return query


def flags_of(chat: dict[str, Any]) -> str:
    found = []
    if chat.get("gizmo_id"):
        found.append("project")
    if chat.get("is_starred"):
        found.append("pinned")
    if chat.get("is_archived"):
        found.append("archived")
    if chat.get("is_temporary_chat"):
        found.append("temporary")
    return " ".join(found)


def rows_for(chats: list[dict[str, Any]], match: str = "") -> list[tuple[str, ...]]:
    """Table rows for the chats whose title contains ``match``."""
    needle = match.lower()
    return [
        (
            str(c.get("id", ""))[:36],
            shorten(c.get("title") or "(untitled)", 48),
            str(c.get("update_time") or c.get("create_time") or "")[:19],
            flags_of(c),
        )
        for c in chats
        if not needle or needle in str(c.get("title") or "").lower()
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--match", default="", help="substring of the title")
    ap.add_argument("--archived", action="store_true", help="archived chats only")
    ap.add_argument("--pinned", action="store_true", help="pinned chats only")
    ap.add_argument(
        "--no-project-chats", action="store_true", help="hide chats inside projects"
    )
    args = ap.parse_args(argv)

    session = open_session()
    _status, body = session.session.call(
        query_for(
            args.limit,
            archived=args.archived,
            pinned=args.pinned,
            hide_project_chats=args.no_project_chats,
        )
    )
    chats = list(body.get("items") or []) if isinstance(body, dict) else []
    rows = rows_for(chats, args.match)
    print(table(rows, ("id", "title", "updated", "flags")))
    print(f"\n{len(rows)} of {len(chats)} listed conversation(s)")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
