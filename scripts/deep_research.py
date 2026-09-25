#!/usr/bin/env python3
"""Start a Deep research chat, poll it, and collect its report.

    deep_research.py start PROMPT_FILE --project g-p-<id> --run RUN.json
                     [--title T] [--effort LEVEL]
    deep_research.py status --run RUN.json [--wait] [--timeout 1800]
                     [--interval 60]
    deep_research.py fetch --run RUN.json --out REPORT.md
                     [--sources FILE] [--json FILE]
    deep_research.py export --run RUN.json --out FILE.docx
                     [--text FILE.md] [--type docx|pdf] [--force]

THE DESIGN THIS REPLACES, measured 2026-09-20: a Deep research run is an
ordinary chat that runs longer, not a connector session hosted by a
separate "carrier conversation". ``start`` sends the prompt exactly the
way the chatgpt.com page does -- one message carrying the system hint
``plugin:connector_openai_deep_research`` -- and ChatGPT attaches a
widget to that same conversation. The widget's whole state then lives
server-side on one of the conversation's own messages, so the result is
read back later over plain HTTP like any other chat: nothing has to stay
open, and no MCP call is made at all. The browser is needed only for
that one send (about 19 s with ``--no-wait``); a result was readable
194 s later in the run measured.

THE EXACT SHAPES, measured on one completed run: ``GET
/backend-api/conversations/<conversation-id>`` (plural "conversations",
no ``/messages`` suffix -- this is not ``chatgpt_client.get_conversation``'s
singular, mapping-tree endpoint) returns ``{"messages": [...], ...}``.
Exactly one message, ``author.role == "tool"``, carries
``metadata.chatgpt_sdk.widget_state``: a JSON *string* (about 105 kB)
that parses to an object with keys ``status``, ``plan``,
``report_message``, ``research_started_at``, ``research_stopped_at``,
``step_statuses_by_plan``, ``last_updated_at``,
``waiting_for_user_response_on_plan_until``.

``status`` was ``"completed"`` on the finished run; anything else means
still running. ``waiting_for_user_response_on_plan_until`` being set
means the research is waiting for the user to confirm its plan, not
running yet. ``plan`` is ``{plan_id, version, title, steps: [{id, text,
status, reason?}]}``; step statuses seen so far: ``in_progress``,
``pending`` (a finished step's own status string was not among them --
this module never interprets one, only displays it). ``report_message``
is a whole assistant message: ``content.content_type == "text"``,
``content.parts[0]`` is the finished report as native Markdown (a
"# Title", "## Executive summary", **bold**, [links](url) -- 5,297 chars
in the sample), plus ``metadata.search_result_groups`` (43 groups in the
sample), ``metadata.citations``, ``metadata.safe_urls``,
``metadata.resolved_model_slug``, and ``end_turn: true``.
``research_started_at`` / ``research_stopped_at`` are ISO strings, 80 s
apart in the sample.

``start`` sends PROMPT_FILE through ``send_prompt.main`` with
``--system-hint plugin:connector_openai_deep_research`` and ``--no-wait``
(only the conversation's existence and content matter here, never a
reply), then writes RUN.json: ``{"conversation_id", "started_at",
"prompt_file", "title", "mode": "widget"}``. No MCP call is made at all.
Exit 0 once written; 1 when the send itself fails (its own exit code
passes through, so this covers a bad argument inside the send too, e.g.
an unknown ``--effort``); 2 for a bad argument caught here first (no such
PROMPT_FILE, or an argparse error such as a missing ``--project``).

``status`` fetches the conversation and finds its widget state
(``find_widget_state``), then prints the plan's title, each step with
its status, the research's started/stopped times, and one of DONE
(``status`` ``"completed"``), WAITING FOR PLAN CONFIRMATION
(``waiting_for_user_response_on_plan_until`` set) or RUNNING. Exit 0
done, 3 running or waiting (so a caller can loop it), 1 on a failed read
-- RUN.json unreadable, the HTTP GET failing, or the conversation
carrying no widget state yet. Missing state immediately after ``start``
does not prove a failed start: use ``status --wait`` for widget startup.
An older MCP-only run also has no widget (see ``export`` below).
``--wait`` polls every ``--interval`` seconds (minimum 60,
enforced) until done or ``--timeout`` runs out, printing a status block
only when it changes; a 429 backs off 120 s and tries again rather than
counting as a change or a failure (carried over from the older polling
loop this replaces; not re-measured against this endpoint specifically).

``fetch`` writes the finished report's Markdown -- ``report_message.
content.parts[0]``, the same field ``status`` reads the widget state
from -- to ``--out``, verbatim: no conversion of any kind, byte for
byte what ChatGPT wrote, nothing added or stripped. ``--sources`` writes
``report_message.metadata.search_result_groups`` as a readable,
deduplicated list, one source per line ("domain | title | url").
``--json`` writes the whole widget state. Exit 0 written, 3 when the
research is not finished yet (``status`` is not ``"completed"``), 1 on a
failed read (the same cases as ``status``'s, plus a "completed" state
whose ``report_message`` does not resolve to report text).

``export`` returns DOCX or PDF through the connector's MCP tool. It also
collects older MCP-only research runs, which leave no widget for
``status``/``fetch``. Its default completion check still uses that older
protocol: it calls ``get_state`` first and
refuses (exit 3) until some message's ``reasoning_title`` starts with
"Generated report", unless ``--force`` skips that check, then calls
``export`` (``docx`` by default, or ``pdf``), decodes
``_meta.encoded_data`` and writes it to ``--out``; with ``--text``
(``docx`` only) it also extracts the report's plain text from
``word/document.xml``. It still keys on RUN.json's ``conversation_id``
alone, and still takes a ``session_id`` from RUN.json when one happens
to be recorded there (never required, and ignored by the server either
way) -- a ``mode: "widget"`` RUN.json from ``start`` above has neither,
so both fall back to a freshly minted ``uuid4`` / the conversation id
itself, exactly as before. Exit 0 once written, 1 on an MCP error or
HTTP failure, 3 on the not-done refusal, 2 when RUN.json cannot be read,
carries no ``conversation_id``, or ``--text`` was given with
``--type pdf``. A completed widget run may have no "Generated report"
title in this legacy state. After ``status --wait`` confirms DONE, use
``export --force`` to skip the legacy check. DOCX and PDF exports from
the widget workflow were verified on 2026-09-25.
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

import send_prompt
from _common import ensure_venv, open_session

# What the page itself sends: the composer "+" item that makes a send
# invoke the Deep Research connector as a tool and attach its widget
# (module docstring, THE EXACT SHAPES).
DEEP_RESEARCH_SYSTEM_HINT = "plugin:connector_openai_deep_research"

# The one place the widget's whole state lives (module docstring, THE
# EXACT SHAPES): plural "conversations", no "/messages" suffix, unlike
# chatgpt_client's own singular /backend-api/conversation/<id>.
CONVERSATIONS_PATH = "/backend-api/conversations/{id}"

# status/fetch's shared answer when a conversation carries no widget
# state yet: it may still be starting, or (the
# export fallback's own territory) it was started through the older MCP
# start tool, which leaves nothing here to find.
NO_WIDGET_STATE_MESSAGE = (
    "no widget state in this conversation yet: after a recent start, use "
    "'status --wait' to allow the widget to appear. If it was started "
    "through the older MCP start tool instead, use 'export' to collect it; "
    "missing state alone does not identify the start method"
)

# Repeated in start/status/fetch's own --help (module docstring, same facts).
WIDGET_NOTE = (
    "A Deep research run is an ordinary chat that runs longer. Its widget "
    "state lives on one of the conversation's own messages and is read "
    "back over plain HTTP -- see 'export' for the older, MCP-only fallback."
)

# Repeated in export's own --help (module docstring, same facts).
EXPORT_FALLBACK_NOTE = (
    "Also supports older MCP-only runs. The default completion check uses "
    "legacy get_state titles. For a widget run, first confirm DONE with "
    "status --wait, then use export --force to skip that legacy check."
)

# Every tool call export makes goes through this one endpoint, naming the
# connector by app_uri (references/endpoint-discovery.md, "Runs 4 to 6").
APP_URI = "connectors://connector_openai_deep_research"
CALL_MCP = "/backend-api/ecosystem/call_mcp"

# export's own get_state's signal that the report is ready (THE MEASURED
# FACTS of the design this replaces: observed "Generated report on Python 3
# Minor-Release Scheduling").
DONE_TITLE_PREFIX = "Generated report"

# Shared by status --wait and, historically, export's own polling: do not
# go faster than once a minute, and a 429 backs off two minutes.
MIN_INTERVAL = 60.0
RATE_LIMIT_BACKOFF = 120.0

EXPORT_TYPES = ("docx", "pdf")

# The one key every RUN.json must carry, whichever path wrote it (module
# docstring): start's own mode: "widget" RUN.json, or an export-only one
# written for the older MCP fallback.
REQUIRED_CONVERSATION_KEYS: tuple[str, ...] = ("conversation_id",)


# ---------------------------------------------------------------------------
# Pure helpers -- the widget path (start / status / fetch)
# ---------------------------------------------------------------------------


def find_widget_state(conversation: Any) -> dict[str, Any] | None:
    """The Deep research widget's own state, parsed out of the one tool
    message that carries it (module docstring, THE EXACT SHAPES: exactly
    one message, ``author.role == "tool"``, carries
    ``metadata.chatgpt_sdk.widget_state``, a JSON *string* that parses to
    the widget's state object).

    ``None`` when ``conversation`` is not shaped like one, no message
    carries a ``widget_state`` string, or that string is not valid JSON,
    or does not parse to a JSON object -- this never raises. When more
    than one message carries one, the last in the list wins (the most
    recently posted).
    """
    if not isinstance(conversation, dict):
        return None
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        return None

    found: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        if not isinstance(author, dict) or author.get("role") != "tool":
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        chatgpt_sdk = metadata.get("chatgpt_sdk")
        if not isinstance(chatgpt_sdk, dict):
            continue
        raw = chatgpt_sdk.get("widget_state")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def state_verdict(state: Any) -> dict[str, Any]:
    """``{"status", "done", "waiting", "started_at", "stopped_at",
    "steps": [...], "title"}`` out of one parsed widget state --
    everything ``status`` prints, and what ``--wait`` compares between
    polls to decide whether to print again.

    ``done`` is ``status == "completed"``; ``waiting`` is
    ``waiting_for_user_response_on_plan_until`` being set (module
    docstring: that means the research is waiting for the user to
    confirm the plan, not that it is running). ``steps`` is
    ``plan.steps``, reduced to ``{"id", "text", "status"}`` (plus
    "reason" when the step itself carries one) -- a step's own status
    string is only ever displayed here, never interpreted, since
    "completed" (or whatever a finished step reads) was not among the
    statuses this module's own measurements confirmed. ``title`` is
    ``plan.title``. A non-dict ``state``, or one missing any of these
    fields, still returns a full verdict with those fields empty or
    ``None`` rather than raising.
    """
    if not isinstance(state, dict):
        state = {}
    plan = state.get("plan")
    if not isinstance(plan, dict):
        plan = {}
    raw_steps = plan.get("steps")
    steps: list[dict[str, Any]] = []
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(step, dict):
            continue
        entry: dict[str, Any] = {
            "id": step.get("id"),
            "text": step.get("text", ""),
            "status": step.get("status", ""),
        }
        if step.get("reason"):
            entry["reason"] = step["reason"]
        steps.append(entry)

    status = state.get("status")
    return {
        "status": status,
        "done": status == "completed",
        "waiting": bool(state.get("waiting_for_user_response_on_plan_until")),
        "started_at": state.get("research_started_at"),
        "stopped_at": state.get("research_stopped_at"),
        "steps": steps,
        "title": plan.get("title"),
    }


def report_markdown(state: Any) -> str | None:
    """The finished report's Markdown, exactly as ChatGPT wrote it --
    ``report_message.content.parts[0]``, with no conversion of any kind
    (module docstring).

    ``None`` when the research has not produced a report yet, or the
    shape does not resolve to a string -- a missing or malformed
    ``report_message`` never raises. The string is returned exactly as
    found, including an empty one or one with no trailing newline: this
    is the one place in this module that must not "clean up" its input.
    """
    if not isinstance(state, dict):
        return None
    report_message = state.get("report_message")
    if not isinstance(report_message, dict):
        return None
    content = report_message.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    part = parts[0]
    return part if isinstance(part, str) else None


def sources_lines(state: Any) -> list[str]:
    """One readable ``"domain | title | url"`` line per source,
    deduplicated and in first-seen order, out of
    ``report_message.metadata.search_result_groups`` (module docstring:
    43 groups in the sample).

    Deduplication is on the exact ``(domain, title, url)`` triple, so two
    groups that both surfaced the same result collapse to one line
    without silently dropping a same-URL entry that carries a different
    title. Tolerates a missing or malformed ``report_message`` /
    ``metadata`` / ``search_result_groups`` -- an empty list, never a
    raised exception.
    """
    if not isinstance(state, dict):
        return []
    report_message = state.get("report_message")
    metadata = (
        report_message.get("metadata") if isinstance(report_message, dict) else None
    )
    groups = (
        metadata.get("search_result_groups") if isinstance(metadata, dict) else None
    )
    if not isinstance(groups, list):
        return []

    lines: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        domain = str(group.get("domain") or "")
        entries = group.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "")
            url = str(entry.get("url") or "")
            key = (domain, title, url)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{domain} | {title} | {url}")
    return lines


# ---------------------------------------------------------------------------
# Pure helpers -- the export fallback (the older MCP start/get_state/export)
# ---------------------------------------------------------------------------


def mcp_body(
    tool: str, arguments: dict[str, Any], conversation_id: str, message_id: str
) -> dict[str, Any]:
    """The ``call_mcp`` POST body for one Deep research connector tool
    call (export's fallback path only)."""
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
    """``{"messages": n, "titles": [...], "done": bool, "error": str |
    None}`` out of one ``get_state`` MCP result -- export's own fallback
    not-done check, unrelated to the widget path's ``state_verdict``.

    ``resp`` is the call's parsed body, whatever its own ``isError``: an
    error or malformed result answers with zeroed counts and ``error``
    set rather than raising, so a caller always has something to print.
    Done means some message's ``metadata.reasoning_title`` starts with
    "Generated report" (``DONE_TITLE_PREFIX``); ``titles`` keeps only the
    last three, in order.
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


def run_session_arg(run: dict[str, Any]) -> str:
    """The value export sends as the MCP call's own ``session_id``
    argument: the recorded one when RUN.json has it, else the
    conversation's own id.

    THE MEASURED FACTS (the design this replaces): the server keys
    ``get_state`` and ``export`` on ``conversation_id`` alone and ignores
    this argument's value entirely, so any non-empty string satisfies it
    and the conversation id always qualifies as a stand-in -- which is
    what a ``mode: "widget"`` RUN.json (no ``session_id`` at all) always
    falls back to.
    """
    return run.get("session_id") or run["conversation_id"]


def run_message_id(run: dict[str, Any]) -> str:
    """The value export sends as the MCP call's own ``message_id``
    argument: the recorded one when RUN.json has it, else a freshly
    minted ``uuid4`` (THE MEASURED FACTS: "A message_id may be any
    uuid.")."""
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


def _get_widget_state(
    session: Any, conversation_id: str
) -> tuple[int, dict[str, Any] | None, str | None]:
    """``(http_status, state, error)`` for one ``GET
    /backend-api/conversations/<id>``: the raw HTTP status, the widget
    state found inside it (``find_widget_state``), and an error line
    ready to print when ``state`` is ``None``.

    A non-200 status and a conversation with no widget state are the two
    ways this can fail, both described as "a failed read" (module
    docstring) by every caller except ``_wait_for_status``, which keeps
    the raw status to treat 429 specially.
    """
    http_status, data = session.session.call(
        CONVERSATIONS_PATH.format(id=conversation_id)
    )
    if http_status != 200:
        return (
            http_status,
            None,
            f"could not read the conversation: HTTP {http_status} {str(data)[:200]}",
        )
    state = find_widget_state(data)
    if state is None:
        return http_status, None, NO_WIDGET_STATE_MESSAGE
    return http_status, state, None


def _call_mcp(
    session: Any,
    tool: str,
    arguments: dict[str, Any],
    conversation_id: str,
    message_id: str,
) -> tuple[int, Any]:
    """POST ``call_mcp`` for one export tool call; the raw ``(status,
    data)``, the same shape every other command in this skill calls
    ``session.session.call`` for."""
    return session.session.call(
        CALL_MCP,
        method="POST",
        payload=mcp_body(tool, arguments, conversation_id, message_id),
    )


def _load_json_object(path: str, required: tuple[str, ...]) -> dict[str, Any] | None:
    """A JSON object read from ``path``, checked for ``required`` keys;
    prints why and returns ``None`` instead of raising.

    One parser and one set of error messages, shared by RUN.json and
    ``start``'s own intermediate ``*.send.json`` record.
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
    key every subcommand here needs (module docstring): ``run_session_arg``
    / ``run_message_id`` supply a safe stand-in for ``session_id`` /
    ``message_id`` when RUN.json (as ``start`` now writes it) carries
    neither."""
    return _load_json_object(path, REQUIRED_CONVERSATION_KEYS)


def _write_run_doc(run_path: str, run_doc: dict[str, Any]) -> Path:
    """Write RUN.json, creating its parent directory; the path written to."""
    out = Path(run_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(run_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out


def _print_status(verdict: dict[str, Any]) -> None:
    print(f"plan: {verdict['title'] or '(untitled)'}")
    if verdict["steps"]:
        for step in verdict["steps"]:
            line = f"  [{step['status'] or '?'}] {step['text']}"
            if step.get("reason"):
                line += f" -- {step['reason']}"
            print(line)
    else:
        print("  (no steps yet)")
    print(f"started: {verdict['started_at'] or '(not started)'}")
    print(f"stopped: {verdict['stopped_at'] or '(not stopped)'}")
    if verdict["done"]:
        print("DONE")
    elif verdict["waiting"]:
        print("WAITING FOR PLAN CONFIRMATION")
    else:
        print("RUNNING")


def _wait_for_status(
    session: Any,
    conversation_id: str,
    *,
    timeout: float,
    interval: float,
    sleep: Any = time.sleep,
) -> int:
    """Poll the conversation until DONE, a failed read, or ``timeout``
    runs out.

    Prints a status block only when the verdict changes since the last
    poll, not on every poll -- running and waiting both keep polling, only
    DONE stops it. A 429 sleeps ``RATE_LIMIT_BACKOFF`` seconds and tries
    again instead of counting as either a change or a failure; any other
    non-200 status ends the wait at once (exit 1).

    A conversation with no widget state yet is **not** a failure while
    waiting: ChatGPT attaches the widget a little after the send, measured
    after the initial send, so a wait that started right after
    `start` would otherwise fail on its first poll (it did, 2026-09-20).
    The wait reports it once and keeps polling; only a wait that runs out
    of time with no widget ever appearing gives up, and a single
    (non-waiting) status reports exit 1 because it cannot classify the
    missing state from a single read.
    """
    deadline = time.monotonic() + timeout
    prev: dict[str, Any] | None = None
    printed_pending = False
    while True:
        http_status, state, error = _get_widget_state(session, conversation_id)
        if http_status == 429:
            print(f"HTTP 429: backing off {RATE_LIMIT_BACKOFF:g} s before the next try")
            sleep(RATE_LIMIT_BACKOFF)
        else:
            if error:
                if state is None and http_status == 200:
                    if prev is not None or not printed_pending:
                        print("no widget state yet; the send was just made")
                        printed_pending = True
                        prev = None
                    if time.monotonic() >= deadline:
                        print(error)
                        return 1
                    sleep(interval)
                    continue
                print(error)
                return 1
            verdict = state_verdict(state)
            if verdict != prev:
                _print_status(verdict)
                prev = verdict
            if verdict["done"]:
                return 0
        if time.monotonic() >= deadline:
            print(f"status: still running after {timeout:g} s")
            return 3
        if http_status != 429:
            sleep(interval)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> int:
    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"no such prompt file: {prompt_path}")
        return 2

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    doc_path = f"{args.run}.send.json"

    argv = [
        str(prompt_path),
        "--project",
        args.project,
        "--system-hint",
        DEEP_RESEARCH_SYSTEM_HINT,
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

    run_doc = {
        "conversation_id": doc["conversation_id"],
        "started_at": started_at,
        "prompt_file": str(prompt_path),
        "title": args.title,
        "mode": "widget",
    }
    out = _write_run_doc(args.run, run_doc)
    print(f"written to {out}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    run = _load_run(args.run)
    if run is None:
        return 1
    print(f"conversation: {run['conversation_id']}")

    session = open_session()

    if not args.wait:
        _http_status, state, error = _get_widget_state(session, run["conversation_id"])
        if error:
            print(error)
            return 1
        verdict = state_verdict(state)
        _print_status(verdict)
        return 0 if verdict["done"] else 3

    interval = args.interval
    if interval < MIN_INTERVAL:
        print(
            f"--interval {interval:g} s is below the minimum; using {MIN_INTERVAL:g} s"
        )
        interval = MIN_INTERVAL
    return _wait_for_status(
        session, run["conversation_id"], timeout=args.timeout, interval=interval
    )


def _cmd_fetch(args: argparse.Namespace) -> int:
    run = _load_run(args.run)
    if run is None:
        return 1

    session = open_session()
    _http_status, state, error = _get_widget_state(session, run["conversation_id"])
    if error:
        print(error)
        return 1

    verdict = state_verdict(state)
    if not verdict["done"]:
        if verdict["waiting"]:
            print("refused: waiting for plan confirmation, not finished yet")
        else:
            print(
                "refused: the research is not finished yet "
                f"(status: {verdict['status']!r})"
            )
        return 3

    report = report_markdown(state)
    if report is None:
        print("done, but report_message carries no report text")
        return 1

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8", newline="")
    print(f"{len(report)} characters -> {out_path}")

    if args.sources:
        lines = sources_lines(state)
        sources_path = Path(args.sources).expanduser()
        sources_path.parent.mkdir(parents=True, exist_ok=True)
        sources_path.write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )
        print(f"{len(lines)} source(s) -> {sources_path}")

    if args.json:
        json_path = Path(args.json).expanduser()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"widget state -> {json_path}")

    return 0


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
                "refused: no 'Generated report' title in legacy get_state; "
                "for a widget run, confirm DONE with status --wait, then "
                "pass --force to skip the legacy check"
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
        help="send PROMPT_FILE with the Deep research hint and record the run",
        description="Start a Deep research run: an ordinary send carrying "
        "the system hint plugin:connector_openai_deep_research, the "
        "browser's only part. No MCP call is made. " + WIDGET_NOTE,
    )
    start.add_argument(
        "prompt_file",
        metavar="PROMPT_FILE",
        help="the research prompt, sent exactly as the page would send it",
    )
    start.add_argument(
        "--project",
        required=True,
        metavar="g-p-ID",
        help="compose inside this project (send_prompt.py --project); a new chat only",
    )
    start.add_argument(
        "--title",
        default="",
        metavar="T",
        help="rename the chat once sent, and record it in RUN.json",
    )
    start.add_argument(
        "--effort",
        default="",
        metavar="LEVEL",
        help="min|standard|extended|max; blank inherits the account's own",
    )
    start.add_argument(
        "--run", required=True, metavar="RUN.json", help="write the run record here"
    )

    status = sub.add_parser(
        "status",
        help="fetch the conversation, find the widget state, print progress",
        description="Fetch the conversation named by RUN.json's "
        "conversation_id and print the widget's plan, steps and DONE / "
        "WAITING FOR PLAN CONFIRMATION / RUNNING. " + WIDGET_NOTE,
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

    fetch = sub.add_parser(
        "fetch",
        help="write the finished report's Markdown, byte for byte",
        description="Write report_message.content.parts[0] verbatim to "
        "--out: no conversion of any kind, byte for byte what ChatGPT "
        "wrote. " + WIDGET_NOTE,
    )
    fetch.add_argument(
        "--run", required=True, metavar="RUN.json", help="the run written by start"
    )
    fetch.add_argument(
        "--out", required=True, metavar="REPORT.md", help="write the report here"
    )
    fetch.add_argument(
        "--sources",
        default="",
        metavar="FILE",
        help="also write a deduplicated 'domain | title | url' list, one "
        "source per line",
    )
    fetch.add_argument(
        "--json",
        default="",
        metavar="FILE",
        help="also write the whole widget state as JSON",
    )

    export = sub.add_parser(
        "export",
        help="export a research report as DOCX or PDF through MCP",
        description="Export the finished report named by RUN.json over "
        "MCP, as a base64 docx or pdf. " + EXPORT_FALLBACK_NOTE,
    )
    export.add_argument(
        "--run", required=True, metavar="RUN.json", help="the run to export"
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
        help="skip the legacy get_state completion check; confirm widget "
        "completion with status --wait first",
    )

    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "start":
        return _cmd_start(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    return _cmd_export(args)


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
