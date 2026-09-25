#!/usr/bin/env python3
"""Delete (uninstall) a custom MCP app connector and everything attached to it.

    delete_connector.py asdk_app_<id> --confirm [--dry-run]

"Plugin actions -> Uninstall" on the plugin page sends
``DELETE aip/connectors/<asdk_app id>`` and answers ``{}``; the app, its plugin
release and its link disappear together (``connectors/batch`` returns nothing
for either id afterwards). Measured 2026-09-25. Irreversible, hence
``--confirm``; ``--dry-run`` prints the request and looks the id up first.
"""

from __future__ import annotations

import argparse
import sys

from _common import ensure_venv, open_session

CONNECTOR = "/backend-api/aip/connectors/{id}"
BATCH = "/backend-api/aip/connectors/batch"


def main() -> int:
    ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("connector_id", help="asdk_app_<32hex>")
    ap.add_argument("--confirm", action="store_true", help="really delete")
    ap.add_argument("--dry-run", action="store_true", help="look the connector up and print the request")
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args()
    if not args.connector_id.startswith("asdk_app_"):
        print("expected an asdk_app_<32hex> id (see list_connectors.py); first-party connectors are not deleted here")
        return 2
    session = open_session(args.browser)
    status, body = session.session.call(BATCH, method="POST", payload={"connector_ids": [args.connector_id], "include_actions": False})
    found = (body.get("connectors") or []) if status == 200 and isinstance(body, dict) else []
    if not found:
        print(f"no connector {args.connector_id} (HTTP {status})")
        return 1
    rec = found[0]
    print(f"target: {rec.get('id')}  name={rec.get('name')!r}  type={rec.get('connector_type')}  tunnel={rec.get('tunnel_id')}  created={str(rec.get('created_at') or '')[:19]}")
    if args.dry_run or not args.confirm:
        print(f"DELETE {CONNECTOR.format(id=args.connector_id)}  (add --confirm to delete)")
        return 0
    status, body = session.session.call(CONNECTOR.format(id=args.connector_id), method="DELETE")
    if status != 200:
        print(f"delete failed: HTTP {status}: {str(body)[:300]}")
        return 1
    print("deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
