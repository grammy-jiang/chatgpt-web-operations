"""Tests for the pure, data-only half of chatgpt_client.py.

Reply parsing, conversation-mapping helpers, the effort cookie rewrite, and
the send-page request filter: everything that decides an outcome over data
with no session, no browser, and no filesystem. ``ChatGPTSession``,
``wait_for_reply``, the cookie-decrypt machinery, the browser slot/lock, and
the display helpers live in ``test_client_session.py`` instead, because they
need a fake clock, a fake filesystem or a fake ``cs`` module. ``BrowserSender``
(roughly line 1038 onward) is covered by ``test_client_browser.py``.

Every test names the failure it defends against, matching ``test_commands.py``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402

# ---------------------------------------------------------------------------
# TransportError -- carries the HTTP status so callers can branch on it
# ---------------------------------------------------------------------------


def test_transport_error_keeps_its_status_and_reads_as_the_message() -> None:
    exc = cc.TransportError("HTTP 404: not found", 404)
    assert exc.status == 404
    assert str(exc) == "HTTP 404: not found"


def test_transport_error_status_defaults_to_zero() -> None:
    """A status-less error (a socket failure) must not look like a real code."""
    assert cc.TransportError("boom").status == 0


# ---------------------------------------------------------------------------
# extract_blocks -- ===BEGIN name=== ... ===END name=== reply sections
# ---------------------------------------------------------------------------


def test_a_single_block_round_trips_its_body() -> None:
    text = "before\n===BEGIN result===\nhello\nworld\n===END result===\nafter"
    assert cc.extract_blocks(text) == {"result": "hello\nworld\n"}


def test_two_differently_named_blocks_both_come_back() -> None:
    text = "===BEGIN a===\nfirst\n===END a===\n===BEGIN b===\nsecond\n===END b===\n"
    blocks = cc.extract_blocks(text)
    assert blocks == {"a": "first\n", "b": "second\n"}


def test_a_repeated_block_name_keeps_the_last_one() -> None:
    """A retried step's reply can carry two attempts; only the final one counts."""
    text = (
        "===BEGIN result===\nfirst try\n===END result===\n"
        "===BEGIN result===\nsecond try\n===END result===\n"
    )
    assert cc.extract_blocks(text) == {"result": "second try\n"}


def test_a_fence_wrapped_around_a_block_body_is_unwrapped() -> None:
    """The model sometimes wraps its whole answer in a code fence anyway."""
    text = '===BEGIN result===\n```json\n{"a": 1}\n```\n===END result===\n'
    assert cc.extract_blocks(text) == {"result": '{"a": 1}\n'}


def test_text_with_no_blocks_yields_nothing() -> None:
    assert cc.extract_blocks("just prose, no markers here") == {}


def test_an_empty_block_body_still_yields_a_trailing_newline() -> None:
    text = "===BEGIN empty===\n===END empty===\n"
    assert cc.extract_blocks(text) == {"empty": "\n"}


# ---------------------------------------------------------------------------
# unclosed_block -- tells "truncated" apart from "wrong format"
# ---------------------------------------------------------------------------


def test_no_markers_at_all_is_not_an_unclosed_block() -> None:
    assert cc.unclosed_block("nothing here") is None


def test_a_block_that_opened_and_closed_is_not_unclosed() -> None:
    text = "===BEGIN synthesis.json===\n{}\n===END synthesis.json===\n"
    assert cc.unclosed_block(text) is None


def test_a_block_that_opened_and_never_closed_is_named() -> None:
    """The failure this exists for: a truncated reply, not a malformed one."""
    text = "===BEGIN synthesis.json===\nhalfway through and then it just stops"
    assert cc.unclosed_block(text) == "synthesis.json"


def test_the_most_recently_opened_unclosed_block_wins() -> None:
    """Among several opens, report the one still missing its END, not the last."""
    text = (
        "===BEGIN a===\n1\n===END a===\n"
        "===BEGIN b===\n2\n"  # never closed
        "===BEGIN c===\n3\n===END c===\n"
    )
    assert cc.unclosed_block(text) == "b"


