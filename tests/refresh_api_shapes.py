#!/usr/bin/env python3
"""Promote recorded API shapes into tests/fixtures/http/
(``make refresh-shapes``; TESTING.md section 6, P5).

    refresh_api_shapes.py [SHAPES_DIR] [--replace] [--dest DIR]

SHAPES_DIR defaults to the newest daily run's ``http`` directory
(``~/.local/state/chatgpt-ops/runs/<last-run-id>/http``). Each fresh shape is
merged into the committed one (``api_shapes.merge``): a field present in only
one of them becomes optional, types and formats are unioned, so the committed
shape learns what varies from day to day. ``--replace`` writes the fresh
shape as it is (after an API change that removed fields for good). Every file
is held to ``test_fixture_hygiene``'s rules before it is written.

Never run from cron. A shape change is a reviewed commit: read the diff, run
``make contract`` and ``make read-paths``, then commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
for path in (HERE, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import api_shapes  # noqa: E402
import test_fixture_hygiene as hygiene  # noqa: E402

STATE = Path.home() / ".local" / "state" / "chatgpt-ops"


def default_source() -> Path:
    """The newest daily run's ``http`` directory, from ``last-run-id``."""
    run_id = (STATE / "last-run-id").read_text(encoding="utf-8").strip()
    return STATE / "runs" / run_id / "http"


def promote(source: Path, dest: Path, replace: bool = False) -> dict[str, str]:
    """``{name: written|unchanged}`` for every shape in ``source``. Raises
    ``FileNotFoundError`` for an empty source and ``ValueError`` when a shape
    would fail the hygiene rules; nothing is written then."""
    fresh = api_shapes.load(source)
    if not fresh:
        raise FileNotFoundError(f"no *{api_shapes.SHAPE_SUFFIX} in {source}")
    committed = api_shapes.load(dest)
    texts: dict[str, str] = {}
    problems: list[str] = []
    for name, shape in sorted(fresh.items()):
        if name not in api_shapes.BY_NAME:
            problems.append(f"{name}: not a registered endpoint")
            continue
        merged = (
            shape
            if replace or name not in committed
            else api_shapes.merge(committed[name], shape)
        )
        text = json.dumps(merged, indent=1, sort_keys=True) + "\n"
        problems += [f"{name}: {p}" for p in hygiene.leaks_in(text)]
        texts[name] = text
    if problems:
        raise ValueError("refusing to promote: " + "; ".join(problems))
    dest.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for name, text in texts.items():
        target = dest / f"{name}{api_shapes.SHAPE_SUFFIX}"
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current == text:
            result[name] = "unchanged"
        else:
            target.write_text(text, encoding="utf-8")
            result[name] = "written"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("source", nargs="?", help="recorded shapes (default: newest run)")
    ap.add_argument("--replace", action="store_true", help="do not merge")
    ap.add_argument("--dest", default=str(api_shapes.COMMITTED))
    args = ap.parse_args(argv)
    try:
        source = Path(args.source) if args.source else default_source()
        result = promote(source, Path(args.dest), args.replace)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"refresh_api_shapes: {exc}")
        return 1
    for name, state in result.items():
        print(f"{name}: {state}")
    if "written" in result.values():
        print("review the diff, run `make contract` and `make read-paths`, then commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
