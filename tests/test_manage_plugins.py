"""Offline tests (T0) for ``manage_plugins.py``: archives, resolution, every
write's read-back, the previews that never write, and the exit codes.

A ``FakeSession`` answers by method and path; the blob PUT is replaced by a
fake ``urlopen``, so nothing here reaches the network (the conftest socket
guard would fail the test if it did).
"""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import manage_plugins as mp
import pytest

PID = "Plugin_" + "a" * 32
OTHER = "Plugin_" + "b" * 32
SIGNED = "https://blob.oaiusercontent.com/files/x/raw?se=1&sig=SECRET"
SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def plugin(
    pid: str = PID,
    name: str = "rp-test-plugin",
    version: str = "0.0.1",
    display: str = "rp-test plugin",
    skills: tuple[str, ...] = ("s1",),
) -> dict:
    return {
        "id": pid,
        "name": name,
        "status": "ENABLED",
        "creator_account_user_id": "user-SECRET",
        "release": {
            "version": version,
            "display_name": display,
            "skills": [{"name": s} for s in skills],
            "bundle_download_url": "https://example.invalid/signed",
        },
    }


class FakeSession:
    """``session.session``: answers from ``routes`` by (method, bare path)."""

    def __init__(self) -> None:
        self.own: list[dict] = [plugin()]
        self.installed: dict[str, dict] = {}
        self.detail: dict[str, dict] = {PID: plugin()}
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.archive_bytes = _zip_bytes(
            {"plugin.json": {"name": "rp-test-plugin", "version": "0.0.1"}}
        )

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int | None = None,
        binary: bool = False,
    ) -> tuple[int, Any]:
        self.calls.append((method, path, payload))
        bare = path.split("?", 1)[0]
        if (method, bare) in self.answers:
            return self.answers[(method, bare)]
        if method == "GET" and bare == "/backend-api/ps/plugins/list":
            return 200, {"plugins": self.own, "pagination": {"next_page_token": None}}
        if method == "GET" and bare == "/backend-api/ps/plugins/installed":
            return 200, {"plugins": list(self.installed.values())}
        if method == "GET" and bare.endswith("/archive"):
            return 200, self.archive_bytes
        if method == "GET" and bare.startswith("/backend-api/ps/plugins/"):
            pid = bare.rsplit("/", 1)[1]
            return (
                (200, self.detail[pid])
                if pid in self.detail
                else (404, {"error": "nf"})
            )
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


