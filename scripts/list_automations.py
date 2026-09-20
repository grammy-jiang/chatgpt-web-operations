#!/usr/bin/env python3
"""List ChatGPT's scheduled tasks ("automations"). Read-only, plain HTTP.

    list_automations.py [--filter scheduled|paused|finished|all] [--prompts]
                        [--json PATH] [--browser chrome]

The page at ``https://chatgpt.com/scheduled`` (``chatgpt.com/tasks``
redirects there) calls, for each of its three tabs:

    GET /backend-api/automations?filter=scheduled   # the "Active" list
    GET /backend-api/automations?filter=paused
    GET /backend-api/automations?filter=finished

Captured 2026-09-21 over plain HTTP against this account
(``references/endpoint-discovery.md``, "Seen on 2026-09-21"). 350-750 ms
per filter. Each response is ``{"items": [...], "cursor": <str|null>}``;
the cursor was null for all three captures seen (7, 13 and 5 items), so
paging is not implemented here -- the paging parameter is unknown -- and a
non-null cursor instead prints "more exist (cursor returned, not
followed)" rather than being guessed at.

Every item, in all three filters, carries the same keys, measured
2026-09-21: ``id``, ``title``, ``display_title``, ``display_emoji``,
``prompt``, ``conversation_id``, ``timing_mode`` (``condition_watch``, a
monitor that polls; ``exact_schedule``; ``flexible_schedule``),
``schedule`` (an iCalendar VEVENT string, for example
``"BEGIN:VEVENT\\nDTSTART:20260830T075934Z\\nRRULE:FREQ=HOURLY\\nEND:VEVENT"``),
``schedule_components`` (``frequency``, ``by_day``, ``by_hour``,
``by_minute``, ``by_month``, ``by_month_day``, ``by_second``,
``by_year_day``, ``start_time`` -- each null or a string),
``display_schedule`` (``"Monitoring"`` for a ``condition_watch`` item,
else null), ``next_run_times`` (a list of ISO strings carrying their own
UTC offset, empty for a finished item), ``last_run_time`` (an ISO UTC
string, or null), ``is_enabled`` (true only for ``scheduled``),
``executor`` ("cloud" on every item seen), ``thread_mode``
("existing_chat" on every item seen), plus ``can_delete``, ``complexity``,
``content_type``, ``created_by_display_name``, ``current_user_role``,
``default_timezone``, ``email_enabled``, ``external_channel``,
``last_edited_at``, ``last_edited_by_display_name``,
``notifications_enabled``, ``schedule_time_of_day``,
``source_conversation_is_work_mode``, ``target_time_utc``, ``team_id``,
``updated_at``, ``webhook_triggers``: none of those extra fields is shown
in the table, but every raw item written by ``--json`` still carries them.

``prompt`` is the task's own instruction text -- the user's content, the
same way a chat title or a search snippet is elsewhere in this skill -- so
it is never printed unless ``--prompts`` asks for it, and even then it is
shortened.

``GET /backend-api/suggested_automations`` (three template suggestions:
title, description, user_message, suggested_automation_type, system_hint,
emoji, icon_url, announcement_id) is documented in
``references/endpoint-discovery.md`` but not used here: it proposes a
*new* automation to create, which this read-only command does not do.
``GET /backend-api/tasks`` looks like it should be this command's
endpoint and is not: it is the account's background-task history (mostly
the retired Deep research mechanism, plus image generations), a different
and older thing -- see that file's corrected row and do not point this
command at it.

``schedule_label()`` turns ``schedule_components`` into a short label such
as "hourly @:59", "daily 09:00", "weekly mon,thu" or "yearly 06-23";
``display_schedule`` wins when it is set, and the RRULE line inside the
raw ``schedule`` string is the fallback when every component is null.
This mapping is inferred from the iCalendar shapes measured 2026-09-21,
not from any OpenAI documentation, and an unrecognised ``frequency``
prints verbatim rather than guessing at a format.

With ``--filter all``, the three filters are fetched in the order
scheduled, paused, finished and the table gains a leading "state" column.
``--json``'s ``items`` is then the concatenation of the three raw
responses in that same order; a raw item does not itself say which bucket
it came from (``is_enabled`` is true only for scheduled, so paused and
finished read alike there) -- read the table's "state" column, or rerun
with a single ``--filter``, when that distinction matters.

Exit 0 when every filter asked for was read (an empty list is a valid
answer for any of them). Exit 1 when a filter's read failed -- the failing
filter and its HTTP status are printed, and whatever filters already
succeeded are still shown, the same way ``search_chats.py`` keeps earlier
pages after a later one fails. Exit 2 for an unrecognised ``--filter``,
before any network call.

Plain HTTP, GET only, like every other read command here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session, shorten, table

AUTOMATIONS = "/backend-api/automations"
FILTERS = ("scheduled", "paused", "finished")
DEFAULT_FILTER = "scheduled"

TITLE_WIDTH = 48
PROMPT_WIDTH = 60

DAY_CODES = {
    "MO": "mon",
    "TU": "tue",
    "WE": "wed",
    "TH": "thu",
    "FR": "fri",
    "SA": "sat",
    "SU": "sun",
}


def automations_url(filter_name: str) -> str:
    """The listing URL for one filter tab."""
    return f"{AUTOMATIONS}?filter={filter_name}"


def _iso_local_minute(value: Any) -> str:
    """ "YYYY-MM-DD HH:MM" parsed from an ISO 8601 string, kept in whatever
    UTC offset it already carries (never converted), or "-" when ``value``
    is empty or does not parse."""
    text = str(value or "").strip()
    if not text:
        return "-"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return "-"
    return parsed.strftime("%Y-%m-%d %H:%M")


def next_run_label(next_run_times: list[Any]) -> str:
    """The "next run" cell: the first of ``next_run_times``, or "-"."""
    if not next_run_times:
        return "-"
    return _iso_local_minute(next_run_times[0])


def last_run_label(last_run_time: Any) -> str:
    """The "last run" cell: ``last_run_time`` itself, or "-" when null."""
    return _iso_local_minute(last_run_time)


def on_label(is_enabled: Any) -> str:
    """ "yes" / "no" for the "on" cell, from ``is_enabled``."""
    return "yes" if is_enabled else "no"


def _two_digits(value: Any) -> str:
    """``value`` as a zero-padded two-digit string, or itself if that
    fails -- a component seen as "9" must print "09", but an unexpected
    shape should never raise."""
    text = str(value or "").strip()
    try:
        return f"{int(text):02d}"
    except ValueError:
        return text


def _days_label(by_day: Any) -> str:
    """iCalendar BYDAY codes ("MO,TH") as "mon,thu"; anything unrecognised
    passes through lower-cased, still comma-joined."""
    parts = [p.strip() for p in str(by_day or "").split(",") if p.strip()]
    return ",".join(DAY_CODES.get(p.upper(), p.lower()) for p in parts)


def rrule_line(schedule: str) -> str:
    """The value of the RRULE line inside a raw iCalendar ``schedule``
    string (for example "FREQ=HOURLY"), or "" when there is no such line."""
    for line in str(schedule or "").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("RRULE:"):
            return stripped[len("RRULE:") :].strip()
    return ""


def schedule_label(
    components: dict[str, Any], display_schedule: Any, schedule: str
) -> str:
    """The compact "schedule" table cell for one automation.

    ``display_schedule`` wins when it is set (the "Monitoring" label
    ChatGPT already computes for a ``condition_watch`` item). Otherwise
    this builds a short label from ``schedule_components`` -- "hourly
    @:59", "daily 09:00", "weekly mon,thu", "yearly 06-23" -- and when
    every component is null (seen on some items), falls back to the RRULE
    line inside the raw ``schedule`` string, or "-" when neither is
    available.
    """
    if display_schedule:
        return str(display_schedule)
    components = components or {}
    frequency = str(components.get("frequency") or "").strip().lower()
    if frequency == "hourly":
        minute = components.get("by_minute")
        return f"hourly @:{minute}" if minute else "hourly"
    if frequency == "daily":
        hour = _two_digits(components.get("by_hour"))
        minute = _two_digits(components.get("by_minute"))
        return f"daily {hour}:{minute}"
    if frequency == "weekly":
        days = _days_label(components.get("by_day"))
        return f"weekly {days}" if days else "weekly"
    if frequency == "yearly":
        month = _two_digits(components.get("by_month"))
        day = _two_digits(components.get("by_month_day"))
        return f"yearly {month}-{day}"
    if frequency:
        return frequency
    return rrule_line(schedule) or "-"


def title_label(item: dict[str, Any], width: int = TITLE_WIDTH) -> str:
    """``display_emoji`` + ``title``, shortened for a table cell."""
    emoji = str(item.get("display_emoji") or "").strip()
    title = str(item.get("title") or "(untitled)")
    text = f"{emoji} {title}" if emoji else title
    return shorten(text, width)


def prompt_label(item: dict[str, Any], width: int = PROMPT_WIDTH) -> str:
    """The task's own instruction text, shortened; only shown with
    ``--prompts`` (module docstring: this is the user's own content)."""
    return shorten(str(item.get("prompt") or ""), width)


def headers_for(*, with_state: bool, show_prompts: bool) -> tuple[str, ...]:
    """Table headers in column order, adding "state" and/or "prompt"."""
    headers = [
        "next run",
        "last run",
        "mode",
        "schedule",
        "on",
        "title",
        "conversation",
    ]
    if with_state:
        headers.insert(0, "state")
    if show_prompts:
        headers.append("prompt")
    return tuple(headers)


def row_for(
    item: dict[str, Any],
    *,
    with_state: bool = False,
    state: str = "",
    show_prompt: bool = False,
) -> tuple[str, ...]:
    """One table row for one automation, in ``headers_for``'s column order."""
    row: list[str] = []
    if with_state:
        row.append(state)
    row.extend(
        [
            next_run_label(item.get("next_run_times") or []),
            last_run_label(item.get("last_run_time")),
            str(item.get("timing_mode") or "-"),
            schedule_label(
                item.get("schedule_components") or {},
                item.get("display_schedule"),
                str(item.get("schedule") or ""),
            ),
            on_label(item.get("is_enabled")),
            title_label(item),
            str(item.get("conversation_id") or "-"),
        ]
    )
    if show_prompt:
        row.append(prompt_label(item))
    return tuple(row)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--filter",
        default=DEFAULT_FILTER,
        help=f"one of {', '.join(FILTERS)}, or all (default {DEFAULT_FILTER})",
    )
    ap.add_argument(
        "--prompts", action="store_true", help="add a shortened prompt column"
    )
    ap.add_argument(
        "--json", default="", metavar="PATH", help="write the raw items here"
    )
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    if args.filter not in (*FILTERS, "all"):
        print(
            f"--filter {args.filter!r} is refused: choose one of "
            f"{', '.join(FILTERS)}, or all"
        )
        return 2

    to_fetch = list(FILTERS) if args.filter == "all" else [args.filter]
    with_state = args.filter == "all"

    session = open_session(args.browser)

    rows: list[tuple[str, ...]] = []
    raw_items: list[dict[str, Any]] = []
    more_in: list[str] = []
    failure = ""

    for filter_name in to_fetch:
        status, body = session.session.call(automations_url(filter_name))
        if status != 200 or not isinstance(body, dict):
            failure = f"filter {filter_name!r} failed: HTTP {status} {str(body)[:200]}"
            break
        items = list(body.get("items") or [])
        raw_items.extend(items)
        rows.extend(
            row_for(
                item, with_state=with_state, state=filter_name, show_prompt=args.prompts
            )
            for item in items
        )
        if body.get("cursor") is not None:
            more_in.append(filter_name)

    print(table(rows, headers_for(with_state=with_state, show_prompts=args.prompts)))
    print()
    print(f"{len(rows)} automation(s) ({args.filter})")
    if more_in:
        where = f": {', '.join(more_in)}" if with_state else ""
        print(f"more exist (cursor returned, not followed){where}")
    if failure:
        print(failure)

    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = {"filter": args.filter, "items": raw_items}
        out.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwritten to {out}")

    return 1 if failure else 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
