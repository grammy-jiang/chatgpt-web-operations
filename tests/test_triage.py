"""triage.py (the owner's layer 1, 2026-10-01 evening): rules over a failed
run's evidence, a stable signature, a streak kept in a registry, a decision
the cron wrappers act on, and the reproduction's effect on it. Everything
here is files in tmp_path; no network, no browser."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import triage  # noqa: E402

CHALLENGE = (
    "composer did not appear at https://chatgpt.com/: Cloudflare's 'Verify you are "
    "human' challenge is on the page; the browser profile is what is challenged, "
    "not the account [page: challenge=yes, login=no, composers=0 (0 hidden), "
    "toggle=no, attach-menu=no, picker=no, title=other]"
)
CHANGED_PAGE = (
    "composer did not appear at https://chatgpt.com/ (logged out or challenged) "
    "[page: challenge=no, login=no, composers=2 (2 hidden), toggle=yes, "
    "attach-menu=no, picker=no, title=chatgpt]"
)
LOGIN_PAGE = (
    "composer did not appear at https://chatgpt.com/ (logged out or challenged) "
    "[page: challenge=no, login=yes, composers=0 (0 hidden), toggle=no, "
    "attach-menu=no, picker=no, title=chatgpt]"
)
OLD_CLIENT = (
    "composer did not appear at https://chatgpt.com/ (logged out or challenged)"
)
OK_COMPOSER = (
    "the composer appeared at https://chatgpt.com/ through the dedicated browser; "
    "the page matches the fixture recorded 2026-10-01T13:13:00Z"
)


def _check(group: str, name: str, state: str, detail: str) -> dict:
    return {"group": group, "name": name, "state": state, "detail": detail, "fix": None}


def _run(
    tmp_path: Path,
    name: str = "20261002T052501+1000-1-browser",
    *,
    checks: list[dict] | None = None,
    status: str = "ALERT",
    summary: str = "composer did not open",
) -> Path:
    run = tmp_path / "runs" / name
    run.mkdir(parents=True, exist_ok=True)
    (run / "result.txt").write_text(
        f"run_id={name}\nstatus={status}\nsummary={summary}\n", encoding="utf-8"
    )
    (run / "health.json").write_text(
        json.dumps(
            {
                "verdict": "DO NOT START (1 blocking)",
                "exit_code": 1,
                "checks": [_check("host", "available memory", "ok", "fine")]
                + (checks or []),
                "facts": {},
            }
        ),
        encoding="utf-8",
    )
    return run


def _registry(tmp_path: Path) -> Path:
    return tmp_path / "state" / "triage.json"


def _repro(tmp_path: Path, state: str, detail: str) -> str:
    path = tmp_path / "repro.json"
    path.write_text(
        json.dumps(
            {
                "verdict": "GO" if state == "ok" else "DO NOT START (1 blocking)",
                "exit_code": 0 if state == "ok" else 1,
                "checks": [_check("browser", "composer", state, detail)],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def test_result_and_health_are_read_tolerantly(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert triage.read_result(run)["status"] == "ALERT"
    assert triage.read_result(tmp_path) == {}
    assert triage.read_health(tmp_path) == {}
    (tmp_path / "health.json").write_text("[]", encoding="utf-8")
    assert triage.read_health(tmp_path) == {}
    (tmp_path / "health.json").write_text("nope", encoding="utf-8")
    assert triage.read_health(tmp_path) == {}
    assert triage.failed_checks({}) == []
    assert triage.read_lines(None) == []
    assert triage.read_lines(str(tmp_path / "absent")) == []
    (tmp_path / "lines").write_text("a\n\n b \n", encoding="utf-8")
    assert triage.read_lines(str(tmp_path / "lines")) == ["a", "b"]


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        (OK_COMPOSER, "ok"),
        ("the composer appeared; the page differs from the fixture in x", "drift"),
        (CHALLENGE, "challenge"),
        ("composer did not appear: ... requests are being refused", "refused"),
        (
            "composer did not appear: the tab is on 'about:blank', not on chatgpt.com",
            "bounced",
        ),
        (
            "composer did not appear at https://chatgpt.com/c/x: this chat's composer",
            "chat-page",
        ),
        ("the page stayed on the Work surface", "work-surface"),
        ("no scripted window until the cool-down ends", "cooldown"),
        ("the one browser slot was already taken (busy)", "busy"),
        (OLD_CLIENT, "logged-out-or-challenged"),
        ("something else entirely", "other"),
    ],
)
def test_composer_reason_reads_the_detail(detail: str, reason: str) -> None:
    assert triage.composer_reason(detail) == reason


def test_page_facts_parse_the_message_suffix_or_are_empty() -> None:
    assert triage.page_facts(CHANGED_PAGE) == {
        "challenge": False,
        "login": False,
        "composers": 2,
        "hidden": 2,
        "toggle": True,
        "attach_menu": False,
        "picker": False,
    }
    assert triage.page_facts(CHALLENGE)["challenge"] is True
    assert triage.page_facts(OLD_CLIENT) == {}


# ---------------------------------------------------------------------------
# classes and signatures
# ---------------------------------------------------------------------------


def _classify(tmp_path: Path, checks: list[dict], **kw) -> tuple[str, list[str], dict]:
    run = _run(tmp_path, checks=checks)
    evidence = triage.gather(run, **kw)
    cls, reasons = triage.classify(evidence)
    return cls, reasons, evidence


def test_a_challenge_is_a_known_transient(tmp_path: Path) -> None:
    cls, reasons, _ = _classify(
        tmp_path, [_check("browser", "composer", "block", CHALLENGE)]
    )
    assert cls == "known-transient"
    assert "challenge widget" in reasons[0]


def test_a_page_with_neither_challenge_nor_login_and_no_composer_is_a_page_change(
    tmp_path: Path,
) -> None:
    cls, reasons, evidence = _classify(
        tmp_path, [_check("browser", "composer", "block", CHANGED_PAGE)]
    )
    assert cls == "page-change"
    assert "2 composer(s), 2 hidden" in reasons[0]
    assert triage.signature(evidence, cls).startswith(
        "page-change|checks=composer:block|"
    )
    assert "facts=--t--c2h2" in triage.signature(evidence, cls)


def test_a_login_route_or_a_session_block_is_a_session_problem(tmp_path: Path) -> None:
    cls, _, _ = _classify(
        tmp_path, [_check("browser", "composer", "block", LOGIN_PAGE)]
    )
    assert cls == "session"
    cls, _, _ = _classify(
        tmp_path, [_check("health", "session token", "block", "expired 2026-09-01")]
    )
    assert cls == "session"


def test_read_path_refusals_and_slots_are_known_transients(tmp_path: Path) -> None:
    cls, reasons, evidence = _classify(
        tmp_path,
        [
            _check("account", "auth and read", "block", "/backend-api/me returned 403"),
            _check("browser", "composer", "block", OLD_CLIENT),
        ],
    )
    assert cls == "known-transient"
    assert "403" in reasons[0]
    assert triage.signature(evidence, cls).endswith("|http=403")
    for detail in (
        "composer did not appear: requests are being refused (F-2026-10-01-23)",
        "no scripted window until the cool-down ends at 12:00",
        "the one browser slot was already taken (busy)",
    ):
        cls, _, _ = _classify(
            tmp_path, [_check("browser", "composer", "block", detail)]
        )
        assert cls == "known-transient", detail


def test_host_only_failures_and_a_bounced_tab_are_host(tmp_path: Path) -> None:
    cls, reasons, _ = _classify(
        tmp_path,
        [
            _check("host", "load average", "warn", "0.9 per core"),
            _check("host", "in-flight browsers", "block", "2 running"),
        ],
    )
    assert cls == "host" and "only host checks failed" in reasons[0]
    cls, _, _ = _classify(
        tmp_path,
        [
            _check(
                "browser",
                "composer",
                "block",
                "composer did not appear: the tab is on 'about:blank', "
                "not on chatgpt.com",
            )
        ],
    )
    assert cls == "host"


def test_an_old_client_s_message_without_facts_is_unknown(tmp_path: Path) -> None:
    cls, reasons, _ = _classify(
        tmp_path, [_check("browser", "composer", "block", OLD_CLIENT)]
    )
    assert cls == "unknown" and "gave no facts" in reasons[0]
    cls, reasons, _ = _classify(tmp_path, [], summary="the weekly send test failed")
    assert (
        cls == "unknown" and "no rule fits: the weekly send test failed" in reasons[0]
    )


def test_dom_drift_against_the_committed_fixture_is_a_page_change(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path, checks=[_check("browser", "composer", "warn", OK_COMPOSER)])
    fixture = json.loads(
        (SCRIPTS.parent / "tests" / "fixtures" / "dom" / "composer.json").read_text()
    )
    fixture["composer"]["aria_label"] = "Work with ChatGPT"
    fixture["send_button"]["found"] = True
    (run / "dom").mkdir()
    (run / "dom" / "composer.json").write_text(json.dumps(fixture), encoding="utf-8")
    (run / "dom" / "unrelated.json").write_text("{}", encoding="utf-8")  # no fixture
    (run / "dom" / "broken.json").write_text("nope", encoding="utf-8")
    drift = triage.dom_drift(run)
    assert "composer" in drift and "composer.aria_label" in drift["composer"]
    assert set(drift) == {"composer"}
    cls, reasons = triage.classify(triage.gather(run))
    assert cls == "page-change" and "composer: " in reasons[0]
    assert triage.dom_drift(tmp_path) == {}


def test_a_send_body_or_api_shape_change_is_an_api_change(tmp_path: Path) -> None:
    import api_shapes

    run = _run(tmp_path, checks=[])
    (run / "dom").mkdir()
    committed = api_shapes.load_send_body_shape()
    assert committed is not None
    changed = json.loads(json.dumps(committed))
    del changed["fields"]["thinking_effort"]
    (run / "dom" / "send_body.shape.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )
    assert triage.send_body_drift(run) == ["send_body: thinking_effort removed"]
    cls, reasons = triage.classify(triage.gather(run))
    assert cls == "api-change" and "thinking_effort removed" in reasons[0]
    (run / "dom" / "send_body.shape.json").write_text("nope", encoding="utf-8")
    assert triage.send_body_drift(run) == []
    assert triage.send_body_drift(tmp_path) == []
    cls, _ = triage.classify(
        triage.gather(
            _run(
                tmp_path,
                "r2",
                checks=[_check("health", "api shapes", "warn", "differs")],
            )
        )
    )
    assert cls == "api-change"
    lines = tmp_path / "drift.txt"
    lines.write_text("send_body: model retyped (string -> number)\n", encoding="utf-8")
    cls, _ = triage.classify(
        triage.gather(
            _run(tmp_path, "r3", checks=[]), drift_lines=triage.read_lines(str(lines))
        )
    )
    assert cls == "api-change"
    cls, reasons = triage.classify(
        triage.gather(
            _run(tmp_path, "r4", checks=[]),
            drift_lines=["conversation: composer.aria_label"],
        )
    )
    assert cls == "page-change" and "conversation: composer.aria_label" in reasons[0]


def test_drift_outranks_a_challenge_in_the_same_run(tmp_path: Path) -> None:
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", CHALLENGE)])
    cls, _ = triage.classify(
        triage.gather(run, drift_lines=["composer-filled: send_button.found"])
    )
    assert cls == "page-change"


# ---------------------------------------------------------------------------
# streaks, reproduction, decisions
# ---------------------------------------------------------------------------


def test_the_same_signature_three_mornings_running_escalates_on_the_second(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    checks = [_check("browser", "composer", "block", CHALLENGE)]
    first = triage.triage(
        _run(tmp_path, "d1", checks=checks), registry_path=registry, now=1.0
    )
    assert (first["class"], first["decision"], first["streak"]) == (
        "known-transient",
        "digest",
        1,
    )
    second = triage.triage(
        _run(tmp_path, "d2", checks=checks), registry_path=registry, now=2.0
    )
    assert second["decision"] == "escalate-repeat" and second["streak"] == 2
    stored = json.loads(registry.read_text())
    assert stored["last"]["run_id"] == "d2" and stored["last"]["streak"] == 2
    assert stored["signatures"][second["signature"]]["count"] == 2
    assert stored["signatures"][second["signature"]]["runs"] == ["d1", "d2"]


def test_the_browser_streak_survives_a_companion_failure_that_comes_and_goes(
    tmp_path: Path,
) -> None:
    """2026-09-29 to 10-01: the composer blocked every morning; on the third
    the read path answered 403 too, which changes the exact signature. The
    browser streak still reads 3, so the third morning escalates."""
    registry = _registry(tmp_path)
    old = [_check("browser", "composer", "block", OLD_CLIENT)]
    triage.triage(_run(tmp_path, "d1", checks=old), registry_path=registry, now=1.0)
    triage.triage(_run(tmp_path, "d2", checks=old), registry_path=registry, now=2.0)
    third = triage.triage(
        _run(
            tmp_path,
            "d3",
            checks=[
                _check(
                    "account", "auth and read", "block", "/backend-api/me returned 403"
                ),
                *old,
            ],
        ),
        registry_path=registry,
        now=3.0,
    )
    assert third["class"] == "known-transient"
    assert (third["streak"], third["browser_streak"]) == (1, 3)
    assert third["decision"] == "escalate-repeat"


def test_an_ok_run_ends_every_streak(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    checks = [_check("browser", "composer", "block", CHALLENGE)]
    triage.triage(_run(tmp_path, "d1", checks=checks), registry_path=registry, now=1.0)
    assert triage.main(["--ok", "d2", "--registry", str(registry)]) == 0
    assert json.loads(registry.read_text())["last"]["signature"] == "ok"
    third = triage.triage(
        _run(tmp_path, "d3", checks=checks), registry_path=registry, now=3.0
    )
    assert (third["streak"], third["browser_streak"], third["decision"]) == (
        1,
        1,
        "digest",
    )


def test_a_reproduction_that_passes_makes_it_a_digest_line(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", CHANGED_PAGE)])
    record = triage.triage(
        run, repro_path=_repro(tmp_path, "ok", OK_COMPOSER), registry_path=registry
    )
    assert record["class"] == "page-change"
    assert record["decision"] == "digest"
    assert record["repro"]["composer"] == "ok" and record["repro"]["ran"] is True
    assert any("did not repeat" in r for r in record["reasons"])


def test_a_reproduction_that_fails_the_same_way_escalates_an_unknown_at_once(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", OLD_CLIENT)])
    record = triage.triage(
        run, repro_path=_repro(tmp_path, "block", OLD_CLIENT), registry_path=registry
    )
    assert record["decision"] == "escalate"
    assert any("failed the same way" in r for r in record["reasons"])


def test_a_known_transient_that_reproduces_stays_a_digest_line_until_it_repeats(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", CHALLENGE)])
    record = triage.triage(
        run, repro_path=_repro(tmp_path, "block", CHALLENGE), registry_path=registry
    )
    assert record["decision"] == "digest"
    assert any("a known transient repeats" in r for r in record["reasons"])


def test_a_reproduction_whose_page_drifted_escalates_as_a_page_change(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", CHALLENGE)])
    drifted = (
        "the composer appeared at https://chatgpt.com/; the page differs from the "
        "fixture recorded 2026-10-01T13:13:42Z in composers.hidden"
    )
    record = triage.triage(
        run, repro_path=_repro(tmp_path, "warn", drifted), registry_path=registry
    )
    assert record["decision"] == "escalate"
    assert record["repro"]["reason"] == "drift"
    assert any("page change" in r for r in record["reasons"])


def test_repro_summary_tolerates_a_missing_or_broken_file(tmp_path: Path) -> None:
    assert triage.repro_summary(None) == {"ran": False}
    assert triage.repro_summary(str(tmp_path / "absent.json")) == {"ran": False}
    (tmp_path / "bad.json").write_text("nope", encoding="utf-8")
    assert triage.repro_summary(str(tmp_path / "bad.json")) == {"ran": False}
    (tmp_path / "empty.json").write_text(json.dumps({"checks": []}), encoding="utf-8")
    assert triage.repro_summary(str(tmp_path / "empty.json"))["composer"] is None


def test_decide_covers_every_class() -> None:
    no_repro = {"ran": False}
    assert triage.decide("page-change", 1, no_repro)[0] == "escalate"
    assert triage.decide("api-change", 1, no_repro)[0] == "escalate"
    assert triage.decide("session", 1, no_repro)[0] == "escalate"
    assert triage.decide("unknown", 1, no_repro)[0] == "escalate"
    assert triage.decide("known-transient", 1, no_repro)[0] == "digest"
    assert triage.decide("host", 1, no_repro)[0] == "digest"
    assert triage.decide("host", 2, no_repro)[0] == "escalate-repeat"
    assert triage.decide("host", 3, no_repro, threshold=5)[0] == "digest"


def test_registry_survives_a_missing_or_broken_file(tmp_path: Path) -> None:
    assert triage.load_registry(tmp_path / "none.json") == {
        "signatures": {},
        "last": None,
    }
    (tmp_path / "bad.json").write_text("[1]", encoding="utf-8")
    assert triage.load_registry(tmp_path / "bad.json") == {
        "signatures": {},
        "last": None,
    }
    (tmp_path / "half.json").write_text("{}", encoding="utf-8")
    assert triage.load_registry(tmp_path / "half.json") == {
        "signatures": {},
        "last": None,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_writes_the_record_and_exits_by_decision(tmp_path: Path, capsys) -> None:
    registry = _registry(tmp_path)
    digest_run = _run(
        tmp_path, "d1", checks=[_check("browser", "composer", "block", CHALLENGE)]
    )
    assert (
        triage.main(
            ["--run", str(digest_run), "--registry", str(registry), "--dry-run"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Triage: digest -- class known-transient, streak 1 (browser 1)" in out
    assert "reproduction: not run" in out
    assert json.loads((digest_run / "triage.json").read_text())["decision"] == "digest"
    assert not registry.exists()  # --dry-run

    changed = _run(
        tmp_path, "d2", checks=[_check("browser", "composer", "block", CHANGED_PAGE)]
    )
    target = tmp_path / "out.json"
    assert (
        triage.main(
            [
                "--run",
                str(changed),
                "--registry",
                str(registry),
                "--json",
                str(target),
                "--repro",
                _repro(tmp_path, "block", CHANGED_PAGE),
            ]
        )
        == 2
    )
    record = json.loads(target.read_text())
    assert record["decision"] == "escalate" and record["repro"]["composer"] == "block"
    assert (
        "reproduction: composer block (logged-out-or-challenged)"
        in capsys.readouterr().out
    )
    assert registry.exists()


def test_main_rejects_a_missing_run_and_needs_an_argument(
    tmp_path: Path, capsys
) -> None:
    assert triage.main(["--run", str(tmp_path / "absent")]) == 1
    assert "not a run directory" in capsys.readouterr().out
    assert triage.main([]) == 1
    assert "give --run DIR or --ok RUN_ID" in capsys.readouterr().out


def test_main_reports_an_unwritable_record_without_failing(
    tmp_path: Path, capsys
) -> None:
    registry = _registry(tmp_path)
    run = _run(tmp_path, checks=[_check("browser", "composer", "block", CHALLENGE)])
    assert (
        triage.main(
            [
                "--run",
                str(run),
                "--registry",
                str(registry),
                "--json",
                str(tmp_path / "no" / "such" / "dir" / "x.json"),
            ]
        )
        == 0
    )
    assert "triage record not written" in capsys.readouterr().out
