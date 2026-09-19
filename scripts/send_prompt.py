#!/usr/bin/env python3
"""Send a prompt to chatgpt.com, and by default wait for the reply.

    send_prompt.py PROMPT_FILE [--project g-p-<id>] [--effort LEVEL]
                    [--model SLUG] [--chat ID] [--attach FILE ...]
                    [--title TEXT] [--no-wait] [--timeout SECONDS]
                    [--json PATH] [--browser chrome] [--visible]

This is the skill's own send entry point (ROADMAP.md, "Decisions of
2026-09-20", #2): the research-pipeline orchestrator is not modified any
more, so a run or a test sends through here instead. It opens one browser
window (``chatgpt_client.BrowserSender``) to post the message, then goes
back to plain HTTP for everything else. A new chat's id starts out
provisional (``WEB:...``) and is resolved by listing the account for the
conversation that appeared since the send, under ``new_chat_lock`` so two
concurrent new-chat sends cannot claim each other's conversation.
``--chat`` continues an existing conversation instead, and needs no resolve
(so it refuses ``--project``, which only targets a new one).

``--effort`` and ``--model`` pin the reasoning effort and the model the
composer uses, through ``with_effort`` / ``with_model``; blank inherits
whatever the profile last used. ``--title`` renames the conversation once
its id is final -- how the sandbox tests mark their chats ``rp-test ...``.

``--attach`` is accepted and recorded in ``--json``, but is **not**
uploaded. Attaching arbitrary files is ROADMAP.md's B4, a separate Stage 3
item that still needs one measured send on a real account before it is
designed; this repository's file-ownership rule for this change also does
not list a client addition for it. The only existing upload mechanism,
``BrowserSender._attach_prompt``, is reachable solely from inside
``_send``'s own page load (it uploads the prompt text itself as a file when
that text is too large to paste), not after a send has already completed,
so there is no seam to attach an arbitrary file through today without
either changing the client beyond what is authorised here or re-implementing
the fragile send flow outside it. Say so rather than guess at either.

Exit 0 when the send resolved to a real conversation id (and, if waited,
the reply arrived); 1 when the send, the resolve, the rename or the wait
failed (one line naming the failure, no traceback); 2 for a bad argument
(no such prompt file, an unknown effort level, or ``--chat`` together with
``--project``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from _common import ensure_venv, load_client, open_session


def _progress(step: int, total: int, message: str) -> None:
    print(f"{step}/{total} {message}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "prompt_file", metavar="PROMPT_FILE", help="path to the prompt text"
    )
    ap.add_argument(
        "--project",
        default="",
        metavar="g-p-ID",
        help="compose inside this project (a new chat only)",
    )
    ap.add_argument(
        "--effort",
        default="",
        metavar="LEVEL",
        help="min|standard|extended|max; blank inherits the profile's own",
    )
    ap.add_argument(
        "--model",
        default="",
        metavar="SLUG",
        help="model slug pinned via the cookie; blank inherits the profile's own",
    )
    ap.add_argument(
        "--chat",
        default="",
        metavar="ID",
        help="continue this conversation instead of starting one",
    )
    ap.add_argument(
        "--attach",
        nargs="+",
        default=[],
        metavar="FILE",
        help="recorded in --json; not yet uploaded (ROADMAP.md, Stage 3, B4)",
    )
    ap.add_argument(
        "--title",
        default="",
        metavar="TEXT",
        help="rename the conversation once it resolves",
    )
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the reply")
    ap.add_argument(
        "--timeout",
        type=float,
        default=1500.0,
        metavar="SECONDS",
        help="how long to wait for the reply",
    )
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write a JSON record of the send here",
    )
    ap.add_argument("--browser", default="chrome")
    ap.add_argument("--visible", action="store_true")
    args = ap.parse_args(argv)

    if args.chat and args.project:
        print(
            "--chat continues an existing conversation; --project only "
            "targets a new one"
        )
        return 2

    cc = load_client()

    if args.effort and args.effort not in cc.EFFORTS:
        print(f"unknown thinking effort {args.effort!r}; expected {cc.EFFORTS}")
        return 2

    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"no such prompt file: {prompt_path}")
        return 2

    text = prompt_path.read_text(encoding="utf-8")
    attachments = list(args.attach)
    if attachments:
        print(
            f"note: {len(attachments)} attachment(s) recorded in --json but not "
            "uploaded (ROADMAP.md, Stage 3, B4 is not implemented yet)"
        )

    steps = ["send"]
    if not args.chat:
        steps.append("resolve")
    if args.title:
        steps.append("rename")
    if not args.no_wait:
        steps.append("wait")
    total = len(steps)
    step = 0

    sent_at = datetime.now(UTC).isoformat(timespec="seconds")
    conversation_id = ""
    reply: str | None = None

    try:
        session = open_session(args.browser)
        if args.chat:
            step += 1
            _progress(step, total, f"sending to {args.chat}...")
            with cc.BrowserSender(
                args.browser,
                project=args.project,
                effort=args.effort,
                model=args.model,
                visible=args.visible,
            ) as sender:
                conversation_id = sender.send(
                    text, chat=args.chat, name=prompt_path.stem
                )
        else:
            with cc.new_chat_lock():
                known_ids = {str(c.get("id")) for c in session.list_conversations()}
                since = time.time()
                step += 1
                _progress(step, total, "sending as a new chat...")
                with cc.BrowserSender(
                    args.browser,
                    project=args.project,
                    effort=args.effort,
                    model=args.model,
                    visible=args.visible,
                ) as sender:
                    conversation_id = sender.send(text, name=prompt_path.stem)
                step += 1
                if cc.is_provisional(conversation_id):
                    _progress(step, total, "resolving the new conversation id...")
                    conversation_id = cc.resolve_new_conversation(
                        session, known_ids, since=since
                    )
                else:
                    _progress(step, total, "already a real conversation id")

        if args.title:
            step += 1
            _progress(step, total, f"renaming to {args.title!r}...")
            session.rename(conversation_id, args.title)

        if not args.no_wait:
            step += 1
            _progress(step, total, "waiting for the reply...")
            reply = cc.wait_for_reply(session, conversation_id, timeout=args.timeout)
            print(reply)
    except Exception as exc:  # BrowserSender, resolve and wait all raise here
        print(f"send failed: {str(exc)[:200]}")
        return 1

    doc = {
        "conversation_id": conversation_id,
        "url": f"https://chatgpt.com/c/{conversation_id}",
        "model": args.model,
        "effort": args.effort,
        "project": args.project,
        "sent_at": sent_at,
        "resolved": not cc.is_provisional(conversation_id),
        "attachments": attachments,
    }
    if not args.no_wait:
        doc["reply"] = reply

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"written to {out}")

    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
