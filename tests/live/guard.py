"""Guards a live-tier session so a test can only ever touch the sandbox.

Wraps something shaped like ``chatgpt_session.Session.call``. In tier
"read" no write reaches the account at all. In "write", "browser" and
"send" a write is allowed only inside the sandbox project — its gizmo, its
conversations, or a conversation id the guard has itself seen — never the
user's own chats or settings (TESTING.md section 1).

Two more rules exist so a T2 test can create and delete its own throwaway
project without ever being able to touch the sandbox that way: ``POST
/backend-api/projects`` is allowed only when the body's ``name`` starts
with ``"rp-test"``, and the id a successful call like that returns is
remembered in ``self.created``. ``DELETE /backend-api/gizmos/<id>`` is then
allowed only for an id in ``self.created`` -- and never for the sandbox id,
even if it were somehow present there too, because a project this guard did
not itself create must never be deletable through it.

Pure Python, no network: the rule is enforced before ``inner`` is ever
called, which is what lets it be tested with a fake in tests/test_harness.py.
"""

from __future__ import annotations

from typing import Any

CONVERSATION_PREFIX = "/backend-api/conversation/"
GIZMO_PREFIX = "/backend-api/gizmos/"
PROJECTS_PATH = "/backend-api/projects"


class GuardViolation(RuntimeError):
    """A test tried to act outside its tier, or outside the sandbox."""


def _stripped(path: str) -> str:
    """``path`` with its query string removed."""
    return path.split("?", 1)[0]


def _is_or_is_under(path: str, base: str) -> bool:
    """True if ``path`` is exactly ``base`` or one of its sub-paths.

    A bare ``startswith(base)`` would also match a sibling id that happens
    to share ``base`` as a text prefix (``g-p-sand`` vs. ``g-p-sandwich``),
    so the sub-path form always requires the separating slash.
    """
    return path == base or path.startswith(base + "/")


def _created_project_id(body: Any) -> str:
    """The new project's id from a ``POST /backend-api/projects`` response,
    or "" if ``body`` is not shaped like the capture (``{"resource":
    {"gizmo": {"id": "g-p-...", ...}}, ...}``)."""
    if not isinstance(body, dict):
        return ""
    gizmo = ((body.get("resource") or {}).get("gizmo")) or {}
    new_id = str(gizmo.get("id") or "")
    return new_id if new_id.startswith("g-p-") else ""


class GuardedSession:
    """Wraps a session's ``.call`` and refuses anything outside the sandbox."""

    def __init__(
        self,
        inner: Any,
        tier: str,
        sandbox_id: str = "",
        known_ids: Any = None,
    ) -> None:
        self.inner = inner
        self.tier = tier
        self.sandbox_id = sandbox_id
        self.known: set[str] = set(known_ids or [])
        self.created: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def _check(self, path: str, method: str, payload: Any) -> None:
        """Raise GuardViolation before ``inner`` ever sees a disallowed call."""
        if method == "GET":
            return
        if self.tier == "read":
            raise GuardViolation(f"{method} {path}: tier 'read' allows GET only")
        if not self.sandbox_id:
            raise GuardViolation(f"{method} {path}: no sandbox id, every write refused")
        stripped = _stripped(path)

        if method == "DELETE" and stripped.startswith(GIZMO_PREFIX):
            gizmo_id = stripped[len(GIZMO_PREFIX) :]
            if gizmo_id != self.sandbox_id and gizmo_id in self.created:
                return
            raise GuardViolation(
                f"{method} {path}: DELETE is only allowed for a project this "
                "guard created itself, and never for the sandbox"
            )

        if method == "POST" and stripped == PROJECTS_PATH:
            name = str((payload or {}).get("name", ""))
            if name.startswith("rp-test"):
                return
            raise GuardViolation(
                f"{method} {path}: a project created through the guard must "
                "have a name starting with 'rp-test'"
            )

        if _is_or_is_under(stripped, f"/backend-api/gizmos/{self.sandbox_id}"):
            return
        if _is_or_is_under(stripped, f"/backend-api/projects/{self.sandbox_id}"):
            return
        if stripped.startswith(CONVERSATION_PREFIX):
            conversation_id = stripped[len(CONVERSATION_PREFIX) :]
            if conversation_id in self.known:
                return
        raise GuardViolation(f"{method} {path}: outside the sandbox")

    def call(
        self,
        path: str,
        method: str = "GET",
        payload: Any = None,
        raw: bool = False,
        retries: int = 3,
    ) -> tuple[int, Any]:
        self._check(path, method, payload)
        status, body = self.inner.call(
            path, method=method, payload=payload, raw=raw, retries=retries
        )
        self.calls.append((method, path))
        if method == "POST" and _stripped(path) == PROJECTS_PATH and status == 200:
            new_id = _created_project_id(body)
            if new_id:
                self.created.add(new_id)
        return status, body

    def refresh(self) -> tuple[int, Any]:
        """GET the sandbox's conversations and learn every id in it."""
        status, body = self.call(
            f"/backend-api/gizmos/{self.sandbox_id}/conversations?cursor=0"
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                self.known.add(item["id"])
        return status, body

    def note(self, conversation_id: str) -> None:
        """Remember an id this test just created, so it may act on it too."""
        self.known.add(conversation_id)
