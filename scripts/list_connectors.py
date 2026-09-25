#!/usr/bin/env python3
"""List the ChatGPT connectors the account can use: links, custom MCP apps, tunnels.

    list_connectors.py                    # links + custom MCP apps
    list_connectors.py --match TEXT       # filter by name or id substring
    list_connectors.py --tunnels          # the OpenAI tunnels ChatGPT can bind a connector to
    list_connectors.py --detail ID [...]  # full record of asdk_app_/link_ ids (connectors/batch)
    list_connectors.py --json

Two populations exist since the plugins era (measured 2026-09-25):

- **links** (``link_<32hex>``): what ``POST aip/connectors/links/list_accessible``
  returns for the user principal -- first-party connectors (GitHub, Gmail, ...)
  and pre-plugin custom MCP connectors such as ``Raspberry Pi MCP``. These are
  what ``chatgpt-refresh`` refreshes through ``refresh_actions``.
- **custom MCP apps** (``asdk_app_<32hex>``): a connector created through the
  "Create MCP App" form is an *app* with a plugin release. It is listed by
  ``GET ps/plugins/installed`` (``release.display_name``, ``version``,
  ``status``, ``discoverability``) and described by
  ``POST aip/connectors/batch`` (``connector_type``, ``tunnel_id``,
  ``base_url``, ``created_at``). It does not appear in ``list_accessible``
  until it is connected.

``--tunnels`` reads ``GET aip/connectors/mcp/tunnels``: the same list the form's
"Tunnel" picker shows (``name (tunnel_id)``). Read-only, plain HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _common import ensure_venv, open_session, shorten, table

LIST_ACCESSIBLE = "/backend-api/aip/connectors/links/list_accessible"
INSTALLED = "/backend-api/ps/plugins/installed"
BATCH = "/backend-api/aip/connectors/batch"
TUNNELS = "/backend-api/aip/connectors/mcp/tunnels"


def call(session: Any, path: str, method: str = "GET", payload: Any = None) -> Any:
    status, body = session.session.call(path, method=method, payload=payload)
    if status != 200:
        print(f"{method} {path} -> HTTP {status}: {str(body)[:200]}")
        raise SystemExit(1)
    return body


def links(session: Any) -> list[dict[str, Any]]:
    body = call(
        session,
        LIST_ACCESSIBLE,
        "POST",
        {"principals": [{"type": "USER", "id": session.session.user_id}]},
    )
    out = []
    for link in body.get("links") or []:
        out.append(
            {
                "kind": "link",
                "id": str(link.get("id") or ""),
                "name": str(link.get("name") or ""),
                "connector_id": str(link.get("connector_id") or ""),
                "auth": str(link.get("auth_type") or ""),
                "tools": len(link.get("actions") or []),
                "created": str(link.get("created_at") or "")[:19],
            }
        )
    return out


def apps(session: Any) -> list[dict[str, Any]]:
    body = call(session, INSTALLED)
    items = body if isinstance(body, list) else (body.get("plugins") or body.get("items") or [])
    out = []
    for item in items:
        cid = str(item.get("connector_id") or "")
        if not cid.startswith("asdk_app_"):
            continue
        rel = item.get("release") or {}
        out.append(
            {
                "kind": "app",
                "id": cid,
                "name": str(rel.get("display_name") or ""),
                "version": str(rel.get("version") or ""),
                "status": str(item.get("status") or ""),
                "discoverability": str(item.get("discoverability") or ""),
                "release_id": str(rel.get("id") or ""),
            }
        )
    return out


def detail(session: Any, ids: list[str]) -> list[dict[str, Any]]:
    body = call(session, BATCH, "POST", {"connector_ids": ids, "include_actions": True})
    return list(body.get("connectors") or [])


def tunnels(session: Any) -> list[dict[str, Any]]:
    body = call(session, TUNNELS)
    items = body if isinstance(body, list) else (body.get("tunnels") or body.get("items") or [])
    return [dict(t) for t in items]


def main() -> int:
    ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--match", default="", help="substring of the name or id (case-insensitive)")
    ap.add_argument("--tunnels", action="store_true", help="list the tunnels ChatGPT offers in the connector form")
    ap.add_argument("--detail", nargs="+", metavar="ID", help="full records for these connector ids")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args()
    session = open_session(args.browser)

    if args.detail:
        rows = detail(session, args.detail)
        print(json.dumps(rows, indent=2) if args.json else "\n".join(
            f"{r.get('id')}  {r.get('connector_type')}  name={r.get('name')!r}  tunnel={r.get('tunnel_id')}  base_url={r.get('base_url')}  created={str(r.get('created_at') or '')[:19]}  actions={len(r.get('actions') or [])}"
            for r in rows))
        return 0
    if args.tunnels:
        rows = tunnels(session)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(table([(str(t.get("id") or t.get("tunnel_id") or ""), shorten(str(t.get("name") or ""), 40), shorten(str(t.get("description") or ""), 60)) for t in rows], ("tunnel", "name", "description")))
        return 0

    rows = links(session) + apps(session)
    needle = args.match.lower()
    if needle:
        rows = [r for r in rows if needle in r["name"].lower() or needle in r["id"].lower()]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(table(
            [(r["kind"], r["id"], shorten(r["name"], 34), str(r.get("tools", "")) if r["kind"] == "link" else r.get("status", ""), r.get("auth", "") if r["kind"] == "link" else r.get("discoverability", "")) for r in rows],
            ("kind", "id", "name", "tools/status", "auth/visibility"),
        ))
        print(f"{sum(r['kind']=='link' for r in rows)} links, {sum(r['kind']=='app' for r in rows)} custom MCP apps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
