#!/usr/bin/env python3
"""Isolated offline tests for parallel ChatGPT worker conversation resolution."""

from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as client  # noqa: E402

PROJECT = "g-p-test-fixture-project"
A = "TASK_ID: HOST-MCP-BROWSER-PARALLEL-20261015-101"
B = "TASK_ID: HOST-MCP-BROWSER-PARALLEL-20261015-102"


def conversation(marker: str, project: str = PROJECT) -> dict:
    return {
        "gizmo_id": project,
        "current_node": "u",
        "mapping": {
            "u": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": [marker + "\nSmoke test."]},
                },
            }
        },
    }


class FakeSession:
    def __init__(self, convs: dict):
        self.convs = convs
        self.calls = 0

    def list_conversations(self, limit: int = 10) -> list:
        self.calls += 1
        return [
            {"id": key, "create_time": "2026-10-15T00:00:00Z"} for key in self.convs
        ]

    def get_conversation(self, key: str) -> dict:
        return self.convs[key]


def resolver(session: FakeSession, marker: str, project: str = PROJECT) -> str:
    return client.resolve_new_conversation_for_marker(
        session,
        marker,
        project=project,
        since=0,
        interval=0,
        sleep=lambda _: None,
        timeout=0.05,
    )


def test_two_new_chats_are_resolved_independently():
    session = FakeSession({"chat_a": conversation(A), "chat_b": conversation(B)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(resolver, session, A)
        second = pool.submit(resolver, session, B)
        assert first.result() == "chat_a"
        assert second.result() == "chat_b"


def test_does_not_assign_another_workers_chat():
    session = FakeSession({"chat_b": conversation(B)})
    with pytest.raises(client.TransportError):
        resolver(session, A)


def test_duplicate_task_id_fails_closed():
    session = FakeSession({"a": conversation(A), "b": conversation(A)})
    with pytest.raises(client.TransportError, match="ambiguous exact TASK_ID"):
        resolver(session, A)


def test_wrong_project_does_not_match():
    session = FakeSession({"a": conversation(A, project="g-p-OTHER")})
    with pytest.raises(client.TransportError):
        resolver(session, A)


def test_legacy_unmarked_prompt_does_not_resolve():
    with pytest.raises(ValueError):
        resolver(FakeSession({"a": conversation("Hello")}), "Hello")


def test_old_timestamp_is_not_matched():
    session = FakeSession({"a": conversation(A)})
    with pytest.raises(client.TransportError):
        client.resolve_new_conversation_for_marker(
            session,
            A,
            project=PROJECT,
            since=4_000_000_000,
            timeout=0.01,
            interval=0,
            sleep=lambda _: None,
        )


def test_temporarily_unreadable_404_is_retried():
    class Once404(FakeSession):
        def get_conversation(self, key: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise client.TransportError("not found", 404)
            return self.convs[key]

    assert resolver(Once404({"a": conversation(A)}), A) == "a"


def test_429_fails_closed_without_retrying_post():
    class RateLimit(FakeSession):
        def get_conversation(self, key: str) -> dict:
            raise client.TransportError("rate-limited", 429)

    with pytest.raises(client.TransportError):
        resolver(RateLimit({"a": conversation(A)}), A)
