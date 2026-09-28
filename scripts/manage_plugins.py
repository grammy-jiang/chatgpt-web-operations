#!/usr/bin/env python3
"""Manage the plugins this ChatGPT account uploaded.

    manage_plugins.py list [--json PATH]
    manage_plugins.py show PLUGIN [--json PATH]
    manage_plugins.py download PLUGIN --out FILE [--force]
    manage_plugins.py upload ARCHIVE [--install] [--dry-run]
    manage_plugins.py update PLUGIN ARCHIVE [--apply]
    manage_plugins.py install PLUGIN [--apply]
    manage_plugins.py uninstall PLUGIN [--confirm]
    manage_plugins.py skill PLUGIN SKILL (--enable | --disable) [--apply]

PLUGIN is one of the account's own plugins (the Plugins page, Personal tab,
"Created by you"; ``GET /backend-api/ps/plugins/list?scope=USER``), named
by its exact id (``Plugin_`` and 32 hex digits), its exact ``name`` from
``plugin.json``, or its exact display name. A name that matches more than
one plugin is refused. Public directory apps are not managed here.

ARCHIVE is a ``.zip`` that holds ``plugin.json`` at its root or in one top
folder, with the Agent Plugins ``$schema``, ``name`` and ``version``; skills
are ``skills/<name>/SKILL.md`` beside it. Without the ``$schema`` ChatGPT
reads the archive under another name, so such an archive is refused. At
most 100 MB. The page also accepts ``.tar.gz``/``.tgz``, but only a zip
upload has been captured, so only a zip is sent.

Upload (a new plugin) and update (a new version of an existing one) take
three steps, as the page does (captured 2026-09-29): ``POST
/backend-api/public/plugins/workspace/upload-url`` returns a short-lived
signed blob URL, the archive is PUT there (the URL is never printed), then
``POST /backend-api/public/plugins/workspace`` (upload) or
``.../workspace/<plugin id>`` (update) makes the release. An update is
refused unless the archive's ``name`` is the plugin's ``name``; an upload is
refused when a plugin of that ``name`` exists already (the page answers
that with HTTP 400; use update). An installation follows a new release by
itself.

There is no delete. A personal account cannot delete an uploaded plugin:
the page has no control for it, the page's code has no such route, and on
2026-09-29 ``DELETE /backend-api/public/plugins/workspace/<id>`` answered
400 ("Workspace plugin creation requires an active workspace.") and
``DELETE /backend-api/ps/plugins/<id>`` 404. Uninstall is the nearest:
the plugin stays in "Created by you" and can be installed again.

Upload acts unless --dry-run; update, install and skill are previews
unless --apply; uninstall is a preview unless --confirm. Every write is
read back: the plugin's name and version after an upload or update, the
installed list after an install or uninstall, the answer and the installed
list's ``disabled_skill_names`` after a skill switch. Output never carries a
user id, a signed URL or an archive's content.

Exit 0: done and verified, or previewed. Exit 1: a request or its read-back
failed. Exit 2: invalid arguments, an unreadable archive, or a refusal (no
such plugin, an ambiguous name, a name mismatch, a name that exists
already, an existing output file).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _common import ensure_venv, open_session, table
from chatgpt_session import UA
from list_skills import PLUGINS_INSTALLED

PLUGINS_LIST = "/backend-api/ps/plugins/list?scope=USER&limit=100"
PLUGIN = "/backend-api/ps/plugins/{id}"
ARCHIVE = "/backend-api/ps/plugins/{id}/archive"
INSTALL = "/backend-api/ps/plugins/{id}/install?includeAppsNeedingAuth=true"
UNINSTALL = "/backend-api/ps/plugins/{id}/uninstall"
SKILL_ENABLE = "/backend-api/ps/plugins/{id}/skills/{skill}/enable"
SKILL_DISABLE = "/backend-api/ps/plugins/{id}/skills/{skill}/disable"
UPLOAD_URL = "/backend-api/public/plugins/workspace/upload-url"
WORKSPACE = "/backend-api/public/plugins/workspace"
WORKSPACE_PLUGIN = "/backend-api/public/plugins/workspace/{id}"

PLUGIN_ID = re.compile(r"Plugin_[0-9a-f]{32}\Z")
# the portable Agent Plugins manifest; without it ChatGPT reads the archive
# under another name (2026-09-29: an update answered HTTP 400 "Plugin upload
# name must match the existing plugin"; the same manifest with it, 201)
SCHEMA = re.compile(
    r"https://agent-plugins\.org/schemas/[^/\s]+/plugin\.schema\.json\Z"
)
MAX_BYTES = 100 * 1024 * 1024
ZIP_MIME = "application/zip"
BLOB_HOST_SUFFIX = ".oaiusercontent.com"
# the four headers the page's XHR sets on the blob PUT (captured 2026-09-29)
BLOB_HEADERS = {
    "Content-Type": ZIP_MIME,
    "x-ms-blob-type": "BlockBlob",
    "x-ms-version": "2020-04-08",
    "x-ms-blob-content-type": ZIP_MIME,
}
# never printed or written: a user id and a download URL that may be signed
PRIVATE_FIELDS = ("creator_account_user_id", "bundle_download_url")

LIST_HEADERS = ("name", "display name", "version", "installed", "skills", "id")
SKILL_HEADERS = ("skill", "on")


class PluginError(RuntimeError):
    """A failed request or read-back; the message holds no secret."""


class Refusal(ValueError):
    """A local safety refusal or an invalid input (exit 2)."""


# ---------------------------------------------------------------------------
# archives (offline)
# ---------------------------------------------------------------------------


@dataclass
class ArchiveInfo:
    path: Path
    size: int
    name: str
    version: str
    display_name: str
    skills: list[str] = field(default_factory=list)


def inspect_archive(path: str | Path) -> ArchiveInfo:
    """Read ``plugin.json`` and the skill names from a plugin zip without
    extracting anything; raise ``Refusal`` for anything the upload would
    not accept or this command cannot verify."""
    archive = Path(path).expanduser()
    if archive.suffix.lower() != ".zip":
        raise Refusal(f"{archive.name}: only a .zip archive is supported")
    if not archive.is_file():
        raise Refusal(f"{archive}: no such file")
    size = archive.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise Refusal(f"{archive.name}: {size} bytes; must be 1 byte to 100 MB")
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            member, prefix = choose_manifest(names, archive.name)
            manifest = json.loads(zf.read(member).decode("utf-8"))
    except zipfile.BadZipFile as exc:
        raise Refusal(f"{archive.name}: not a zip archive") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"{archive.name}: plugin.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise Refusal(f"{archive.name}: plugin.json is not an object")
    if not SCHEMA.match(str(manifest.get("$schema") or "")):
        raise Refusal(
            f"{archive.name}: plugin.json lacks the Agent Plugins $schema"
            " (https://agent-plugins.org/schemas/<version>/plugin.schema.json);"
            " without it ChatGPT reads the archive under another name"
        )
    name, version = manifest.get("name"), manifest.get("version")
    if not (isinstance(name, str) and name.strip()):
        raise Refusal(f"{archive.name}: plugin.json has no name")
    if not (isinstance(version, str) and version.strip()):
        raise Refusal(f"{archive.name}: plugin.json has no version")
    interface = ((manifest.get("extensions") or {}).get("com.openai") or {}).get(
        "interface"
    ) or {}
    display = interface.get("displayName") if isinstance(interface, dict) else None
    skills = sorted(
        {
            n[len(prefix) :].split("/")[1]
            for n in names
            if n.startswith(prefix + "skills/")
            and n.endswith("/SKILL.md")
            and n[len(prefix) :].count("/") == 2
        }
    )
    return ArchiveInfo(
        archive,
        size,
        name,
        version,
        display if isinstance(display, str) else name,
        skills,
    )


CODEX_MANIFEST = ".codex-plugin/plugin.json"


def choose_manifest(names: list[str], label: str) -> tuple[str, str]:
    """``(manifest member, plugin root prefix)``: a root ``plugin.json``
    first; else the Codex layout's ``.codex-plugin/plugin.json`` with its
    content at the root (ChatGPT's own download adds that file beside the
    root one); else the one ``<dir>/plugin.json``, whose folder is the root.
    ``Refusal`` when none or several top folders qualify."""
    if "plugin.json" in names:
        return "plugin.json", ""
    if CODEX_MANIFEST in names:
        return CODEX_MANIFEST, ""
    nested = [
        n
        for n in names
        if n.count("/") == 1 and n.endswith("/plugin.json") and not n.startswith("/")
    ]
    if len(nested) != 1:
        raise Refusal(
            f"{label}: expected one plugin.json at the root or in one top folder,"
            f" found {len(nested)}"
        )
    return nested[0], nested[0][: -len("plugin.json")]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _read(session: Any, path: str, what: str) -> dict[str, Any]:
    status, body = session.call(path)
    if status != 200 or not isinstance(body, dict):
        raise PluginError(f"{what} read failed: HTTP {status}")
    return body


def own_plugins(session: Any) -> list[dict[str, Any]]:
    body = _read(session, PLUGINS_LIST, "plugin list")
    return [p for p in body.get("plugins") or [] if isinstance(p, dict)]


def installed_plugins(session: Any) -> dict[str, dict[str, Any]]:
    body = _read(session, PLUGINS_INSTALLED, "installed plugins")
    return {
        str(p.get("id")): p
        for p in body.get("plugins") or []
        if isinstance(p, dict) and p.get("id")
    }


def plugin_detail(session: Any, plugin_id: str) -> dict[str, Any]:
    return _read(session, PLUGIN.format(id=plugin_id), "plugin")


def release(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("release")
    return value if isinstance(value, dict) else {}


def display_name(item: dict[str, Any]) -> str:
    return str(release(item).get("display_name") or item.get("name") or "?")


def skill_names(item: dict[str, Any]) -> list[str]:
    names = []
    for skill in release(item).get("skills") or []:
        name = skill.get("name") if isinstance(skill, dict) else skill
        if isinstance(name, str) and name:
            names.append(name)
    return names


def resolve(items: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """The one own plugin ``target`` names exactly: id, then name, then
    display name. ``Refusal`` when none or several match."""
    for label, key in (
        ("id", lambda p: p.get("id")),
        ("name", lambda p: p.get("name")),
        ("display name", display_name),
    ):
        hits = [p for p in items if key(p) == target]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise Refusal(f"{target!r}: {len(hits)} plugins have this {label}")
    raise Refusal(f"{target!r}: no plugin of this account has this id or name")


def redacted(item: dict[str, Any]) -> dict[str, Any]:
    """``item`` without the fields output never carries."""
    out = {k: v for k, v in item.items() if k not in PRIVATE_FIELDS}
    if isinstance(out.get("release"), dict):
        out["release"] = {
            k: v for k, v in out["release"].items() if k not in PRIVATE_FIELDS
        }
    return out


def list_row(item: dict[str, Any], installed: dict[str, dict[str, Any]]) -> tuple:
    plugin_id = str(item.get("id") or "?")
    return (
        str(item.get("name") or "?"),
        display_name(item),
        str(release(item).get("version") or "?"),
        "yes" if plugin_id in installed else "no",
        str(len(skill_names(item))),
        plugin_id,
    )


def skill_rows(item: dict[str, Any], installed_item: dict[str, Any] | None) -> list:
    disabled = set((installed_item or {}).get("disabled_skill_names") or [])
    return [
        (name, _skill_state(name, installed_item, disabled))
        for name in skill_names(item)
    ]


def _skill_state(
    name: str, installed_item: dict[str, Any] | None, disabled: set
) -> str:
    """``-`` when the plugin is not installed (the state is unknown), else
    whether the installed plugin has this skill on."""
    if installed_item is None:
        return "-"
    return "no" if name in disabled else "yes"


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def put_blob(url: str, data: bytes, timeout: float = 300.0) -> int:
    """PUT ``data`` to the signed blob URL the upload-url call returned.
    Only an https URL on the blob host is accepted; the URL is never
    printed, because its query string is a credential."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(BLOB_HOST_SUFFIX):
        raise PluginError("the upload URL is not on the expected blob host")
    # the blob host sits behind Cloudflare, which refuses urllib's own
    # User-Agent with "error code: 1010" (HTTP 403; seen 2026-09-29): send
    # the browser User-Agent the chatgpt.com session already sends
    headers = dict(BLOB_HEADERS, **{"User-Agent": UA})
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise PluginError(f"archive upload failed: {type(exc).__name__}") from exc


