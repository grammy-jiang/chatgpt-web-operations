"""Tests for the chatgpt-web-operations commands.

Each command is written as pure functions over data plus a thin ``main`` that
supplies the live session, so everything that decides an outcome is tested
here without a network or a browser.

The point of these tests is that the commands keep working when someone edits
them a year from now, in a session that has forgotten why each rule exists.
Every test therefore names the failure it is defending against.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    """Import a command module by path, with its siblings importable."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


common = _load("_common")
list_chats = _load("list_chats")
read_chat = _load("read_chat")
clean_chats = _load("clean_chats")
probe_send_gates = _load("probe_send_gates")
probe_cookies = _load("probe_cookies")
discover_endpoints = _load("discover_endpoints")


# ---------------------------------------------------------------------------
# _common
# ---------------------------------------------------------------------------


def test_a_table_aligns_to_its_widest_cell() -> None:
    text = common.table([("a", "long value"), ("bbbb", "x")], ("id", "value"))
    lines = text.splitlines()
    assert lines[0].startswith("id")
    assert len({len(line.rstrip()) > 0 for line in lines}) == 1


def test_an_empty_table_says_so_rather_than_printing_headers() -> None:
    assert common.table([], ("id", "value")) == "(nothing)"


def test_shorten_collapses_whitespace_and_bounds_length() -> None:
    assert common.shorten("a   b\n\nc") == "a b c"
    assert len(common.shorten("x" * 200, 20)) == 20


# ---------------------------------------------------------------------------
# list_chats
# ---------------------------------------------------------------------------


CHATS = [
    {"id": "aaa", "title": "rp msgloom-topic-04 · terminology", "update_time": "t1"},
    {"id": "bbb", "title": "Holiday planning", "update_time": "t2"},
    {"id": "ccc", "title": "rp msgloom-topic-04 · screen", "update_time": "t3"},
]


def test_listing_without_a_filter_returns_every_chat() -> None:
    assert len(list_chats.rows_for(CHATS)) == 3


def test_listing_filters_on_the_title_case_insensitively() -> None:
    rows = list_chats.rows_for(CHATS, "RP MSGLOOM")
    assert [r[0] for r in rows] == ["aaa", "ccc"]


def test_a_chat_without_a_title_is_still_listed() -> None:
    rows = list_chats.rows_for([{"id": "ddd"}])
    assert rows[0][1] == "(untitled)"


# ---------------------------------------------------------------------------
# clean_chats — the one command that can destroy something
# ---------------------------------------------------------------------------


def test_an_empty_match_selects_nothing() -> None:
    """A bare run must never be able to clear an account."""
    assert clean_chats.select(CHATS, "") == []
    assert clean_chats.select(CHATS, "   ") == []


def test_a_match_selects_only_the_titles_that_contain_it() -> None:
    chosen = clean_chats.select(CHATS, "rp msgloom")
    assert [c["id"] for c in chosen] == ["aaa", "ccc"]
    assert all("Holiday" not in c["title"] for c in chosen)


def test_the_user_s_own_chats_are_not_swept_up_by_a_worker_match() -> None:
    """Workers share the account with the user; picking wrong is the risk."""
    assert clean_chats.select(CHATS, "holiday")[0]["id"] == "bbb"


def test_deleting_needs_apply(monkeypatch, capsys) -> None:
    """Without --apply nothing is touched, whatever else is passed."""
    touched: list[str] = []

    class _Session:
        def list_conversations(self, limit: int = 0) -> list[dict]:
            return CHATS

        def delete(self, chat: str) -> None:
            touched.append(chat)

        def archive(self, chat: str) -> None:
            touched.append(chat)

    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: _Session())
    assert clean_chats.main(["--match", "rp msgloom", "--delete"]) == 0
    assert touched == []
    assert "dry run" in capsys.readouterr().out


def test_apply_deletes_exactly_the_selected_chats(monkeypatch) -> None:
    touched: list[str] = []

    class _Session:
        def list_conversations(self, limit: int = 0) -> list[dict]:
            return CHATS

        def delete(self, chat: str) -> None:
            touched.append(chat)

    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: _Session())
    assert clean_chats.main(["--match", "rp msgloom", "--delete", "--apply"]) == 0
    assert touched == ["aaa", "ccc"]


def test_asking_for_both_delete_and_archive_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(clean_chats, "open_session", lambda *a, **k: None)
    assert clean_chats.main(["--match", "x", "--delete", "--archive"]) == 2
    assert clean_chats.main(["--match", "x"]) == 2


# ---------------------------------------------------------------------------
# probe_send_gates
# ---------------------------------------------------------------------------


def test_every_required_gate_is_reported() -> None:
    gates = probe_send_gates.gates_from(
        {
            "proofofwork": {"required": True, "seed": "s"},
            "turnstile": {"required": True, "dx": "d"},
            "so": {"required": True, "collector_dx": "c"},
            "persona": "chatgpt-paid",
        }
    )
    assert gates == {"proofofwork": True, "turnstile": True, "so": True}


def test_a_gate_that_is_present_but_not_required_is_reported_as_off() -> None:
    assert probe_send_gates.gates_from({"turnstile": {"required": False}}) == {
        "turnstile": False
    }


