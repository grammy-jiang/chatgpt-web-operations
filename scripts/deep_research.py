#!/usr/bin/env python3
"""Start, poll and collect a Deep research run.

    deep_research.py start PROMPT_FILE --conversation CONV_ID --run RUN.json
    deep_research.py start PROMPT_FILE --from-send SEND.json --run RUN.json
    deep_research.py start PROMPT_FILE --project g-p-<id> --run RUN.json
                     [--title T] [--effort LEVEL]
    deep_research.py status --run RUN.json [--wait] [--timeout 1800]
                     [--interval 60]
    deep_research.py export --run RUN.json --out FILE.docx
                     [--text FILE.md] [--type docx|pdf] [--force]

A Deep research run lives inside a "carrier conversation": an ordinary
conversation the caller owns, named by its id. ``start``, ``status`` and
``export`` all key on that id (RUN.json's ``conversation_id``, the only
key RUN.json must carry); only one research runs at a time per carrier
conversation, because a later ``start`` there replaces the state a later
``get_state`` -- so also ``status`` and ``export`` -- will return for it
(THE MEASURED FACTS, 2026-09-20, twelve live runs: "a second start on it
yields a session id whose state is unreachable"). The carrier
conversation's own messages are never added to or changed by any of
this, not by ``start``, not by polling, not by exporting (a six-turn
conversation still had six turns after a headless ``start``, though its
title was regenerated).

THE TOPIC COMES FROM THE CARRIER, NEVER FROM AN ARGUMENT: ``start``'s own
``user_query`` argument is sent, because the tool takes it, but twelve
live runs agree it is not what decides the research question -- the
carrier conversation's own content does. A carrier whose only turn was
"reply with the single word OK" produced a report titled "Handling
'Reply with the Single Word OK' Test Prompts" even though ``user_query``
asked about tide gauges. So before any ``start``, the carrier must
already contain the prompt to research, and a fresh random uuid as
``--conversation`` is refused outright (``isError``): it must be a real
conversation the caller owns. This is why ``start`` has three ways to
reach a carrier that already qualifies:

``--conversation CONV_ID`` calls the connector's own ``start`` tool
directly over ``POST /backend-api/ecosystem/call_mcp`` on a conversation
the caller says already contains the prompt --
``mcp_body("start", {"user_query": <PROMPT_FILE's text>}, conversation_id,
message_id)``, with ``message_id`` a fresh ``uuid4`` minted here, never
read back from anywhere. No browser opens and no message is ever posted;
use this once a qualifying carrier already exists -- one ``--project`` or
``--from-send`` minted earlier, or any other conversation of the
caller's own that already states the question.

``--from-send SEND.json`` is the two-step flow's second half: run
``send_prompt.py PROMPT_FILE --project g-p-<id> --json SEND.json``
first (an ordinary send -- no system hint needed, since posting the
prompt as a normal message already mints a carrier that contains it),
then point this at that document. It reads SEND.json's
``conversation_id`` (required) and ``session_id`` (carried through when
present, though ``start`` mints its own and that supersedes it -- kept
only as a fallback for the unlikely case that call answers with none),
then calls the same MCP ``start`` that ``--conversation`` does, on that
conversation.

``--project g-p-<id>`` is the one-command path: it posts PROMPT_FILE with
``send_prompt.py`` (``--no-wait``, since only the carrier's existence and
content matter here, never any reply -- a plain send, not the old
system-hinted one: a hint is what makes a turn invoke the connector as a
tool, and this no longer relies on that, so none is sent) to mint a fresh
carrier conversation containing PROMPT_FILE, records that send's own
document at ``RUN.json.send.json``, then calls the same MCP ``start`` on
the conversation ``send_prompt.py`` resolved. Minting a brand new carrier
in one command is the only reason to reach for ``--project``; once a
carrier exists, run later research on it through ``--conversation``
instead.

Give exactly one of ``--conversation``, ``--from-send`` or ``--project``.
``--title`` (a label recorded in RUN.json) and ``--effort`` (``min``,
``standard``, ``extended`` or ``max``) act only through ``--project``'s
own send -- ``--title`` renames the new chat once sent, ``--effort`` pins
the send's reasoning effort -- and do nothing for a ``start`` that never
sends anything, so both are refused with ``--conversation`` or
``--from-send``.

The new session id comes from the ``start`` result's own
``structuredContent.session_id``; a recursive ``chatgpt_client.find_key``
search over the whole result is the fallback, in case that shape moves
later. RUN.json then records ``conversation_id``, ``session_id``,
``message_id``, ``started_at``, ``prompt_file``, ``title`` and ``"mode"``
(``"headless"``, ``"send"`` or ``"from_send"``, one per ``start`` mode).
Exit 0 once RUN.json is written. Exit 1 when the start call fails over
HTTP, answers ``isError``, or carries no session id at all (each prints
what came back, truncated); also when ``--project``'s own send fails
(its exit code passes through unchanged: 1 a send, resolve or wait
failure; 2 a bad argument such as an unknown ``--effort``) or its record
cannot be read back afterwards. Exit 2 for a bad argument: no such
PROMPT_FILE, none or more than one of
``--conversation``/``--from-send``/``--project`` given, ``--title`` or
``--effort`` given without ``--project``, or ``--from-send``'s SEND.json
missing, unreadable, not valid JSON, or missing its own
``conversation_id``.

``status`` polls ``get_state`` for the run named by RUN.json and prints
the carrier conversation, the number of progress messages, the last
three reasoning titles, and DONE (a message's ``metadata.reasoning_title``
starts with "Generated report") or RUNNING. Exit 0 when done, 3 when
still running (so a caller can loop it), 1 on an MCP error or an HTTP
failure, 2 when RUN.json cannot be read or carries no ``conversation_id``
(the only key it must carry). ``--wait`` polls every ``--interval``
seconds (minimum 60, enforced -- three calls every 30 s for ten minutes
earned an HTTP 429) until done or ``--timeout`` runs out, printing a line
only when the summary changes; a 429 sleeps 120 s and tries again rather
than counting as either a change or a failure.

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
research) -- ``session_id`` is kept for the record when a caller has it,
never required, and never used to pick which research comes back:
``status`` and ``export`` send whatever RUN.json recorded, or the
conversation id itself when RUN.json has none, since the server ignores
the value either way. ``message_id`` is treated just as loosely -- "a
message_id may be any uuid" -- so a RUN.json written by hand with only
``conversation_id`` still works: a freshly minted ``uuid4`` stands in for
a missing ``message_id``, minted once per command and reused across
every MCP call that command makes (every poll of one ``--wait``, or both
calls one ``export`` makes), never regenerated mid-command.
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

# The one key every RUN.json and every send_prompt.py --json document
# ("start --from-send", and this module's own intermediate send record)
# must carry (module docstring, THE MEASURED FACTS: get_state and export
# key on conversation_id alone).
REQUIRED_CONVERSATION_KEYS: tuple[str, ...] = ("conversation_id",)

# Repeated in every subcommand's --help (module docstring, same facts).
CARRIER_NOTE = (
    "The research is hosted by the carrier conversation named by "
    "--conversation, --from-send or --project. Only one research runs at "
    "a time per conversation: a later start there replaces the state "
    "that get_state -- so also status and export -- will return. The "
    "conversation's own messages are never added to or changed."
)

# Repeated in "start"'s own --help (module docstring, same facts, rule 2:
# say plainly that the carrier's content, not user_query, decides the topic).
START_TOPIC_NOTE = (
    "The research topic comes from the carrier conversation's own "
    "content, never from user_query: the API accepts user_query, but "
    "measurement shows the carrier decides the topic regardless of what "
    "user_query says, so the carrier must already contain the prompt you "
    "want researched. --title and --effort act only through --project's "
    "send; they do nothing, and are refused, with --conversation or "
    "--from-send."
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


def run_session_arg(run: dict[str, Any]) -> str:
    """The value ``status``/``export`` send as the MCP call's own
    ``session_id`` argument: the recorded one when RUN.json has it, else
    the carrier conversation's own id.

    THE MEASURED FACTS, 2026-09-20: the server keys ``get_state`` and
    ``export`` on ``conversation_id`` alone and ignores this argument's
    value entirely, so any non-empty string satisfies it and the
    conversation id always qualifies as a stand-in.
    """
    return run.get("session_id") or run["conversation_id"]


def run_message_id(run: dict[str, Any]) -> str:
    """The value ``status``/``export`` send as the MCP call's own
    ``message_id`` argument: the recorded one when RUN.json has it, else a
    freshly minted ``uuid4`` (THE MEASURED FACTS: "A message_id may be
    any uuid.")."""
    return run.get("message_id") or str(uuid.uuid4())


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


def _load_json_object(path: str, required: tuple[str, ...]) -> dict[str, Any] | None:
    """A JSON object read from ``path``, checked for ``required`` keys;
    prints why and returns ``None`` instead of raising.

    One parser and one set of error messages, shared by RUN.json (for
    ``status``/``export``), a ``--from-send`` SEND.json, and this
    module's own intermediate ``*.send.json`` record.
    """
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
    missing = [key for key in required if not data.get(key)]
    if missing:
        print(f"{path} is missing {', '.join(missing)}")
        return None
    return data


def _load_run(path: str) -> dict[str, Any] | None:
    """RUN.json, parsed and checked for ``conversation_id`` -- the only
    key ``status`` and ``export`` need (module docstring, THE MEASURED
    FACTS): ``run_session_arg``/``run_message_id`` supply a safe stand-in
    for ``session_id``/``message_id`` when RUN.json carries neither."""
    return _load_json_object(path, REQUIRED_CONVERSATION_KEYS)


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

    ``run_session_arg(run)`` and ``run_message_id(run)`` are each resolved
    once, before the loop starts, and reused for every poll -- a RUN.json
    missing either still works, and does not mint a new ``message_id`` on
    every poll. Prints a summary line only when it changes, not on every
    poll. A 429 sleeps ``RATE_LIMIT_BACKOFF`` seconds and tries again
    instead of counting as either a change or a failure; any other
    non-200 status ends the wait at once.
    """
    session_arg = run_session_arg(run)
    message_id = run_message_id(run)
    deadline = time.monotonic() + timeout
    prev: dict[str, Any] | None = None
    while True:
        status, data = _call_mcp(
            session,
            "get_state",
            {"session_id": session_arg},
            run["conversation_id"],
            message_id,
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
    args: argparse.Namespace,
    prompt_path: Path,
    started_at: str,
    *,
    conversation_id: str = "",
    fallback_session_id: str | None = None,
    mode: str = "headless",
) -> int:
    """Call the connector's own ``start`` tool directly -- the one MCP
    call every ``start`` mode ends in (module docstring, THE MEASURED
    FACTS 2026-09-20). ``conversation_id`` defaults to ``args.conversation``
    (the ``--conversation`` path); ``--project`` and ``--from-send`` pass
    their own resolved conversation id instead. ``user_query`` is
    PROMPT_FILE's text, sent because the tool takes it, though measurement
    shows the carrier's own content, not this argument, decides the
    topic. ``fallback_session_id`` is used only when the start call
    itself carries none: ``--project`` and ``--from-send`` pass through
    whatever their own JSON document already recorded, when it did.
    """
    conversation_id = conversation_id or args.conversation
    prompt_text = prompt_path.read_text(encoding="utf-8")
    message_id = str(uuid.uuid4())

    session = open_session()
    status, data = _call_mcp(
        session, "start", {"user_query": prompt_text}, conversation_id, message_id
    )
    if status != 200:
        print(f"start failed: HTTP {status} {str(data)[:200]}")
        return 1
    if isinstance(data, dict) and data.get("isError"):
        print(f"MCP error: {_error_text(data)}")
        return 1

    session_id = start_session_id(data) or fallback_session_id
    if not session_id:
        print(f"the start call carried no session id: {str(data)[:200]}")
        return 1

    run_doc = {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "message_id": message_id,
        "started_at": started_at,
        "prompt_file": str(prompt_path),
        "title": args.title,
        "mode": mode,
    }
    out = _write_run_doc(args.run, run_doc)
    print(f"written to {out}")
    return 0


def _start_from_doc(
    args: argparse.Namespace,
    prompt_path: Path,
    started_at: str,
    doc: dict[str, Any],
    mode: str,
) -> int:
    """The shared tail of ``--project`` and ``--from-send``: both resolve
    a ``conversation_id`` (and maybe a ``session_id``) from someone
    else's JSON document, then start on it exactly like ``--conversation``
    does. ``doc`` is already validated to carry ``conversation_id``
    (``_load_json_object`` / ``REQUIRED_CONVERSATION_KEYS``)."""
    return _start_headless(
        args,
        prompt_path,
        started_at,
        conversation_id=doc["conversation_id"],
        fallback_session_id=doc.get("session_id") or None,
        mode=mode,
    )


def _start_send(args: argparse.Namespace, prompt_path: Path, started_at: str) -> int:
    """``--project``, the one-command path: ``send_prompt.py`` posts
    PROMPT_FILE as an ordinary message (``--no-wait``, since only the
    carrier's existence and content matter here) to mint a fresh carrier
    conversation containing it -- no system hint, since none is needed to
    mint a conversation with content in it. Its own exit code passes
    through unchanged. Once that carrier exists, ``_start_from_doc`` does
    the same MCP ``start`` the headless path does, on the conversation it
    resolved."""
    doc_path = f"{args.run}.send.json"

    argv = [
        str(prompt_path),
        "--project",
        args.project,
        "--no-wait",
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

    doc = _load_json_object(doc_path, REQUIRED_CONVERSATION_KEYS)
    if doc is None:
        return 1
    return _start_from_doc(args, prompt_path, started_at, doc, mode="send")


def _start_from_send(
    args: argparse.Namespace, prompt_path: Path, started_at: str
) -> int:
    """``--from-send``, the two-step flow's second half: read the
    ``send_prompt.py --json`` document named by ``--from-send`` (a prior,
    separate ``send_prompt.py`` call already minted the carrier and put
    the prompt in it), then start on the conversation it names exactly
    like ``--conversation`` does. A missing, unreadable, invalid or
    conversation_id-less document is a bad argument here (exit 2, before
    any session opens), unlike the internal document ``--project`` writes
    for itself, which is a runtime failure (exit 1) at that point."""
    doc = _load_json_object(args.from_send, REQUIRED_CONVERSATION_KEYS)
    if doc is None:
        return 2
    return _start_from_doc(args, prompt_path, started_at, doc, mode="from_send")


def _cmd_start(args: argparse.Namespace) -> int:
    modes = [
        flag
        for flag, value in (
            ("--conversation", args.conversation),
            ("--from-send", args.from_send),
            ("--project", args.project),
        )
        if value
    ]
    if len(modes) != 1:
        detail = f" (got {' and '.join(modes)})" if modes else ""
        print(
            "give exactly one of --conversation CONV_ID (direct, no "
            "browser), --from-send SEND.json (the two-step flow's second "
            "half) or --project g-p-<id> (one command: send_prompt.py "
            "mints the carrier, then the same MCP start)" + detail
        )
        return 2

    if (args.title or args.effort) and not args.project:
        print(
            "--title and --effort act only through --project's send; "
            "they do nothing for --conversation or --from-send and are "
            "refused there"
        )
        return 2

    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"no such prompt file: {prompt_path}")
        return 2

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    if args.project:
        return _start_send(args, prompt_path, started_at)
    if args.from_send:
        return _start_from_send(args, prompt_path, started_at)
    return _start_headless(args, prompt_path, started_at)


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
            {"session_id": run_session_arg(run)},
            run["conversation_id"],
            run_message_id(run),
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
    session_arg = run_session_arg(run)
    message_id = run_message_id(run)

    if not args.force:
        status, data = _call_mcp(
            session,
            "get_state",
            {"session_id": session_arg},
            run["conversation_id"],
            message_id,
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
        {"session_id": session_arg, "export_type": args.type},
        run["conversation_id"],
        message_id,
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
        help="start a Deep research run (--conversation, --from-send or --project)",
        description="Start a Deep research run. "
        + START_TOPIC_NOTE
        + " "
        + CARRIER_NOTE,
    )
    start.add_argument(
        "prompt_file",
        metavar="PROMPT_FILE",
        help="the text sent as user_query; the carrier's own content, not "
        "this, decides the topic",
    )
    start.add_argument(
        "--conversation",
        default="",
        metavar="CONVERSATION_ID",
        help="a conversation you own that already states the research "
        "question (direct MCP call, no browser). Give this, --from-send "
        "or --project, never more than one",
    )
    start.add_argument(
        "--from-send",
        default="",
        metavar="SEND.json",
        help="start on the conversation named by a prior send_prompt.py "
        "--json document's conversation_id (the two-step flow's second "
        "half: send that prompt yourself first, so the carrier already "
        "contains it). Give this, --conversation or --project, never "
        "more than one",
    )
    start.add_argument(
        "--project",
        default="",
        metavar="g-p-ID",
        help="mint a fresh carrier by sending PROMPT_FILE there first "
        "(send_prompt.py), then start on it -- the one-command path, and "
        "the only reason to use this. Give this, --conversation or "
        "--from-send, never more than one",
    )
    start.add_argument(
        "--title",
        default="",
        metavar="T",
        help="a label recorded in RUN.json, and the chat's new name once "
        "sent -- --project only, refused otherwise",
    )
    start.add_argument(
        "--effort",
        default="",
        metavar="LEVEL",
        help="min|standard|extended|max, pins --project's own send -- "
        "--project only, refused otherwise",
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