# ---------------------------------------------------------------------------
# fenced_json -- last fence wins, or the whole text if it already looks JSON
# ---------------------------------------------------------------------------


def test_a_json_fence_returns_its_content() -> None:
    text = 'prose\n```json\n{"a": 1}\n```\nmore prose'
    assert cc.fenced_json(text) == '{"a": 1}\n'


@pytest.mark.parametrize("lang", ["json", "jsonl", "markdown", "md", ""])
def test_every_documented_fence_language_is_recognised(lang: str) -> None:
    text = f"```{lang}\ncontent\n```"
    assert cc.fenced_json(text) == "content\n"


def test_the_last_of_several_fences_is_used() -> None:
    text = '```json\n{"old": 1}\n```\nsome words\n```json\n{"new": 2}\n```'
    assert cc.fenced_json(text) == '{"new": 2}\n'


def test_unfenced_text_that_already_looks_like_json_is_accepted() -> None:
    assert cc.fenced_json('  {"a": 1}  ') == '{"a": 1}\n'
    assert cc.fenced_json("[1, 2]") == "[1, 2]\n"


def test_plain_prose_with_no_fence_and_no_json_shape_returns_none() -> None:
    assert cc.fenced_json("just an ordinary sentence.") is None


def test_an_empty_reply_is_not_json() -> None:
    """``"" in "{["`` is vacuously True in Python; the check once let an
    empty reply through as a JSON block of one newline (fixed 2026-09-20).
    """
    assert cc.fenced_json("") is None
    assert cc.fenced_json("   ") is None


# ---------------------------------------------------------------------------
# parse_json_text -- json.loads, falling back to the largest {...} / [...]
# ---------------------------------------------------------------------------


def test_text_that_is_already_valid_json_parses_directly() -> None:
    assert cc.parse_json_text('{"ok": true}') == {"ok": True}


def test_an_object_embedded_in_prose_is_extracted() -> None:
    text = 'Here you go: {"a": 1, "b": [1, 2]} -- hope that helps.'
    assert cc.parse_json_text(text) == {"a": 1, "b": [1, 2]}


def test_the_object_pair_is_tried_before_the_array_pair() -> None:
    """A stray unmatched '[' in the prose must not derail an object extract."""
    text = 'result {"a": 1} and a lone [ bracket that is not JSON'
    assert cc.parse_json_text(text) == {"a": 1}


def test_an_array_embedded_in_prose_is_extracted_when_no_object_is_present() -> None:
    assert cc.parse_json_text("prefix [1, 2, 3] suffix") == [1, 2, 3]


def test_unbalanced_braces_never_produce_a_span_to_try() -> None:
    """A lone '{' with no matching '}' must not be sliced into a bogus span."""
    with pytest.raises(ValueError, match="no JSON object found"):
        cc.parse_json_text("only a lone { and a lone [ here, nothing closes")


def test_prose_with_no_json_shape_at_all_raises() -> None:
    with pytest.raises(ValueError, match="no JSON object found"):
        cc.parse_json_text("no braces or brackets in this reply whatsoever")


# ---------------------------------------------------------------------------
# chat_id / is_provisional -- an id from either a bare id or a /c/ URL
# ---------------------------------------------------------------------------


