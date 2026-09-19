"""Shared pytest setup: the T0 network guard and the live-tier gate.

Every rule here defends one property from TESTING.md section 1: a test
without a live_* marker must never reach the network, and a live_* test
must never run on its marker alone, only with its CHATGPT_LIVE value too.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ["pytester"]

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# marker name -> the CHATGPT_LIVE value that opts it in (TESTING.md tier table)
LIVE_MARKERS = {
    "live_read": "read",
    "live_write": "write",
    "live_browser": "browser",
    "live_send": "send",
}


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("tier T0 test tried to open a socket; see TESTING.md")


def is_live_marked(item: Any) -> bool:
    """True if ``item`` carries any of the four live markers."""
    return any(item.get_closest_marker(m) for m in LIVE_MARKERS)


def missing_live_var(item: Any, environ: Any = os.environ) -> str | None:
    """The CHATGPT_LIVE value ``item`` still needs, or None if it may run."""
    for marker, variable in LIVE_MARKERS.items():
        if item.get_closest_marker(marker) and environ.get("CHATGPT_LIVE") != variable:
            return variable
    return None


@pytest.fixture(autouse=True)
def _no_network_in_t0(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test with none of the four live markers may not open a socket."""
    if is_live_marked(request.node):
        return
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-sandbox-chats",
        action="store_true",
        help="skip the live sandbox sweep after write/browser/send, print what stayed",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """A live_X item needs CHATGPT_LIVE=X; the marker alone must not run it."""
    for item in items:
        variable = missing_live_var(item)
        if variable:
            item.add_marker(pytest.mark.skip(reason=f"needs CHATGPT_LIVE={variable}"))
