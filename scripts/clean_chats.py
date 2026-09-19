#!/usr/bin/env python3
"""Archive or delete worker conversations a run left behind.

    clean_chats.py --match TEXT [--delete] [--archive] [--apply]

Nothing happens without ``--apply``: the default is a dry run that prints
exactly what would be touched. The user's own conversations share the account
with the run's workers, so selecting the wrong ones is the failure mode this
guards against, and a title substring is a blunt selector.

Deleting is ``PATCH {"is_visible": false}``, the same thing the web UI does.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table


def select(chats: list[dict[str, Any]], match: str) -> list[dict[str, Any]]:
    """Chats whose title contains ``match``. An empty match selects nothing.

    Refusing to match everything is deliberate: a bare run of this command
    must never be able to clear an account.
    """
    if not match.strip():
        return []
    needle = match.lower()
    return [c for c in chats if needle in str(c.get("title") or "").lower()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", required=True, help="substring of the title")
    ap.add_argument("--limit", type=int, default=50, help="how many to consider")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    args = ap.parse_args(argv)

    if args.delete == args.archive:
        print("choose exactly one of --delete or --archive")
        return 2

    session = open_session()
    chosen = select(session.list_conversations(limit=args.limit), args.match)
    action = "delete" if args.delete else "archive"
    print(
        table(
            [(str(c["id"])[:36], shorten(c.get("title") or "", 56)) for c in chosen],
            ("id", "title"),
        )
    )
    if not chosen:
        print(f"\nnothing matches {args.match!r}")
        return 0
    if not args.apply:
        print(f"\ndry run: {len(chosen)} conversation(s) would be {action}d")
        print("re-run with --apply to do it")
        return 0

    for chat in chosen:
        (session.delete if args.delete else session.archive)(str(chat["id"]))
        print(f"{action}d {chat['id']}")
    print(f"\n{len(chosen)} conversation(s) {action}d")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
