#!/usr/bin/env python3
"""Create a custom MCP connector (app) bound to an OpenAI tunnel, No Auth.

    create_connector.py --name NAME --tunnel tunnel_<id> [--description TEXT] [--dry-run]

The "Create MCP App" form (developer mode on) posts JSON to
``aip/connectors/mcp``::

    {"name": NAME, "tunnel_id": "tunnel_...", "description": "", "logo_url": null,
     "auth_request": {"supported_auth": [], "oauth_client_params": null}}

and answers ``{"connector": {"id": "asdk_app_<32hex>", "connector_type": "MCP",
"tunnel_id": ..., "supported_auth": [{"type": "NONE"}], "status": "ONLY_ME",
"distribution_channel": "INDIVIDUAL", ...}}``. A name already in use answers
HTTP 409 with ``existing_connector_id``. Measured 2026-09-25 from the form.

The new app is not usable until it is connected (``connect_connector.py``),
which also discovers its tools through the tunnel; ChatGPT probes the tunnel
during creation, so the tunnel-client and the MCP server behind it must be up.
Only the tunnel + No Auth shape was captured: "Server URL" and OAuth
connectors are not supported here.
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import ensure_venv, open_session

CREATE = "/backend-api/aip/connectors/mcp"


def payload_for(name: str, tunnel_id: str, description: str) -> dict:
    return {
        "name": name,
        "tunnel_id": tunnel_id,
        "description": description,
        "logo_url": None,
        "auth_request": {"supported_auth": [], "oauth_client_params": None},
    }


def main() -> int:
    ensure_venv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--tunnel", required=True, help="tunnel_<32hex> (list_connectors.py --tunnels)")
    ap.add_argument("--description", default="")
    ap.add_argument("--dry-run", action="store_true", help="print the request and stop")
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args()
    if not args.tunnel.startswith("tunnel_"):
        print("expected a tunnel_<32hex> id")
        return 2
    payload = payload_for(args.name, args.tunnel, args.description)
    if args.dry_run:
        print(f"POST {CREATE} {json.dumps(payload)}")
        return 0
    session = open_session(args.browser)
    status, body = session.session.call(CREATE, method="POST", payload=payload)
    if status == 409:
        detail = body.get("detail") if isinstance(body, dict) else None
        existing = detail.get("existing_connector_id") if isinstance(detail, dict) else None
        print(f"name already exists: {existing or str(body)[:200]}")
        return 1
    connector = body.get("connector") if status == 200 and isinstance(body, dict) else None
    if not connector or not connector.get("id"):
        print(f"create failed: HTTP {status}: {str(body)[:300]}")
        return 1
    print(f"created: {connector['id']}  name={connector.get('name')!r}  tunnel={connector.get('tunnel_id')}  auth={[a.get('type') for a in connector.get('supported_auth') or []]}  status={connector.get('status')}")
    print(f"next: connect_connector.py {connector['id']} --name {json.dumps(connector.get('name'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
