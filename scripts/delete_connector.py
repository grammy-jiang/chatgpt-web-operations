#!/usr/bin/env python3
"""Delete (uninstall) a custom MCP app connector together with its links.

    delete_connector.py asdk_app_<id> --confirm [--dry-run]
    delete_connector.py link_<id> --confirm          # one dangling link only

"Plugin actions -> Uninstall" on the plugin page sends
``DELETE aip/connectors/<asdk_app id>`` (answer ``{}``): the app and its
plugin release disappear, but the user's link created by "Connect" stays
behind as an ACTIVE link with no connector (measured 2026-09-25: it was still
in ``links/list_accessible`` five minutes later). ``DELETE
aip/connectors/links/<link id>`` (answer ``{}``) removes such a link. So this
command first deletes every accessible link whose ``connector_id`` is the
app, then the app. Irreversible, hence ``--confirm``; ``--dry-run`` looks
everything up and prints the requests.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, open_session

CONNECTOR = "/backend-api/aip/connectors/{id}"
LINK = "/backend-api/aip/connectors/links/{id}"
BATCH = "/backend-api/aip/connectors/batch"
LIST_ACCESSIBLE = "/backend-api/aip/connectors/links/list_accessible"


def links_of(session: Any, connector_id: str) -> list[dict[str, Any]]:
    status, body = session.session.call(
        LIST_ACCESSIBLE, method="POST",
        payload={"principals": [{"type": "USER", "id": session.session.user_id}]},
    )
    if status != 200 or not isinstance(body, dict):
        print(f"could not list links: HTTP {status}: {str(body)[:200]}")
        raise SystemExit(1)
    return [link for link in body.get("links") or [] if link.get("connector_id") == connector_id]


def delete(session: Any, path: str) -> bool:
    status, body = session.session.call(path, method="DELETE")
    if status != 200:
        print(f"DELETE {path} failed: HTTP {status}: {str(body)[:300]}")
        return False
    print(f"deleted {path.rsplit('/', 1)[-1]}")
    return True


def main() -> int:
    ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", help="asdk_app_<32hex> (app + its links) or link_<32hex> (that link only)")
    ap.add_argument("--confirm", action="store_true", help="really delete")
    ap.add_argument("--dry-run", action="store_true", help="look everything up and print the requests")
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args()
    session = open_session(args.browser)

    if args.target.startswith("link_"):
        if args.dry_run or not args.confirm:
            print(f"DELETE {LINK.format(id=args.target)}  (add --confirm to delete)")
            return 0
        return 0 if delete(session, LINK.format(id=args.target)) else 1
    if not args.target.startswith("asdk_app_"):
        print("expected asdk_app_<32hex> or link_<32hex> (see list_connectors.py); first-party connectors are not deleted here")
        return 2

    status, body = session.session.call(BATCH, method="POST", payload={"connector_ids": [args.target], "include_actions": False})
    found = (body.get("connectors") or []) if status == 200 and isinstance(body, dict) else []
    links = links_of(session, args.target)
    if not found and not links:
        print(f"no connector {args.target} and no link points at it (HTTP {status})")
        return 1
    if found:
        rec = found[0]
        print(f"app: {rec.get('id')}  name={rec.get('name')!r}  type={rec.get('connector_type')}  tunnel={rec.get('tunnel_id')}  created={str(rec.get('created_at') or '')[:19]}")
    for link in links:
        print(f"link: {link.get('id')}  name={link.get('name')!r}  auth={link.get('auth_type')}  tools={len(link.get('actions') or [])}")
    if args.dry_run or not args.confirm:
        for link in links:
            print(f"DELETE {LINK.format(id=link['id'])}")
        if found:
            print(f"DELETE {CONNECTOR.format(id=args.target)}")
        print("(add --confirm to delete)")
        return 0
    ok = all(delete(session, LINK.format(id=link["id"])) for link in links)
    if found:
        ok = delete(session, CONNECTOR.format(id=args.target)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
