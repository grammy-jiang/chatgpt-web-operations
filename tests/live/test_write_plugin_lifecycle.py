"""Tier T2: manage_plugins.py's writes, round-tripped on the sandbox plugin
and nothing else (marker live_write, CHATGPT_LIVE=write).

An uploaded plugin cannot be deleted (references/endpoint-discovery.md,
"Seen on 2026-09-29: plugins"), so this test never creates one. It adds a
release to ``rp-test-plugin`` (its id in ``sandbox.json`` under ``plugin``),
installs it, switches its skill off and on, downloads it, and uninstalls
it, reading every step back through the command's own verification and
once more here. ``finally`` leaves the plugin installed or not, as it was
found. ``plugin_session`` verifies the plugin's name before this test runs,
and its guard refuses every plugin write outside that one id.
"""

from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import manage_plugins  # noqa: E402

pytestmark = pytest.mark.live_write

SKILL = "rp-test-skill"


class _Outer:
    """What ``open_session()`` returns: only ``.session`` is used."""

    def __init__(self, session: Any) -> None:
        self.session = session


def _archive(tmp_path: Path, version: str) -> Path:
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "rp-test-plugin",
        "version": version,
        "description": "Throwaway test plugin for the chatgpt-web-operations "
        "skill's plugin commands. Safe to uninstall.",
        "extensions": {
            "com.openai": {
                "interface": {
                    "displayName": "rp-test plugin",
                    "shortDescription": "Throwaway test plugin",
                    "developerName": "rp-test",
                    "category": "Developer Tools",
                    "capabilities": ["Read"],
                }
            }
        },
    }
    skill = (
        "---\n"
        f"name: {SKILL}\n"
        "description: Throwaway test skill inside the rp-test plugin. Use only "
        'when the user says the exact phrase "rp-test plugin probe".\n'
        "---\n\n"
        f"Reply with the single line: rp-test plugin version {version}.\n"
    )
    path = tmp_path / f"rp-test-plugin-{version}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("rp-test-plugin/plugin.json", json.dumps(manifest, indent=2))
        zf.writestr(f"rp-test-plugin/skills/{SKILL}/SKILL.md", skill)
    return path


def test_plugin_round_trip_on_the_sandbox_plugin(
    monkeypatch, plugin_session, sandbox_plugin_id, tmp_path
) -> None:
    monkeypatch.setattr(
        manage_plugins, "open_session", lambda *a, **k: _Outer(plugin_session)
    )
    pid = sandbox_plugin_id
    version = f"0.1.{int(time.time())}"
    was_installed = pid in manage_plugins.installed_plugins(plugin_session)
    try:
        archive = _archive(tmp_path, version)
        assert manage_plugins.main(["update", pid, str(archive), "--apply"]) == 0
        detail = manage_plugins.plugin_detail(plugin_session, pid)
        assert manage_plugins.release(detail)["version"] == version
        if not was_installed:
            assert manage_plugins.main(["install", pid, "--apply"]) == 0
        assert pid in manage_plugins.installed_plugins(plugin_session)
        off = ["skill", pid, SKILL, "--disable", "--apply"]
        assert manage_plugins.main(off) == 0
        on = ["skill", pid, SKILL, "--enable", "--apply"]
        assert manage_plugins.main(on) == 0
        back = tmp_path / "back.zip"
        assert manage_plugins.main(["download", pid, "--out", str(back)]) == 0
        assert manage_plugins.inspect_archive(back).version == version
        assert manage_plugins.main(["uninstall", pid, "--confirm"]) == 0
        assert pid not in manage_plugins.installed_plugins(plugin_session)
    finally:
        installed = pid in manage_plugins.installed_plugins(plugin_session)
        if was_installed and not installed:
            manage_plugins.install(plugin_session, pid)
        elif installed and not was_installed:
            manage_plugins.uninstall(plugin_session, pid)
