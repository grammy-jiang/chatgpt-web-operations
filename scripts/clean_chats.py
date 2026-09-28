#!/usr/bin/env python3
"""Archive, delete or unarchive worker conversations a run left behind.

    clean_chats.py --match TEXT [--delete] [--archive] [--unarchive] [--apply]
    clean_chats.py --project g-p-<id> [--match TEXT] [--delete | ...] [--apply]
    clean_chats.py --id ID_OR_URL [--id ...] [--delete | ...] [--backup DIR] [--apply]
    clean_chats.py --tracked [--delete | ...] [--backup DIR] [--apply]
    clean_chats.py --track ID_OR_URL [--note TEXT] | --untrack ID_OR_URL

``--id`` selects exact conversations (an id, or a ``/c/<id>`` URL), read one
by one, so a chat in any project or list is found and a chat that no longer
exists is reported as already gone. ``--tracked`` selects the ids in a local
ledger of test chats: ``--track`` records one while it is open in a tab,
``--untrack`` drops one without touching it, and every tracked chat this
command deletes, archives or finds gone is dropped from the ledger. The
ledger is ``~/.local/share/chatgpt-chats/test-chats.json``
(``CHATGPT_CHAT_LEDGER`` overrides it), the file binnacle's ``chatgpt-chats``
kept, so its entries carry over. ``--backup DIR`` saves each chat's whole
conversation JSON as ``DIR/<UTC stamp>_<id>.json`` before acting on it; a
chat whose backup cannot be written is not touched.

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
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
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
# Account-wide --match is the blunt selector: a pattern this short would
# match too much of the owner's own list (binnacle's chatgpt-chats refused it
# too). A project's own listing is already isolated, so it is not held to it.
MIN_MATCH = 3
LEDGER_ENV = "CHATGPT_CHAT_LEDGER"
DEFAULT_LEDGER = Path.home() / ".local" / "share" / "chatgpt-chats" / "test-chats.json"
CHAT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def parse_chat_id(value: str) -> str:
    """The conversation id in ``value`` (an id or a ``/c/<id>`` URL), lower
    case, or "" when there is none."""
    match = CHAT_ID_RE.search(value)
    return match.group(0).lower() if match else ""


def ledger_path() -> Path:
    return Path(os.environ.get(LEDGER_ENV) or DEFAULT_LEDGER).expanduser()


def load_ledger(path: Path) -> list[dict[str, Any]]:
    """The tracked chats; ``ValueError`` when the file is not a ledger."""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, list) or not all(
        isinstance(c, dict) and isinstance(c.get("id"), str) for c in chats
    ):
        raise ValueError(f"{path} is not a chat ledger")
    return chats


def save_ledger(path: Path, chats: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chats": chats}, indent=2) + "\n", encoding="utf-8")


def backup_conversation(directory: Path, chat: str, text: str) -> Path:
    """``DIR/<UTC stamp>_<id>.json`` holding ``text``, the server's JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}_{chat}.json"
    path.write_text(text, encoding="utf-8")
    return path


def read_chat_raw(session: Any, chat: str) -> tuple[int, str]:
    """``(status, the conversation's JSON text)``; ``(0, reason)`` when the
    request itself failed."""
    try:
        status, text = session.session.call(CONVERSATION.format(id=chat), raw=True)
    except Exception as exc:  # the transport's failures have no common base
        return 0, str(exc)[:120]
    return status, text if isinstance(text, str) else json.dumps(text)


