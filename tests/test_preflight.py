"""Tests for preflight.py (TESTING.md kind "unit", tier T0).

Every pure check function is tested over plain data. The thin fetch_*
wrappers and main() are tested over a fake session -- ``.session.call(path)``
returning a canned ``(status, body)`` -- and a fake ``cc`` (chatgpt_client)
namespace, never a real ChatGPTSession/BrowserSender and never a socket
(conftest.py's autouse guard fails any T0 test that tries).

``probe_send_gates.fetch`` does real ``urllib`` network I/O, so every test
that reaches ``account_checks`` or the happy path of ``main()`` uses the
``fake_psg`` fixture, which replaces preflight's own ``psg`` reference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import preflight  # noqa: E402
import probe_send_gates  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fakes and fixtures
# ---------------------------------------------------------------------------


class _Backend:
    """``session.session.call`` over canned responses, matched by prefix."""

    def __init__(self, responses: dict[str, tuple[int, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def call(self, path: str) -> tuple[int, Any]:
        self.calls.append(path)
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return 404, {"error": "no such fake path: " + path}


class _FakeSession:
    def __init__(self, backend: _Backend) -> None:
        self.session = backend


class _FnSession:
    """A session whose ``.session.call`` is one function: the simplest fake
    for edge cases that do not need path-based branching."""

    def __init__(self, responder: Any) -> None:
        self.session = SimpleNamespace(call=responder)


class _FakeSenderOK:
    def __init__(self, browser: str) -> None:
        self.browser = browser

    def __enter__(self) -> _FakeSenderOK:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def _composer(self) -> object:
        return object()


class _FakeSenderBlocked:
    def __init__(self, browser: str) -> None:
        self.browser = browser

    def __enter__(self) -> _FakeSenderBlocked:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def _composer(self) -> object:
        raise RuntimeError(
            "composer did not appear at https://chatgpt.com/ (logged out or challenged)"
        )


def _fake_cc(
    session: Any,
    *,
    available_mb: float = 8000.0,
    min_available_mb: float = 4000.0,
    pids: frozenset[int] = frozenset(),
    max_browsers: int = 1,
    sender_cls: type = _FakeSenderOK,
) -> SimpleNamespace:
    """A minimal stand-in for chatgpt_client: a healthy host by default, so
    a main() test only has to override what it cares about."""
    return SimpleNamespace(
        available_mb=lambda: available_mb,
        MIN_AVAILABLE_MB=min_available_mb,
        scripted_browser_pids=lambda: set(pids),
        MAX_BROWSERS=max_browsers,
        ChatGPTSession=lambda browser: session,
        BrowserSender=sender_cls,
    )


MODELS_PAYLOAD = {
    "versions": [
        {
            "id": "latest",
            "intelligence_presets": [
                {"id": 0, "title": "Instant", "model_slug": "gpt-5-6-instant"},
                {
                    "id": 6,
                    "title": "Extra High",
                    "model_slug": "gpt-5-6-thinking",
                    "thinking_effort": "max",
                },
            ],
        }
    ],
    "models": [{"slug": "gpt-5-6-instant", "configurable_thinking_effort": False}],
}

SETTINGS_PAYLOAD = {
    "settings": {
        "last_used_model_config": {
            "slugs": {"web": "gpt-5-6-thinking"},
            "juices": {"web": {"gpt-5-6-thinking": "max"}},
        }
    }
}

GIZMO_PAYLOAD = {
    "gizmo": {
        "id": "g-p-abc",
        "short_url": "g-p-abc-workers",
        "display": {"name": "workers"},
        "instructions": "Answer in JSON.",
        "memory_enabled": True,
        "memory_scope": "global",
        "context_stuffing_budget": 110000,
        "model": None,
        "default_model": None,
    },
    "files": [],
}


def _happy_backend(extra: dict[str, tuple[int, Any]] | None = None) -> _Backend:
    """Every endpoint account_checks/run_checks touch, all answering 200."""
    responses: dict[str, tuple[int, Any]] = {
        preflight.ME: (200, {}),
        preflight.CONVERSATIONS_PROBE: (200, {"items": []}),
        preflight.pa.USAGE: (200, {}),
        preflight.ms.MODELS: (200, MODELS_PAYLOAD),
        preflight.ms.SETTINGS: (200, SETTINGS_PAYLOAD),
        preflight.pc.USER_SYSTEM_MESSAGES: (200, {}),
        preflight.pc.MEMORY_SUMMARY: (200, {}),
        preflight.pc.MEMORY_ENTRIES: (200, {}),
    }
    if extra:
        responses.update(extra)
    return _Backend(responses)


REAL_WIRELESS_SAMPLE = (
    "Inter-| sta-|   Quality        |   Discarded packets               "
    "| Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc "
    "| beacon | 22\n"
    " wlan2: 0000   62.  -48.  -256        0      0      0      0    635"
    "        0\n"
    " wlan0: 0000   49.  -61.  -256        0      0      0    475      0"
    "        0\n"
    " wlan1: 0000   62.  -48.  -256        0      0      0      0      0"
    "        0\n"
)

DISCONNECTED_WIRELESS_SAMPLE = (
    "Inter-| sta-|   Quality        |   Discarded packets               "
    "| Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc "
    "| beacon | 22\n"
    " wlan0: 0000    0.    0.    0.       0      0      0      0        0"
    "        0\n"
)


@pytest.fixture
def fake_psg(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace preflight's own ``psg`` reference so account_checks and the
    happy path of main() never reach probe_send_gates' real network fetch."""
    ns = SimpleNamespace(
        fetch=lambda session: {
            "proofofwork": {"required": True},
            "turnstile": {"required": True},
            "so": {"required": True},
        },
        gates_from=probe_send_gates.gates_from,
    )
    monkeypatch.setattr(preflight, "psg", ns)
    return ns