def test_a_bare_id_is_returned_stripped() -> None:
    assert cc.chat_id("  6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8  ") == (
        "6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    )


def test_a_full_c_url_is_reduced_to_its_id() -> None:
    url = "https://chatgpt.com/c/6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    assert cc.chat_id(url) == "6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"


def test_a_provisional_web_id_keeps_its_prefix() -> None:
    url = "https://chatgpt.com/c/WEB:6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    assert cc.chat_id(url) == "WEB:6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"


def test_an_id_shorter_than_twenty_characters_is_not_treated_as_a_c_url_id() -> None:
    """Below the length floor, the whole string is kept rather than sliced."""
    assert cc.chat_id("/c/abc123") == "/c/abc123"


def test_is_provisional_is_true_only_for_a_web_prefixed_id() -> None:
    assert cc.is_provisional("WEB:abc") is True
    assert cc.is_provisional("6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8") is False
    url = "https://chatgpt.com/c/WEB:6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    assert cc.is_provisional(url) is True


# ---------------------------------------------------------------------------
# epoch_of / _created_since -- a listing's create_time may be a string, a
# number, or missing, and an unreadable stamp must never discard the row
# ---------------------------------------------------------------------------


def test_an_iso_string_with_a_z_suffix_parses_as_utc() -> None:
    got = cc.epoch_of("2026-09-16T14:04:05Z")
    want = datetime(2026, 9, 16, 14, 4, 5, tzinfo=UTC).timestamp()
    assert got == want


def test_an_iso_string_with_an_explicit_offset_parses_too() -> None:
    got = cc.epoch_of("2026-09-16T14:04:05.700000+00:00")
    want = datetime(2026, 9, 16, 14, 4, 5, 700000, tzinfo=UTC).timestamp()
    assert got == want


@pytest.mark.parametrize("value", [1758030000, 1758030000.5])
def test_a_numeric_value_is_returned_as_a_float_unchanged(value: Any) -> None:
    assert cc.epoch_of(value) == float(value)


@pytest.mark.parametrize("value", [None, "", [], {}, 0])
def test_an_unreadable_or_empty_value_is_zero_not_an_exception(value: Any) -> None:
    if value == 0:
        # 0 is numeric and a legitimate (if odd) epoch; kept separate below.
        assert cc.epoch_of(value) == 0.0
    else:
        assert cc.epoch_of(value) == 0.0


def test_a_string_that_is_not_a_date_returns_zero() -> None:
    assert cc.epoch_of("not a timestamp") == 0.0


def test_an_unreadable_stamp_is_kept_rather_than_discarded() -> None:
    """Losing the right conversation is worse than keeping one extra."""
    assert cc._created_since(None, since=1_800_000_000.0) is True
    assert cc._created_since("garbage", since=1_800_000_000.0) is True


def test_a_stamp_at_or_after_the_bound_is_kept() -> None:
    since = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).timestamp()
    exactly_at = "2026-09-16T12:00:00+00:00"
    assert cc._created_since(exactly_at, since) is True


def test_a_five_second_slack_covers_the_send_before_the_server_stamp() -> None:
    since = datetime(2026, 9, 16, 12, 0, 5, tzinfo=UTC).timestamp()
    four_seconds_earlier = "2026-09-16T12:00:01+00:00"
    assert cc._created_since(four_seconds_earlier, since) is True


def test_a_stamp_well_before_the_bound_is_discarded() -> None:
    since = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).timestamp()
    long_before = "2026-09-16T11:00:00+00:00"
    assert cc._created_since(long_before, since) is False


# ---------------------------------------------------------------------------
# resolve_new_conversation -- picking the one conversation a send created
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock and a sleep that advances it, with no real delay."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds


class _ListingSession:
    """A session whose list_conversations/get_conversation are scripted."""

    def __init__(self, pages: list[list[dict]], not_found_first: int = 0) -> None:
        self.pages = pages
        self.not_found_first = not_found_first
        self.list_calls = 0
        self.get_calls = 0

    def list_conversations(self, limit: int = 10) -> list[dict]:
        page = self.pages[min(self.list_calls, len(self.pages) - 1)]
        self.list_calls += 1
        return page

    def get_conversation(self, chat: str) -> dict:
        self.get_calls += 1
        if self.get_calls <= self.not_found_first:
            raise cc.TransportError("not found yet", 404)
        return {"id": chat}


