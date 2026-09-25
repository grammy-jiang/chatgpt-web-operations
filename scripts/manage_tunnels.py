#!/usr/bin/env python3
"""Manage OpenAI Platform tunnels separately from ChatGPT apps and links.

    manage_tunnels.py list --organization ORG
    manage_tunnels.py get TUNNEL_ID
    manage_tunnels.py create NAME --organization ORG [--workspace WORKSPACE]
    manage_tunnels.py update TUNNEL_ID --name NAME [--apply]
    manage_tunnels.py delete TUNNEL_ID [--expect-name NAME] [--confirm]

Create acts unless --dry-run; update and delete are previews unless --apply
or --confirm. JSON output contains metadata only. --id-only on create prints
the verified tunnel id for a caller. No daemon or connector is changed.

The request shapes match tunnel-client v0.0.12. Unlike a ChatGPT connector,
a tunnel lives at https://api.openai.com/v1/tunnels. Management needs an
OpenAI admin key or an authorized Platform dashboard session token, not a
ChatGPT bearer or the daemon's runtime key. Runtime credentials work for get.
See references/tunnels.md for credentials, ownership and live-test status.

Exit 0: completed, verified or previewed. Exit 1: request/read-back failed.
Exit 2: invalid arguments, missing credentials or a name-guard refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from _common import ensure_venv

BASE = "https://api.openai.com"
TUNNELS = "/v1/tunnels"
TUNNEL_ID = re.compile(r"tunnel_[0-9a-f]{32}\Z")
CONFIG = Path.home() / ".config" / "tunnel-client"


class TunnelError(RuntimeError):
    """An operator-facing error that contains no credential or response body."""


def env_file_value(path: Path, name: str) -> str:
    """Read one value from an owner-only env file, without evaluating shell code."""
    if not path.exists():
        return ""
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            f"credential file must be owned by this user with mode 600: {path}"
        )
    for line in path.read_text().splitlines():
        fields = shlex.split(line, comments=True)
        if fields and fields[0] == "export":
            fields = fields[1:]
        if len(fields) == 1 and fields[0].startswith(name + "="):
            return fields[0].split("=", 1)[1].strip()
    return ""


def credential(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve only credentials intended for this API; never print their values."""
    mode = args.auth
    if mode in ("auto", "dashboard"):
        token = os.environ.get("OPENAI_DASHBOARD_TOKEN", "").strip()
        command = os.environ.get("OPENAI_DASHBOARD_TOKEN_CMD", "")
        if not token and command:
            try:
                result = subprocess.run(
                    ["bash", "-c", command], capture_output=True, text=True, timeout=30
                )
            except subprocess.TimeoutExpired:
                raise ValueError("dashboard-token command timed out") from None
            if result.returncode:
                raise ValueError("dashboard-token command failed; output withheld")
            token = result.stdout.strip()
        if token:
            return token, "dashboard"
    if mode in ("auto", "admin"):
        token = os.environ.get("OPENAI_ADMIN_KEY", "").strip() or env_file_value(
            Path(args.admin_env).expanduser(), "OPENAI_ADMIN_KEY"
        )
        if token:
            return token, "admin"
    if mode in ("auto", "runtime") and args.command == "get":
        token = os.environ.get("CONTROL_PLANE_API_KEY", "").strip() or env_file_value(
            Path(args.runtime_env).expanduser(), "CONTROL_PLANE_API_KEY"
        )
        if token:
            return token, "runtime"
    raise ValueError(
        "no suitable Platform credential: set OPENAI_ADMIN_KEY or configure "
        "OPENAI_DASHBOARD_TOKEN(_CMD). Runtime credentials permit get only. "
        "See references/tunnels.md; do not paste credentials into a chat."
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the Platform Authorization header to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PlatformSession:
    def __init__(self, token: str, organization: str = "") -> None:
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("Platform credential is empty or has multiple lines")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if organization:
            self.headers["OpenAI-Organization"] = organization
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        if not path.startswith(TUNNELS) or "#" in path:
            raise ValueError("only Platform tunnel paths are allowed")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            BASE + path, data=data, headers=self.headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return status, None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TunnelError(f"{method} {path}: network request failed") from None
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except (ValueError, UnicodeError):
            raise TunnelError(f"{method} {path}: response was not JSON") from None


def require_record(status: int, body: Any, action: str) -> dict[str, Any]:
    if status not in (200, 201) or not isinstance(body, dict):
        raise TunnelError(f"{action} failed: HTTP {status}")
    if not TUNNEL_ID.fullmatch(str(body.get("id", ""))):
        raise TunnelError(f"{action} returned no valid tunnel id")
    return body


def read_tunnel(session: PlatformSession, tunnel_id: str) -> dict[str, Any]:
    status, body = session.call(f"{TUNNELS}/{tunnel_id}")
    record = require_record(status, body, f"get {tunnel_id}")
    if record["id"] != tunnel_id:
        raise TunnelError(f"get {tunnel_id} returned a different id")
    return record


def verify_fields(record: dict[str, Any], expected: dict[str, Any]) -> None:
    mismatches = [key for key, value in expected.items() if record.get(key) != value]
    if mismatches:
        raise TunnelError(
            f"tunnel {record['id']} read-back mismatch: {', '.join(mismatches)}"
        )


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def run(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    organization = args.organization or os.environ.get("TUNNEL_ORG", "")
    if args.command in ("create", "list") and not organization:
        raise ValueError("--organization or TUNNEL_ORG is required")
    if args.command == "create":
        body = {
            "name": args.name,
            "description": args.description,
            "organization_ids": [organization],
        }
        if args.workspace:
            body["workspace_ids"] = args.workspace
        if args.dry_run:
            emit({"method": "POST", "path": TUNNELS, "body": body})
            return 0
    if args.command == "update":
        body = {
            key: getattr(args, key)
            for key in ("name", "description")
            if getattr(args, key) is not None
        }
        if not body:
            raise ValueError("update needs --name or --description")
    if getattr(args, "tunnel_id", "") and not TUNNEL_ID.fullmatch(args.tunnel_id):
        raise ValueError("expected tunnel_<32 lowercase hex digits>")
    token, _source = credential(args)
    session = PlatformSession(token, organization)
    if args.command == "list":
        path = TUNNELS + "?" + urllib.parse.urlencode({"organization_id": organization})
        status, response = session.call(path)
        if status != 200 or not isinstance(response, (dict, list)):
            raise TunnelError(f"list tunnels failed: HTTP {status}")
        emit(response)  # Keep any paging metadata. Do not guess a cursor parameter.
        return 0
    if args.command == "create":
        status, response = session.call(TUNNELS, "POST", body)
        created = require_record(status, response, "create tunnel")
        tunnel_id = created["id"]
        try:
            record = read_tunnel(session, tunnel_id)
            verify_fields(record, body)
        except TunnelError as error:
            raise TunnelError(
                f"created {tunnel_id}; verification failed: {error}"
            ) from None
        if args.id_only:
            print(tunnel_id)
        else:
            emit(record)
        print("Allow 25–30 seconds before using the new tunnel.", file=sys.stderr)
        return 0
    record = read_tunnel(session, args.tunnel_id)
    if args.command == "get":
        emit(record)
        return 0
    if args.expect_name is not None and record.get("name") != args.expect_name:
        raise ValueError("tunnel name does not match --expect-name; no change made")
    path = f"{TUNNELS}/{args.tunnel_id}"
    if args.command == "update":
        if not args.apply:
            emit(
                {
                    "method": "POST",
                    "path": path,
                    "current_name": record.get("name"),
                    "body": body,
                }
            )
            return 0
        status, _response = session.call(path, "POST", body)
        if status != 200:
            raise TunnelError(f"update {args.tunnel_id} failed: HTTP {status}")
        record = read_tunnel(session, args.tunnel_id)
        verify_fields(record, body)
        emit(record)
        return 0
    if not args.confirm:
        emit({"method": "DELETE", "path": path, "current_name": record.get("name")})
        return 0
    status, _response = session.call(path, "DELETE")
    if status not in (200, 204):
        raise TunnelError(f"delete {args.tunnel_id} failed: HTTP {status}")
    status, _response = session.call(path)
    if status != 404:
        raise TunnelError(
            f"deleted {args.tunnel_id}; absence not confirmed (HTTP {status})"
        )
    emit({"id": args.tunnel_id, "deleted": True, "verified": True})
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("list", "get", "create", "update", "delete"):
        cmd = sub.add_parser(name)
        cmd.add_argument(
            "--organization", default="", help="Platform org id; defaults to TUNNEL_ORG"
        )
        cmd.add_argument(
            "--auth", choices=("auto", "admin", "dashboard", "runtime"), default="auto"
        )
        cmd.add_argument("--admin-env", default=str(CONFIG / "admin.env"))
        cmd.add_argument("--runtime-env", default=str(CONFIG / "binnacle-tunnel.env"))
        if name in ("get", "update", "delete"):
            cmd.add_argument("tunnel_id")
        if name == "create":
            cmd.add_argument("name")
            cmd.add_argument("--description", default="")
            cmd.add_argument("--workspace", action="append", default=[])
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--id-only", action="store_true")
        if name == "update":
            cmd.add_argument("--name")
            cmd.add_argument("--description")
            cmd.add_argument("--apply", action="store_true")
        if name == "delete":
            cmd.add_argument("--confirm", action="store_true")
        if name in ("update", "delete"):
            cmd.add_argument("--expect-name")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except TunnelError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main())
