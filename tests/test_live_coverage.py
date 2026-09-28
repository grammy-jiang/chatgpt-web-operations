"""Consistency (TESTING.md section 6, P4): every command has a live test, or
a named reason why it cannot have one.

The lesson behind this table is preflight's (references/failure-atlas.md,
2026-09-20): 31 offline-tested functions had never met the real account,
and the first real run failed on two defects the fakes could not show. A
command that is not in a live tier is not proven against ChatGPT, however
green its unit tests are, so the gap has to be visible and deliberate.

``LIVE_COVERAGE`` maps every command in ``scripts/`` to the live test that
exercises it (``<file under tests/>::<function>``) or to one of the
``REASONS``. The tests below fail when a command is missing from the table,
when a row names a command or a test function that does not exist, or when
a reason is not one of the named ones. Adding a command means adding a row.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS = TESTS_DIR.parent / "scripts"

# Bundled library modules, never commands (tests/test_consistency.py).
LIBRARY_MODULES = {
    "_common.py",
    "api_shapes.py",
    "round_state.py",
    "chatgpt_client.py",
    "chatgpt_session.py",
    "chatgpt_cookies.py",
}

REASONS = {
    "manual-acceptance": (
        "mutates connectors or runs Deep research: covered by the acceptance "
        "procedure in TESTING.md section 4, never by the permanent live tiers"
    ),
    "platform-credentials": (
        "uses the OpenAI Platform credential, not the ChatGPT session; the "
        "ChatGPT live tiers never hold it"
    ),
    "browser-diagnostic": (
        "a measuring command that opens its own diagnostic window; run by "
        "hand when no send is in flight"
    ),
    "offline-only": "reads local files only",
    "research-workdir": (
        "needs a research run's working directory and ledger; exercised by "
        "the research-pipeline orchestrator's own tests"
    ),
}

ROUNDTRIP = (
    "live/test_send_cli_roundtrip.py::"
    "test_send_prompt_main_posts_gets_a_reply_and_is_cleaned_up"
)
PROJECT_LIFECYCLE = (
    "live/test_write_project_lifecycle.py::"
    "test_creating_and_deleting_a_throwaway_project_round_trips"
)

LIVE_COVERAGE: dict[str, str] = {
    "clean_chats.py": (
        "live/test_read_commands.py::test_clean_chats_dry_runs_over_the_sandbox"
    ),
    "connect_connector.py": "manual-acceptance",
    "create_connector.py": "manual-acceptance",
    "create_project.py": PROJECT_LIFECYCLE,
    "deep_research.py": "manual-acceptance",
    "delete_connector.py": "manual-acceptance",
    "delete_project.py": PROJECT_LIFECYCLE,
    "delete_skill.py": (
        "live/test_read_commands.py::"
        "test_delete_skill_refuses_an_unknown_name_and_dry_runs_a_known_one"
    ),
    "discover_endpoints.py": "browser-diagnostic",
    "health.py": "live/test_read_health.py::test_the_whole_command_exits_as_documented",
    "list_automations.py": (
        "live/test_read_commands.py::test_list_automations_reads_every_filter"
    ),
    "list_chats.py": "live/test_read_commands.py::test_list_chats_lists_and_filters",
    "list_connectors.py": (
        "live/test_read_commands.py::test_list_connectors_reads_links_apps_and_tunnels"
    ),
    "list_projects.py": (
        "live/test_read_commands.py::"
        "test_list_projects_finds_the_sandbox_and_reports_a_missing_id"
    ),
    "list_skills.py": (
        "live/test_read_skills.py::test_list_skills_main_exits_0_through_the_guarded_session"
    ),
    "manage_tunnels.py": "platform-credentials",
    "measure_window.py": "browser-diagnostic",
    "model_settings.py": (
        "live/test_read_commands.py::test_model_settings_resolves_the_account_s_preset"
    ),
    "pin_chat.py": (
        "live/test_send_chat_flags.py::"
        "test_pin_unpin_archive_unarchive_round_trip_on_the_sandbox_chat"
    ),
    "preflight.py": (
        "live/test_read_preflight.py::test_the_whole_command_exits_as_documented"
    ),
    "probe_account.py": "live/test_read_commands.py::test_probe_account_answers",
    "probe_cookies.py": (
        "live/test_read_commands.py::test_probe_cookies_reads_this_machine_s_jar"
    ),
    "probe_send_gates.py": (
        "live/test_read_commands.py::test_probe_send_gates_reads_the_sentinel"
    ),
    "profile_context.py": (
        "live/test_read_commands.py::test_profile_context_reads_the_sandbox_context"
    ),
    "project_settings.py": (
        "live/test_write_project_settings.py::"
        "test_setting_and_restoring_the_sandbox_instructions_round_trips"
    ),
    "read_chat.py": ROUNDTRIP,
    "review_topic.py": "offline-only",
    "round_status.py": "research-workdir",
    "search_chats.py": (
        "live/test_read_commands.py::test_search_chats_main_exits_as_documented"
    ),
    "send_prompt.py": ROUNDTRIP,
}


def command_names() -> list[str]:
    return sorted(p.name for p in SCRIPTS.glob("*.py") if p.name not in LIBRARY_MODULES)


def functions_in(path: Path) -> set[str]:
    """Every ``test_*`` function defined at module level in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def test_every_command_has_a_row() -> None:
    missing = [name for name in command_names() if name not in LIVE_COVERAGE]
    assert missing == [], f"commands without a LIVE_COVERAGE row: {missing}"


def test_every_row_names_an_existing_command() -> None:
    stale = [name for name in LIVE_COVERAGE if name not in command_names()]
    assert stale == [], f"LIVE_COVERAGE rows for commands that no longer exist: {stale}"


@pytest.mark.parametrize(("command", "entry"), sorted(LIVE_COVERAGE.items()))
def test_every_row_resolves_to_a_live_test_or_a_named_reason(
    command: str, entry: str
) -> None:
    if "::" not in entry:
        assert entry in REASONS, f"{command}: {entry!r} is not a named reason"
        return
    file_part, function = entry.split("::", 1)
    path = TESTS_DIR / file_part
    assert path.is_file(), f"{command}: {file_part} does not exist under tests/"
    assert path.parent.name == "live", f"{command}: {file_part} is not a live test"
    assert function in functions_in(path), (
        f"{command}: {file_part} defines no {function}"
    )


def test_the_reasons_are_the_exception_not_the_rule() -> None:
    """More than a third of the commands excused would mean the table has
    become a list of excuses; today 9 of 30 have a reason."""
    excused = [c for c, e in LIVE_COVERAGE.items() if "::" not in e]
    assert len(excused) * 3 <= len(LIVE_COVERAGE), excused
