#!/usr/bin/env python3
"""Send a prompt to chatgpt.com, and by default wait for the reply.

    send_prompt.py PROMPT_FILE [--project g-p-<id>] [--effort LEVEL]
                    [--model SLUG] [--search] [--record-send-body PATH]
                    [--chat ID] [--attach FILE ...]
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

``--effort`` and ``--model`` pin the reasoning effort and the model this
send actually uses; ``--search`` turns Web search on. All three are pinned
by rewriting the ``f/conversation`` POST body in flight, inside this
window, before it leaves the browser (``chatgpt_client.rewrite_send_body``,
applied through a route registered in ``BrowserSender._open`` whenever
effort, model or search is set) -- the only mechanism proven to work; two
other approaches were measured and abandoned, and why is recorded in
SKILL.md, "Reasoning effort". Blank inherits whatever the account's own
settings last used (``model_settings.py`` prints that record). ``--title``
renames the conversation once its id is final -- how the sandbox tests
mark their chats ``rp-test ...``.

``--record-send-body PATH`` writes the ``f/conversation`` POST's method,
url and body to PATH (``original``/``sent`` when effort, model or search
rewrote it, else plain ``post_data``), so one real send can document what
actually left the browser (ROADMAP.md, Stage 3 items 1-2).

``--attach`` uploads each file through the composer's file input before
the prompt is filled (``BrowserSender.attachments`` / ``_upload_files``,
the same mechanism ``_attach_prompt`` already used to upload the prompt
text itself as a file). A missing attachment exits 2 before any session or
browser window opens, the same as a missing ``PROMPT_FILE``; ``--json``
keeps the resolved absolute paths under ``"attachments"``.

Exit 0 when the send resolved to a real conversation id (and, if waited,
the reply arrived); 1 when the send, the resolve, the rename or the wait
failed (one line naming the failure, no traceback); 2 for a bad argument
(no such prompt file, no such attachment file, an unknown effort level, or
``--chat`` together with ``--project``).
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
        help="min|standard|extended|max; blank inherits the account's own",
    )
    ap.add_argument(
        "--model",
        default="",
        metavar="SLUG",
        help="model slug, pinned by rewriting the send body; blank inherits "
        "the account's own",
    )
    ap.add_argument(
        "--search",
        action="store_true",
        help="turn on Web search for this send (rewrites the send body)",
    )
    ap.add_argument(
        "--record-send-body",
        default="",
        metavar="PATH",
        help="write the f/conversation POST's method, url and body to PATH",
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
        help="upload through the composer before the prompt is sent; "
        "recorded in --json as absolute paths",
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

    # Resolved before any session or browser window opens (open_session is
    # below, inside the try:), so a typo here is a quick exit 2, the same
    # contract as a missing PROMPT_FILE, instead of a browser window that
    # can never attach anything.
    attachments: list[str] = []
    for raw in args.attach:
        attach_path = Path(raw).expanduser()
        if not attach_path.is_file():
            print(f"no such attachment file: {attach_path}")
            return 2
        attachments.append(str(attach_path.resolve()))

    steps = ["send"]
    if not args.chat:
        steps.append("resolve")
    if not args.no_wait:
        steps.append("wait")
    if args.title:
        steps.append("rename")
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
                search=args.search,
                record_send_body=args.record_send_body,
                attachments=attachments,
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
                    search=args.search,
                    record_send_body=args.record_send_body,
                    attachments=attachments,
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

        if not args.no_wait:
            step += 1
            _progress(step, total, "waiting for the reply...")
            reply = cc.wait_for_reply(session, conversation_id, timeout=args.timeout)
            print(reply)

        # After the reply: ChatGPT titles a new chat itself once the first
        # reply lands, and that overwrote a rename done before the wait
        # (2026-09-20, "rp-test send 1" became "Reply PONG"). With --no-wait
        # the rename still happens now and the auto-title may win later.
        if args.title:
            step += 1
            _progress(step, total, f"renaming to {args.title!r}...")
            session.rename(conversation_id, args.title)
    except Exception as exc:  # BrowserSender, resolve and wait all raise here
        print(f"send failed: {str(exc)[:200]}")
        return 1

    doc = {
        "conversation_id": conversation_id,
        "url": f"https://chatgpt.com/c/{conversation_id}",
        "model": args.model,
        "effort": args.effort,
        "project": args.project,
        "search": args.search,
        "send_body_file": args.record_send_body or None,
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