def upload_file(session: Any, info: ArchiveInfo, plugin_id: str | None) -> dict:
    """Steps one and two: a signed URL, then the archive. Returns the
    ``{"file_id", "etag"}`` the release call needs."""
    payload: dict[str, Any] = {
        "filename": info.path.name,
        "mime_type": ZIP_MIME,
        "size_bytes": info.size,
    }
    if plugin_id:
        payload["plugin_id"] = plugin_id
    status, body = session.call(UPLOAD_URL, method="POST", payload=payload, retries=1)
    if status not in (200, 201) or not isinstance(body, dict):
        raise PluginError(f"upload URL request failed: HTTP {status}")
    file_id, etag, url = body.get("file_id"), body.get("etag"), body.get("upload_url")
    if not (
        isinstance(file_id, str) and isinstance(etag, str) and isinstance(url, str)
    ):
        raise PluginError("upload URL answer lacks file_id, etag or upload_url")
    put_status = put_blob(url, info.path.read_bytes())
    if put_status != 201:
        raise PluginError(f"archive upload failed: HTTP {put_status}")
    return {"file_id": file_id, "etag": etag}


def make_release(session: Any, path: str, file_ref: dict) -> dict[str, Any]:
    """Step three: the release. Returns the answer (``plugin_id``,
    ``release_id``, ...)."""
    status, body = session.call(path, method="POST", payload=file_ref, retries=1)
    if status not in (200, 201) or not isinstance(body, dict):
        raise PluginError(f"release request failed: HTTP {status}")
    if not PLUGIN_ID.match(str(body.get("plugin_id") or "")):
        raise PluginError("release answer lacks a plugin id")
    return body


