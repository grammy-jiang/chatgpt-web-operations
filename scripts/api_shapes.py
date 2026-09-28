"""Response shapes of the chatgpt.com endpoints this skill reads.

A shape is what a response looks like without anything it says: field
names, JSON types, a format label for every string (``uuid``, ``datetime``,
``url``, an id with its type prefix, free ``text``...), the values seen for a
short list of enumeration fields, and whether a field was present in every
object observed at its place. A dict keyed by ids, paths or model names is a
*map*: its keys are dropped and only its value shape is kept. Nothing that
belongs to the account survives, so a shape is safe for the public mirror by
construction rather than by a sanitizer that has to be right.

Three uses (TESTING.md section 6, P5):

* **record** -- ``health.py --record-shapes DIR`` wraps its session in a
  ``RecordingSession``, notes the shape of every response it reads anyway,
  fetches the registered endpoints it did not read (``fill``), and writes
  ``DIR/<name>.shape.json``;
* **drift** -- ``compare`` sets a fresh recording against the committed
  shapes in ``tests/fixtures/http`` and names the fields that disappeared or
  changed type; only fields the skill's code actually reads
  (``tests/fixtures/http/read_paths.json``) make it a warning;
* **synthesize** -- ``synthesize`` builds a payload from a shape; the
  contract tests serve those payloads to every read command's ``main()``,
  which is how "does the skill still parse what the server returned" is
  answered offline and deterministically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
COMMITTED = SKILL_DIR / "tests" / "fixtures" / "http"
READ_PATHS_FILE = COMMITTED / "read_paths.json"
SHAPE_SUFFIX = ".shape.json"

# ---------------------------------------------------------------------------
# string formats
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(_UUID)
PREFIXED_UUID_RE = re.compile(rf"([A-Za-z_]{{1,24}}):{_UUID}")
GP_RE = re.compile(r"g-p-[0-9a-f]{32}")
GP_SLUG_RE = re.compile(r"g-p-[0-9a-f]{32}-[A-Za-z0-9-]+")
PREFIXED_HEX_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_)([0-9a-f]{16,})"
)
ACCOUNT_ID_RE = re.compile(r"(user|org)-[A-Za-z0-9]{6,}")
HEX_RE = re.compile(r"[0-9a-f]{12,}")
DATETIME_RE = re.compile(
    r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?"
)
DATE_RE = re.compile(r"\d{4}-\d\d-\d\d")
URL_RE = re.compile(r"(?:https?|wss?)://\S+")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")
TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_.:-]{24,}")
ENUM_VALUE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

# Fields whose values are the server's own vocabulary (roles, states, kinds),
# never the account's words: their values are kept, up to MAX_ENUM_VALUES.
ENUM_KEYS = frozenset(
    {
        "auth_status",
        "auth_type",
        "authentication_policy",
        "connector_status",
        "connector_type",
        "content_type",
        "current_user_role",
        "discoverability",
        "distribution_channel",
        "executor",
        "gizmo_type",
        "installation_policy",
        "item_type",
        "kind",
        "match_kind",
        "memory_scope",
        "plan_type",
        "role",
        "safety_check_status",
        "scope",
        "source_key",
        "source_type",
        "status",
        "thread_mode",
        "timing_mode",
        "type",
        "visibility",
    }
)
MAX_ENUM_VALUES = 24
# Paging fields: synthesized as null when null was ever seen, so a walk over
# synthesized pages ends after the first one.
PAGINATION_KEYS = frozenset({"cursor", "next_cursor", "next_page_token"})

FORMAT_PRIORITY = (
    "uuid",
    "prefixed-uuid:",
    "account-id:",
    "id:",
    "g-p-id",
    "g-p-id-slug",
    "datetime",
    "date",
    "url",
    "numeric",
    "enum",
    "email",
    "hexid",
    "token",
    "text",
    "empty",
)
TYPE_PRIORITY = (
    "object",
    "map",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
)


def string_format(value: str, key: str | None = None) -> str:
    """The format label of ``value``: what kind of string it is, never
    what it says."""
    if value == "":
        return "empty"
    if UUID_RE.fullmatch(value):
        return "uuid"
    match = PREFIXED_UUID_RE.fullmatch(value)
    if match:
        return f"prefixed-uuid:{match.group(1)}"
    if GP_RE.fullmatch(value):
        return "g-p-id"
    if GP_SLUG_RE.fullmatch(value):
        return "g-p-id-slug"
    match = ACCOUNT_ID_RE.fullmatch(value)
    if match:
        return f"account-id:{match.group(1)}"
    match = PREFIXED_HEX_RE.fullmatch(value)
    if match:
        return f"id:{match.group(1)}"
    if DATETIME_RE.fullmatch(value):
        return "datetime"
    if DATE_RE.fullmatch(value):
        return "date"
    if URL_RE.fullmatch(value):
        return "url"
    if EMAIL_RE.fullmatch(value):
        return "email"
    if NUMERIC_RE.fullmatch(value):
        return "numeric"
    if key in ENUM_KEYS and ENUM_VALUE_RE.fullmatch(value):
        return "enum"
    if HEX_RE.fullmatch(value):
        return "hexid"
    if TOKEN_RE.fullmatch(value):
        return "token"
    return "text"


def is_field_name(key: str) -> bool:
    """A JSON field name, as opposed to data used as a key (an id, a path,
    a model name): identifier-shaped and not an id."""
    return (
        FIELD_NAME_RE.fullmatch(key) is not None
        and HEX_RE.fullmatch(key) is None
        and PREFIXED_HEX_RE.fullmatch(key) is None
    )


# ---------------------------------------------------------------------------
# infer and merge
# ---------------------------------------------------------------------------


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        if all(is_field_name(str(k)) for k in value):
            return "object"
        return "map"
    return "string"  # anything else JSON cannot hold is reported as text


def infer(
    value: Any,
    key: str | None = None,
    path: str = "",
    opaque: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The shape of one JSON value observed under field ``key`` at ``path``.
    A container at a path in ``opaque`` keeps only its type: its insides are
    somebody else's structure (an MCP server's own tool schemas, the names
    of the apps installed), not the API's, and not ours to record."""
    kind = _type_of(value)
    node: dict[str, Any] = {"types": [kind]}
    if path in opaque and kind in ("object", "map", "array"):
        node["opaque"] = True
        return node
    if kind == "boolean":
        node["booleans"] = [bool(value)]
    elif kind == "string":
        fmt = string_format(str(value), key)
        node["formats"] = [fmt]
        if fmt == "enum":
            node["enum"] = [str(value)]
    elif kind == "array":
        items = None
        for element in value:
            shape = infer(element, key, f"{path}[]", opaque)
            items = shape if items is None else merge(items, shape)
        node["items"] = items
    elif kind == "object":
        node["fields"] = {
            str(k): {
                **infer(v, str(k), f"{path}.{k}" if path else str(k), opaque),
                "optional": False,
            }
            for k, v in value.items()
        }
    elif kind == "map":
        values = None
        for element in value.values():
            shape = infer(element, None, f"{path}{{}}", opaque)
            values = shape if values is None else merge(values, shape)
        node["values"] = values
    return node


