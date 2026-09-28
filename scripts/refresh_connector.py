#!/usr/bin/env python3
"""Refresh a connected MCP app's cached tool list: the terminal equivalent of
the "Refresh" button under Settings -> Plugins -> <connector> -> Information.

    refresh_connector.py NAME [--no-retry] [--dry-run] [--json] [--browser B]
    refresh_connector.py --link-id link_<id> [...]
    refresh_connector.py --list [--json]

NAME matches a connector link's name case-insensitively: an exact match
first, else a unique substring. ``--link-id`` skips the lookup. The links
come from ``POST aip/connectors/links/list_accessible`` (a read); the refresh
is ``POST aip/connectors/mcp/refresh_actions`` with ``{"link_id": ...}``,
which makes ChatGPT re-read the tools from the MCP server behind the link
and answers with the new list. Run it after the server's tools changed.

HTTP 424 means ChatGPT's tunnel service cannot reach the server yet, as in
the seconds after a tunnel client restarts; it is retried 5 times, 10 s
apart, unless ``--no-retry``. The command line is the one binnacle's
``chatgpt-refresh`` had (``--browser auto`` included), so that installed
name can point here.

Exit 0 refreshed, listed or previewed; 1 a request failed; 2 no such
connector, an ambiguous name, or bad arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import list_connectors
from _common import ensure_venv, open_session

REFRESH = "/backend-api/aip/connectors/mcp/refresh_actions"
RETRY_424_ATTEMPTS = 5
RETRY_424_DELAY_S = 10.0
SLEEP = time.sleep


def resolve(query: str, links: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """``(link, "")`` for the one link ``query`` names, or ``({}, reason)``:
    an exact case-insensitive name first, else a unique substring."""
    wanted = query.casefold()
    exact = [link for link in links if link["name"].casefold() == wanted]
    hits = exact or [link for link in links if wanted in link["name"].casefold()]
    if not hits:
        known = ", ".join(sorted(repr(link["name"]) for link in links)) or "none"
        return {}, f"no connector matches {query!r}; known: {known}"
    if len(hits) > 1:
        names = ", ".join(sorted(repr(link["name"]) for link in hits))
        return {}, f"{query!r} is ambiguous; it matches {names}"
    return hits[0], ""


def refresh(
    session: Any, link_id: str, attempts: int = RETRY_424_ATTEMPTS
) -> tuple[int, list[str]]:
    """``(status, tool names)`` of the refresh; 424 is retried up to
    ``attempts`` times. Any other failure is returned at once."""
    status, body = 0, {}
    for attempt in range(1, attempts + 1):
        status, body = session.session.call(
            REFRESH, method="POST", payload={"link_id": link_id}
        )
        if status == 200:
            actions = body.get("actions") if isinstance(body, dict) else None
            return 200, [
                str(action.get("name"))
                for action in actions or []
                if isinstance(action, dict)
            ]
        if status == 424 and attempt < attempts:
            print(
                f"refresh_connector: ChatGPT could not reach the server yet "
                f"(HTTP 424, attempt {attempt}/{attempts}); retrying in "
                f"{RETRY_424_DELAY_S:g} s",
                file=sys.stderr,
            )
            SLEEP(RETRY_424_DELAY_S)
            continue
        break
    detail = body.get("detail") if isinstance(body, dict) else body
    print(f"refresh failed: HTTP {status}: {str(detail)[:200]}")
    return status, []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "name", nargs="?", help="connector name (exact or unique substring)"
    )
    ap.add_argument("--list", action="store_true", help="list the connectors and exit")
    ap.add_argument("--link-id", metavar="ID", help="refresh this link, no lookup")
    ap.add_argument("--no-retry", action="store_true", help="fail at once on HTTP 424")
    ap.add_argument(
        "--dry-run", action="store_true", help="resolve and show; do not refresh"
    )
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--browser", default="chrome", help="chrome, chromium or auto")
    args = ap.parse_args(argv)
    if not (args.list or args.name or args.link_id):
        ap.error("give a connector name, --list or --link-id")

    session = open_session(args.browser)
    if args.list:
        rows = sorted(
            list_connectors.links(session), key=lambda r: r["name"].casefold()
        )
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            width = max((len(r["name"]) for r in rows), default=0)
            for row in rows:
                print(f"{row['name']:{width}}  {row['id']}  ({row['tools']} tools)")
        return 0

    if args.link_id:
        label, link_id = args.link_id, args.link_id
    else:
        link, reason = resolve(args.name, list_connectors.links(session))
        if reason:
            print(reason)
            return 2
        label, link_id = link["name"], link["id"]

    if args.dry_run:
        print(f"dry run: would refresh {label} ({link_id}); drop --dry-run to do it")
        return 0
    status, tools = refresh(
        session, link_id, 1 if args.no_retry else RETRY_424_ATTEMPTS
    )
    if status != 200:
        return 1
    if args.json:
        print(json.dumps({"name": label, "link_id": link_id, "tools": tools}, indent=2))
    else:
        print(f"Refreshed {label}. Tools now: " + ", ".join(tools))
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