def test_a_gate_absent_from_the_response_is_not_invented() -> None:
    assert probe_send_gates.gates_from({"persona": "free"}) == {}


def test_a_new_gate_name_is_ignored_rather_than_silently_passing() -> None:
    """Unknown keys must not read as "no gates"; the known list is explicit."""
    gates = probe_send_gates.gates_from({"brandnewgate": {"required": True}})
    assert gates == {}, "add the name to BROWSER_ONLY when ChatGPT adds a gate"


# ---------------------------------------------------------------------------
# probe_cookies — must never print a value
# ---------------------------------------------------------------------------


def test_cookie_values_are_never_returned_only_their_lengths() -> None:
    """A secret in a log is worse than the bug this command diagnoses."""
    rows = [("__Secure-next-auth.session-token.0", "chatgpt.com", b"enc")]
    good, bad = probe_cookies.classify(rows, lambda _enc: "super-secret-value")
    assert not bad
    assert "super-secret-value" not in str(good)
    assert good[0][2] == "18 chars"


def test_an_unreadable_cookie_is_reported_not_raised() -> None:
    def boom(_enc: bytes) -> str:
        raise ValueError("bad padding")

    good, bad = probe_cookies.classify([("_dd_s", "chatgpt.com", b"x")], boom)
    assert good == []
    assert bad == [("_dd_s", "chatgpt.com", "ValueError")]


def test_one_bad_cookie_does_not_hide_the_good_ones() -> None:
    """The whole point: an analytics cookie must not mask a working session."""

    def picky(enc: bytes) -> str:
        if enc == b"bad":
            raise ValueError("nope")
        return "ok"

    rows = [
        ("_dd_s", "chatgpt.com", b"bad"),
        ("__Secure-next-auth.session-token.0", "chatgpt.com", b"good"),
    ]
    good, bad = picky and probe_cookies.classify(rows, picky)
    assert [g[0] for g in good] == ["__Secure-next-auth.session-token.0"]
    assert [b[0] for b in bad] == ["_dd_s"]


# ---------------------------------------------------------------------------
# discover_endpoints
# ---------------------------------------------------------------------------


def test_query_strings_are_stripped_so_one_endpoint_is_one_row() -> None:
    rows = discover_endpoints.summarise(
        [
            ("GET", "https://chatgpt.com/backend-api/conversations?offset=0&limit=28"),
            ("GET", "https://chatgpt.com/backend-api/conversations?offset=28&limit=28"),
        ]
    )
    assert rows == [("GET", "https://chatgpt.com/backend-api/conversations", "2")]


def test_conversation_ids_collapse_so_traffic_does_not_swamp_the_report() -> None:
    rows = discover_endpoints.summarise(
        [
            (
                "GET",
                "https://chatgpt.com/backend-api/conversation/6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8",
            ),
            (
                "GET",
                "https://chatgpt.com/backend-api/conversation/6aaa139a-b7fc-83ec-841b-14931c7f7f56",
            ),
        ]
    )
    assert rows == [("GET", "https://chatgpt.com/backend-api/conversation/<id>", "2")]


def test_the_busiest_endpoint_is_listed_first() -> None:
    rows = discover_endpoints.summarise(
        [("GET", "https://a/x")] * 3 + [("POST", "https://a/y")]
    )
    assert rows[0][1].endswith("/x")


def test_method_distinguishes_two_uses_of_one_path() -> None:
    rows = discover_endpoints.summarise(
        [("GET", "https://a/conversation/x"), ("PATCH", "https://a/conversation/x")]
    )
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# read_chat
# ---------------------------------------------------------------------------


class _FakeClient:
    """The three client helpers read_chat uses, over a simple message list."""

    def __init__(self, messages: list[dict], final: bool) -> None:
        self.messages = messages
        self.final = final

    def tail_signature(self, _conv: dict) -> tuple[str, int, str, bool]:
        return ("node", len(self.messages), "text", self.final)

    def assistant_text_messages(self, _conv: dict) -> list[dict]:
        return [m for m in self.messages if m["author"]["role"] == "assistant"]

    def chain(self, _conv: dict) -> list[dict]:
        return self.messages

    def message_text(self, msg: dict) -> str:
        return msg.get("text", "")


def _msg(role: str, text: str = "") -> dict:
    return {
        "author": {"role": role},
        "content": {"content_type": "text"},
        "recipient": "all",
        "status": "finished_successfully",
        "text": text,
    }


def test_a_finished_turn_is_reported_as_ready_to_collect() -> None:
    """This is the check that stops a 30-minute wait for work already done."""
    client = _FakeClient([_msg("user"), _msg("assistant", "done")], final=True)
    state = read_chat.turn_state(client, {"title": "t"})
    assert state["finished"] is True
    assert state["visible_replies"] == 1


def test_a_turn_still_running_is_not_reported_as_ready() -> None:
    client = _FakeClient([_msg("user"), _msg("assistant", "partial")], final=False)
    assert read_chat.turn_state(client, {})["finished"] is False


