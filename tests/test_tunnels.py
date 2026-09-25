"""Platform tunnel lifecycle and credential boundaries, without network access."""

import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace

import manage_tunnels as tunnels
import pytest

TID = "tunnel_" + "a" * 32
RECORD = {
    "id": TID,
    "name": "rp-test tunnel",
    "description": "acceptance",
    "organization_ids": ["org-test"],
    "workspace_ids": [],
}
CREATE = [
    "create",
    RECORD["name"],
    "--organization",
    "org-test",
    "--description",
    "acceptance",
]


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch, tmp_path):
    for key in (
        "OPENAI_ADMIN_KEY",
        "OPENAI_DASHBOARD_TOKEN",
        "OPENAI_DASHBOARD_TOKEN_CMD",
        "CONTROL_PLANE_API_KEY",
        "TUNNEL_ORG",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tunnels, "CONFIG", tmp_path)


class API:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def call(self, path, method="GET", body=None):
        self.calls.append((method, path, body))
        return next(self.replies)


def api(monkeypatch, *replies):
    fake = API(replies)
    monkeypatch.setattr(tunnels, "credential", lambda args: ("synthetic", "admin"))
    monkeypatch.setattr(tunnels, "PlatformSession", lambda *args: fake)
    return fake


def test_create_preview_needs_no_credential_or_network(capsys):
    assert tunnels.main([*CREATE, "--workspace", "ws-test", "--dry-run"]) == 0
    request = json.loads(capsys.readouterr().out)
    assert request["body"]["workspace_ids"] == ["ws-test"]
    assert request["method"] == "POST"


@pytest.mark.parametrize("id_only", [False, True])
def test_created_tunnel_is_read_back_before_success(monkeypatch, capsys, id_only):
    fake = api(monkeypatch, (201, RECORD), (200, RECORD))
    assert tunnels.main(CREATE + (["--id-only"] if id_only else [])) == 0
    out = capsys.readouterr()
    assert (out.out.strip() if id_only else json.loads(out.out)["id"]) == TID
    assert [call[0] for call in fake.calls] == ["POST", "GET"]
    assert "25–30" in out.err


@pytest.mark.parametrize("reply", [(403, None), (200, {}), (200, [])])
def test_create_failure_is_not_reported_as_success(monkeypatch, reply):
    api(monkeypatch, reply)
    assert tunnels.main(CREATE) == 1


def test_created_id_survives_a_failed_readback_for_manual_recovery(monkeypatch, capsys):
    fake = api(monkeypatch, (200, RECORD), (200, {**RECORD, "name": "wrong"}))
    assert tunnels.main(CREATE) == 1
    assert TID in capsys.readouterr().err
    assert len(fake.calls) == 2  # Never retry a possibly successful create.


@pytest.mark.parametrize(
    ("operation", "flag"), [("delete", "--confirm"), ("update", "--apply")]
)
def test_name_guard_refuses_mutation(monkeypatch, operation, flag):
    fake = api(monkeypatch, (200, RECORD))
    args = [operation, TID, "--expect-name", "a different tunnel", flag]
    if operation == "update":
        args += ["--name", "renamed"]
    assert tunnels.main(args) == 2
    assert [call[0] for call in fake.calls] == ["GET"]


@pytest.mark.parametrize("operation", ["delete", "update"])
def test_update_and_delete_default_to_read_only_preview(monkeypatch, capsys, operation):
    fake = api(monkeypatch, (200, RECORD))
    args = [operation, TID] + (["--name", "new name"] if operation == "update" else [])
    assert tunnels.main(args) == 0
    assert [call[0] for call in fake.calls] == ["GET"]
    assert json.loads(capsys.readouterr().out)["current_name"] == RECORD["name"]


def test_update_preserves_scope_by_sending_only_selected_fields(monkeypatch):
    fake = api(
        monkeypatch, (200, RECORD), (200, {}), (200, {**RECORD, "name": "renamed"})
    )
    assert tunnels.main(["update", TID, "--name", "renamed", "--apply"]) == 0
    assert fake.calls[1] == ("POST", f"/v1/tunnels/{TID}", {"name": "renamed"})