def _union(a: Iterable[Any], b: Iterable[Any], limit: int | None = None) -> list:
    merged = sorted(set(a) | set(b), key=lambda v: (str(type(v)), v))
    return merged[:limit] if limit else merged


def merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> Any:
    """The shape of a place where both ``a`` and ``b`` were observed; ``None``
    (an array or a map never seen with an element) merges as nothing."""
    if a is None or b is None:
        other = a if b is None else b
        return None if other is None else dict(other)
    out: dict[str, Any] = {"types": _union(a["types"], b["types"])}
    if a.get("opaque") or b.get("opaque"):
        out["opaque"] = True
        if "optional" in a or "optional" in b:
            out["optional"] = bool(a.get("optional")) or bool(b.get("optional"))
        return out
    for listed in ("booleans", "formats"):
        if listed in a or listed in b:
            out[listed] = _union(a.get(listed, []), b.get(listed, []))
    if "enum" in a or "enum" in b:
        out["enum"] = _union(a.get("enum", []), b.get("enum", []), MAX_ENUM_VALUES)
    if "items" in a or "items" in b:
        out["items"] = merge(a.get("items"), b.get("items"))
    if "values" in a or "values" in b:
        out["values"] = merge(a.get("values"), b.get("values"))
    if "fields" in a or "fields" in b:
        fa, fb = a.get("fields"), b.get("fields")
        if fa is None or fb is None:
            out["fields"] = dict(fa or fb or {})
        else:
            fields: dict[str, Any] = {}
            for name in sorted(set(fa) | set(fb)):
                if name in fa and name in fb:
                    child = merge(fa[name], fb[name])
                    child["optional"] = bool(fa[name].get("optional")) or bool(
                        fb[name].get("optional")
                    )
                else:
                    child = {**(fa.get(name) or fb.get(name)), "optional": True}
                fields[name] = child
            out["fields"] = fields
    if "optional" in a or "optional" in b:
        out["optional"] = bool(a.get("optional")) or bool(b.get("optional"))
    return out


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