def title_of(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    return str(data.get("title") or "") if isinstance(data, dict) else ""


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
    ap.add_argument(
        "--id",
        dest="ids",
        action="append",
        default=[],
        metavar="ID_OR_URL",
        help="select exactly this conversation (repeatable)",
    )
    ap.add_argument(
        "--tracked", action="store_true", help="select the chats in the ledger"
    )
    ap.add_argument("--track", metavar="ID_OR_URL", help="record a test chat")
    ap.add_argument("--note", default="", help="a note to keep with --track")
    ap.add_argument("--untrack", metavar="ID_OR_URL", help="forget a tracked chat")
    ap.add_argument(
        "--backup",
        metavar="DIR",
        help="save each chat's conversation JSON here before acting",
    )
    ap.add_argument("--limit", type=int, default=50, help="how many to consider")
    ap.add_argument(
        "--max",
        type=int,
        default=0,
        metavar="N",
        help="refuse to --apply to more than N chats (0: no cap)",
    )
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--unarchive", action="store_true")
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    ap.add_argument("--browser", default="chrome", help="whose session to use")
    args = ap.parse_args(argv)

    if args.track or args.untrack:
        return edit_ledger(args)
    if sum((args.delete, args.archive, args.unarchive)) != 1:
        print("choose exactly one of --delete, --archive or --unarchive")
        return 2
    if (args.ids or args.tracked) and (
        args.match.strip() or args.project or (args.ids and args.tracked)
    ):
        print("--id and --tracked select on their own: drop --match / --project")
        return 2
    if args.ids or args.tracked:
        return by_id(args)
    if not args.project and not args.match.strip():
        print("--match is required unless --project selects every one of its chats")
        return 2
    if not args.project and len(args.match) < MIN_MATCH:  # raw: "rp " is 3
        print(
            f"--match needs at least {MIN_MATCH} characters: a shorter pattern "
            "matches too much of the account's own list"
        )
        return 2

    session = open_session(args.browser)
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
    if args.max and len(chosen) > args.max:
        print(f"\nrefusing: {len(chosen)} selected, more than --max {args.max}")
        return 2

    done, failed, _ids = act_on(
        session, [str(c["id"]) for c in chosen], action, args, {}
    )
    print(
        f"\n{done} conversation(s) {action}d" + (f", {failed} failed" if failed else "")
    )
    return 1 if failed else 0


def edit_ledger(args: argparse.Namespace) -> int:
    """--track / --untrack: the local ledger only, no session."""
    path = ledger_path()
    value = args.track or args.untrack
    chat = parse_chat_id(value)
    if not chat:
        print(f"{value!r} holds no conversation id (an id or a /c/<id> URL)")
        return 2
    try:
        chats = load_ledger(path)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.track:
        if any(c["id"] == chat for c in chats):
            print(f"already tracked: {chat}")
            return 0
        chats.append(
            {
                "id": chat,
                "note": args.note,
                "tracked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        save_ledger(path, chats)
        print(f"tracked {chat}" + (f" ({args.note})" if args.note else ""))
        return 0
    kept = [c for c in chats if c["id"] != chat]
    if len(kept) == len(chats):
        print(f"{chat} is not tracked")
        return 1
    save_ledger(path, kept)
    print(f"untracked {chat}; the conversation itself was not touched")
    return 0


def by_id(args: argparse.Namespace) -> int:
    """--id / --tracked: read each chat, then act on the ones that exist."""
    action = "delete" if args.delete else "archive" if args.archive else "unarchive"
    path = ledger_path()
    try:
        ledger = load_ledger(path)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.tracked:
        wanted = [c["id"] for c in ledger]
        if not wanted:
            print(f"no tracked chats in {path}; record one with --track")
            return 0
    else:
        wanted = []
        for value in args.ids:
            chat = parse_chat_id(value)
            if not chat:
                print(f"{value!r} holds no conversation id (an id or a /c/<id> URL)")
                return 2
            if chat not in wanted:
                wanted.append(chat)

    session = open_session(args.browser)
    rows, texts, gone, unreadable = [], {}, [], 0
    for chat in wanted:
        status, text = read_chat_raw(session, chat)
        if status == 404:
            gone.append(chat)
            rows.append((chat, "(already gone)"))
        elif status == 200:
            texts[chat] = text
            rows.append((chat, shorten(title_of(text), 56)))
        else:
            unreadable += 1
            rows.append((chat, f"(could not read: HTTP {status} {text[:60]})"))
    print(table(rows, ("id", "title")))
    if not args.apply:
        print(f"\ndry run: {len(texts)} conversation(s) would be {action}d")
        print("re-run with --apply to do it")
        return 1 if unreadable else 0
    if args.max and len(texts) > args.max:
        print(f"\nrefusing: {len(texts)} selected, more than --max {args.max}")
        return 2

    done, failed, done_ids = act_on(session, list(texts), action, args, texts)
    tracked_ids = {c["id"] for c in ledger}
    finished = set(gone) | done_ids
    if tracked_ids & finished:
        save_ledger(path, [c for c in ledger if c["id"] not in finished])
    summary = f"\n{done} conversation(s) {action}d"
    if gone:
        summary += f", {len(gone)} already gone"
    if failed or unreadable:
        summary += f", {failed + unreadable} failed"
    print(summary)
    return 1 if failed or unreadable else 0


def act_on(
    session: Any,
    chats: list[str],
    action: str,
    args: argparse.Namespace,
    texts: dict[str, str],
) -> tuple[int, int, set[str]]:
    """Back up (with --backup) and act on each chat; ``(done, failed, the
    ids acted on)``. A chat whose backup could not be read or written is
    not touched."""
    done, failed = 0, 0
    done_ids: set[str] = set()
    for chat in chats:
        if args.backup:
            text = texts.get(chat)
            if text is None:
                status, text = read_chat_raw(session, chat)
                if status != 200:
                    print(f"not {action}d {chat}: backup read failed (HTTP {status})")
                    failed += 1
                    continue
            try:
                saved = backup_conversation(Path(args.backup).expanduser(), chat, text)
            except OSError as exc:
                print(f"not {action}d {chat}: backup failed: {exc}")
                failed += 1
                continue
        try:
            if action == "unarchive":
                status, _ = session.session.call(
                    CONVERSATION.format(id=chat),
                    method="PATCH",
                    payload={"is_archived": False},
                )
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
            else:
                (session.delete if action == "delete" else session.archive)(chat)
        except Exception as exc:  # the transport's failures have no common base
            print(f"{action} failed for {chat}: {str(exc)[:160]}")
            failed += 1
            continue
        done += 1
        done_ids.add(chat)
        print(f"{action}d {chat}" + (f" (backup: {saved})" if args.backup else ""))
    return done, failed, done_ids


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
