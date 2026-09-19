#!/usr/bin/env python3
"""Record a sanitized fixture from one real backend-api GET.

    record_fixture.py <backend-api path> <fixture-name> [--browser chrome]

Writes tests/fixtures/<fixture-name>.json. Not collected by pytest (no
test_ prefix) and not run by any test or agent: it authenticates against
the real account over the network, so only a human runs it, by hand
(TESTING.md section 1; SKILL.md's transport split). ``sanitize()`` below is
pure and has no side effects; that is what tests/test_harness.py exercises,
and tests/test_fixture_hygiene.py checks its output stayed clean on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_USER_ID_RE = re.compile(r"user-[A-Za-z0-9]+")
_ORG_ID_RE = re.compile(r"org-[A-Za-z0-9]+")
_GP_ID_RE = re.compile(r"g-p-[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Keys whose text is free-form account content: kept only as a length, never
# as text, regardless of whether it also happens to match a pattern above.
REDACT_LENGTH_KEYS = {
    "about_user_message",
    "other_user_message",
    "about_model_message",
    "traits_model_message",
    "name_user_message",
    "role_user_message",
    "instructions",
    "content",
    "snippet",
    "title",
    "display_name",
    "first_name",
    "phone_number",
}


def sanitize(obj: Any, *, keep: Iterable[str] = (), redact: Iterable[str] = ()) -> Any:
    """A copy of ``obj`` with ids, emails and free text replaced.

    A g-p- id or a UUID keeps one placeholder everywhere it appears in this
    call, assigned in the order it is first seen, so a fixture stays
    internally consistent (one project, one conversation) without keeping
    the real value anywhere. Numbers, booleans and null pass through as-is.

    ``keep`` names keys from ``REDACT_LENGTH_KEYS`` to leave as text for one
    fixture whose values are public (the model presets' ``title``);
    ``redact`` adds keys for one fixture whose values are personal (a
    sidebar's project ``name``). Both are recorded in the fixture's
    ``README.md`` entry.
    """
    length_keys = (REDACT_LENGTH_KEYS | set(redact)) - set(keep)
    gp_map: dict[str, str] = {}
    uuid_map: dict[str, str] = {}

    def gp_placeholder(match: re.Match[str]) -> str:
        real = match.group(0)
        if real not in gp_map:
            gp_map[real] = f"g-p-{len(gp_map) + 1:032d}"
        return gp_map[real]

    def uuid_placeholder(match: re.Match[str]) -> str:
        real = match.group(0)
        if real not in uuid_map:
            uuid_map[real] = f"00000000-0000-4000-8000-{len(uuid_map) + 1:012d}"
        return uuid_map[real]

    def sanitize_string(text: str) -> str:
        text = _EMAIL_RE.sub("user@example.invalid", text)
        text = _USER_ID_RE.sub("user-XXXXXXXX", text)
        text = _ORG_ID_RE.sub("org-XXXXXXXX", text)
        text = _GP_ID_RE.sub(gp_placeholder, text)
        text = _UUID_RE.sub(uuid_placeholder, text)
        return text

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            if key in length_keys:
                return f"<redacted {len(node)} chars>" if node else ""
            return sanitize_string(node)
        return node

    return walk(obj)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="a /backend-api/... path, with its query string")
    parser.add_argument("fixture_name", help="written to tests/fixtures/<name>.json")
    parser.add_argument("--browser", default="chrome", choices=["chrome", "chromium"])
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="KEY",
        help="leave this normally redacted key as text (public values only)",
    )
    parser.add_argument(
        "--redact",
        action="append",
        default=[],
        metavar="KEY",
        help="also redact this key for this fixture",
    )
    args = parser.parse_args(argv)

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import chatgpt_client  # imported here only: never at module import time

    session = chatgpt_client.ChatGPTSession(args.browser)
    status, body = session.session.call(args.path)
    if status != 200:
        print(f"record_fixture: HTTP {status} on {args.path}", file=sys.stderr)
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{args.fixture_name}.json"
    cleaned = sanitize(body, keep=args.keep, redact=args.redact)
    out.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