def _comparable(types: Iterable[str]) -> set[str]:
    """Types as compared for drift: null never counts, an integral number is
    a number, and a dict is a dict whether its keys looked like fields."""
    out = set()
    for kind in types:
        if kind == "null":
            continue
        out.add({"integer": "number", "map": "object"}.get(kind, kind))
    return out


def drift(fresh: dict[str, Any], committed: dict[str, Any], path: str = "") -> list:
    """How ``fresh`` differs from ``committed``, as ``(path, change, detail)``
    with change one of ``removed`` (a field present in every committed
    observation is missing where its parent is present), ``retyped`` (the
    non-null types share nothing) and ``added`` (a field never seen)."""
    out: list[tuple[str, str, str]] = []
    old_types, new_types = _comparable(committed["types"]), _comparable(fresh["types"])
    if old_types and new_types and old_types.isdisjoint(new_types):
        return [
            (
                path or "(root)",
                "retyped",
                f"{'/'.join(sorted(old_types))} -> {'/'.join(sorted(new_types))}",
            )
        ]
    if committed.get("opaque") or fresh.get("opaque"):
        return out
    old_fields, new_fields = committed.get("fields"), fresh.get("fields")
    if old_fields is not None and new_fields is not None:
        for name in sorted(set(old_fields) | set(new_fields)):
            child = f"{path}.{name}" if path else name
            if name not in new_fields:
                if not old_fields[name].get("optional"):
                    out.append((child, "removed", ""))
            elif name not in old_fields:
                out.append((child, "added", ""))
            else:
                out += drift(new_fields[name], old_fields[name], child)
    for part, marker in (("items", "[]"), ("values", "{}")):
        old_part, new_part = committed.get(part), fresh.get(part)
        if old_part is not None and new_part is not None:
            out += drift(new_part, old_part, f"{path}{marker}")
    return out


# ---------------------------------------------------------------------------
# synthesize, and the log of what code read from a synthesized payload
# ---------------------------------------------------------------------------


class ReadLog:
    """The paths code read from synthesized payloads, per endpoint."""

    def __init__(self) -> None:
        self.paths: dict[str, set[str]] = {}

    def note(self, endpoint: str, path: str) -> None:
        self.paths.setdefault(endpoint, set()).add(path)

    def as_json(self) -> dict[str, list[str]]:
        return {name: sorted(paths) for name, paths in sorted(self.paths.items())}


class TrackedDict(dict):
    """A dict that notes every key read through ``[]``, ``get`` or ``in``."""

    def __init__(self, data: dict, log: ReadLog, endpoint: str, path: str, is_map):
        super().__init__(data)
        self._log, self._endpoint, self._path, self._is_map = (
            log,
            endpoint,
            path,
            is_map,
        )

    def _child(self, key: Any) -> str:
        if self._is_map:
            return f"{self._path}{{}}"
        return f"{self._path}.{key}" if self._path else str(key)

    def __getitem__(self, key: Any) -> Any:
        self._log.note(self._endpoint, self._child(key))
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._log.note(self._endpoint, self._child(key))
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        self._log.note(self._endpoint, self._child(key))
        return super().__contains__(key)


