"""T0 tests for scripts/api_shapes.py (TESTING.md section 6, P5): the shape
algebra (infer, merge, drift), synthesis and its round trip, the endpoint
registry against the scripts' endpoint constants, recording, the health
check row, and the promotion script."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import api_shapes as sh
import pytest
import refresh_api_shapes
import refresh_read_paths
import test_consistency
from test_fixture_hygiene import leaks_in

UUID = "0ab9cc0a-b708-83ec-9337-00000000000a"
HEX32 = "6ab5bed1195c8191b35529e60f4d1547"


# ---------------------------------------------------------------------------
# string formats and field names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "key", "expected"),
    [
        ("", None, "empty"),
        (UUID, None, "uuid"),
        (f"conversation:{UUID}", None, "prefixed-uuid:conversation"),
        (f"g-p-{HEX32}", None, "g-p-id"),
        (f"g-p-{HEX32}-rp-test-sandbox", None, "g-p-id-slug"),
        ("user-AbCdEf123456", None, "account-id:user"),
        ("org-15FjIoynRqX0", None, "account-id:org"),
        (f"asdk_app_{HEX32}", None, "id:asdk_app_"),
        (f"link_{HEX32}", None, "id:link_"),
        ("2026-09-27T00:32:33.614960Z", None, "datetime"),
        ("2026-09-21T08:59:34+10:00", None, "datetime"),
        ("2026-09-27 01:02", None, "datetime"),
        ("2026-09-27", None, "date"),
        ("https://chatgpt.com/c/x", None, "url"),
        ("wss://ws.chatgpt.com/x", None, "url"),
        ("someone@example.com", None, "email"),
        ("12", None, "numeric"),
        ("-1.5", None, "numeric"),
        ("in_progress", "status", "enum"),
        ("in_progress", "title", "text"),
        ("in progress", "status", "text"),
        ("0123456789abcdef", None, "hexid"),
        ("A" * 30, None, "token"),
        ("hello world", None, "text"),
    ],
)
def test_string_format_names_the_kind_of_string_never_its_content(
    value: str, key: str | None, expected: str
) -> None:
    assert sh.string_format(value, key) == expected


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("items", True),
        ("_private", True),
        ("gpt-5-6-thinking", False),
        ("research-pipeline/SKILL.md", False),
        ("0123456789abcdef", False),
        (f"asdk_app_{HEX32}", False),
        ("1abc", False),
        (UUID, False),
    ],
)
def test_is_field_name_tells_a_field_from_data_used_as_a_key(key, field) -> None:
    assert sh.is_field_name(key) is field


# ---------------------------------------------------------------------------
# infer and merge
# ---------------------------------------------------------------------------


def test_infer_keeps_field_names_types_and_formats_but_no_value() -> None:
    body = {
        "items": [
            {"id": UUID, "title": "my secret plans", "is_archived": False},
            {"id": UUID, "title": "", "is_archived": True, "gizmo_id": None},
        ],
        "total": 2,
        "limit": 1.5,
        "mapping": {UUID: {"role": "user"}},
    }
    shape = sh.infer(body)
    text = json.dumps(shape)
    assert "secret" not in text and UUID not in text
    items = shape["fields"]["items"]["items"]
    assert items["fields"]["title"]["formats"] == ["empty", "text"]
    assert items["fields"]["is_archived"]["booleans"] == [False, True]
    assert items["fields"]["gizmo_id"]["optional"] is True
    assert items["fields"]["id"]["optional"] is False
    assert shape["fields"]["total"]["types"] == ["integer"]
    assert shape["fields"]["limit"]["types"] == ["number"]
    mapping = shape["fields"]["mapping"]
    assert mapping["types"] == ["map"] and "fields" not in mapping
    assert mapping["values"]["fields"]["role"]["enum"] == ["user"]


def test_infer_reports_an_unknown_python_type_as_text_and_a_list_of_nothing():
    assert sh.infer(("a", "b"))["types"] == ["string"]
    assert sh.infer([]) == {"types": ["array"], "items": None}
    assert sh.infer({}) == {"types": ["object"], "fields": {}}


def test_infer_keeps_only_the_type_of_an_opaque_subtree() -> None:
    body = {"actions": [{"params": {"properties": {"workdir": {"type": "string"}}}}]}
    shape = sh.infer(body, opaque=frozenset({"actions[].params"}))
    params = shape["fields"]["actions"]["items"]["fields"]["params"]
    assert params == {"types": ["object"], "opaque": True, "optional": False}
    assert "workdir" not in json.dumps(shape)


def test_merge_unions_types_and_marks_one_sided_fields_optional() -> None:
    a = sh.infer({"x": 1, "y": "a"})
    b = sh.infer({"x": "two", "z": None})
    merged = sh.merge(a, b)
    assert merged["fields"]["x"]["types"] == ["integer", "string"]
    assert merged["fields"]["x"]["optional"] is False
    assert merged["fields"]["y"]["optional"] is True
    assert merged["fields"]["z"]["optional"] is True
    assert sh.merge(None, None) is None
    assert sh.merge(a, None) == a
    assert sh.merge(None, b) == b


def test_merge_does_not_mark_fields_optional_because_of_a_null_observation():
    merged = sh.merge(sh.infer({"a": 1}), sh.infer(None))
    assert merged["types"] == ["null", "object"]
    assert merged["fields"]["a"]["optional"] is False


def test_merge_caps_enum_values_and_keeps_opaque_opaque() -> None:
    nodes = [sh.infer({"status": f"s{n}"}) for n in range(40)]
    merged = nodes[0]
    for node in nodes[1:]:
        merged = sh.merge(merged, node)
    assert len(merged["fields"]["status"]["enum"]) == sh.MAX_ENUM_VALUES
    opaque = {"types": ["object"], "opaque": True, "optional": True}
    assert sh.merge(opaque, sh.infer({"a": 1})) == {
        "types": ["object"],
        "opaque": True,
        "optional": True,
    }


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


def test_drift_names_removed_required_fields_but_not_optional_ones() -> None:
    committed = sh.merge(sh.infer({"a": 1, "b": 2}), sh.infer({"a": 1}))
    assert sh.drift(sh.infer({"b": 2}), committed) == [("a", "removed", "")]
    assert sh.drift(sh.infer({"a": 1}), committed) == []


def test_drift_names_added_fields_and_retyped_ones() -> None:
    committed = sh.infer({"a": 1, "items": [{"id": "x"}]})
    fresh = sh.infer({"a": "one", "items": [{"id": 3, "new": True}]})
    assert sh.drift(fresh, committed) == [
        ("a", "retyped", "number -> string"),
        ("items[].id", "retyped", "string -> number"),
        ("items[].new", "added", ""),
    ]


def test_drift_ignores_null_integer_number_and_object_map_differences() -> None:
    assert sh.drift(sh.infer({"a": None}), sh.infer({"a": "x"})) == []
    assert sh.drift(sh.infer({"a": 1.5}), sh.infer({"a": 1})) == []
    assert sh.drift(sh.infer({"m": {UUID: 1}}), sh.infer({"m": {"k": 1}})) == []


def test_drift_follows_maps_and_stops_at_opaque_nodes_and_root_retypes() -> None:
    committed = sh.infer({"m": {UUID: {"a": 1}}})
    assert sh.drift(sh.infer({"m": {UUID: {}}}), committed) == [
        ("m{}.a", "removed", "")
    ]
    opaque = {"types": ["object"], "opaque": True}
    assert sh.drift(sh.infer({"x": 1}), opaque) == []
    assert sh.drift(sh.infer([1]), sh.infer({"a": 1})) == [
        ("(root)", "retyped", "object -> array")
    ]


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(sh.load(sh.COMMITTED)))
def test_a_synthesized_payload_has_exactly_the_recorded_shape(name: str) -> None:
    """The round trip that makes the contract tests mean something: infer
    the shape of what synthesize built from a shape, and nothing is missing
    or of another type."""
    shape = sh.load(sh.COMMITTED)[name]
    endpoint = sh.BY_NAME[name]
    payload = sh.synthesize(shape)
    again = sh.infer(payload, opaque=endpoint.opaque)
    broken = [d for d in sh.drift(again, shape) if d[1] != "added"]
    assert broken == [], broken[:5]
    assert leaks_in(json.dumps(payload)) == []


@pytest.mark.parametrize(
    ("value", "key"),
    [
        (UUID, None),
        (f"conversation:{UUID}", None),
        (f"g-p-{HEX32}", None),
        (f"g-p-{HEX32}-slug", None),
        ("user-AbCdEf123456", None),
        (f"asdk_app_{HEX32}", None),
        ("2026-09-27T00:32:33Z", None),
        ("2026-09-27", None),
        ("https://x.y/z", None),
        ("a@b.co", None),
        ("12", None),
        ("in_progress", "status"),
        ("0123456789abcdef", None),
        ("A" * 30, None),
        ("free text", "title"),
        ("", None),
    ],
)
def test_every_format_synthesizes_a_value_of_the_same_format(value, key) -> None:
    fmt = sh.string_format(value, key)
    synthesized = sh.synthesize(
        {"types": ["string"], "formats": [fmt], "enum": [value]}
    )
    assert sh.string_format(synthesized, key) == fmt, synthesized


def test_synthesize_ends_paging_and_honours_preferences() -> None:
    shape = sh.infer({"cursor": "abc", "items": [{"role": "user", "on": False}]})
    shape = sh.merge(shape, sh.infer({"cursor": None, "items": []}))
    body = sh.synthesize(shape, prefer={"role": "assistant", "on": True})
    assert body["cursor"] is None
    assert [i["role"] for i in body["items"]] == ["assistant", "assistant"]
    assert [i["on"] for i in body["items"]] == [True, True]
    plain = sh.synthesize(shape)
    assert [i["role"] for i in plain["items"]] == ["user", "user"]


def test_synthesize_builds_opaque_nodes_empty() -> None:
    assert sh.synthesize({"types": ["array"], "opaque": True}) == []
    assert sh.synthesize({"types": ["object"], "opaque": True}) == {}


def test_synthesize_links_a_conversation_tree() -> None:
    shape = sh.infer(
        {
            "current_node": UUID,
            "mapping": {UUID: {"id": UUID, "parent": None, "children": [UUID]}},
        }
    )
    body = sh.synthesize(shape)
    keys = list(body["mapping"])
    assert body["current_node"] == keys[-1]
    assert body["mapping"][keys[0]]["parent"] is None
    assert body["mapping"][keys[0]]["children"] == [keys[1]]
    assert body["mapping"][keys[1]]["parent"] == keys[0]
    assert body["mapping"][keys[1]]["id"] == keys[1]


def test_tracked_dicts_note_what_code_reads() -> None:
    log = sh.ReadLog()
    shape = sh.infer({"a": {"b": 1}, "m": {UUID: {"c": 2}}, "l": [{"d": 3}]})
    body = sh.synthesize(shape, log, "ep")
    _ = body["a"].get("b")
    _ = "zzz" in body
    for value in body["m"].values():
        _ = value["c"]
    _ = body["m"].get("any-key")
    _ = body["l"][0]["d"]
    assert log.as_json() == {
        "ep": ["a", "a.b", "l", "l[].d", "m", "m{}", "m{}.c", "zzz"]
    }


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

EXCUSED = {
    "/backend-api/aip/connectors/links/noauth": "a write: connects an app",
    "/backend-api/aip/connectors/mcp": "a write: creates an app",
    "/backend-api/aip/connectors/<id>": "a write: deletes an app",
    "/backend-api/aip/connectors/links/<id>": "a write: deletes or updates a link",
    "/backend-api/projects": "a write: creates a project",
    "/backend-api/projects/<id>": "a write: updates a project",
    "/backend-api/hazelnuts/<id>": "a write: deletes a skill",
    "/backend-api/aip/connectors/mcp/refresh_actions": (
        "a refresh: makes ChatGPT re-read a server's tools (refresh_connector.py); "
        "done by hand and in the connector acceptance run"
    ),
    "/backend-api/ecosystem/call_mcp": (
        "Deep research's MCP calls answer only for a conversation that hosts "
        "a research: covered by the manual acceptance run"
    ),
    "/backend-api/sentinel/chat-requirements": (
        "posted by probe_send_gates through its own request, not the "
        "session; the daily send gates check reads its three fields"
    ),
    "/v1/tunnels": "the Platform API, a different credential",
}


def test_every_endpoint_constant_is_recorded_or_excused_by_name() -> None:
    registered = {endpoint.folded for endpoint in sh.ENDPOINTS}
    missing = [
        f"{label} ({path})"
        for label, path in test_consistency._endpoint_constants()
        if path not in registered and path not in EXCUSED
    ]
    assert missing == [], "register in api_shapes.ENDPOINTS or excuse: " + ", ".join(
        missing
    )


def test_every_excuse_and_every_registered_endpoint_names_a_real_constant() -> None:
    constants = {path for _label, path in test_consistency._endpoint_constants()}
    assert sorted(set(EXCUSED) - constants) == []
    assert sorted(e.folded for e in sh.ENDPOINTS if e.folded not in constants) == []


@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("GET", "/backend-api/conversations?offset=0&limit=3", "conversations"),
        ("GET", f"/backend-api/conversation/{UUID}", "conversation"),
        ("PATCH", f"/backend-api/conversation/{UUID}", None),
        ("GET", f"/backend-api/conversations/{UUID}", "conversation_widgets"),
        ("GET", "/backend-api/gizmos/snorlax/sidebar?limit=5", "sidebar"),
        ("GET", f"/backend-api/gizmos/g-p-{HEX32}", "gizmo"),
        (
            "GET",
            f"/backend-api/gizmos/g-p-{HEX32}/conversations?cursor=0",
            "gizmo_conversations",
        ),
        ("post", "/backend-api/global/search", "global_search"),
        ("GET", "/backend-api/global/search", None),
        ("GET", "/backend-api/nothing", None),
    ],
)
def test_endpoint_for_matches_method_and_path_without_query(method, path, name):
    endpoint = sh.endpoint_for(method, path)
    assert (endpoint.name if endpoint else None) == name


def test_fetch_request_needs_its_ids_and_builds_the_read_posts() -> None:
    by = sh.BY_NAME
    assert sh.fetch_request(by["conversation"], {}) is None
    assert sh.fetch_request(by["conversation"], {"conversation_id": UUID}) == (
        f"/backend-api/conversation/{UUID}",
        None,
    )
    assert sh.fetch_request(by["gizmo"], {}) is None
    assert (
        sh.fetch_request(by["gizmo"], {"gizmo_id": "g"})[0] == "/backend-api/gizmos/g"
    )
    assert sh.fetch_request(by["connector_links"], {}) is None
    assert sh.fetch_request(by["connector_links"], {"user_id": "u"})[1] == {
        "principals": [{"type": "USER", "id": "u"}]
    }
    assert sh.fetch_request(by["connector_batch"], {}) is None
    ids = [f"asdk_app_{n}" for n in range(8)]
    assert sh.fetch_request(by["connector_batch"], {"connector_ids": ids})[1] == {
        "connector_ids": ids[:5],
        "include_actions": True,
    }
    assert sh.fetch_request(by["global_search"], {})[1]["limit"] == 5
    assert sh.fetch_request(by["me"], {}) == ("/backend-api/me", None)
    unfetchable = sh.Endpoint("x", "GET", "/backend-api/x")
    assert sh.fetch_request(unfetchable, {}) is None


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


class _Inner:
    """A chatgpt_session.Session stand-in: scripted replies by path."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Any, dict]] = []
        self.user_id = "user-XXXXXXXX"

    def call(self, path: str, method: str = "GET", payload: Any = None, **kw: Any):
        self.calls.append((method, path, payload, kw))
        reply = self.replies.get(path.split("?", 1)[0], (404, {"detail": "x"}))
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_the_recording_session_observes_every_call_and_passes_attributes():
    recording = sh.Recording()
    inner = _Inner({"/backend-api/me": (200, {"id": "user-AbCdEf123456"})})
    session = sh.RecordingSession(inner, recording)
    assert session.call("/backend-api/me", retries=1) == (
        200,
        {"id": "user-AbCdEf123456"},
    )
    assert inner.calls[0][3] == {"retries": 1}
    assert recording.shapes["me"]["fields"]["id"]["formats"] == ["account-id:user"]
    assert session.user_id == "user-XXXXXXXX"
    with pytest.raises(AttributeError):
        _ = session._secret
    session.call("/backend-api/me", raw=True)  # text, not a JSON document
    session.call("/backend-api/unknown")
    assert set(recording.shapes) == {"me"}


