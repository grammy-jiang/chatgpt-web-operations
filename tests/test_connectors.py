"""Connector CLI contracts and failure handling, without account access."""

from types import SimpleNamespace

import connect_connector
import create_connector
import delete_connector
import list_connectors
import pytest

APP = {"id": "asdk_app_test", "name": "rp-test connector", "tunnel_id": "tunnel_test"}
LINK = {
    "id": "link_test",
    "connector_id": APP["id"],
    "name": APP["name"],
    "actions": ["echo"],
    "auth_type": "NONE",
}
PLUGIN = {
    "connector_id": APP["id"],
    "release": {"display_name": APP["name"], "version": "1.0.0"},
    "status": "ENABLED",
    "discoverability": "PRIVATE",
}


class HTTP:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.user_id = "test-principal"

    def call(self, path, method="GET", payload=None):
        self.calls.append((method, path, payload))
        return next(self.replies)


def session_for(monkeypatch, module, *replies):
    http = HTTP(replies)
    monkeypatch.setattr(
        module, "open_session", lambda *_: SimpleNamespace(session=http)
    )
    return http


def forbid_session(monkeypatch, module):
    def fail(*_):
        pytest.fail("invalid arguments or a local preview must not authenticate")

    monkeypatch.setattr(module, "open_session", fail)


@pytest.mark.parametrize("dry", [False, True])
def test_create_success_and_preview(monkeypatch, capsys, dry):
    http = session_for(monkeypatch, create_connector, (200, {"connector": APP}))
    args = ["--name", APP["name"], "--tunnel", "tunnel_test"]
    if dry:
        args.append("--dry-run")
    assert create_connector.main(args) == 0
    assert len(http.calls) == (0 if dry else 1)
    if not dry:
        payload = http.calls[0][2]
        assert payload["auth_request"]["supported_auth"] == []
        assert payload["tunnel_id"] == "tunnel_test"
    assert ("POST" if dry else "created:") in capsys.readouterr().out


def test_create_rejects_invalid_tunnel_without_auth(monkeypatch):
    forbid_session(monkeypatch, create_connector)
    assert create_connector.main(["--name", "test", "--tunnel", "bad"]) == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, {"detail": {"existing_connector_id": APP["id"]}}),
        (409, "conflict"),
        (500, {}),
        (200, {}),
        (200, {"connector": {}}),
    ],
)
def test_create_reports_failed_or_incomplete_response(monkeypatch, status, body):
    session_for(monkeypatch, create_connector, (status, body))
    assert create_connector.main(["--name", "test", "--tunnel", "tunnel_test"]) == 1


