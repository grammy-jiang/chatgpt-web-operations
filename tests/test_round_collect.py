"""Tests for scripts/round_status.py's ``--collect [--apply]``.

An interrupted research run can leave a ``chatgpt/conversations.json`` entry
at status "sent" forever: the orchestrator's own ``--resume`` only picks one
back up when its round matches the round the run is currently on and its
prompt fingerprint still matches, so an entry from a past round can never be
resumed -- its reply, if it still exists, can only be archived as evidence.
``--collect`` classifies every such entry into exactly one of four verdicts
("resumable", "superseded", "collectable", "lost") and, with ``--apply``,
acts on it. This file owns that behaviour; tests/test_commands.py's and
tests/test_command_mains_2.py's existing round_status tests are untouched
and must keep passing exactly as they are.

Runs entirely over fake sessions and tmp_path fixtures (HARD RULE 1: no
network, no browser, no real Session). Every test names the failure it
defends against, the rule test_commands.py uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import round_status  # noqa: E402
from chatgpt_client import TransportError  # noqa: E402

# ---------------------------------------------------------------------------
# conversation builders (test_read_chat_effort.py's _msg/_conv pattern,
# duplicated locally rather than imported -- house convention, see that
# file's own docstring: nothing outside a test file may be edited to add a
# shared fixture).
# ---------------------------------------------------------------------------


def _msg(
    node_id: str,
    role: str,
    *,
    text: str = "text",
    recipient: str | None = "all",
    status: str = "finished_successfully",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "author": {"role": role},
        "content": {"content_type": "text", "parts": [text]},
        "recipient": recipient,
        "status": status,
        "metadata": metadata or {},
    }


def _conv(*msgs: dict[str, Any], title: str = "a chat") -> dict[str, Any]:
    """A conversation whose mapping links each message to the previous one."""
    mapping: dict[str, Any] = {}
    parent = None
    for m in msgs:
        mapping[m["id"]] = {"message": m, "parent": parent}
        parent = m["id"]
    return {"mapping": mapping, "current_node": parent, "title": title}


# ---------------------------------------------------------------------------
# ledger / workdir builders
# ---------------------------------------------------------------------------


def _topic(tmp_path: Path, current_round: int = 2, run_id: str = "r1") -> Path:
    """A topic directory with an admitted-nothing-yet run -- these tests
    only care about the ledger and --collect, never the paper-completeness
    report, so the run's own screen/analysis stay empty on purpose."""
    work = tmp_path / "topic"
    run = work / "runs" / run_id
    (run / "screen").mkdir(parents=True)
    (run / "analysis").mkdir(parents=True)
    (work / "chatgpt").mkdir(parents=True)
    (work / "workflow_state.json").write_text(
        json.dumps({"run_id": run_id, "round": current_round, "max_rounds": 4})
    )
    return work


def _entry(job: str, chat: str, round_no: int, status: str, **extra: Any) -> dict:
    return {"job": job, "chat": chat, "round": round_no, "status": status, **extra}


def _write_ledger(work: Path, entries: list[dict[str, Any]]) -> Path:
    path = work / "chatgpt" / "conversations.json"
    path.write_text(json.dumps(entries))
    return path


def _ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "chatgpt" / "conversations.json").read_text())