class _Synth:
    def __init__(
        self, log: ReadLog | None, endpoint: str, prefer: dict[str, Any]
    ) -> None:
        self.log, self.endpoint, self.prefer = log, endpoint, prefer
        self.counters: dict[str, int] = {}

    def next(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    def value(self, node: dict[str, Any] | None, key: str | None, i: int, path: str):
        if node is None:
            return None
        if key in self.prefer and not {"object", "map", "array"} & set(
            node.get("types", [])
        ):
            return self.prefer[key]
        types = [t for t in TYPE_PRIORITY if t in node.get("types", [])]
        if key in PAGINATION_KEYS and "null" in types:
            return None
        kind = types[0] if types else "null"
        if node.get("opaque") and kind in ("object", "map", "array"):
            return [] if kind == "array" else self.wrap({}, path, kind == "map")
        if kind == "object":
            data = {
                name: self.value(child, name, i, f"{path}.{name}" if path else name)
                for name, child in (node.get("fields") or {}).items()
            }
            _link_tree(data)
            return self.wrap(data, path, is_map=False)
        if kind == "map":
            values = node.get("values")
            data = {}
            if values is not None:
                for n in range(2):
                    data[self.uuid()] = self.value(values, None, n, f"{path}{{}}")
            return self.wrap(data, path, is_map=True)
        if kind == "array":
            items = node.get("items")
            if items is None:
                return []
            return [self.value(items, key, n, f"{path}[]") for n in range(2)]
        if kind == "string":
            return self.string(node, key, i)
        if kind == "number":
            return 1.5 + i
        if kind == "integer":
            return 1 + i
        if kind == "boolean":
            seen = node.get("booleans") or [False]
            ordered = sorted(seen, reverse=True)
            return ordered[i % len(ordered)]
        return None

    def wrap(self, data: dict, path: str, is_map: bool) -> dict:
        if self.log is None:
            return data
        return TrackedDict(data, self.log, self.endpoint, path, is_map)

    def uuid(self) -> str:
        return f"00000000-0000-4000-8000-{self.next('uuid'):012d}"

    def string(self, node: dict[str, Any], key: str | None, i: int) -> str:
        formats = node.get("formats") or ["text"]
        fmt = min(
            formats,
            key=lambda f: next(
                (n for n, p in enumerate(FORMAT_PRIORITY) if f.startswith(p)),
                len(FORMAT_PRIORITY),
            ),
        )
        if fmt == "uuid":
            return self.uuid()
        if fmt.startswith("prefixed-uuid:"):
            return f"{fmt.split(':', 1)[1]}:{self.uuid()}"
        if fmt == "g-p-id":
            return f"g-p-{self.next('gp'):032d}"
        if fmt == "g-p-id-slug":
            return f"g-p-{self.next('gp'):032d}-slug"
        if fmt.startswith("account-id:"):
            return f"{fmt.split(':', 1)[1]}-XXXXXXXX"
        if fmt.startswith("id:"):
            return f"{fmt.split(':', 1)[1]}{self.next('id'):032x}"
        if fmt == "datetime":
            return f"2026-01-{1 + i % 28:02d}T00:00:00Z"
        if fmt == "date":
            return f"2026-01-{1 + i % 28:02d}"
        if fmt == "url":
            return f"https://example.invalid/{self.next('url')}"
        if fmt == "numeric":
            return str(1 + i)
        if fmt == "enum":
            values = node.get("enum") or ["x"]
            return values[i % len(values)]
        if fmt == "email":
            return "user@example.invalid"
        if fmt == "hexid":
            return f"deadbeef{self.next('hex'):024x}"
        if fmt == "token":
            return "t" * 48
        if fmt == "empty":
            return ""
        return f"{key or 'text'} {self.next(key or 'text')}"


def _link_tree(data: dict[str, Any]) -> None:
    """ChatGPT's conversation tree, linked: ``mapping`` is keyed by node id,
    each node names its ``parent`` and ``children``, and ``current_node`` is
    the leaf. Synthesized one field at a time they would point at nothing
    and a transcript walk would stop at once; linked, it reaches every
    message. Reads go through ``dict`` itself so nothing is logged."""
    mapping = dict.get(data, "mapping")
    if not isinstance(mapping, dict) or "current_node" not in dict.keys(data):
        return
    keys = list(dict.keys(mapping))
    for n, key in enumerate(keys):
        node = dict.get(mapping, key)
        if not isinstance(node, dict):
            continue
        fields = dict.keys(node)
        if "id" in fields:
            dict.__setitem__(node, "id", key)
        if "parent" in fields:
            dict.__setitem__(node, "parent", keys[n - 1] if n else None)
        if "children" in fields:
            dict.__setitem__(node, "children", keys[n + 1 : n + 2])
    if keys:
        dict.__setitem__(data, "current_node", keys[-1])


def synthesize(
    node: dict[str, Any],
    log: ReadLog | None = None,
    endpoint: str = "",
    prefer: dict[str, Any] | None = None,
) -> Any:
    """A deterministic payload with exactly ``node``'s shape: every field
    present, two elements per array and map, strings of each field's format,
    enumeration values in the order they were seen, a conversation tree
    linked (``_link_tree``). ``prefer`` sets the value of every scalar field
    with that name (a test's choice, e.g. ``role: assistant``). With ``log``
    every dict notes which keys code reads from it (``TrackedDict``)."""
    return _Synth(log, endpoint, dict(prefer or {})).value(node, None, 0, "")


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class Endpoint:
    """One read the skill makes. ``template`` is the path with ``{name}``
    placeholders; ``fetch`` is what ``fill`` requests when a run did not read
    it already (``None``: never fetched by ``fill``)."""

    name: str
    method: str
    template: str
    fetch: str | None = None
    needs: str = ""
    opaque: frozenset[str] = frozenset()

    @property
    def pattern(self) -> re.Pattern[str]:
        parts = PLACEHOLDER_RE.split(self.template)
        return re.compile("[^/]+".join(re.escape(p) for p in parts))

    @property
    def folded(self) -> str:
        """The template in the consistency tests' placeholder form."""
        return PLACEHOLDER_RE.sub("<id>", self.template)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("me", "GET", "/backend-api/me", "/backend-api/me"),
    Endpoint(
        "conversations",
        "GET",
        "/backend-api/conversations",
        "/backend-api/conversations?offset=0&limit=3&order=updated",
    ),
    Endpoint(
        "conversation",
        "GET",
        "/backend-api/conversation/{conversation_id}",
        "/backend-api/conversation/{conversation_id}",
        needs="conversation_id",
    ),
    Endpoint(
        "conversation_widgets",
        "GET",
        "/backend-api/conversations/{conversation_id}",
        "/backend-api/conversations/{conversation_id}",
        needs="conversation_id",
    ),
    Endpoint("pins", "GET", "/backend-api/pins", "/backend-api/pins"),
    Endpoint(
        "models",
        "GET",
        "/backend-api/models",
        "/backend-api/models?iim=false&is_gizmo=false"
        "&supports_model_picker_upgrade_presets=true",
    ),
    Endpoint(
        "settings_user",
        "GET",
        "/backend-api/settings/user",
        "/backend-api/settings/user",
    ),
    Endpoint(
        "memories",
        "GET",
        "/backend-api/memories",
        "/backend-api/memories?include_memory_entries=false",
    ),
    Endpoint(
        "user_system_messages",
        "GET",
        "/backend-api/user_system_messages",
        "/backend-api/user_system_messages",
    ),
    Endpoint("wham_usage", "GET", "/backend-api/wham/usage", "/backend-api/wham/usage"),
    Endpoint(
        "sidebar",
        "GET",
        "/backend-api/gizmos/snorlax/sidebar",
        "/backend-api/gizmos/snorlax/sidebar?owned_only=true&limit=5",
    ),
    Endpoint(
        "gizmo",
        "GET",
        "/backend-api/gizmos/{gizmo_id}",
        "/backend-api/gizmos/{gizmo_id}",
        needs="gizmo_id",
    ),
    Endpoint(
        "gizmo_conversations",
        "GET",
        "/backend-api/gizmos/{gizmo_id}/conversations",
        "/backend-api/gizmos/{gizmo_id}/conversations?cursor=0&limit=50",
        needs="gizmo_id",
    ),
    Endpoint(
        "hazelnuts",
        "GET",
        "/backend-api/hazelnuts",
        "/backend-api/hazelnuts?include_permissions=true&scope=installed",
    ),
    Endpoint(
        "plugins_installed",
        "GET",
        "/backend-api/ps/plugins/installed",
        "/backend-api/ps/plugins/installed?limit=1000",
        # keyed by the names of the apps a plugin bundles: changes whenever
        # an app is installed, and is the plugin's manifest, not the API
        opaque=frozenset({"plugins[].release.app_manifest"}),
    ),
    Endpoint(
        "automations",
        "GET",
        "/backend-api/automations",
        "/backend-api/automations?filter=scheduled",
    ),
    Endpoint(
        "connector_tunnels",
        "GET",
        "/backend-api/aip/connectors/mcp/tunnels",
        "/backend-api/aip/connectors/mcp/tunnels",
    ),
    Endpoint(
        "connector_links",
        "POST",
        "/backend-api/aip/connectors/links/list_accessible",
        "/backend-api/aip/connectors/links/list_accessible",
        needs="user_id",
    ),
    Endpoint(
        "connector_batch",
        "POST",
        "/backend-api/aip/connectors/batch",
        "/backend-api/aip/connectors/batch",
        needs="connector_ids",
        # the connected MCP servers' own tool schemas: their parameter and
        # result field names belong to the server, not to the API
        opaque=frozenset(
            {
                "connectors[].action_param_schemas",
                "connectors[].actions[].params",
                "connectors[].actions[].return_type",
                "connectors[].link_params_schema",
            }
        ),
    ),
    Endpoint(
        "global_search",
        "POST",
        "/backend-api/global/search",
        "/backend-api/global/search",
    ),
)
BY_NAME = {endpoint.name: endpoint for endpoint in ENDPOINTS}