def test_connect_preview_includes_privacy_without_auth(monkeypatch, capsys):
    forbid_session(monkeypatch, connect_connector)
    assert (
        connect_connector.main(
            [APP["id"], "--name", "test", "--apps-privacy", "full_access", "--dry-run"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "POST" in output and "PATCH" in output and "full_access" in output


def test_connect_rejects_invalid_app_without_auth(monkeypatch):
    forbid_session(monkeypatch, connect_connector)
    assert connect_connector.main(["bad", "--name", "test"]) == 2


@pytest.mark.parametrize("privacy", [False, True])
def test_connect_success_and_requested_privacy(monkeypatch, privacy):
    http = session_for(
        monkeypatch,
        connect_connector,
        (200, LINK),
        (200, {"link": {"apps_privacy_control": "full_access"}}),
    )
    args = [APP["id"], "--name", APP["name"]]
    if privacy:
        args += ["--apps-privacy", "full_access"]
    assert connect_connector.main(args) == 0
    assert len(http.calls) == (2 if privacy else 1)


@pytest.mark.parametrize("reply", [(500, {}), (200, {}), (200, None)])
def test_connect_failure_is_nonzero(monkeypatch, reply):
    session_for(monkeypatch, connect_connector, reply)
    assert connect_connector.main([APP["id"], "--name", "test"]) == 1


@pytest.mark.parametrize(
    "reply",
    [(500, {}), (200, {}), (200, {"link": {"apps_privacy_control": "wrong"}})],
)
def test_connect_does_not_report_success_when_privacy_failed(monkeypatch, reply):
    session_for(monkeypatch, connect_connector, (200, LINK), reply)
    assert (
        connect_connector.main(
            [APP["id"], "--name", "test", "--apps-privacy", "full_access"]
        )
        == 1
    )


def test_delete_rejects_first_party_id_without_auth(monkeypatch):
    forbid_session(monkeypatch, delete_connector)
    assert delete_connector.main(["connector_first_party", "--confirm"]) == 2


@pytest.mark.parametrize(
    "args", [["link_test"], ["link_test", "--confirm", "--dry-run"]]
)
def test_delete_link_preview_never_deletes(monkeypatch, args):
    http = session_for(monkeypatch, delete_connector)
    assert delete_connector.main(args) == 0
    assert http.calls == []


@pytest.mark.parametrize(("status", "expected"), [(200, 0), (500, 1)])
def test_delete_one_link(monkeypatch, status, expected):
    http = session_for(monkeypatch, delete_connector, (status, {}))
    assert delete_connector.main(["link_test", "--confirm"]) == expected
    assert http.calls[0][:2] == (
        "DELETE",
        "/backend-api/aip/connectors/links/link_test",
    )


def test_delete_app_preview_lists_links_first(monkeypatch, capsys):
    http = session_for(
        monkeypatch,
        delete_connector,
        (200, {"connectors": [APP]}),
        (200, {"links": [LINK, {**LINK, "connector_id": "asdk_app_other"}]}),
    )
    assert delete_connector.main([APP["id"]]) == 0
    assert all(method != "DELETE" for method, *_ in http.calls)
    output = capsys.readouterr().out
    assert output.index("DELETE /backend-api/aip/connectors/links/") < output.index(
        "DELETE /backend-api/aip/connectors/asdk_app_test"
    )


@pytest.mark.parametrize("app_exists", [False, True])
def test_delete_cleans_matching_links_and_then_app(monkeypatch, app_exists):
    http = session_for(
        monkeypatch,
        delete_connector,
        (200, {"connectors": [APP] if app_exists else []}),
        (200, {"links": [LINK]}),
        (200, {}),
        (200, {}),
    )
    assert delete_connector.main([APP["id"], "--confirm"]) == 0
    paths = [path for method, path, _ in http.calls if method == "DELETE"]
    assert paths[0].endswith("/links/link_test")
    assert len(paths) == (2 if app_exists else 1)


def test_failed_link_delete_must_not_uninstall_app(monkeypatch):
    http = session_for(
        monkeypatch,
        delete_connector,
        (200, {"connectors": [APP]}),
        (200, {"links": [LINK]}),
        (500, {}),
    )
    assert delete_connector.main([APP["id"], "--confirm"]) == 1
    assert not any(
        method == "DELETE" and path.endswith(APP["id"])
        for method, path, _ in http.calls
    )


def test_failed_app_delete_is_nonzero(monkeypatch):
    session_for(
        monkeypatch,
        delete_connector,
        (200, {"connectors": [APP]}),
        (200, {"links": []}),
        (500, {}),
    )
    assert delete_connector.main([APP["id"], "--confirm"]) == 1


def test_delete_missing_app_and_links_is_nonzero(monkeypatch):
    session_for(
        monkeypatch, delete_connector, (200, {"connectors": []}), (200, {"links": []})
    )
    assert delete_connector.main([APP["id"], "--confirm"]) == 1


def test_delete_failed_lookup_does_not_mutate(monkeypatch):
    http = session_for(monkeypatch, delete_connector, (500, {}))
    assert delete_connector.main([APP["id"], "--confirm"]) == 1
    assert len(http.calls) == 1


def test_delete_failed_link_listing_does_not_mutate(monkeypatch):
    http = session_for(
        monkeypatch, delete_connector, (200, {"connectors": [APP]}), (500, {})
    )
    with pytest.raises(SystemExit) as exc:
        delete_connector.main([APP["id"], "--confirm"])
    assert exc.value.code == 1
    assert all(method != "DELETE" for method, *_ in http.calls)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("mode", ["list", "tunnels", "detail"])
def test_list_modes_and_json(monkeypatch, capsys, mode, json_output):
    replies = {
        "list": [(200, {"links": [LINK]}), (200, {"plugins": [PLUGIN]})],
        "tunnels": [(200, {"tunnels": [{"id": "tunnel_test", "name": "test"}]})],
        "detail": [(200, {"connectors": [APP]})],
    }
    http = session_for(monkeypatch, list_connectors, *replies[mode])
    args = {
        "list": ["--match", "RP-TEST"],
        "tunnels": ["--tunnels"],
        "detail": ["--detail", APP["id"]],
    }[mode]
    if json_output:
        args += ["--json"]
    assert list_connectors.main(args) == 0
    assert (
        "tunnel_test" if mode == "tunnels" else APP["id"]
    ) in capsys.readouterr().out
    assert all(method in {"GET", "POST"} for method, *_ in http.calls)


@pytest.mark.parametrize("body", [[PLUGIN], {"items": [PLUGIN]}, {"plugins": [PLUGIN]}])
def test_apps_accepts_recorded_list_shapes(monkeypatch, body):
    http = session_for(monkeypatch, list_connectors, (200, body))
    assert list_connectors.apps(SimpleNamespace(session=http))[0]["id"] == APP["id"]


def test_apps_ignores_first_party_and_null_optional_fields(monkeypatch):
    http = session_for(
        monkeypatch,
        list_connectors,
        (
            200,
            {
                "plugins": [
                    {"connector_id": "other"},
                    {"connector_id": APP["id"], "release": None},
                ]
            },
        ),
    )
    assert len(list_connectors.apps(SimpleNamespace(session=http))) == 1


@pytest.mark.parametrize(
    "body", [[{"id": "tunnel_test"}], {"items": [{"id": "tunnel_test"}]}]
)
def test_tunnels_accepts_list_shapes(monkeypatch, body):
    http = session_for(monkeypatch, list_connectors, (200, body))
    assert (
        list_connectors.tunnels(SimpleNamespace(session=http))[0]["id"] == "tunnel_test"
    )


def test_failed_listing_exits_nonzero(monkeypatch):
    session_for(monkeypatch, list_connectors, (503, {}))
    with pytest.raises(SystemExit) as exc:
        list_connectors.main([])
    assert exc.value.code == 1
