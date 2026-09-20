"""Tests for scripts/list_skills.py (ROADMAP.md R5, skills and apps).

Same house style as tests/test_search_chats.py: a fake ``session.session``
records every call it is given and answers from a queue, ``main()`` is
exercised over that fake, and every test name says which behaviour it
defends. Fixture items are hand-written here, never recorded from the real
account: real names would need the fixture scanner to allowlist them for
no reason (TESTING.md section 1), so every name, description and prompt
below is fake.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


list_skills = _load("list_skills")


# ---------------------------------------------------------------------------
# fixtures -- hand-written, never recorded (see module docstring)
# ---------------------------------------------------------------------------


def _skill(
    name: str = "fake-skill",
    *,
    enabled: bool = True,
    default_version_no: str | None = "1",
    latest_version_no: str | None = "1",
    safety_check_status: str = "unchecked",
    risk_score: int | None = 0,
    labels: list[str] | None = None,
    files: dict[str, Any] | None = None,
    updated_at: str | None = "2026-09-18T00:00:00Z",
    last_updated_at: str | None = "2026-09-17T00:00:00Z",
    check_sum_hash: str = "abcdef0123456789abcdef0123456789",
    sample_prompts: list[str] | None = None,
) -> dict[str, Any]:
    """One hand-written hazelnut, shaped like the keys measured 2026-09-21
    (module docstring). Fake name, description and prompts throughout."""
    return {
        "base_sediment_id": "sed-base-fake-1",
        "brand_color": "#123456",
        "check_sum_hash": check_sum_hash,
        "creator_id": "user-fake",
        "creator_name": "Fake Person",
        "default_version_no": default_version_no,
        "description": "a fake skill made up for a test",
        "display_name": name.replace("-", " ").title(),
        "enabled": enabled,
        "files": (
            files if files is not None else {"SKILL.md": {"start": 0, "length": 100}}
        ),
        "icon_small": "fake-icon-small.png",
        "iconography": "generic",
        "id": f"hz-{name}",
        "in_my_list": True,
        "last_updated_at": last_updated_at,
        "latest_version_no": latest_version_no,
        "name": name,
        "permissions": {
            "can_read": True,
            "can_write": True,
            "can_export": True,
            "can_share": False,
            "can_share_workspace": False,
            "can_enable_share": False,
            "can_delete": True,
        },
        "safety_check_status": safety_check_status,
        "safety_scan": {
            "risk_score": risk_score,
            "justification": "fake justification text",
            "labels": labels if labels is not None else [],
        },
        "sample_prompts": (
            sample_prompts if sample_prompts is not None else ["fake sample prompt"]
        ),
        "sediment_id": "sed-fake-1",
        "short_description": "a short fake description",
        "surfaces": ["tpp"],
        "updated_at": updated_at,
    }


def _plugin(
    name: str = "fake-app",
    *,
    display_name: str = "Fake App",
    creator_name: str = "OpenAI",
    scope: str = "GLOBAL",
    status: str = "ENABLED",
    enabled: bool = True,
    version: str = "1.0.0",
    installed_at: str = "2026-09-10T00:00:00Z",
) -> dict[str, Any]:
    """One hand-written plugin/app record, shaped like the keys measured
    2026-09-21. Fake names throughout."""
    return {
        "id": f"plugin-{name}",
        "name": name,
        "created_at": "2026-01-01T00:00:00Z",
        "scope": scope,
        "status": status,
        "enabled": enabled,
        "installed_at": installed_at,
        "creator_name": creator_name,
        "connector_id": f"connector_{name}",
        "canonical_app_id": f"app_{name}",
        "release": {
            "version": version,
            "display_name": display_name,
            "description": "a fake release description",
            "interface": {},
            "skills": [],
        },
        "installation_policy": "auto",
        "authentication_policy": "none",
        "disabled_reason": None,
        "disabled_skill_names": [],
    }


class _FakeSkillsBackend:
    """``session.session.call`` recording every call; answers from a queue."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Any]] = []

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int = 3,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        if not self.responses:
            return 200, {"hazelnuts": []}
        return self.responses.pop(0)


class _FakeSkillsSession:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.session = _FakeSkillsBackend(responses)


# ---------------------------------------------------------------------------
# files_count
# ---------------------------------------------------------------------------