def test_observe_notes_failures_and_forgets_them_after_a_success() -> None:
    recording = sh.Recording()
    recording.observe("GET", "/backend-api/pins", 403, {"detail": "x"})
    assert recording.errors == {"pins": "HTTP 403"}
    recording.observe("GET", "/backend-api/pins", 200, "not json")
    assert recording.errors == {"pins": "HTTP 200"}
    recording.observe("GET", "/backend-api/pins", 200, [])
    assert recording.errors == {} and "pins" in recording.shapes


def test_context_takes_the_newest_conversation_and_the_app_connectors() -> None:
    recording = sh.Recording()
    recording.observe(
        "GET",
        "/backend-api/conversations",
        200,
        {"items": [{"title": "no id"}, {"id": UUID}, {"id": "later"}]},
    )
    recording.observe(
        "POST",
        "/backend-api/aip/connectors/links/list_accessible",
        200,
        {"links": [{"connector_id": f"asdk_app_{HEX32}"}, {"connector_id": "x"}, 7]},
    )
    context = recording.context({"gizmo_id": "g"})
    assert context == {
        "gizmo_id": "g",
        "conversation_id": UUID,
        "connector_ids": [f"asdk_app_{HEX32}"],
    }


def test_fill_requests_what_the_run_did_not_read_and_notes_what_it_cannot():
    recording = sh.Recording()
    replies: dict[str, Any] = {
        e.template.split("{", 1)[0].rstrip("/"): (200, {"ok": True})
        for e in sh.ENDPOINTS
        if not e.needs
    }
    replies["/backend-api/conversations"] = (200, {"items": [{"id": UUID}]})
    replies[f"/backend-api/conversation/{UUID}"] = (200, {"mapping": {}})
    replies[f"/backend-api/conversations/{UUID}"] = RuntimeError("boom")
    replies["/backend-api/aip/connectors/links/list_accessible"] = (200, {"links": []})
    inner = _Inner(replies)
    session = sh.RecordingSession(inner, recording)
    session.call("/backend-api/me")  # the run read this one already
    requested = sh.fill(session, recording, {"user_id": "u"})
    assert "me" not in requested
    assert "conversation" in requested and "conversation_widgets" in requested
    assert recording.errors["conversation_widgets"] == "boom"
    assert recording.errors["gizmo"] == "not requested: no id to ask for"
    assert recording.errors["connector_batch"] == "not requested: no id to ask for"
    assert "conversation" in recording.shapes


