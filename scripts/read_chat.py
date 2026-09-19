#!/usr/bin/env python3
"""Inspect one conversation: is its turn finished, and what did it say?

    read_chat.py <chat-id-or-url> [--text | --effort] [--tail N]

Answers the question that decides whether a stalled run is stuck or merely
slow: the reply may already be written and waiting to be collected. Reading
is plain HTTP, so this works even while the send path is rate limited.

``--text`` prints the last assistant reply instead of the message table.

``--effort`` prints, per assistant turn, ``thinking_effort`` and
``resolved_model_slug`` and whether ``search_result_groups`` / ``citations``
are non-empty, read straight from each reply's own metadata (``turn_facts``).
This is the only record of what a send actually used: the composer's cookie
only steers its label, and the account's server-side settings decide the
real ``thinking_effort`` and ``model`` a send posts (SKILL.md, "Reasoning
effort"). Use it after a send made with ``send_prompt.py --effort/--model/
--search`` to confirm the pin took.
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


def turn_facts(conv: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per visible assistant reply: its effort, model, search use.

    A pure function over the raw ``/backend-api/conversation/<id>`` payload
    -- no ``chatgpt_client`` import, so a hand-built conversation shaped
    like the real metadata is enough to test it (walks ``mapping`` /
    ``current_node`` itself, the same way ``chatgpt_client.chain`` does).

    This is how a real send's effort and search use are checked afterwards:
    the page takes ``thinking_effort`` and ``model`` from the account's own
    server-side settings, not from the composer's cookie, so the reply's
    own metadata is the only place that says what a send really used
    (SKILL.md, "Reasoning effort"; two recorded sends on 2026-09-20 both
    carried ``max`` regardless of the cookie).
    """
    mapping = conv.get("mapping") or {}
    node = conv.get("current_node")
    chain: list[dict[str, Any]] = []
    while node and node in mapping:
        chain.append(mapping[node].get("message") or {})
        node = mapping[node].get("parent")
    chain.reverse()

    facts: list[dict[str, Any]] = []
    for msg in chain:
        author = msg.get("author") or {}
        content = msg.get("content") or {}
        if author.get("role") != "assistant":
            continue
        if content.get("content_type") != "text":
            continue
        if msg.get("recipient") not in (None, "all"):
            continue
        parts = content.get("parts") or []
        text = "\n".join(p for p in parts if isinstance(p, str))
        if not text.strip():
            continue
        meta = msg.get("metadata") or {}
        facts.append(
            {
                "thinking_effort": meta.get("thinking_effort"),
                "resolved_model_slug": meta.get("resolved_model_slug"),
                "has_search": bool(meta.get("search_result_groups")),
                "has_citations": bool(meta.get("citations")),
            }
        )
    return facts


def format_turn_fact(index: int, fact: dict[str, Any]) -> str:
    """One printable line for ``turn_facts``' ``index``-th entry."""
    effort = fact["thinking_effort"] or "(none)"
    model = fact["resolved_model_slug"] or "(unknown)"
    search = "yes" if fact["has_search"] else "no"
    citations = "yes" if fact["has_citations"] else "no"
    return (
        f"turn {index}: effort={effort} model={model} "
        f"search={search} citations={citations}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("chat", help="conversation id or /c/ URL")
    ap.add_argument("--text", action="store_true", help="print the last reply")
    ap.add_argument(
        "--effort",
        action="store_true",
        help=(
            "print thinking_effort, resolved_model_slug and whether "
            "search/citations are present, per assistant turn, instead of "
            "the message table -- how a real send is verified afterwards"
        ),
    )
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
    elif args.effort:
        print()
        facts = turn_facts(conv)
        if facts:
            for i, fact in enumerate(facts, start=1):
                print(format_turn_fact(i, fact))
        else:
            print("(no assistant turns yet)")
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
