#!/usr/bin/env python3
"""Pin or unpin a chat (``is_starred``).

    pin_chat.py <chat-id-or-url> [--unpin] [--apply]

Pinned chats appear in ``GET /backend-api/pins`` and carry ``is_starred:
true`` in listings. Pin and unpin are the same conversation PATCH family the
client already uses for rename, archive and delete; there is no
``starred()`` method on ``ChatGPTSession``, so this command calls
``session.session.call`` directly rather than adding one. Both directions
were captured 2026-09-20 on the sandbox project and each verified with a
read afterward (references/endpoint-discovery.md, "Captured 2026-09-20,
later"):

    PATCH /backend-api/conversation/<id>   {"is_starred": true}    Pin chat
    PATCH /backend-api/conversation/<id>   {"is_starred": false}   Unpin chat

Dry run by default: reads the conversation (``session.get_conversation``,
the same call ``read_chat.py`` makes), prints its title and current
``is_starred``, and the PATCH body a send would use, then exits 0 without
sending anything. ``--apply`` sends the PATCH, reads the conversation again,
and checks that ``is_starred`` came back the way it was asked for.

Exit 0 on a dry run, and on --apply when the PATCH answered 200 and the
read-back agreed. Exit 1 when the PATCH does not answer 200, or the
read-back disagrees (both printed). Exit 2 when <chat-id-or-url> is not a
usable conversation id, even once ``chatgpt_client.chat_id`` has stripped a
``/c/...`` URL down to whatever id it found inside it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from _common import ensure_venv, load_client, open_session

CONVERSATION = "/backend-api/conversation/{id}"

# The same shape chatgpt_client.chat_id() looks for inside a /c/ URL: hex
# digits and dashes, long enough to be a real id. Checking what chat_id()
# returns against it means a string it could not resolve to anything
# id-shaped (too short, still wrapped in a URL it did not recognise, or a
# new chat's un-resolved "WEB:..." id) is refused before a session is ever
# opened, rather than sent to the server to fail.
ID_RE = re.compile(r"^[0-9a-f-]{20,}$")


def patch_body(pin: bool) -> dict[str, bool]:
    """The PATCH body a pin or unpin sends."""
    return {"is_starred": pin}


def verify(conv: dict[str, Any], wanted: bool) -> list[str]:
    """Mismatch, if any, between the read-back ``is_starred`` and ``wanted``.

    A read-back that could not be obtained is passed in as ``{}``; its
    missing ``is_starred`` then disagrees with any wanted value, so a failed
    re-read fails the check rather than reading as a silent match.
    """
    actual = conv.get("is_starred")
    if actual == wanted:
        return []
    return [f"is_starred: wanted {wanted!r}, read back {actual!r}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("chat", help="conversation id or /c/ URL")
    ap.add_argument("--unpin", action="store_true", help="unpin instead of pin")
    ap.add_argument("--apply", action="store_true", help="without it, dry run")
    args = ap.parse_args(argv)

    cc = load_client()
    resolved = cc.chat_id(args.chat)
    if not ID_RE.match(resolved):
        print(f"{args.chat!r} is not a usable conversation id or /c/ URL")
        return 2

    pin = not args.unpin
    body = patch_body(pin)

    session = open_session()
    conv = session.get_conversation(resolved)

    print(f"{resolved}  {conv.get('title') or '(untitled)'}")
    print(f"current is_starred: {conv.get('is_starred')!r}")
    print()
    print("PATCH body:")
    print(json.dumps(body, indent=2, ensure_ascii=False))

    if not args.apply:
        verb = "unpin" if args.unpin else "pin"
        print()
        print(f"dry run: would {verb}; add --apply")
        return 0

    patch_status, patch_resp = session.session.call(
        CONVERSATION.format(id=resolved), method="PATCH", payload=body
    )
    if patch_status != 200:
        print(f"\nPATCH failed: HTTP {patch_status} {str(patch_resp)[:200]}")
        return 1

    read_status, after_body = session.session.call(CONVERSATION.format(id=resolved))
    after = after_body if read_status == 200 and isinstance(after_body, dict) else {}

    mismatches = verify(after, pin)
    if mismatches:
        print("\nread-back disagrees:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1

    done = "unpinned" if args.unpin else "pinned"
    print(f"\n{done} {resolved}: is_starred now {after.get('is_starred')!r}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