def test_write_and_load_round_trip_with_an_index(tmp_path: Path) -> None:
    recording = sh.Recording()
    recording.observe("GET", "/backend-api/pins", 200, {"items": []})
    recording.errors["me"] = "HTTP 403"
    out = sh.write(recording, tmp_path / "http")
    assert sh.load(out) == {"pins": recording.shapes["pins"]}
    index = json.loads((out / "index.json").read_text())
    assert index["recorded"] == ["pins"] and index["not_recorded"] == {"me": "HTTP 403"}
    assert sh.load(tmp_path / "absent") == {}


def test_read_paths_file_and_the_read_rule(tmp_path: Path) -> None:
    assert sh.read_paths(tmp_path / "absent.json") is None
    (tmp_path / "r.json").write_text('{"ep": ["items[].id", "m{}.a"]}')
    assert sh.read_paths(tmp_path / "r.json") == {"ep": {"items[].id", "m{}.a"}}
    assert sh._is_read("items", {"items[].id"})
    assert sh._is_read("items[].id", {"items[].id"})
    assert sh._is_read("m", {"m{}.a"})
    assert not sh._is_read("item", {"items[].id"})


def test_compare_splits_breaking_from_other_changes() -> None:
    committed = {"ep": sh.infer({"read": 1, "unread": 1}), "gone": sh.infer({})}
    fresh = {"ep": sh.infer({"new": 1}), "brand_new": sh.infer({})}
    report = sh.compare(fresh, committed, {"ep": {"read"}})
    assert report == {
        "breaking": ["ep: read removed"],
        "other": ["ep: unread removed"],
        "added": ["ep: new added"],
        "unknown": ["brand_new"],
    }
    everything = sh.compare(fresh, committed, None)
    assert everything["breaking"] == ["ep: read removed", "ep: unread removed"]