def test_files_count_counts_the_dict() -> None:
    item = _skill(
        files={"a.md": {"start": 0, "length": 1}, "b.md": {"start": 1, "length": 2}}
    )
    assert list_skills.files_count(item) == 2


def test_files_count_is_zero_when_missing_or_wrong_type() -> None:
    assert list_skills.files_count({}) == 0
    assert list_skills.files_count({"files": ["not", "a", "dict"]}) == 0


# ---------------------------------------------------------------------------
# versions_label
# ---------------------------------------------------------------------------


def test_versions_label_both_present() -> None:
    item = _skill(default_version_no="1", latest_version_no="14")
    assert list_skills.versions_label(item) == "1/14"


def test_versions_label_missing_default() -> None:
    item = _skill(default_version_no=None, latest_version_no="14")
    assert list_skills.versions_label(item) == "?/14"


def test_versions_label_missing_both() -> None:
    item = _skill(default_version_no=None, latest_version_no=None)
    assert list_skills.versions_label(item) == "?/?"


# ---------------------------------------------------------------------------
# risk_label / labels_label
# ---------------------------------------------------------------------------


def test_risk_label_shows_zero_not_a_dash() -> None:
    """risk_score 0 is a real, meaningful score and must not read as absent."""
    item = _skill(risk_score=0)
    assert list_skills.risk_label(item) == "0"


def test_risk_label_dash_when_absent() -> None:
    item = _skill(risk_score=None)
    assert list_skills.risk_label(item) == "-"
    assert list_skills.risk_label({}) == "-"


def test_labels_label_joins_with_commas() -> None:
    item = _skill(labels=["safeguard_evasion", "other_label"])
    assert list_skills.labels_label(item) == "safeguard_evasion,other_label"


def test_labels_label_empty_when_none() -> None:
    item = _skill(labels=[])
    assert list_skills.labels_label(item) == ""
    assert list_skills.labels_label({}) == ""


# ---------------------------------------------------------------------------
# checksum_label / updated_label / on_label
# ---------------------------------------------------------------------------


def test_checksum_label_first_12_characters() -> None:
    item = _skill(check_sum_hash="abcdef0123456789abcdef0123456789")
    assert list_skills.checksum_label(item) == "abcdef012345"


def test_checksum_label_empty_when_absent() -> None:
    assert list_skills.checksum_label({}) == ""


def test_updated_label_prefers_updated_at() -> None:
    item = _skill(
        updated_at="2026-09-18T00:00:00Z", last_updated_at="2026-09-01T00:00:00Z"
    )
    assert list_skills.updated_label(item) == "2026-09-18"


def test_updated_label_falls_back_to_last_updated_at() -> None:
    item = _skill(updated_at=None, last_updated_at="2026-09-01T00:00:00Z")
    assert list_skills.updated_label(item) == "2026-09-01"


def test_updated_label_empty_when_both_absent() -> None:
    item = _skill(updated_at=None, last_updated_at=None)
    assert list_skills.updated_label(item) == ""


def test_on_label() -> None:
    assert list_skills.on_label(True) == "yes"
    assert list_skills.on_label(False) == "no"
    assert list_skills.on_label(None) == "no"


# ---------------------------------------------------------------------------
# skill_row / app_row
# ---------------------------------------------------------------------------


def test_skill_row_column_order() -> None:
    item = _skill(
        name="fake-skill",
        enabled=True,
        default_version_no="1",
        latest_version_no="14",
        safety_check_status="blocked",
        risk_score=3,
        labels=["safeguard_evasion"],
        files={"a.md": {"start": 0, "length": 1}},
        updated_at="2026-09-18T00:00:00Z",
        check_sum_hash="abcdef0123456789",
    )
    assert list_skills.skill_row(item) == (
        "fake-skill",
        "yes",
        "1/14",
        "blocked",
        "3",
        "safeguard_evasion",
        "1",
        "2026-09-18",
        "abcdef012345",
    )


def test_app_row_column_order() -> None:
    item = _plugin(
        name="fake-app",
        display_name="Fake App",
        creator_name="OpenAI",
        scope="GLOBAL",
        status="ENABLED",
        version="1.2.3",
        installed_at="2026-09-10T00:00:00Z",
    )
    assert list_skills.app_row(item) == (
        "Fake App",
        "OpenAI",
        "GLOBAL",
        "ENABLED",
        "1.2.3",
        "2026-09-10",
    )