def test_a_final_tail_with_no_visible_reply_is_not_ready() -> None:
    """Tool traffic can end a turn without anything addressed to the user."""
    client = _FakeClient([_msg("user")], final=True)
    assert read_chat.turn_state(client, {})["finished"] is False


def test_the_message_table_shows_the_last_n_messages() -> None:
    client = _FakeClient([_msg("user", f"m{i}") for i in range(10)], final=True)
    rows = read_chat.message_rows(client, {}, tail=3)
    assert len(rows) == 3
    assert rows[-1][4] == "m9"


@pytest.mark.parametrize("missing", ["author", "content"])
def test_a_malformed_message_does_not_crash_the_table(missing: str) -> None:
    """A half-written message arrives mid-stream; the report must survive it."""
    msg = _msg("user", "x")
    del msg[missing]
    rows = read_chat.message_rows(_FakeClient([msg], final=False), {}, tail=5)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# round_status — an empty corpus is not a pass
# ---------------------------------------------------------------------------

round_status = _load("round_status")


def _workdir(
    tmp_path: Path, admitted: list[str], analysed: list[str], round_no: int = 1
) -> Path:
    work = tmp_path / "work"
    run = work / "runs" / "abc123"
    (run / "screen").mkdir(parents=True)
    (run / "analysis").mkdir(parents=True)
    (work / "chatgpt").mkdir(parents=True)
    (work / "workflow_state.json").write_text(
        json.dumps({"run_id": "abc123", "round": round_no, "max_rounds": 4})
    )
    (work / "chatgpt" / "conversations.json").write_text("[]")
    (run / "screen" / "screened.jsonl").write_text(
        "".join(json.dumps({"paper_id": p}) + "\n" for p in admitted)
    )
    for pid in analysed:
        (run / "analysis" / f"{pid}_analysis.json").write_text("{}")
    return work


def test_a_round_before_the_admission_gate_is_not_called_complete(
    tmp_path: Path, capsys
) -> None:
    """0 admitted minus 0 read is an empty set, which is not a pass."""
    assert round_status.main(_workdir(tmp_path, [], [], round_no=3)) == 0
    out = capsys.readouterr().out
    assert "admitted nothing yet" in out
    assert "Every one of" not in out


def test_a_fully_read_round_says_how_many_it_accounted_for(
    tmp_path: Path, capsys
) -> None:
    assert round_status.main(_workdir(tmp_path, ["a", "b"], ["a", "b"])) == 0
    assert "Every one of the 2 admitted papers" in capsys.readouterr().out


def test_an_unread_paper_fails_the_gate(tmp_path: Path, capsys) -> None:
    """Non-zero exit is what makes this usable as a gate before a report."""
    assert round_status.main(_workdir(tmp_path, ["a", "b"], ["a"])) == 1
    assert "NOT read everything" in capsys.readouterr().out


def test_a_written_off_paper_settles_the_gate(tmp_path: Path) -> None:
    work = _workdir(tmp_path, ["a", "b"], ["a"])
    (work / "runs" / "abc123" / "analysis" / "skipped.json").write_text(
        json.dumps([{"paper_id": "b", "reason": "no retrievable text"}])
    )
    assert round_status.main(work) == 0


# ---------------------------------------------------------------------------
# list_projects — projects group chats and keep them out of the main list
# ---------------------------------------------------------------------------

list_projects = _load("list_projects")


def _sidebar(*names: str) -> dict:
    """A snorlax sidebar payload shaped like the real one."""
    return {
        "items": [
            {
                "gizmo": {
                    "id": f"g-p-{i:032x}",
                    "short_url": f"g-p-{i:032x}-{name.lower().replace(' ', '-')}",
                    "display": {"name": name},
                    "instructions": f"rules for {name}",
                    "updated_at": "2026-09-07T06:50:58.252004+00:00",
                },
                "files": [],
            }
            for i, name in enumerate(names)
        ]
    }


def test_projects_are_flattened_out_of_the_sidebar_wrapper() -> None:
    found = list_projects.projects_from(_sidebar("Raspberry Pi 5", "LLM Learning"))
    assert [p["name"] for p in found] == ["Raspberry Pi 5", "LLM Learning"]
    assert found[0]["id"].startswith("g-p-")
    assert found[0]["instructions"] == "rules for Raspberry Pi 5"


def test_projects_filter_on_the_name_case_insensitively() -> None:
    found = list_projects.projects_from(
        _sidebar("Raspberry Pi 5", "LLM Learning"), "llm"
    )
    assert [p["name"] for p in found] == ["LLM Learning"]


def test_an_unnamed_project_is_still_listed() -> None:
    sidebar = {"items": [{"gizmo": {"id": "g-p-x"}}]}
    assert list_projects.projects_from(sidebar)[0]["name"] == "(unnamed)"


def test_an_empty_sidebar_yields_no_projects() -> None:
    assert list_projects.projects_from({}) == []
    assert list_projects.projects_from({"items": []}) == []


def test_the_project_url_is_where_composing_creates_a_chat_inside_it() -> None:
    """This is the point: a worker started here never reaches the main list."""
    project = list_projects.projects_from(_sidebar("Raspberry Pi 5"))[0]
    url = list_projects.project_url(project)
    assert url.startswith("https://chatgpt.com/g/")
    assert url.endswith("/project")
    assert "raspberry-pi-5" in url


