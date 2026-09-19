#!/usr/bin/env python3
"""Does the ChatGPT account answer at all? Read-only.

    python3 scripts/probe_account.py   # switches to the skill's .venv itself

Run this before concluding anything is blocked. It exercises the *read* path
only, which is the point: a send-path rate limit leaves reads working, so a
clean result here means replies already generated can still be collected.

Once the account answers, it also prints the weekly plan window and credits
from ``wham/usage``. Both are informational only: they describe rate limits
and codex-style credits, never chat sends (SKILL.md, "Reasoning effort"), so
a failed usage read is reported and never turns CLEAR into NOT CLEAR.

Exit 0 when the account answers normally, 1 when it does not.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

import chatgpt_client as cc
from _common import ensure_venv

USAGE = "/backend-api/wham/usage"


def usage_lines(payload: dict[str, Any]) -> list[str]:
    """The plan window and credits from ``wham/usage``, plus the caveat.

    Pure so the fixture and a payload with fields missing are both tested
    without a session: every field is read with a fallback, so an absent or
    reshaped key prints ``?`` instead of raising.
    """
    plan = payload.get("plan_type") or "?"
    primary = (payload.get("rate_limit") or {}).get("primary_window") or {}
    used = primary.get("used_percent")
    used_s = "?" if used is None else str(used)
    reset_at = primary.get("reset_at")
    if isinstance(reset_at, (int, float)):
        resets = (
            datetime.fromtimestamp(reset_at, UTC)
            .astimezone()
            .isoformat(timespec="seconds")
        )
    else:
        resets = "?"
    credit = payload.get("credits") or {}
    balance = credit.get("balance")
    balance_s = "?" if balance is None else str(balance)
    available = (payload.get("rate_limit_reset_credits") or {}).get("available_count")
    available_s = "?" if available is None else str(available)
    return [
        f"plan {plan}: weekly window {used_s}% used, resets {resets}",
        f"credits: balance {balance_s}, {available_s} reset credit(s)",
        "This window and these credits do not cover chat sends.",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome", help="which browser's cookies")
    args = ap.parse_args(argv)

    try:
        session = cc.ChatGPTSession(args.browser)
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

    usage_status, usage = session.session.call(USAGE)
    if usage_status != 200 or not isinstance(usage, dict):
        print(f"usage: unavailable (HTTP {usage_status})")
    else:
        for line in usage_lines(usage):
            print(line)
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