@pytest.mark.parametrize("status", [200, 204])
def test_delete_requires_confirm_and_verifies_absence(monkeypatch, capsys, status):
    fake = api(monkeypatch, (200, RECORD), (status, None), (404, None))
    assert tunnels.main(["delete", TID, "--confirm"]) == 0
    assert [c[0] for c in fake.calls] == ["GET", "DELETE", "GET"]
    assert json.loads(capsys.readouterr().out)["verified"] is True


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_failed_mutation_is_not_reported_as_success(monkeypatch, operation):
    api(monkeypatch, (200, RECORD), (403, None))
    args = [operation, TID] + (
        ["--name", "new", "--apply"] if operation == "update" else ["--confirm"]
    )
    assert tunnels.main(args) == 1


@pytest.mark.parametrize("last", [(200, RECORD), (403, None)])
def test_delete_does_not_claim_absence_without_404(monkeypatch, last):
    api(monkeypatch, (200, RECORD), (204, None), last)
    assert tunnels.main(["delete", TID, "--confirm"]) == 1


def test_get_checks_returned_identity(monkeypatch):
    api(monkeypatch, (200, {**RECORD, "id": "tunnel_" + "b" * 32}))
    assert tunnels.main(["get", TID]) == 1


def test_get_metadata_and_list_preserve_server_paging(monkeypatch, capsys):
    page = {"tunnels": [RECORD], "cursor": "opaque-next"}
    fake = api(monkeypatch, (200, RECORD), (200, page))
    assert tunnels.main(["get", TID]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == TID
    monkeypatch.setenv("TUNNEL_ORG", "org-test")
    assert tunnels.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out) == page
    assert fake.calls[-1][1] == "/v1/tunnels?organization_id=org-test"


@pytest.mark.parametrize("reply", [(403, None), (200, "invalid")])
def test_failed_list_is_an_error(monkeypatch, reply):
    api(monkeypatch, reply)
    assert tunnels.main(["list", "--organization", "org-test"]) == 1


@pytest.mark.parametrize(
    "args", [["list"], ["create", "x"], ["update", TID], ["get", "../bad"]]
)
def test_local_argument_errors_never_request_credentials(monkeypatch, args):
    monkeypatch.setattr(
        tunnels, "credential", lambda args: pytest.fail("credential read")
    )
    assert tunnels.main(args) == 2


def test_missing_credentials_exits_two_without_disclosing_environment(capsys):
    assert tunnels.main(["list", "--organization", "org-test"]) == 2
    assert "no suitable Platform credential" in capsys.readouterr().err


def test_owner_only_env_file_is_read_without_executing_shell(tmp_path):
    path = tmp_path / "admin.env"
    path.write_text(
        "# comment\nIGNORED=value\n"
        "export OPENAI_ADMIN_KEY='literal-$(not-executed)' # comment\n"
    )
    path.chmod(0o600)
    assert tunnels.env_file_value(path, "OPENAI_ADMIN_KEY") == "literal-$(not-executed)"
    assert tunnels.env_file_value(path, "MISSING") == ""
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        tunnels.env_file_value(path, "OPENAI_ADMIN_KEY")


def test_credential_file_from_another_owner_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "admin.env"
    path.write_text("OPENAI_ADMIN_KEY=synthetic")
    path.chmod(0o600)
    monkeypatch.setattr(tunnels.os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError):
        tunnels.env_file_value(path, "OPENAI_ADMIN_KEY")


def test_explicit_credential_modes_and_auto_priority(monkeypatch):
    monkeypatch.setenv("OPENAI_DASHBOARD_TOKEN", "dashboard-fake")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-fake")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "runtime-fake")
    args = tunnels.parser().parse_args(["get", TID])
    assert tunnels.credential(args) == ("dashboard-fake", "dashboard")
    args.auth = "admin"
    assert tunnels.credential(args) == ("admin-fake", "admin")
    args.auth = "runtime"
    assert tunnels.credential(args) == ("runtime-fake", "runtime")
    args.command = "delete"
    with pytest.raises(ValueError):
        tunnels.credential(args)


