"""Tier T1: each read command's ``main()`` against the real account, through
the guarded session (TESTING.md section 6, P4; marker live_read,
CHATGPT_LIVE=read).

Until 2026-09-27 the smoke tier checked endpoints, and a command's own
``main()`` met the account only when someone ran it by hand. These tests
run the entry points themselves, the way ``python3 scripts/<command>``
does, and assert the exit codes SKILL.md documents; shape and codes only,
never the account's content (TESTING.md section 2).

Every command opens its session through ``_common.open_session`` (or
``chatgpt_client.ChatGPTSession`` for probe_account), so each one is
pointed at ``_Adapter``: the guarded ``live_session`` under the attribute
names a command reads -- ``.session.call``, ``list_conversations``,
``get_conversation`` -- so no test opens a second, unguarded session.
Nothing here writes: the guard refuses anything but GET and the read-only
POSTs it names, and every ``main`` below is a dry run or a read.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

from live.minting import GuardedConversationReader

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import clean_chats  # noqa: E402
import delete_skill  # noqa: E402
import list_automations  # noqa: E402
import list_chats  # noqa: E402
import list_connectors  # noqa: E402
import list_projects  # noqa: E402
import list_skills  # noqa: E402
import manage_plugins  # noqa: E402
import model_settings  # noqa: E402
import probe_cookies  # noqa: E402
import probe_send_gates  # noqa: E402
import profile_context  # noqa: E402
import search_chats  # noqa: E402

pytestmark = pytest.mark.live_read

NO_SUCH_PROJECT = "g-p-" + "0" * 32


class _Adapter(GuardedConversationReader):
    """What a command's ``open_session()`` returns, over the guarded session."""

    def __init__(self, guarded: Any, cc: Any) -> None:
        super().__init__(guarded, cc)
        self.session = guarded


@pytest.fixture
def adapter(live_session: Any) -> _Adapter:
    import chatgpt_client as cc

    return _Adapter(live_session, cc)


def _point(monkeypatch: pytest.MonkeyPatch, module: Any, adapter: _Adapter) -> None:
    monkeypatch.setattr(module, "open_session", lambda *a, **k: adapter)


def test_list_chats_lists_and_filters(monkeypatch, adapter) -> None:
    _point(monkeypatch, list_chats, adapter)
    assert list_chats.main([]) == 0
    assert list_chats.main(["--match", f"rp-none-{secrets.token_hex(3)}"]) == 0
    assert list_chats.main(["--pinned", "--limit", "5"]) == 0
    assert list_chats.main(["--archived", "--limit", "5"]) == 0


def test_list_projects_finds_the_sandbox_and_reports_a_missing_id(
    monkeypatch, adapter, sandbox_id
) -> None:
    _point(monkeypatch, list_projects, adapter)
    assert list_projects.main(["--match", "rp-test-sandbox"]) == 0
    assert list_projects.main(["--id", sandbox_id, "--files", "--chats"]) == 0
    assert list_projects.main(["--id", NO_SUCH_PROJECT]) == 1


def test_list_connectors_reads_links_apps_and_tunnels(monkeypatch, adapter) -> None:
    """The lookup POSTs (links/list_accessible, connectors/batch) only read;
    the guard names them in READ_POSTS for exactly this."""
    _point(monkeypatch, list_connectors, adapter)
    assert list_connectors.main(["--json"]) == 0
    assert list_connectors.main(["--tunnels"]) == 0
    assert list_connectors.main(["--match", f"rp-none-{secrets.token_hex(3)}"]) == 0


def test_probe_account_answers(monkeypatch, adapter) -> None:
    import chatgpt_client
    import probe_account

    monkeypatch.setattr(chatgpt_client, "ChatGPTSession", lambda *a, **k: adapter)
    assert probe_account.main([]) == 0


def test_model_settings_resolves_the_account_s_preset(monkeypatch, adapter) -> None:
    _point(monkeypatch, model_settings, adapter)
    assert model_settings.main([]) == 0


def test_profile_context_reads_the_sandbox_context(
    monkeypatch, adapter, sandbox_id, tmp_path
) -> None:
    _point(monkeypatch, profile_context, adapter)
    out = tmp_path / "profile.json"
    assert profile_context.main(["--project", sandbox_id, "--json", str(out)]) == 0
    assert out.is_file()


def test_clean_chats_dry_runs_over_the_sandbox(monkeypatch, adapter, sandbox_id):
    """Without --apply nothing is written; the guard would refuse a PATCH in
    this tier anyway, which is the second net under the first. An action
    flag is required (exit 2 without one), and a dry run exits 0."""
    _point(monkeypatch, clean_chats, adapter)
    assert clean_chats.main(["--project", sandbox_id]) == 2
    assert clean_chats.main(["--project", sandbox_id, "--delete"]) == 0
    nonsense = f"rp-none-{secrets.token_hex(3)}"
    assert clean_chats.main(["--match", nonsense, "--archive"]) == 0


