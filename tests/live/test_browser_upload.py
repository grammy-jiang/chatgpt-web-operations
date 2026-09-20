"""Live browser dry run for an attachment (T3, TESTING.md section 1).

Marker live_browser, skipped unless CHATGPT_LIVE=browser (tests/conftest.py).
Opens exactly one Chrome window on the sandbox project, the same way
scripts/measure_window.py does (``BrowserSender`` as a context manager,
every Playwright call made through ``sender._owner.submit(fn).result()``,
since Playwright's sync objects may only be used from the thread that
created them -- chatgpt_client.py's ``BrowserSender`` docstring on
``_owner``). It uploads one small file through the composer's real upload
path (``BrowserSender._upload_files``) and checks the composer's own DOM
for the result: TESTING.md's T3 row is "the send path opens: window,
cookies, composer found; never sends", and an attachment is part of what
"opens" now covers (ROADMAP.md Stage 3 item 3 / B4). The prompt is never
filled and send is never clicked -- ``verify`` below only ever looks, it
never acts -- so this never creates a conversation in the sandbox and
leaves nothing for tests/live/conftest.py's sweep to find.

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

# Scoped to the composer's own <form> when one is found (BrowserSender's
# SEND_BUTTONS selectors show the send button lives inside one), so a
# same-named button elsewhere on the page could never produce a false pass.
_REMOVE_CHIP_AND_SEND_JS = """
() => {
    const composer = document.querySelector('#prompt-textarea');
    const form = composer ? composer.closest('form') : null;
    const scope = form || document;
    const removeButton = Array.from(
        scope.querySelectorAll('button[aria-label]')
    ).find((button) =>
        (button.getAttribute('aria-label') || '').startsWith('Remove file')
    );
    const sendButton =
        scope.querySelector('button[data-testid="send-button"]') ||
        scope.querySelector('button[aria-label="Send prompt"]');
    return {
        remove_label: removeButton ? removeButton.getAttribute('aria-label') : null,
        send_exists: Boolean(sendButton),
        send_enabled: Boolean(sendButton) && !sendButton.disabled,
    };
}
"""


def verify(info: dict[str, Any], filename: str) -> list[str]:
    """Mismatches between one ``_REMOVE_CHIP_AND_SEND_JS`` result and what a
    completed, unsent upload must look like.

    Pure -- no page, no browser -- so it is unit-tested in
    tests/test_harness.py by fabricating ``info`` in the shape
    ``_REMOVE_CHIP_AND_SEND_JS`` returns, the same ``mismatches == []``
    pattern tests/live/test_send_chat_flags.py and
    tests/live/test_write_project_settings.py use for their own round trips.

    Measured 2026-09-20: once ``_upload_files`` returns, the chip "Remove
    file 1: <name>" is present and the send button is enabled.
    """
    mismatches: list[str] = []
    label = info.get("remove_label")
    if not label or not str(label).startswith("Remove file"):
        mismatches.append(
            f"no button aria-label starting with 'Remove file'; got {label!r}"
        )
    elif filename not in str(label):
        mismatches.append(
            f"remove-file aria-label {label!r} does not name {filename!r}"
        )
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
    """Attach one file, never fill the prompt, never click send.

    ``sender._owner.submit(work).result()`` is the same pattern
    scripts/measure_window.py uses for every Playwright call once the
    window is open: one owner thread made the browser, so it is the only
    thread allowed to touch it.
    """
    import chatgpt_client as cc

    upload_path = tmp_path / "rp-test-browser-upload.txt"
    upload_path.write_text("rp-test browser upload probe\n", encoding="utf-8")

    with cc.BrowserSender("chrome", project=sandbox_id, visible=False) as sender:

        def work() -> dict[str, Any]:
            page = sender.page
            page.goto(
                sender.new_chat_url(),
                wait_until="domcontentloaded",
                timeout=cc.PAGE_LOAD_MS,
            )
            composer = sender._composer()
            sender._focus_composer(composer)
            sender._upload_files(page, [str(upload_path)])
            return page.evaluate(_REMOVE_CHIP_AND_SEND_JS)

        info = sender._owner.submit(work).result()

    mismatches = verify(info, upload_path.name)
    assert mismatches == [], mismatches
