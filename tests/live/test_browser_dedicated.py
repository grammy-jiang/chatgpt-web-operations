"""Tier T3: the dedicated browser window a send attaches to (SKILL.md,
"Sending through the dedicated browser window"). Read-only: ``status``
reads the window's debugging endpoint on 127.0.0.1, and preflight's host
check reads the same; nothing is sent and no tab is opened here (the T3
upload dry run and the T4 sends attach to the window themselves when it
answers). Marker live_browser, CHATGPT_LIVE=browser.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import dedicated_browser  # noqa: E402
import preflight  # noqa: E402

pytestmark = pytest.mark.live_browser


def test_dedicated_browser_status_answers_and_a_window_attaches(
    tier: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``dedicated_browser.py status`` exits 0 and names the browser when the
    window is up, and the mode the sends will take agrees with it: the host
    check says "ok" and ``cdp_endpoint`` resolves to the same endpoint. When
    the window is down both say so, and ``status`` exits 1: a test that
    cannot tell the two apart would prove nothing."""
    record = tmp_path / "status.json"
    code = dedicated_browser.main(["status", "--json", str(record)])
    status = json.loads(record.read_text(encoding="utf-8"))
    out = capsys.readouterr().out
    check = preflight.dedicated_browser_check(
        cc.CDP_MODE, cc.DEDICATED_URL, cc.dedicated_browser_version
    )
    if status["running"]:
        assert code == 0
        assert status["browser"].startswith("Chrome/"), status
        assert "running:" in out
        assert check["state"] == "ok", check
        assert cc.cdp_endpoint() == status["endpoint"] or cc.CDP_MODE == "off"
    else:
        assert code == 1
        assert "not running" in out
        assert check["state"] == "block", check
        assert cc.cdp_endpoint("auto") == ""
        pytest.fail(
            "the dedicated browser is not running: start it with "
            "dedicated_browser.py start (and log in once); a send would use "
            "the scripted window"
        )
