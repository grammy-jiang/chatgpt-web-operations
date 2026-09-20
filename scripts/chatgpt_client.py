#!/usr/bin/env python3
"""chatgpt_client.py — deterministic access to ChatGPT web conversations.

Every read goes over plain HTTP with the bearer token of the logged-in
chatgpt.com session (cookies from the local Chrome profile, decrypted through
the GNOME keyring by the bundled ``chatgpt_session`` helper):

    ChatGPTSession.get_conversation / list_conversations / archive / rename
    wait_for_reply(session, chat_id)   poll until the assistant's turn ended
    latest_reply(conv)                  the last assistant text message

Only *sending* a message needs a browser page: ``POST /backend-api/f/conversation``
is gated by proof-of-work, Turnstile, and a behavioural collector that only the
page can satisfy (re-measured 2026-09-16, see docs/chatgpt-skill-plan.md §9.6).
``BrowserSender`` opens one scripted Chrome page (Playwright on Xvfb, no human)
for each send and closes it again.

A window costs about 1.26 GB idle, and about 1.75 GB while a large prompt is
in the composer, so it is budgeted. The limits are environment variables,
because they must bind every process that opens one, not just the
orchestrator:

    RP_MAX_BROWSERS         windows at once, across processes   (default 1)
    RP_BROWSER_MAX_SECONDS  hard ceiling on one window's life   (default 1500)
    RP_MIN_AVAILABLE_MB     refuse to open below this           (default 4000)
    RP_BROWSER_WAIT_SECONDS how long to wait for either         (default 900)

Reply parsing:

    extract_blocks(text)  -> {name: content} for ===BEGIN name=== ... ===END name===
    fenced_json(text)     -> content of the last ```json fence (fallback)
    stream_events(text)   -> the data payloads of a recorded SSE stream, any JSON type
    find_session_id(events) -> the connector session id nested in them, or None

The binnacle helpers and Playwright are imported lazily so this module can
be unit-tested without them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator, Iterable, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

# Vendored into the chatgpt-web-operations skill (see VENDORED.md). The
# cookie helpers are siblings of this file and the Playwright interpreter is
# the skill's own .venv, so nothing here depends on where the skill sits.
# Both stay overridable through the environment for experiments.
_HERE = Path(__file__).resolve().parent
HELPERS = Path(os.environ.get("CHATGPT_HELPERS_DIR", str(_HERE))).expanduser()
PW_PYTHON = os.path.expanduser(
    os.environ.get(
        "CHATGPT_SEND_PYTHON", str(_HERE.parent / ".venv" / "bin" / "python")
    )
)

BLOCK_RE = re.compile(
    r"^===BEGIN (?P<name>[^=\n]+?)===\s*\n(?P<body>.*?)\n?^===END (?P=name)===\s*$",
    re.S | re.M,
)
FENCE_RE = re.compile(r"```(?:json|jsonl|markdown|md)?\s*\n(.*?)\n```", re.S)

logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """HTTP-level failure (403/429/5xx, Cloudflare page, expired session)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Reply parsing (pure functions)
# ---------------------------------------------------------------------------


def extract_blocks(text: str) -> dict[str, str]:
    """Named ``===BEGIN name=== ... ===END name===`` blocks of a reply.

    Fenced code around a block is tolerated (the model sometimes wraps the
    whole answer in a fence); the last block wins when a name repeats.
    """
    blocks: dict[str, str] = {}
    for m in BLOCK_RE.finditer(text):
        body = m.group("body")
        stripped = body.strip()
        # Unwrap a fence that starts right after BEGIN and ends before END.
        fence = re.fullmatch(r"```[a-zA-Z]*\s*\n(.*?)\n```", stripped, re.S)
        if fence:
            body = fence.group(1)
        blocks[m.group("name").strip()] = body.strip("\n") + "\n"
    return blocks


def unclosed_block(text: str) -> str | None:
    """Name of a block that opened and never closed, if the reply has one.

    A truncated reply is the commonest way a large step fails, and it is not
    the same failure as a reply that ignored the format. msgloom Topic 08
    round 4 spent two attempts and an aborted run on the message "no
    `===BEGIN synthesis.json===` block found" while the reply in fact began
    with exactly that marker and stopped mid-JSON 20 kB later. Telling the
    two apart is the difference between "ask again" and "ask for less".
    """
    opened = [m.group(1).strip() for m in re.finditer(r"===BEGIN ([^=\n]+)===", text)]
    closed = {m.group(1).strip() for m in re.finditer(r"===END ([^=\n]+)===", text)}
    for name in reversed(opened):
        if name not in closed:
            return name
    return None


def fenced_json(text: str) -> str | None:
    """Content of the last fenced block, or the whole text when it is JSON."""
    fences = FENCE_RE.findall(text)
    if fences:
        return fences[-1].strip() + "\n"
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        return stripped + "\n"
    return None


def parse_json_text(text: str) -> Any:
    """json.loads with a fallback to the largest {...} / [...] span."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if 0 <= start < end:
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in the reply")


def stream_events(text: str) -> list[Any]:
    """Parse a recorded SSE stream (``record_send_body``'s ``.stream.txt``
    sibling, see ``BrowserSender._record_send_stream``) into its JSON payloads.

    A recorded stream is server-sent events: ``event: `` lines, blank
    lines, and ``data: `` lines -- the opening ``event: delta_encoding`` /
    ``data: "v1"``, a ``data: {"type":"resume_conversation_token",...}``
    object, a run of ``event: delta`` / ``data: {...}`` frames (a full
    message object, then content-append and patch deltas such as
    ``{"p": ..., "o": "append", "v": "..."}`` and
    ``{"o": "patch", "v": [...]}``), trailing
    ``{"type":"message_stream_complete",...}`` /
    ``{"type":"title_generation",...}`` /
    ``{"type":"conversation_detail_metadata",...}`` payloads, and finally
    the stream's own ``data: [DONE]`` end marker.

    One entry per ``data: `` line, parsed independently and in stream
    order: a payload can be any JSON type -- an object, a list, a bare
    string (``"v1"``), or a number -- so this returns whatever
    ``json.loads`` gives back, unchanged. ``event: `` lines, blank lines,
    ``data: [DONE]``, and any payload that does not parse as JSON are all
    skipped; nothing here raises on odd input.
    """
    events: list[Any] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def find_key(events: Iterable[Any], key: str) -> str | None:
    """The first non-empty string value of ``key`` found anywhere inside
    ``events``, walked depth first, in stream order.

    ``events`` is normally ``stream_events(text)``'s list, but any iterable
    of JSON-shaped values works, including one holding plain strings or
    numbers (they simply contribute no match, never an error). The walk is
    unconditional on shape:

    * a dict -- ``key`` itself first: a non-empty string value there wins
      outright, over anything else nested in the dict; otherwise (the key
      is absent, or its value is not a non-empty string) every value is
      walked in turn, in insertion order, which still reaches that same
      key's value when it is itself a container worth searching;
    * a list -- every item, in order;
    * a string -- a value inside a recorded stream is often JSON text in
      its own right (a tool call's code, or a tool reply's own JSON), so a
      string containing ``key`` is parsed with ``json.loads`` and the
      result is walked the same way; when that parse fails (the string is
      not clean JSON on its own, e.g. ``key`` sits inside a larger blob of
      code or log text), a regex over the raw text is tried instead:
      ``"<key>"\\s*:\\s*"([0-9a-f-]{20,})"``.

    Every other type contributes nothing. Nothing here raises on odd input.
    """
    pattern = re.compile(r'"' + re.escape(key) + r'"\s*:\s*"([0-9a-f-]{20,})"')

    def _search(value: Any) -> str | None:
        if isinstance(value, dict):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
            for v in value.values():
                found = _search(v)
                if found is not None:
                    return found
            return None
        if isinstance(value, list):
            for item in value:
                found = _search(item)
                if found is not None:
                    return found
            return None
        if isinstance(value, str):
            if key not in value:
                return None
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                m = pattern.search(value)
                return m.group(1) if m else None
            return _search(parsed)
        return None

    for event in events:
        found = _search(event)
        if found is not None:
            return found
    return None


def find_session_id(events: Iterable[Any]) -> str | None:
    """The Deep research connector's session id found anywhere inside
    ``events`` (``find_key(events, "session_id")``).

    ``events`` is normally ``stream_events(text)``'s list. The recorded
    stream this exists for (module docstring, WHY) carried it twice, both
    times inside a string value that was itself JSON text -- a tool call's
    code and a tool reply -- never as a plain top-level key, which is why
    ``find_key`` walks into strings too, not only dicts and lists.
    """
    return find_key(events, "session_id")


# ---------------------------------------------------------------------------
# Conversation helpers (pure functions over the /backend-api/conversation JSON)
# ---------------------------------------------------------------------------


def chat_id(chat: str) -> str:
    """Conversation id from an id or URL.

    New chats first show an optimistic client id (``/c/WEB:<uuid>``) that the
    backend does not know; ``is_provisional`` tells callers to resolve it.
    """
    m = re.search(r"/c/((?:WEB:)?[0-9a-f-]{20,})", chat)
    return m.group(1) if m else chat.strip()


def is_provisional(chat: str) -> bool:
    return chat_id(chat).startswith("WEB:")


def epoch_of(value: Any) -> float:
    """Seconds since the epoch for a listing's ``create_time``, or 0.0.

    The API returns an ISO-8601 UTC string such as ``2026-09-16T14:04:05.7Z``
    on listings and a float on some other routes, so both are accepted. An
    unparseable value returns 0.0, which makes every time filter admit it:
    a resolve that cannot read the clock must not start discarding
    conversations.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _created_since(value: Any, since: float) -> bool:
    """Was this conversation created at or after ``since``?

    An unreadable stamp answers ``True``. Losing the right conversation
    because a field was renamed is far worse than keeping one too many: the
    caller already fails loudly when two candidates survive, whereas
    discarding the real one fails with a misleading message.
    """
    stamp = epoch_of(value)
    if stamp == 0.0:
        return True
    # A few seconds of slack: the send starts before the server stamps it.
    return stamp >= since - 5


