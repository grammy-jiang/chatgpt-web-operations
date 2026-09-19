"""Live browser dry run for the composer's model/effort label (T3,
TESTING.md section 1, and its "browser dry run" row in section 2).

Opens scripts/chatgpt_client.py:BrowserSender on the sandbox project with
an explicit (effort, model) -- the same cookie-pinning path item B1 in
ROADMAP.md exercises for a real send -- and reads the composer's
model-control button. THE UI FACT, read from the live sandbox project page
2026-09-20: a <button aria-haspopup="menu"> with no aria-label and no
data-testid, a random radix id, whose visible text is the current preset's
title, one of the Power slider's five fixed positions.

The label must match whatever scripts/model_settings.py:resolve_preset
predicts for that (model, effort) pair. The model slug comes from the live
models payload's "Medium" preset, never a literal such as
"gpt-5-6-thinking", so a model rename does not break this test.

Two cases, one window at a time (the browser budget allows one; each test
opens and closes its own BrowserSender before the next case runs):

    effort "standard" on the thinking model -> today this is "Medium"
    effort "max"      on the thinking model -> today this is "Extra High"

Resolving to the Pro preset skips instead of opening a window: ROADMAP.md
rule 4 says nothing that spends the user's limits is automated, and Pro is
an opt-in lane a test must never switch the account into. Nothing is
filled and send is never clicked; the only navigation is to the project's
own composer page, the same URL a real send would use.

Marker live_browser, skipped unless CHATGPT_LIVE=browser (tests/conftest.py).
chatgpt_client is imported inside the test, never at module level, so
collection never needs Playwright.

Not run by this agent (see the skill's HARD RULES); collect-only proves it
is wired up without opening a browser:

    .venv/bin/python -m pytest tests/live/test_browser_model_label.py \
        --collect-only -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import model_settings  # noqa: E402

pytestmark = pytest.mark.live_browser

# THE UI FACT: the composer's model control has no aria-label and no
# data-testid; its accessible name is the current preset's title, always
# one of the Power slider's five fixed positions.
MODEL_BUTTON_NAME = re.compile(r"^(Instant|Medium|High|Extra High|Pro)$")


def label_matches(label: str, preset_title: str) -> bool:
    """True if the composer's button text names the same preset.

    Both sides are stripped and compared case-insensitively: Playwright's
    ``inner_text`` can carry incidental whitespace, and a case difference
    would be a second, unrelated way for this test to fail. Covered offline
    by tests/test_model_label_helper.py, which imports this function
    directly rather than re-implementing it.
    """
    return label.strip().casefold() == preset_title.strip().casefold()


def _presets(live_session: Any) -> list[dict[str, Any]]:
    """The Power slider's positions for version 'latest', read fresh."""
    status, body = live_session.call(model_settings.MODELS)
    assert status == 200, f"GET {model_settings.MODELS}: HTTP {status}"
    return model_settings.presets_of(body if isinstance(body, dict) else {})


def _thinking_model(presets: list[dict[str, Any]]) -> str:
    """The model slug behind the "Medium" preset -- the thinking model.

    Read from the payload rather than a literal slug, so a model rename
    does not break this test: "Medium" is one of the five fixed slider
    titles THE UI FACT names, whatever model it currently resolves to.
    """
    medium = next((p for p in presets if p["title"] == "Medium"), None)
    if not medium or not medium.get("model"):
        pytest.skip("models payload has no 'Medium' preset with a model slug")
    return str(medium["model"])


def _label_for(
    sandbox_id: str, presets: list[dict[str, Any]], model: str, effort: str
) -> tuple[str, str]:
    """Open one window pinned to (model, effort); read and return its label.

    Returns ``(label, expected_title)``, where ``expected_title`` is
    whatever ``model_settings.resolve_preset`` predicts for this pair.
    Skips before any window opens when the payload has no such preset, or
    when the pair would resolve to "Pro" (ROADMAP.md rule 4).

    Opens exactly as scripts/measure_window.py does: BrowserSender as a
    context manager, then ``sender._owner.submit(fn).result()`` to reach
    the page from its own owner thread -- Playwright objects belong to the
    thread that created them. The window closes at the end of the ``with``
    block, before this function returns, so the next case's window never
    overlaps this one. Imports chatgpt_client here, never at module import
    time (the skill's HARD RULES), so collection never needs Playwright.
    """
    preset = model_settings.resolve_preset(presets, model, effort)
    if preset is None:
        pytest.skip(
            f"models payload has no preset for model={model!r} effort={effort!r}"
        )
    if preset["title"] == "Pro":
        pytest.skip("resolved to the Pro preset; never opened here (ROADMAP.md rule 4)")

    import chatgpt_client as cc

    with cc.BrowserSender(
        "chrome", project=sandbox_id, effort=effort, model=model, visible=False
    ) as sender:

        def read_label() -> str:
            page = sender.page
            page.goto(
                sender.new_chat_url(),
                wait_until="domcontentloaded",
                timeout=cc.PAGE_LOAD_MS,
            )
            sender._composer()  # waits for the composer, so the button has painted
            button = page.get_by_role("button", name=MODEL_BUTTON_NAME)
            return button.first.inner_text(timeout=60_000)

        label = sender._owner.submit(read_label).result()
    return label, str(preset["title"])


def test_standard_effort_on_the_thinking_model_matches_its_preset(
    live_session: Any, sandbox_id: str
) -> None:
    """Case 1: effort "standard" on the thinking model -- today "Medium"."""
    presets = _presets(live_session)
    model = _thinking_model(presets)
    label, expected = _label_for(sandbox_id, presets, model, "standard")
    assert label_matches(label, expected), (
        f"composer showed {label!r}, expected the {expected!r} preset"
    )


def test_max_effort_on_the_thinking_model_matches_its_preset(
    live_session: Any, sandbox_id: str
) -> None:
    """Case 2: effort "max" on the thinking model -- today "Extra High"."""
    presets = _presets(live_session)
    model = _thinking_model(presets)
    label, expected = _label_for(sandbox_id, presets, model, "max")
    assert label_matches(label, expected), (
        f"composer showed {label!r}, expected the {expected!r} preset"
    )