def _zip_bytes(files: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            data = json.dumps(content) if isinstance(content, dict) else content
            zf.writestr(name, data)
    return buf.getvalue()


def make_zip(tmp_path: Path, files: dict[str, Any], name: str = "p.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(_zip_bytes(files))
    return path


def good_zip(
    tmp_path: Path, version: str = "0.0.2", name: str = "rp-test-plugin"
) -> Path:
    return make_zip(
        tmp_path,
        {
            "rp-test-plugin/plugin.json": {
                "$schema": SCHEMA_URL,
                "name": name,
                "version": version,
                "extensions": {
                    "com.openai": {"interface": {"displayName": "rp-test plugin"}}
                },
            },
            "rp-test-plugin/skills/s1/SKILL.md": "---\nname: s1\n---\n",
        },
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    session = FakeSession()

    class Outer:
        pass

    outer = Outer()
    outer.session = session
    monkeypatch.setattr(mp, "open_session", lambda browser="chrome": outer)
    return session


@pytest.fixture
def blob(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replaces the blob PUT's urlopen; ``state["status"]`` is its answer."""
    state: dict[str, Any] = {"status": 201, "requests": []}

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=300.0):
        state["requests"].append(req)
        if isinstance(state["status"], Exception):
            raise state["status"]
        return Response(state["status"])

    monkeypatch.setattr(mp.urllib.request, "urlopen", fake_urlopen)
    return state


def upload_answers(session: FakeSession, pid: str = PID) -> None:
    session.answers[("POST", mp.UPLOAD_URL)] = (
        201,
        {"file_id": "file_1", "upload_url": SIGNED, "etag": '"0x1"'},
    )
    session.answers[("POST", mp.WORKSPACE)] = (
        201,
        {"plugin_id": pid, "release_id": "r1"},
    )
    session.answers[("POST", mp.WORKSPACE_PLUGIN.format(id=pid))] = (
        201,
        {"plugin_id": pid, "release_id": "r2"},
    )


# ---------------------------------------------------------------------------
# archives
# ---------------------------------------------------------------------------


def test_inspect_archive_reads_a_top_folder_layout(tmp_path: Path) -> None:
    info = mp.inspect_archive(good_zip(tmp_path))
    assert (info.name, info.version, info.display_name, info.skills) == (
        "rp-test-plugin",
        "0.0.2",
        "rp-test plugin",
        ["s1"],
    )


def test_inspect_archive_prefers_the_root_manifest_over_the_codex_copy(
    tmp_path: Path,
) -> None:
    """ChatGPT's own download carries plugin.json and .codex-plugin/plugin.json."""
    path = make_zip(
        tmp_path,
        {
            "plugin.json": {"$schema": SCHEMA_URL, "name": "a", "version": "1"},
            ".codex-plugin/plugin.json": {
                "$schema": SCHEMA_URL,
                "name": "a",
                "version": "1",
            },
            "skills/x/SKILL.md": "x",
        },
    )
    info = mp.inspect_archive(path)
    assert (info.name, info.display_name, info.skills) == ("a", "a", ["x"])


def test_inspect_archive_accepts_a_codex_layout_alone(tmp_path: Path) -> None:
    path = make_zip(
        tmp_path,
        {
            ".codex-plugin/plugin.json": {
                "$schema": SCHEMA_URL,
                "name": "c",
                "version": "2",
            }
        },
    )
    assert mp.inspect_archive(path).name == "c"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {
                "a/plugin.json": {"$schema": SCHEMA_URL, "name": "a", "version": "1"},
                "b/plugin.json": {"$schema": SCHEMA_URL, "name": "b", "version": "1"},
            },
            "found 2",
        ),
        ({"README.md": "x"}, "found 0"),
        ({"plugin.json": "not json"}, "not valid JSON"),
        ({"plugin.json": "[1, 2]"}, "not an object"),
        ({"plugin.json": {"$schema": SCHEMA_URL, "version": "1"}}, "no name"),
        (
            {"plugin.json": {"$schema": SCHEMA_URL, "name": "a", "version": 3}},
            "no version",
        ),
    ],
)
def test_inspect_archive_refuses_what_it_cannot_verify(
    tmp_path: Path, files, message
) -> None:
    with pytest.raises(mp.Refusal, match=message):
        mp.inspect_archive(make_zip(tmp_path, files))


def test_inspect_archive_refuses_other_types_missing_files_and_bad_zips(
    tmp_path: Path,
) -> None:
    with pytest.raises(mp.Refusal, match="only a .zip"):
        mp.inspect_archive(tmp_path / "p.tar.gz")
    with pytest.raises(mp.Refusal, match="no such file"):
        mp.inspect_archive(tmp_path / "missing.zip")
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    with pytest.raises(mp.Refusal, match="1 byte to 100 MB"):
        mp.inspect_archive(empty)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(mp.Refusal, match="not a zip"):
        mp.inspect_archive(bad)


def test_inspect_archive_refuses_a_manifest_without_the_schema(tmp_path: Path) -> None:
    """ChatGPT reads such an archive under another name (HTTP 400 on update)."""
    path = make_zip(tmp_path, {"plugin.json": {"name": "a", "version": "1"}})
    with pytest.raises(mp.Refusal, match="schema"):
        mp.inspect_archive(path)
    other = make_zip(
        tmp_path,
        {
            "plugin.json": {
                "$schema": "https://example.com/x.json",
                "name": "a",
                "version": "1",
            }
        },
        "o.zip",
    )
    with pytest.raises(mp.Refusal, match="schema"):
        mp.inspect_archive(other)