def resolve_new_conversation(
    session: Any,
    known_ids: set[str],
    *,
    since: float = 0.0,
    timeout: float = 150,
    interval: float = 15,
    sleep: Any = time.sleep,
) -> str:
    """Id of the conversation that appeared since ``known_ids`` was taken.

    ``since`` is an epoch lower bound, normally the moment just before the
    send. It exists for the case of two runs sharing one account: a
    conversation another run opened *before* this send is not in this run's
    ``known_ids`` and would otherwise look new, which fails the resolve. The
    bound discards it. Conversations opened *during* the send are excluded by
    ``new_chat_lock`` instead, which no clock can separate.

    A listing with no readable ``create_time`` is kept, so a changed field
    name degrades to the old behaviour rather than losing the conversation.
    """
    deadline = time.monotonic() + timeout
    sleep(interval)  # the listing needs a moment; asking at once only burns a call
    while True:
        fresh = [
            str(c["id"])
            for c in session.list_conversations(limit=10)
            if c.get("id") not in known_ids
            and _created_since(c.get("create_time"), since)
        ]
        if len(fresh) == 1:
            # The listing can show the chat a few seconds before it is readable.
            for _ in range(6):
                try:
                    session.get_conversation(fresh[0])
                    return fresh[0]
                except TransportError as exc:
                    if exc.status != 404:
                        raise
                    sleep(interval)
            raise TransportError(f"new conversation {fresh[0]} is not readable yet")
        if len(fresh) > 1:
            raise TransportError(f"several new conversations appeared: {fresh}")
        if time.monotonic() >= deadline:
            raise TransportError("the posted message did not create a conversation")
        sleep(interval)


def chain(conv: dict) -> list[dict]:
    """Messages from the root to ``current_node`` in order."""
    mapping = conv.get("mapping", {})
    node = conv.get("current_node")
    out: list[dict] = []
    while node and node in mapping:
        out.append(mapping[node].get("message") or {})
        node = mapping[node].get("parent")
    out.reverse()
    return out


def message_text(msg: dict) -> str:
    content = msg.get("content") or {}
    parts = content.get("parts") or []
    return "\n".join(p if isinstance(p, str) else json.dumps(p) for p in parts)


def assistant_text_messages(conv: dict) -> list[dict]:
    """Assistant messages the user actually sees (content_type text)."""
    return [m for m in chain(conv) if is_visible_assistant_text(m)]


def is_visible_assistant_text(msg: dict) -> bool:
    """Assistant text addressed to the user (not a tool call such as web.run)."""
    return (
        (msg.get("author") or {}).get("role") == "assistant"
        and (msg.get("content") or {}).get("content_type") == "text"
        and msg.get("recipient") in (None, "all")
        and bool(message_text(msg).strip())
    )


def latest_reply(conv: dict) -> str:
    msgs = assistant_text_messages(conv)
    return message_text(msgs[-1]) if msgs else ""


def tail_signature(conv: dict) -> tuple[str, int, str, bool]:
    """(last node id, chain length, last content type, looks-final).

    A turn looks finished when the newest node is a finished assistant text
    message. ChatGPT also emits interim narration as assistant text, so
    callers must additionally require the signature to stay unchanged across
    polls.
    """
    msgs = chain(conv)
    if not msgs:
        return ("", 0, "", False)
    last = msgs[-1]
    ct = (last.get("content") or {}).get("content_type")
    final = (
        is_visible_assistant_text(last)
        and last.get("status") == "finished_successfully"
    )
    return (str(last.get("id")), len(msgs), str(ct), final)


def user_turns(conv: dict) -> int:
    return sum(1 for m in chain(conv) if (m.get("author") or {}).get("role") == "user")


def model_of(conv: dict) -> str:
    """The model slug that answered, as the conversation itself reports it."""
    for msg in reversed(chain(conv)):
        meta = msg.get("metadata") or {}
        slug = meta.get("resolved_model_slug") or meta.get("model_slug")
        if slug:
            return str(slug)
    return "unknown"