def endpoint_for(method: str, path: str) -> Endpoint | None:
    """The registered endpoint ``method path`` is, or ``None``. Only the
    method an endpoint is registered with matches: a PATCH to a conversation
    is a write, not the conversation read."""
    bare = path.split("?", 1)[0]
    for endpoint in ENDPOINTS:
        if endpoint.method == method.upper() and endpoint.pattern.fullmatch(bare):
            return endpoint
    return None


def fetch_request(
    endpoint: Endpoint, context: dict[str, Any]
) -> tuple[str, Any] | None:
    """``(path, payload)`` for ``fill`` to request ``endpoint``, or ``None``
    when what it needs is not known (no conversation, no connector)."""
    if endpoint.fetch is None:
        return None
    if endpoint.needs == "conversation_id":
        if not context.get("conversation_id"):
            return None
        return endpoint.fetch.format(conversation_id=context["conversation_id"]), None
    if endpoint.needs == "gizmo_id":
        if not context.get("gizmo_id"):
            return None
        return endpoint.fetch.format(gizmo_id=context["gizmo_id"]), None
    if endpoint.needs == "user_id":
        if not context.get("user_id"):
            return None
        return endpoint.fetch, {
            "principals": [{"type": "USER", "id": context["user_id"]}]
        }
    if endpoint.needs == "connector_ids":
        if not context.get("connector_ids"):
            return None
        return endpoint.fetch, {
            "connector_ids": list(context["connector_ids"])[:5],
            "include_actions": True,
        }
    if endpoint.name == "global_search":
        return endpoint.fetch, {
            "query": "the",
            "limit": 5,
            "source_requests": [{"type": "conversation"}],
        }
    return endpoint.fetch, None


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


