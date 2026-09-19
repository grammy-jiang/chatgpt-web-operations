"""Tests for ``read_chat.turn_facts`` and the ``--effort`` output.

This is how the lead verifies a live send made with ``send_prompt.py
--effort/--model/--search``: the page takes the real ``thinking_effort``
and ``model`` from the account's server-side settings, not from the
composer's cookie (SKILL.md, "Reasoning effort"; two recorded sends on
2026-09-20 both carried ``max`` regardless of the cookie), so the reply's
own message metadata -- ``thinking_effort``, ``resolved_model_slug``,
``search_result_groups``, ``citations`` -- is the only place that says what
a send actually used.

``turn_facts`` is a pure function over the raw
``/backend-api/conversation/<id>`` payload shape (``mapping`` /
``current_node``), with no ``chatgpt_client`` import, so it is tested with
hand-built conversations shaped like that payload -- the same style
``tests/test_client_core.py`` uses for ``chain`` (its ``_conv`` / ``_msg``
builders are duplicated here rather than imported, since nothing outside
this file may be edited to add a fixture). ``read_chat.main``'s ``--effort``
path is tested the way ``tests/test_command_mains_2.py`` already tests its
other output modes: a small fake client for the header lines
(``turn_state`` still needs one) plus a real conversation dict for
``turn_facts`` to walk.

Every test names the failure or behaviour it defends, matching
``test_commands.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import read_chat  # noqa: E402

# ---------------------------------------------------------------------------
# conversation builders (duplicated from tests/test_client_core.py's _conv /
# _msg; see this file's own docstring for why)
# ---------------------------------------------------------------------------


def _msg(
    node_id: str,
    role: str,
    *,
    recipient: str | None = "all",
    content_type: str = "text",
    parts: list | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "id": node_id,
        "author": {"role": role},
        "content": {
            "content_type": content_type,
            "parts": ["text"] if parts is None else parts,
        },
        "recipient": recipient,
        "metadata": metadata or {},
    }


def _conv(*msgs: dict, current: str | None = None) -> dict:
    """A conversation whose mapping links each message to the previous one."""
    mapping: dict[str, dict] = {}
    parent = None
    for m in msgs:
        mapping[m["id"]] = {"message": m, "parent": parent}
        parent = m["id"]
    return {
        "mapping": mapping,
        "current_node": current if current is not None else parent,
    }


def _reply(node_id: str, *, recipient: str | None = "all", **metadata: object) -> dict:
    """An assistant text message carrying the given metadata; visible to
    the user unless ``recipient`` is overridden away from ``"all"``."""
    return _msg(
        node_id,
        "assistant",
        recipient=recipient,
        parts=["hello"],
        metadata=dict(metadata),
    )


# ---------------------------------------------------------------------------
# turn_facts -- pure, over a hand-built conversation
# ---------------------------------------------------------------------------


def test_an_empty_conversation_has_no_facts() -> None:
    assert read_chat.turn_facts({}) == []
    assert read_chat.turn_facts({"mapping": {}, "current_node": None}) == []


def test_current_node_missing_from_the_mapping_yields_no_facts() -> None:
    assert read_chat.turn_facts({"mapping": {}, "current_node": "ghost"}) == []


def test_a_reply_with_full_metadata_reports_every_field() -> None:
    conv = _conv(
        _msg("1", "user", parts=["hi"]),
        _reply(
            "2",
            thinking_effort="max",
            model_slug="gpt-5-6-thinking",
            resolved_model_slug="gpt-5-6-thinking",
            search_result_groups=[{"type": "search"}],
            citations=[{"url": "https://example.com"}],
        ),
    )
    facts = read_chat.turn_facts(conv)
    assert facts == [
        {
            "thinking_effort": "max",
            "resolved_model_slug": "gpt-5-6-thinking",
            "has_search": True,
            "has_citations": True,
        }
    ]


def test_empty_search_and_citation_lists_report_as_absent() -> None:
    """An empty list is still present in the payload; it must not count as
    "used search"."""
    conv = _conv(
        _reply(
            "1",
            thinking_effort="standard",
            resolved_model_slug="gpt-5-6-thinking",
            search_result_groups=[],
            citations=[],
        )
    )
    facts = read_chat.turn_facts(conv)
    assert facts[0]["has_search"] is False
    assert facts[0]["has_citations"] is False


def test_a_reply_with_no_metadata_at_all_still_reports_a_turn() -> None:
    conv = _conv(_reply("1"))
    facts = read_chat.turn_facts(conv)
    assert facts == [
        {
            "thinking_effort": None,
            "resolved_model_slug": None,
            "has_search": False,
            "has_citations": False,
        }
    ]


def test_multiple_replies_are_reported_in_chain_order() -> None:
    conv = _conv(
        _msg("1", "user", parts=["hi"]),
        _reply("2", thinking_effort="max"),
        _msg("3", "user", parts=["again"]),
        _reply("4", thinking_effort="min"),
    )
    facts = read_chat.turn_facts(conv)
    assert [f["thinking_effort"] for f in facts] == ["max", "min"]


def test_a_user_turn_is_never_reported_as_a_fact() -> None:
    conv = _conv(_msg("1", "user", parts=["hi"]))
    assert read_chat.turn_facts(conv) == []


def test_a_tool_call_recipient_is_not_a_visible_turn() -> None:
    """web.run and similar tool traffic is assistant-authored but not
    addressed to the user; it must not count as a reply."""
    conv = _conv(
        _reply("1", thinking_effort="max", recipient="web.run"),
    )
    assert read_chat.turn_facts(conv) == []


def test_a_non_text_content_type_is_not_a_visible_turn() -> None:
    conv = _conv(_msg("1", "assistant", content_type="code", parts=["print(1)"]))
    assert read_chat.turn_facts(conv) == []


def test_an_empty_text_assistant_message_is_not_a_visible_turn() -> None:
    """A finished-but-empty interim node must not read as a reply."""
    conv = _conv(_msg("1", "assistant", parts=[]))
    assert read_chat.turn_facts(conv) == []


def test_non_string_parts_are_ignored_but_do_not_crash() -> None:
    conv = _conv(_msg("1", "assistant", parts=[{"type": "image"}]))
    assert read_chat.turn_facts(conv) == []


# ---------------------------------------------------------------------------
# format_turn_fact
# ---------------------------------------------------------------------------


def test_format_turn_fact_renders_every_field() -> None:
    line = read_chat.format_turn_fact(
        1,
        {
            "thinking_effort": "max",
            "resolved_model_slug": "gpt-5-6-thinking",
            "has_search": True,
            "has_citations": False,
        },
    )
    assert line == ("turn 1: effort=max model=gpt-5-6-thinking search=yes citations=no")


def test_format_turn_fact_shows_placeholders_for_missing_values() -> None:
    line = read_chat.format_turn_fact(
        2,
        {
            "thinking_effort": None,
            "resolved_model_slug": None,
            "has_search": False,
            "has_citations": False,
        },
    )
    assert line == "turn 2: effort=(none) model=(unknown) search=no citations=no"


# ---------------------------------------------------------------------------
# read_chat.main -- the --effort output, wired the way
# tests/test_command_mains_2.py wires --text
# ---------------------------------------------------------------------------


class _FakeClient:
    """The client helpers read_chat.main uses regardless of output mode;
    ignores conv the same way tests/test_command_mains_2.py's
    _FakeReadClient does, since turn_facts reads the real conv directly."""

    def __init__(self, messages: list[dict], final: bool, reply: str = "") -> None:
        self.messages = messages
        self.final = final
        self.reply = reply

    def chat_id(self, chat: str) -> str:
        return chat.rsplit("/", 1)[-1]

    def tail_signature(self, _conv: dict) -> tuple[str, int, str, bool]:
        return ("node", len(self.messages), "text", self.final)

    def assistant_text_messages(self, _conv: dict) -> list[dict]:
        return [m for m in self.messages if m["author"]["role"] == "assistant"]

    def latest_reply(self, _conv: dict) -> str:
        return self.reply


class _FakeConvSession:
    def __init__(self, conv: dict) -> None:
        self.conv = conv

    def get_conversation(self, chat_id: str) -> dict:
        assert chat_id == "chat-123"
        return self.conv


def _wire(monkeypatch, client: _FakeClient, conv: dict) -> None:
    monkeypatch.setattr(read_chat, "load_client", lambda: client)
    monkeypatch.setattr(
        read_chat, "open_session", lambda *a, **k: _FakeConvSession(conv)
    )


def test_effort_flag_prints_one_line_per_turn_and_exits_0_when_finished(
    monkeypatch, capsys
) -> None:
    conv = _conv(
        _msg("1", "user", parts=["hi"]),
        _reply(
            "2",
            thinking_effort="max",
            resolved_model_slug="gpt-5-6-thinking",
            search_result_groups=[{"a": 1}],
            citations=[],
        ),
    )
    client = _FakeClient([_msg("1", "user"), _reply("2")], final=True)
    _wire(monkeypatch, client, conv)

    assert read_chat.main(["chat-123", "--effort"]) == 0

    out = capsys.readouterr().out
    assert "turn finished: yes, ready to collect" in out
    assert "turn 1: effort=max model=gpt-5-6-thinking search=yes citations=no" in out


def test_effort_flag_reports_no_turns_yet_when_there_are_none(
    monkeypatch, capsys
) -> None:
    conv = _conv(_msg("1", "user", parts=["hi"]))
    client = _FakeClient([_msg("1", "user")], final=False)
    _wire(monkeypatch, client, conv)

    assert read_chat.main(["chat-123", "--effort"]) == 1

    out = capsys.readouterr().out
    assert "(no assistant turns yet)" in out


def test_effort_flag_does_not_print_the_message_table(monkeypatch, capsys) -> None:
    """--effort replaces the table output, the same way --text does."""
    conv = _conv(_reply("1", thinking_effort="min"))
    client = _FakeClient([_reply("1")], final=True)
    _wire(monkeypatch, client, conv)

    read_chat.main(["chat-123", "--effort"])

    out = capsys.readouterr().out
    assert "role" not in out  # the table header, absent here
    assert "turn 1: effort=min" in out


def test_text_flag_still_takes_priority_over_effort_when_both_are_given(
    monkeypatch, capsys
) -> None:
    """Documents the precedence the if/elif chain gives: --text wins."""
    conv = _conv(_reply("1", thinking_effort="max"))
    client = _FakeClient([_reply("1")], final=True)
    _wire(monkeypatch, client, conv)

    read_chat.main(["chat-123", "--text", "--effort"])

    out = capsys.readouterr().out
    assert "turn 1:" not in out