def test_inspect_archive_uses_the_name_when_there_is_no_display_name(
    tmp_path: Path,
) -> None:
    path = make_zip(
        tmp_path,
        {
            "plugin.json": {
                "$schema": SCHEMA_URL,
                "name": "n",
                "version": "1",
                "extensions": {"com.openai": {"interface": "x"}},
            }
        },
    )
    assert mp.inspect_archive(path).display_name == "n"


# ---------------------------------------------------------------------------
# resolution and presentation
# ---------------------------------------------------------------------------


def test_resolve_by_id_name_and_display_name() -> None:
    items = [plugin(), plugin(OTHER, "other", display="Other")]
    assert mp.resolve(items, OTHER)["id"] == OTHER
    assert mp.resolve(items, "rp-test-plugin")["id"] == PID
    assert mp.resolve(items, "Other")["id"] == OTHER


def test_resolve_refuses_unknown_and_ambiguous_names() -> None:
    items = [plugin(), plugin(OTHER)]
    with pytest.raises(mp.Refusal, match="2 plugins have this name"):
        mp.resolve(items, "rp-test-plugin")
    with pytest.raises(mp.Refusal, match="no plugin"):
        mp.resolve(items, "nope")


def test_redacted_drops_the_user_id_and_the_download_url() -> None:
    out = mp.redacted(plugin())
    text = json.dumps(out)
    assert "user-SECRET" not in text and "example.invalid" not in text
    assert out["release"]["version"] == "0.0.1"


def test_rows_show_install_state_and_skill_state() -> None:
    item = plugin(skills=("s1", "s2"))
    assert mp.list_row(item, {})[3:5] == ("no", "2")
    assert mp.skill_rows(item, None) == [("s1", "-"), ("s2", "-")]
    assert mp.skill_rows(item, {"disabled_skill_names": ["s2"]}) == [
        ("s1", "yes"),
        ("s2", "no"),
    ]
    assert mp.skill_names({"release": {"skills": ["plain", 3, {"name": ""}]}}) == [
        "plain"
    ]
    assert mp.display_name({"name": "n", "release": "x"}) == "n"


# ---------------------------------------------------------------------------
# the blob PUT
# ---------------------------------------------------------------------------


def test_put_blob_sends_the_captured_headers(blob: dict) -> None:
    assert mp.put_blob(SIGNED, b"zip") == 201
    req = blob["requests"][0]
    assert req.get_method() == "PUT"
    assert req.get_header("X-ms-blob-type") == "BlockBlob"
    assert req.get_header("Content-type") == "application/zip"
    # Cloudflare answers urllib's own User-Agent with error 1010 (HTTP 403)
    assert req.get_header("User-agent") == mp.UA


@pytest.mark.parametrize(
    "url", ["http://blob.oaiusercontent.com/x", "https://evil.example/x"]
)
def test_put_blob_refuses_any_other_host(url: str, blob: dict) -> None:
    with pytest.raises(mp.PluginError, match="expected blob host"):
        mp.put_blob(url, b"zip")
    assert blob["requests"] == []


def test_put_blob_reports_http_and_network_failures(blob: dict) -> None:
    blob["status"] = urllib.error.HTTPError(SIGNED, 403, "no", None, io.BytesIO(b""))
    assert mp.put_blob(SIGNED, b"zip") == 403
    blob["status"] = urllib.error.URLError("down")
    with pytest.raises(mp.PluginError, match="URLError") as exc:
        mp.put_blob(SIGNED, b"zip")
    assert "SECRET" not in str(exc.value)


# ---------------------------------------------------------------------------
# list / show / download
# ---------------------------------------------------------------------------