# ---------------------------------------------------------------------------
# the health check row
# ---------------------------------------------------------------------------


def _health_row(tmp_path: Path, replies: dict[str, Any], committed: dict, read: dict):
    import health

    committed_dir = tmp_path / "committed"
    committed_dir.mkdir()
    for name, shape in committed.items():
        (committed_dir / f"{name}{sh.SHAPE_SUFFIX}").write_text(json.dumps(shape))
    (tmp_path / "read.json").write_text(json.dumps(read))
    recording = sh.Recording()
    session = sh.RecordingSession(_Inner(replies), recording)
    return health.api_shapes_check(
        session,
        recording,
        str(tmp_path / "out"),
        {"id": "", "name": ""},
        committed_dir,
        tmp_path / "read.json",
    )


def test_the_health_row_warns_only_for_a_field_the_skill_reads(tmp_path) -> None:
    replies = {"/backend-api/me": (200, {"email": "user@example.invalid"})}
    committed = {"me": sh.infer({"id": "user-AbCdEf123456", "email": "a@b.co"})}
    row = _health_row(tmp_path, replies, committed, {"me": ["id"]})
    assert row["state"] == "warn"
    assert "differs from the fixture" in row["detail"]
    assert "me: id removed" in row["detail"]
    assert "make contract SHAPES=" in row["fix"]
    assert (tmp_path / "out" / f"me{sh.SHAPE_SUFFIX}").is_file()