def test_exactly_one_fresh_conversation_is_returned_once_readable() -> None:
    session = _ListingSession([[{"id": "new1"}]])
    clock = _FakeClock()
    got = cc.resolve_new_conversation(session, known_ids=set(), sleep=clock.sleep)
    assert got == "new1"
    assert session.get_calls == 1


def test_a_conversation_already_known_is_not_fresh() -> None:
    session = _ListingSession([[{"id": "old"}, {"id": "new1"}]])
    got = cc.resolve_new_conversation(
        session, known_ids={"old"}, sleep=_FakeClock().sleep
    )
    assert got == "new1"


def test_a_conversation_created_before_since_is_excluded() -> None:
    """Another run's chat, opened earlier, must not look like this send's."""
    since = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).timestamp()
    page = [
        {"id": "too-old", "create_time": "2026-09-16T11:00:00+00:00"},
        {"id": "new1", "create_time": "2026-09-16T12:00:02+00:00"},
    ]
    got = cc.resolve_new_conversation(
        _ListingSession([page]), known_ids=set(), since=since, sleep=_FakeClock().sleep
    )
    assert got == "new1"


def test_a_conversation_not_yet_readable_is_retried_then_succeeds() -> None:
    session = _ListingSession([[{"id": "new1"}]], not_found_first=2)
    got = cc.resolve_new_conversation(
        session, known_ids=set(), sleep=_FakeClock().sleep
    )
    assert got == "new1"
    assert session.get_calls == 3


def test_a_conversation_that_never_becomes_readable_raises() -> None:
    session = _ListingSession([[{"id": "new1"}]], not_found_first=999)
    with pytest.raises(cc.TransportError, match="not readable yet"):
        cc.resolve_new_conversation(session, known_ids=set(), sleep=_FakeClock().sleep)


def test_a_non_404_error_while_confirming_readability_is_not_swallowed() -> None:
    """A 404 means "not yet visible"; anything else is a real failure."""

    class _Session:
        def list_conversations(self, limit: int = 10) -> list[dict]:
            return [{"id": "new1"}]

        def get_conversation(self, chat: str) -> dict:
            raise cc.TransportError("server exploded", 500)

    with pytest.raises(cc.TransportError, match="server exploded"):
        cc.resolve_new_conversation(
            _Session(), known_ids=set(), sleep=_FakeClock().sleep
        )


def test_two_fresh_conversations_is_reported_as_ambiguous_not_guessed() -> None:
    session = _ListingSession([[{"id": "a"}, {"id": "b"}]])
    with pytest.raises(cc.TransportError, match="several new conversations"):
        cc.resolve_new_conversation(session, known_ids=set(), sleep=_FakeClock().sleep)


def test_no_fresh_conversation_before_the_deadline_times_out() -> None:
    session = _ListingSession([[]])
    clock = _FakeClock()
    monkeypatch_target = cc.time
    original = monkeypatch_target.monotonic
    monkeypatch_target.monotonic = clock.monotonic
    try:
        with pytest.raises(cc.TransportError, match="did not create a conversation"):
            cc.resolve_new_conversation(
                session, known_ids=set(), timeout=20, interval=10, sleep=clock.sleep
            )
    finally:
        monkeypatch_target.monotonic = original
    assert clock.calls == [10, 10]  # the initial settle sleep, then one retry


# ---------------------------------------------------------------------------
# chain / message_text -- walking mapping[current_node] back to the root
# ---------------------------------------------------------------------------


def _msg(
    node_id: str,
    role: str,
    text: str = "",
    *,
    recipient: str | None = "all",
    status: str = "finished_successfully",
    content_type: str = "text",
    metadata: dict | None = None,
    parts: list | None = None,
) -> dict:
    return {
        "id": node_id,
        "author": {"role": role},
        "content": {
            "content_type": content_type,
            "parts": parts if parts is not None else ([text] if text else []),
        },
        "recipient": recipient,
        "status": status,
        "metadata": metadata or {},
    }


