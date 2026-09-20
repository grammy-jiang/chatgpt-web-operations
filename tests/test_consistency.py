"""Consistency tests (TESTING.md, kind "consistency"): the docs and the code
must never drift apart silently.

Every check here reads the current tree rather than a fixed list, so it
keeps working as commands are added or removed: a command cannot be added
without its ``SKILL.md`` row, and an endpoint constant cannot be added
without a mention in the transport map. Anything this file finds wrong in
the docs is reported, not silently fixed here -- these tests do not own
``SKILL.md``, ``TESTING.md`` or ``references/endpoint-discovery.md``.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
SKILL_MD = SKILL_DIR / "SKILL.md"
TESTING_MD = SKILL_DIR / "TESTING.md"
MAKEFILE = SKILL_DIR / "Makefile"
ENDPOINT_DISCOVERY_MD = SKILL_DIR / "references" / "endpoint-discovery.md"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Bundled library modules SKILL.md describes in prose ("Where this lives, and
# what it needs" / "_common.py holds only session bootstrap..."), never as a
# row of "## The commands": these are not commands.
LIBRARY_MODULES = {
    "_common.py",
    "round_state.py",
    "chatgpt_client.py",
    "chatgpt_session.py",
    "chatgpt_cookies.py",
}


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _command_names() -> list[str]:
    """scripts/*.py filenames that are commands: they define main() and are
    not one of the bundled library modules."""
    names = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name in LIBRARY_MODULES:
            continue
        module = _import(path.stem)
        if hasattr(module, "main"):
            names.append(path.name)
    return names


def _skill_command_rows() -> list[str]:
    """The `<name>.py` cell of every row in SKILL.md's "## The commands" table."""
    text = SKILL_MD.read_text(encoding="utf-8")
    section = text.split("## The commands", 1)[1]
    section = section.split("\n## ", 1)[0]
    names = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        match = re.fullmatch(r"`([\w.]+\.py)`", cells[0])
        if match:
            names.append(match.group(1))
    return names


# ---------------------------------------------------------------------------
# (a) SKILL.md's command table matches scripts/*.py, both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _command_names())
def test_every_command_script_has_a_skill_md_row(name: str) -> None:
    """A command cannot be added without its row (TESTING.md, "consistency")."""
    assert name in _skill_command_rows()


@pytest.mark.parametrize("name", _skill_command_rows())
def test_every_skill_md_command_row_names_an_existing_script(name: str) -> None:
    assert (SCRIPTS / name).is_file(), f"SKILL.md names {name}, which does not exist"


# ---------------------------------------------------------------------------
# (b) every /backend-api/... constant in the code is in the transport map
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{[^}]*\}|<[^>]*>")


def _fold_placeholders(text: str) -> str:
    """{id}, <id>, <g-p-id>, ... all become one form, so the two sides compare."""
    return PLACEHOLDER.sub("<id>", text)


def _endpoint_path(raw: str) -> str:
    """The /backend-api/... path a constant names: no query string, one
    placeholder form. ``raw`` may be a bare path or a full URL."""
    index = raw.find("/backend-api/")
    path = raw[index:] if index != -1 else raw
    path = path.split("?", 1)[0]
    return _fold_placeholders(path)


def _endpoint_constants() -> list[tuple[str, str]]:
    """(label, normalised path) for every plain string assigned to a
    module-level name in scripts/*.py whose value names a /backend-api/ path.

    Parsed with ast rather than executed, so this never imports a module for
    the sake of this check -- every scripts/*.py file is covered, including
    the vendored ones no test here otherwise touches.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            raw = value.value
            if "/backend-api/" not in raw:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.append((f"{path.name}:{target.id}", _endpoint_path(raw)))
    return found


def test_every_backend_api_constant_is_documented() -> None:
    """A path a command actually calls must be traceable in the transport
    map, or rediscovering it after ChatGPT changes starts from nothing."""
    docs = _fold_placeholders(
        ENDPOINT_DISCOVERY_MD.read_text(encoding="utf-8")
        + "\n"
        + SKILL_MD.read_text(encoding="utf-8")
    )
    misses = [
        f"{label} ({path})" for label, path in _endpoint_constants() if path not in docs
    ]
    assert not misses, "undocumented backend-api constant(s): " + ", ".join(misses)


# ---------------------------------------------------------------------------
# (c) the live sandbox record names the one project live tests may touch
# ---------------------------------------------------------------------------


def test_live_sandbox_json_names_the_rp_test_sandbox_project() -> None:
    data = json.loads((TESTS_DIR / "live" / "sandbox.json").read_text(encoding="utf-8"))
    assert {"id", "name"} <= data.keys()
    assert data["name"] == "rp-test-sandbox"


# ---------------------------------------------------------------------------
# (d) TESTING.md names every make target the Makefile defines
# ---------------------------------------------------------------------------


def _make_targets() -> list[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    return re.findall(r"(?m)^([A-Za-z][\w-]*):", text)


def test_testing_md_names_every_make_target() -> None:
    targets = _make_targets()
    text = TESTING_MD.read_text(encoding="utf-8")
    missing = [t for t in targets if not re.search(rf"\bmake {re.escape(t)}\b", text)]
    assert not missing, f"Makefile target(s) not named in TESTING.md: {missing}"


# ---------------------------------------------------------------------------
# (e) nobody outside chatgpt_client.py touches a sender's private members
# ---------------------------------------------------------------------------

PRIVATE_SENDER_USE = re.compile(r"\bsender\._[A-Za-z]")


def _docstring_lines(source: str) -> set[int]:
    """Line numbers occupied by module, class and function docstrings, so a
    docstring that *names* a private member is never mistaken for code."""
    lines: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc = body[0]
                lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return lines


def private_sender_uses(source: str) -> list[tuple[int, str]]:
    """Every code line in ``source`` that reaches into ``sender._<name>``.
    Comments and docstrings do not count. Pure, so it is tested on strings
    below as well as run over the tree."""
    skip = _docstring_lines(source)
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if lineno in skip:
            continue
        if PRIVATE_SENDER_USE.search(line.split("#", 1)[0]):
            found.append((lineno, line.strip()))
    return found


def _sender_callers() -> list[Path]:
    files = [p for p in sorted(SCRIPTS.glob("*.py")) if p.name != "chatgpt_client.py"]
    return files + sorted((TESTS_DIR / "live").glob("*.py"))


@pytest.mark.parametrize(
    "path", _sender_callers(), ids=lambda p: str(p.relative_to(SKILL_DIR))
)
def test_no_code_outside_the_client_uses_a_senders_private_members(
    path: Path,
) -> None:
    """``BrowserSender`` runs every Playwright call on its owner thread and
    opens its window on about:blank; only its public methods know both.
    Three callers reached past them, and all three were broken or had
    silently stopped working (references/failure-atlas.md, 2026-09-20):
    preflight.py, measure_window.py, tests/live/test_browser_upload.py.
    A new need is a new public method on the sender, never a reach."""
    offenders = [
        f"{path.relative_to(SKILL_DIR)}:{lineno}: {text}"
        for lineno, text in private_sender_uses(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


def test_private_sender_use_finder_sees_code_but_not_docstrings_or_comments() -> None:
    source = (
        '"""Module docstring naming sender._owner on purpose."""\n'
        "def f(sender):\n"
        '    """Also sender._composer, in a docstring."""\n'
        "    x = sender.send('hi')  # not sender._send\n"
        "    return sender._composer()\n"
    )
    assert private_sender_uses(source) == [(5, "return sender._composer()")]
