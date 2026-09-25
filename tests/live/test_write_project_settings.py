"""Live round trip for scripts/project_settings.py (T2, TESTING.md section 1).

Exercises the same pure functions the command sends through --
``patch_body`` and ``verify`` -- against the real sandbox project, over the
guarded session, so this is the actual ``PATCH .../projects/<id>`` the
command makes, not a re-implementation of it. Every path stays inside what
tests/live/guard.py allows for a T2 test: ``/backend-api/gizmos/<sandbox_id>``
and ``/backend-api/projects/<sandbox_id>``. Everything is restored in a
``finally`` block, and the sandbox is asserted to end project-only memory
(Stage 2's exit criterion) whatever happens partway through.

Run this test only through its opt-in live tier. See TESTING.md and
VERIFICATION.md for current execution results.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import project_settings  # noqa: E402
from list_projects import project_of  # noqa: E402

pytestmark = pytest.mark.live_write


def _gizmo(live_session: Any, sandbox_id: str) -> dict[str, Any]:
    """GET gizmos/<sandbox_id>, the same read ``project_settings.py`` opens with."""
    status, payload = live_session.call(project_settings.GIZMO.format(id=sandbox_id))
    assert status == 200, f"gizmos/{sandbox_id}: HTTP {status}"
    return payload


def _apply(
    live_session: Any,
    sandbox_id: str,
    *,
    instructions: str | None = None,
    memory: str | None = None,
) -> dict[str, Any]:
    """The command's own round trip, inline: read, PATCH, read again."""
    current = _gizmo(live_session, sandbox_id)
    body = project_settings.patch_body(
        current, instructions=instructions, memory=memory
    )
    status, _resp = live_session.call(
        project_settings.PROJECT.format(id=sandbox_id), method="PATCH", payload=body
    )
    assert status == 200, f"PATCH projects/{sandbox_id}: HTTP {status}"
    return _gizmo(live_session, sandbox_id)


def test_setting_and_restoring_the_sandbox_instructions_round_trips(
    live_session, sandbox_id
) -> None:
    """The instructions really change on the account, and really go back."""
    before = project_of(_gizmo(live_session, sandbox_id))
    previous_text = before["instructions"]
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    probe_text = f"rp-test instructions {stamp}"

    try:
        after = project_of(_apply(live_session, sandbox_id, instructions=probe_text))
        mismatches = project_settings.verify(
            before, after, {"instructions": probe_text}
        )
        assert mismatches == [], mismatches
    finally:
        restored = project_of(
            _apply(live_session, sandbox_id, instructions=previous_text)
        )
        mismatches = project_settings.verify(
            before, restored, {"instructions": previous_text}
        )
        assert mismatches == [], f"sandbox instructions NOT restored: {mismatches}"


def test_switching_memory_scope_round_trips_and_ends_project_only(
    live_session, sandbox_id
) -> None:
    """Stage 2's exit criterion is that the sandbox runs project-only memory;
    this restores it in ``finally`` however far the test got."""
    before = project_of(_gizmo(live_session, sandbox_id))
    assert before["memory_scope"] == "project_v2", (
        "sandbox must already be project-only; fix it by hand, then re-run"
    )

    try:
        after_default = project_of(_apply(live_session, sandbox_id, memory="global"))
        mismatches = project_settings.verify(
            before, after_default, {"memory_scope": "global"}
        )
        assert mismatches == [], mismatches
    finally:
        after_restore = project_of(
            _apply(live_session, sandbox_id, memory="project_v2")
        )
        mismatches = project_settings.verify(
            before, after_restore, {"memory_scope": "project_v2"}
        )
        assert mismatches == [], f"sandbox NOT restored to project-only: {mismatches}"
