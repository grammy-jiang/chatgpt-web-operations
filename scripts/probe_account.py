#!/usr/bin/env python3
"""Does the ChatGPT account answer at all? Read-only, three calls.

Run this before concluding anything is blocked. It exercises the *read* path
only, which is the point: a send-path rate limit leaves reads working, so a
clean result here means replies already generated can still be collected.

    python3 scripts/probe_account.py   # switches to the skill's .venv itself

Exit 0 when the account answers normally, 1 when it does not.
"""

import chatgpt_client as cc
from _common import ensure_venv


def main() -> int:
    try:
        session = cc.ChatGPTSession("chrome")
    except Exception as exc:
        print(f"NOT CLEAR: could not authenticate: {str(exc)[:200]}")
        return 1

    status, _ = session.session.call("/backend-api/me")
    if status != 200:
        print(f"NOT CLEAR: /backend-api/me returned {status}")
        return 1

    try:
        chats = session.list_conversations(limit=3)
    except Exception as exc:
        print(f"NOT CLEAR: listing failed: {str(exc)[:200]}")
        return 1

    print(f"CLEAR: authenticated, /backend-api/me 200, listing returned {len(chats)}")
    print("Reads work. A composer that still refuses prompts is a send-path limit.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main())