# ---------------------------------------------------------------------------
# fake session / process helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """``get_conversation`` over a fixed table, recording every chat id
    asked for -- unreadable ids raise the real TransportError."""

    def __init__(
        self, convs: dict[str, dict[str, Any]], unreadable: frozenset[str] = frozenset()
    ) -> None:
        self.convs = convs
        self.unreadable = unreadable
        self.calls: list[str] = []

    def get_conversation(self, chat_id: str) -> dict[str, Any]:
        self.calls.append(chat_id)
        if chat_id in self.unreadable:
            raise TransportError(f"GET conversation/{chat_id} -> HTTP 404", 404)
        return self.convs[chat_id]


def _boom_if_called(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("open_session must not run for a refused --collect --apply")


def _wire(monkeypatch: Any, session: Any) -> None:
    monkeypatch.setattr(round_status, "open_session", lambda *a, **k: session)


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _pgrep_finds_nothing(monkeypatch: Any) -> None:
    monkeypatch.setattr(round_status.subprocess, "run", lambda *a, **k: _Completed(1))


def _pgrep_finds_a_match(monkeypatch: Any) -> None:
    monkeypatch.setattr(round_status.subprocess, "run", lambda *a, **k: _Completed(0))


def _pgrep_must_not_run(monkeypatch: Any) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a dry run must never check for the orchestrator")

    monkeypatch.setattr(round_status.subprocess, "run", _boom)


# ---------------------------------------------------------------------------
# classify_sent_entry / successful_twin -- pure, over ledger data alone
# ---------------------------------------------------------------------------


def test_successful_twin_finds_the_matching_done_entry() -> None:
    done = _entry("screen", "chat-old", 1, "done", transcript="/t/r1-screen.json")
    sent = _entry("screen", "chat-new", 1, "sent")
    assert round_status.successful_twin(sent, [sent, done]) == done


def test_successful_twin_ignores_a_different_job_or_round() -> None:
    sent = _entry("screen", "chat-1", 1, "sent")
    other_job = _entry("terminology", "chat-2", 1, "done")
    other_round = _entry("screen", "chat-3", 2, "done")
    assert round_status.successful_twin(sent, [sent, other_job, other_round]) is None


def test_successful_twin_skips_a_non_dict_entry() -> None:
    """A malformed ledger row must not crash the classifier."""
    sent = _entry("screen", "chat-1", 1, "sent")
    assert round_status.successful_twin(sent, [sent, "not-a-dict", None]) is None


def test_classify_current_round_is_resumable_even_when_unreadable() -> None:
    """The orchestrator's own --resume still owns this one; readable must
    never override that."""
    entry = _entry("screen", "chat-1", 2, "sent")
    verdict = round_status.classify_sent_entry(entry, 2, [entry], readable=False)
    assert verdict == "resumable"


def test_classify_past_round_with_a_done_twin_is_superseded() -> None:
    sent = _entry("screen", "chat-new", 1, "sent")
    done = _entry("screen", "chat-old", 1, "done")
    verdict = round_status.classify_sent_entry(sent, 2, [sent, done], readable=False)
    assert verdict == "superseded"


def test_classify_past_round_no_twin_readable_is_collectable() -> None:
    entry = _entry("screen", "chat-1", 1, "sent")
    assert (
        round_status.classify_sent_entry(entry, 2, [entry], readable=True)
        == "collectable"
    )


def test_classify_past_round_no_twin_unreadable_is_lost() -> None:
    entry = _entry("screen", "chat-1", 1, "sent")
    assert round_status.classify_sent_entry(entry, 2, [entry], readable=False) == "lost"


# ---------------------------------------------------------------------------
# longest_reply, orphan_transcript, orphan_paths -- pure, over a conversation
# ---------------------------------------------------------------------------


def test_longest_reply_picks_the_longest_not_the_last() -> None:
    """An interrupted turn can leave more than one assistant text message;
    the finished answer is worth archiving, not a short trailing line."""
    conv = _conv(
        _msg("1", "user", text="hi"),
        _msg("2", "assistant", text="a" * 500),
        _msg("3", "assistant", text="ok"),
    )
    assert round_status.longest_reply(conv) == "a" * 500


def test_longest_reply_of_an_empty_conversation_is_empty_string() -> None:
    assert round_status.longest_reply({}) == ""
    assert round_status.longest_reply(_conv(_msg("1", "user", text="hi"))) == ""


def test_orphan_transcript_carries_the_orchestrators_field_set() -> None:
    conv = _conv(
        _msg("1", "user", text="please answer"),
        _msg(
            "2",
            "assistant",
            text="the answer",
            metadata={"resolved_model_slug": "gpt-5-6-thinking"},
        ),
        title="rp msgloom · screen",
    )
    entry = _entry("screen", "chat-1", 1, "sent", problems=["retried once"], turns=3)
    doc = round_status.orphan_transcript(
        entry, conv, archived_at="2026-09-20T00:00:00+00:00"
    )
    assert doc == {
        "job": "screen",
        "round": 1,
        "conversation_id": "chat-1",
        "model": "gpt-5-6-thinking",
        "title": "rp msgloom · screen",
        "status": "collected",
        "problems": ["retried once"],
        "turns_sent": 3,
        "archived_at": "2026-09-20T00:00:00+00:00",
        "turns": [
            {
                "role": "user",
                "text": "please answer",
                "model": "",
                "create_time": None,
            },
            {
                "role": "assistant",
                "text": "the answer",
                "model": "",
                "create_time": None,
            },
        ],
    }


def test_a_turn_carries_the_same_four_keys_the_orchestrator_writes() -> None:
    """Checked against a real transcript on 2026-09-20: role, text, model and
    create_time. A reader must not be able to tell an orphan archive from a
    normal one by the shape of a turn."""
    conv = _conv(
        _msg(
            "1",
            "assistant",
            text="answered",
            metadata={"model_slug": "gpt-5-6-thinking"},
        )
    )
    doc = round_status.orphan_transcript(
        _entry("screen", "c", 1, "sent"), conv, archived_at="t"
    )
    assert sorted(doc["turns"][0]) == ["create_time", "model", "role", "text"]
    assert doc["turns"][0]["model"] == "gpt-5-6-thinking"


def test_orphan_transcript_model_is_empty_with_no_assistant_turn() -> None:
    conv = _conv(_msg("1", "user", text="hi"))
    entry = _entry("screen", "chat-1", 1, "sent")
    doc = round_status.orphan_transcript(entry, conv, archived_at="x")
    assert doc["model"] == ""
    assert doc["problems"] == []
    assert doc["turns_sent"] is None


def test_orphan_transcript_defaults_problems_when_the_ledger_has_none() -> None:
    conv = _conv(_msg("1", "assistant", text="ok"))
    entry = _entry("screen", "chat-1", 1, "sent")
    assert (
        round_status.orphan_transcript(entry, conv, archived_at="x")["problems"] == []
    )


def test_orphan_paths_names_the_transcript_and_reply_file(tmp_path: Path) -> None:
    transcript, reply = round_status.orphan_paths(tmp_path, 1, "screen")
    assert transcript == tmp_path / "chatgpt" / "transcripts" / "r1-screen.orphan.json"
    assert reply == tmp_path / "chatgpt" / "transcripts" / "r1-screen.orphan.reply.md"


# ---------------------------------------------------------------------------
# CLI: parse_collect_args
# ---------------------------------------------------------------------------


def test_parse_collect_args_help_exits_0_and_shows_the_new_flags(capsys: Any) -> None:
    with pytest.raises(SystemExit) as exc:
        round_status.parse_collect_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "--collect" in out
    assert "--apply" in out


def test_parse_collect_args_defaults_are_false(tmp_path: Path) -> None:
    args = round_status.parse_collect_args([str(tmp_path)])
    assert args.workdir == tmp_path.resolve()
    assert args.collect is False
    assert args.apply is False


def test_parse_collect_args_reads_both_flags(tmp_path: Path) -> None:
    args = round_status.parse_collect_args([str(tmp_path), "--collect", "--apply"])
    assert args.collect is True
    assert args.apply is True


def test_parse_args_return_contract_is_unaffected_by_the_new_flags(
    tmp_path: Path,
) -> None:
    """round_status.parse_args must stay "just the workdir" -- test_cli.py
    checks it returns exactly target.resolve(); this proves adding the new
    flags to the shared parser did not change that."""
    target = tmp_path / "topic"
    assert round_status.parse_args([str(target), "--collect"]) == target.resolve()


# ---------------------------------------------------------------------------
# orchestrator_running -- subprocess.run, monkeypatched
# ---------------------------------------------------------------------------


def test_orchestrator_running_true_when_pgrep_finds_a_match(monkeypatch: Any) -> None:
    _pgrep_finds_a_match(monkeypatch)
    assert round_status.orchestrator_running() is True


def test_orchestrator_running_false_when_pgrep_finds_nothing(monkeypatch: Any) -> None:
    _pgrep_finds_nothing(monkeypatch)
    assert round_status.orchestrator_running() is False


# ---------------------------------------------------------------------------
# write_ledger -- atomic, refused when the file changed underneath
# ---------------------------------------------------------------------------


def test_write_ledger_writes_atomically_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([{"a": 1}]))
    original = path.read_bytes()
    ok = round_status.write_ledger(path, [{"a": 1, "status": "collected"}], original)
    assert ok is True
    assert json.loads(path.read_text()) == [{"a": 1, "status": "collected"}]
    assert list(tmp_path.iterdir()) == [path]  # no leftover temp file


def test_write_ledger_refuses_when_the_file_changed_underneath(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([{"a": 1}]))
    original = path.read_bytes()
    path.write_text(json.dumps([{"a": 1}, {"b": 2}]))  # a concurrent writer
    changed = path.read_bytes()
    ok = round_status.write_ledger(path, [{"a": 1, "status": "collected"}], original)
    assert ok is False
    assert path.read_bytes() == changed


# ---------------------------------------------------------------------------
# main(..., collect=True) -- dry run
# ---------------------------------------------------------------------------


def test_collect_with_nothing_sent_reports_nothing_to_collect(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path)
    _write_ledger(work, [_entry("screen", "chat-1", 2, "done")])
    monkeypatch.setattr(round_status, "open_session", _boom_if_called)
    assert round_status.main(work, collect=True) == 0
    assert "nothing to collect" in capsys.readouterr().out


def test_collect_skips_a_non_dict_ledger_entry(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path)
    path = work / "chatgpt" / "conversations.json"
    path.write_text(json.dumps([None, "garbage"]))
    monkeypatch.setattr(round_status, "open_session", _boom_if_called)
    assert round_status.main(work, collect=True) == 0
    assert "0 entrie(s) stuck at status 'sent'" in capsys.readouterr().out


def test_collect_reports_resumable_and_adds_no_extra_fetch(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The pre-existing "sent but not collected" report already fetches a
    current-round entry once; --collect's own classification must not
    fetch it again -- SKILL.md says resumable means "do not touch it"."""
    work = _topic(tmp_path, current_round=2)
    _write_ledger(work, [_entry("screen", "chat-1", 2, "sent")])
    conv = _conv(_msg("1", "assistant", text="still going", status="in_progress"))
    session = _FakeSession({"chat-1": conv})
    _wire(monkeypatch, session)
    assert round_status.main(work, collect=True) == 0
    out = capsys.readouterr().out
    assert "[resumable]" in out
    assert "Re-run the orchestrator with --resume" in out
    assert session.calls.count("chat-1") == 1