@pytest.fixture
def clean_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiet, four-core, fully-tooled host for main() tests that are not
    about the host group itself."""
    monkeypatch.setattr(preflight.os, "getloadavg", lambda: (0.5,))
    monkeypatch.setattr(preflight.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: object())


@pytest.fixture
def linked_wireless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "wireless"
    p.write_text(REAL_WIRELESS_SAMPLE)
    monkeypatch.setattr(preflight, "WIRELESS_PATH", p)
    return p


@pytest.fixture
def dead_wireless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "wireless"
    p.write_text(DISCONNECTED_WIRELESS_SAMPLE)
    monkeypatch.setattr(preflight, "WIRELESS_PATH", p)
    return p


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------


def test_check_builds_the_five_field_record_with_fix_defaulting_to_none() -> None:
    c = preflight.check("host", "thing", "ok", "detail text")
    assert c == {
        "group": "host",
        "name": "thing",
        "state": "ok",
        "detail": "detail text",
        "fix": None,
    }


def test_check_carries_the_fix_when_given() -> None:
    c = preflight.check("host", "thing", "block", "bad", "do something")
    assert c["fix"] == "do something"


# ---------------------------------------------------------------------------
# GROUP host
# ---------------------------------------------------------------------------


def test_memory_check_blocks_below_the_minimum_and_names_both_numbers() -> None:
    c = preflight.memory_check(1500.0, 4000.0)
    assert c["group"] == "host"
    assert c["state"] == "block"
    assert "1500" in c["detail"]
    assert "4000" in c["detail"]
    assert c["fix"]


def test_memory_check_is_ok_at_or_above_the_minimum() -> None:
    c = preflight.memory_check(4000.0, 4000.0)
    assert c["state"] == "ok"
    assert "4000" in c["detail"]
    assert c["fix"] is None


def test_load_check_warns_above_075_per_core() -> None:
    c = preflight.load_check(3.5, 4)
    assert c["state"] == "warn"
    assert "per core" in c["detail"]
    assert "CPU" in c["fix"]


def test_load_check_is_ok_at_or_below_075_per_core() -> None:
    c = preflight.load_check(3.0, 4)  # exactly the 0.75 threshold
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_load_check_falls_back_to_one_core_when_cpu_count_is_none() -> None:
    c = preflight.load_check(1.0, None)
    assert "1 core(s)" in c["detail"]
    assert c["state"] == "warn"


def test_inflight_browsers_blocks_and_names_the_count() -> None:
    c = preflight.inflight_browsers_check({111, 222}, 1)
    assert c["state"] == "block"
    assert "2 " in c["detail"]
    assert c["fix"] == "wait for the send in flight, or kill it between gates"


def test_inflight_browsers_is_ok_when_none_are_running() -> None:
    c = preflight.inflight_browsers_check(set(), 1)
    assert c["state"] == "ok"


def test_tooling_check_ok_when_everything_present() -> None:
    c = preflight.tooling_check(True, True, True, True, True)
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_tooling_check_blocks_and_names_every_missing_item() -> None:
    c = preflight.tooling_check(False, True, True, False, True)
    assert c["state"] == "block"
    assert c["fix"] == "bash bootstrap.sh"
    assert "Xvfb" in c["detail"]
    assert "cryptography" in c["detail"]
    assert "Google Chrome" not in c["detail"]


def test_disk_check_warns_under_2gb() -> None:
    workdir = Path("/tmp/example-topic")
    c = preflight.disk_check(1 * 1024**3, workdir)
    assert c["state"] == "warn"
    assert str(workdir) in c["detail"]
    assert str(workdir) in c["fix"]


def test_disk_check_is_ok_at_or_above_2gb() -> None:
    workdir = Path("/tmp/example-topic")
    c = preflight.disk_check(3 * 1024**3, workdir)
    assert c["state"] == "ok"


def test_existing_ancestor_returns_the_path_itself_when_it_exists(
    tmp_path: Path,
) -> None:
    assert preflight._existing_ancestor(tmp_path) == tmp_path


def test_existing_ancestor_walks_up_to_a_real_parent(tmp_path: Path) -> None:
    missing = tmp_path / "not" / "yet" / "created"
    assert preflight._existing_ancestor(missing) == tmp_path


def test_existing_ancestor_falls_back_to_the_anchor_when_nothing_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight.Path, "exists", lambda self: False)
    target = tmp_path / "a" / "b"
    assert preflight._existing_ancestor(target) == Path(target.anchor)


def test_host_checks_returns_four_checks_without_a_workdir(
    clean_host: None,
) -> None:
    client = _fake_cc(None)
    checks = preflight.host_checks(client, None)
    assert [c["name"] for c in checks] == [
        "available memory",
        "load average",
        "in-flight browsers",
        "browser tooling",
    ]
    assert all(c["state"] == "ok" for c in checks)


def test_host_checks_adds_disk_space_last_with_a_workdir(
    clean_host: None, tmp_path: Path
) -> None:
    client = _fake_cc(None)
    checks = preflight.host_checks(client, tmp_path)
    assert [c["name"] for c in checks][-1] == "disk space"
    assert len(checks) == 5


def test_host_checks_reflects_a_low_memory_client(clean_host: None) -> None:
    client = _fake_cc(None, available_mb=100.0, min_available_mb=4000.0)
    checks = preflight.host_checks(client, None)
    assert checks[0]["name"] == "available memory"
    assert checks[0]["state"] == "block"


# ---------------------------------------------------------------------------
# GROUP link
# ---------------------------------------------------------------------------


def test_wireless_status_is_absent_when_the_file_does_not_exist() -> None:
    assert preflight.wireless_status(None) == {"present": False, "interfaces": {}}


def test_wireless_status_parses_every_real_interface_line() -> None:
    status = preflight.wireless_status(REAL_WIRELESS_SAMPLE)
    assert status["present"] is True
    assert set(status["interfaces"]) == {"wlan0", "wlan1", "wlan2"}
    assert status["interfaces"]["wlan1"]["linked"] is True
    assert status["interfaces"]["wlan1"]["level"] == -48.0
    assert status["interfaces"]["wlan1"]["link"] == 62.0


def test_wireless_status_reads_a_disconnected_interface_as_not_linked() -> None:
    status = preflight.wireless_status(DISCONNECTED_WIRELESS_SAMPLE)
    assert status["interfaces"]["wlan0"]["linked"] is False


def test_wireless_status_skips_header_and_unparseable_lines() -> None:
    text = "Inter-|header\n face |header2\ngarbage-no-colon\nwlan0: only-two\n"
    status = preflight.wireless_status(text)
    assert status["interfaces"] == {}


def test_wireless_status_treats_unparseable_numeric_fields_as_none() -> None:
    status = preflight.wireless_status("wlan3: 0000 abc def\n")
    info = status["interfaces"]["wlan3"]
    assert info["link"] is None
    assert info["level"] is None
    assert info["linked"] is False


def test_interface_line_reports_unknown_level_as_a_question_mark() -> None:
    line = preflight._interface_line(
        "wlan3", {"link": None, "level": None, "linked": False}
    )
    assert line == "wlan3 not linked (level ?)"


def test_wireless_check_is_ok_when_the_file_is_absent() -> None:
    c = preflight.wireless_check({"present": False, "interfaces": {}})
    assert c["group"] == "link"
    assert c["state"] == "ok"


def test_wireless_check_is_ok_when_at_least_one_interface_is_linked() -> None:
    c = preflight.wireless_check(preflight.wireless_status(REAL_WIRELESS_SAMPLE))
    assert c["state"] == "ok"
    assert "wlan1 linked" in c["detail"]


def test_wireless_check_blocks_when_the_file_exists_with_no_linked_interface() -> None:
    status = preflight.wireless_status(DISCONNECTED_WIRELESS_SAMPLE)
    c = preflight.wireless_check(status)
    assert c["state"] == "block"
    assert c["fix"]


def test_wireless_check_blocks_when_the_file_exists_but_lists_nothing() -> None:
    c = preflight.wireless_check({"present": True, "interfaces": {}})
    assert c["state"] == "block"
    assert c["detail"] == "no interface listed"


def test_read_wireless_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert preflight._read_wireless(tmp_path / "nope") is None


def test_read_wireless_returns_the_text_of_a_real_file(tmp_path: Path) -> None:
    p = tmp_path / "wireless"
    p.write_text(REAL_WIRELESS_SAMPLE)
    assert preflight._read_wireless(p) == REAL_WIRELESS_SAMPLE


def test_link_checks_returns_one_check_built_from_a_real_file(
    tmp_path: Path,
) -> None:
    p = tmp_path / "wireless"
    p.write_text(REAL_WIRELESS_SAMPLE)
    checks = preflight.link_checks(p)
    assert len(checks) == 1
    assert checks[0]["group"] == "link"
    assert checks[0]["state"] == "ok"


# ---------------------------------------------------------------------------
# GROUP account
# ---------------------------------------------------------------------------


def test_account_reads_block_on_a_429_on_me_with_the_rate_limit_fix() -> None:
    c = preflight.account_reads_check(429, 200, 3)
    assert c["state"] == "block"
    assert c["detail"] == "rate limited on the read path"
    assert c["fix"] == preflight.RATE_LIMIT_FIX


def test_account_reads_block_on_a_429_on_the_listing() -> None:
    c = preflight.account_reads_check(200, 429, 0)
    assert c["state"] == "block"
    assert c["detail"] == "rate limited on the read path"


def test_account_reads_block_on_a_non_429_failure_on_me() -> None:
    c = preflight.account_reads_check(403, 200, 0)
    assert c["state"] == "block"
    assert "403" in c["detail"]


def test_account_reads_block_on_a_non_429_failure_on_the_listing() -> None:
    c = preflight.account_reads_check(200, 500, 0)
    assert c["state"] == "block"
    assert "500" in c["detail"]


def test_account_reads_ok_when_both_reads_succeed() -> None:
    c = preflight.account_reads_check(200, 200, 3)
    assert c["state"] == "ok"
    assert "3" in c["detail"]


def test_fetch_account_reads_counts_the_listing_items() -> None:
    backend = _Backend(
        {
            preflight.ME: (200, {}),
            preflight.CONVERSATIONS_PROBE: (200, {"items": [{}, {}]}),
        }
    )
    c = preflight.fetch_account_reads(_FakeSession(backend))
    assert c["state"] == "ok"
    assert "2" in c["detail"]


def test_fetch_account_reads_tolerates_a_non_dict_listing_body() -> None:
    def responder(path: str) -> tuple[int, Any]:
        if path == preflight.ME:
            return 200, {}
        return 200, "not-a-dict"

    c = preflight.fetch_account_reads(_FnSession(responder))
    assert c["state"] == "ok"
    assert "0" in c["detail"]


def test_send_gates_ok_when_the_normal_trio_is_required() -> None:
    c = preflight.send_gates_check(dict(preflight.NORMAL_GATES))
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_send_gates_warns_when_a_new_gate_name_appears() -> None:
    """arkose is a known-but-not-yet-seen gate (BROWSER_ONLY in
    probe_send_gates.py); its appearance is the "ChatGPT changed something"
    signal, and it is still a warn, never a block."""
    gates = {**preflight.NORMAL_GATES, "arkose": True}
    c = preflight.send_gates_check(gates)
    assert c["state"] == "warn"


def test_send_gates_warns_when_one_of_the_three_is_no_longer_required() -> None:
    gates = {"proofofwork": True, "turnstile": True, "so": False}
    c = preflight.send_gates_check(gates)
    assert c["state"] == "warn"


def test_send_gates_never_blocks_on_a_read_failure_and_says_why() -> None:
    c = preflight.send_gates_check(None, error="timed out")
    assert c["state"] == "warn"
    assert "timed out" in c["detail"]


def test_send_gates_warn_on_fetch_failure_without_an_error_message() -> None:
    c = preflight.send_gates_check(None)
    assert c["state"] == "warn"
    assert c["detail"] == "could not read the sentinel endpoint"


def test_fetch_gates_reads_the_sentinel_through_probe_send_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "proofofwork": {"required": True},
        "turnstile": {"required": True},
        "so": {"required": True},
    }
    ns = SimpleNamespace(
        fetch=lambda session: body, gates_from=probe_send_gates.gates_from
    )
    monkeypatch.setattr(preflight, "psg", ns)
    c = preflight.fetch_gates(_FakeSession(_Backend({})))
    assert c["state"] == "ok"


def test_fetch_gates_turns_a_fetch_failure_into_a_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(session: Any) -> dict:
        raise RuntimeError("network is down")

    ns = SimpleNamespace(fetch=boom, gates_from=probe_send_gates.gates_from)
    monkeypatch.setattr(preflight, "psg", ns)
    c = preflight.fetch_gates(_FakeSession(_Backend({})))
    assert c["state"] == "warn"
    assert "network is down" in c["detail"]


def test_usage_check_is_ok_and_reports_unavailable_on_a_bad_status() -> None:
    c = preflight.usage_check(500, None)
    assert c["state"] == "ok"
    assert "unavailable" in c["detail"]


def test_usage_check_is_ok_and_joins_the_usage_lines_on_success() -> None:
    payload = {
        "plan_type": "plus",
        "rate_limit": {"primary_window": {"used_percent": 10, "reset_at": 0}},
        "credits": {"balance": 5},
        "rate_limit_reset_credits": {"available_count": 1},
    }
    c = preflight.usage_check(200, payload)
    assert c["state"] == "ok"
    assert "do not cover chat sends" in c["detail"]


def test_fetch_usage_reads_probe_accounts_endpoint() -> None:
    backend = _Backend({preflight.pa.USAGE: (200, {"plan_type": "free"})})
    c = preflight.fetch_usage(_FakeSession(backend))
    assert c["state"] == "ok"
    assert "free" in c["detail"]


def test_fetch_usage_tolerates_a_non_dict_response() -> None:
    c = preflight.fetch_usage(_FnSession(lambda path: (200, "boom")))
    assert "unavailable" in c["detail"]


def test_account_checks_returns_the_three_reads_in_order(
    fake_psg: SimpleNamespace,
) -> None:
    session = _FakeSession(_happy_backend())
    checks = preflight.account_checks(session)
    assert [c["name"] for c in checks] == [
        "auth and read",
        "send gates",
        "plan and credits",
    ]
    assert all(c["state"] == "ok" for c in checks)


# ---------------------------------------------------------------------------
# GROUP run
# ---------------------------------------------------------------------------


def test_model_effort_check_names_model_effort_and_the_matched_preset() -> None:
    preset = {
        "position": 2,
        "title": "Extra High",
        "model": "gpt-5-6-thinking",
        "effort": "max",
    }
    c = preflight.model_effort_check(
        {"model": "gpt-5-6-thinking", "effort": "max"}, preset, 2
    )
    assert c["state"] == "ok"
    assert "gpt-5-6-thinking" in c["detail"]
    assert "max" in c["detail"]
    assert "Extra High" in c["detail"]


def test_model_effort_check_reports_no_match_without_blocking() -> None:
    c = preflight.model_effort_check({"model": "", "effort": ""}, None, 5)
    assert c["state"] == "ok"
    assert "matches no preset" in c["detail"]


def test_fetch_model_effort_resolves_the_servers_own_record() -> None:
    backend = _Backend(
        {
            preflight.ms.MODELS: (200, MODELS_PAYLOAD),
            preflight.ms.SETTINGS: (200, SETTINGS_PAYLOAD),
        }
    )
    c = preflight.fetch_model_effort(_FakeSession(backend))
    assert c["state"] == "ok"
    assert "Extra High" in c["detail"]


def test_fetch_model_effort_tolerates_a_non_dict_response() -> None:
    c = preflight.fetch_model_effort(_FnSession(lambda path: (500, "boom")))
    assert c["state"] == "ok"
    assert "matches no preset" in c["detail"]


def test_instructions_memory_check_reports_enabled_lengths_and_counts() -> None:
    ci = {
        "enabled": True,
        "name": "Grammy",
        "role": "Engineer",
        "traits": "x",
        "about": "y",
    }
    mem = {"entries": 2, "tokens_used": 100, "tokens_max": 5000}
    c = preflight.instructions_memory_check(ci, mem)
    assert c["state"] == "ok"
    assert "enabled" in c["detail"]
    assert "2 entries" in c["detail"]


def test_instructions_memory_check_reports_unread_entries_as_a_question_mark() -> None:
    ci = {"enabled": False, "name": "", "role": "", "traits": "", "about": ""}
    mem = {"entries": None, "tokens_used": 0, "tokens_max": 0}
    c = preflight.instructions_memory_check(ci, mem)
    assert "? entries" in c["detail"]
    assert "disabled" in c["detail"]


def test_fetch_instructions_memory_reads_all_three_endpoints() -> None:
    backend = _Backend(
        {
            preflight.pc.USER_SYSTEM_MESSAGES: (200, {"enabled": True}),
            preflight.pc.MEMORY_SUMMARY: (
                200,
                {"memory_num_tokens": 1, "memory_max_tokens": 2},
            ),
            preflight.pc.MEMORY_ENTRIES: (200, {"memories": []}),
        }
    )
    c = preflight.fetch_instructions_memory(_FakeSession(backend))
    assert c["state"] == "ok"
    assert "0 entries" in c["detail"]


def test_fetch_instructions_memory_tolerates_a_non_dict_response() -> None:
    c = preflight.fetch_instructions_memory(_FnSession(lambda path: (500, "boom")))
    assert c["state"] == "ok"


def test_project_check_blocks_when_the_project_is_not_found() -> None:
    c = preflight.project_check("g-p-gone", 404, None)
    assert c["state"] == "block"
    assert "g-p-gone" in c["detail"]
    assert "404" in c["detail"]


def test_project_check_warns_when_memory_scope_is_global() -> None:
    c = preflight.project_check("g-p-abc", 200, GIZMO_PAYLOAD)
    assert c["state"] == "warn"
    assert "worker chats can read and write the account's memory" in c["detail"]
    assert c["fix"] == "project_settings.py g-p-abc --memory project-only --apply"


def test_project_check_ok_when_memory_scope_is_project_only() -> None:
    payload = json.loads(json.dumps(GIZMO_PAYLOAD))
    payload["gizmo"]["memory_scope"] = "project_v2"
    c = preflight.project_check("g-p-abc", 200, payload)
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_fetch_project_reads_the_gizmo_endpoint_for_the_given_id() -> None:
    backend = _Backend({preflight.lp.GIZMO.format(id="g-p-abc"): (200, GIZMO_PAYLOAD)})
    c = preflight.fetch_project(_FakeSession(backend), "g-p-abc")
    assert c["state"] == "warn"


def test_fetch_project_treats_a_200_non_dict_body_as_not_found() -> None:
    c = preflight.fetch_project(_FnSession(lambda path: (200, "nope")), "g-p-x")
    assert c["state"] == "block"


def test_load_conversations_is_empty_when_the_file_is_missing(
    tmp_path: Path,
) -> None:
    assert preflight.load_conversations(tmp_path) == []


def test_load_conversations_is_empty_on_malformed_json(tmp_path: Path) -> None:
    d = tmp_path / "chatgpt"
    d.mkdir()
    (d / "conversations.json").write_text("{not json")
    assert preflight.load_conversations(tmp_path) == []


def test_load_conversations_is_empty_when_the_top_level_is_not_a_list(
    tmp_path: Path,
) -> None:
    d = tmp_path / "chatgpt"
    d.mkdir()
    (d / "conversations.json").write_text(json.dumps({"oops": True}))
    assert preflight.load_conversations(tmp_path) == []


def test_load_conversations_reads_a_real_list(tmp_path: Path) -> None:
    d = tmp_path / "chatgpt"
    d.mkdir()
    entries = [{"status": "sent"}]
    (d / "conversations.json").write_text(json.dumps(entries))
    assert preflight.load_conversations(tmp_path) == entries


def test_uncollected_check_ok_with_no_sent_entries(tmp_path: Path) -> None:
    c = preflight.uncollected_check(tmp_path, [{"status": "done"}])
    assert c["state"] == "ok"


def test_uncollected_check_warns_and_names_the_count_and_fix(
    tmp_path: Path,
) -> None:
    conversations = [{"status": "sent"}, {"status": "sent"}, {"status": "done"}]
    c = preflight.uncollected_check(tmp_path, conversations)
    assert c["state"] == "warn"
    assert c["detail"] == "2 reply(ies) generated and never collected"
    assert c["fix"] == f"round_status.py {tmp_path}"


def test_profile_context_freshness_warns_when_none_recorded(
    tmp_path: Path,
) -> None:
    c = preflight.profile_context_freshness_check(tmp_path)
    assert c["state"] == "warn"
    assert "no chatgpt/profile_context" in c["detail"]
    assert "profile_context.py --json" in c["fix"]


def test_profile_context_freshness_warns_when_stale(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chatgpt"
    chat_dir.mkdir()
    (chat_dir / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-10T00:00:00+00:00"})
    )
    (chat_dir / "conversations.json").write_text(
        json.dumps([{"round": 2, "sent_at": "2026-09-15T00:00:00+00:00"}])
    )
    c = preflight.profile_context_freshness_check(tmp_path)
    assert c["state"] == "warn"
    assert "older than the newest round" in c["detail"]


def test_profile_context_freshness_ok_when_fresh(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chatgpt"
    chat_dir.mkdir()
    (chat_dir / "profile_context.json").write_text(
        json.dumps({"captured_at": "2026-09-20T00:00:00+00:00"})
    )
    (chat_dir / "conversations.json").write_text(
        json.dumps([{"round": 1, "sent_at": "2026-09-10T00:00:00+00:00"}])
    )
    c = preflight.profile_context_freshness_check(tmp_path)
    assert c["state"] == "ok"
    assert c["fix"] is None


def test_run_checks_always_includes_model_and_instructions() -> None:
    checks = preflight.run_checks(_FakeSession(_happy_backend()), "", None)
    assert [c["name"] for c in checks] == [
        "model and effort",
        "custom instructions and memory",
    ]


def test_run_checks_adds_the_project_check_when_given() -> None:
    backend = _happy_backend(
        {preflight.lp.GIZMO.format(id="g-p-abc"): (200, GIZMO_PAYLOAD)}
    )
    checks = preflight.run_checks(_FakeSession(backend), "g-p-abc", None)
    assert [c["name"] for c in checks] == [
        "model and effort",
        "custom instructions and memory",
        "project",
    ]


def test_run_checks_adds_workdir_checks_when_given(tmp_path: Path) -> None:
    checks = preflight.run_checks(_FakeSession(_happy_backend()), "", tmp_path)
    assert [c["name"] for c in checks] == [
        "model and effort",
        "custom instructions and memory",
        "uncollected replies",
        "profile context",
    ]


# ---------------------------------------------------------------------------
# GROUP browser (opt-in)
# ---------------------------------------------------------------------------


def test_browser_composer_check_ok_when_the_composer_appears() -> None:
    cc = SimpleNamespace(BrowserSender=_FakeSenderOK)
    c = preflight.browser_composer_check(cc, "chrome")
    assert c["group"] == "browser"
    assert c["state"] == "ok"


def test_browser_composer_check_blocks_on_logged_out_or_challenged() -> None:
    cc = SimpleNamespace(BrowserSender=_FakeSenderBlocked)
    c = preflight.browser_composer_check(cc, "chrome")
    assert c["state"] == "block"
    assert "logged out or challenged" in c["detail"]
    assert c["fix"]


# ---------------------------------------------------------------------------
# open_chatgpt_session
# ---------------------------------------------------------------------------


def test_open_chatgpt_session_returns_the_session_and_no_error() -> None:
    cc = SimpleNamespace(ChatGPTSession=lambda browser: "the-session")
    session, error = preflight.open_chatgpt_session(cc, "chrome")
    assert session == "the-session"
    assert error is None


def test_open_chatgpt_session_never_raises_and_names_the_reason() -> None:
    def boom(browser: str) -> None:
        raise RuntimeError("no cookie database")

    cc = SimpleNamespace(ChatGPTSession=boom)
    session, error = preflight.open_chatgpt_session(cc, "chrome")
    assert session is None
    assert error is not None
    assert "no cookie database" in error


# ---------------------------------------------------------------------------
# verdict_of / render
# ---------------------------------------------------------------------------


def test_verdict_go_when_nothing_is_warn_or_block() -> None:
    checks = [preflight.check("host", "x", "ok", "fine")]
    assert preflight.verdict_of(checks) == ("GO", 0)


def test_verdict_go_with_warnings_counts_only_warns() -> None:
    checks = [
        preflight.check("host", "x", "ok", "fine"),
        preflight.check("host", "y", "warn", "meh"),
        preflight.check("host", "z", "warn", "meh2"),
    ]
    assert preflight.verdict_of(checks) == ("GO WITH WARNINGS (2)", 2)


def test_verdict_do_not_start_wins_over_warnings() -> None:
    checks = [
        preflight.check("host", "x", "warn", "meh"),
        preflight.check("host", "y", "block", "nope"),
    ]
    assert preflight.verdict_of(checks) == ("DO NOT START (1 blocking)", 1)


def test_render_groups_checks_and_prints_fix_lines_only_when_present() -> None:
    checks = [
        preflight.check("host", "a", "ok", "a is fine"),
        preflight.check("link", "b", "block", "b is bad", "fix b"),
    ]
    text = preflight.render(checks)
    assert "== host ==" in text
    assert "== link ==" in text
    assert "fix: fix b" in text
    assert "fix:" not in text.split("== link ==")[0]


def test_render_omits_groups_with_no_checks() -> None:
    text = preflight.render([preflight.check("host", "a", "ok", "fine")])
    assert "== link ==" not in text
    assert "== account ==" not in text
    assert "== run ==" not in text
    assert "== browser ==" not in text


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_help_exits_0_and_prints_usage(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        preflight.main(["--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_main_go_on_a_fully_clean_run(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend())
    monkeypatch.setattr(preflight, "cc", _fake_cc(session))
    code = preflight.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip().splitlines()[-1] == "GO"


def test_main_go_with_warnings_when_something_warns(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.os, "getloadavg", lambda: (3.5,))  # 0.875/core
    session = _FakeSession(_happy_backend())
    monkeypatch.setattr(preflight, "cc", _fake_cc(session))
    code = preflight.main([])
    out = capsys.readouterr().out
    assert code == 2
    assert out.strip().splitlines()[-1].startswith("GO WITH WARNINGS")


def test_main_do_not_start_when_memory_is_low(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend())
    monkeypatch.setattr(preflight, "cc", _fake_cc(session, available_mb=100.0))
    code = preflight.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert out.strip().splitlines()[-1].startswith("DO NOT START")


def test_main_skips_account_and_run_when_the_link_is_dead(
    clean_host: None,
    dead_wireless: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The house rule: a dead link must never be reported as a blocked
    account, so the account group must never even be attempted."""
    calls: list[str] = []

    def spy_open(cc_mod: Any, browser: str) -> tuple[Any, str | None]:
        calls.append(browser)
        return _FakeSession(_happy_backend()), None

    monkeypatch.setattr(preflight, "open_chatgpt_session", spy_open)
    monkeypatch.setattr(preflight, "cc", _fake_cc(None))
    code = preflight.main([])
    out = capsys.readouterr().out
    assert calls == [], "open_chatgpt_session must never be called when link is dead"
    assert code == 1
    assert "DO NOT START" in out
    assert "skipped" in out
    assert "== account ==" in out  # the skip note itself, not the real checks
    assert "auth and read" not in out