def test_probe_send_gates_reads_the_sentinel(monkeypatch, adapter, capsys) -> None:
    """Exit 0 means no browser is needed; on this account the three gates are
    required, so 1 is the documented answer. Either way the gates are named."""
    _point(monkeypatch, probe_send_gates, adapter)
    rc = probe_send_gates.main([])
    out = capsys.readouterr().out.lower()
    assert rc in (0, 1), out
    assert "proofofwork" in out or "turnstile" in out, out


def test_delete_skill_refuses_an_unknown_name_and_dry_runs_a_known_one(
    monkeypatch, adapter, live_session
) -> None:
    """Never a DELETE: an unknown name exits 2 after the one installed-list
    read; a known name without --confirm is a dry run (exit 0). The guard
    refuses DELETE in this tier, so a defect here would surface as exit 1,
    not as a deleted skill."""
    _point(monkeypatch, delete_skill, adapter)
    unknown = f"rp-no-such-skill-{secrets.token_hex(4)}"
    assert delete_skill.main([unknown, "--confirm"]) == 2
    status, body = live_session.call(list_skills.HAZELNUTS)
    assert status == 200
    names = [
        item.get("name")
        for item in (body or {}).get("hazelnuts", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    if names:
        assert delete_skill.main([names[0]]) == 0


def test_probe_cookies_reads_this_machine_s_jar() -> None:
    """Local only: Chrome's cookie database and the keyring, read; no value
    is printed (the command's own rule)."""
    assert probe_cookies.main([]) == 0


def test_search_chats_main_exits_as_documented(monkeypatch, adapter) -> None:
    _point(monkeypatch, search_chats, adapter)
    assert search_chats.main(["rp-test", "--limit", "5"]) in (0, 1)
    assert search_chats.main([f"rp-none-{secrets.token_hex(6)}"]) == 1


def test_list_automations_reads_every_filter(monkeypatch, adapter) -> None:
    _point(monkeypatch, list_automations, adapter)
    assert list_automations.main(["--filter", "all"]) == 0


def test_refresh_connector_lists_resolves_and_dry_runs_a_real_connector(
    monkeypatch, adapter
) -> None:
    """The listing is a read POST (READ_POSTS); the refresh itself is not,
    and the guard would refuse it: a dry run on a real name must never try."""
    import refresh_connector

    _point(monkeypatch, refresh_connector, adapter)
    assert refresh_connector.main(["--list"]) == 0
    assert refresh_connector.main([f"rp-no-such-connector-{secrets.token_hex(4)}"]) == 2
    names = [link["name"] for link in list_connectors.links(adapter)]
    if names:
        assert refresh_connector.main([names[0], "--dry-run"]) == 0


def test_project_settings_shows_the_sandbox_instructions_by_name(
    monkeypatch, adapter, capsys
) -> None:
    """--name walks the sidebar, --show prints the text and changes nothing
    (the guard would refuse the PATCH in this tier anyway)."""
    import project_settings

    _point(monkeypatch, project_settings, adapter)
    assert project_settings.main(["--name", "rp-test-sandbox", "--show"]) == 0
    assert capsys.readouterr().out.endswith("\n")
    assert (
        project_settings.main(["--name", f"rp-none-{secrets.token_hex(4)}", "--show"])
        == 2
    )


def test_clean_chats_by_id_reports_a_missing_chat_as_already_gone(
    monkeypatch, adapter, capsys
) -> None:
    """A dry run by exact id reads the chat raw; an id that names nothing is
    'already gone', never an error and never a write."""
    _point(monkeypatch, clean_chats, adapter)
    missing = "00000000-0000-4000-8000-" + secrets.token_hex(6)
    assert clean_chats.main(["--id", missing, "--delete"]) == 0
    assert "(already gone)" in capsys.readouterr().out


def test_manage_plugins_lists_shows_downloads_and_previews(
    monkeypatch, adapter, sandbox_plugin_id, tmp_path
) -> None:
    """Reads and previews only, on the sandbox plugin: list, show and
    download read; install, uninstall, skill and update without --apply or
    --confirm write nothing, and upload of an existing name is refused
    before any write. The read tier's guard refuses every plugin write, so
    a defect here surfaces as exit 1, never as a changed plugin."""
    _point(monkeypatch, manage_plugins, adapter)
    pid = sandbox_plugin_id
    assert manage_plugins.main(["list"]) == 0
    assert manage_plugins.main(["show", pid]) == 0
    archive = tmp_path / "sandbox.zip"
    assert manage_plugins.main(["download", pid, "--out", str(archive)]) == 0
    info = manage_plugins.inspect_archive(archive)
    assert info.name == "rp-test-plugin" and info.skills
    assert manage_plugins.main(["install", pid]) == 0
    assert manage_plugins.main(["uninstall", pid]) == 0
    assert manage_plugins.main(["skill", pid, info.skills[0], "--disable"]) == 0
    assert manage_plugins.main(["update", pid, str(archive)]) == 0
    assert manage_plugins.main(["upload", str(archive), "--dry-run"]) == 2
