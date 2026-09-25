#!/usr/bin/env python3
"""Connect a custom MCP app (no-auth) so it becomes a usable link with tools.

    connect_connector.py asdk_app_<id> --name NAME [--dry-run]

A connector created through the "Create MCP App" form is an *app*
(``asdk_app_<32hex>``) and is not usable until it is connected: the "Connect"
button in its settings dialog posts ``aip/connectors/links/noauth`` with
``{"connector_id": ..., "name": ..., "action_names": []}`` and gets back the
link record (``link_<32hex>``, the discovered tool names, ``auth_type NONE``,
``auth_status ACTIVE``). From then on ``list_connectors.py`` and
``chatgpt-refresh --list`` show it as a normal link. Measured 2026-09-25.
Only for connectors whose Authentication is "No Auth" (the bearer, if any, is
injected by the tunnel-client profile). ``--apps-privacy full_access`` then
sends what the connect interstitial's privacy choice sends:
``PATCH aip/connectors/links/<link_id> {"apps_privacy_control": "full_access"}``
(measured 2026-09-25; the other values of that field were not observed).
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import ensure_venv, open_session

NOAUTH = "/backend-api/aip/connectors/links/noauth"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("connector_id", help="asdk_app_<32hex>")
    ap.add_argument(
        "--name", required=True, help="the link name (normally the connector's name)"
    )
    ap.add_argument(
        "--apps-privacy",
        default="",
        metavar="VALUE",
        help="set apps_privacy_control on the new link, e.g. full_access",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the request and stop")
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)
    if not args.connector_id.startswith("asdk_app_"):
        print("expected an asdk_app_<32hex> id (see list_connectors.py)")
        return 2
    payload = {"connector_id": args.connector_id, "name": args.name, "action_names": []}
    if args.dry_run:
        print(f"POST {NOAUTH} {json.dumps(payload)}")
        if args.apps_privacy:
            print(
                "PATCH /backend-api/aip/connectors/links/<new-link-id> "
                + json.dumps({"apps_privacy_control": args.apps_privacy})
            )
        return 0
    session = open_session(args.browser)
    status, body = session.session.call(NOAUTH, method="POST", payload=payload)
    if status != 200 or not isinstance(body, dict) or not body.get("id"):
        print(f"connect failed: HTTP {status}: {str(body)[:300]}")
        return 1
    print(
        f"connected: {body.get('id')}  name={body.get('name')!r}  "
        f"auth={body.get('auth_type')}  tools={len(body.get('actions') or [])}: "
        f"{', '.join(body.get('actions') or [])}"
    )
    if args.apps_privacy:
        link_path = f"/backend-api/aip/connectors/links/{body['id']}"
        status, patched = session.session.call(
            link_path,
            method="PATCH",
            payload={"apps_privacy_control": args.apps_privacy},
        )
        got = (
            (patched.get("link") or {}).get("apps_privacy_control")
            if status == 200 and isinstance(patched, dict)
            else None
        )
        if got != args.apps_privacy:
            print(f"privacy patch failed: HTTP {status}: {str(patched)[:200]}")
            return 1
        print(f"apps_privacy_control={got}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    sys.exit(main())
