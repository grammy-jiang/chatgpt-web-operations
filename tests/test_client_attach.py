"""Tests for real file attachments on a send (ROADMAP.md, Stage 3 item 3 /
B4, the offline part): ``BrowserSender(attachments=...)``, the shared
``_upload_files`` it factors out of ``_attach_prompt``, and the attachment
upload step ``_send`` runs before the prompt is filled.

Nothing here touches a real browser, Xvfb, the GNOME keyring, the real
Chrome profile, or the network, the same as ``tests/test_client_browser.py``
and ``tests/test_client_rewrite.py``, whose fixtures this file mirrors
rather than imports, matching how those two files already duplicate them
for each other. ``_attach_prompt``'s own four tests already exercise
``_upload_files``'s primary-selector, fallback-selector and polling
branches by construction, since ``_attach_prompt`` is now built on it; this
file covers what is new: multiple paths in one call, the call sites inside
``_send``, construction-time validation, and the recorded doc.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REAL_URL = f"https://chatgpt.com/c/{REAL_ID}"

USER_TURN_SELECTOR = '[data-message-author-role="user"]'
UPLOADING_SELECTOR = (
    '[aria-label*="Uploading" i], [aria-busy="true"], [role="progressbar"]'
)
UPLOAD_INPUT = "input#upload-files"


# ---------------------------------------------------------------------------
# fixtures and small shared helpers, mirroring test_client_browser.py /
# test_client_rewrite.py rather than importing from either
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_pw(monkeypatch):
    """Every test gets a fake playwright.sync_api; nothing real ever starts."""
    return fp.install(monkeypatch)


@pytest.fixture
def make_sender():
    """BrowserSender instances, with their owner thread pool always freed."""
    created: list[cc.BrowserSender] = []

    def _make(**kw):
        sender = cc.BrowserSender(**kw)
        created.append(sender)
        return sender

    yield _make
    for sender in created:
        sender._owner.shutdown(wait=True)


@pytest.fixture
def enter_env(monkeypatch, tmp_path):
    """Everything BrowserSender.__enter__/_open touch besides Playwright,
    for the one test here that records a send body through a real _open()
    (moved here the same way tests/test_client_rewrite.py's enter_env is)."""
    monkeypatch.setattr(cc, "virtual_display", _noop_cm)
    monkeypatch.setattr(cc, "browser_slot", _noop_cm)
    monkeypatch.setattr(cc, "new_chat_lock", _noop_cm)
    monkeypatch.setattr(cc, "available_mb", lambda: 9999.0)
    monkeypatch.setattr(cc, "PROFILE_DIR", tmp_path / "profile")

    fake_cs = types.SimpleNamespace(pick_browser=lambda choice: "chrome")
    monkeypatch.setattr(cc, "_helpers", lambda: fake_cs)

    cookies_mod = types.ModuleType("chatgpt_cookies")
    cookies_mod._rp_tolerant = True
    cookies_mod.canned = [_session_cookie()]
    cookies_mod.export = lambda browser: cookies_mod.canned
    monkeypatch.setitem(sys.modules, "chatgpt_cookies", cookies_mod)

    return types.SimpleNamespace(cs=fake_cs, cookies=cookies_mod)


def _noop_cm(*a, **kw):
    return _NoopCM()


class _NoopCM:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _session_cookie(value: str = "tok") -> dict:
    return {
        "name": "__Secure-next-auth.session-token.0",
        "value": value,
        "domain": "chatgpt.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }


def _calls(page: fp.Page, method: str, selector: str | None = None) -> list[tuple]:
    return [
        c
        for c in page.calls
        if c[0] == "locator"
        and c[2] == method
        and (selector is None or c[1] == selector)
    ]


def _page_calls(page: fp.Page, method: str) -> list[tuple]:
    return [c for c in page.calls if c[0] == "page" and c[1] == method]


def _call_order(page: fp.Page) -> list[tuple]:
    """(kind, selector_or_name, method) for every call, in order -- so a
    test can prove one call happened before another with .index()."""
    return [(c[0], c[1], c[2]) for c in page.calls]


def _upload_chip_key(name: str) -> str:
    """The get_by_text key _attach_prompt's chip wait looks for, matching
    tests/test_client_browser.py's helper of the same name."""
    stem = f"rp-prompt-{name}"[:24]
    return fp.key_for_text(re.compile(re.escape(stem), re.I))


def _wire_click_send_success(page: fp.Page) -> None:
    page.set_locator(
        cc.BrowserSender.SEND_BUTTONS[0], count=1, visible=True, enabled=True
    )


def _wire_post_confirmed(page: fp.Page) -> None:
    page.set_locator(USER_TURN_SELECTOR, count=fp.sequence(0, 1))


# ---------------------------------------------------------------------------
# __init__ -- a missing attachment raises before any browser opens
# ---------------------------------------------------------------------------


def test_a_missing_attachment_raises_filenotfounderror_at_construction(
    tmp_path,
) -> None:
    missing = tmp_path / "no-such-paper.pdf"

    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        cc.BrowserSender(attachments=[str(missing)])


def test_a_directory_is_not_a_valid_attachment(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        cc.BrowserSender(attachments=[str(tmp_path)])


def test_attachments_are_resolved_to_absolute_paths_and_kept_in_order(
    tmp_path, make_sender
) -> None:
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    sender = make_sender(attachments=[str(first), str(second)])

    assert sender.attachments == [str(first.resolve()), str(second.resolve())]


def test_no_attachments_is_the_default_and_stays_an_empty_list(make_sender) -> None:
    assert make_sender().attachments == []


# ---------------------------------------------------------------------------
# _upload_files -- the mechanism _attach_prompt already used, generalised
# ---------------------------------------------------------------------------


def test_upload_files_sends_multiple_paths_as_one_ordered_list_call(
    make_sender,
) -> None:
    """set_input_files replaces the input's files on every call, so more
    than one path must go through in a single call, in order, rather than
    one call per path (which would leave only the last one attached)."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)
    paths = ["/tmp/one.pdf", "/tmp/two.pdf", "/tmp/three.pdf"]

    sender._upload_files(page, paths)

    uploads = _calls(page, "set_input_files", UPLOAD_INPUT)
    assert len(uploads) == 1
    assert uploads[0][3] == (paths,)


def test_upload_files_sends_a_single_path_bare_like_attach_prompt_always_has(
    make_sender,
) -> None:
    """Byte-for-byte with the large-prompt case: _attach_prompt's own tests
    (tests/test_client_browser.py) assert the recorded call's argument is
    the bare path string, not a one-element list."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)

    sender._upload_files(page, ["/tmp/solo.pdf"])

    uploads = _calls(page, "set_input_files", UPLOAD_INPUT)
    assert uploads[0][3] == ("/tmp/solo.pdf",)


def test_upload_files_falls_back_to_the_generic_file_input(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    fallback = 'input[type="file"]:not([accept*="image"])'
    page.set_locator(UPLOAD_INPUT, count=0)
    page.set_locator(fallback, count=1)

    sender._upload_files(page, ["/tmp/solo.pdf"])

    assert _calls(page, "set_input_files", UPLOAD_INPUT) == []
    assert len(_calls(page, "set_input_files", fallback)) == 1


def test_upload_files_polls_until_the_uploading_indicator_clears(make_sender) -> None:
    """The same settle wait as test_client_browser.py's
    test_attach_prompt_polls_until_the_uploading_indicator_clears: two
    polls while "uploading" is still true, plus the final settle pause."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)
    page.set_locator(UPLOADING_SELECTOR, count=fp.sequence(1, 1, 0))

    sender._upload_files(page, ["/tmp/solo.pdf"])

    waits = _page_calls(page, "wait_for_timeout")
    assert len(waits) == 3


def test_upload_files_waits_the_quiet_window_when_no_busy_indicator_appears(
    make_sender,
) -> None:
    """Measured 2026-09-20: the busy indicator appears only ~2.5 s after the
    input is set. A wait that returned on the first "not busy" poll let a
    send go out mid-upload and the message was never posted. With no
    indicator at all, the whole quiet window is waited out: 30 polls of
    500 ms, then the settle pause."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)
    page.set_locator(UPLOADING_SELECTOR, count=0)

    sender._upload_files(page, ["/tmp/solo.pdf"])

    waits = _page_calls(page, "wait_for_timeout")
    assert len(waits) == sender.UPLOAD_QUIET_MS // 500 + 1


def test_upload_files_waits_for_a_late_busy_indicator_to_clear(make_sender) -> None:
    """Busy shows up on the third poll and clears on the fifth: two idle
    polls, two busy polls, then the break and the settle pause."""
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)
    page.set_locator(UPLOADING_SELECTOR, count=fp.sequence(0, 0, 1, 1, 0))

    sender._upload_files(page, ["/tmp/solo.pdf"])

    waits = _page_calls(page, "wait_for_timeout")
    assert len(waits) == 5


def test_upload_files_waits_for_the_input_to_be_attached_first(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    page.set_locator(UPLOAD_INPUT, count=1)

    sender._upload_files(page, ["/tmp/solo.pdf"])

    waits = _calls(page, "wait_for", UPLOAD_INPUT)
    assert waits and waits[0][4]["state"] == "attached"


# ---------------------------------------------------------------------------
# _send -- attachments upload through the same input, before the fill
# ---------------------------------------------------------------------------


def test_send_uploads_attachments_before_filling_the_composer(
    tmp_path, make_sender
) -> None:
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    sender = make_sender(attachments=[str(first), str(second)])
    page = fp.Page()
    sender.page = page
    page.set_locator(UPLOAD_INPUT, count=1)
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    uploads = _calls(page, "set_input_files", UPLOAD_INPUT)
    assert len(uploads) == 1
    assert uploads[0][3] == ([str(first.resolve()), str(second.resolve())],)

    order = _call_order(page)
    upload_index = order.index(("locator", UPLOAD_INPUT, "set_input_files"))
    fill_index = order.index(("locator", "#prompt-textarea", "fill"))
    assert upload_index < fill_index


def test_send_skips_the_upload_step_when_there_are_no_attachments(
    make_sender,
) -> None:
    sender = make_sender()  # attachments defaults to ()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)
    # UPLOAD_INPUT left unconfigured entirely: a locate would report count 0.

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert _calls(page, "set_input_files", UPLOAD_INPUT) == []
    assert _calls(page, "wait_for", UPLOAD_INPUT) == []


def test_send_uploads_attachments_before_attach_prompt_runs_for_a_large_prompt(
    tmp_path, make_sender
) -> None:
    """Above ATTACH_ABOVE_BYTES the prompt itself is uploaded through the
    same input (_attach_prompt); attachments must go through first, since
    set_input_files replaces rather than adds to the input's files."""
    paper = tmp_path / "paper.pdf"
    paper.write_text("paper", encoding="utf-8")
    sender = make_sender(attachments=[str(paper)])
    sender.ATTACH_ABOVE_BYTES = 10
    page = fp.Page()
    sender.page = page
    page.set_locator(UPLOAD_INPUT, count=1)
    page.set_locator(_upload_chip_key("task"), count=1, visible=True)
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    prompt_tmp = Path(tempfile.gettempdir()) / "rp-prompt-task.md"
    try:
        result = sender._send("x" * 1000, chat=None, name="task")

        assert result == REAL_ID
        uploads = _calls(page, "set_input_files", UPLOAD_INPUT)
        assert len(uploads) == 2
        assert uploads[0][3] == (str(paper.resolve()),)
        assert uploads[1][3] == (str(prompt_tmp),)
        cover_fill = _calls(page, "fill", "#prompt-textarea")
        assert cover_fill[0][3] == (cc.BrowserSender.ATTACH_COVER,)
    finally:
        prompt_tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# record_send_body -- every attachment path is in the recorded doc
# ---------------------------------------------------------------------------


def test_record_send_body_carries_attachments_when_any_are_set(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    paper = tmp_path / "paper.pdf"
    paper.write_text("paper", encoding="utf-8")
    sender = cc.BrowserSender(record_send_body=str(target), attachments=[str(paper)])
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation",
            method="POST",
            post_data='{"a": 1}',
        )

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["attachments"] == [str(paper.resolve())]
        # nothing else about the recorded shape changes when nothing is
        # pinned (test_client_rewrite.py covers that shape without
        # attachments; only the new key is this file's concern).
        assert doc["post_data"] == '{"a": 1}'
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_omits_attachments_when_there_are_none(
    enter_env, fake_pw, tmp_path
) -> None:
    """Backward compatibility, proven here too: no attachments must not
    widen the doc, matching test_client_rewrite.py's old-shape test."""
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation",
            method="POST",
            post_data='{"a": 1}',
        )

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert "attachments" not in doc
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)