def test_the_health_row_is_ok_and_notes_the_rest(tmp_path) -> None:
    replies = {
        "/backend-api/me": (200, {"id": "user-AbCdEf123456", "extra": 1}),
        "/backend-api/pins": (200, {"items": []}),
    }
    committed = {"me": sh.infer({"id": "user-AbCdEf123456", "old": 1})}
    row = _health_row(tmp_path, replies, committed, {"me": ["id"]})
    assert row["state"] == "ok"
    assert "no field the skill reads changed" in row["detail"]
    assert "1 other difference(s)" in row["detail"]
    assert "1 new field(s)" in row["detail"]
    assert "no committed shape yet for pins" in row["detail"]
    assert "not recorded:" in row["detail"]


def test_the_health_row_warns_when_nothing_was_recorded(tmp_path) -> None:
    row = _health_row(tmp_path, {}, {}, {})
    assert row["state"] == "warn"
    assert row["detail"].startswith("no API shape recorded")


def test_the_health_row_warns_when_the_shapes_cannot_be_written(tmp_path) -> None:
    import health

    blocker = tmp_path / "file"
    blocker.write_text("x")
    recording = sh.Recording()
    session = sh.RecordingSession(_Inner({}), recording)
    row = health.api_shapes_check(session, recording, str(blocker / "out"), {})
    assert row["state"] == "warn" and row["detail"].startswith("shapes not written")