def test_a_project_without_a_slug_still_yields_a_usable_url() -> None:
    """short_url is absent on a freshly created project."""
    project = {"id": "g-p-abc", "short_url": "", "name": "n", "instructions": ""}
    assert list_projects.project_url(project) == "https://chatgpt.com/g/g-p-abc/project"


# ---------------------------------------------------------------------------
# create_project — the capture is the evidence, and a rate limit means stop
# ---------------------------------------------------------------------------

create_project = _load("create_project")


def _call(method: str, url: str, status: int = 200, response: str = "") -> dict:
    return {"method": method, "url": url, "status": status, "response": response}


def test_only_non_get_backend_calls_are_treated_as_mutations() -> None:
    calls = [
        _call("GET", "https://chatgpt.com/backend-api/conversations"),
        _call("POST", "https://chatgpt.com/backend-api/gizmos"),
        _call("POST", "https://cdn.example.com/telemetry"),
    ]
    assert [c["url"] for c in create_project.mutations(calls)] == [
        "https://chatgpt.com/backend-api/gizmos"
    ]


def test_the_creating_call_is_the_one_that_returned_a_project_id() -> None:
    """Identify it by the id it returned, not by an endpoint name we guessed."""
    calls = [
        _call("POST", "https://chatgpt.com/backend-api/a", 200, '{"ok": true}'),
        _call(
            "POST",
            "https://chatgpt.com/backend-api/b",
            200,
            '{"gizmo": {"id": "g-p-abc123"}}',
        ),
    ]
    found = create_project.created_project(calls)
    assert found is not None
    assert found["url"].endswith("/backend-api/b")


def test_a_failed_call_that_mentions_a_project_id_is_not_the_creator() -> None:
    calls = [_call("POST", "https://chatgpt.com/backend-api/b", 500, "g-p-abc")]
    assert create_project.created_project(calls) is None


def test_no_creating_call_is_reported_as_none_not_guessed() -> None:
    calls = [_call("POST", "https://chatgpt.com/backend-api/a", 200, '{"ok": 1}')]
    assert create_project.created_project(calls) is None


def test_the_rate_limit_modal_is_recognised_by_its_test_id() -> None:
    """The selector that blocked a real run; it means stop, not retry harder."""
    assert "modal-conversation-history-rate-limit" in create_project.RATE_LIMIT_MODAL


def test_the_new_project_control_is_found_by_its_aria_label() -> None:
    """Discovered from a live page; the visible wording has changed before."""
    assert create_project.NEW_PROJECT == 'button[aria-label="New project"]'


# ---------------------------------------------------------------------------
# review_topic.py -- was a finished topic done properly?
# ---------------------------------------------------------------------------

rt = _load("review_topic")


def _round(
    run: Path,
    *,
    rnd: int,
    candidates: int,
    scored: int,
    admit: list[str],
    reject: int = 0,
    shortlist: int | None = None,
    corpus: list[str],
    analysed: list[str] | None = None,
    skipped: list[tuple[str, str]] | None = None,
) -> None:
    """Write the artifacts one round of the pipeline leaves behind."""
    (run / "search").mkdir(parents=True, exist_ok=True)
    (run / "screen").mkdir(parents=True, exist_ok=True)
    (run / "analysis").mkdir(parents=True, exist_ok=True)
    (run / "round_context.json").write_text(json.dumps({"round": rnd}))
    (run / "search" / "candidates.jsonl").write_text(
        "".join(json.dumps({"id": f"c{i}"}) + "\n" for i in range(candidates))
    )
    (run / "screen" / "cheap_scores.jsonl").write_text(
        "".join(json.dumps({"id": f"c{i}"}) + "\n" for i in range(scored))
    )
    # Everything shortlisted must be judged, so unless a test is exercising
    # that invariant the shortlist is exactly what admission decided on.
    size = len(admit) + reject if shortlist is None else shortlist
    (run / "screen" / "shortlist.json").write_text(
        json.dumps([{"id": f"s{i}"} for i in range(size)])
    )
    decisions = [{"paper_id": p, "decision": "ADMIT"} for p in admit]
    decisions += [{"paper_id": f"r{i}", "decision": "REJECT"} for i in range(reject)]
    (run / "screen" / "admission.json").write_text(json.dumps({"decisions": decisions}))
    (run / "screen" / "screened.jsonl").write_text(
        "".join(json.dumps({"id": p}) + "\n" for p in corpus)
    )
    for paper in analysed if analysed is not None else corpus:
        (run / "analysis" / f"{paper}_analysis.json").write_text("{}")
    if skipped:
        (run / "analysis" / "skipped.json").write_text(
            json.dumps([{"paper_id": p, "reason": why} for p, why in skipped])
        )


def test_a_clean_round_reports_nothing(tmp_path: Path, capsys) -> None:
    run = tmp_path / "runs" / "r1"
    _round(
        run,
        rnd=1,
        candidates=53,
        scored=53,
        admit=["p1", "p2"],
        reject=23,
        corpus=["p1", "p2"],
    )
    assert rt.main([str(tmp_path)]) == 0
    assert "no integrity problem found" in capsys.readouterr().out


