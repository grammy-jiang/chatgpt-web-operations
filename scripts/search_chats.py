#!/usr/bin/env python3
"""Search chat content, not only titles: ChatGPT's own global search.

    search_chats.py QUERY [--limit N] [--pages N] [--json PATH] [--browser chrome]

``list_chats.py --match`` only ever looks at a conversation's title, so it
cannot find the chat that discussed a paper or a phrase somewhere in its
body. This command reproduces the page's own search box over plain HTTP,
which is the only way here to search by *content*:

    POST /backend-api/global/search
    {"query": "<text>", "limit": N, "source_requests": [{"type": "conversation"}]}

Captured 2026-09-21 from the page's own search box and replayed over plain
HTTP (``references/endpoint-discovery.md``, "Captured 2026-09-21"). The
page also sends ``query_id`` (a uuid) and ``entrypoint: "global_search"``;
both are optional and results are identical without them, so neither is
sent here. The server caps ``limit`` at 40 and answers HTTP 422 above it
(a pydantic detail, "Input should be less than or equal to 40"), so
``--limit`` over 40 is refused locally, before any call is made.

**Scope: conversations only, on purpose.** The page's own search box also
asks for ``{"type": "project"}`` and ``{"type": "library", ...}`` sources;
this command never does. Projects have their own commands
(``list_projects.py``), and the file library is out of this skill's scope
entirely. A project's chats already come back as ordinary conversation
hits -- confirmed 2026-09-21 by reading four of ten "worker" hits back and
finding a ``gizmo_id`` -- but the hit's own payload carries no project id,
so this table cannot say *which* project; ``list_projects.py --id ...
--chats`` answers that.

Paging follows the response's ``cursor`` (opaque; never built by hand) for
up to ``--pages`` pages, stopping early the moment a page's ``cursor``
comes back null, and hard-stopped at the module constant ``MAX_PAGES`` the
way ``list_projects.py`` stops sidebar paging. A page can answer
``partial_results`` true, or report a source status other than ``"ok"``;
``page_status()`` turns either into a warning line, so a degraded search
is never silently read as a clean "no results".

What is verified and what is not: project chats are included among the
results (see above). Whether an *archived* chat can match is not
verified -- none happened to match during capture -- so a hit's
``archived`` flag is shown whenever ``payload.is_archived`` is true, but
its absence in every result seen so far proves nothing either way.

Exit 0 when at least one hit came back on any page read. Exit 1 when there
were no hits, or when a page read failed partway through paging -- the
failed page's number and HTTP status are printed, and whatever hits were
already collected from earlier pages are still shown. Exit 2 for a bad
argument -- today, only ``--limit`` over 40 -- before any network call.

This is plain HTTP, like every other read command here: the one POST it
makes only reads (the page's own search box); it can neither create nor
change anything, the same as a GET.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session, shorten, table

SEARCH = "/backend-api/global/search"
MAX_LIMIT = 40
DEFAULT_LIMIT = 20
DEFAULT_PAGES = 1
# a server that never returns a null cursor stops here, like list_projects.py
MAX_PAGES = 25

SNIPPET_WIDTH = 80


def body_for(query: str, limit: int, cursor: str | None = None) -> dict[str, Any]:
    """The POST body for one page of a content search.

    Always asks for conversation sources only, never ``project`` or
    ``library`` (the module docstring's scope rule); a ``cursor`` from a
    previous page's response asks for the next one, omitted on the first.
    """
    body: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "source_requests": [{"type": "conversation"}],
    }
    if cursor:
        body["cursor"] = cursor
    return body


def flags_of(payload: dict[str, Any]) -> str:
    """archived / pinned flags for one hit, list_chats.flags_of style."""
    found = []
    if payload.get("is_archived"):
        found.append("archived")
    if payload.get("is_starred"):
        found.append("pinned")
    return " ".join(found)


def _updated(update_time: Any) -> str:
    """``update_time`` (epoch seconds) as a local ``YYYY-MM-DD``, or "?"."""
    if not isinstance(update_time, (int, float)):
        return "?"
    return datetime.fromtimestamp(update_time, UTC).astimezone().date().isoformat()


def rows_for(items: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str, str]]:
    """Table rows for one page of search results, one row per item.

    Columns: updated (local date), match kind, flags (from the payload),
    conversation id, title, snippet. ``snippet`` is null on a title match;
    ``shorten()`` turns that into "" and, for a real snippet, collapses any
    newline to a single space the same way it collapses any other run of
    whitespace.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []
    for item in items:
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            (
                _updated(item.get("update_time")),
                str(item.get("match_kind") or "?"),
                flags_of(payload),
                str(payload.get("conversation_id") or ""),
                str(item.get("title") or "(untitled)"),
                shorten(item.get("snippet") or "", SNIPPET_WIDTH),
            )
        )
    return rows


def page_status(resp: dict[str, Any]) -> list[str]:
    """Warning lines for a degraded page of results.

    A search answers HTTP 200 whether or not it is complete: ``resp`` can
    carry ``partial_results: true``, or a ``source_statuses`` entry whose
    own ``status`` is not ``"ok"``. Without this, a degraded page reads as
    an ordinary (if short) set of hits, or a clean "no results", with
    nothing to say it was actually incomplete.
    """
    warnings: list[str] = []
    if resp.get("partial_results"):
        warnings.append("warning: partial_results is true; this page is incomplete")
    for status in resp.get("source_statuses") or []:
        if not isinstance(status, dict):
            continue
        state = status.get("status")
        if state == "ok":
            continue
        source = status.get("source_key") or status.get("source_type") or "?"
        warnings.append(
            f"warning: source {source!r} status {state!r} "
            f"(error_code {status.get('error_code')!r})"
        )
    return warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query", help="text to find in a conversation's title or content")
    ap.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"hits per page (default {DEFAULT_LIMIT}, server cap {MAX_LIMIT})",
    )
    ap.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_PAGES,
        help=f"pages to follow via cursor (default {DEFAULT_PAGES}, "
        f"hard stop {MAX_PAGES})",
    )
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write the raw items and paging state here",
    )
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    if args.limit > MAX_LIMIT:
        print(
            f"--limit {args.limit} is refused: the server caps global search "
            f"at {MAX_LIMIT} and answers HTTP 422 above it"
        )
        return 2

    session = open_session(args.browser)

    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    has_more = False
    cursor: str | None = None
    pages_done = 0
    failure = ""

    for page_num in range(1, min(args.pages, MAX_PAGES) + 1):
        body = body_for(args.query, args.limit, cursor)
        status, resp = session.session.call(SEARCH, method="POST", payload=body)
        if status != 200 or not isinstance(resp, dict):
            failure = f"page {page_num} failed: HTTP {status} {str(resp)[:200]}"
            break
        pages_done += 1
        items.extend(resp.get("items") or [])
        warnings.extend(page_status(resp))
        has_more = any(
            isinstance(s, dict) and s.get("has_more")
            for s in resp.get("source_statuses") or []
        )
        cursor = resp.get("cursor") or None
        if not cursor:
            break

    print(
        table(
            rows_for(items),
            ("updated", "match", "flags", "conversation", "title", "snippet"),
        )
    )
    print()
    more = "more available" if has_more else "no more"
    print(f"{len(items)} hit(s) on {pages_done} page(s); {more}")
    for warning in warnings:
        print(warning)
    if failure:
        print(failure)

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "query": args.query,
            "limit": args.limit,
            "pages": pages_done,
            "items": items,
            "has_more": has_more,
        }
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwritten to {out}")

    if failure:
        return 1
    return 0 if items else 1


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
