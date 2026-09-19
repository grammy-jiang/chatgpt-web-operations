#!/usr/bin/env python3
"""The hidden inputs of a run: what the profile looks like before the first send.

    profile_context.py [--project g-p-<id>] [--json PATH]

A worker conversation inherits more than its prompt: the account's custom
instructions, its memory, the model and effort the composer last used and,
inside a project, that project's instructions and files. A transcript records
none of it. This command reads all of it over plain HTTP and prints a summary
of lengths and counts; ``--json PATH`` writes the full text and numbers so a
run can keep them next to ``chatgpt/conversations.json`` (``-`` prints the
document instead of the summary).

Read-only. Memory entries are counted, never kept: their content goes
neither to the document nor to the terminal.

Exit 0 when every read answered, 1 when one did not; the document still
records what was read and lists the failures under ``errors``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _common import ensure_venv, load_client, open_session
from list_projects import project_of
from model_settings import (
    CONFIG_COOKIE,
    MODELS,
    effort_levels_of,
    presets_of,
    profile_config,
    resolve_preset,
)

USER_SYSTEM_MESSAGES = "/backend-api/user_system_messages"
MEMORY_SUMMARY = "/backend-api/memories?include_memory_entries=false"
MEMORY_ENTRIES = "/backend-api/memories?include_memory_entries=true"
SETTINGS = "/backend-api/settings/user"
GIZMO = "/backend-api/gizmos/{id}"

# The send path drives the web composer, so that is the surface whose
# server-side record matters. settings/user also keeps ios_app and windows_app.
SURFACE = "web"
# The account-wide "Enable memory" switch lives here and nowhere the reads
# above expose (checked 2026-09-20); the command reports usage instead.
PERSONALIZATION_PAGE = "https://chatgpt.com/#settings/Personalization"


def custom_instructions_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The four fields the Personalization tab edits, and whether they apply.

    ``user_system_messages`` carries each text twice, under an old name and a
    new one (``about_user_message`` / ``other_user_message``,
    ``about_model_message`` / ``traits_model_message``); the new names are
    the ones kept here.
    """
    return {
        "enabled": bool(payload.get("enabled")),
        "name": str(payload.get("name_user_message") or ""),
        "role": str(payload.get("role_user_message") or ""),
        "traits": str(payload.get("traits_model_message") or ""),
        "about": str(payload.get("other_user_message") or ""),
        "traits_enabled": bool(payload.get("traits_enabled")),
        "personality": str(payload.get("personality_type_selection") or ""),
        "disabled_tools": [str(t) for t in payload.get("disabled_tools") or []],
    }


def memory_of(summary: dict[str, Any], entries: dict[str, Any]) -> dict[str, Any]:
    """Usage from the page's own call, a count from the entries; no content.

    ``entries`` is empty when the entries could not be read, and the count
    is then ``None``: an unread memory must not look like an empty one.
    """
    items = entries.get("memories")
    counted = isinstance(items, list)
    return {
        "tokens_used": int(summary.get("memory_num_tokens") or 0),
        "tokens_max": int(summary.get("memory_max_tokens") or 0),
        "entries": len(items) if counted else None,
        "entries_project_scoped": (
            sum(1 for m in items if isinstance(m, dict) and m.get("gizmo_id"))
            if counted
            else None
        ),
    }


def preset_for(
    presets: list[dict[str, Any]], model: str, effort: str
) -> dict[str, Any] | None:
    """The slider position for a (model, effort) pair.

    An exact match first. Failing that, a preset that is the model alone
    (Instant, Pro) matches whatever effort the server remembers for it, since
    those positions carry no effort of their own.
    """
    found = resolve_preset(presets, model, effort)
    if found is None and model and effort:
        found = resolve_preset(presets, model, "")
    return found


def _placed(presets: list[dict[str, Any]], model: str, effort: str) -> dict[str, Any]:
    found = preset_for(presets, model, effort)
    return {
        "model": model,
        "effort": effort,
        "preset": found["title"] if found else None,
        "position": found["position"] if found else None,
    }


def model_of(
    models: dict[str, Any], settings: dict[str, Any], cookie: dict[str, str]
) -> dict[str, Any]:
    """What a send without an explicit effort would use, from both records.

    The browser's ``oai-last-model-config`` cookie is what the composer reads;
    ``settings/user`` keeps the server's copy per surface in
    ``last_used_model_config`` (``slugs`` for the model, ``juices`` for the
    effort per model). Both are reported so a drift between them is visible.
    """
    presets = presets_of(models)
    prefs = settings.get("settings") or {}
    last = prefs.get("last_used_model_config") or {}
    slug = str((last.get("slugs") or {}).get(SURFACE) or "")
    juices = (last.get("juices") or {}).get(SURFACE) or {}
    default = prefs.get("default_model_config") or {}
    return {
        "presets": presets,
        "api_levels": effort_levels_of(models),
        "cookie": (
            _placed(presets, cookie.get("model", ""), cookie.get("effort", ""))
            if cookie
            else None
        ),
        "server": {
            "surface": SURFACE,
            **_placed(presets, slug, str(juices.get(slug) or "")),
            "efforts_by_model": {str(k): str(v) for k, v in juices.items()},
            "default_model_slug": default.get("default_model_slug"),
            "sticky_for_new_chats": bool(prefs.get("model_sticky_for_new_chats")),
        },
        "ultra_effort_enabled": bool(prefs.get("model_picker_persists_ultra_effort")),
    }