def test_an_unread_paper_is_caught(tmp_path: Path, capsys) -> None:
    """The failure the command exists for.

    msgloom Topic 04 round 2 admitted 29 papers, read 13, and then
    synthesised, reviewed and reported as though nothing were missing.
    """
    run = tmp_path / "runs" / "r1"
    _round(
        run,
        rnd=1,
        candidates=10,
        scored=10,
        admit=["p1", "p2", "p3"],
        corpus=["p1", "p2", "p3"],
        analysed=["p1"],
    )
    assert rt.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "!!" in out
    assert "p2" in out and "p3" in out


def test_a_paper_readmitted_in_a_later_round_is_not_a_loss(
    tmp_path: Path, capsys
) -> None:
    """Counting says Topic 08 round 2 lost a paper. Identifiers say otherwise.

    It admitted 15 and the corpus grew by 14, because one admitted paper was
    already in the corpus from round 1. A check that calls ordinary dedup a
    loss is a check nobody reads.
    """
    _round(
        tmp_path / "runs" / "r1",
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1", "p2"],
        corpus=["p1", "p2"],
    )
    _round(
        tmp_path / "runs" / "r2",
        rnd=2,
        candidates=9,
        scored=9,
        admit=["p2", "p3"],  # p2 is already in the corpus
        corpus=["p1", "p2", "p3"],
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 already in the corpus" in out


def test_a_written_off_paper_settles_but_stays_visible(tmp_path: Path, capsys) -> None:
    """analysis/skipped.json is how the system says "this cannot be read".

    Demanding an analysis that will never arrive blocks the round for ever,
    but hiding the write-off would let a topic claim it read everything.
    """
    run = tmp_path / "runs" / "r1"
    _round(
        run,
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1", "p2"],
        corpus=["p1", "p2"],
        analysed=["p1"],
        skipped=[("p2", "no bibliographic title in the shard")],
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "written off: p2" in out
    assert "no bibliographic title" in out


def test_a_round_still_running_is_skipped_not_flagged(tmp_path: Path, capsys) -> None:
    """A round in its search steps has nothing to account for yet.

    Reporting it as "the corpus shrank by 14" is how a check teaches its
    reader to ignore it.
    """
    _round(
        tmp_path / "runs" / "r1",
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1"],
        corpus=["p1"],
    )
    started = tmp_path / "runs" / "r2"
    (started / "search").mkdir(parents=True)
    (started / "round_context.json").write_text(json.dumps({"round": 2}))
    assert rt.main([str(tmp_path)]) == 0
    assert "still running, skipped" in capsys.readouterr().out


def test_a_sent_conversation_is_not_a_failure(tmp_path: Path, capsys) -> None:
    """ "sent" means the reply was not collected here, which is not "failed".

    Topic 06's search-llm stayed at "sent" because a host crash meant the
    reply was harvested by hand instead of by the orchestrator.
    """
    _round(
        tmp_path / "runs" / "r1",
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1"],
        corpus=["p1"],
    )
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "job": "search-llm",
                    "round": 1,
                    "status": "sent",
                    "sent_at": "2026-09-17T00:03:57+00:00",
                },
                {
                    "job": "terminology",
                    "round": 1,
                    "status": "done",
                    "sent_at": "2026-09-17T00:00:00+00:00",
                },
            ]
        )
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "reply not collected here" in out
    assert "failed" not in out


