"""Tests for the harness pieces added with the replay tier (TESTING.md
section 6, P1): the loopback rule in tests/conftest.py and the fixture
promotion script tests/refresh_dom_fixtures.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import conftest
import pytest
import refresh_dom_fixtures as rdf


class _Item:
    def __init__(self, markers: set[str]) -> None:
        self._markers = markers

    def get_closest_marker(self, name: str):
        return object() if name in self._markers else None


# ---------------------------------------------------------------------------
# the loopback rule
# ---------------------------------------------------------------------------


def test_is_local_marked_true_only_for_the_replay_marker() -> None:
    assert conftest.is_local_marked(_Item({"replay"})) is True
    assert conftest.is_local_marked(_Item({"live_read"})) is False
    assert conftest.is_local_marked(_Item(set())) is False
    assert conftest.is_live_marked(_Item({"replay"})) is False


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "", None, b"::1"])
def test_loopback_getaddrinfo_passes_loopback_hosts_through(host) -> None:
    seen: list = []
    wrapped = conftest.loopback_getaddrinfo(lambda *a, **k: seen.append((a, k)) or "ok")
    assert wrapped(host, 80) == "ok"
    assert seen == [((host, 80), {})]


@pytest.mark.parametrize("host", ["chatgpt.com", "198.51.100.1", b"example.org"])
def test_loopback_getaddrinfo_refuses_anything_else(host) -> None:
    wrapped = conftest.loopback_getaddrinfo(lambda *a, **k: pytest.fail("resolved"))
    with pytest.raises(RuntimeError, match="not loopback"):
        wrapped(host, 443)


def test_loopback_create_connection_checks_the_address_host() -> None:
    wrapped = conftest.loopback_create_connection(lambda address, *a, **k: address)
    assert wrapped(("127.0.0.1", 9)) == ("127.0.0.1", 9)
    with pytest.raises(RuntimeError, match="not loopback"):
        wrapped(("chatgpt.com", 443), timeout=1)


# ---------------------------------------------------------------------------
# refresh_dom_fixtures.promote
# ---------------------------------------------------------------------------

RAW = (
    "<!doctype html><html><body><main>\n"
    '<form class="c"><div id="prompt-textarea" contenteditable="true">'
    "typed text stays out</div>"
    '<button data-testid="send-button" aria-label="Send prompt"></button></form>\n'
    "</main></body></html>\n"
)
FACTS = {
    "url": (
        "https://chatgpt.com/g/g-p-6aaea9da2bc881918d6f9eb5177cf904-x"
        "/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ),
    "composer": {"found": True, "matched": ["#prompt-textarea"]},
    "send_button": {"found": True, "testid": "send-button"},
    "turns": {"matched_user": ['[data-user-message-bubble="true"]']},
    "recorded_at": "2026-09-27T00:00:00Z",
}


def _source(tmp_path: Path, markup: str = RAW, facts: dict | None = None) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "composer.html").write_text(markup, encoding="utf-8")
    (src / "composer.json").write_text(json.dumps(facts or FACTS), encoding="utf-8")
    return src


def test_promote_writes_a_sanitized_document_and_record(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    assert rdf.promote(_source(tmp_path), "composer", dest) == {
        "html": "written",
        "json": "written",
    }
    html = (dest / "composer.html").read_text(encoding="utf-8")
    assert "typed text" not in html
    assert (
        '<div id="prompt-textarea" contenteditable="true">'
        '<br data-snapshot-text=""></div>'
    ) in html
    assert html.startswith("<!doctype html>")
    record = json.loads((dest / "composer.json").read_text(encoding="utf-8"))
    assert record["composer"]["matched"] == ["#prompt-textarea"]
    # Only the URL is scrubbed (its g-p- and conversation ids); a selector
    # in the facts is never rewritten (the first promotion turned
    # data-user-message-bubble into data-user-XXXXXXXX-bubble).
    assert "aaaaaaaa-bbbb" not in record["url"]
    assert "6aaea9da2bc881918d6f9eb5177cf904" not in record["url"]
    assert record["turns"]["matched_user"] == ['[data-user-message-bubble="true"]']


def test_promote_reports_unchanged_on_a_second_run(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    src = _source(tmp_path)
    rdf.promote(src, "composer", dest)
    assert rdf.promote(src, "composer", dest) == {
        "html": "unchanged",
        "json": "unchanged",
    }


def test_promote_refuses_markup_the_hygiene_rules_reject(tmp_path: Path) -> None:
    """An attribute value can carry an identity past the sanitizer; the
    hygiene check is what refuses it, before anything is written."""
    src = _source(tmp_path, RAW.replace('class="c"', 'aria-label="mail x@y.z"'))
    with pytest.raises(ValueError, match="refusing to promote"):
        rdf.promote(src, "composer", tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_promote_refuses_a_record_the_hygiene_rules_reject(tmp_path: Path) -> None:
    facts = dict(FACTS, note="from grammy")
    src = _source(tmp_path, facts=facts)
    with pytest.raises(ValueError, match="json: the account holder's name"):
        rdf.promote(src, "composer", tmp_path / "dest")


def test_promote_names_a_missing_file(tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.mkdir()
    with pytest.raises(FileNotFoundError, match="composer.html"):
        rdf.promote(src, "composer", tmp_path / "dest")


def test_main_prints_the_result_and_exits_1_on_a_refusal(tmp_path: Path, capsys):
    src = _source(tmp_path)
    assert rdf.main([str(src), "--dest", str(tmp_path / "dest")]) == 0
    out = capsys.readouterr().out
    assert "composer.html: written" in out and "make replay" in out
    assert rdf.main([str(tmp_path / "nowhere"), "--dest", str(tmp_path / "d")]) == 1
    assert "refresh_dom_fixtures:" in capsys.readouterr().out


def test_default_source_follows_the_last_run_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(rdf, "STATE", tmp_path)
    (tmp_path / "last-run-id").write_text("20260927T052501+1000-1-browser\n")
    assert rdf.default_source() == (
        tmp_path / "runs" / "20260927T052501+1000-1-browser" / "dom"
    )


# ---------------------------------------------------------------------------
# the suite is isolated from the shell's recording settings (2026-09-27)
# ---------------------------------------------------------------------------


def test_a_plain_test_never_sees_the_shell_s_recording_settings(
    pytester, monkeypatch
) -> None:
    """The daily wrapper exports RP_SNAPSHOT_DIR before it runs make test;
    on 2026-09-27 three fake-sender tests inherited it and failed under
    cron only (references/failure-atlas.md). An inner pytest session run
    with the variables set in the shell must show a plain test none of
    them, and must point the evidence paths into its own tmp_path."""
    monkeypatch.setenv("RP_SNAPSHOT_DIR", "/tmp/leaked-by-the-shell")
    monkeypatch.setenv("RP_SNAPSHOT_FIXTURE", "/tmp/leaked-fixture.json")
    monkeypatch.setenv("RP_EVENTS_FILE", "/tmp/leaked-events.jsonl")
    monkeypatch.setenv("RP_SCREENSHOT_DIR", "/tmp/leaked-shots")
    pytester.makeconftest(Path(conftest.__file__).read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_probe="""
import os

def test_plain_test_environment():
    assert "RP_SNAPSHOT_DIR" not in os.environ
    assert "RP_SNAPSHOT_FIXTURE" not in os.environ
    assert os.environ["RP_EVENTS_FILE"] != "/tmp/leaked-events.jsonl"
    assert "rp-evidence" in os.environ["RP_EVENTS_FILE"]
    assert os.environ["RP_SCREENSHOT_DIR"] != "/tmp/leaked-shots"
"""
    )
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