def test_app_row_falls_back_to_the_plugin_name() -> None:
    item = _plugin(name="fake-app")
    item["release"] = {}
    assert list_skills.app_row(item)[0] == "fake-app"


# ---------------------------------------------------------------------------
# find_skill
# ---------------------------------------------------------------------------


def test_find_skill_matches_by_exact_name() -> None:
    items = [_skill("alpha"), _skill("beta")]
    found = list_skills.find_skill(items, "beta")
    assert found is not None
    assert found["name"] == "beta"


def test_find_skill_returns_none_when_absent() -> None:
    items = [_skill("alpha")]
    assert list_skills.find_skill(items, "no-such-skill") is None


def test_find_skill_tolerates_a_non_dict_item() -> None:
    items = ["not-a-dict", _skill("alpha")]
    assert list_skills.find_skill(items, "alpha") is not None  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# expectation_line
# ---------------------------------------------------------------------------


def test_expectation_line_not_installed() -> None:
    line, met = list_skills.expectation_line("missing-skill", None)
    assert line == "expect missing-skill: NOT installed"
    assert met is False


def test_expectation_line_installed_and_enabled() -> None:
    item = _skill(
        "fake-skill",
        enabled=True,
        safety_check_status="blocked",
        default_version_no="1",
        latest_version_no="14",
    )
    line, met = list_skills.expectation_line("fake-skill", item)
    assert (
        line == "expect fake-skill: installed, enabled, safety blocked, versions 1/14"
    )
    assert met is True


def test_expectation_line_installed_but_disabled() -> None:
    item = _skill("fake-skill", enabled=False)
    line, met = list_skills.expectation_line("fake-skill", item)
    assert "disabled" in line
    assert met is False


# ---------------------------------------------------------------------------
# redacted_skill
# ---------------------------------------------------------------------------


def test_redacted_skill_replaces_files_with_a_count() -> None:
    item = _skill(
        files={"a.md": {"start": 0, "length": 1}, "b.md": {"start": 1, "length": 2}}
    )
    out = list_skills.redacted_skill(item)
    assert out["files"] == 2


def test_redacted_skill_drops_sample_prompts() -> None:
    item = _skill(sample_prompts=["do the fake thing"])
    out = list_skills.redacted_skill(item)
    assert "sample_prompts" not in out


def test_redacted_skill_keeps_other_fields_verbatim() -> None:
    item = _skill("fake-skill", safety_check_status="blocked")
    out = list_skills.redacted_skill(item)
    assert out["name"] == "fake-skill"
    assert out["safety_check_status"] == "blocked"
    assert out["check_sum_hash"] == item["check_sum_hash"]


# ---------------------------------------------------------------------------
# main() -- basic skills listing
# ---------------------------------------------------------------------------


