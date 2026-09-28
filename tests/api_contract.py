"""Run the skill's read commands over payloads synthesized from recorded API
shapes (TESTING.md section 6, P5).

Shared by ``tests/test_api_contracts.py`` (the contract tests) and
``tests/refresh_read_paths.py`` (``make read-paths``). Not collected by
pytest: no ``test_`` prefix.

``FakeInner`` stands where ``chatgpt_session.Session`` stands: every
``call`` is matched to a registered endpoint (``api_shapes.endpoint_for``)
and answered with ``api_shapes.synthesize`` of that endpoint's recorded
shape. A call to anything else fails the scenario: a command that reads an
endpoint nobody records is exactly the gap P5 closes. The client session
around it is a real ``chatgpt_client.ChatGPTSession`` built without
authenticating, so its own methods (``list_conversations``,
``get_conversation``) run for real too.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_shapes  # noqa: E402

GIZMO = "g-p-" + "1".zfill(32)
CHAT = "00000000-0000-4000-8000-000000000001"
APP = "asdk_app_" + "1".zfill(32)


class UnrecordedEndpoint(AssertionError):
    """A command called an endpoint that has no registered, recorded shape."""


class FakeInner:
    """``chatgpt_session.Session`` answering from recorded shapes."""

    def __init__(
        self,
        shapes: dict[str, Any],
        log: api_shapes.ReadLog | None,
        prefer: dict[str, Any] | None = None,
    ):
        self.shapes = shapes
        self.log = log
        self.prefer = dict(prefer or {})
        self.calls: list[tuple[str, str]] = []
        # Raised calls can be swallowed by a command's own error handling,
        # so they are also kept here for the contract test to check.
        self.unrecorded: list[str] = []
        self.user_id = "user-XXXXXXXX"
        self.token = "t" * 48
        self.cookie = "c=1"

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int | None = None,
    ) -> tuple[int, Any]:
        self.calls.append((method, path))
        endpoint = api_shapes.endpoint_for(method, path)
        if endpoint is None:
            self.unrecorded.append(f"{method} {path}: not a registered read")
            raise UnrecordedEndpoint(self.unrecorded[-1])
        shape = self.shapes.get(endpoint.name)
        if shape is None:
            self.unrecorded.append(f"{method} {path}: no shape for {endpoint.name}")
            raise UnrecordedEndpoint(self.unrecorded[-1])
        return 200, api_shapes.synthesize(shape, self.log, endpoint.name, self.prefer)


def client_session(inner: FakeInner) -> Any:
    """A real ``ChatGPTSession`` over ``inner``, built without logging in."""
    import chatgpt_client as cc

    session = object.__new__(cc.ChatGPTSession)
    session.browser = "chrome"
    session._diagnostic = None
    session._session_kwargs = {}
    session._cs = None
    session.session = inner
    return session


def skill_name(shapes: dict[str, Any]) -> str:
    """The name the synthesized installed-skills list starts with."""
    body = api_shapes.synthesize(shapes["hazelnuts"])
    items = body.get("hazelnuts") if isinstance(body, dict) else None
    return str(items[0]["name"]) if items else "no-skill"


def run_file(shapes: dict[str, Any]) -> str:
    """A Deep research RUN.json naming the synthesized conversation, the
    one key ``deep_research.py status`` needs; written once per process."""
    path = Path(tempfile.gettempdir()) / f"rp-contract-run-{os.getpid()}.json"
    path.write_text(json.dumps({"conversation_id": CHAT}), encoding="utf-8")
    return str(path)


def _first_plugin(shapes: dict[str, Any]) -> dict[str, Any]:
    body = api_shapes.synthesize(shapes["plugins_list"])
    items = body.get("plugins") if isinstance(body, dict) else None
    return items[0] if items else {}


def plugin_id(shapes: dict[str, Any]) -> str:
    """The id the synthesized own-plugins list starts with. Not the name:
    the synthesized items share one text value for ``name``, which the
    command rightly refuses as ambiguous."""
    return str(_first_plugin(shapes).get("id") or "no-plugin")


def plugin_name(shapes: dict[str, Any]) -> str:
    return str(_first_plugin(shapes).get("name") or "no-plugin")


def plugin_skill(shapes: dict[str, Any]) -> str:
    """The first skill of the synthesized plugin."""
    body = api_shapes.synthesize(shapes["plugin"])
    skills = (
        ((body.get("release") or {}).get("skills") or [])
        if isinstance(body, dict)
        else []
    )
    first = skills[0] if skills else None
    name = first.get("name") if isinstance(first, dict) else first
    return str(name or "no-skill")


def plugin_archive(shapes: dict[str, Any]) -> str:
    """A zip whose plugin.json names the synthesized plugin: update's
    preview accepts it, upload's refuses it as a name that exists already."""
    path = Path(tempfile.gettempdir()) / f"rp-contract-plugin-{os.getpid()}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        manifest = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": plugin_name(shapes),
            "version": "0.0.0-contract",
        }
        zf.writestr("plugin.json", json.dumps(manifest))
    return str(path)


PLACEHOLDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "<gizmo>": lambda shapes: GIZMO,
    "<chat>": lambda shapes: CHAT,
    "<app>": lambda shapes: APP,
    "<skill>": skill_name,
    "<run>": run_file,
    "<plugin>": plugin_id,
    "<plugin-skill>": plugin_skill,
    "<plugin-archive>": plugin_archive,
}


@dataclass(frozen=True)
class Scenario:
    """One command line of a read command and the exit codes SKILL.md
    documents for it."""

    module: str
    argv: tuple[str, ...]
    exits: frozenset[int]
    prefer: tuple[tuple[str, Any], ...] = ()

    @property
    def id(self) -> str:
        return " ".join((self.module, *self.argv))


# A conversation whose messages are visible assistant text: the path
# read_chat takes for a real reply (author, content type, recipient).
ASSISTANT_TEXT = (
    ("role", "assistant"),
    ("content_type", "text"),
    ("recipient", "all"),
    ("status", "finished_successfully"),
    ("end_turn", True),
)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("list_chats", (), frozenset({0})),
    Scenario("list_chats", ("--pinned", "--limit", "5"), frozenset({0})),
    Scenario("list_chats", ("--archived", "--limit", "5"), frozenset({0})),
    Scenario("list_chats", ("--match", "title"), frozenset({0})),
    Scenario("list_projects", (), frozenset({0})),
    Scenario(
        "list_projects", ("--id", "<gizmo>", "--files", "--chats"), frozenset({0})
    ),
    Scenario("list_connectors", (), frozenset({0})),
    Scenario("list_connectors", ("--json",), frozenset({0})),
    Scenario("list_connectors", ("--tunnels",), frozenset({0})),
    Scenario("list_connectors", ("--detail", "<app>"), frozenset({0})),
    Scenario("probe_account", (), frozenset({0, 1})),
    Scenario("model_settings", (), frozenset({0, 1})),
    Scenario("profile_context", ("--project", "<gizmo>"), frozenset({0, 1})),
    Scenario("clean_chats", ("--match", "title", "--archive"), frozenset({0})),
    Scenario("clean_chats", ("--project", "<gizmo>", "--delete"), frozenset({0})),
    Scenario("delete_skill", ("<skill>",), frozenset({0, 2})),
    Scenario("search_chats", ("query",), frozenset({0, 1})),
    Scenario("list_automations", ("--filter", "all", "--prompts"), frozenset({0})),
    Scenario("list_skills", ("--apps",), frozenset({0, 1})),
    Scenario("manage_plugins", ("list",), frozenset({0})),
    Scenario("manage_plugins", ("show", "<plugin>"), frozenset({0})),
    Scenario("manage_plugins", ("install", "<plugin>"), frozenset({0})),
    Scenario("manage_plugins", ("uninstall", "<plugin>"), frozenset({0})),
    Scenario(
        "manage_plugins",
        ("skill", "<plugin>", "<plugin-skill>", "--disable"),
        frozenset({0}),
    ),
    Scenario(
        "manage_plugins", ("update", "<plugin>", "<plugin-archive>"), frozenset({0})
    ),
    Scenario(
        "manage_plugins", ("upload", "<plugin-archive>", "--dry-run"), frozenset({2})
    ),
    Scenario("read_chat", ("<chat>",), frozenset({0, 1})),
    Scenario("read_chat", ("<chat>", "--text"), frozenset({0}), ASSISTANT_TEXT),
    Scenario("read_chat", ("<chat>", "--effort"), frozenset({0}), ASSISTANT_TEXT),
    Scenario("pin_chat", ("<chat>",), frozenset({0})),
    Scenario(
        "deep_research",
        ("status", "--run", "<run>"),
        frozenset({0, 1, 3}),
        (("role", "tool"),),
    ),
)