class Recording:
    """Shapes observed during one run, by endpoint name. Bodies are kept in
    memory only, to find the ids ``fill`` needs; they are never written."""

    def __init__(self) -> None:
        self.shapes: dict[str, dict[str, Any]] = {}
        self.bodies: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    def observe(self, method: str, path: str, status: int, body: Any) -> None:
        endpoint = endpoint_for(method, path)
        if endpoint is None:
            return
        if status != 200 or not isinstance(body, dict | list):
            self.errors[endpoint.name] = f"HTTP {status}"  # the latest failure
            return
        self.errors.pop(endpoint.name, None)
        shape = infer(body, opaque=endpoint.opaque)
        self.shapes[endpoint.name] = merge(self.shapes.get(endpoint.name), shape)
        self.bodies[endpoint.name] = body

    def context(self, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ids for ``fill``: the newest conversation, the connectors' ids."""
        context = dict(base or {})
        listing = self.bodies.get("conversations")
        if isinstance(listing, dict):
            for item in listing.get("items") or []:
                if isinstance(item, dict) and item.get("id"):
                    context.setdefault("conversation_id", str(item["id"]))
                    break
        links = self.bodies.get("connector_links")
        if isinstance(links, dict):
            ids = [
                str(link.get("connector_id"))
                for link in links.get("links") or []
                if isinstance(link, dict)
                and str(link.get("connector_id") or "").startswith("asdk_app_")
            ]
            if ids:
                context.setdefault("connector_ids", ids)
        return context


class RecordingSession:
    """Wraps a ``chatgpt_session.Session``: every call goes through
    unchanged, and its response is observed. Every other attribute
    (``user_id``, ``token``, ``cookie``) is the wrapped session's."""

    def __init__(self, inner: Any, recording: Recording) -> None:
        self._inner = inner
        self._recording = recording

    def call(self, path: str, method: str = "GET", payload: Any = None, **kwargs):
        status, body = self._inner.call(path, method=method, payload=payload, **kwargs)
        if not kwargs.get("raw"):
            self._recording.observe(method, path, status, body)
        return status, body

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def fill(session: Any, recording: Recording, base: dict[str, Any]) -> list[str]:
    """Request every registered endpoint the run has not read yet, in
    registry order (connector links before the batch that needs their ids),
    through ``session`` -- a ``RecordingSession``, which observes each
    response; return the names requested. A failure is noted in
    ``recording.errors`` and never raised: a shape is evidence, not a check
    of its own."""
    requested: list[str] = []
    for endpoint in ENDPOINTS:
        if endpoint.name in recording.shapes:
            continue
        request = fetch_request(endpoint, recording.context(base))
        if request is None:
            recording.errors.setdefault(
                endpoint.name, "not requested: no id to ask for"
            )
            continue
        path, payload = request
        requested.append(endpoint.name)
        try:
            session.call(path, method=endpoint.method, payload=payload)
        except Exception as exc:  # the transport's failures have no common base
            recording.errors[endpoint.name] = str(exc)[:120]
    return requested


def write(recording: Recording, out_dir: str | Path) -> Path:
    """``<name>.shape.json`` per recorded endpoint, and ``index.json``."""
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for name, shape in sorted(recording.shapes.items()):
        (out / f"{name}{SHAPE_SUFFIX}").write_text(
            json.dumps(shape, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    index = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "recorded": sorted(recording.shapes),
        "not_recorded": dict(sorted(recording.errors.items())),
    }
    (out / "index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out


def load(directory: str | Path) -> dict[str, dict[str, Any]]:
    """The shapes in ``directory``, by endpoint name."""
    shapes: dict[str, dict[str, Any]] = {}
    base = Path(directory).expanduser()
    if not base.is_dir():
        return shapes
    for path in sorted(base.glob(f"*{SHAPE_SUFFIX}")):
        shapes[path.name[: -len(SHAPE_SUFFIX)]] = json.loads(
            path.read_text(encoding="utf-8")
        )
    return shapes


def read_paths(path: str | Path = READ_PATHS_FILE) -> dict[str, set[str]] | None:
    """The fields the skill's code reads, per endpoint, or ``None`` when the
    file is missing (then every difference counts)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {name: set(paths) for name, paths in data.items()}


def _is_read(path: str, read: set[str]) -> bool:
    """``path`` or anything under it is read (a removed object takes every
    field under it along)."""
    return any(
        r == path or r.startswith((f"{path}.", f"{path}[", f"{path}{{")) for r in read
    )


def compare(
    fresh: dict[str, dict[str, Any]],
    committed: dict[str, dict[str, Any]],
    read: dict[str, set[str]] | None,
) -> dict[str, Any]:
    """The drift report of a recording against the committed shapes:
    ``breaking`` (removed or retyped fields the code reads, or every one when
    ``read`` is None), ``other`` (the rest), ``added`` (new fields),
    ``unknown`` (recorded endpoints with no committed shape)."""
    report: dict[str, Any] = {"breaking": [], "other": [], "added": [], "unknown": []}
    for name, shape in sorted(fresh.items()):
        if name not in committed:
            report["unknown"].append(name)
            continue
        for path, change, detail in drift(shape, committed[name]):
            entry = f"{name}: {path} {change}" + (f" ({detail})" if detail else "")
            if change == "added":
                report["added"].append(entry)
            elif read is None or _is_read(path, read.get(name, set())):
                report["breaking"].append(entry)
            else:
                report["other"].append(entry)
    return report
