"""Contract tests: the parsers read the real payload shapes (TESTING.md, kind
"fixture (contract)").

Fixtures under ``tests/fixtures/`` are sanitized recordings of real
``backend-api`` payloads (see ``tests/fixtures/README.md``): ids, emails and
free-text fields are replaced before they ever land here. Every assertion in
this file is therefore about shape or documented vocabulary -- position
counts, key presence, value types, the placeholder pattern the sanitizer
writes -- never about what the account's own text happened to say.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import list_projects  # noqa: E402
import model_settings  # noqa: E402
import probe_account  # noqa: E402
import profile_context  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

REDACTED = re.compile(r"^<redacted (\d+) chars>$")


def _load_fixture(name: str) -> Any:
    """Read one fixture JSON file from disk, exactly once."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# Loaded once at import time; every test below only reads these.
USER_SYSTEM_MESSAGES = _load_fixture("user_system_messages")
MEMORIES_SUMMARY = _load_fixture("memories_summary")
MODELS = _load_fixture("models")
SETTINGS_USER = _load_fixture("settings_user")
GIZMO_SANDBOX = _load_fixture("gizmo_sandbox")
WHAM_USAGE = _load_fixture("wham_usage")
SYSTEM_HINTS_BASIC = _load_fixture("system_hints_basic")


def _redacted_chars(text: str) -> int:
    """The N in a "<redacted N chars>" placeholder; fails if it is not one."""
    match = REDACTED.match(text)
    assert match, f"not a redacted placeholder: {text!r}"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# user_system_messages.json -> profile_context.custom_instructions_of
# ---------------------------------------------------------------------------


def test_custom_instructions_of_reads_the_recorded_shape() -> None:
    """The four editable texts must survive as placeholders, not vanish."""
    ci = profile_context.custom_instructions_of(USER_SYSTEM_MESSAGES)
    assert isinstance(ci["enabled"], bool)
    assert REDACTED.match(ci["name"])
    assert REDACTED.match(ci["role"])
    assert _redacted_chars(ci["traits"]) > 0
    assert _redacted_chars(ci["about"]) > 0
    assert isinstance(ci["personality"], str) and ci["personality"]
    assert isinstance(ci["disabled_tools"], list)


# ---------------------------------------------------------------------------
# memories_summary.json -> profile_context.memory_of
# ---------------------------------------------------------------------------


def test_memory_of_leaves_entries_none_without_an_entries_payload() -> None:
    """An unread memory (no entries payload) must not look like an empty one."""
    mem = profile_context.memory_of(MEMORIES_SUMMARY, {})
    assert isinstance(mem["tokens_used"], int) and mem["tokens_used"] > 0
    assert isinstance(mem["tokens_max"], int) and mem["tokens_max"] > 0
    assert mem["entries"] is None


# ---------------------------------------------------------------------------
# models.json -> model_settings.presets_of / effort_levels_of
# ---------------------------------------------------------------------------


def test_presets_of_has_five_positions_in_slider_order() -> None:
    presets = model_settings.presets_of(MODELS)
    assert [p["title"] for p in presets] == [
        "Instant",
        "Medium",
        "High",
        "Extra High",
        "Pro",
    ]


def test_presets_2_to_4_share_one_thinking_model_with_the_three_efforts() -> None:
    """Medium/High/Extra High are one model at three efforts, not three models."""
    presets = model_settings.presets_of(MODELS)
    middle = presets[1:4]
    assert len({p["model"] for p in middle}) == 1
    assert [p["effort"] for p in middle] == ["standard", "extended", "max"]


def test_preset_5_changes_the_model_and_carries_no_effort() -> None:
    """Pro is a model switch, not a fifth effort level (SKILL.md)."""
    presets = model_settings.presets_of(MODELS)
    thinking_model = presets[1]["model"]
    assert presets[4]["model"] != thinking_model
    assert presets[4]["effort"] == ""


def test_effort_levels_of_lists_the_shared_thinking_model_with_four_levels() -> None:
    presets = model_settings.presets_of(MODELS)
    thinking_model = presets[1]["model"]
    levels = model_settings.effort_levels_of(MODELS)
    assert levels[thinking_model] == ["min", "standard", "extended", "max"]


def test_no_effort_level_anywhere_is_called_ultra() -> None:
    """ "Ultra" is a settings toggle, never a thinking_effort value (SKILL.md)."""
    levels = model_settings.effort_levels_of(MODELS)
    for values in levels.values():
        assert "ultra" not in values
    for preset in model_settings.presets_of(MODELS):
        assert preset["effort"] != "ultra"


# ---------------------------------------------------------------------------
# settings_user.json + models.json -> profile_context.model_of
# ---------------------------------------------------------------------------


def _web_cookie() -> dict[str, str]:
    """The cookie a browser on this profile would carry, from the server's
    own record of the last web send (settings_user.json)."""
    last = SETTINGS_USER["settings"]["last_used_model_config"]
    slug = last["slugs"]["web"]
    effort = last["juices"]["web"][slug]
    return {"model": slug, "effort": effort}


def test_model_of_agrees_between_server_and_cookie_for_the_recorded_web_slug() -> None:
    doc = profile_context.model_of(MODELS, SETTINGS_USER, _web_cookie())
    assert doc["server"]["surface"] == "web"
    assert doc["server"]["preset"] == doc["cookie"]["preset"]
    assert isinstance(doc["ultra_effort_enabled"], bool)


def test_model_of_reports_no_cookie_when_given_an_empty_dict() -> None:
    doc = profile_context.model_of(MODELS, SETTINGS_USER, {})
    assert doc["cookie"] is None


# ---------------------------------------------------------------------------
# gizmo_sandbox.json -> list_projects.project_of
# ---------------------------------------------------------------------------

SCALAR = (str, int, float, bool, type(None))


def test_project_of_reads_the_sandbox_project_shape() -> None:
    project = list_projects.project_of(GIZMO_SANDBOX)
    assert project["id"].startswith("g-p-")
    assert project["url"].endswith("/project")
    assert project["memory_scope"] in {"global", "project_v2"}
    assert isinstance(project["files"], list)
    assert all(
        isinstance(f, dict) and all(isinstance(v, SCALAR) for v in f.values())
        for f in project["files"]
    )
    assert project["instructions"] == "" or REDACTED.match(project["instructions"])


# ---------------------------------------------------------------------------
# wham_usage.json -> probe_account.usage_lines
# ---------------------------------------------------------------------------


def test_usage_lines_has_three_lines_plan_first_and_the_caveat_last() -> None:
    """The caveat that this window and these credits are not about sends."""
    lines = probe_account.usage_lines(WHAM_USAGE)
    assert len(lines) == 3
    assert lines[0].startswith("plan ")
    assert lines[-1] == "This window and these credits do not cover chat sends."


# ---------------------------------------------------------------------------
# system_hints_basic.json -- no parser yet (only documented in
# references/endpoint-discovery.md); the vocabulary itself is the contract.
# ---------------------------------------------------------------------------


def test_system_hints_basic_has_search_in_category_source() -> None:
    hints = {h["system_hint"]: h for h in SYSTEM_HINTS_BASIC["system_hints"]}
    assert hints["search"]["category"] == "source"


def test_system_hints_basic_has_no_hint_named_ultra() -> None:
    """ "Ultra" is a settings toggle, never a composer "+" item (SKILL.md)."""
    names = {h["system_hint"] for h in SYSTEM_HINTS_BASIC["system_hints"]}
    assert "ultra" not in names
