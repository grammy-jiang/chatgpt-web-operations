#!/usr/bin/env python3
"""What does posting a message require right now? Read-only.

    probe_send_gates.py

Asks the sentinel endpoint and prints which controls are in force. Run it
when a send starts failing, or when someone proposes moving the send to
plain HTTP: the answer decides that question and it can change under us.

Send the request with an **empty** body. A body of ``{"p": ...}`` returns 500
and reads exactly like an account block, which is how one wrong diagnosis
started.

Exit 0 when a send needs only a token this can fetch, 1 when a browser is
required.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from _common import ensure_venv, load_client, open_session

SENTINEL = "https://chatgpt.com/backend-api/sentinel/chat-requirements"
BROWSER_ONLY = ("turnstile", "proofofwork", "so", "arkose")


def gates_from(body: dict) -> dict[str, bool]:
    """Which controls the response says are required."""
    return {
        name: bool((body.get(name) or {}).get("required"))
        for name in BROWSER_ONLY
        if name in body
    }


def fetch(session) -> dict:
    cc = load_client()
    request = urllib.request.Request(
        SENTINEL,
        headers={
            "Cookie": session.session.cookie,
            "User-Agent": cc._helpers().UA,
            "Accept": "application/json",
            "Authorization": "Bearer " + session.session.token,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome", help="which browser's cookies")
    args = ap.parse_args(argv)

    try:
        body = fetch(open_session(args.browser))
    except urllib.error.HTTPError as exc:
        print(f"sentinel returned {exc.code}: {exc.read().decode()[:200]}")
        return 1

    gates = gates_from(body)
    print(f"persona: {body.get('persona', 'unknown')}")
    print(f"token valid for: {body.get('expire_after', '?')} s\n")
    for name, required in sorted(gates.items()):
        print(f"  {name:12s} {'REQUIRED' if required else 'not required'}")

    blocking = sorted(n for n, required in gates.items() if required)
    if blocking:
        print(
            f"\nA browser is needed to send: {', '.join(blocking)}.\n"
            "'so' is a behavioural collector (~40 __oai_so_* page globals for\n"
            "keypress, pointer, scroll and input timing). Reproducing it outside\n"
            "a browser fabricates human-interaction telemetry; do not build that."
        )
        return 1
    print("\nNo browser-only control is in force. Re-check before relying on it.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
