"""Tier T1 smoke test: skills and apps installed on the account
(ROADMAP.md R5) against the real account.

Marker live_read, skipped unless CHATGPT_LIVE=read (tests/conftest.py), same
pattern as tests/live/test_read_search.py. Shape only, never the user's own
data (TESTING.md section 2): a skill's description or sample prompts are
never asserted on here, only that the documented keys are present with the
right kind of value.

The last test proves ``list_skills.py``'s ``main()`` works end to end
against the real account, through the guarded session -- the same pattern
``tests/live/test_read_preflight.py`` uses for ``open_chatgpt_session``,
adapted here for ``_common.open_session``. Until 2026-09-25 it also
required the ``research-pipeline`` upload (``--expect research-pipeline``).
That requirement was the implementing agent's own choice on 2026-09-21,
taken from ROADMAP R5's value line; the user had asked only for a
scheduled check that reports blocked skills, and earlier text here that
called it "the expectation the user set" was wrong. The user had the
upload deleted on 2026-09-25, and no particular skill is required any
more.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import list_skills  # noqa: E402

pytestmark = pytest.mark.live_read


def test_hazelnuts_returns_shaped_skills(live_session) -> None:
    status, body = live_session.call(
        "/backend-api/hazelnuts?include_permissions=true&scope=installed"
    )
    assert status == 200
    items = body["hazelnuts"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item["name"], str)
        assert isinstance(item["enabled"], bool)
        assert "safety_check_status" in item
        assert "latest_version_no" in item


def test_plugins_installed_returns_shaped_apps(live_session) -> None:
    status, body = live_session.call("/backend-api/ps/plugins/installed?limit=1000")
    assert status == 200
    assert isinstance(body["plugins"], list)
    assert isinstance(body["pagination"], dict)


def test_list_skills_main_exits_0_through_the_guarded_session(
    live_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatch list_skills.open_session, the way
    tests/live/test_read_preflight.py monkeypatches open_chatgpt_session, so
    main() makes its real HTTP calls through the guarded live_session rather
    than opening a fresh, unguarded one (_common.open_session returns an
    object whose .session is what main() calls .call on; SimpleNamespace
    reproduces just that shape)."""
    monkeypatch.setattr(
        list_skills,
        "open_session",
        lambda *a, **k: SimpleNamespace(session=live_session),
    )
    assert list_skills.main([]) == 0