def _gizmo_name(shapes: dict[str, Any]) -> str:
    """The display name the synthesized sandbox project carries, so the
    sandbox check takes its full path (it reads the chats only when the
    name matches)."""
    body = api_shapes.synthesize(shapes["gizmo"])
    return str(((body.get("gizmo") or {}).get("display") or {}).get("name") or "")


def _send_gates(_session: Any) -> dict[str, Any]:
    return {
        "proofofwork": {"required": True},
        "turnstile": {"required": True},
        "so": {"required": True},
    }


def _functions() -> dict[str, Callable[[Any], Any]]:
    import health
    import preflight

    return {
        "preflight.account_checks": preflight.account_checks,
        "preflight.run_checks": lambda s: preflight.run_checks(s, "", None),
        "preflight.run_checks project": lambda s: preflight.run_checks(s, GIZMO, None),
        "health.fetch_sandbox": lambda s: health.fetch_sandbox(
            s, {"id": GIZMO, "name": _gizmo_name(s.session.shapes)}
        ),
        "health.fetch_read_endpoints": health.fetch_read_endpoints,
        "health.fetch_skills_inventory": lambda s: health.fetch_skills_inventory(s),
    }


FUNCTION_NAMES: tuple[str, ...] = (
    "preflight.account_checks",
    "preflight.run_checks",
    "preflight.run_checks project",
    "health.fetch_sandbox",
    "health.fetch_read_endpoints",
    "health.fetch_skills_inventory",
)


@dataclass
class Outcome:
    code: int | None
    output: str
    calls: list[tuple[str, str]]
    result: Any = None
    unrecorded: tuple[str, ...] = ()


def run_scenario(
    scenario: Scenario, shapes: dict[str, Any], log: api_shapes.ReadLog | None
) -> Outcome:
    """``main(argv)`` of the scenario's command over ``shapes``."""
    inner = FakeInner(shapes, log, dict(scenario.prefer))
    session = client_session(inner)
    module = importlib.import_module(scenario.module)
    argv = [PLACEHOLDERS[a](shapes) if a in PLACEHOLDERS else a for a in scenario.argv]
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        if hasattr(module, "open_session"):
            stack.enter_context(
                mock.patch.object(module, "open_session", return_value=session)
            )
        stack.enter_context(
            mock.patch("chatgpt_client.ChatGPTSession", return_value=session)
        )
        stack.enter_context(contextlib.redirect_stdout(output))
        stack.enter_context(contextlib.redirect_stderr(output))
        try:
            code = module.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return Outcome(code, output.getvalue(), inner.calls, None, tuple(inner.unrecorded))


def run_function(
    name: str, shapes: dict[str, Any], log: api_shapes.ReadLog | None
) -> Outcome:
    """One of the preflight/health read functions over ``shapes``."""
    import preflight

    inner = FakeInner(shapes, log)
    session = client_session(inner)
    output = io.StringIO()
    with (
        mock.patch.object(preflight.psg, "fetch", _send_gates),
        contextlib.redirect_stdout(output),
    ):
        result = _functions()[name](session)
    return Outcome(
        None, output.getvalue(), inner.calls, result, tuple(inner.unrecorded)
    )


def read_paths(shapes: dict[str, Any]) -> dict[str, list[str]]:
    """Every field path the scenarios and functions read, per endpoint."""
    log = api_shapes.ReadLog()
    for scenario in SCENARIOS:
        run_scenario(scenario, shapes, log)
    for name in FUNCTION_NAMES:
        run_function(name, shapes, log)
    return log.as_json()
