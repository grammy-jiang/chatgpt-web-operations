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

# Scripts other agents are adding this same wave (see the task brief); only
# used to soften a "no row yet" failure into an expected xfail, and only for
# the specific names below -- never to hide any other kind of miss.
# project_settings.py and send_prompt.py: both confirmed still missing their
# row at the final test run on 2026-09-20 (see the FINAL REPORT).
_MID_WAVE_PENDING: set[str] = {"project_settings.py", "send_prompt.py"}


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


def _command_name_params() -> list[Any]:
    """One parametrize case per command, xfail(strict=False) only for a name
    in _MID_WAVE_PENDING that is, right now, still missing its row -- so a
    row added later turns the case into a plain pass, not a masked failure.
    """
    rows = set(_skill_command_rows())
    params = []
    for name in _command_names():
        if name in _MID_WAVE_PENDING and name not in rows:
            marks = pytest.mark.xfail(
                strict=False, reason="command added this wave; row pending"
            )
            params.append(pytest.param(name, marks=marks))
        else:
            params.append(name)
    return params


@pytest.mark.parametrize("name", _command_name_params())
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