@pytest.mark.parametrize(
    ("mode", "key", "file_name"),
    [
        ("admin", "OPENAI_ADMIN_KEY", "admin.env"),
        ("runtime", "CONTROL_PLANE_API_KEY", "binnacle-tunnel.env"),
    ],
)
def test_private_file_credential_source(tmp_path, mode, key, file_name):
    path = tmp_path / file_name
    path.write_text(f'{key}="synthetic"\n')
    path.chmod(0o600)
    args = tunnels.parser().parse_args(["get", TID, "--auth", mode])
    assert tunnels.credential(args) == ("synthetic", mode)


@pytest.mark.parametrize("result", ["ok", "failure", "timeout"])
def test_dashboard_token_provider_hides_credential_and_stderr(
    monkeypatch, capsys, result
):
    monkeypatch.setenv("OPENAI_DASHBOARD_TOKEN_CMD", "configured-provider")

    def provider(*args, **kwargs):
        assert kwargs["timeout"] == 30 and kwargs["capture_output"]
        if result == "timeout":
            raise subprocess.TimeoutExpired("provider", 30)
        return SimpleNamespace(
            returncode=0 if result == "ok" else 1,
            stdout="secret-fake\n",
            stderr="secret-fake",
        )

    monkeypatch.setattr(tunnels.subprocess, "run", provider)
    args = tunnels.parser().parse_args(["get", TID, "--auth", "dashboard"])
    if result == "ok":
        assert tunnels.credential(args) == ("secret-fake", "dashboard")
    else:
        with pytest.raises(ValueError) as error:
            tunnels.credential(args)
        assert "secret-fake" not in str(error.value)
    assert "secret-fake" not in str(capsys.readouterr())


@pytest.mark.parametrize("token", ["", "a\nb", "a\rb"])
def test_invalid_credential_is_not_interpolated_into_errors(token):
    with pytest.raises(ValueError, match="Platform credential"):
        tunnels.PlatformSession(token)


def test_http_request_targets_only_official_origin_and_never_follows_redirects(
    monkeypatch,
):
    session = tunnels.PlatformSession("synthetic", "org-test")
    seen = []

    class Response(io.BytesIO):
        status = 200

    def open_request(request, timeout):
        seen.append(request)
        assert timeout == 30
        return Response(json.dumps(RECORD).encode())

    monkeypatch.setattr(session.opener, "open", open_request)
    status, body = session.call("/v1/tunnels", "POST", {"name": "test"})
    assert status == 200 and body == RECORD
    assert seen[0].full_url == "https://api.openai.com/v1/tunnels"
    assert json.loads(seen[0].data) == {"name": "test"}
    assert (
        tunnels.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://unrelated.invalid"
        )
        is None
    )
    with pytest.raises(ValueError):
        session.call("https://unrelated.invalid")


@pytest.mark.parametrize("kind", ["http", "network", "timeout", "malformed", "empty"])
def test_transport_errors_do_not_echo_sensitive_server_text(monkeypatch, kind):
    session = tunnels.PlatformSession("secret-fake")

    class Response(io.BytesIO):
        status = 204 if kind == "empty" else 200

    def open_request(*args, **kwargs):
        if kind == "http":
            raise urllib.error.HTTPError(
                "https://api.openai.com",
                403,
                "secret-fake",
                {},
                io.BytesIO(b"secret-fake"),
            )
        if kind == "network":
            raise urllib.error.URLError("secret-fake")
        if kind == "timeout":
            raise TimeoutError("secret-fake")
        return Response(b"" if kind == "empty" else b"secret-fake")

    monkeypatch.setattr(session.opener, "open", open_request)
    if kind in ("http", "empty"):
        assert session.call("/v1/tunnels") == (403 if kind == "http" else 204, None)
    else:
        with pytest.raises(tunnels.TunnelError) as error:
            session.call("/v1/tunnels")
        assert "secret-fake" not in str(error.value)