def collect(
    session: Any, cc: Any, browser: str, project_id: str = ""
) -> dict[str, Any]:
    """Every read, in one document; a failed read is recorded, not raised."""
    errors: list[str] = []

    def read(path: str) -> dict[str, Any]:
        status, body = session.session.call(path)
        if status != 200 or not isinstance(body, dict):
            errors.append(f"{path.split('?')[0]}: HTTP {status}")
            return {}
        return body

    return {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "custom_instructions": custom_instructions_of(read(USER_SYSTEM_MESSAGES)),
        "memory": memory_of(read(MEMORY_SUMMARY), read(MEMORY_ENTRIES)),
        "model": model_of(read(MODELS), read(SETTINGS), profile_config(cc, browser)),
        "project": (
            project_of(read(GIZMO.format(id=project_id))) if project_id else None
        ),
        "errors": errors,
    }


def _placement(placed: dict[str, Any], total: int) -> str:
    where = (
        f"preset {placed['preset']!r} ({placed['position']} of {total})"
        if placed["preset"]
        else "matches no preset"
    )
    return f"{placed['model'] or '-'} / {placed['effort'] or '-'} -> {where}"


def render(doc: dict[str, Any]) -> str:
    """Lengths and counts only: a run's log is no place for the profile text."""
    ci = doc["custom_instructions"]
    mem = doc["memory"]
    model = doc["model"]
    total = len(model["presets"])
    lines = [f"profile context read from chatgpt.com over HTTP at {doc['captured_at']}"]

    lines.append("")
    lines.append(
        f"custom instructions ({USER_SYSTEM_MESSAGES}): "
        + ("enabled" if ci["enabled"] else "disabled")
    )
    lines.append(
        f"  name {len(ci['name'])} chars, role {len(ci['role'])} chars, "
        f"traits {len(ci['traits'])} chars, about {len(ci['about'])} chars; "
        f"personality {ci['personality'] or '-'}"
    )

    entries = (
        "entries not read"
        if mem["entries"] is None
        else f"{mem['entries']} entries "
        f"({mem['entries_project_scoped']} project-scoped)"
    )
    lines.append(
        f"memory ({MEMORY_SUMMARY.split('?')[0]}): {entries}, "
        f"{mem['tokens_used']} of {mem['tokens_max']} tokens"
    )
    lines.append(f"  the 'Enable memory' switch shows only at {PERSONALIZATION_PAGE}")

    lines.append(f"model ({MODELS.split('?')[0]}, {SETTINGS}, cookie):")
    cookie = model["cookie"]
    lines.append(
        f"  cookie {CONFIG_COOKIE}: "
        + (_placement(cookie, total) if cookie else "unreadable or absent")
    )
    server = model["server"]
    lines.append(
        f"  server last_used_model_config[{server['surface']}]: "
        + _placement(server, total)
    )
    lines.append(
        "  Ultra effort setting "
        + ("on" if model["ultra_effort_enabled"] else "off")
        + "; model sticky for new chats "
        + ("on" if server["sticky_for_new_chats"] else "off")
    )

    project = doc["project"]
    if project:
        lines.append(
            f"project {project['id']}: {project['name']!r} ({GIZMO.format(id='<id>')})"
        )
        lines.append(
            f"  instructions {len(project['instructions'])} chars, "
            f"{len(project['files'])} file(s), memory "
            + ("enabled" if project["memory_enabled"] else "disabled")
            + f", scope {project['memory_scope'] or '-'}, "
            f"context budget {project['context_stuffing_budget'] or '-'}"
        )
        lines.append(f"  {project['url']}")

    if doc["errors"]:
        lines.append("")
        lines.append(f"NOT COMPLETE: {len(doc['errors'])} read(s) failed")
        lines.extend(f"  {e}" for e in doc["errors"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--project",
        default="",
        metavar="g-p-ID",
        help="also read this project's instructions and files",
    )
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="write the full document here; - prints it instead of the summary",
    )
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    cc = load_client()
    session = open_session(args.browser)
    doc = collect(session, cc, args.browser, args.project)
    text = json.dumps(doc, indent=2, ensure_ascii=False)

    if args.json == "-":
        print(text)
    else:
        print(render(doc))
        if args.json:
            out = Path(args.json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
            print(f"\nwritten to {out}")
    return 1 if doc["errors"] else 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