def test_collect_apply_never_mutates_a_resumable_entry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    before = _entry("screen", "chat-1", 2, "sent")
    _write_ledger(work, [before])
    conv = _conv(_msg("1", "assistant", text="still going", status="in_progress"))
    _wire(monkeypatch, _FakeSession({"chat-1": conv}))
    _pgrep_finds_nothing(monkeypatch)
    assert round_status.main(work, collect=True, apply=True) == 0
    [after] = _ledger(work)
    assert after == before


def test_collect_reports_superseded_and_names_the_transcript(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    sent = _entry("screen", "chat-new", 1, "sent")
    done = _entry(
        "screen", "chat-old", 1, "done", transcript="/topic/chatgpt/r1-screen.json"
    )
    _write_ledger(work, [sent, done])
    session = _FakeSession({})
    _wire(monkeypatch, session)
    assert round_status.main(work, collect=True) == 0
    out = capsys.readouterr().out
    assert "[superseded]" in out
    assert "/topic/chatgpt/r1-screen.json" in out
    assert session.calls == []  # a superseded entry is never fetched


def test_collect_reports_superseded_without_a_recorded_transcript(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    sent = _entry("screen", "chat-new", 1, "sent")
    done = _entry("screen", "chat-old", 1, "done")
    _write_ledger(work, [sent, done])
    _wire(monkeypatch, _FakeSession({}))
    assert round_status.main(work, collect=True) == 0
    out = capsys.readouterr().out
    assert "has no recorded transcript" in out


def test_collect_reports_lost_when_the_conversation_does_not_read(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    _write_ledger(work, [_entry("screen", "chat-1", 1, "sent")])
    session = _FakeSession({}, unreadable=frozenset({"chat-1"}))
    _wire(monkeypatch, session)
    assert round_status.main(work, collect=True) == 0
    out = capsys.readouterr().out
    assert "[lost]" in out
    assert "Nothing to recover" in out
    assert "404" in out


def test_collect_reports_collectable_when_the_conversation_still_reads(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    _write_ledger(work, [_entry("screen", "chat-1", 1, "sent")])
    conv = _conv(_msg("1", "assistant", text="the reply"))
    _wire(monkeypatch, _FakeSession({"chat-1": conv}))
    assert round_status.main(work, collect=True) == 0
    out = capsys.readouterr().out
    assert "[collectable]" in out
    assert "r1-screen.orphan.json" in out
    assert "whole round re-opened" in out


def test_collect_without_apply_changes_nothing_on_disk(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Dry run by default: --collect alone prints the verdicts and changes
    nothing -- compared by raw bytes, not just by re-parsing the JSON."""
    work = _topic(tmp_path, current_round=3)
    entries = [
        _entry("resume", "chat-r", 3, "sent"),
        _entry("super-new", "chat-s1", 1, "sent"),
        _entry("super-new", "chat-s0", 1, "done"),
        _entry("lost-job", "chat-l", 1, "sent"),
        _entry("collect-job", "chat-c", 1, "sent"),
    ]
    ledger_path = _write_ledger(work, entries)
    before = ledger_path.read_bytes()
    conv = _conv(_msg("1", "assistant", text="reply"))
    session = _FakeSession(
        {"chat-r": conv, "chat-c": conv}, unreadable=frozenset({"chat-l"})
    )
    _wire(monkeypatch, session)
    _pgrep_must_not_run(monkeypatch)  # a dry run must never even check
    transcripts_dir = work / "chatgpt" / "transcripts"

    assert round_status.main(work, collect=True, apply=False) == 0

    assert ledger_path.read_bytes() == before
    assert not transcripts_dir.exists()


# ---------------------------------------------------------------------------
# main(..., collect=True, apply=True) -- the write path
# ---------------------------------------------------------------------------


def test_collect_apply_archives_a_collectable_entry_and_updates_the_ledger(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    sent = _entry("screen", "chat-1", 1, "sent", problems=["retried once"], turns=2)
    _write_ledger(work, [sent])
    conv = _conv(
        _msg("1", "user", text="please answer"),
        _msg("2", "assistant", text="short"),
        _msg("3", "assistant", text="the real, much longer final answer"),
        title="rp msgloom · screen",
    )
    session = _FakeSession({"chat-1": conv})
    _wire(monkeypatch, session)
    _pgrep_finds_nothing(monkeypatch)

    assert round_status.main(work, collect=True, apply=True) == 0

    transcript_path = work / "chatgpt" / "transcripts" / "r1-screen.orphan.json"
    reply_path = work / "chatgpt" / "transcripts" / "r1-screen.orphan.reply.md"
    doc = json.loads(transcript_path.read_text())
    assert doc["job"] == "screen"
    assert doc["round"] == 1
    assert doc["conversation_id"] == "chat-1"
    assert doc["status"] == "collected"
    assert doc["problems"] == ["retried once"]
    assert doc["turns_sent"] == 2
    assert doc["title"] == "rp msgloom · screen"
    assert len(doc["turns"]) == 3
    assert reply_path.read_text() == "the real, much longer final answer"

    [after] = _ledger(work)
    assert after["status"] == "collected"
    assert after["orphan_transcript"] == str(transcript_path)
    assert "collected_at" in after
    assert "Collected: wrote" in capsys.readouterr().out


def test_collect_apply_marks_superseded_and_lost_in_the_ledger(
    tmp_path: Path, monkeypatch: Any
) -> None:
    work = _topic(tmp_path, current_round=3)
    superseded = _entry("screen", "chat-s1", 1, "sent")
    twin = _entry("screen", "chat-s0", 1, "done")
    lost = _entry("terminology", "chat-l", 1, "sent")
    _write_ledger(work, [superseded, twin, lost])
    session = _FakeSession({}, unreadable=frozenset({"chat-l"}))
    _wire(monkeypatch, session)
    _pgrep_finds_nothing(monkeypatch)

    assert round_status.main(work, collect=True, apply=True) == 0

    by_chat = {e["chat"]: e for e in _ledger(work)}
    assert by_chat["chat-s1"]["status"] == "superseded"
    assert "classified_at" in by_chat["chat-s1"]
    assert by_chat["chat-l"]["status"] == "lost"
    assert "classified_at" in by_chat["chat-l"]
    assert by_chat["chat-s0"] == twin  # the successful twin is untouched


def test_collect_apply_is_refused_while_the_orchestrator_is_running(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path)
    ledger_path = _write_ledger(work, [_entry("screen", "chat-1", 1, "sent")])
    before = ledger_path.read_bytes()
    _pgrep_finds_a_match(monkeypatch)
    monkeypatch.setattr(round_status, "open_session", _boom_if_called)

    assert round_status.main(work, collect=True, apply=True) == 2

    out = capsys.readouterr().out
    assert "refusing to write" in out
    assert "chatgpt_research.py is running" in out
    assert ledger_path.read_bytes() == before


def test_collect_apply_fetch_failure_during_archiving_exits_1(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The classification read can succeed and the archiving read moments
    later can still fail; the entry must be left at "sent" for a retry."""
    work = _topic(tmp_path, current_round=2)
    sent = _entry("screen", "chat-1", 1, "sent")
    _write_ledger(work, [sent])
    conv = _conv(_msg("1", "assistant", text="ok"))

    class _FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def get_conversation(self, chat_id: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return conv
            raise TransportError("GET conversation/chat-1 -> HTTP 500", 500)

    _wire(monkeypatch, _FlakySession())
    _pgrep_finds_nothing(monkeypatch)

    assert round_status.main(work, collect=True, apply=True) == 1

    out = capsys.readouterr().out
    assert "FAILED to fetch the conversation for archiving" in out
    [after] = _ledger(work)
    assert after["status"] == "sent"  # left alone for a retry


def test_collect_apply_write_failure_during_archiving_exits_1(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    sent = _entry("screen", "chat-1", 1, "sent")
    _write_ledger(work, [sent])
    # a plain file where the transcripts directory should go: mkdir(parents
    # =True, exist_ok=True) then raises, a real, not simulated, OSError.
    (work / "chatgpt" / "transcripts").write_text("not a directory")
    conv = _conv(_msg("1", "assistant", text="ok"))
    _wire(monkeypatch, _FakeSession({"chat-1": conv}))
    _pgrep_finds_nothing(monkeypatch)

    assert round_status.main(work, collect=True, apply=True) == 1

    out = capsys.readouterr().out
    assert "FAILED to write the archive" in out
    [after] = _ledger(work)
    assert after["status"] == "sent"


def test_collect_apply_refused_when_the_ledger_changed_since_it_was_read(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """A concurrent writer between this process's own read and its write
    must never be overwritten -- the narrower guard on top of the
    orchestrator_running() refusal, for the gap while a fetch was in
    flight."""
    work = _topic(tmp_path, current_round=2)
    ledger_path = _write_ledger(work, [_entry("screen", "chat-1", 1, "sent")])

    class _RacingSession:
        def get_conversation(self, chat_id: str) -> dict:
            # simulate another process touching the ledger while this
            # fetch was in flight
            ledger_path.write_text(json.dumps([{"raced": True}]))
            raise TransportError("GET conversation/chat-1 -> HTTP 404", 404)

    _wire(monkeypatch, _RacingSession())
    _pgrep_finds_nothing(monkeypatch)

    assert round_status.main(work, collect=True, apply=True) == 2

    out = capsys.readouterr().out
    assert "refusing to write" in out
    assert "changed on disk" in out
    assert json.loads(ledger_path.read_text()) == [{"raced": True}]


def test_collect_apply_ledger_write_error_is_reported_and_exits_1(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    work = _topic(tmp_path, current_round=2)
    _write_ledger(work, [_entry("screen", "chat-1", 1, "sent")])
    _wire(monkeypatch, _FakeSession({}, unreadable=frozenset({"chat-1"})))
    _pgrep_finds_nothing(monkeypatch)

    def _boom(*_a: Any, **_k: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(round_status, "write_ledger", _boom)

    assert round_status.main(work, collect=True, apply=True) == 1
    assert "FAILED to write the ledger" in capsys.readouterr().out