def transcript(conv: dict) -> list[dict]:
    """Every user and visible-assistant turn, in order, for the local archive."""
    out: list[dict] = []
    for msg in chain(conv):
        role = (msg.get("author") or {}).get("role")
        if role != "user" and not is_visible_assistant_text(msg):
            continue
        text = message_text(msg)
        if text.strip():
            out.append(
                {
                    "role": role,
                    "text": text,
                    "create_time": msg.get("create_time"),
                    "model": (msg.get("metadata") or {}).get("resolved_model_slug"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


# The reasoning-effort levels a send may pin; send_prompt.py --effort
# validates against this. Effort, model and search reach a send only by
# rewriting the outgoing f/conversation POST body in flight
# (rewrite_send_body, applied through BrowserSender._rewrite_send_route,
# below). An earlier version pinned effort and model by rewriting the
# oai-last-model-config cookie instead (with_effort / with_model); measured
# 2026-09-20, that cookie steers neither what a send carries nor the
# composer's own label (SKILL.md, "Reasoning effort"), so that path and its
# cookie constant are gone.
EFFORTS = ("min", "standard", "extended", "max")


# Requests the send page makes that a scripted send never needs. Blocking
# them is not an optimisation for us: it is load we should not be putting on
# ChatGPT at all. The sidebar's conversation history is the big one, fetched
# on every window open and never read here; the account's own history is also
# what the rate-limit notice is about, so not asking for it is the most
# direct way to stop earning that notice.
#
# Deliberately narrow. Anything the composer, the sentinel handshake or the
# post itself might touch is left alone: breaking a send to save a request
# would be a bad trade.
UNUSED_ON_SEND = (
    "/backend-api/conversations",
    "/backend-api/gizmos/snorlax/sidebar",
    "/backend-api/gizmos/bootstrap",
    "/backend-api/prompt_library",
    "/backend-api/hazelnuts",
    "/backend-api/settings/voices",
    "/ces/v1/projects/",
)


def rewrite_send_body(
    body: dict,
    effort: str,
    model: str,
    search: bool,
    hints: Iterable[str] = (),
) -> dict:
    """The ``f/conversation`` POST body, with effort/model/search/hints pinned.

    Two recorded sends on 2026-09-20 proved ``with_effort``/``with_model``
    do not, by themselves, pin what a send uses: the page takes
    ``thinking_effort`` and ``model`` from the account's server-side
    ``last_used_model_config``, not from the ``oai-last-model-config``
    cookie those two rewrite, so both sends carried ``"max"`` regardless of
    the cookie. This rewrites the body the page already built -- the one
    thing that actually reaches the server -- which is what makes the pin
    real (SKILL.md, "Reasoning effort").

    ``search`` appends ``"search"`` to ``system_hints`` rather than
    replacing it, so a hint the page itself set survives; a hint already
    present is not duplicated. ``hints`` is the general form (ROADMAP.md,
    Stage 3 item 4): every id in it is appended the same way, in order,
    skipping one already present -- whether it was already on the body, put
    there by ``search``, or repeated within ``hints`` itself. The composer's
    "+" menu items are system hints too (``references/endpoint-discovery.md``,
    the ``system_hints`` rows), e.g. ``plugin:connector_openai_deep_research``
    for Deep research. Returns ``body`` itself, unchanged, when ``effort``,
    ``model``, ``search`` and ``hints`` are all blank/false/empty: nothing to
    rewrite, so nothing is copied.
    """
    hint_list = list(hints)
    if not effort and not model and not search and not hint_list:
        return body
    out = dict(body)
    if effort:
        out["thinking_effort"] = effort
    if model:
        out["model"] = model
    if search or hint_list:
        current = list(out.get("system_hints") or [])
        if search and "search" not in current:
            current.append("search")
        for hint in hint_list:
            if hint not in current:
                current.append(hint)
        out["system_hints"] = current
    return out


def block_unused(
    context: Any, allow_project: bool = False, rewrite_send: Any = None
) -> None:
    """Refuse the page's fetches that a scripted send does not read.

    ``allow_project`` keeps the project sidebar, because composing inside a
    project needs its page to render.

    ``rewrite_send``, when given, is offered every request this function
    would otherwise pass through unchanged (never one it aborts): a
    callable taking the intercepted route and returning ``True`` once it
    has resolved the route itself, so this function's own ``continue_()``
    is skipped for that one request. ``_open`` passes ``BrowserSender``'s
    own ``_rewrite_send_route`` here, and only when a send needs to pin its
    effort, model or search, so "block what a send never reads" and
    "rewrite the one request that matters" share a single route per
    pattern instead of two racing for the same URL. Real Playwright would
    resolve two separately-registered routes on the same URL in the order
    opposite to their registration; this module's own test fake
    (``tests/fake_playwright.py``) resolves the same case by first
    registration instead; a second route could not be ordered to agree
    with both. One route with an optional extra step avoids the question.
    """
    skip = tuple(
        path for path in UNUSED_ON_SEND if not (allow_project and "gizmo" in path)
    )

    def route(handler: Any) -> None:
        url = handler.request.url
        if any(path in url for path in skip):
            with contextlib.suppress(Exception):
                handler.abort()
            return
        if rewrite_send is not None and rewrite_send(handler):
            return
        with contextlib.suppress(Exception):
            handler.continue_()

    context.route("**/backend-api/**", route)
    context.route("**/ces/v1/**", route)


class UnreadableCookie(RuntimeError):
    """One cookie value could not be decoded. The others are still good."""


# ---------------------------------------------------------------------------
# Browser budget: this host cannot survive two send windows at once
# ---------------------------------------------------------------------------

# A send window costs about 1.26 GB idle and about 1.75 GB while a large
# prompt is in the composer, measured on the Pi. Three limits keep that from
# taking the machine down, and all three are deliberately cross-process: the
# orchestrator is not the only thing that opens one.
MAX_BROWSERS = int(os.environ.get("RP_MAX_BROWSERS", "1"))
# A hung Playwright call blocks its owner thread forever, so a window with no
# ceiling holds its memory until the run is killed by hand. This is a backstop
# against a hang, not a performance knob, so it must stay clear of the largest
# legitimate send: a 172 kB review prompt is allowed 900 s to fill, plus the
# click budget and the page load on top.
BROWSER_MAX_SECONDS = float(os.environ.get("RP_BROWSER_MAX_SECONDS", "1500"))
# Refuse to open a window that would leave the host with less than this.
# 2500 cleared the 1.76 GB a large prompt needs and nothing more, which was
# not enough: on 2026-09-16 the host's memory monitor killed a run outright
# while the user's Chrome and five Claude sessions were also resident. The
# floor now leaves room for the spike *and* for whatever else grows during
# it, because being killed costs the whole round while waiting costs minutes.
MIN_AVAILABLE_MB = float(os.environ.get("RP_MIN_AVAILABLE_MB", "4000"))
BROWSER_WAIT_SECONDS = float(os.environ.get("RP_BROWSER_WAIT_SECONDS", "900"))
# How long a run waits for another run to finish opening a conversation.
# Resolution normally takes one listing after a 15 s settle, so this is
# generous: it covers a full resolve_new_conversation timeout plus a send.
NEW_CHAT_WAIT_SECONDS = float(os.environ.get("RP_NEW_CHAT_WAIT_SECONDS", "600"))
SLOT_DIR = Path(os.environ.get("RP_BROWSER_SLOT_DIR", "/tmp/rp-browser-slots"))
# One Chrome profile shared by every send, so the app bundle is fetched
# once instead of once per send. Safe to share because MAX_BROWSERS is a
# cross-process lock: two windows never exist at the same moment. Set
# RP_BROWSER_PROFILE to an empty string to go back to a fresh profile.
_profile = os.environ.get("RP_BROWSER_PROFILE", "/tmp/rp-browser-profile")
PROFILE_DIR = Path(_profile) if _profile else None
DISK_CACHE_BYTES = int(os.environ.get("RP_DISK_CACHE_BYTES", str(256 * 1024**2)))
PAGE_LOAD_MS = int(os.environ.get("RP_PAGE_LOAD_MS", "150000"))
# Playwright launches Chrome with its own temporary profile. Matching on that
# is what keeps the watchdog from ever touching the user's own browser.
PW_MARKER = "playwright"


def available_mb() -> float:
    """Memory the kernel says is available, without counting swap."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024
    except OSError:
        pass
    return float("inf")  # unknown: do not block the run over it


# argv[0] basenames a Playwright-launched browser can have: Google Chrome
# (channel="chrome", what this skill uses), Playwright's own Chromium builds,
# and its headless shell. The user's own Chrome has the same argv[0] and is
# told apart by PW_MARKER in its arguments (Playwright's profile path).
BROWSER_BINARIES = frozenset(
    {
        "chrome",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "headless_shell",
        "chrome-headless-shell",
    }
)


def scripted_browser_pids() -> set[int]:
    """PIDs of browser processes started by Playwright, never the user's own.

    Two things must both hold: argv[0] is a browser binary
    (``BROWSER_BINARIES``), and the arguments carry ``PW_MARKER``. The
    first test is what keeps a shell script, a pytest run or an editor
    whose command line merely *mentions* both words from counting as a
    window: on 2026-09-21 a bash sampler with "playwright" and "chrome" in
    its text held preflight's "in-flight browsers" block for an hour.
    """
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue  # the process ended while we looked
        argv = raw.decode("utf-8", "replace").split("\0")
        if not argv or os.path.basename(argv[0]) not in BROWSER_BINARIES:
            continue
        if PW_MARKER in " ".join(argv[1:]):
            pids.add(int(entry.name))
    return pids


@contextlib.contextmanager
def browser_slot(
    sleep: Any = time.sleep, wait: float = BROWSER_WAIT_SECONDS
) -> Generator[int, None, None]:
    """Hold one of ``MAX_BROWSERS`` slots, and wait for memory, or fail.

    The slot is an advisory lock on a file, so the cap holds across processes:
    a second orchestrator, or a probe script, cannot sneak a window past it.
    Failing here is the point. A ``TransportError`` retries later, whereas
    opening the window anyway takes the host into swap and every Playwright
    call then times out.
    """
    import fcntl

    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    handle, slot = None, -1
    while handle is None:
        for index in range(MAX_BROWSERS):
            candidate = (SLOT_DIR / f"slot{index}.lock").open("w")
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                candidate.close()
                continue
            handle, slot = candidate, index
            break
        if handle is None:
            if time.monotonic() >= deadline:
                raise TransportError(
                    f"all {MAX_BROWSERS} send window(s) are busy after {wait:.0f} s"
                )
            logger.warning("every send window is busy; waiting for one to close")
            sleep(5)
    try:
        while (free := available_mb()) < MIN_AVAILABLE_MB:
            if time.monotonic() >= deadline:
                raise TransportError(
                    f"only {free:.0f} MB available, {MIN_AVAILABLE_MB:.0f} MB needed "
                    f"to open a send window; waited {wait:.0f} s"
                )
            logger.warning(
                "holding the send window: %.0f MB available, %.0f MB needed",
                free,
                MIN_AVAILABLE_MB,
            )
            sleep(15)
        yield slot
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@contextlib.contextmanager
def new_chat_lock(
    wait: float = NEW_CHAT_WAIT_SECONDS, sleep: Any = time.sleep
) -> Generator[bool, None, None]:
    """Serialise "open a new conversation and learn its id" across processes.

    A send returns an optimistic ``WEB:`` id, so the real one is recovered by
    listing the account and taking the conversation that was not there before.
    That only works while this process is the only one creating conversations:
    with two orchestrators running, both see two new chats and both fail with
    ``several new conversations appeared``. Neither picks the wrong chat, so
    the data stays correct, but both rounds lose the send.

    Holding this lock from before the send until the id is resolved makes the
    window exclusive. It is deliberately *not* the browser slot: the browser
    is already closed while the id is being resolved, and blocking another
    run's window for that would waste the memory budget it protects.

    Yields ``True`` when the lock was taken, ``False`` when the wait ran out.
    Failing to take it is not worth aborting a send over -- a single run, the
    normal case, never contends -- so the caller proceeds either way.
    """
    import fcntl

    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    handle = (SLOT_DIR / "newchat.lock").open("w")
    deadline = time.monotonic() + wait
    taken = False
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                taken = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "another run still holds the new-conversation lock after "
                        "%.0f s; sending anyway",
                        wait,
                    )
                    break
                sleep(2)
        yield taken
    finally:
        if taken:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _tolerant_decryptor(cs: Any, app: str) -> Any:
    """Decrypt a cookie value, and raise instead of killing the process.

    The helper accepts a decrypted value only when every character is
    printable ASCII, and calls ``fail()`` — which exits — otherwise. Three
    ordinary values fall foul of that: an empty one, one holding a tab, and
    one holding any non-ASCII character. Chrome writes and clears analytics
    cookies (``_dd_s``, ``g_state``, ``oai_consent_marketing``) constantly,
    so the jar intermittently holds such a value and a whole run died with
    "could not decode a decrypted cookie value" while the session cookie
    itself was perfectly readable.

    Accept any valid UTF-8 without control characters, empty included. Newer
    Chromium prepends a 32-byte domain hash, so the stripped candidate is
    tried as well, in the helper's order to keep short values decoding the
    same way.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    def derive(pw: bytes) -> bytes:
        return PBKDF2HMAC(
            algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
        ).derive(pw)

    keys = {b"v10": derive(b"peanuts"), b"v11": derive(cs._keyring_password(app))}

    def readable(candidate: bytes, allow_empty: bool) -> str | None:
        try:
            text = candidate.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not text:
            return "" if allow_empty else None
        if any(ord(ch) < 32 and ch != "\t" for ch in text):
            return None
        return text

    def decrypt(enc: bytes) -> str:
        key = keys.get(bytes(enc[:3]))
        if key is None:
            raise UnreadableCookie(f"unexpected encryption version {bytes(enc[:3])!r}")
        engine = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = engine.update(enc[3:]) + engine.finalize()
        if not plain:
            raise UnreadableCookie("empty ciphertext")
        pad = plain[-1]
        plain = plain[:-pad] if 0 < pad <= 16 else plain
        # The whole plaintext first, then without the 32-byte domain hash that
        # newer Chromium prepends. An empty value is legitimate only in the
        # second form, and only when the hash is all there was: without that
        # check, random bytes from a torn DB copy would pass as an empty
        # cookie and a corrupt session token would reach the server silently.
        candidates: list[tuple[bytes, bool]] = [(plain, False)]
        if len(plain) >= 32:
            candidates.append((plain[32:], len(plain) == 32))
        for candidate, allow_empty in candidates:
            text = readable(candidate, allow_empty)
            if text is not None:
                return text
        raise UnreadableCookie("value is not decodable text")

    return decrypt


def _skip_unreadable(decrypt: Any, value: str, enc: bytes) -> str | None:
    """The stored value, or ``None`` when this one cookie cannot be read."""
    if not enc:
        return value
    try:
        return str(decrypt(enc))
    except UnreadableCookie:
        # A cookie we cannot read is dropped, exactly as a browser drops a
        # corrupt entry. Losing the session cookie is still fatal, and the
        # callers below check for it.
        return None


def _patch_cookie_header(cs: Any) -> None:
    """Make ``cs._cookie_header`` skip unreadable cookies instead of exiting."""
    if getattr(cs, "_rp_tolerant", False):
        return

    def cookie_header(browser: str) -> str:
        db, app = cs.BROWSERS[browser]
        if not db.exists():
            raise TransportError(f"{browser} cookie DB not found at {db}")
        decrypt = _tolerant_decryptor(cs, app)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            shutil.copy2(db, tmp.name)  # the live DB is locked by the browser
            con = sqlite3.connect(tmp.name)
            rows = con.execute(
                "SELECT name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%chatgpt.com' ORDER BY name"
            ).fetchall()
            con.close()
        pairs, have_session, dropped = [], False, []
        for name, value, enc in rows:
            text = _skip_unreadable(decrypt, value, enc)
            if text is None:
                dropped.append(name)
                continue
            pairs.append(f"{name}={text}")
            if name.startswith("__Secure-next-auth.session-token"):
                have_session = True
        if not have_session:
            raise TransportError(
                f"no readable ChatGPT session cookie in {browser} "
                f"(logged in? unreadable: {', '.join(dropped) or 'none'})"
            )
        return "; ".join(pairs)

    cs._cookie_header = cookie_header
    cs._rp_tolerant = True


def _patch_cookie_export(cs: Any, mod: Any) -> None:
    """Make ``chatgpt_cookies.export`` skip unreadable cookies, and apply
    the same session-token renewal choice the plain-HTTP client makes
    (``cs.choose_session`` between Chrome's own jar and this machine's
    keyring, through ``cs.apply_session_to_jar`` -- one shared rule with
    ``chatgpt_cookies.export`` itself, not two that could drift apart), so
    a scripted send never carries a session token staler than what
    ``ChatGPTSession`` would use."""
    if getattr(mod, "_rp_tolerant", False):
        return

    def export(browser: str) -> list[dict[str, Any]]:
        db, app = cs.BROWSERS[browser]
        if not db.exists():
            raise TransportError(f"{browser} cookie DB not found at {db}")
        decrypt = _tolerant_decryptor(cs, app)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            shutil.copy2(db, tmp.name)
            con = sqlite3.connect(tmp.name)
            rows = con.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
                "is_secure, is_httponly, samesite FROM cookies "
                "WHERE host_key LIKE '%chatgpt.com' OR host_key LIKE '%openai.com'"
            ).fetchall()
            con.close()
        same_site = {0: "None", 1: "Lax", 2: "Strict"}
        out: list[dict[str, Any]] = []
        for host, name, value, enc, path, exp, secure, httponly, ss in rows:
            text = _skip_unreadable(decrypt, value, enc)
            if text is None:
                continue
            cookie: dict[str, Any] = {
                "name": name,
                "value": text,
                "domain": host,
                "path": path or "/",
                "secure": bool(secure),
                "httpOnly": bool(httponly),
                "sameSite": same_site.get(ss, "Lax"),
            }
            if exp:
                cookie["expires"] = exp / 1_000_000 - mod._EPOCH_DELTA_S
            out.append(cookie)
        if not any(
            c["name"].startswith("__Secure-next-auth.session-token") for c in out
        ):
            raise TransportError(
                f"no readable ChatGPT session cookie in {browser} (is it logged in?)"
            )
        chrome_record = cs.chrome_session_record(
            (c["name"], c["value"], c.get("expires")) for c in out
        )
        chosen, _source = cs.choose_session(chrome_record, cs.load_stored_session())
        if chosen is not None:
            cs.apply_session_to_jar(out, chosen)
        return out

    mod.export = export
    mod._rp_tolerant = True


def _helpers() -> Any:
    if str(HELPERS) not in sys.path:
        sys.path.insert(0, str(HELPERS))
    import chatgpt_session  # type: ignore[import-not-found]

    _patch_cookie_header(chatgpt_session)
    return chatgpt_session


class ChatGPTSession:
    """Plain-HTTP access to conversations through the logged-in session."""

    # Authenticating is the first thing a run does, and a transient 403 there
    # killed a resumed run outright. The helper's own retries are seconds
    # apart, which does not outlast a burst block.
    AUTH_BACKOFF = (30.0, 90.0, 180.0)

    def __init__(self, browser: str = "chrome", sleep: Any = time.sleep):
        cs = _helpers()
        cs.set_prog("chatgpt-client")
        self._cs = cs
        self.browser = browser
        for attempt, wait in enumerate((*self.AUTH_BACKOFF, None)):
            try:
                self.session = cs.Session(browser)
                return
            except SystemExit as exc:  # cs.fail() exits on an auth failure
                if wait is None:
                    raise TransportError(
                        f"could not authenticate after {attempt} retries: {exc}"
                    ) from exc
                sleep(wait)

    # The helper's own retries are seconds apart, which answers a Cloudflare
    # burst but not a backend rate limit. A rate limit is a request to slow
    # down, not a reason to lose a research round, so waiting minutes is the
    # cheaper outcome.
    RATE_LIMIT_BACKOFF = (60.0, 150.0, 300.0)

    def _call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        sleep: Any = time.sleep,
    ) -> Any:
        last: tuple[int, Any] = (0, "")
        reauthed = False
        for attempt in range(len(self.RATE_LIMIT_BACKOFF) + 1):
            status, data = self.session.call(
                path, method=method, payload=payload, retries=3
            )
            if status == 200:
                return data
            last = (status, data)
            if status == 403 and not reauthed:
                # The bearer token is minted when the session is built, and a
                # run outlives it: a process started at midnight is still
                # reading at 04:00. Treating 403 as fatal meant the whole run
                # died and had to be restarted, losing the step in flight.
                # Rebuilding the session re-reads the browser cookies, which
                # is exactly what a restart would have done, without the cost.
                # Once per call: if the account is really logged out, the
                # rebuild fails on its own ladder and says so.
                reauthed = True
                logger.warning("HTTP 403 on %s; rebuilding the session", path)
                try:
                    self.session = self._cs.Session(self.browser)
                    continue
                except SystemExit as exc:
                    raise TransportError(
                        f"403 on {path} and re-authentication failed: {exc}", 403
                    ) from exc
            if status not in (429, 500, 502, 503, 504):
                break
            if attempt < len(self.RATE_LIMIT_BACKOFF):
                sleep(self.RATE_LIMIT_BACKOFF[attempt])
        status, data = last
        raise TransportError(
            f"{method} {path} -> HTTP {status}: {str(data)[:200]}", status
        )

    def get_conversation(self, chat: str) -> dict:
        data = self._call(f"/backend-api/conversation/{chat_id(chat)}")
        if not isinstance(data, dict):
            raise TransportError("conversation payload is not an object")
        return data

    def list_conversations(self, limit: int = 28, offset: int = 0) -> list[dict]:
        data = self._call(
            f"/backend-api/conversations?offset={offset}&limit={limit}&order=updated"
        )
        return list(data.get("items", [])) if isinstance(data, dict) else []

    def rename(self, chat: str, title: str) -> None:
        self._call(
            f"/backend-api/conversation/{chat_id(chat)}", "PATCH", {"title": title}
        )

    def archive(self, chat: str) -> None:
        self._call(
            f"/backend-api/conversation/{chat_id(chat)}", "PATCH", {"is_archived": True}
        )

    def delete(self, chat: str) -> None:
        """Remove the conversation from the account's list.

        Same call the web app's delete button makes. Archive the transcript
        first: after this the conversation cannot be read back.
        """
        self._call(
            f"/backend-api/conversation/{chat_id(chat)}", "PATCH", {"is_visible": False}
        )


def wait_for_reply(
    session: ChatGPTSession,
    chat: str,
    *,
    timeout: float = 1500,
    interval: float = 60,
    max_interval: float = 150,
    min_user_turns: int = 1,
    first_poll: float = 0,
    confirm_interval: float = 0,
    sleep: Any = time.sleep,
) -> str:
    """Poll over HTTP until the assistant's turn has ended, return its text.

    Finished = the newest node is a final-looking assistant text message and
    the conversation tail has not changed between two consecutive polls.
    ``min_user_turns`` guards against reading the reply of an earlier turn.
    """
    deadline = time.monotonic() + timeout
    prev: tuple[str, int, str, bool] | None = None
    stable = 0
    # Do not ask before the turn could plausibly be finished. Measured across
    # Topics 03-05, the fastest a search turn ever finished was 378 s, yet we
    # asked first at 60 s and five more times before it could answer. Polling
    # volume is what earns this account its rate-limit notices, so the cheapest
    # request is the one not made.
    if first_poll > 0:
        sleep(first_poll)
    # The interval grows as the turn runs on. A research worker takes minutes,
    # and hundreds of eager polls across a multi-round run are what earn an
    # account a backend rate limit.
    wait = interval
    while True:
        try:
            conv = session.get_conversation(chat)
        except (TransportError, TimeoutError, OSError) as exc:
            # A poll that fails is not a step that failed. The reply is on
            # ChatGPT's servers either way, and the deadline below still
            # bounds the wait.
            #
            # msgloom Topic 07 round 2 was aborted by one socket timeout on
            # one poll: "The read operation timed out" propagated out of
            # wait_for_reply, run_job caught it as a fatal TimeoutError, and
            # the run stopped with the terminology reply most likely already
            # written. _call's retry ladder could not help, because it
            # matches on status codes and a socket timeout never produces
            # one.
            if time.monotonic() >= deadline:
                raise
            logger.warning("poll of %s failed (%s); retrying", chat_id(chat), exc)
            sleep(min(max_interval, wait))
            continue
        sig = tail_signature(conv)
        stable = stable + 1 if sig == prev else 0
        prev = sig
        if sig[3] and stable >= 1 and user_turns(conv) >= min_user_turns:
            return latest_reply(conv)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"assistant did not finish within {timeout:g} s ({chat_id(chat)})"
            )
        # Confirming a finished-looking turn does not need a full interval.
        # The second poll exists only to catch a reply still streaming, which
        # settles in seconds; waiting the grown interval instead cost up to
        # 150 s per step, eight steps a round, on a round that runs 71 minutes.
        # Only the *waiting* case keeps the long interval, because that is
        # where polling volume comes from.
        confirming = sig[3] and confirm_interval > 0
        sleep(confirm_interval if confirming else wait)
        if not confirming:
            wait = min(max_interval, wait * 1.25)