def test_main_blocks_on_auth_and_read_when_the_session_cannot_open(
    clean_host: None,
    linked_wireless: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_open(cc_mod: Any, browser: str) -> tuple[Any, str | None]:
        return None, "no cookie database"

    monkeypatch.setattr(preflight, "open_chatgpt_session", failing_open)
    monkeypatch.setattr(preflight, "cc", _fake_cc(None))
    code = preflight.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "could not authenticate: no cookie database" in out


def test_main_includes_the_project_check_with_project_flag(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _happy_backend(
        {preflight.lp.GIZMO.format(id="g-p-abc"): (200, GIZMO_PAYLOAD)}
    )
    session = _FakeSession(backend)
    monkeypatch.setattr(preflight, "cc", _fake_cc(session))
    code = preflight.main(["--project", "g-p-abc"])
    out = capsys.readouterr().out
    assert "project" in out
    assert code == 2  # global memory scope -> warn


def test_main_includes_workdir_checks(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    capsys: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_dir = tmp_path / "chatgpt"
    chat_dir.mkdir()
    (chat_dir / "conversations.json").write_text(json.dumps([{"status": "sent"}]))
    session = _FakeSession(_happy_backend())
    monkeypatch.setattr(preflight, "cc", _fake_cc(session))
    code = preflight.main(["--workdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "uncollected replies" in out
    assert "profile context" in out
    assert code == 2  # 1 uncollected reply + no profile context recorded


def test_main_runs_the_browser_check_last_and_writes_json(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _happy_backend(
        {preflight.lp.GIZMO.format(id="g-p-abc"): (200, GIZMO_PAYLOAD)}
    )
    session = _FakeSession(backend)
    monkeypatch.setattr(preflight, "cc", _fake_cc(session, sender_cls=_FakeSenderOK))
    out_path = tmp_path / "out" / "preflight.json"
    code = preflight.main(
        [
            "--project",
            "g-p-abc",
            "--workdir",
            str(tmp_path),
            "--browser",
            "--json",
            str(out_path),
        ]
    )
    doc = json.loads(out_path.read_text())
    assert doc["exit_code"] == code
    assert doc["verdict"]
    assert "checked_at" in doc
    assert doc["checks"][-1]["group"] == "browser"
    assert doc["checks"][-1]["state"] == "ok"


def test_main_records_a_blocked_browser_check_in_json(
    clean_host: None,
    linked_wireless: Path,
    fake_psg: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_happy_backend())
    monkeypatch.setattr(
        preflight, "cc", _fake_cc(session, sender_cls=_FakeSenderBlocked)
    )
    out_path = tmp_path / "preflight.json"
    code = preflight.main(["--browser", "--json", str(out_path)])
    doc = json.loads(out_path.read_text())
    assert code == 1
    assert doc["checks"][-1]["group"] == "browser"
    assert doc["checks"][-1]["state"] == "block"