def verify_release(session: Any, plugin_id: str, info: ArchiveInfo) -> dict:
    detail = plugin_detail(session, plugin_id)
    version = release(detail).get("version")
    if detail.get("name") != info.name or version != info.version:
        raise PluginError(
            f"read-back shows {detail.get('name')!r} {version!r},"
            f" expected {info.name!r} {info.version!r}"
        )
    return detail


def install(session: Any, plugin_id: str) -> list[str]:
    """Install and read back; returns the app ids that still need sign-in."""
    payload = {"install_attempt_id": str(uuid.uuid4())}
    path = INSTALL.format(id=plugin_id)
    status, body = session.call(path, method="POST", payload=payload, retries=1)
    if status != 200 or not isinstance(body, dict):
        raise PluginError(f"install failed: HTTP {status}")
    if plugin_id not in installed_plugins(session):
        raise PluginError(
            "install answered, but the plugin is not in the installed list"
        )
    return [str(a) for a in body.get("app_ids_needing_auth") or []]


def uninstall(session: Any, plugin_id: str) -> None:
    status, _body = session.call(
        UNINSTALL.format(id=plugin_id), method="POST", retries=1
    )
    if status != 200:
        raise PluginError(f"uninstall failed: HTTP {status}")
    if plugin_id in installed_plugins(session):
        raise PluginError("uninstall answered, but the plugin is still installed")


