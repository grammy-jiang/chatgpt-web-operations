#!/usr/bin/env python3
"""Promote a recorded DOM snapshot into tests/fixtures/dom/
(``make refresh-dom-fixtures``; TESTING.md section 6, P1).

    refresh_dom_fixtures.py [SNAPSHOT_DIR] [--name composer] [--dest DIR]

SNAPSHOT_DIR defaults to the newest daily run's ``dom`` directory
(``~/.local/state/chatgpt-ops/runs/<last-run-id>/dom``). ``<name>.html`` is
re-sanitized on the way in (``chatgpt_client.sanitize_html`` over its
``<main>``, so a hand-edited copy cannot smuggle text in) and
``<name>.json`` goes through ``record_fixture.sanitize`` (ids, emails).
Both are then held to ``test_fixture_hygiene``'s rules and refused on a
hit: this script writes nothing the suite would fail on.

Never run from cron. A fixture change is a reviewed commit: read the diff,
run ``make replay``, then commit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
for path in (HERE, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import chatgpt_client as cc  # noqa: E402
import record_fixture  # noqa: E402
import test_fixture_hygiene as hygiene  # noqa: E402

DEST = HERE / "fixtures" / "dom"
STATE = Path.home() / ".local" / "state" / "chatgpt-ops"
_MAIN = re.compile(r"<main>(.*)</main>", re.S)


def default_source() -> Path:
    """The newest daily run's ``dom`` directory, from ``last-run-id``."""
    run_id = (STATE / "last-run-id").read_text(encoding="utf-8").strip()
    return STATE / "runs" / run_id / "dom"


def promote(source: Path, name: str, dest: Path) -> dict[str, str]:
    """Copy ``<name>.html`` and ``<name>.json`` from ``source`` to ``dest``,
    sanitized; return ``{"html": ..., "json": ...}`` with ``written`` or
    ``unchanged``. Raises ``ValueError`` when either file would fail the
    hygiene rules, and ``FileNotFoundError`` when a file is missing."""
    html_src = source / f"{name}.html"
    json_src = source / f"{name}.json"
    for path in (html_src, json_src):
        if not path.is_file():
            raise FileNotFoundError(f"no {path.name} in {source}")
    markup = html_src.read_text(encoding="utf-8")
    inner = _MAIN.search(markup)
    body = cc.sanitize_html(inner.group(1) if inner else markup)
    document = cc.snapshot_document({"html": {"composer": body, "turns": []}})
    record = json.loads(json_src.read_text(encoding="utf-8"))
    # Only the URL can carry an id (a project's g-p- id, a conversation's
    # uuid). The other strings are selectors and UI labels, and the generic
    # sanitizer would rewrite a selector such as [data-user-message-bubble]
    # into nonsense, which is what it did on the first promotion.
    if isinstance(record, dict) and isinstance(record.get("url"), str):
        record["url"] = record_fixture.sanitize(record["url"])
    record_text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    problems = hygiene.leaks_in_markup(document) + [
        f"json: {p}" for p in hygiene.leaks_in_facts(record_text)
    ]
    if problems:
        raise ValueError(f"refusing to promote {name}: {'; '.join(problems)}")
    dest.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for suffix, text in (("html", document), ("json", record_text)):
        target = dest / f"{name}.{suffix}"
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current == text:
            result[suffix] = "unchanged"
        else:
            target.write_text(text, encoding="utf-8")
            result[suffix] = "written"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument(
        "source", nargs="?", help="snapshot directory (default: newest run)"
    )
    ap.add_argument(
        "--name", default="composer", help="snapshot name (default composer)"
    )
    ap.add_argument(
        "--dest", default=str(DEST), help=f"fixture directory (default {DEST})"
    )
    args = ap.parse_args(argv)
    try:
        source = Path(args.source) if args.source else default_source()
        result = promote(source, args.name, Path(args.dest))
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"refresh_dom_fixtures: {exc}")
        return 1
    for suffix, state in result.items():
        print(f"{args.name}.{suffix}: {state}")
    if "written" in result.values():
        print("review the diff, run `make replay`, then commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