def _conv(
    *msgs: dict, current: str | None = None, mapping_extra: dict | None = None
) -> dict:
    """A conversation whose mapping links each message to the previous one."""
    mapping: dict[str, dict] = dict(mapping_extra or {})
    parent = None
    for m in msgs:
        mapping[m["id"]] = {"message": m, "parent": parent}
        parent = m["id"]
    return {
        "mapping": mapping,
        "current_node": current if current is not None else parent,
    }


def test_an_empty_conversation_has_no_chain() -> None:
    assert cc.chain({}) == []
    assert cc.chain({"mapping": {}, "current_node": None}) == []


def test_current_node_missing_from_the_mapping_yields_an_empty_chain() -> None:
    assert cc.chain({"mapping": {}, "current_node": "ghost"}) == []


def test_the_chain_walks_root_to_current_in_order() -> None:
    conv = _conv(_msg("1", "user", "hi"), _msg("2", "assistant", "hello"))
    got = cc.chain(conv)
    assert [m["id"] for m in got] == ["1", "2"]


def test_a_root_node_with_no_message_becomes_an_empty_dict() -> None:
    """The real API's system root node carries ``message: null``."""
    conv = _conv(_msg("2", "assistant", "hi"))
    conv["mapping"]["1"] = {"message": None, "parent": None}
    conv["mapping"]["2"]["parent"] = "1"
    got = cc.chain(conv)
    assert got[0] == {}
    assert got[1]["id"] == "2"


def test_a_broken_parent_link_stops_the_chain_early_rather_than_crashing() -> None:
    conv = _conv(_msg("1", "user", "hi"), _msg("2", "assistant", "hello"))
    conv["mapping"]["2"]["parent"] = "missing-node"
    got = cc.chain(conv)
    assert [m["id"] for m in got] == ["2"]


def test_message_text_joins_multiple_parts_with_newlines() -> None:
    msg = _msg("1", "user", parts=["line one", "line two"])
    assert cc.message_text(msg) == "line one\nline two"


def test_message_text_json_encodes_a_non_string_part() -> None:
    msg = _msg("1", "assistant", parts=[{"type": "image"}])
    assert cc.message_text(msg) == '{"type": "image"}'


def test_message_text_of_a_missing_content_is_empty() -> None:
    assert cc.message_text({}) == ""
    assert cc.message_text({"content": None}) == ""


def test_message_text_with_no_parts_key_is_empty() -> None:
    assert cc.message_text({"content": {"content_type": "text"}}) == ""


# ---------------------------------------------------------------------------
# is_visible_assistant_text / assistant_text_messages / latest_reply
# ---------------------------------------------------------------------------


def test_a_finished_assistant_text_message_addressed_to_all_is_visible() -> None:
    assert cc.is_visible_assistant_text(_msg("1", "assistant", "hi")) is True


def test_a_tool_call_recipient_is_not_visible() -> None:
    """web.run and similar tool turns are assistant-authored but not for us."""
    msg = _msg("1", "assistant", "browsing...", recipient="browser")
    assert cc.is_visible_assistant_text(msg) is False


def test_a_non_text_content_type_is_not_visible() -> None:
    msg = _msg("1", "assistant", "print('hi')", content_type="code")
    assert cc.is_visible_assistant_text(msg) is False


def test_recipient_none_is_still_visible() -> None:
    msg = _msg("1", "assistant", "hi", recipient=None)
    assert cc.is_visible_assistant_text(msg) is True


def test_blank_text_is_not_visible() -> None:
    msg = _msg("1", "assistant", parts=["   "])
    assert cc.is_visible_assistant_text(msg) is False


def test_a_user_message_is_never_visible_assistant_text() -> None:
    assert cc.is_visible_assistant_text(_msg("1", "user", "hi")) is False


def test_a_message_with_no_author_does_not_crash() -> None:
    assert cc.is_visible_assistant_text({"content": {}}) is False