def test_main_lists_skills_with_one_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_skill("fake-skill-one"), _skill("fake-skill-two")]
    session = _FakeSkillsSession([(200, {"hazelnuts": items})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main([]) == 0
    assert session.session.calls == [
        (
            "GET",
            "/backend-api/hazelnuts?include_permissions=true&scope=installed",
            None,
        )
    ]
    out = capsys.readouterr().out
    assert "2 skill(s)" in out
    assert "fake-skill-one" in out and "fake-skill-two" in out


def test_main_forwards_the_browser_flag_to_open_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}
    session = _FakeSkillsSession([(200, {"hazelnuts": []})])

    def _open(browser: str = "chrome") -> Any:
        seen["browser"] = browser
        return session

    monkeypatch.setattr(list_skills, "open_session", _open)
    list_skills.main(["--browser", "chromium"])
    assert seen["browser"] == "chromium"


def test_main_hazelnuts_read_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSkillsSession([(500, {"error": "boom"})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main([]) == 1
    out = capsys.readouterr().out
    assert "hazelnuts read failed" in out
    assert "500" in out


def test_main_treats_a_non_dict_200_response_as_a_failed_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSkillsSession([(200, None)])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main([]) == 1
    assert "hazelnuts read failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() -- --apps
# ---------------------------------------------------------------------------


def test_main_apps_flag_makes_a_second_call_and_lists_apps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = [
        _plugin("fake-app-one", display_name="Fake App One"),
        _plugin("fake-app-two", display_name="Fake App Two"),
    ]
    session = _FakeSkillsSession(
        [
            (200, {"hazelnuts": []}),
            (
                200,
                {
                    "plugins": plugins,
                    "pagination": {"limit": 1000, "next_page_token": None},
                },
            ),
        ]
    )
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--apps"]) == 0
    assert [c[1] for c in session.session.calls] == [
        "/backend-api/hazelnuts?include_permissions=true&scope=installed",
        "/backend-api/ps/plugins/installed?limit=1000",
    ]
    out = capsys.readouterr().out
    assert "2 app(s)" in out
    assert "Fake App One" in out and "Fake App Two" in out


def test_main_without_apps_flag_never_calls_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSkillsSession([(200, {"hazelnuts": []})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    list_skills.main([])
    assert len(session.session.calls) == 1


def test_main_apps_next_page_token_prints_more_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSkillsSession(
        [
            (200, {"hazelnuts": []}),
            (
                200,
                {
                    "plugins": [_plugin("fake-app")],
                    "pagination": {"limit": 1000, "next_page_token": "opaque"},
                },
            ),
        ]
    )
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--apps"]) == 0
    out = capsys.readouterr().out
    assert "more exist (next_page_token returned, not followed)" in out


def test_main_apps_read_failure_exits_1_but_keeps_the_skills_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSkillsSession(
        [(200, {"hazelnuts": [_skill("fake-skill")]}), (500, {"error": "boom"})]
    )
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--apps"]) == 1
    out = capsys.readouterr().out
    assert "fake-skill" in out
    assert "plugins read failed" in out
    assert "500" in out


# ---------------------------------------------------------------------------
# main() -- --expect
# ---------------------------------------------------------------------------


def test_main_expect_met_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_skill("research-pipeline", enabled=True, safety_check_status="blocked")]
    session = _FakeSkillsSession([(200, {"hazelnuts": items})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--expect", "research-pipeline"]) == 0
    out = capsys.readouterr().out
    assert "expect research-pipeline: installed, enabled" in out


def test_main_expect_missing_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _FakeSkillsSession([(200, {"hazelnuts": []})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--expect", "no-such-skill"]) == 1
    out = capsys.readouterr().out
    assert "expect no-such-skill: NOT installed" in out


def test_main_expect_disabled_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_skill("fake-skill", enabled=False)]
    session = _FakeSkillsSession([(200, {"hazelnuts": items})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    assert list_skills.main(["--expect", "fake-skill"]) == 1


def test_main_expect_multiple_names_reports_every_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [_skill("fake-skill-good", enabled=True)]
    session = _FakeSkillsSession([(200, {"hazelnuts": items})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    code = list_skills.main(
        ["--expect", "fake-skill-good", "--expect", "fake-skill-missing"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "expect fake-skill-good: installed, enabled" in out
    assert "expect fake-skill-missing: NOT installed" in out


# ---------------------------------------------------------------------------
# main() -- --json
# ---------------------------------------------------------------------------


def test_main_json_skills_only_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [_skill("fake-skill", files={"a.md": {"start": 0, "length": 1}})]
    session = _FakeSkillsSession([(200, {"hazelnuts": items})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "skills.json"
    assert list_skills.main(["--json", str(out_path)]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(doc) == {"skills"}
    assert len(doc["skills"]) == 1
    assert doc["skills"][0]["files"] == 1
    assert "sample_prompts" not in doc["skills"][0]
    assert doc["skills"][0]["name"] == "fake-skill"


def test_main_json_with_apps_adds_the_apps_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugins = [_plugin("fake-app")]
    session = _FakeSkillsSession(
        [
            (200, {"hazelnuts": []}),
            (200, {"plugins": plugins, "pagination": {"next_page_token": None}}),
        ]
    )
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "skills.json"
    assert list_skills.main(["--apps", "--json", str(out_path)]) == 0
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(doc) == {"skills", "apps"}
    assert doc["apps"] == plugins


def test_main_json_omits_apps_key_without_the_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _FakeSkillsSession([(200, {"hazelnuts": []})])
    monkeypatch.setattr(list_skills, "open_session", lambda *a, **k: session)
    out_path = tmp_path / "skills.json"
    list_skills.main(["--json", str(out_path)])
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert "apps" not in doc
