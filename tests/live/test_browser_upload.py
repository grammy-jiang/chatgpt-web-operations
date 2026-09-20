"""Live browser dry run for an attachment (T3, TESTING.md section 1).

Marker live_browser, skipped unless CHATGPT_LIVE=browser (tests/conftest.py).
Opens exactly one Chrome window on the sandbox project and asks the sender,
through its public ``attach_files``, to do what a send with ``attachments``
does up to the point of sending: load the compose page, focus the composer,
upload one small file through the real upload path, wait for it to settle,
and report what the composer shows. TESTING.md's T3 row is "the send path
opens: window, cookies, composer found; never sends", and an attachment is
part of what "opens" now covers (ROADMAP.md Stage 3 item 3 / B4). The
prompt is never filled and send is never clicked -- ``verify`` below only
ever looks, it never acts -- so this never creates a conversation in the
sandbox and leaves nothing for tests/live/conftest.py's sweep to find.

Until 2026-09-20 this test drove the sender's private members itself
(``_composer``, ``_focus_composer``, ``_upload_files``, the owner thread)
with its own copy of the DOM-reading script. It worked, because it also
did its own navigation; the other two private-seam callers, preflight.py
and measure_window.py, had not and had never worked
(references/failure-atlas.md). A consistency test now refuses any code
outside chatgpt_client.py that reaches past the sender's public methods.

``chatgpt_client`` is imported inside the test function, not at module
import time, the same rule tests/live/conftest.py follows for the same
reason: importing it must never become something that happens merely by
collecting this file.

Not run by this agent (see the skill's HARD RULES); collect-only proves it
is wired up without touching the account:

    .venv/bin/python -m pytest tests/live/test_browser_upload.py \\
        --collect-only -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.live_browser


def verify(info: dict[str, Any], filename: str) -> list[str]:
    """Mismatches between one ``BrowserSender.attach_files`` report and what
    a completed, unsent upload must look like.

    Pure -- no page, no browser -- so it is unit-tested in
    tests/test_harness.py by fabricating ``info`` in the shape
    ``attach_files`` returns, the same ``mismatches == []`` pattern
    tests/live/test_send_chat_flags.py and
    tests/live/test_write_project_settings.py use for their own round trips.

    Measured 2026-09-20: once the upload has settled, the chip "Remove file
    1: <name>" is present and the send button is enabled.
    """
    mismatches: list[str] = []
    labels = [str(label) for label in info.get("remove_labels") or []]
    if not labels:
        mismatches.append("no button aria-label starting with 'Remove file'")
    elif not any(filename in label for label in labels):
        mismatches.append(f"no remove-file label names {filename!r}; got {labels!r}")
    if not info.get("send_exists"):
        mismatches.append(
            "no send button (neither [data-testid=send-button] nor "
            '[aria-label="Send prompt"])'
        )
    elif not info.get("send_enabled"):
        mismatches.append("send button is present but not enabled")
    return mismatches


def test_uploading_a_file_shows_its_remove_chip_and_leaves_send_enabled(
    sandbox_id: str, tmp_path: Path
) -> None:
    """Attach one file, never fill the prompt, never click send."""
    import chatgpt_client as cc

    upload_path = tmp_path / "rp-test-browser-upload.txt"
    upload_path.write_text("rp-test browser upload probe\n", encoding="utf-8")

    with cc.BrowserSender("chrome", project=sandbox_id, visible=False) as sender:
        info = sender.attach_files([str(upload_path)])

    assert info["url"].startswith("https://chatgpt.com/"), info["url"]
    mismatches = verify(info, upload_path.name)
    assert mismatches == [], mismatches
