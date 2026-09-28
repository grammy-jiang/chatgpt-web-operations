"""Loopback-tier fixtures (TESTING.md section 6, P6): the fake chatgpt.com
of ``server.py`` on 127.0.0.1, a synthetic cookie jar, and the skill's
session module pointed at both.

Every test here is marked ``loopback``: ``tests/conftest.py`` lets it open
sockets but refuses any name or connection that is not loopback. The real
keyring is never opened: ``CHATGPT_SESSION_STORE=0`` keeps the stored-token
copy off the bus, the jar holds no keyring-encrypted (v11) value, and
``_keyring_password`` is replaced by a function that fails the test.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from loopback.server import LoopbackServer, write_jar

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import api_shapes  # noqa: E402
import chatgpt_session  # noqa: E402


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    running = LoopbackServer(api_shapes.load(api_shapes.COMMITTED)).start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def jar(tmp_path: Path) -> Path:
    return write_jar(tmp_path / "chrome" / "Default" / "Cookies")


def _no_keyring(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the loopback tier opened the real keyring")


@pytest.fixture
def wired(
    server: LoopbackServer, jar: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> LoopbackServer:
    """The session module talks to ``server`` and reads ``jar``; nothing else
    about the HTTP path is changed."""
    monkeypatch.setattr(chatgpt_session, "BASE", server.url)
    monkeypatch.setattr(
        chatgpt_session,
        "BROWSERS",
        {
            "chrome": (jar, "chrome"),
            "chromium": (tmp_path / "no-chromium" / "Cookies", "chromium"),
        },
    )
    monkeypatch.setattr(chatgpt_session, "_keyring_password", _no_keyring)
    monkeypatch.setenv(chatgpt_session.SESSION_STORE_ENV, "0")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    return server