# ---------------------------------------------------------------------------
# promotion and the read-paths writer
# ---------------------------------------------------------------------------


def _shapes_dir(path: Path, shapes: dict[str, Any]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, shape in shapes.items():
        (path / f"{name}{sh.SHAPE_SUFFIX}").write_text(json.dumps(shape))
    return path


def test_promote_merges_by_default_and_replaces_on_request(tmp_path) -> None:
    dest = _shapes_dir(tmp_path / "dest", {"me": sh.infer({"a": 1, "b": 1})})
    src = _shapes_dir(tmp_path / "src", {"me": sh.infer({"a": 1})})
    assert refresh_api_shapes.promote(src, dest) == {"me": "written"}
    merged = sh.load(dest)["me"]
    assert merged["fields"]["b"]["optional"] is True
    assert refresh_api_shapes.promote(src, dest) == {"me": "unchanged"}
    assert refresh_api_shapes.promote(src, dest, replace=True) == {"me": "written"}
    assert "b" not in sh.load(dest)["me"]["fields"]


def test_promote_refuses_unregistered_names_leaks_and_empty_sources(tmp_path):
    dest = tmp_path / "dest"
    with pytest.raises(FileNotFoundError):
        refresh_api_shapes.promote(tmp_path / "nothing", dest)
    bad = _shapes_dir(tmp_path / "bad", {"nope": sh.infer({})})
    with pytest.raises(ValueError, match="not a registered endpoint"):
        refresh_api_shapes.promote(bad, dest)
    leak = _shapes_dir(
        tmp_path / "leak", {"me": {"types": ["string"], "enum": ["grammy"]}}
    )
    with pytest.raises(ValueError, match="account holder"):
        refresh_api_shapes.promote(leak, dest)
    assert not dest.exists()


def test_promote_main_and_default_source(tmp_path, monkeypatch, capsys) -> None:
    src = _shapes_dir(tmp_path / "src", {"me": sh.infer({"a": 1})})
    assert refresh_api_shapes.main([str(src), "--dest", str(tmp_path / "d")]) == 0
    assert "me: written" in capsys.readouterr().out
    assert (
        refresh_api_shapes.main([str(tmp_path / "none"), "--dest", str(tmp_path)]) == 1
    )
    monkeypatch.setattr(refresh_api_shapes, "STATE", tmp_path)
    (tmp_path / "last-run-id").write_text("run-1\n")
    assert refresh_api_shapes.default_source() == tmp_path / "runs" / "run-1" / "http"


def test_refresh_read_paths_writes_then_reports_unchanged(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "read_paths.json"
    monkeypatch.setattr(sh, "READ_PATHS_FILE", target)
    monkeypatch.setattr(
        refresh_read_paths.api_contract, "read_paths", lambda s: {"a": ["b"]}
    )
    assert refresh_read_paths.main() == 0
    assert json.loads(target.read_text()) == {"a": ["b"]}
    assert "written" in capsys.readouterr().out
    assert refresh_read_paths.main() == 0
    assert "unchanged" in capsys.readouterr().out