# ---------------------------------------------------------------------------
# Browser sender (the only non-HTTP action)
# ---------------------------------------------------------------------------


def ensure_desktop_env(environ: MutableMapping[str, str] | None = None) -> None:
    """Default the D-Bus session-bus variables cron never sets.

    A thin delegate: the defaulting logic lives next to the D-Bus call that
    needs it, in ``chatgpt_session`` (see ``ensure_desktop_env`` there, and
    VENDORED.md for why this module keeps only one copy). Kept importable
    here, under its original name, because callers such as ``preflight.py``
    and :func:`virtual_display` reach it as ``chatgpt_client.ensure_desktop_env``.

    Imports the session module plainly, never through ``_helpers()``: that
    loader also patches the module's cookie reader as a side effect, and a
    defaulting helper that quietly did that broke three unrelated tests the
    first time the full suite ran (2026-09-20).
    """
    if str(HELPERS) not in sys.path:
        sys.path.insert(0, str(HELPERS))
    import chatgpt_session  # type: ignore[import-not-found]

    chatgpt_session.ensure_desktop_env(environ)


def reexec_with_playwright(argv: list[str] | None = None) -> None:
    """Re-run the current script under the Playwright venv when needed."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        if not os.path.exists(PW_PYTHON) or os.environ.get("CHATGPT_SEND_REEXEC"):
            raise SystemExit(
                f"playwright missing in {sys.executable} and no venv at {PW_PYTHON}"
            ) from None
        env = dict(os.environ, CHATGPT_SEND_REEXEC="1")
        script = os.path.abspath(sys.argv[0])
        os.execve(
            PW_PYTHON,
            [PW_PYTHON, script, *(argv if argv is not None else sys.argv[1:])],
            env,
        )


@contextlib.contextmanager
def virtual_display(visible: bool = False) -> Generator[None]:
    """Xvfb virtual screen (default) or the on-screen display (visible)."""
    ensure_desktop_env()
    xvfb = None if visible else shutil.which("Xvfb")
    saved = {k: os.environ.get(k) for k in ("DISPLAY", "WAYLAND_DISPLAY")}
    proc = None
    try:
        if xvfb:
            n = 99
            while os.path.exists(f"/tmp/.X11-unix/X{n}"):
                n += 1
            proc = subprocess.Popen(
                [xvfb, f":{n}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(100):
                if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                    break
                time.sleep(0.05)
            os.environ["DISPLAY"] = f":{n}"
            os.environ.pop("WAYLAND_DISPLAY", None)
        else:
            os.environ.setdefault("DISPLAY", ":0")
            if os.path.exists(f"{os.environ['XDG_RUNTIME_DIR']}/wayland-0"):
                os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if proc:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)


# The sibling suffix a send's recorded reply stream is written under
# (``BrowserSender._record_send_stream``): a module-level constant, not a
# ``BrowserSender`` one, so ``send_prompt.py`` can read it as
# ``cc.STREAM_SUFFIX`` even in a test that monkeypatches ``cc.BrowserSender``
# itself to a fake.
STREAM_SUFFIX = ".stream.txt"


# What the composer shows after an upload, read from its own DOM. Scoped to
# the composer's <form> when one is found (SEND_BUTTONS shows the send
# button lives inside one), so a same-named button elsewhere on the page can
# never produce a false answer. One label per "Remove file ..." chip, in
# page order.
COMPOSER_STATE_JS = """
() => {
    const composer = document.querySelector('#prompt-textarea');
    const form = composer ? composer.closest('form') : null;
    const scope = form || document;
    const removeLabels = Array.from(scope.querySelectorAll('button[aria-label]'))
        .map((button) => button.getAttribute('aria-label') || '')
        .filter((label) => label.startsWith('Remove file'));
    const sendButton =
        scope.querySelector('button[data-testid="send-button"]') ||
        scope.querySelector('button[aria-label="Send prompt"]');
    return {
        remove_labels: removeLabels,
        send_exists: Boolean(sendButton),
        send_enabled: Boolean(sendButton) && !sendButton.disabled,
    };
}
"""


def fill_budget_ms(n_chars: int) -> int:
    """How long a send gives the composer to ingest ``n_chars`` of prompt:
    6 s per kB, never under 120 s, capped at 900 s. One function, so a
    measurement (``measure_window.py``) compares against exactly the budget
    a send would use.

    Why these numbers, from the sends that set them:

    ProseMirror ingests a large prompt slowly on this host, so the budget
    scales; above ATTACH_ABOVE_BYTES the prompt is uploaded instead,
    because minutes of re-rendering is what made the big synthesis and
    review prompts fail outright.
    Measured 2026-09-16: an 89 kB review prompt fills in 63-69 s on an
    idle host, whatever the launch flags. The old 2.5 s/kB rate gave
    that prompt 225 s, a 3.5x margin, and a busy host still blew
    through it: the fill that cost a round ran while a full-repository
    pre-commit scan had the CPU. The rate is what absorbs contention,
    so it is generous. A timeout costs a whole retry cycle; waiting
    longer costs nothing when the fill was going to finish anyway.
    The cap has to clear the largest prompt this pipeline builds. A
    round-2 review prompt is 172 kB, and 600 s was not enough for it on
    a host whose load average was 5: the send failed twice and took the
    round with it. Keep the cap under BROWSER_MAX_SECONDS so a genuine
    long fill is never killed by the window watchdog.
    """
    return max(120_000, min(900_000, 6_000 * (n_chars // 1_000 + 1)))


class BrowserSender:
    """One scripted Chrome page per run that posts messages to chatgpt.com.

    Use as a context manager. ``send(text)`` opens a new conversation and
    returns its id; ``send(text, chat=<id>)`` continues a conversation.

    ``effort``, ``model``, ``search`` and ``hints`` pin what this send uses.
    None of them touches a cookie: measured 2026-09-20, the composer's own
    ``oai-last-model-config`` cookie steers neither what a send carries nor
    the composer's label (SKILL.md, "Reasoning effort"). Instead ``_open``
    registers a route, only when one of the four is set, that rewrites the
    outgoing ``f/conversation`` POST body in flight
    (``rewrite_send_body`` / ``_rewrite_send_route``).
    """

    SEND_BUTTONS = (
        '[data-testid="send-button"]',
        'button[aria-label*="Send" i]',
        'form button[type="submit"]',
    )

    # The message itself, never its sentinel .../f/conversation/prepare
    # handshake sibling nor any GET (SKILL.md, "The transport split";
    # ROADMAP.md, Stage 3 item 2): the only POST worth recording for
    # ``record_send_body``, and the only one ``_rewrite_send_route`` ever
    # rewrites.
    # An upload that never shows a busy indicator within this window is
    # taken as instant; one that does must clear it within UPLOAD_MAX_MS.
    UPLOAD_QUIET_MS = 15_000
    UPLOAD_MAX_MS = 180_000
    RECORD_ENDPOINT = "/backend-api/f/conversation"

    # This host is memory-tight and swaps under load. One scripted page for a
    # send cost about 1.65 GB with the first four flags alone, measured on the
    # Pi against a loaded composer; the rest bring that down by roughly 470 MB
    # without changing what the page can do. Headful under Xvfb stays: the
    # send path is gated by a behavioural check, and a real window passes it.
    WIDTH, HEIGHT = 1000, 800
    LAUNCH_ARGS = (
        f"--window-size={WIDTH},{HEIGHT}",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-renderer-backgrounding",
        "--renderer-process-limit=1",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-accelerated-2d-canvas",
        "--disable-sync",
        "--disable-translate",
        "--mute-audio",
        "--disable-component-extensions-with-background-pages",
        "--js-flags=--max-old-space-size=384",
    )

    def __init__(
        self,
        browser: str = "chrome",
        visible: bool = False,
        screenshot_dir: str = "/tmp",
        project: str = "",
        effort: str = "",
        model: str = "",
        search: bool = False,
        hints: tuple[str, ...] = (),
        record_send_body: str = "",
        attachments: list[str] | tuple[str, ...] = (),
    ):
        self.browser = browser
        self.visible = visible
        self.screenshot_dir = Path(screenshot_dir)
        # A ChatGPT project (a "snorlax gizmo", g-p-… id or short_url slug).
        # New conversations are composed on its page, so they belong to it.
        self.project = project.strip()
        # Reasoning effort for this send. Empty inherits whatever the
        # account's own settings/user record last used (model_settings.py
        # prints it); pinned by rewriting the send body in flight, in
        # _open, never a cookie (rewrite_send_body).
        self.effort = effort.strip()
        # Model slug for this send, pinned the same way as effort. Empty
        # inherits the account's own model.
        self.model = model.strip()
        # Web search on for this send (ROADMAP.md, Stage 3 item 2 / B2),
        # pinned the same way as effort: rewrite_send_body appends
        # "search" to the send body's system_hints. Off by default, like
        # every other opt-in choice here.
        self.search = bool(search)
        # Generic system hints for this send (ROADMAP.md, Stage 3 item 4),
        # pinned the same way as search: rewrite_send_body appends each one
        # to the send body's system_hints that is not already present, in
        # order. The composer's "+" menu items are system hints too
        # (references/endpoint-discovery.md); e.g.
        # "plugin:connector_openai_deep_research" for Deep research. Empty
        # by default, like every other opt-in choice here.
        self.hints = tuple(h.strip() for h in hints if str(h).strip())
        # File path: when set, the f/conversation POST body (method, url,
        # post_data) is recorded there, so one real send can document what
        # the page actually sends with and without search. This send's own
        # reply stream is captured too, to the sibling STREAM_SUFFIX path,
        # synchronously in _send once the post-click checks confirm the
        # message went through (_record_send_stream) -- a Deep research
        # send's connector session id is only ever visible there (module
        # docstring).
        self.record_send_body = record_send_body.strip()
        # Files to upload through the composer before the prompt is filled
        # (ROADMAP.md, Stage 3 item 3 / B4): the same file input and the
        # same settle wait _attach_prompt already uses for the
        # prompt-as-a-file case (_upload_files). Resolved to absolute
        # paths and checked to exist right here, before any browser opens
        # -- a typo must fail in an instant, not cost a whole window
        # (SKILL.md, "The browser is budgeted").
        resolved_attachments: list[str] = []
        for raw in attachments:
            attachment_path = Path(raw).expanduser()
            if not attachment_path.is_file():
                raise FileNotFoundError(f"no such attachment file: {attachment_path}")
            resolved_attachments.append(str(attachment_path.resolve()))
        self.attachments = resolved_attachments
        # Set by _rewrite_send_route: the exact post_data (JSON text) that
        # went on the wire for the last f/conversation POST, once effort,
        # model or search rewrote it. Stays None until then, and forever
        # None when nothing here is pinned. _record_request reads it to
        # tell "what the page built" from "what was actually sent".
        self._last_sent_body: str | None = None
        self._stack = contextlib.ExitStack()
        self.page: Any = None
        # Playwright's sync objects may only be used from the thread that
        # created them, and the dispatcher runs jobs in a pool. One owner
        # thread creates the browser and performs every call on it.
        self._owner = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="chatgpt-send"
        )
        self._closed = threading.Event()
        self._own_pids: set[int] = set()

    def __enter__(self) -> BrowserSender:
        # Hold a slot for the whole window, so the cap counts open windows and
        # not merely concurrent opens. It is released by the exit stack.
        self._stack.enter_context(browser_slot())
        self._owner.submit(self._open).result()
        self._stack.callback(self._closed.set)
        threading.Thread(
            target=self._watchdog,
            args=(time.monotonic() + BROWSER_MAX_SECONDS,),
            name="chatgpt-send-watchdog",
            daemon=True,
        ).start()
        return self

    def _watchdog(self, deadline: float) -> None:
        """Kill a window that outlives its ceiling, so its memory comes back.

        A hung Playwright call blocks the owner thread, so a graceful close
        cannot run: it would be queued behind the very call that is stuck.
        Killing the scripted Chrome makes that call raise, which unblocks the
        owner and lets the normal teardown finish. Only processes carrying
        Playwright's temporary profile are touched, never the user's Chrome.
        """
        while not self._closed.wait(5):
            if time.monotonic() < deadline:
                continue
            victims = self._own_pids & scripted_browser_pids()
            logger.warning(
                "send window exceeded %.0f s; killing %d scripted browser "
                "process(es) to release its memory",
                BROWSER_MAX_SECONDS,
                len(victims),
            )
            for pid in victims:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            return

    def __exit__(self, *exc: object) -> None:
        try:
            # A window the watchdog already killed can raise while this
            # closes it (the context is already gone). Letting that escape
            # would replace whatever exception the with-block body raised --
            # or invent one where the body raised nothing -- so a failure to
            # close an already-dead context is swallowed rather than masking
            # the real outcome of the send.
            with contextlib.suppress(Exception):
                self._owner.submit(self._stack.close).result()
        finally:
            self._closed.set()
            self._owner.shutdown(wait=True)
            self.page = None

    def _open(self) -> None:
        from playwright.sync_api import sync_playwright

        cs = _helpers()
        if str(HELPERS) not in sys.path:
            sys.path.insert(0, str(HELPERS))
        import chatgpt_cookies  # type: ignore[import-not-found]

        _patch_cookie_export(cs, chatgpt_cookies)
        # The exported jar reaches the context unchanged: effort, model and
        # search never touch a cookie here (measured 2026-09-20: rewriting
        # oai-last-model-config pinned neither the send nor the composer's
        # own label). What pins them is the route registered below, only
        # when one of the three is set, which rewrites the f/conversation
        # POST body in flight (rewrite_send_body / _rewrite_send_route).
        cookies = chatgpt_cookies.export(cs.pick_browser(self.browser))
        self._stack.enter_context(virtual_display(self.visible))
        pw = self._stack.enter_context(sync_playwright())
        # Note which scripted browsers existed before, so the watchdog can
        # tell this window's processes from any other.
        before = scripted_browser_pids()
        ctx = self._launch(pw)
        self._own_pids = scripted_browser_pids() - before
        ctx.add_cookies(cookies)
        # rewrite_send_body only has work to do when one of these is set;
        # block_unused must not be handed a hook otherwise, or every send
        # (even one pinning nothing) would pay the extra JSON round trip.
        rewrites = bool(self.effort or self.model or self.search or self.hints)
        block_unused(
            ctx,
            allow_project=bool(self.project),
            rewrite_send=self._rewrite_send_route if rewrites else None,
        )
        self.page = ctx.new_page()
        if self.record_send_body:
            self.page.on("request", self._record_request)

    def _launch(self, pw: Any) -> Any:
        """A browser context that keeps its HTTP cache between sends.

        One chatgpt.com page load fetches about 420 responses. The send needs
        the page's own JavaScript to run -- proof-of-work, Turnstile and the
        behavioural collector are the whole reason a browser is involved --
        so the 333 script files cannot be blocked. They can be cached.

        Measured on this host, the same page loaded twice in one persistent
        profile: 1 disk-cache hit and 505 kB over the network the first time,
        354 hits and 144 kB the second. A fresh profile per send, which is
        what this did before, paid the 505 kB every time -- roughly 100 MB
        and 100,000 avoidable CDN fetches across one night of five topics.

        Falls back to a throwaway browser if the shared profile cannot be
        used, so the worst case is the behaviour this replaces rather than a
        run that cannot send at all.
        """
        if PROFILE_DIR:
            try:
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                # A window killed by the watchdog leaves this behind, and
                # Chrome then refuses the profile. The slot lock guarantees
                # nobody else is using it, so it is safe to clear.
                for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                    (PROFILE_DIR / stale).unlink(missing_ok=True)
                ctx = pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    args=[*self.LAUNCH_ARGS, f"--disk-cache-size={DISK_CACHE_BYTES}"],
                    viewport={"width": self.WIDTH, "height": self.HEIGHT},
                )
                # Chrome flushes its disk cache on a clean shutdown. Without
                # this the profile survives but its cache does not, and the
                # next send pays for the whole bundle again -- measured as
                # 24 kB of cache data and one hit, instead of 354.
                self._stack.callback(ctx.close)
                return ctx
            except Exception as exc:  # any failure falls back to a fresh profile
                logger.warning(
                    "shared browser profile unusable (%s); using a fresh one", exc
                )
        browser = pw.chromium.launch(
            channel="chrome", headless=False, args=list(self.LAUNCH_ARGS)
        )
        self._stack.callback(browser.close)
        return browser.new_context(
            viewport={"width": self.WIDTH, "height": self.HEIGHT}
        )

    def _rewrite_send_route(self, route: Any) -> bool:
        """Rewrite this send's own POST body in flight: effort, model, search.

        Offered to ``block_unused`` as its ``rewrite_send`` hook, so it only
        ever sees a request ``block_unused`` was already about to let
        through unchanged (never one of ``UNUSED_ON_SEND``). Resolves the
        route itself and returns ``True`` for a POST to exactly
        ``RECORD_ENDPOINT`` with a JSON object body -- the one request this
        exists for; returns ``False`` for anything else (a GET, the
        ``/prepare`` sibling, a body that is not a JSON object), so the
        caller's own ``continue_()`` finishes it untouched.

        ``self._last_sent_body`` records the exact ``post_data`` that went
        on the wire, as text, so ``_record_request`` can tell "what the
        page built" from "what was actually sent" when both are being
        recorded (ROADMAP.md, Stage 3 items 1-2).
        """
        req = route.request
        if req.method != "POST":
            return False
        path = req.url.split("?", 1)[0].split("#", 1)[0]
        if not path.endswith(self.RECORD_ENDPOINT):
            return False
        try:
            body = json.loads(req.post_data) if req.post_data else None
        except (TypeError, ValueError):
            return False
        if not isinstance(body, dict):
            return False
        sent = json.dumps(
            rewrite_send_body(body, self.effort, self.model, self.search, self.hints)
        )
        self._last_sent_body = sent
        # The page's ``request`` event fires before any route runs, so the
        # record of a rewritten send must be written here, where both
        # bodies are known; ``_record_request`` writes only unrewritten ones.
        if self.record_send_body:
            self._write_record(
                {
                    "method": req.method,
                    "url": req.url,
                    "original": req.post_data,
                    "sent": sent,
                }
            )
        with contextlib.suppress(Exception):
            route.continue_(post_data=sent)
        return True

    def _write_record(self, doc: dict[str, Any]) -> None:
        """Write ``doc`` to ``record_send_body``; never raises.

        ``"stream_file"`` -- the sibling path ``_record_send_stream`` writes
        this same send's reply stream to -- is added to every record, in
        both shapes, so a reader can always find it and pull the Deep
        research connector's session id out of it (``stream_events`` /
        ``find_session_id``). Every attachment path is added under
        ``"attachments"`` when this send has any, so the one file both call
        sites (the rewritten and the plain shape) write through stays
        exactly as it was otherwise -- the backward-compatibility contract
        of ``record_send_body`` -- for a send that pins nothing at all,
        including no attachments.
        """
        doc = {**doc, "stream_file": self.record_send_body + STREAM_SUFFIX}
        if self.attachments:
            doc["attachments"] = self.attachments
        with contextlib.suppress(Exception):
            target = Path(self.record_send_body)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def _record_request(self, req: Any) -> None:
        """Write one f/conversation POST's body to ``record_send_body``.

        Ignores every GET and the sentinel's own
        ``.../f/conversation/prepare`` handshake, so only the message
        itself documents what the composer actually sent -- with or
        without Web search -- for the lead's one real verification send
        (ROADMAP.md, Stage 3 item 2). Never raises: a recording failure
        must not fail the send it is only there to document.

        When this send pins effort, model, search or a hint, this writes a
        provisional record with ``"sent"`` equal to ``"original"``, and
        ``_rewrite_send_route`` overwrites it with the real ``"sent"`` once
        it has rewritten the body: the ``request`` event fires before any
        route has run, so this handler cannot know the rewritten body
        (2026-09-20: a record written only here showed ``sent`` equal to
        ``original`` while the reply's metadata proved the rewrite had gone
        out). A body the route declines (not a JSON object) keeps the
        provisional record, which is then exact. Without a rewrite the file
        keeps its plain ``"post_data"`` field.
        """
        if req.method != "POST":
            return
        path = req.url.split("?", 1)[0].split("#", 1)[0]
        if not path.endswith(self.RECORD_ENDPOINT):
            return
        if self.effort or self.model or self.search or self.hints:
            doc: dict[str, Any] = {
                "method": req.method,
                "url": req.url,
                "original": req.post_data,
                "sent": req.post_data,
            }
        else:
            doc = {"method": req.method, "url": req.url, "post_data": req.post_data}
        self._write_record(doc)

    def _is_send_stream_response(self, response: Any) -> bool:
        """The predicate ``_send``'s ``page.expect_response`` watches with.

        True for a POST whose URL (query and fragment stripped) ends with
        exactly ``RECORD_ENDPOINT`` -- never its ``/prepare`` handshake
        sibling, and never a GET; the same test ``_record_request`` uses.
        Only ``response.request`` is read, so this can be evaluated the
        moment the response object exists, before its body -- streaming or
        not -- has arrived.
        """
        req = response.request
        if req.method != "POST":
            return False
        path = req.url.split("?", 1)[0].split("#", 1)[0]
        return path.endswith(self.RECORD_ENDPOINT)

    def _record_send_stream(self, info: Any) -> None:
        """Write this send's own reply stream to a sibling ``.stream.txt``.

        ``info`` is the ``page.expect_response(...)`` context manager
        ``_send`` opened just before clicking the send button; ``_send``
        calls this only after its own post-click checks have confirmed the
        message actually posted. A Deep research send's connector session
        id -- what a later, plain-HTTP ``get_state`` call needs to follow
        the research after this window closes -- is returned only inside
        this response's own server-sent-event stream; the conversation
        JSON read back afterwards shows the tool reply as ``{}``.

        Reading ``info.value`` is itself a wait: it blocks until a response
        matching ``_is_send_stream_response`` has arrived, or raises once
        the timeout given to ``expect_response`` runs out. ``resp.finished()``
        then blocks until the whole streaming body has arrived -- recording
        a send means staying until its reply stream has ended, which is why
        this runs here, synchronously, instead of in a
        ``page.on("response", ...)`` handler (removed): that handler ran the
        moment the response object existed, before a still-streaming body
        had finished, and could not block for it, so a send whose window
        closed first produced a JSON record with no sibling stream file.
        ``resp.text()`` then returns the complete text. Everything here runs
        inside one ``contextlib.suppress(Exception)``: no matching response,
        a body that never finishes, or any other Playwright failure leaves
        the sibling file unwritten rather than failing a send that already
        succeeded.
        """
        with contextlib.suppress(Exception):
            resp = info.value
            resp.finished()
            text = resp.text()
            target = Path(self.record_send_body + STREAM_SUFFIX)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    RATE_LIMIT_TEXT = "too many requests"
    RATE_LIMIT_MODAL = '[data-testid="modal-conversation-history-rate-limit"]'

    def _check_rate_limit_dialog(self) -> str:
        """Clear ChatGPT's rate-limit dialog and report what it said.

        It does **not** raise. The dialog's test id is
        ``modal-conversation-history-rate-limit`` and its text says access to
        *conversations* is limited: it is about the history the sidebar reads,
        which our polling hammers, not about posting a message. The user
        confirmed from their own browser that dismissing it leaves ChatGPT
        working, and refusing to send on sight of it cost real runs.

        So dismiss it and let the send try. If the send is genuinely blocked
        it fails on its own, and ``_rate_limited_now`` turns that into the
        429 the dispatcher needs. Assuming the block was never measured; this
        way every occurrence measures it.
        """
        dialogs = self.page.locator('[role="dialog"], [role="alertdialog"]')
        seen = ""
        for i in range(min(dialogs.count(), 3)):
            try:
                text = dialogs.nth(i).inner_text(timeout=2_000)
            except Exception:  # an unreadable dialog is not our signal
                continue
            if self.RATE_LIMIT_TEXT in text.lower():
                seen = " ".join(text.split())[:160]
                break
        if not seen:
            return ""
        for label in ("Got it", "OK", "Okay", "Close"):
            btn = self.page.get_by_role("button", name=label, exact=True)
            if btn.count() and btn.first.is_visible():
                with contextlib.suppress(Exception):
                    btn.first.click(timeout=5_000)
                break
        else:
            with contextlib.suppress(Exception):
                self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(800)
        logger.warning("cleared the rate-limit notice and trying the send: %s", seen)
        return seen

    def _rate_limited_now(self) -> str:
        """The rate-limit notice's text if it is on the page, else empty.

        Checked after a send fails, which is the only moment the notice tells
        us anything: before the attempt it may be stale history nagging.
        """
        modal = self.page.locator(self.RATE_LIMIT_MODAL)
        with contextlib.suppress(Exception):
            if modal.count():
                return " ".join(modal.first.inner_text(timeout=2_000).split())[:160]
        return ""

    def _dismiss_overlay(self) -> str:
        """Press Escape to clear whatever modal is covering the composer.

        Any dialog, not only the rate-limit one, sets aria-hidden on the page
        behind it, and the composer then refuses a click for 30 s with nothing
        in the message to say why. Escape is the one dismissal that cannot
        agree to anything: no button is pressed, so no announcement is
        accepted and no setting is changed. The dialog's text is returned so
        the caller can say what was in the way.

        A rate-limit dialog is never *cleared to click through*. It is the one
        modal that means stop, so it raises 429 here and the dispatcher backs
        off; clearing it and retrying would answer a request for room with a
        faster retry. Seen once on 2026-09-16, when this path swallowed the
        limit instead of reporting it.
        """
        self._check_rate_limit_dialog()
        dialogs = self.page.locator('[role="dialog"], [role="alertdialog"]')
        seen = ""
        for i in range(min(dialogs.count(), 3)):
            with contextlib.suppress(Exception):
                seen = " ".join(dialogs.nth(i).inner_text(timeout=2_000).split())[:160]
            if seen:
                break
        with contextlib.suppress(Exception):
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(500)
        return seen

    def _focus_composer(self, composer: Any) -> None:
        """Put the caret in the composer, past anything covering it.

        The send button already had a forced-click fallback; the composer had
        none, so a modal turned a send into a 30 s timeout and a full retry
        cycle. Try politely, clear the overlay, try again, and only then force
        the click.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        try:
            composer.click(timeout=15_000)
            return
        except PWTimeout:
            pass
        blocking = self._dismiss_overlay()
        logger.warning(
            "the composer was covered%s; cleared it and retrying",
            f" by: {blocking}" if blocking else "",
        )
        try:
            composer.click(timeout=15_000)
            return
        except PWTimeout:
            pass
        with contextlib.suppress(Exception):
            self.page.screenshot(
                path=str(self.screenshot_dir / "chatgpt-composer-blocked.png")
            )
        composer.click(timeout=30_000, force=True)

    def _composer(self) -> Any:
        from playwright.sync_api import TimeoutError as PWTimeout

        self._check_rate_limit_dialog()
        composer = self.page.locator("#prompt-textarea")
        try:
            composer.wait_for(state="visible", timeout=60_000)
        except PWTimeout:
            self.page.screenshot(
                path=str(self.screenshot_dir / "chatgpt-composer-missing.png")
            )
            raise TransportError(
                f"composer did not appear at {self.page.url} (logged out or challenged)"
            ) from None
        return composer

    # Uploading the prompt instead of pasting it looked like the answer to slow
    # ProseMirror rendering, and the upload itself works: the file arrives and
    # the covering message types fine. Submission is what does not happen —
    # neither the send button nor Enter posts the turn while an attachment is
    # present, twice in a row, costing a research round each time. The paste
    # path has carried 149 KB successfully since the forced-click fix, so it
    # stays the default. Set RP_ATTACH_ABOVE_BYTES to re-enable uploading.
    ATTACH_ABOVE_BYTES = int(os.environ.get("RP_ATTACH_ABOVE_BYTES", 10**9))
    ATTACH_COVER = (
        "The attached file is your complete task. Read it and follow it exactly, "
        "including its reply format. Reply here in this conversation; do not "
        "summarise the file back to me."
    )

    def _upload_files(self, page: Any, paths: list[str]) -> None:
        """Upload ``paths`` through the composer's file input, then wait
        for the upload(s) to settle.

        The same primary selector and generic fallback, and the same
        "wait until attached, then poll the Uploading indicator clear"
        sequence ``_attach_prompt`` already used for the prompt-as-a-file
        case (below, now built on this). A single path is sent bare, the
        way ``_attach_prompt`` always has; more than one is sent as a
        single list in one call, because Playwright's ``set_input_files``
        replaces the input's files on every call rather than adding to
        them, so looping one at a time would leave only the last file
        attached.
        """
        file_input = page.locator("input#upload-files")
        if not file_input.count():
            file_input = page.locator('input[type="file"]:not([accept*="image"])')
        file_input.first.wait_for(state="attached", timeout=30_000)
        file_input.first.set_input_files(paths[0] if len(paths) == 1 else list(paths))
        # Measured 2026-09-20 on a 60-byte file: the chip appears at once,
        # the busy indicator only ~2.5 s later, and the backend's
        # process_upload_stream finishes ~10 s after the input was set. A
        # wait that only checks "not busy right now" returns before the
        # upload has even started, and a send clicked then is ignored
        # ("message was not posted"). So: wait for busy to appear and then
        # clear; if it never appears within UPLOAD_QUIET_MS, assume the
        # upload was instant.
        busy = page.locator(
            '[aria-label*="Uploading" i], [aria-busy="true"], [role="progressbar"]'
        )
        seen_busy = False
        waited = 0
        while waited < self.UPLOAD_MAX_MS:
            if busy.count():
                seen_busy = True
            elif seen_busy or waited >= self.UPLOAD_QUIET_MS:
                break
            page.wait_for_timeout(500)
            waited += 500
        page.wait_for_timeout(1_500)

    def _attach_prompt(self, page: Any, text: str, name: str) -> None:
        """Upload the prompt as a file and wait for the upload to finish."""
        from playwright.sync_api import TimeoutError as PWTimeout

        path = Path(tempfile.gettempdir()) / f"rp-prompt-{name}.md"
        path.write_text(text, encoding="utf-8")
        self._upload_files(page, [str(path)])
        chip = page.get_by_text(re.compile(re.escape(path.stem[:24]), re.I))
        try:
            chip.first.wait_for(state="visible", timeout=60_000)
        except PWTimeout:
            raise TransportError(
                "the attached prompt never appeared in the composer"
            ) from None

    def _click_send(self, page: Any, budget: int) -> bool:
        """Press the send button, forcing past the actionability check if needed.

        A large prompt keeps ProseMirror re-rendering, so the button never
        settles and Playwright's stability check waits until the timeout even
        though the button is present and enabled. A forced click skips that
        check; it is safe here because the button was just confirmed visible
        and enabled.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        for sel in self.SEND_BUTTONS:
            btn = page.locator(sel)
            if not (btn.count() and btn.first.is_visible() and btn.first.is_enabled()):
                continue
            try:
                btn.first.click(timeout=min(budget, 30_000))
                return True
            except PWTimeout:
                pass
            try:
                btn.first.click(timeout=budget, force=True)
                return True
            except PWTimeout:
                continue
        return False

    def send(self, text: str, chat: str | None = None, name: str = "task") -> str:
        """Post ``text``; return the conversation id, from any thread.

        Playwright failures (composer disabled while the model streams, page
        challenge, navigation timeout) surface as ``TransportError`` so the
        dispatcher's backoff and retry policy applies to them.
        """
        try:
            return self._owner.submit(self._send, text, chat, name).result()
        except TransportError:
            raise
        except Exception as exc:  # playwright errors have no common base here
            with contextlib.suppress(Exception):
                self.page.screenshot(
                    path=str(self.screenshot_dir / "chatgpt-send-error.png")
                )
            # Only now does the notice mean anything: the send was tried and
            # it failed. Before the attempt it may be stale history nagging,
            # and treating that as a block is what cost runs.
            limited = self._rate_limited_now()
            if limited:
                raise TransportError(
                    f"ChatGPT is rate-limiting this account: {limited}", 429
                ) from exc
            raise TransportError(f"browser send failed: {exc}"[:300]) from exc

    def probe_composer(self) -> str:
        """Load the page a send would compose on and wait for its composer;
        return the URL it appeared at. From any thread, like ``send``.

        This is what ``preflight.py --browser`` asks: the first half of
        ``_send`` -- navigate to ``new_chat_url()``, wait for
        ``#prompt-textarea`` -- and nothing after it: no fill, no click, no
        turn. A composer that never appears raises the same "logged out or
        challenged" ``TransportError`` a send would, so the preflight
        reports exactly what a send would have hit; any other failure is
        wrapped the way ``send`` wraps one.

        Until 2026-09-20 preflight called ``_composer`` itself, on the
        calling thread and on a page nothing had navigated: two defects,
        either of which alone made the check fail every time, with a
        message blaming the login. Playwright's sync objects belong to the
        owner thread, which is why every public method here submits to it
        and why the private methods are never the seam.
        """
        try:
            return self._owner.submit(self._probe_composer).result()
        except TransportError:
            raise
        except Exception as exc:  # playwright errors have no common base here
            raise TransportError(f"composer probe failed: {exc}"[:300]) from exc

    def _load_composer(self, url: str | None = None) -> Any:
        """Owner thread only. Load ``url`` (default: where a new chat is
        composed) and return its composer once visible. The one place the
        page is navigated: ``_send``, ``probe_composer`` and
        ``fill_composer`` all go through here. ``__enter__`` opens the window
        on about:blank, so nothing that wants a composer may skip this."""
        self.page.goto(
            url or self.new_chat_url(),
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_MS,
        )
        return self._composer()

    def _probe_composer(self) -> str:
        self._load_composer()
        return str(self.page.url)

    def fill_composer(self, text: str) -> tuple[float, float]:
        """Load the page a send would compose on and put ``text`` in its
        composer the way the paste path of a send does: focus, then
        ``fill`` under the send's own budget (``fill_budget_ms``). Never
        clicks send. From any thread, like ``send``.

        Returns ``(ready, fill)`` in seconds: how long the composer took to
        appear after the navigation began, and how long the fill took. This
        is what ``measure_window.py --fill-file`` measures, and the reason
        the measurement is honest: it is the send's own load, focus and fill,
        not a re-implementation beside them. The upload path a send takes
        above ``ATTACH_ABOVE_BYTES`` is not measured here.
        """
        try:
            return self._owner.submit(self._fill_composer, text).result()
        except TransportError:
            raise
        except Exception as exc:  # playwright errors have no common base here
            raise TransportError(f"composer fill failed: {exc}"[:300]) from exc

    def _fill_composer(self, text: str) -> tuple[float, float]:
        begin = time.monotonic()
        composer = self._load_composer()
        ready = time.monotonic() - begin
        self._focus_composer(composer)
        begin = time.monotonic()
        composer.fill(text, timeout=fill_budget_ms(len(text)))
        return ready, time.monotonic() - begin

    def attach_files(self, paths: list[str]) -> dict[str, Any]:
        """Load the page a send would compose on, upload ``paths`` through
        the composer's file input the way a send with ``attachments`` does,
        wait for the upload to settle, and report what the composer then
        shows: ``remove_labels`` (one "Remove file ..." label per chip, in
        page order), ``send_exists`` and ``send_enabled`` (the send button,
        scoped to the composer's own form) and ``url``. Never fills, never
        clicks. From any thread, like ``send``.

        The T3 browser dry run (tests/live/test_browser_upload.py) is built
        on this. Until 2026-09-20 it drove the private ``_composer``,
        ``_focus_composer`` and ``_upload_files`` itself, with its own copy
        of the state-reading script; it worked only because it also did its
        own navigation, which the other two private-seam callers
        (preflight.py, measure_window.py) had not.
        """
        try:
            return self._owner.submit(self._attach_files, list(paths)).result()
        except TransportError:
            raise
        except Exception as exc:  # playwright errors have no common base here
            raise TransportError(f"attachment probe failed: {exc}"[:300]) from exc

    def _attach_files(self, paths: list[str]) -> dict[str, Any]:
        composer = self._load_composer()
        self._focus_composer(composer)
        self._upload_files(self.page, paths)
        raw = self.page.evaluate(COMPOSER_STATE_JS)
        state = dict(raw) if isinstance(raw, dict) else {}
        return {
            "url": str(self.page.url),
            "remove_labels": [str(label) for label in state.get("remove_labels") or []],
            "send_exists": bool(state.get("send_exists")),
            "send_enabled": bool(state.get("send_enabled")),
        }

    def new_chat_url(self) -> str:
        """Where to compose a new conversation.

        Composing on a project's page creates the conversation *inside* that
        project, so the run's workers never reach the user's main chat list
        and nothing has to be deleted afterwards. A failed run then leaves its
        evidence somewhere findable rather than gone.

        ``project`` is a ``g-p-…`` id or the ``short_url`` slug; either works
        in the path.
        """
        if not self.project:
            return "https://chatgpt.com/"
        return f"https://chatgpt.com/g/{self.project}/project"

    def _send(self, text: str, chat: str | None = None, name: str = "task") -> str:
        page = self.page
        url = f"https://chatgpt.com/c/{chat_id(chat)}" if chat else self.new_chat_url()
        # 60 s was too tight for this host. On 2026-09-17, with three topics
        # running and binnacle reporting wlan2 down and wlan1 demoted, page
        # loads that would have finished kept expiring at 60 s; Topic 09 spent
        # its backoff ladder on them and aborted twice. Waiting longer can
        # only delay a load that was going to fail anyway -- it cannot break
        # one that would have worked -- so the ceiling is where the slack
        # belongs.
        composer = self._load_composer(url)
        # Chat surface only: leave the surface toggle alone when absent.
        chat_toggle = page.get_by_role("radio", name=re.compile(r"^Chat$", re.I))
        if chat_toggle.count() and chat_toggle.first.is_visible():
            state = chat_toggle.first.get_attribute("aria-checked") or ""
            if state != "true":
                chat_toggle.first.click(timeout=3_000)
                page.wait_for_timeout(500)
        turns_before = page.locator('[data-message-author-role="user"]').count()
        budget = fill_budget_ms(len(text))
        if len(text) > self.ATTACH_ABOVE_BYTES:
            # Before _attach_prompt runs: it uploads the prompt itself
            # through the same input, and set_input_files replaces rather
            # than adds, so any attachments must go through first.
            if self.attachments:
                self._upload_files(page, self.attachments)
            self._attach_prompt(page, text, name)
            self._focus_composer(composer)
            composer.fill(self.ATTACH_COVER, timeout=30_000)
        else:
            self._focus_composer(composer)
            if self.attachments:
                self._upload_files(page, self.attachments)
            composer.fill(text, timeout=budget)
        page.wait_for_timeout(800)
        # Start watching for this send's own reply now, before the click, so
        # record_send_body's stream capture is not missed; its value is not
        # read until the post-click checks below have confirmed the message
        # actually posted -- see _record_send_stream.
        capture = (
            page.expect_response(
                self._is_send_stream_response, timeout=max(budget, 60_000)
            )
            if self.record_send_body
            else contextlib.nullcontext()
        )
        with capture as info:
            sent = self._click_send(page, budget)
            if not sent:
                self._focus_composer(composer)
                page.keyboard.press("Enter")
            else:
                # The button can report a successful click without the app acting on
                # it, most often while an attachment is still settling. Give it a
                # moment, then submit from the keyboard instead of losing the turn.
                for _ in range(20):
                    page.wait_for_timeout(1_000)
                    if (
                        page.locator('[data-message-author-role="user"]').count()
                        > turns_before
                    ):
                        break
                else:
                    self._focus_composer(composer)
                    page.keyboard.press("Enter")
        # Confirm the post: a new user bubble and, for a new chat, a /c/ URL.
        posted = False
        for _ in range(90):
            page.wait_for_timeout(1_000)
            posted = (
                page.locator('[data-message-author-role="user"]').count() > turns_before
            )
            if posted and "/c/" in page.url and not is_provisional(page.url):
                if self.record_send_body:
                    self._record_send_stream(info)
                return chat_id(page.url)
        if posted and "/c/" in page.url:
            if self.record_send_body:
                self._record_send_stream(info)
            return chat_id(page.url)  # provisional WEB: id; the caller resolves it
        page.screenshot(path=str(self.screenshot_dir / "chatgpt-send-fail.png"))
        raise TransportError(
            "message was not posted (no new user turn / conversation URL)"
        )