def test_list_prints_and_writes_json_without_private_fields(
    fake, tmp_path, capsys
) -> None:
    fake.installed[PID] = plugin()
    out = tmp_path / "list.json"
    assert mp.main(["list", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "rp-test-plugin" in text and "1 plugin(s)" in text
    doc = json.loads(out.read_text())
    assert doc[0]["installed"] is True and "creator_account_user_id" not in doc[0]


def test_list_read_failure_exits_1(fake, capsys) -> None:
    fake.answers[("GET", "/backend-api/ps/plugins/list")] = (500, {"error": "x"})
    assert mp.main(["list"]) == 1
    assert "plugin list read failed: HTTP 500" in capsys.readouterr().out


def test_show_prints_skill_state_and_writes_json(fake, tmp_path, capsys) -> None:
    fake.installed[PID] = {"id": PID, "disabled_skill_names": ["s1"]}
    out = tmp_path / "show.json"
    assert mp.main(["show", "rp-test plugin", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "installed yes" in text and "s1" in text
    assert json.loads(out.read_text())["installed"] is True


def test_show_of_an_unknown_plugin_exits_2(fake, capsys) -> None:
    assert mp.main(["show", "nope"]) == 2
    assert "refused" in capsys.readouterr().out


def test_download_writes_the_zip(fake, tmp_path, capsys) -> None:
    out = tmp_path / "sub" / "p.zip"
    assert mp.main(["download", PID, "--out", str(out)]) == 0
    assert zipfile.is_zipfile(out)
    assert "sha256" in capsys.readouterr().out


def test_download_refuses_an_existing_file_without_force(fake, tmp_path) -> None:
    out = tmp_path / "p.zip"
    out.write_bytes(b"old")
    assert mp.main(["download", PID, "--out", str(out)]) == 2
    assert out.read_bytes() == b"old"
    assert mp.main(["download", PID, "--out", str(out), "--force"]) == 0
    assert zipfile.is_zipfile(out)


def test_download_failures_exit_1(fake, tmp_path) -> None:
    fake.archive_bytes = b"not a zip"
    assert mp.main(["download", PID, "--out", str(tmp_path / "a.zip")]) == 1
    fake.answers[("GET", mp.ARCHIVE.format(id=PID))] = (404, {"error": "nf"})
    assert mp.main(["download", PID, "--out", str(tmp_path / "b.zip")]) == 1


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_dry_run_reads_only(fake, tmp_path, capsys) -> None:
    fake.own = []
    assert mp.main(["upload", str(good_zip(tmp_path)), "--dry-run"]) == 0
    assert fake.writes() == []
    assert "dry run" in capsys.readouterr().out


def test_upload_refuses_a_name_that_exists(fake, tmp_path) -> None:
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 2
    assert fake.writes() == []


def test_upload_refuses_a_bad_archive_before_opening_a_session(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        mp, "open_session", lambda browser="chrome": pytest.fail("session")
    )
    assert mp.main(["upload", str(tmp_path / "missing.zip")]) == 2
    assert mp.main(["update", PID, str(tmp_path / "missing.zip")]) == 2


def test_upload_creates_verifies_and_installs(fake, blob, tmp_path, capsys) -> None:
    fake.own = []
    upload_answers(fake)
    fake.detail[PID] = plugin(version="0.0.2")
    fake.answers[("POST", mp.INSTALL.split("?")[0].format(id=PID))] = (
        200,
        {"id": PID, "enabled": True, "app_ids_needing_auth": ["asdk_app_1"]},
    )
    original = fake.call

    def call(path, method="GET", payload=None, **kw):
        status, body = original(path, method, payload, **kw)
        if method == "POST" and "/install" in path:
            fake.installed[PID] = plugin()
        return status, body

    fake.call = call
    assert mp.main(["upload", str(good_zip(tmp_path)), "--install"]) == 0
    text = capsys.readouterr().out
    assert "created and verified" in text and "installed and verified" in text
    assert "needing sign-in: 1" in text and "SECRET" not in text
    upload_payload = next(c[2] for c in fake.calls if c[1] == mp.UPLOAD_URL)
    assert (
        upload_payload["mime_type"] == "application/zip"
        and "plugin_id" not in upload_payload
    )
    create_payload = next(c[2] for c in fake.calls if c[1] == mp.WORKSPACE)
    assert create_payload == {"file_id": "file_1", "etag": '"0x1"'}
    install_payload = next(c[2] for c in fake.calls if "/install" in c[1])
    assert len(install_payload["install_attempt_id"]) == 36


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ((500, {"error": "x"}), "upload URL request failed"),
        ((201, {"file_id": "f"}), "lacks file_id"),
    ],
)
def test_upload_url_failures_exit_1(
    fake, blob, tmp_path, answer, message, capsys
) -> None:
    fake.own = []
    fake.answers[("POST", mp.UPLOAD_URL)] = answer
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 1
    assert message in capsys.readouterr().out


def test_upload_blob_failure_and_release_failures_exit_1(
    fake, blob, tmp_path, capsys
) -> None:
    fake.own = []
    upload_answers(fake)
    blob["status"] = 403
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 1
    assert "archive upload failed: HTTP 403" in capsys.readouterr().out
    blob["status"] = 201
    fake.answers[("POST", mp.WORKSPACE)] = (400, {"error": "x"})
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 1
    fake.answers[("POST", mp.WORKSPACE)] = (201, {"plugin_id": "not-a-plugin"})
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 1
    assert "lacks a plugin id" in capsys.readouterr().out


def test_upload_read_back_mismatch_exits_1(fake, blob, tmp_path, capsys) -> None:
    fake.own = []
    upload_answers(fake)
    fake.detail[PID] = plugin(version="9.9.9")
    assert mp.main(["upload", str(good_zip(tmp_path))]) == 1
    assert "read-back shows" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_preview_writes_nothing(fake, tmp_path, capsys) -> None:
    assert mp.main(["update", PID, str(good_zip(tmp_path))]) == 0
    assert fake.writes() == []
    assert "0.0.1 -> 0.0.2" in capsys.readouterr().out


def test_update_refuses_another_plugins_archive(fake, tmp_path) -> None:
    assert (
        mp.main(["update", PID, str(good_zip(tmp_path, name="other")), "--apply"]) == 2
    )
    assert fake.writes() == []


def test_update_apply_uploads_with_the_plugin_id_and_verifies(
    fake, blob, tmp_path, capsys
) -> None:
    upload_answers(fake)
    fake.detail[PID] = plugin(version="0.0.2")
    assert mp.main(["update", PID, str(good_zip(tmp_path)), "--apply"]) == 0
    assert "new version verified: 0.0.2" in capsys.readouterr().out
    upload_payload = next(c[2] for c in fake.calls if c[1] == mp.UPLOAD_URL)
    assert upload_payload["plugin_id"] == PID


def test_update_refuses_an_answer_about_another_plugin(
    fake, blob, tmp_path, capsys
) -> None:
    upload_answers(fake)
    fake.answers[("POST", mp.WORKSPACE_PLUGIN.format(id=PID))] = (
        201,
        {"plugin_id": OTHER},
    )
    assert mp.main(["update", PID, str(good_zip(tmp_path)), "--apply"]) == 1
    assert "names another plugin" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


INSTALL_PATH = mp.INSTALL.split("?")[0].format(id=PID)


def test_install_preview_and_already_installed(fake, capsys) -> None:
    assert mp.main(["install", PID]) == 0
    assert "preview" in capsys.readouterr().out and fake.writes() == []
    fake.installed[PID] = plugin()
    assert mp.main(["install", PID, "--apply"]) == 0
    assert "installed already" in capsys.readouterr().out and fake.writes() == []


def test_install_apply_verifies_the_installed_list(fake, capsys) -> None:
    fake.answers[("POST", INSTALL_PATH)] = (200, {"id": PID, "enabled": True})
    assert mp.main(["install", PID, "--apply"]) == 1  # answered, but not installed
    assert "not in the installed list" in capsys.readouterr().out
    fake.answers[("POST", INSTALL_PATH)] = (500, {"error": "x"})
    assert mp.main(["install", PID, "--apply"]) == 1


def test_install_apply_success(fake, capsys) -> None:
    fake.answers[("POST", INSTALL_PATH)] = (
        200,
        {"id": PID, "enabled": True, "app_ids_needing_auth": ["a"]},
    )
    original = fake.call

    def call(path, method="GET", payload=None, **kw):
        status, body = original(path, method, payload, **kw)
        if method == "POST":
            fake.installed[PID] = plugin()
        return status, body

    fake.call = call
    assert mp.main(["install", PID, "--apply"]) == 0
    assert "needing sign-in: 1" in capsys.readouterr().out


def test_uninstall_preview_confirm_and_failures(fake, capsys) -> None:
    assert mp.main(["uninstall", PID]) == 0
    assert "is not installed" in capsys.readouterr().out
    fake.installed[PID] = plugin()
    assert mp.main(["uninstall", PID]) == 0
    assert "pass --confirm" in capsys.readouterr().out and fake.writes() == []
    path = mp.UNINSTALL.format(id=PID)
    fake.answers[("POST", path)] = (200, {"id": PID, "enabled": False})
    assert mp.main(["uninstall", PID, "--confirm"]) == 1  # still in the list
    assert "still installed" in capsys.readouterr().out
    fake.answers[("POST", path)] = (500, {"error": "x"})
    assert mp.main(["uninstall", PID, "--confirm"]) == 1

    def gone(path, method="GET", payload=None, **kw):
        if method == "POST":
            fake.installed.clear()
            return 200, {"id": PID, "enabled": False}
        return FakeSession.call(fake, path, method, payload, **kw)

    fake.call = gone
    assert mp.main(["uninstall", PID, "--confirm"]) == 0


# ---------------------------------------------------------------------------
# skill switch
# ---------------------------------------------------------------------------


def test_skill_refuses_an_unknown_skill_and_previews(fake, capsys) -> None:
    assert mp.main(["skill", PID, "nope", "--disable", "--apply"]) == 2
    assert mp.main(["skill", PID, "s1", "--disable"]) == 0
    assert "pass --apply to disable" in capsys.readouterr().out
    assert fake.writes() == []


def test_skill_apply_checks_the_answer_and_the_installed_list(fake, capsys) -> None:
    path = mp.SKILL_DISABLE.format(id=PID, skill="s1")
    fake.answers[("POST", path)] = (200, {"enabled": False})
    assert mp.main(["skill", PID, "s1", "--disable", "--apply"]) == 0  # not installed
    fake.installed[PID] = {"id": PID, "disabled_skill_names": []}
    assert mp.main(["skill", PID, "s1", "--disable", "--apply"]) == 1
    assert "does not show the new skill state" in capsys.readouterr().out
    fake.installed[PID] = {"id": PID, "disabled_skill_names": ["s1"]}
    assert mp.main(["skill", PID, "s1", "--disable", "--apply"]) == 0
    fake.answers[("POST", path)] = (200, {"enabled": True})
    assert mp.main(["skill", PID, "s1", "--disable", "--apply"]) == 1


def test_skill_enable_uses_the_enable_path(fake) -> None:
    path = mp.SKILL_ENABLE.format(id=PID, skill="s1")
    fake.answers[("POST", path)] = (200, {"enabled": True})
    fake.installed[PID] = {"id": PID, "disabled_skill_names": []}
    assert mp.main(["skill", PID, "s1", "--enable", "--apply"]) == 0
    assert fake.writes()[-1][1] == path


def test_parser_requires_a_subcommand_and_one_switch() -> None:
    with pytest.raises(SystemExit):
        mp.parser().parse_args([])
    with pytest.raises(SystemExit):
        mp.parser().parse_args(["skill", PID, "s1"])
