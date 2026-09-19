"""Live round trip for scripts/create_project.py and scripts/delete_project.py
(T2, TESTING.md section 1).

Creates a throwaway project of its own -- never the sandbox -- through the
same pure functions the two commands send through (``create_project.
create_body`` / ``created_id`` / ``verify``, ``delete_project.chat_count`` /
``refusal``), calling ``live_session.call`` directly so this is the actual
``POST`` and ``DELETE`` the commands make, not a re-implementation of them.
``tests/live/guard.py`` is what actually enforces the boundary here: a
created project's name must start with "rp-test" or the POST itself is
refused, and the DELETE at the end is only ever allowed for the id this
test's own create returned -- never for ``sandbox_id``, which is read back
unchanged at the end. Deletion happens in a ``finally`` block, so a failed
check partway through still cleans up.

Not run by this agent (see the skill's HARD RULES); collect-only proves it
is wired up without touching the account:

    .venv/bin/python -m pytest tests/live/test_write_project_lifecycle.py \
        --collect-only -q
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

import create_project  # noqa: E402
import delete_project  # noqa: E402
from list_projects import project_of  # noqa: E402

pytestmark = pytest.mark.live_write


def test_creating_and_deleting_a_throwaway_project_round_trips(
    live_session: Any, sandbox_id: str
) -> None:
    """The full lifecycle, on a project this test made and owns, never on
    the sandbox -- deletion happens in ``finally`` even if a check fails
    partway through, so a throwaway project never survives a failed run."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    name = f"rp-test lifecycle {stamp}"
    body = create_project.create_body(name, "", "project_v2")

    status, resp = live_session.call(
        "/backend-api/projects", method="POST", payload=body
    )
    assert status == 200, f"POST /backend-api/projects: HTTP {status}"
    new_id = create_project.created_id(resp)
    assert new_id, f"no g-p-... id in the create response: {resp}"
    assert new_id != sandbox_id, "created a project but got the sandbox id back"

    try:
        read_status, gizmo_payload = live_session.call(
            create_project.GIZMO.format(id=new_id)
        )
        assert read_status == 200, f"gizmos/{new_id}: HTTP {read_status}"
        after = project_of(gizmo_payload)
        mismatches = create_project.verify(
            after, {"instructions": "", "memory_scope": "project_v2"}
        )
        assert mismatches == [], mismatches
        assert after["name"] == name

        conv_status, conv_body = live_session.call(
            f"/backend-api/gizmos/{new_id}/conversations?cursor=0&limit=100"
        )
        assert conv_status == 200, f"gizmos/{new_id}/conversations: HTTP {conv_status}"
        assert delete_project.chat_count(conv_body) == 0, (
            "a brand-new project must start with no chats in it"
        )

        assert delete_project.refusal({"id": new_id, "name": name}, name, False) is None
    finally:
        _delete_status, _resp = live_session.call(
            delete_project.GIZMO.format(id=new_id), method="DELETE", payload=None
        )
        confirm_status, _after = live_session.call(
            create_project.GIZMO.format(id=new_id)
        )
        assert confirm_status == 404, (
            f"cleanup failed: gizmos/{new_id} read back HTTP {confirm_status} "
            "after DELETE, wanted 404 -- a throwaway project was left behind"
        )

    sandbox_status, sandbox_after = live_session.call(
        create_project.GIZMO.format(id=sandbox_id)
    )
    assert sandbox_status == 200, "the sandbox project itself must still be reachable"
    assert project_of(sandbox_after)["name"] == "rp-test-sandbox", (
        "the sandbox must read back unchanged; this test must never mutate it"
    )