def test_assistant_text_messages_keeps_only_the_visible_ones() -> None:
    conv = _conv(
        _msg("1", "user", "question"),
        _msg("2", "assistant", "thinking...", recipient="browser"),
        _msg("3", "assistant", "the answer"),
    )
    got = cc.assistant_text_messages(conv)
    assert [m["id"] for m in got] == ["3"]


def test_latest_reply_is_the_last_visible_assistant_message() -> None:
    conv = _conv(
        _msg("1", "user", "q"),
        _msg("2", "assistant", "first reply"),
        _msg("3", "user", "follow-up"),
        _msg("4", "assistant", "second reply"),
    )
    assert cc.latest_reply(conv) == "second reply"


def test_latest_reply_of_a_conversation_with_no_assistant_text_is_empty() -> None:
    conv = _conv(_msg("1", "user", "q"))
    assert cc.latest_reply(conv) == ""


# ---------------------------------------------------------------------------
# tail_signature -- is the newest node a finished, visible assistant reply?
# ---------------------------------------------------------------------------


def test_an_empty_chain_signature_is_not_final() -> None:
    assert cc.tail_signature({}) == ("", 0, "", False)


def test_a_finished_visible_reply_is_the_final_signature() -> None:
    conv = _conv(_msg("1", "user", "q"), _msg("2", "assistant", "done"))
    node, length, ct, final = cc.tail_signature(conv)
    assert node == "2"
    assert length == 2
    assert ct == "text"
    assert final is True


def test_a_still_streaming_reply_is_not_final() -> None:
    conv = _conv(
        _msg("1", "user", "q"),
        _msg("2", "assistant", "partial", status="in_progress"),
    )
    assert cc.tail_signature(conv)[3] is False


def test_a_finished_tool_call_tail_is_not_final() -> None:
    """A finished turn that ends on tool traffic has nothing for the user yet."""
    conv = _conv(
        _msg("1", "user", "q"),
        _msg("2", "assistant", "searching", recipient="browser"),
    )
    assert cc.tail_signature(conv)[3] is False


def test_a_tail_ending_on_a_user_message_is_not_final() -> None:
    conv = _conv(_msg("1", "assistant", "hi"), _msg("2", "user", "q"))
    assert cc.tail_signature(conv)[3] is False


def test_the_signature_reports_the_actual_content_type_even_when_not_final() -> None:
    conv = _conv(_msg("1", "assistant", "code", content_type="code"))
    assert cc.tail_signature(conv)[2] == "code"


# ---------------------------------------------------------------------------
# user_turns / model_of
# ---------------------------------------------------------------------------


def test_user_turns_counts_only_user_authored_messages() -> None:
    conv = _conv(
        _msg("1", "user", "a"),
        _msg("2", "assistant", "b"),
        _msg("3", "user", "c"),
    )
    assert cc.user_turns(conv) == 2


def test_user_turns_of_an_empty_conversation_is_zero() -> None:
    assert cc.user_turns({}) == 0


def test_user_turns_survives_a_root_node_with_no_message() -> None:
    conv = _conv(_msg("2", "user", "a"))
    conv["mapping"]["1"] = {"message": None, "parent": None}
    conv["mapping"]["2"]["parent"] = "1"
    assert cc.user_turns(conv) == 1


def test_model_of_reads_the_resolved_slug_from_the_last_message_that_has_one() -> None:
    meta = {"resolved_model_slug": "gpt-5-6-thinking"}
    conv = _conv(
        _msg("1", "assistant", "a", metadata=meta),
        _msg("2", "assistant", "b"),
    )
    assert cc.model_of(conv) == "gpt-5-6-thinking"


def test_model_of_falls_back_to_model_slug_when_resolved_is_absent() -> None:
    meta = {"model_slug": "gpt-5-6-instant"}
    conv = _conv(_msg("1", "assistant", "a", metadata=meta))
    assert cc.model_of(conv) == "gpt-5-6-instant"