def switch_skill(session: Any, plugin_id: str, skill: str, enable: bool) -> None:
    template = SKILL_ENABLE if enable else SKILL_DISABLE
    path = template.format(id=plugin_id, skill=urllib.parse.quote(skill, safe=""))
    status, body = session.call(path, method="POST", retries=1)
    if status != 200 or not isinstance(body, dict) or body.get("enabled") is not enable:
        raise PluginError(f"skill switch failed: HTTP {status}")
    item = installed_plugins(session).get(plugin_id)
    if item is not None:
        disabled = set(item.get("disabled_skill_names") or [])
        if (skill in disabled) == enable:
            raise PluginError("the installed list does not show the new skill state")


def download(session: Any, plugin_id: str) -> bytes:
    status, data = session.call(ARCHIVE.format(id=plugin_id), binary=True, retries=1)
    if status != 200 or not isinstance(data, bytes):
        raise PluginError(f"archive download failed: HTTP {status}")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise PluginError("the downloaded archive is not a zip")
    return data


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _write_json(path: str, document: Any) -> None:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwritten to {out}")


def cmd_list(session: Any, args: argparse.Namespace) -> int:
    items = own_plugins(session)
    installed = installed_plugins(session)
    print(table([list_row(p, installed) for p in items], LIST_HEADERS))
    print(f"\n{len(items)} plugin(s) created by this account")
    if args.json:
        _write_json(
            args.json,
            [dict(redacted(p), installed=str(p.get("id")) in installed) for p in items],
        )
    return 0


