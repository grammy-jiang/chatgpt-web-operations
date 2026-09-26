#!/usr/bin/env python3
"""Send a prompt to chatgpt.com, and by default wait for the reply.

    send_prompt.py PROMPT_FILE [PROMPT_FILE ...] [--project g-p-<id>]
                    [--effort LEVEL] [--model SLUG] [--search]
                    [--system-hint HINT] [--record-send-body PATH]
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
(so it refuses ``--project``, which only targets a new one, and refuses
more than one ``PROMPT_FILE``, since one chat cannot be two conversations).

``--effort`` and ``--model`` pin the reasoning effort and the model this
send actually uses; ``--search`` turns Web search on; ``--system-hint HINT``
(repeatable) adds any other composer "+" item by the id the system-hints endpoint
names it, e.g. ``plugin:connector_openai_deep_research`` for Deep research
(``references/endpoint-discovery.md``, the ``system_hints`` rows). All four
are pinned by rewriting the ``f/conversation`` POST body in flight, inside
this window, before it leaves the browser
(``chatgpt_client.rewrite_send_body``, applied through a route registered
in ``BrowserSender._open`` whenever effort, model, search or a hint is set)
-- the only mechanism proven to work; two other approaches were measured
and abandoned, and why is recorded in SKILL.md, "Reasoning effort". Blank
inherits whatever the account's own settings last used (``model_settings.py``
prints that record). ``--title`` renames the conversation once its id is
final -- how the sandbox tests mark their chats ``rp-test ...``.

More than one ``PROMPT_FILE`` shares a single browser window instead of one
per file, to share browser startup and page loading. Each is sent in turn
as a new chat and its id resolved the same way as a single send, all inside
that one window; only once the window is
closed does it wait for every reply, in order, and only then rename each
conversation (``--title`` becomes a numbered prefix, "``<title> 1``",
"``<title> 2``", ...). A failure sending, resolving, waiting for or renaming
one prompt is recorded in that prompt's own document under ``"error"`` and
does not stop the rest of the batch. A single ``PROMPT_FILE`` keeps today's
single-document ``--json`` shape and behaviour, including that a failure
exits before anything is written.

``--record-send-body PATH`` writes the ``f/conversation`` POST's method,
url and body to PATH (``original``/``sent`` when effort, model, search or a
hint rewrote it, else plain ``post_data``), so one real send can document
what actually left the browser (ROADMAP.md, Stage 3 items 1-2). It also
records that POST's own response stream to the sibling ``PATH.stream.txt``,
because a Deep research send's connector session id is visible only there
(WHY, ``chatgpt_client.py``'s module docstring); ``--json`` then carries
``"stream_file"`` (that sibling path, or null when ``--record-send-body``
was not given) and ``"session_id"`` (the id read back out of it with
``chatgpt_client.stream_events`` / ``find_session_id``, or null when the
file does not exist or carries none). With more than one ``PROMPT_FILE``
every send in the window writes to the same PATH, so only the last one's
body survives; point it elsewhere when that matters.

``--attach`` uploads each file through the composer's file input before
the prompt is filled (``BrowserSender.attachments`` / ``_upload_files``,
the same mechanism ``_attach_prompt`` already used to upload the prompt
text itself as a file). A missing attachment exits 2 before any session or
browser window opens, the same as a missing ``PROMPT_FILE``; ``--json``
keeps the resolved absolute paths under ``"attachments"``. The same
attachments are offered with every prompt in a batch.

Exit 0 when every prompt's send resolved to a real conversation id (and, if
waited, its reply arrived); 1 when any prompt's send, resolve, wait or
rename failed (one line naming the failure; with a single ``PROMPT_FILE``,
no traceback and nothing written to ``--json``; with more than one, the
batch continues and each failure is recorded in its own document); 2 for a
bad argument (no such prompt file, no such attachment file, an unknown
effort level, ``--chat`` together with ``--project``, or ``--chat``
together with more than one ``PROMPT_FILE``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from _common import ensure_venv, load_client, open_session

# Conservative planning allowance, not a measured browser-startup time.
# Page loading, attachments and stream recording change the duration.
# Warn before a batch is likely to outlast the shared window's budget.
SECONDS_PER_PROMPT_ESTIMATE = 180.0


def _progress(step: int, total: int, message: str) -> None:
    print(f"{step}/{total} {message}")
    logging.getLogger("send_prompt").info("%d/%d %s", step, total, message)


def _debug_log() -> None:
    """RP_LOG_FILE=<path>: append every step (this script's and the client's) with a timestamp and pid.

    A caller that captures this process's output and only keeps the exception text (the chat-scheduling benchmark
    harness) otherwise loses where a slow or failed send spent its time.
    """
    path = os.environ.get("RP_LOG_FILE")
    if not path:
        return
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s pid=%(process)d %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "prompt_files",
        nargs="+",
        metavar="PROMPT_FILE",
        help="path to the prompt text; more than one shares a single "
        "browser window and sends each as a new chat",
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
        "--system-hint",
        dest="system_hints",
        action="append",
        default=[],
        metavar="HINT",
        help="add a composer system hint (repeatable): the id the models "
        "endpoint uses, e.g. plugin:connector_openai_deep_research for "
        "Deep research (rewrites the send body, like --search)",
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
        help="continue this conversation instead of starting one "
        "(refuses more than one PROMPT_FILE)",
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
        help="rename the conversation once it resolves; with more than one "
        "PROMPT_FILE this becomes a numbered prefix, 'TEXT 1', 'TEXT 2', ...",
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
        help="write a JSON record of the send here (a list, one per "
        "prompt, when more than one PROMPT_FILE was given)",
    )
    ap.add_argument("--browser", default="chrome")
    ap.add_argument("--visible", action="store_true")
    return ap


def _stream_session_id(cc, stream_file: str | None) -> str | None:
    """The Deep research connector's session id recorded in ``stream_file``.

    ``None`` when ``--record-send-body`` was not given (``stream_file`` is
    then ``None`` too) or the sibling file does not exist (a plain send
    with nothing to stream, or the recorder itself found nothing); ``None``
    too when the file exists but carries no ``session_id`` anywhere.
    ``stream_events`` / ``find_session_id`` are pure parsers that do not
    raise on odd input.
    """
    if not stream_file or not Path(stream_file).is_file():
        return None
    text = Path(stream_file).read_text(encoding="utf-8")
    return cc.find_session_id(cc.stream_events(text))


def _send_single(
    cc,
    args: argparse.Namespace,
    prompt_path: Path,
    text: str,
    attachments: list[str],
    hints: tuple[str, ...],
) -> int:
    """Today's single-prompt path, unchanged: one window, one document, and
    any exception aborts the whole send (exit 1, nothing written to
    ``--json``). ROADMAP.md, Stage 3 items 1-2 and 4."""
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
                hints=hints,
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
                    hints=hints,
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

    stream_file = (
        f"{args.record_send_body}{cc.STREAM_SUFFIX}" if args.record_send_body else None
    )
    doc = {
        "conversation_id": conversation_id,
        "url": f"https://chatgpt.com/c/{conversation_id}",
        "model": args.model,
        "effort": args.effort,
        "project": args.project,
        "search": args.search,
        "system_hints": list(args.system_hints),
        "send_body_file": args.record_send_body or None,
        "stream_file": stream_file,
        "session_id": _stream_session_id(cc, stream_file),
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


def _send_batch(
    cc,
    args: argparse.Namespace,
    prompts: list[tuple[Path, str]],
    attachments: list[str],
    hints: tuple[str, ...],
) -> int:
    """More than one PROMPT_FILE: one window for the whole batch.

    Sent one after another as new chats, each resolved right away (the
    listing snapshot a resolve needs must be taken, and read back, before
    the next send or two provisional ids race each other); only once the
    window is closed does it wait for every reply, in order, then rename.
    A per-prompt failure is recorded in that prompt's own document and does
    not stop the rest (ROADMAP.md, Stage 3, batch sends in one window)."""
    n = len(prompts)
    estimated = n * SECONDS_PER_PROMPT_ESTIMATE
    if estimated > cc.BROWSER_MAX_SECONDS:
        print(
            f"warning: {n} prompts x ~{SECONDS_PER_PROMPT_ESTIMATE:g} s could "
            f"exceed the {cc.BROWSER_MAX_SECONDS:g} s browser budget "
            "(RP_BROWSER_MAX_SECONDS); the window may be killed mid-batch"
        )

    per_prompt_steps = 2  # send, then resolve (or the failure that stands in)
    if not args.no_wait:
        per_prompt_steps += 1
    if args.title:
        per_prompt_steps += 1
    total = n * per_prompt_steps
    step = 0

    sent_ats: list[str] = [""] * n
    conversation_ids: list[str | None] = [None] * n
    replies: list[str | None] = [None] * n
    errors: list[str | None] = [None] * n

    session = open_session(args.browser)
    try:
        with (
            cc.new_chat_lock(),
            cc.BrowserSender(
                args.browser,
                project=args.project,
                effort=args.effort,
                model=args.model,
                visible=args.visible,
                search=args.search,
                hints=hints,
                record_send_body=args.record_send_body,
                attachments=attachments,
            ) as sender,
        ):
            for i, (prompt_path, text) in enumerate(prompts):
                label = f"({i + 1}/{n})"
                sent_ats[i] = datetime.now(UTC).isoformat(timespec="seconds")
                step += 1
                _progress(
                    step, total, f"sending {prompt_path.name} {label} as a new chat..."
                )
                try:
                    known_ids = {str(c.get("id")) for c in session.list_conversations()}
                    since = time.time()
                    conversation_id = sender.send(text, name=prompt_path.stem)
                    step += 1
                    if cc.is_provisional(conversation_id):
                        _progress(
                            step,
                            total,
                            "resolving the new conversation id for "
                            f"{prompt_path.name} {label}...",
                        )
                        conversation_id = cc.resolve_new_conversation(
                            session, known_ids, since=since
                        )
                    else:
                        _progress(
                            step,
                            total,
                            f"{prompt_path.name} {label} already a real "
                            "conversation id",
                        )
                    conversation_ids[i] = conversation_id
                except Exception as exc:
                    step += 1
                    errors[i] = f"send failed: {str(exc)[:200]}"
                    print(f"{prompt_path.name} {label} failed: {errors[i]}")
    except Exception as exc:
        # The window itself, the lock or the session never got as far as
        # any per-prompt attempt: nothing was sent, so every prompt still
        # without its own error shares this one.
        shared = f"send failed: {str(exc)[:200]}"
        print(shared)
        for i in range(n):
            if conversation_ids[i] is None and errors[i] is None:
                errors[i] = shared

    if not args.no_wait:
        for i, (prompt_path, _text) in enumerate(prompts):
            if conversation_ids[i] is None:
                continue
            label = f"({i + 1}/{n})"
            step += 1
            _progress(
                step, total, f"waiting for the reply to {prompt_path.name} {label}..."
            )
            try:
                replies[i] = cc.wait_for_reply(
                    session, conversation_ids[i], timeout=args.timeout
                )
                print(replies[i])
            except Exception as exc:
                errors[i] = f"send failed: {str(exc)[:200]}"
                print(f"{prompt_path.name} {label} failed: {errors[i]}")

    if args.title:
        for i, (prompt_path, _text) in enumerate(prompts):
            if conversation_ids[i] is None or errors[i] is not None:
                continue
            label = f"({i + 1}/{n})"
            step += 1
            title_i = f"{args.title} {i + 1}"
            _progress(
                step, total, f"renaming {prompt_path.name} {label} to {title_i!r}..."
            )
            try:
                session.rename(conversation_ids[i], title_i)
            except Exception as exc:
                errors[i] = f"send failed: {str(exc)[:200]}"
                print(f"{prompt_path.name} {label} failed: {errors[i]}")

    stream_file = (
        f"{args.record_send_body}{cc.STREAM_SUFFIX}" if args.record_send_body else None
    )
    session_id = _stream_session_id(cc, stream_file)

    docs = []
    for i, (_prompt_path, _text) in enumerate(prompts):
        conversation_id = conversation_ids[i] or ""
        resolved = bool(conversation_id) and not cc.is_provisional(conversation_id)
        doc = {
            "conversation_id": conversation_id,
            "url": (
                f"https://chatgpt.com/c/{conversation_id}" if conversation_id else ""
            ),
            "model": args.model,
            "effort": args.effort,
            "project": args.project,
            "search": args.search,
            "system_hints": list(args.system_hints),
            "send_body_file": args.record_send_body or None,
            "stream_file": stream_file,
            "session_id": session_id,
            "sent_at": sent_ats[i],
            "resolved": resolved,
            "attachments": attachments,
        }
        if not args.no_wait:
            doc["reply"] = replies[i]
        if errors[i]:
            doc["error"] = errors[i]
        docs.append(doc)

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(docs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"written to {out}")

    return 1 if any(errors) else 0


def main(argv: list[str] | None = None) -> int:
    _debug_log()
    args = _build_parser().parse_args(argv)

    if args.chat and args.project:
        print(
            "--chat continues an existing conversation; --project only "
            "targets a new one"
        )
        return 2

    if args.chat and len(args.prompt_files) > 1:
        print(
            "--chat continues one existing conversation; it refuses more "
            "than one PROMPT_FILE"
        )
        return 2

    cc = load_client()

    if args.effort and args.effort not in cc.EFFORTS:
        print(f"unknown thinking effort {args.effort!r}; expected {cc.EFFORTS}")
        return 2

    # Resolved before any session or browser window opens, so a typo here is
    # a quick exit 2, the same contract as a missing PROMPT_FILE, instead of
    # a browser window that can never attach or send anything.
    prompts: list[tuple[Path, str]] = []
    for raw in args.prompt_files:
        prompt_path = Path(raw)
        if not prompt_path.is_file():
            print(f"no such prompt file: {prompt_path}")
            return 2
        prompts.append((prompt_path, prompt_path.read_text(encoding="utf-8")))

    attachments: list[str] = []
    for raw in args.attach:
        attach_path = Path(raw).expanduser()
        if not attach_path.is_file():
            print(f"no such attachment file: {attach_path}")
            return 2
        attachments.append(str(attach_path.resolve()))

    hints = tuple(args.system_hints)

    if len(prompts) == 1:
        prompt_path, text = prompts[0]
        return _send_single(cc, args, prompt_path, text, attachments, hints)
    return _send_batch(cc, args, prompts, attachments, hints)


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
