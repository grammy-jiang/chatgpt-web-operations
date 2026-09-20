#!/usr/bin/env python3
"""Start, poll and collect a Deep research run.

    deep_research.py start PROMPT_FILE --conversation CONV_ID --run RUN.json
                     [--title T]
    deep_research.py start PROMPT_FILE --project g-p-<id> --run RUN.json
                     [--title T] [--effort LEVEL]
    deep_research.py status --run RUN.json [--wait] [--timeout 1800]
                     [--interval 60]
    deep_research.py export --run RUN.json --out FILE.docx
                     [--text FILE.md] [--type docx|pdf] [--force]

A Deep research run lives inside a "carrier conversation": an ordinary
conversation the caller owns, named by its id. ``start``, ``status`` and
``export`` all key on that id (RUN.json's ``conversation_id``); only one
research runs at a time per carrier conversation, because a later
``start`` there replaces the state a later ``get_state`` -- so also
``status`` and ``export`` -- will return for it. The carrier
conversation's own messages are never added to or changed by any of
this, not by ``start``, not by polling, not by exporting (THE MEASURED
FACTS, 2026-09-20: a six-turn conversation still had six turns after a
headless ``start``, though its title was regenerated).

``start`` is headless by default: ``--conversation`` names a conversation
the caller already owns (an id the caller does not own still answers
200, with that other conversation's own unrelated state, so never guess
one), and ``start`` calls the connector's own ``start`` tool directly
over ``POST /backend-api/ecosystem/call_mcp`` -- ``mcp_body("start",
{"user_query": <PROMPT_FILE's text>}, conversation_id, message_id)``,
with ``message_id`` a fresh ``uuid4`` minted here, never read back from
anywhere. No browser opens, ``send_prompt.py`` is not called, and no
message is ever posted (THE MEASURED FACTS, 2026-09-20: a live-verified
simplification of the send-based flow ROADMAP.md records under Stage 3
item 4, "Deep research measured 2026-09-20"). The new session id comes
from the result's ``structuredContent.session_id``; a recursive
``chatgpt_client.find_key`` search over the whole result is the
fallback, in case that shape moves later (``start_session_id``). RUN.json
then records ``conversation_id``, ``session_id``, ``message_id``,
``started_at``, ``prompt_file``, ``title`` and ``"mode": "headless"``.
Exit 0 once RUN.json is written. Exit 1 when the call fails over HTTP,
answers ``isError``, or carries no session id (each prints what came
back, truncated). Exit 2 for a bad argument: no such PROMPT_FILE,
neither ``--conversation`` nor ``--project`` given, or both given.

``--project g-p-<id>`` keeps the original browser path exactly as it
was, for the one thing headless ``start`` cannot do: mint a brand new
carrier conversation. Once that conversation exists, run later research
on it through ``--conversation`` instead; there is no other reason to
send a real prompt. It sends PROMPT_FILE with the Deep research system
hint (``plugin:connector_openai_deep_research``) through
``send_prompt.py`` in process -- ``--no-wait``, because a Deep research
turn is not the assistant reply that wait is for -- reads the session
id back out of the document ``send_prompt.py`` wrote, opens a session
and reads the new conversation for the assistant ``code`` message
addressed to ``api_tool.call_tool`` (its id is the ``message_id`` every
later MCP call needs, ``references/endpoint-discovery.md``, "Runs 4 to
6"), then writes RUN.json the same way but with ``"mode": "send"``.
Before the send is attempted: exit 2 for a missing PROMPT_FILE. The
send itself: ``send_prompt.py``'s own exit code passes through
unchanged (1 a send, resolve or wait failure; 2 a bad argument such as
an unknown ``--effort``). Once the send succeeded: exit 1 when the
stream carried no session id ("the stream carried no session id") or
the conversation carries no tool-call message yet.

``status`` polls ``get_state`` for the run named by RUN.json and prints
the carrier conversation, the number of progress messages, the last
three reasoning titles, and DONE (a message's ``metadata.reasoning_title``
starts with "Generated report") or RUNNING. Exit 0 when done, 3 when
still running (so a caller can loop it), 1 on an MCP error or an HTTP
failure, 2 when RUN.json cannot be read or carries no ``conversation_id``.
``--wait`` polls every ``--interval`` seconds (minimum 60, enforced --
three calls every 30 s for ten minutes earned an HTTP 429) until done or
``--timeout`` runs out, printing a line only when the summary changes; a
429 sleeps 120 s and tries again rather than counting as either a change
or a failure.

``export`` calls ``get_state`` first and refuses with exit 3 when no
"Generated report" title exists yet, unless ``--force`` skips that check
entirely. It then calls ``export`` (``docx`` by default, or ``pdf``),
decodes ``_meta.encoded_data`` and writes it to ``--out``; with ``--text``
(``docx`` only) it also extracts the report's plain text from
``word/document.xml`` with ``zipfile`` and a regex -- paragraphs on their
own lines, tags stripped, entities unescaped -- and writes that to
``--text``. Prints the filename ``_meta.content_disposition`` names, the
byte count, and with ``--text`` the character count. Exit 0 once written,
1 on an MCP error or HTTP failure, 3 on the not-done refusal, 2 when
RUN.json cannot be read, carries no ``conversation_id``, or ``--text``
was given with ``--type pdf``.

The front conversation never receives the report (60 minutes observed);
``export`` is the only way to read it. ``get_state`` and ``export`` key
on RUN.json's ``conversation_id``, never ``session_id`` (THE MEASURED
FACTS, 2026-09-20: ``get_state`` called with one conversation's id and
another run's session id still returned the first conversation's own
research) -- ``session_id`` is kept for the record, never used to pick
which research comes back.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import uuid
import zipfile
from datetime import UTC, datetime
from html import unescape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import chatgpt_client
import send_prompt
from _common import ensure_venv, open_session

# The connector, as the composer's own "+" menu names it
# (references/endpoint-discovery.md, "Deep research through system_hints").
DEEP_RESEARCH_HINT = "plugin:connector_openai_deep_research"

# Every tool call goes through this one endpoint, naming the connector by
# app_uri (references/endpoint-discovery.md, "Runs 4 to 6").
APP_URI = "connectors://connector_openai_deep_research"
CALL_MCP = "/backend-api/ecosystem/call_mcp"

# get_state's own signal that the report is ready (THE MEASURED FACTS:
# observed "Generated report on Python 3 Minor-Release Scheduling").
DONE_TITLE_PREFIX = "Generated report"

# Three polls every 30 s for ten minutes earned an HTTP 429; --wait refuses
# to go faster than this.
MIN_INTERVAL = 60.0
RATE_LIMIT_BACKOFF = 120.0

EXPORT_TYPES = ("docx", "pdf")

_RUN_KEYS = ("conversation_id", "session_id", "message_id")

# Repeated in every subcommand's --help (module docstring, same facts).
CARRIER_NOTE = (
    "The research is hosted by the carrier conversation named by "
    "--conversation (or created fresh by --project). Only one research "
    "runs at a time per conversation: a later start there replaces the "
    "state that get_state -- so also status and export -- will return. "
    "The conversation's own messages are never added to or changed."
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def mcp_body(
    tool: str, arguments: dict[str, Any], conversation_id: str, message_id: str
) -> dict[str, Any]:
    """The ``call_mcp`` POST body for one Deep research connector tool call."""
    return {
        "app_uri": APP_URI,
        "method": "tools/call",
        "params": {"name": tool, "arguments": dict(arguments)},
        "conversation_id": conversation_id,
        "message_id": message_id,
    }


def _error_text(resp: dict[str, Any]) -> str:
    """The tool's own error text out of an ``isError`` MCP result, or a
    generic label when the result carries no text (THE MEASURED FACTS:
    ``{"_meta": {"openai/tool_error_kind": "..."}, "content": [{"type":
    "text", "text": "..."}], "isError": true}``)."""
    for item in resp.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            return str(item["text"])
    kind = (resp.get("_meta") or {}).get("openai/tool_error_kind")
    return str(kind) if kind else "MCP call failed"


def state_summary(resp: Any) -> dict[str, Any]:
    """``{"messages": n, "titles": [...], "done": bool, "error": str | None}``
    out of one ``get_state`` MCP result.

    ``resp`` is the call's parsed body, whatever its own ``isError``: an
    error or malformed result answers with zeroed counts and ``error`` set
    rather than raising, so a caller always has something to print. Done
    means some message's ``metadata.reasoning_title`` starts with
    "Generated report" (``DONE_TITLE_PREFIX``); ``titles`` keeps only the
    last three, in order, which is what ``status`` prints.
    """
    if not isinstance(resp, dict):
        return {
            "messages": 0,
            "titles": [],
            "done": False,
            "error": "malformed response",
        }
    if resp.get("isError"):
        return {"messages": 0, "titles": [], "done": False, "error": _error_text(resp)}
    messages = (resp.get("_meta") or {}).get("deep_research_widget_messages") or []
    titles = [
        title
        for msg in messages
        if isinstance(msg, dict)
        and (title := (msg.get("metadata") or {}).get("reasoning_title"))
    ]
    done = any(title.startswith(DONE_TITLE_PREFIX) for title in titles)
    return {
        "messages": len(messages),
        "titles": titles[-3:],
        "done": done,
        "error": None,
    }


def start_session_id(resp: Any) -> str | None:
    """The new session id out of one ``start`` MCP result.

    ``structuredContent.session_id`` is where THE MEASURED FACTS
    (2026-09-20) put it, so that is read directly first. A recursive
    ``chatgpt_client.find_key`` search over the whole result is the
    fallback alone, in case the connector's own response shape moves the
    id elsewhere later. ``None`` when neither finds a non-empty string,
    or ``resp`` is not shaped like a result at all -- this never raises.
    """
    if isinstance(resp, dict):
        structured = resp.get("structuredContent")
        if isinstance(structured, dict):
            session_id = structured.get("session_id")
            if isinstance(session_id, str) and session_id:
                return session_id
    return chatgpt_client.find_key([resp], "session_id")


def tool_call_message_id(conversation: dict[str, Any]) -> str | None:
    """The id of the assistant's ``code`` message addressed to
    ``api_tool.call_tool`` -- the id every later MCP call needs as its
    ``message_id`` (THE MEASURED FACTS: "an assistant message with
    content_type 'code' and recipient 'api_tool.call_tool'").

    Walks ``conversation["mapping"]`` (the same conversation shape
    ``chatgpt_client.chain`` reads, unordered here since only one such
    message is expected); ``None`` when there is no such message yet, or
    ``conversation`` is not shaped like a conversation at all.
    """
    mapping = conversation.get("mapping") if isinstance(conversation, dict) else None
    if not isinstance(mapping, dict):
        return None
    for node in mapping.values():
        msg = (node or {}).get("message") or {}
        author = msg.get("author") or {}
        content = msg.get("content") or {}
        if (
            author.get("role") == "assistant"
            and content.get("content_type") == "code"
            and msg.get("recipient") == "api_tool.call_tool"
        ):
            mid = msg.get("id")
            return str(mid) if mid else None
    return None


_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*UTF-8''([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"([^"]*)"', re.IGNORECASE)


def parse_disposition(header: str) -> str:
    """The filename inside a ``Content-Disposition`` header, or ``""``
    when none is present.

    Prefers the RFC 5987 ``filename*=UTF-8''...`` form (percent-decoded)
    over the plain quoted ``filename="..."`` form, since that is the one
    that survives a non-ASCII title; falls back to the plain form when
    only that is present (THE MEASURED FACTS: ``'attachment;
    filename="<title>.docx"; filename*=UTF-8''<title>.docx'``).
    """
    header = header or ""
    star = _FILENAME_STAR_RE.search(header)
    if star:
        return unquote(star.group(1).strip())
    plain = _FILENAME_RE.search(header)
    return plain.group(1) if plain else ""


def decode_export(resp: dict[str, Any]) -> tuple[str, bytes]:
    """``(filename, bytes)`` out of one ``export`` MCP result: the name
    from ``_meta.content_disposition`` and the file decoded from
    ``_meta.encoded_data``. Missing fields decode to ``""`` / ``b""``
    rather than raising, matching this module's other parsers."""
    meta = (resp.get("_meta") or {}) if isinstance(resp, dict) else {}
    filename = parse_disposition(meta.get("content_disposition") or "")
    encoded = meta.get("encoded_data") or ""
    return filename, base64.b64decode(encoded) if encoded else b""


_PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def docx_text(data: bytes) -> str:
    """Plain text out of a ``.docx``'s ``word/document.xml``: one
    paragraph per line, tags stripped, XML entities unescaped.

    ``zipfile`` plus a regex, not a full XML parser (THE MEASURED FACTS:
    "decoded with zipfile + word/document.xml"): each ``<w:p ...>...
    </w:p>`` becomes one line, with every ``<...>`` tag inside it removed
    before the entities are unescaped, so text split across several
    ``<w:r><w:t>`` runs in one paragraph still joins into one line.
    """
    with zipfile.ZipFile(BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    paragraphs = [
        unescape(_TAG_RE.sub("", m.group(0))) for m in _PARAGRAPH_RE.finditer(xml)
    ]
    return "\n".join(paragraphs)


# ---------------------------------------------------------------------------
# Session-calling helpers
# ---------------------------------------------------------------------------


def _call_mcp(
    session: Any,
    tool: str,
    arguments: dict[str, Any],
    conversation_id: str,
    message_id: str,
) -> tuple[int, Any]:
    """POST ``call_mcp`` for one tool call; the raw ``(status, data)``,
    the same shape every other command in this skill calls
    ``session.session.call`` for."""
    return session.session.call(
        CALL_MCP,
        method="POST",
        payload=mcp_body(tool, arguments, conversation_id, message_id),
    )


def _load_run(path: str) -> dict[str, Any] | None:
    """RUN.json, parsed and checked for the three ids every MCP call
    needs (``conversation_id`` is the one ``get_state`` and ``export``
    actually key on; module docstring, THE MEASURED FACTS); prints why
    and returns ``None`` instead of raising."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"could not read {path}: {exc}")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"{path} is not valid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        print(f"{path} is not a JSON object")
        return None
    missing = [key for key in _RUN_KEYS if not data.get(key)]
    if missing:
        print(f"{path} is missing {', '.join(missing)}")
        return None
    return data


def _write_run_doc(run_path: str, run_doc: dict[str, Any]) -> Path:
    """Write RUN.json, creating its parent directory; the path written to."""
    out = Path(run_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(run_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"messages: {summary['messages']}")
    if summary["titles"]:
        print("last reasoning titles:")
        for title in summary["titles"]:
            print(f"  - {title}")
    else:
        print("last reasoning titles: (none yet)")
    print("DONE" if summary["done"] else "RUNNING")


def _wait_for_done(
    session: Any,
    run: dict[str, Any],
    *,
    timeout: float,
    interval: float,
    sleep: Any = time.sleep,
) -> int:
    """Poll ``get_state`` until DONE, an MCP error, or ``timeout`` runs out.

    Prints a summary line only when it changes, not on every poll. A 429
    sleeps ``RATE_LIMIT_BACKOFF`` seconds and tries again instead of
    counting as either a change or a failure; any other non-200 status
    ends the wait at once.
    """
    deadline = time.monotonic() + timeout
    prev: dict[str, Any] | None = None
    while True:
        status, data = _call_mcp(
            session,
            "get_state",
            {"session_id": run["session_id"]},
            run["conversation_id"],
            run["message_id"],
        )
        if status == 429:
            print(f"HTTP 429: backing off {RATE_LIMIT_BACKOFF:g} s before the next try")
            sleep(RATE_LIMIT_BACKOFF)
        else:
            if status != 200:
                print(f"get_state failed: HTTP {status} {str(data)[:200]}")
                return 1
            summary = state_summary(data)
            if summary["error"]:
                print(f"MCP error: {summary['error']}")
                return 1
            if summary != prev:
                _print_summary(summary)
                prev = summary
            if summary["done"]:
                return 0
        if time.monotonic() >= deadline:
            print(f"status: still running after {timeout:g} s")
            return 3
        if status != 429:
            sleep(interval)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _start_headless(
    args: argparse.Namespace, prompt_path: Path, started_at: str
) -> int:
    prompt_text = prompt_path.read_text(encoding="utf-8")
    message_id = str(uuid.uuid4())

    session = open_session()
    status, data = _call_mcp(
        session, "start", {"user_query": prompt_text}, args.conversation, message_id
    )
    if status != 200:
        print(f"start failed: HTTP {status} {str(data)[:200]}")
        return 1
    if isinstance(data, dict) and data.get("isError"):
        print(f"MCP error: {_error_text(data)}")
        return 1

    session_id = start_session_id(data)
    if not session_id:
        print(f"the start call carried no session id: {str(data)[:200]}")
        return 1

    run_doc = {
        "conversation_id": args.conversation,
        "session_id": session_id,
        "message_id": message_id,
        "started_at": started_at,
        "prompt_file": str(prompt_path),
        "title": args.title,
        "mode": "headless",
    }
    out = _write_run_doc(args.run, run_doc)
    print(f"written to {out}")
    return 0


def _start_send(args: argparse.Namespace, prompt_path: Path, started_at: str) -> int:
    body_path = f"{args.run}.body.json"
    doc_path = f"{args.run}.send.json"

    argv = [
        str(prompt_path),
        "--project",
        args.project,
        "--system-hint",
        DEEP_RESEARCH_HINT,
        "--no-wait",
        "--record-send-body",
        body_path,
        "--json",
        doc_path,
    ]
    if args.title:
        argv += ["--title", args.title]
    if args.effort:
        argv += ["--effort", args.effort]

    rc = send_prompt.main(argv)
    if rc != 0:
        return rc

    try:
        doc = json.loads(Path(doc_path).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"could not read the send record at {doc_path}: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"{doc_path} is not valid JSON: {exc}")
        return 1

    conversation_id = doc.get("conversation_id")
    session_id = doc.get("session_id")
    if not session_id:
        print("the stream carried no session id")
        return 1

    session = open_session()
    conversation = session.get_conversation(conversation_id)
    message_id = tool_call_message_id(conversation)
    if not message_id:
        print(f"no tool-call message found in conversation {conversation_id}")
        return 1

    run_doc = {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "message_id": message_id,
        "started_at": started_at,
        "prompt_file": str(prompt_path),
        "title": args.title,
        "mode": "send",
    }
    out = _write_run_doc(args.run, run_doc)
    print(f"written to {out}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    if args.conversation and args.project:
        print("give --conversation or --project, never both")
        return 2
    if not args.conversation and not args.project:
        print(
            "give --conversation (headless, the default) or --project "
            "(browser path, to mint a fresh carrier conversation)"
        )
        return 2

    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"no such prompt file: {prompt_path}")
        return 2

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    if args.conversation:
        return _start_headless(args, prompt_path, started_at)
    return _start_send(args, prompt_path, started_at)


def _cmd_status(args: argparse.Namespace) -> int:
    run = _load_run(args.run)
    if run is None:
        return 2

    print(f"carrier conversation: {run['conversation_id']}")

    session = open_session()

    if not args.wait:
        status, data = _call_mcp(
            session,
            "get_state",
            {"session_id": run["session_id"]},
            run["conversation_id"],
            run["message_id"],
        )
        if status != 200:
            print(f"get_state failed: HTTP {status} {str(data)[:200]}")
            return 1
        summary = state_summary(data)
        if summary["error"]:
            print(f"MCP error: {summary['error']}")
            return 1
        _print_summary(summary)
        return 0 if summary["done"] else 3

    interval = args.interval
    if interval < MIN_INTERVAL:
        print(
            f"--interval {interval:g} s is below the minimum; using {MIN_INTERVAL:g} s"
        )
        interval = MIN_INTERVAL
    return _wait_for_done(session, run, timeout=args.timeout, interval=interval)


def _cmd_export(args: argparse.Namespace) -> int:
    if args.text and args.type != "docx":
        print("--text needs --type docx")
        return 2

    run = _load_run(args.run)
    if run is None:
        return 2

    session = open_session()

    if not args.force:
        status, data = _call_mcp(
            session,
            "get_state",
            {"session_id": run["session_id"]},
            run["conversation_id"],
            run["message_id"],
        )
        if status != 200:
            print(f"get_state failed: HTTP {status} {str(data)[:200]}")
            return 1
        summary = state_summary(data)
        if summary["error"]:
            print(f"MCP error: {summary['error']}")
            return 1
        if not summary["done"]:
            print(
                "refused: no 'Generated report' title yet (state not done); "
                "pass --force to export anyway"
            )
            return 3

    status, data = _call_mcp(
        session,
        "export",
        {"session_id": run["session_id"], "export_type": args.type},
        run["conversation_id"],
        run["message_id"],
    )
    if status != 200:
        print(f"export failed: HTTP {status} {str(data)[:200]}")
        return 1
    if data.get("isError"):
        print(f"MCP error: {_error_text(data)}")
        return 1

    filename, content = decode_export(data)
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(content)
    print(f"{filename or out_path.name}: {len(content)} bytes -> {out_path}")

    if args.text:
        text = docx_text(content)
        text_path = Path(args.text).expanduser()
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        print(f"{len(text)} characters -> {text_path}")

    return 0


# ---------------------------------------------------------------------------
# argparse and main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="start a Deep research run (headless by default)",
        description=(
            "Start a Deep research run. Headless by default: "
            "--conversation calls the connector directly over MCP, no "
            "browser, no message ever posted. --project instead opens a "
            "fresh carrier conversation through the browser first, the "
            "only reason to use it. " + CARRIER_NOTE
        ),
    )
    start.add_argument("prompt_file", metavar="PROMPT_FILE", help="the prompt to send")
    start.add_argument(
        "--conversation",
        default="",
        metavar="CONVERSATION_ID",
        help=(
            "the carrier conversation to run in (an id you own; headless, "
            "the default path). Give this or --project, never both"
        ),
    )
    start.add_argument(
        "--project",
        default="",
        metavar="g-p-ID",
        help=(
            "open a fresh carrier conversation in this project through "
            "the browser first, then start there (only needed to mint a "
            "new conversation). Give this or --conversation, never both"
        ),
    )
    start.add_argument(
        "--title",
        default="",
        metavar="T",
        help="a label recorded in RUN.json; --project also renames the chat once sent",
    )
    start.add_argument(
        "--effort",
        default="",
        metavar="LEVEL",
        help="min|standard|extended|max (--project's send only)",
    )
    start.add_argument(
        "--run", required=True, metavar="RUN.json", help="write the run record here"
    )

    status = sub.add_parser(
        "status",
        help="poll get_state and print progress plus DONE or RUNNING",
        description="Poll get_state for the run named by RUN.json. " + CARRIER_NOTE,
    )
    status.add_argument(
        "--run", required=True, metavar="RUN.json", help="the run written by start"
    )
    status.add_argument(
        "--wait", action="store_true", help="poll until done or --timeout runs out"
    )
    status.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        metavar="SECONDS",
        help="with --wait, how long to keep polling",
    )
    status.add_argument(
        "--interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="with --wait, seconds between polls (minimum 60, enforced)",
    )

    export = sub.add_parser(
        "export",
        help="export the finished report and write it to a file",
        description="Export the finished report named by RUN.json. " + CARRIER_NOTE,
    )
    export.add_argument(
        "--run", required=True, metavar="RUN.json", help="the run written by start"
    )
    export.add_argument(
        "--out", required=True, metavar="FILE", help="write the exported file here"
    )
    export.add_argument(
        "--text",
        default="",
        metavar="FILE",
        help="also write the docx's plain text here (docx only)",
    )
    export.add_argument(
        "--type", choices=EXPORT_TYPES, default="docx", help="docx (default) or pdf"
    )
    export.add_argument(
        "--force",
        action="store_true",
        help="export even when get_state shows no finished report yet",
    )

    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "start":
        return _cmd_start(args)
    if args.command == "status":
        return _cmd_status(args)
    return _cmd_export(args)


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