def cmd_show(session: Any, args: argparse.Namespace) -> int:
    item = resolve(own_plugins(session), args.plugin)
    plugin_id = str(item["id"])
    detail = plugin_detail(session, plugin_id)
    installed_item = installed_plugins(session).get(plugin_id)
    print(f"{display_name(detail)} ({detail.get('name')}), id {plugin_id}")
    print(
        f"version {release(detail).get('version')}, status {detail.get('status')},"
        f" installed {'yes' if installed_item else 'no'}"
    )
    print()
    print(table(skill_rows(detail, installed_item), SKILL_HEADERS))
    if args.json:
        _write_json(args.json, dict(redacted(detail), installed=bool(installed_item)))
    return 0


def cmd_download(session: Any, args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        raise Refusal(f"{out} exists; pass --force to replace it")
    item = resolve(own_plugins(session), args.plugin)
    data = download(session, str(item["id"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    print(f"{display_name(item)}: {len(data)} bytes, sha256 {digest[:16]}, to {out}")
    return 0


def cmd_upload(session: Any, args: argparse.Namespace) -> int:
    info = inspect_archive(args.archive)
    items = own_plugins(session)
    if any(p.get("name") == info.name for p in items):
        raise Refusal(f"a plugin named {info.name!r} exists already; use update")
    print(
        f"upload {info.path.name}: {info.name} {info.version},"
        f" {len(info.skills)} skill(s), {info.size} bytes"
    )
    if args.dry_run:
        print("dry run: nothing uploaded")
        return 0
    answer = make_release(session, WORKSPACE, upload_file(session, info, None))
    plugin_id = str(answer["plugin_id"])
    verify_release(session, plugin_id, info)
    print(f"created and verified: {plugin_id}, version {info.version}")
    if args.install:
        needing = install(session, plugin_id)
        print("installed and verified")
        if needing:
            print(f"apps still needing sign-in: {len(needing)}")
    return 0


def cmd_update(session: Any, args: argparse.Namespace) -> int:
    info = inspect_archive(args.archive)
    item = resolve(own_plugins(session), args.plugin)
    plugin_id = str(item["id"])
    if item.get("name") != info.name:
        raise Refusal(
            f"archive is {info.name!r}, but the plugin is {item.get('name')!r}"
        )
    current = release(item).get("version")
    print(f"update {display_name(item)} ({plugin_id}): {current} -> {info.version}")
    if not args.apply:
        print("preview: pass --apply to upload the new version")
        return 0
    answer = make_release(
        session,
        WORKSPACE_PLUGIN.format(id=plugin_id),
        upload_file(session, info, plugin_id),
    )
    if answer["plugin_id"] != plugin_id:
        raise PluginError("the release answer names another plugin")
    verify_release(session, plugin_id, info)
    print(f"new version verified: {info.version}")
    return 0


def cmd_install(session: Any, args: argparse.Namespace) -> int:
    item = resolve(own_plugins(session), args.plugin)
    plugin_id = str(item["id"])
    if plugin_id in installed_plugins(session):
        print(f"{display_name(item)} is installed already")
        return 0
    print(f"install {display_name(item)} ({plugin_id})")
    if not args.apply:
        print("preview: pass --apply to install")
        return 0
    needing = install(session, plugin_id)
    print("installed and verified")
    if needing:
        print(f"apps still needing sign-in: {len(needing)}")
    return 0


def cmd_uninstall(session: Any, args: argparse.Namespace) -> int:
    item = resolve(own_plugins(session), args.plugin)
    plugin_id = str(item["id"])
    if plugin_id not in installed_plugins(session):
        print(f"{display_name(item)} is not installed")
        return 0
    print(
        f"uninstall {display_name(item)} ({plugin_id}); it stays in"
        " 'Created by you' and can be installed again"
    )
    if not args.confirm:
        print("preview: pass --confirm to uninstall")
        return 0
    uninstall(session, plugin_id)
    print("uninstalled and verified")
    return 0


def cmd_skill(session: Any, args: argparse.Namespace) -> int:
    item = resolve(own_plugins(session), args.plugin)
    plugin_id = str(item["id"])
    if args.skill not in skill_names(plugin_detail(session, plugin_id)):
        raise Refusal(f"{args.skill!r} is not a skill of {display_name(item)}")
    action = "enable" if args.enable else "disable"
    print(f"{action} {args.skill} in {display_name(item)} ({plugin_id})")
    if not args.apply:
        print(f"preview: pass --apply to {action}")
        return 0
    switch_skill(session, plugin_id, args.skill, args.enable)
    print(f"{action}d and verified")
    return 0


COMMANDS = {
    "list": cmd_list,
    "show": cmd_show,
    "download": cmd_download,
    "upload": cmd_upload,
    "update": cmd_update,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "skill": cmd_skill,
}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--browser", default="chrome")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="the account's own plugins")
    p.add_argument("--json", default="", metavar="PATH")
    p = sub.add_parser("show", help="one plugin, its version and skills")
    p.add_argument("plugin")
    p.add_argument("--json", default="", metavar="PATH")
    p = sub.add_parser("download", help="save the plugin's current archive")
    p.add_argument("plugin")
    p.add_argument("--out", required=True, metavar="FILE")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    p = sub.add_parser("upload", help="create a new plugin from a zip")
    p.add_argument("archive")
    p.add_argument("--install", action="store_true", help="install it afterwards")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("update", help="upload a new version of a plugin")
    p.add_argument("plugin")
    p.add_argument("archive")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("install", help="install a plugin")
    p.add_argument("plugin")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("uninstall", help="uninstall a plugin (there is no delete)")
    p.add_argument("plugin")
    p.add_argument("--confirm", action="store_true")
    p = sub.add_parser("skill", help="switch one of a plugin's skills on or off")
    p.add_argument("plugin")
    p.add_argument("skill")
    toggle = p.add_mutually_exclusive_group(required=True)
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    p.add_argument("--apply", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in ("upload", "update"):
        try:
            inspect_archive(
                args.archive
            )  # before any session: a bad file costs nothing
        except Refusal as exc:
            print(f"refused: {exc}")
            return 2
    session = open_session(args.browser).session
    try:
        return COMMANDS[args.command](session, args)
    except Refusal as exc:
        print(f"refused: {exc}")
        return 2
    except PluginError as exc:
        print(f"failed: {exc}")
        return 1


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