def test_a_failed_conversation_is_reported(tmp_path: Path, capsys) -> None:
    """Topic 05 round 2 lost a terminology worker to a rate limit."""
    _round(
        tmp_path / "runs" / "r1",
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1"],
        corpus=["p1"],
    )
    chat = tmp_path / "chatgpt"
    chat.mkdir()
    (chat / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "job": "terminology",
                    "round": 1,
                    "status": "failed",
                    "sent_at": "2026-09-16T10:12:29+00:00",
                },
                {
                    "job": "search-llm",
                    "round": 1,
                    "status": "done",
                    "sent_at": "2026-09-16T10:20:00+00:00",
                },
            ]
        )
    )
    assert rt.main([str(tmp_path)]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_rounds_are_ordered_by_round_not_by_directory_name(
    tmp_path: Path, capsys
) -> None:
    """Run directories are random hex; sorting them is not sorting rounds.

    Ordering by name made the corpus appear to shrink between rounds, which
    broke every growth check downstream.
    """
    _round(
        tmp_path / "runs" / "zzz",
        rnd=1,
        candidates=9,
        scored=9,
        admit=["p1"],
        corpus=["p1"],
    )
    _round(
        tmp_path / "runs" / "aaa",
        rnd=2,
        candidates=9,
        scored=9,
        admit=["p2"],
        corpus=["p1", "p2"],
    )
    assert rt.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.index("round 1") < out.index("round 2")


def test_two_records_of_one_study_are_caught(tmp_path: Path, capsys) -> None:
    """Identifier checks are blind to this; the ids genuinely differ.

    msgloom Topic 08 was rejected by the independent reviewer for citing
    "Scene-Text Oriented Referring Expression Comprehension" twice as
    independent corroboration, once as a bare manual- row and once with its
    DOI. A scan then found two more pairs in Topic 04, already closed and
    reported as complete with 47 papers, when it has 45.
    """
    run = tmp_path / "runs" / "r1"
    _round(
        run,
        rnd=1,
        candidates=9,
        scored=9,
        admit=["doi-10-1", "manual-abc"],
        corpus=["doi-10-1", "manual-abc"],
    )
    # Same study, two ids: overwrite the corpus rows with matching titles.
    (run / "screen" / "screened.jsonl").write_text(
        json.dumps({"id": "doi-10-1", "title": "Scene-Text Oriented REC"})
        + "\n"
        + json.dumps({"id": "manual-abc", "title": "scene-text   oriented rec"})
        + "\n"
    )
    assert rt.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "1 distinct of 2" in out
    assert "same study:" in out


def test_distinct_papers_are_not_merged(tmp_path: Path, capsys) -> None:
    """Normalisation is case and whitespace only, on purpose.

    Anything more aggressive starts collapsing papers that genuinely differ,
    and a review that hides real evidence is worse than one that miscounts.
    """
    run = tmp_path / "runs" / "r1"
    _round(
        run,
        rnd=1,
        candidates=9,
        scored=9,
        admit=["a", "b"],
        corpus=["a", "b"],
    )
    (run / "screen" / "screened.jsonl").write_text(
        json.dumps({"id": "a", "title": "Attention Is All You Need"})
        + "\n"
        + json.dumps({"id": "b", "title": "Attention Is All You Need II"})
        + "\n"
    )
    assert rt.main([str(tmp_path)]) == 0
    assert "2 distinct of 2" in capsys.readouterr().out


def test_a_row_without_a_title_is_not_a_duplicate(tmp_path: Path) -> None:
    """Empty titles must not all collapse into one imaginary study."""
    run = tmp_path / "runs" / "r1"
    _round(run, rnd=1, candidates=9, scored=9, admit=["a", "b"], corpus=["a", "b"])
    (run / "screen" / "screened.jsonl").write_text(
        json.dumps({"id": "a"}) + "\n" + json.dumps({"id": "b", "title": ""}) + "\n"
    )
    assert rt.main([str(tmp_path)]) == 0


# ---------------------------------------------------------------------------
# model_settings -- the Power slider is presets, not effort levels
# ---------------------------------------------------------------------------

model_settings = _load("model_settings")

MODELS_PAYLOAD = {
    "versions": [
        {
            "id": "latest",
            "intelligence_presets": [
                {"id": 0, "title": "Instant", "model_slug": "gpt-5-6-instant"},
                {
                    "id": 1,
                    "title": "Medium",
                    "model_slug": "gpt-5-6-thinking",
                    "thinking_effort": "standard",
                },
                {
                    "id": 6,
                    "title": "Extra High",
                    "model_slug": "gpt-5-6-thinking",
                    "thinking_effort": "max",
                },
                {"id": 3, "title": "Pro", "model_slug": "gpt-6-pro"},
            ],
        },
        {"id": "5.5", "intelligence_presets": [{"id": 0, "title": "Old"}]},
    ],
    "models": [
        {"slug": "gpt-5-6-instant", "configurable_thinking_effort": False},
        {
            "slug": "gpt-5-6-thinking",
            "configurable_thinking_effort": True,
            "thinking_efforts": [
                {"thinking_effort": "min"},
                {"thinking_effort": "standard"},
                {"thinking_effort": "extended"},
                {"thinking_effort": "max"},
            ],
        },
    ],
}


def test_presets_come_from_the_selected_version_in_slider_order() -> None:
    presets = model_settings.presets_of(MODELS_PAYLOAD)
    assert [p["title"] for p in presets] == ["Instant", "Medium", "Extra High", "Pro"]
    assert [p["position"] for p in presets] == [1, 2, 3, 4]
    assert presets[0]["effort"] == "" and presets[2]["effort"] == "max"


def test_only_models_with_a_choice_list_their_levels() -> None:
    levels = model_settings.effort_levels_of(MODELS_PAYLOAD)
    assert levels == {"gpt-5-6-thinking": ["min", "standard", "extended", "max"]}


def test_a_model_and_effort_pair_resolves_to_the_matching_preset() -> None:
    """Extra High on the slider is max on the API; the two must agree."""
    presets = model_settings.presets_of(MODELS_PAYLOAD)
    found = model_settings.resolve_preset(presets, "gpt-5-6-thinking", "max")
    assert found is not None and found["title"] == "Extra High"


def test_a_model_change_is_a_preset_of_its_own_without_an_effort() -> None:
    presets = model_settings.presets_of(MODELS_PAYLOAD)
    assert model_settings.resolve_preset(presets, "gpt-6-pro", "")["title"] == "Pro"
    assert model_settings.resolve_preset(presets, "gpt-5-6-thinking", "") is None


def test_an_unknown_pair_resolves_to_nothing_rather_than_a_guess() -> None:
    presets = model_settings.presets_of(MODELS_PAYLOAD)
    assert model_settings.resolve_preset(presets, "gpt-5-6-thinking", "ultra") is None


SETTINGS_PAYLOAD_FOR_MODEL_SETTINGS = {
    "settings": {
        "last_used_model_config": {
            "slugs": {"web": "gpt-5-6-thinking", "ios_app": "gpt-6-pro"},
            "juices": {
                "web": {"gpt-5-6-thinking": "max", "gpt-6-pro": "standard"},
                "ios_app": {"gpt-6-pro": "standard"},
            },
        }
    }
}


def test_server_config_reads_the_web_surface_slug_and_its_own_effort() -> None:
    """slugs[web] names the model; juices[web][<that model>] its effort --
    never juices[web] read as a whole, and never another surface."""
    assert model_settings.server_config(SETTINGS_PAYLOAD_FOR_MODEL_SETTINGS) == {
        "model": "gpt-5-6-thinking",
        "effort": "max",
    }


def test_server_config_is_empty_when_last_used_model_config_is_absent() -> None:
    assert model_settings.server_config({"settings": {}}) == {
        "model": "",
        "effort": "",
    }
    assert model_settings.server_config({}) == {"model": "", "effort": ""}


def test_server_config_effort_is_empty_when_the_web_slug_has_none_remembered() -> None:
    """The slug is known but juices[web] has no entry for it (e.g. Instant,
    which carries no effort of its own): effort reads as "", not KeyError."""
    settings = {
        "settings": {
            "last_used_model_config": {
                "slugs": {"web": "gpt-5-6-instant"},
                "juices": {"web": {}},
            }
        }
    }
    assert model_settings.server_config(settings) == {
        "model": "gpt-5-6-instant",
        "effort": "",
    }


# ---------------------------------------------------------------------------
# list_chats filters -- pinned is is_starred, project chats hide with snorlax
# ---------------------------------------------------------------------------


def test_the_default_query_sends_no_filters() -> None:
    assert list_chats.query_for(20) == (
        "/backend-api/conversations?offset=0&limit=20&order=updated"
    )


def test_each_view_adds_only_its_own_flag() -> None:
    assert list_chats.query_for(5, archived=True).endswith("&is_archived=true")
    assert list_chats.query_for(5, pinned=True).endswith("&is_starred=true")
    assert list_chats.query_for(5, hide_project_chats=True).endswith(
        "&hide_snorlax=true"
    )
    assert "is_archived" not in list_chats.query_for(5, pinned=True)


def test_flags_name_what_the_sidebar_shows() -> None:
    chat = {"gizmo_id": "g-p-x", "is_starred": True, "is_archived": False}
    assert list_chats.flags_of(chat) == "project pinned"
    assert list_chats.flags_of({"is_temporary_chat": True}) == "temporary"
    assert list_chats.flags_of({}) == ""


def test_rows_carry_the_flags_column() -> None:
    rows = list_chats.rows_for([{"id": "a", "title": "t", "is_archived": True}])
    assert rows[0][3] == "archived"


# ---------------------------------------------------------------------------
# profile_context -- the hidden inputs of a run, read before the first send
# ---------------------------------------------------------------------------

profile_context = _load("profile_context")

USER_SYSTEM_MESSAGES = {
    "object": "user_system_message_detail",
    "about_user_message": "I am a software engineer.",
    "about_model_message": "Please remain neutral.",
    "name_user_message": "Grammy",
    "role_user_message": "Software Engineer",
    "traits_model_message": "Please remain neutral.",
    "other_user_message": "I am a software engineer.",
    "personality_type_selection": "default",
    "disabled_tools": [],
    "enabled": True,
    "traits_enabled": True,
}

MEMORY_SUMMARY = {
    "memories": [],
    "memory_max_tokens": 5000000,
    "memory_num_tokens": 2407,
}

MEMORY_ENTRIES = {
    "memories": [
        {"id": "m1", "content": "Prefers Simplified Chinese.", "gizmo_id": None},
        {"id": "m2", "content": "Runs a Raspberry Pi 5.", "gizmo_id": "g-p-abc"},
    ],
    "memory_max_tokens": 5000000,
    "memory_num_tokens": 2407,
}

SETTINGS_PAYLOAD = {
    "settings": {
        "last_used_model_config": {
            "slugs": {"web": "gpt-5-6-thinking", "ios_app": "gpt-5-6-instant"},
            "juices": {
                "web": {"gpt-5-6-thinking": "max", "gpt-6-pro": "standard"},
                "ios_app": {},
            },
        },
        "default_model_config": {"default_model_slug": None},
        "model_sticky_for_new_chats": False,
        "model_picker_persists_ultra_effort": True,
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
    "files": [{"id": "file-1", "name": "paper.pdf", "size": 12, "nested": {"x": 1}}],
}


def test_custom_instructions_keep_the_text_and_whether_it_applies() -> None:
    ci = profile_context.custom_instructions_of(USER_SYSTEM_MESSAGES)
    assert ci["enabled"] is True
    assert ci["name"] == "Grammy"
    assert ci["about"] == "I am a software engineer."
    assert ci["traits"] == "Please remain neutral."


def test_memory_is_counted_but_its_content_is_never_kept() -> None:
    """The document travels with a run's archive; the user's memories do not."""
    mem = profile_context.memory_of(MEMORY_SUMMARY, MEMORY_ENTRIES)
    assert mem == {
        "tokens_used": 2407,
        "tokens_max": 5000000,
        "entries": 2,
        "entries_project_scoped": 1,
    }
    assert "Raspberry" not in json.dumps(mem)


def test_unread_memory_entries_are_none_not_zero() -> None:
    """A failed read must not pass for an empty memory."""
    mem = profile_context.memory_of(MEMORY_SUMMARY, {})
    assert mem["entries"] is None
    assert mem["tokens_used"] == 2407


def test_the_server_record_resolves_to_its_preset() -> None:
    """settings/user's last_used_model_config is what a send without
    --effort/--model inherits (the composer's own cookie is inert)."""
    model = profile_context.model_of(MODELS_PAYLOAD, SETTINGS_PAYLOAD)
    assert model["server"]["preset"] == "Extra High"
    assert model["server"]["surface"] == "web"
    assert model["server"]["efforts_by_model"]["gpt-6-pro"] == "standard"


def test_a_model_only_preset_matches_whatever_effort_the_server_remembers() -> None:
    """Pro carries no effort on the slider, but the server remembers one."""
    presets = model_settings.presets_of(MODELS_PAYLOAD)
    found = profile_context.preset_for(presets, "gpt-6-pro", "standard")
    assert found is not None and found["title"] == "Pro"
    assert profile_context.preset_for(presets, "gpt-5-6-thinking", "") is None


def test_ultra_is_reported_as_a_setting_and_never_as_an_effort_level() -> None:
    model = profile_context.model_of(MODELS_PAYLOAD, SETTINGS_PAYLOAD)
    assert model["ultra_effort_enabled"] is True
    assert "cookie" not in model  # the inert cookie is not reported any more
    assert "ultra" not in {e for levels in model["api_levels"].values() for e in levels}


def test_project_fields_come_from_the_gizmo_and_files_keep_scalars_only() -> None:
    project = profile_context.project_of(GIZMO_PAYLOAD)
    assert project["instructions"] == "Answer in JSON."
    assert project["memory_scope"] == "global"
    assert project["url"] == "https://chatgpt.com/g/g-p-abc-workers/project"
    assert project["files"] == [{"id": "file-1", "name": "paper.pdf", "size": 12}]


class _Backend:
    """``session.session.call`` over canned responses, matched by prefix."""

    def __init__(self, responses: dict[str, tuple[int, dict]]) -> None:
        self.responses = responses

    def call(self, path: str) -> tuple[int, dict]:
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return 404, {"error": "no such fake"}


class _ProfileSession:
    def __init__(self, backend: _Backend) -> None:
        self.session = backend


_READS = {
    "/backend-api/user_system_messages": (200, USER_SYSTEM_MESSAGES),
    "/backend-api/memories?include_memory_entries=false": (200, MEMORY_SUMMARY),
    "/backend-api/memories?include_memory_entries=true": (200, MEMORY_ENTRIES),
    "/backend-api/settings/user": (200, SETTINGS_PAYLOAD),
    "/backend-api/models": (200, MODELS_PAYLOAD),
    "/backend-api/gizmos/g-p-abc": (200, GIZMO_PAYLOAD),
}


def _wire(monkeypatch, reads: dict) -> None:
    monkeypatch.setattr(
        profile_context,
        "open_session",
        lambda *a, **k: _ProfileSession(_Backend(reads)),
    )


def test_the_document_is_written_next_to_where_a_run_keeps_its_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _wire(monkeypatch, _READS)
    out = tmp_path / "chatgpt" / "profile_context.json"
    assert profile_context.main(["--project", "g-p-abc", "--json", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["errors"] == []
    assert doc["custom_instructions"]["about"] == "I am a software engineer."
    assert doc["memory"]["entries"] == 2
    assert doc["model"]["server"]["preset"] == "Extra High"
    assert doc["project"]["id"] == "g-p-abc"
    assert "written to" in capsys.readouterr().out


def test_the_summary_shows_lengths_and_counts_but_never_the_profile_text(
    monkeypatch, capsys
) -> None:
    """The orchestrator logs stdout; the user's profile must not end up there."""
    _wire(monkeypatch, _READS)
    assert profile_context.main(["--project", "g-p-abc"]) == 0
    out = capsys.readouterr().out
    assert "software engineer" not in out
    assert "Answer in JSON" not in out
    assert "about 25 chars" in out
    assert "2 entries (1 project-scoped)" in out
    assert "preset 'Extra High' (3 of 4)" in out


def test_a_failed_read_is_recorded_and_fails_the_exit_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Partial evidence is still saved, but the run must know it is partial."""
    reads = {k: v for k, v in _READS.items() if "gizmos" not in k}
    _wire(monkeypatch, reads)
    out = tmp_path / "ctx.json"
    assert profile_context.main(["--project", "g-p-gone", "--json", str(out)]) == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["errors"] == ["/backend-api/gizmos/g-p-gone: HTTP 404"]
    assert doc["memory"]["entries"] == 2
    assert "NOT COMPLETE" in capsys.readouterr().out
