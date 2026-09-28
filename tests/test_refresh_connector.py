"""T0 tests for scripts/refresh_connector.py (PLAN-2026-09-27 A1): name
resolution, the 424 retry ladder, the dry run, listing and the command line
binnacle's chatgpt-refresh had."""

from __future__ import annotations

import json
from typing import Any

import pytest
import refresh_connector as rc

LIST = "/backend-api/aip/connectors/links/list_accessible"
LINKS = [
    {
        "id": "link_a",
        "name": "Raspberry Pi MCP",
        "connector_id": "asdk_app_1",
        "auth_type": "NONE",
        "actions": ["read_file", "run_command"],
    },
    {"id": "link_b", "name": "rp-skilltest", "actions": []},
    {"id": "link_c", "name": "Raspberry Pi MCP staging", "actions": ["x"]},
]


class _Inner:
    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, Any]] = []
        self.user_id = "user-XXXXXXXX"

    def call(self, path: str, method: str = "GET", payload: Any = None, **kw: Any):
        self.calls.append((method, path, payload))
        if path == LIST:
            return 200, {"links": LINKS}
        return self.replies.pop(0)


class _Session:
    def __init__(self, inner: _Inner) -> None:
        self.session = inner


@pytest.fixture
def wire(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(rc, "SLEEP", sleeps.append)

    def _wire(*replies: tuple[int, Any]) -> _Inner:
        inner = _Inner(list(replies))
        monkeypatch.setattr(rc, "open_session", lambda *a, **k: _Session(inner))
        inner.sleeps = sleeps  # type: ignore[attr-defined]
        return inner

    return _wire


def _links() -> list[dict[str, Any]]:
    return [{"id": r["id"], "name": r["name"]} for r in LINKS]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("raspberry pi mcp", "link_a"),  # exact, case-insensitive, beats substrings
        ("SKILLTEST", "link_b"),
        ("staging", "link_c"),
    ],
)
def test_resolve_prefers_an_exact_name_then_a_unique_substring(query, expected):
    link, reason = rc.resolve(query, _links())
    assert reason == "" and link["id"] == expected


def test_resolve_names_what_it_knows_when_nothing_or_too_much_matches() -> None:
    _, reason = rc.resolve("nothing", _links())
    assert "no connector matches 'nothing'" in reason and "'rp-skilltest'" in reason
    _, reason = rc.resolve("raspberry", _links())
    assert "ambiguous" in reason and "'Raspberry Pi MCP staging'" in reason
    _, reason = rc.resolve("x", [])
    assert "known: none" in reason


def test_refresh_by_name_prints_the_new_tool_list(wire, capsys) -> None:
    inner = wire((200, {"actions": [{"name": "read_file"}, {"name": "job_status"}, 7]}))
    assert rc.main(["raspberry pi mcp"]) == 0
    assert inner.calls[-1] == ("POST", rc.REFRESH, {"link_id": "link_a"})
    assert "Refreshed Raspberry Pi MCP. Tools now: read_file, job_status" in (
        capsys.readouterr().out
    )


def test_refresh_retries_424_then_succeeds(wire, capsys) -> None:
    inner = wire((424, {"detail": "tunnel"}), (424, {}), (200, {"actions": []}))
    assert rc.main(["skilltest"]) == 0
    refreshes = [c for c in inner.calls if c[1] == rc.REFRESH]
    assert len(refreshes) == 3
    assert inner.sleeps == [rc.RETRY_424_DELAY_S, rc.RETRY_424_DELAY_S]
    assert "attempt 2/5" in capsys.readouterr().err


def test_refresh_gives_up_after_the_ladder_or_at_once_without_retry(wire, capsys):
    inner = wire(*[(424, {"detail": "tunnel down"})] * 5)
    assert rc.main(["skilltest"]) == 1
    assert sum(c[1] == rc.REFRESH for c in inner.calls) == 5
    assert "refresh failed: HTTP 424: tunnel down" in capsys.readouterr().out
    inner = wire((424, {}))
    assert rc.main(["skilltest", "--no-retry"]) == 1
    assert sum(c[1] == rc.REFRESH for c in inner.calls) == 1


def test_any_other_failure_is_not_retried(wire, capsys) -> None:
    inner = wire((500, "boom"))
    assert rc.main(["--link-id", "link_z"]) == 1
    assert [c[1] for c in inner.calls] == [rc.REFRESH]  # --link-id: no listing
    assert "HTTP 500: boom" in capsys.readouterr().out


def test_an_unknown_or_ambiguous_name_exits_2_without_refreshing(wire) -> None:
    inner = wire()
    assert rc.main(["nothing-like-it"]) == 2
    assert rc.main(["raspberry"]) == 2
    assert all(c[1] == LIST for c in inner.calls)


def test_dry_run_resolves_and_never_refreshes(wire, capsys) -> None:
    inner = wire()
    assert rc.main(["skilltest", "--dry-run"]) == 0
    assert all(c[1] == LIST for c in inner.calls)
    assert "dry run: would refresh rp-skilltest (link_b)" in capsys.readouterr().out


def test_list_prints_name_id_and_tool_count_or_json(wire, capsys) -> None:
    wire()
    assert rc.main(["--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Raspberry Pi MCP ") and "link_a  (2 tools)" in lines[0]
    assert lines[-1].startswith("rp-skilltest") and lines[-1].endswith("(0 tools)")
    assert rc.main(["--list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["link_a", "link_c", "link_b"]


def test_json_output_of_a_refresh(wire, capsys) -> None:
    wire((200, {"actions": [{"name": "a"}]}))
    assert rc.main(["skilltest", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name": "rp-skilltest",
        "link_id": "link_b",
        "tools": ["a"],
    }


def test_the_command_needs_a_name_a_link_id_or_list(wire) -> None:
    wire()
    with pytest.raises(SystemExit) as exc:
        rc.main([])
    assert exc.value.code == 2


def test_binnacle_s_command_line_is_accepted(wire) -> None:
    """``chatgpt-refresh --browser auto NAME`` and ``--no-retry``: the
    installed name can point here without breaking a caller."""
    wire((200, {"actions": []}))
    assert rc.main(["--browser", "auto", "--no-retry", "rp-skilltest"]) == 0