def test_model_of_scans_backward_past_messages_with_no_metadata() -> None:
    meta = {"resolved_model_slug": "gpt-5-6-thinking"}
    conv = _conv(
        _msg("1", "assistant", "a", metadata=meta),
        _msg("2", "user", "b"),
        _msg("3", "assistant", "c"),  # no metadata at all
    )
    assert cc.model_of(conv) == "gpt-5-6-thinking"


def test_model_of_an_unlabelled_conversation_is_unknown() -> None:
    conv = _conv(_msg("1", "user", "a"))
    assert cc.model_of(conv) == "unknown"


# ---------------------------------------------------------------------------
# transcript -- every user and visible-assistant turn, for the local archive
# ---------------------------------------------------------------------------


def test_transcript_keeps_user_and_visible_assistant_turns_in_order() -> None:
    conv = _conv(
        _msg("1", "user", "question"),
        _msg("2", "assistant", "searching", recipient="browser"),
        _msg("3", "assistant", "the answer", metadata={"resolved_model_slug": "m1"}),
    )
    out = cc.transcript(conv)
    assert [t["role"] for t in out] == ["user", "assistant"]
    assert out[1]["text"] == "the answer"
    assert out[1]["model"] == "m1"


def test_transcript_drops_a_user_turn_with_no_text() -> None:
    """An empty submission (e.g. attachment-only) must not appear as a turn."""
    conv = _conv(_msg("1", "user", parts=[]), _msg("2", "assistant", "hi"))
    out = cc.transcript(conv)
    assert [t["role"] for t in out] == ["assistant"]


def test_transcript_of_an_empty_conversation_is_empty() -> None:
    assert cc.transcript({}) == []


def test_transcript_carries_create_time_through_unchanged() -> None:
    msg = _msg("1", "user", "hi")
    msg["create_time"] = 1758030000.0
    conv = _conv(msg)
    assert cc.transcript(conv)[0]["create_time"] == 1758030000.0


def test_transcript_model_is_none_for_a_user_turn() -> None:
    conv = _conv(_msg("1", "user", "hi"))
    assert cc.transcript(conv)[0]["model"] is None


# ---------------------------------------------------------------------------
# EFFORTS / EFFORT_COOKIE / with_effort -- pinning the reasoning effort cookie
# ---------------------------------------------------------------------------


def test_the_documented_effort_levels_are_exactly_these_four() -> None:
    assert cc.EFFORTS == ("min", "standard", "extended", "max")


def test_the_cookie_name_matches_what_the_composer_reads() -> None:
    assert cc.EFFORT_COOKIE == "oai-last-model-config"


def test_a_blank_effort_returns_the_same_list_unchanged() -> None:
    """Inheriting the profile's own setting must not rewrite anything."""
    cookies = [{"name": "a", "value": "1"}]
    assert cc.with_effort(cookies, "") is cookies


def test_an_unknown_effort_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown thinking effort"):
        cc.with_effort([], "ultra")


def _decoded_effort_cookie(cookies: list[dict]) -> dict:
    import json
    from urllib.parse import unquote

    (cookie,) = [c for c in cookies if c["name"] == cc.EFFORT_COOKIE]
    return json.loads(unquote(cookie["value"]))


def test_a_missing_effort_cookie_is_added() -> None:
    out = cc.with_effort([{"name": "other", "value": "x"}], "max")
    assert _decoded_effort_cookie(out) == {"effort": "max"}
    assert {"name": "other", "value": "x"} in out


def test_an_existing_effort_cookie_is_replaced_and_keeps_the_model() -> None:
    import json
    from urllib.parse import quote

    existing = {
        "name": cc.EFFORT_COOKIE,
        "value": quote(json.dumps({"model": "gpt-5-6-thinking", "effort": "standard"})),
    }
    out = cc.with_effort([existing, {"name": "other", "value": "x"}], "max")
    assert len([c for c in out if c["name"] == cc.EFFORT_COOKIE]) == 1
    assert _decoded_effort_cookie(out) == {"model": "gpt-5-6-thinking", "effort": "max"}


