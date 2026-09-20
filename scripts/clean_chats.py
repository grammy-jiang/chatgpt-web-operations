#!/usr/bin/env python3
"""Archive, delete or unarchive worker conversations a run left behind.

    clean_chats.py --match TEXT [--delete] [--archive] [--unarchive] [--apply]
    clean_chats.py --project g-p-<id> [--match TEXT] [--delete | ...] [--apply]

Nothing happens without ``--apply``: the default is a dry run that prints
exactly what would be touched. The user's own conversations share the account
with the run's workers, so selecting the wrong ones is the failure mode this
guards against, and a title substring is a blunt selector. Exactly one of
--delete / --archive / --unarchive is required, and so is --match, unless
--project is given.

``--project g-p-<id>`` selects from that project's own listing (``GET
gizmos/<id>/conversations``, paged) instead of the account-wide one. A
project's chats are already isolated from the user's own (SKILL.md, "Projects
keep a run's chats out of the user's list"), so the blunt-selector risk
--match guards against does not apply there: with --project and no --match,
every conversation in the project is selected, which is the point -- ChatGPT
renames a chat once its first reply lands (measured 2026-09-20: "rp-test send
1" became "Reply PONG"), so a title match can miss a worker chat that a plain
--match sweep would never find. --match still narrows the selection further
when it is given alongside --project.

Deleting is ``PATCH {"is_visible": false}``, the same thing the web UI does.
Archiving is ``PATCH {"is_archived": true}``. --unarchive is the same
conversation PATCH with ``{"is_archived": false}`` -- captured 2026-09-20
alongside pin and unpin (references/endpoint-discovery.md, "Captured
2026-09-20, later") -- and it selects from the ARCHIVED listing
(``list_chats.query_for(limit, archived=True)``) rather than the default
one, since an archived chat is absent from the default listing; --project
overrides that listing choice too, since the project endpoint carries a
project's chats whatever their archived state.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table
from list_chats import query_for

CONVERSATION = "/backend-api/conversation/{id}"
PROJECT_CONVERSATIONS = "/backend-api/gizmos/{id}/conversations"
PROJECT_PAGE_LIMIT = 50
# A server that never returns a null cursor must not turn this into an
# infinite loop -- the same hard stop list_projects.py's sidebar_items and
# tests/live/conftest.py's sandbox sweep use.
MAX_PROJECT_PAGES = 50


def select(chats: list[dict[str, Any]], match: str) -> list[dict[str, Any]]:
    """Chats whose title contains ``match``. An empty match selects nothing.

    Refusing to match everything is deliberate: a bare run of this command
    must never be able to clear an account.
    """
    if not match.strip():
        return []
    needle = match.lower()
    return [c for c in chats if needle in str(c.get("title") or "").lower()]


def select_from_project(
    chats: list[dict[str, Any]], match: str
) -> list[dict[str, Any]]:
    """Every conversation in a project, or those matching a title substring.

    Unlike ``select``, an empty match here selects everything rather than
    nothing: a project's chats are already isolated from the user's own, so
    the safety net that protects the account-wide listing would only defeat
    the point of reading a project's own listing in the first place -- a
    chat ChatGPT renamed after its first reply is found by project
    membership, not by a title that may no longer say what it once did.
    """
    if not match.strip():
        return list(chats)
    needle = match.lower()
    return [c for c in chats if needle in str(c.get("title") or "").lower()]


def project_conversations(
    session: Any,
    project_id: str,
    *,
    limit: int = PROJECT_PAGE_LIMIT,
    max_pages: int = MAX_PROJECT_PAGES,
) -> list[dict[str, Any]]:
    """Every conversation in a project, paging ``cursor``.

    ``GET gizmos/<id>/conversations?cursor=<cursor>&limit=<n>`` returns
    ``{"items": [...], "cursor": "<opaque>" or null}``; the walk starts at
    ``cursor=0`` and stops at a null or missing cursor, an empty page, or
    ``max_pages`` pages read, the same pattern list_projects.py's
    ``sidebar_items`` and the sandbox sweep in tests/live/conftest.py use.
    """
    items: list[dict[str, Any]] = []
    cursor = "0"
    for _ in range(max_pages):
        status, body = session.session.call(
            f"{PROJECT_CONVERSATIONS.format(id=project_id)}"
            f"?cursor={cursor}&limit={limit}"
        )
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", default="", help="substring of the title")
    ap.add_argument(
        "--project",
        default="",
        help=(
            "a project's gizmo id (g-p-<id>); select from its own listing "
            "instead of the account-wide one, so a chat ChatGPT renamed is "
            "still found -- every chat in the project when --match is not "
            "given"
        ),
    )
    ap.add_argument("--limit", type=int, default=50, help="how many to consider")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--unarchive", action="store_true")
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    args = ap.parse_args(argv)

    if sum((args.delete, args.archive, args.unarchive)) != 1:
        print("choose exactly one of --delete, --archive or --unarchive")
        return 2
    if not args.project and not args.match.strip():
        print("--match is required unless --project selects every one of its chats")
        return 2

    session = open_session()
    action = "delete" if args.delete else "archive" if args.archive else "unarchive"

    if args.project:
        chats = project_conversations(session, args.project, limit=args.limit)
        chosen = select_from_project(chats, args.match)
    elif args.unarchive:
        _status, body = session.session.call(query_for(args.limit, archived=True))
        chats = list(body.get("items") or []) if isinstance(body, dict) else []
        chosen = select(chats, args.match)
    else:
        chats = session.list_conversations(limit=args.limit)
        chosen = select(chats, args.match)

    print(
        table(
            [(str(c["id"])[:36], shorten(c.get("title") or "", 56)) for c in chosen],
            ("id", "title"),
        )
    )
    if not chosen:
        if args.project and not args.match.strip():
            print("\nthe project has no conversations")
        else:
            print(f"\nnothing matches {args.match!r}")
        return 0
    if not args.apply:
        print(f"\ndry run: {len(chosen)} conversation(s) would be {action}d")
        print("re-run with --apply to do it")
        return 0

    for chat in chosen:
        if args.unarchive:
            session.session.call(
                CONVERSATION.format(id=str(chat["id"])),
                method="PATCH",
                payload={"is_archived": False},
            )
        else:
            (session.delete if args.delete else session.archive)(str(chat["id"]))
        print(f"{action}d {chat['id']}")
    print(f"\n{len(chosen)} conversation(s) {action}d")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
