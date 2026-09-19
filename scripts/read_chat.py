#!/usr/bin/env python3
"""Inspect one conversation: is its turn finished, and what did it say?

    read_chat.py <chat-id-or-url> [--text] [--tail N]

Answers the question that decides whether a stalled run is stuck or merely
slow: the reply may already be written and waiting to be collected. Reading
is plain HTTP, so this works even while the send path is rate limited.

``--text`` prints the last assistant reply instead of the message table.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import ensure_venv, load_client, open_session, shorten, table


def turn_state(cc: Any, conv: dict[str, Any]) -> dict[str, Any]:
    """Whether the assistant has finished, and what the tail looks like."""
    _node, length, content_type, final = cc.tail_signature(conv)
    replies = cc.assistant_text_messages(conv)
    return {
        "messages": length,
        "content_type": content_type,
        "finished": bool(final and replies),
        "visible_replies": len(replies),
        "title": conv.get("title") or "(untitled)",
    }


def message_rows(cc: Any, conv: dict[str, Any], tail: int) -> list[tuple[str, ...]]:
    rows = []
    for msg in cc.chain(conv)[-tail:]:
        rows.append(
            (
                str((msg.get("author") or {}).get("role") or "?"),
                str((msg.get("content") or {}).get("content_type") or "?"),
                str(msg.get("recipient") or "all"),
                str(msg.get("status") or ""),
                shorten(cc.message_text(msg), 54),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("chat", help="conversation id or /c/ URL")
    ap.add_argument("--text", action="store_true", help="print the last reply")
    ap.add_argument("--tail", type=int, default=8)
    args = ap.parse_args(argv)

    cc = load_client()
    conv = open_session().get_conversation(cc.chat_id(args.chat))
    state = turn_state(cc, conv)

    print(f"{state['title']}")
    print(
        f"{state['messages']} message(s), last is {state['content_type']}, "
        f"{state['visible_replies']} visible reply(ies)"
    )
    print("turn finished: " + ("yes, ready to collect" if state["finished"] else "no"))

    if args.text:
        print("\n" + (cc.latest_reply(conv) or "(no visible reply yet)"))
    else:
        print()
        print(
            table(
                message_rows(cc, conv, args.tail),
                ("role", "type", "to", "status", "text"),
            )
        )
    return 0 if state["finished"] else 1


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
