"""Guards a live-tier session so a test can only ever touch the sandbox.

Wraps something shaped like ``chatgpt_session.Session.call``. In tier
"read" no write reaches the account at all. In "write", "browser" and
"send" a write is allowed only inside the sandbox project — its gizmo, its
conversations, or a conversation id the guard has itself seen — never the
user's own chats or settings (TESTING.md section 1).

Pure Python, no network: the rule is enforced before ``inner`` is ever
called, which is what lets it be tested with a fake in tests/test_harness.py.
"""

from __future__ import annotations

from typing import Any

CONVERSATION_PREFIX = "/backend-api/conversation/"


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
        self.calls: list[tuple[str, str]] = []

    def _check(self, path: str, method: str) -> None:
        """Raise GuardViolation before ``inner`` ever sees a disallowed call."""
        if method == "GET":
            return
        if self.tier == "read":
            raise GuardViolation(f"{method} {path}: tier 'read' allows GET only")
        if not self.sandbox_id:
            raise GuardViolation(f"{method} {path}: no sandbox id, every write refused")
        stripped = _stripped(path)
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
        self._check(path, method)
        status, body = self.inner.call(
            path, method=method, payload=payload, raw=raw, retries=retries
        )
        self.calls.append((method, path))
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