def test_other_cookies_are_left_untouched() -> None:
    other = {"name": "__Secure-next-auth.session-token.0", "value": "sess"}
    out = cc.with_effort([other], "max")
    assert other in out


def test_a_malformed_existing_cookie_value_is_tolerated_not_raised() -> None:
    """A cookie the browser has not written yet must not break every send."""
    broken = {"name": cc.EFFORT_COOKIE, "value": "not%20valid%20json"}
    out = cc.with_effort([broken], "max")
    assert _decoded_effort_cookie(out) == {"effort": "max"}


def test_the_new_cookie_carries_the_documented_attributes() -> None:
    out = cc.with_effort([], "min")
    (cookie,) = out
    assert cookie["domain"] == "chatgpt.com"
    assert cookie["path"] == "/"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is False
    assert cookie["sameSite"] == "Lax"


# ---------------------------------------------------------------------------
# block_unused / UNUSED_ON_SEND -- abort the requests a scripted send never
# needs, without ever touching the composer, sentinel or post calls
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = _FakeRequest(url)
        self.aborted = False
        self.continued = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class _RaisingRoute(_FakeRoute):
    """abort()/continue_() raise, as a closed page's Playwright calls do."""

    def abort(self) -> None:
        raise RuntimeError("page already closed")

    def continue_(self) -> None:
        raise RuntimeError("page already closed")


class _FakeContext:
    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}

    def route(self, pattern: str, handler: Any) -> None:
        self.routes[pattern] = handler


def test_both_documented_url_patterns_get_a_route() -> None:
    ctx = _FakeContext()
    cc.block_unused(ctx)
    assert set(ctx.routes) == {"**/backend-api/**", "**/ces/v1/**"}


@pytest.mark.parametrize("path", cc.UNUSED_ON_SEND)
def test_every_unused_path_is_aborted_by_default(path: str) -> None:
    ctx = _FakeContext()
    cc.block_unused(ctx)
    handler = ctx.routes["**/backend-api/**"]
    route = _FakeRoute(f"https://chatgpt.com{path}?x=1")
    handler(route)
    assert route.aborted is True
    assert route.continued is False


def test_a_url_outside_the_unused_list_is_let_through() -> None:
    ctx = _FakeContext()
    cc.block_unused(ctx)
    handler = ctx.routes["**/backend-api/**"]
    route = _FakeRoute("https://chatgpt.com/backend-api/f/conversation")
    handler(route)
    assert route.continued is True
    assert route.aborted is False


def test_allow_project_lets_the_gizmo_sidebar_through() -> None:
    """Composing inside a project needs its own page to render."""
    ctx = _FakeContext()
    cc.block_unused(ctx, allow_project=True)
    handler = ctx.routes["**/backend-api/**"]
    route = _FakeRoute("https://chatgpt.com/backend-api/gizmos/snorlax/sidebar")
    handler(route)
    assert route.continued is True
    assert route.aborted is False


def test_allow_project_still_blocks_the_conversation_list() -> None:
    ctx = _FakeContext()
    cc.block_unused(ctx, allow_project=True)
    handler = ctx.routes["**/backend-api/**"]
    route = _FakeRoute("https://chatgpt.com/backend-api/conversations?offset=0")
    handler(route)
    assert route.aborted is True


def test_a_route_call_that_raises_does_not_propagate() -> None:
    """A page torn down mid-request must not crash the send over a filter."""
    ctx = _FakeContext()
    cc.block_unused(ctx)
    handler = ctx.routes["**/backend-api/**"]
    handler(_RaisingRoute("https://chatgpt.com/backend-api/conversations"))
    handler(_RaisingRoute("https://chatgpt.com/backend-api/f/conversation"))
