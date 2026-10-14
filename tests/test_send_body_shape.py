"""The send body's shape (TESTING.md section 6; 2026-10-01 evening): the
``f/conversation`` POST a send makes is a request, so ``api_shapes`` records
it beside the DOM fixtures (``tests/fixtures/send/send_body.shape.json``,
never under ``http/``, whose every shape must be a registered endpoint),
the weekly send re-records it, and the wrapper compares the two: a field
the client rewrites or the server reads going missing is breaking, a new
field is information. The committed shape was taken from a recorded send
with the picker on a thinking preset, so ``thinking_effort`` is required
there -- a send made at "Instant" would be reported, correctly, as a body
without an effort.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_shapes  # noqa: E402

BODY = {
    "action": "next",
    "model": "gpt-5-6-thinking",
    "thinking_effort": "extended",
    "parent_message_id": "client-created-root",
    "conversation_mode": {"kind": "gizmo_interaction", "gizmo_id": "g-p-" + "0" * 32},
    "messages": [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["hello"]},
            "metadata": {},
        }
    ],
    "timezone": "Australia/Sydney",
    "timezone_offset_min": -600,
    "supported_encodings": ["v1"],
}


def test_send_body_shape_keeps_fields_and_types_never_values() -> None:
    shape = api_shapes.send_body_shape(BODY)
    assert shape["types"] == ["object"]
    assert set(shape["fields"]) == set(BODY)
    text = json.dumps(shape)
    assert "hello" not in text and "Australia" not in text and "0" * 32 not in text
    assert shape["fields"]["thinking_effort"]["types"] == ["string"]
    parts = shape["fields"]["messages"]["items"]["fields"]["content"]["fields"]["parts"]
    assert parts["types"] == ["array"]
    assert api_shapes.send_body_shape("not a body")["types"] == ["object"]


def test_write_and_load_a_send_body_shape(tmp_path: Path) -> None:
    path = api_shapes.write_send_body_shape(BODY, tmp_path / "dom")
    assert path.name == "send_body.shape.json"
    loaded = api_shapes.load_send_body_shape(path)
    assert loaded == api_shapes.send_body_shape(BODY)
    assert api_shapes.load_send_body_shape(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("nope", encoding="utf-8")
    assert api_shapes.load_send_body_shape(tmp_path / "bad.json") is None


def test_compare_send_body_sorts_removed_read_fields_from_additions_and_the_rest():
    committed = api_shapes.send_body_shape(BODY)
    changed = dict(BODY)
    del changed["thinking_effort"]  # the client pins it: breaking
    del changed["timezone"]  # nobody reads it: other
    changed["one_off_model_override"] = True  # new: information
    changed["requested_default_model"] = "gpt-5-6-thinking"
    report = api_shapes.compare_send_body(
        api_shapes.send_body_shape(changed), committed
    )
    assert report["breaking"] == ["send_body: thinking_effort removed"]
    assert report["other"] == ["send_body: timezone removed"]
    assert sorted(report["added"]) == [
        "send_body: one_off_model_override added",
        "send_body: requested_default_model added",
    ]
    retyped = dict(BODY)
    retyped["model"] = 7
    report = api_shapes.compare_send_body(
        api_shapes.send_body_shape(retyped), committed
    )
    assert report["breaking"] == ["send_body: model retyped (string -> number)"]
    assert api_shapes.compare_send_body(committed, committed) == {
        "breaking": [],
        "other": [],
        "added": [],
    }


def test_the_committed_send_body_shape_carries_what_the_client_depends_on() -> None:
    shape = api_shapes.load_send_body_shape()
    assert shape is not None, api_shapes.SEND_BODY_FILE
    assert api_shapes.SEND_BODY_FILE.parent.name == "send"
    fields = shape["fields"]
    for name in ("action", "model", "thinking_effort", "messages", "parent_message_id"):
        assert name in fields, name
        assert not fields[name].get("optional"), name
    assert fields["conversation_mode"]["fields"]["kind"]["types"] == ["string"]
    assert "one_off_model_override" not in fields  # a thinking preset's body
    message = shape["fields"]["messages"]["items"]["fields"]
    assert message["content"]["fields"]["parts"]["types"] == ["array"]
