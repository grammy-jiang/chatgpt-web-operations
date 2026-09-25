"""Tests for chatgpt_client.BrowserSender: the only non-HTTP part of a send.

Nothing here touches a real browser, Xvfb, the GNOME keyring, the real
Chrome profile, or the network: ``fake_playwright.install()`` puts a fake
``playwright`` / ``playwright.sync_api`` module pair in ``sys.modules``
before any lazily-imported Playwright name is looked up, ``cc._helpers``
and the ``chatgpt_cookies`` module are replaced with small fakes so no
cookie ever comes from a real browser profile, and ``cc.virtual_display`` /
``cc.browser_slot`` / ``cc.PROFILE_DIR`` are replaced or redirected into
``tmp_path`` so no Xvfb process and no shared ``/tmp/rp-browser-profile``
file is ever touched. Every test names the failure it is defending
against, several of them straight out of ``references/failure-atlas.md``.
"""

from __future__ import annotations

import re
import signal
import sys
import tempfile
import threading
import types
from pathlib import Path
from urllib.parse import quote

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

# A fake conversation id long enough to satisfy chat_id()'s
# ``[0-9a-f-]{20,}`` pattern, so resolve/URL parsing behaves like a real one.
REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROVISIONAL_ID = f"WEB:{REAL_ID}"
REAL_URL = f"https://chatgpt.com/c/{REAL_ID}"
PROVISIONAL_URL = f"https://chatgpt.com/c/{PROVISIONAL_ID}"

DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"]'
USER_TURN_SELECTOR = '[data-message-author-role="user"]'
UPLOADING_SELECTOR = (
    '[aria-label*="Uploading" i], [aria-busy="true"], [role="progressbar"]'
)


# ---------------------------------------------------------------------------
# fixtures and small shared helpers
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
    """Everything BrowserSender.__enter__/_open touch besides Playwright:
    the display, the cross-process slot, the shared profile directory (kept
    inside tmp_path, never the real /tmp/rp-browser-profile), and the
    cookie-export chain (_helpers + a fake chatgpt_cookies module).
    """
    monkeypatch.setattr(cc, "virtual_display", _noop_cm)
    monkeypatch.setattr(cc, "browser_slot", _noop_cm)
    monkeypatch.setattr(cc, "new_chat_lock", _noop_cm)
    monkeypatch.setattr(cc, "available_mb", lambda: 9999.0)
    monkeypatch.setattr(cc, "PROFILE_DIR", tmp_path / "profile")

    fake_cs = types.SimpleNamespace(pick_browser=lambda choice: "chrome")
    monkeypatch.setattr(cc, "_helpers", lambda: fake_cs)

    cookies_mod = types.ModuleType("chatgpt_cookies")
    cookies_mod._rp_tolerant = True  # short-circuits _patch_cookie_export
    cookies_mod.canned = [_session_cookie()]
    cookies_mod.export = lambda browser: cookies_mod.canned
    monkeypatch.setitem(sys.modules, "chatgpt_cookies", cookies_mod)

    return types.SimpleNamespace(cs=fake_cs, cookies=cookies_mod)


def _noop_cm(*a, **kw):
    """A context manager that opens and closes nothing (import shape below)."""
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


def _raise_with(exc: BaseException):
    def _fn(*a, **kw):
        raise exc

    return _fn


class FakeEvent:
    """A controllable stand-in for threading.Event, for the watchdog tests.

    A real ``threading.Event.wait(5)`` really blocks for up to 5 s; the
    watchdog loop calls it every iteration, so a direct unit test of
    ``_watchdog`` must not use the real thing.
    """

    def __init__(self, closed: bool = False) -> None:
        self._closed = closed
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        return self._closed

    def set(self) -> None:
        self._closed = True

    def is_set(self) -> bool:
        return self._closed


def _wire_click_send_success(page: fp.Page, selector: str | None = None) -> None:
    page.set_locator(
        selector or cc.BrowserSender.SEND_BUTTONS[0],
        count=1,
        visible=True,
        enabled=True,
    )


def _wire_post_confirmed(page: fp.Page) -> None:
    """The user-turn count reads 0 once (the baseline) then 1 forever."""
    page.set_locator(USER_TURN_SELECTOR, count=fp.sequence(0, 1))


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


def _never_dismissed(page: fp.Page) -> bool:
    """True if nothing on the page was ever clicked or Escape-pressed."""
    no_click = not any(c[0] == "locator" and c[2] == "click" for c in page.calls)
    no_escape = not _page_calls(page, "keyboard.press")
    return no_click and no_escape


def _upload_chip_key(name: str) -> str:
    stem = f"rp-prompt-{name}"[:24]
    return fp.key_for_text(re.compile(re.escape(stem), re.I))


# ---------------------------------------------------------------------------
# __init__ -- string arguments are stripped (moved from the old
# tests/test_client_model.py, which covered model specifically)
# ---------------------------------------------------------------------------


def test_the_model_argument_is_stripped_like_effort_and_project() -> None:
    sender = cc.BrowserSender(
        project="  g-p-x  ", effort="  max  ", model="  gpt-6-pro  "
    )
    try:
        assert sender.project == "g-p-x"
        assert sender.effort == "max"
        assert sender.model == "gpt-6-pro"
    finally:
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# new_chat_url -- where composing lands, home page vs. a project
# ---------------------------------------------------------------------------


def test_new_chat_url_is_the_home_page_without_a_project(make_sender) -> None:
    assert make_sender().new_chat_url() == "https://chatgpt.com/"


def test_new_chat_url_points_at_the_project_page_when_one_is_set(make_sender) -> None:
    """Composing on a project's page is what keeps a run's chats out of the
    user's main list (SKILL.md, "Projects keep a run's chats out")."""
    sender = make_sender(project="g-p-abc123")
    assert sender.new_chat_url() == "https://chatgpt.com/g/g-p-abc123/project"


def test_new_chat_url_accepts_a_short_url_slug_too(make_sender) -> None:
    sender = make_sender(project="g-p-abc123-workers")
    assert sender.new_chat_url() == "https://chatgpt.com/g/g-p-abc123-workers/project"


# ---------------------------------------------------------------------------
# __enter__ -- the browser budget: refuse rather than open into swap
# ---------------------------------------------------------------------------


def test_enter_refuses_when_available_memory_is_below_the_budget(
    monkeypatch, make_sender
) -> None:
    """__enter__ must propagate browser_slot's MB-available refusal instead
    of opening a window the host cannot afford (SKILL.md: "Refusing to open
    is correct: a window opened into swap makes every Playwright call time
    out")."""
    monkeypatch.setattr(
        cc,
        "browser_slot",
        _raise_with(
            cc.TransportError(
                "only 500 MB available, 4000 MB needed to open a send "
                "window; waited 900 s"
            )
        ),
    )
    sender = make_sender()
    opened = []
    monkeypatch.setattr(sender, "_open", lambda: opened.append(True))

    with pytest.raises(cc.TransportError, match="MB available"):
        sender.__enter__()
    assert opened == []


def test_enter_refuses_when_no_browser_slot_is_free(monkeypatch, make_sender) -> None:
    """A second sender must never sneak a window past the cross-process cap
    (SKILL.md: RP_MAX_BROWSERS binds every process that opens one)."""
    monkeypatch.setattr(
        cc,
        "browser_slot",
        _raise_with(cc.TransportError("all 1 send window(s) are busy after 900 s")),
    )
    sender = make_sender()
    opened = []
    monkeypatch.setattr(sender, "_open", lambda: opened.append(True))

    with pytest.raises(cc.TransportError, match="busy"):
        sender.__enter__()
    assert opened == []


# ---------------------------------------------------------------------------
# _launch -- the trimmed flags, the shared profile, and its fallback
# ---------------------------------------------------------------------------


def test_launch_reuses_the_shared_profile_with_the_trimmed_flags(
    monkeypatch, tmp_path, fake_pw, make_sender
) -> None:
    """The measured-fastest launch flags (failure-atlas.md) must reach the
    real launch call unchanged, plus the persistent-profile disk cache."""
    profile = tmp_path / "profile"
    monkeypatch.setattr(cc, "PROFILE_DIR", profile)
    sender = make_sender()

    ctx = sender._launch(fake_pw)

    user_data_dir, kw = fake_pw.chromium.launch_persistent_context_calls[0]
    assert user_data_dir == str(profile)
    assert kw["channel"] == "chrome"
    assert kw["headless"] is False
    assert kw["args"] == [
        *cc.BrowserSender.LAUNCH_ARGS,
        f"--disk-cache-size={cc.DISK_CACHE_BYTES}",
    ]
    assert kw["viewport"] == {
        "width": cc.BrowserSender.WIDTH,
        "height": cc.BrowserSender.HEIGHT,
    }
    assert ctx is fake_pw.chromium.launch_persistent_context_result
    assert fake_pw.chromium.launch_calls == []  # the fallback never ran


def test_launch_clears_stale_singleton_lock_files_before_reuse(
    monkeypatch, tmp_path, fake_pw, make_sender
) -> None:
    """A window the watchdog killed leaves lock files behind that make
    Chrome refuse the profile next time; _launch must clear them first."""
    profile = tmp_path / "profile"
    profile.mkdir(parents=True)
    stale = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    for name in stale:
        (profile / name).write_text("stale")
    monkeypatch.setattr(cc, "PROFILE_DIR", profile)
    sender = make_sender()

    sender._launch(fake_pw)

    for name in stale:
        assert not (profile / name).exists()


def test_launch_falls_back_to_a_fresh_browser_when_the_profile_is_unusable(
    monkeypatch, tmp_path, fake_pw, make_sender
) -> None:
    """A busted shared profile must fall back to a throwaway browser rather
    than failing the send outright (the behaviour this replaces)."""
    monkeypatch.setattr(cc, "PROFILE_DIR", tmp_path / "profile")
    fake_pw.chromium.launch_persistent_context_raises = RuntimeError("locked")
    sender = make_sender()

    ctx = sender._launch(fake_pw)

    assert fake_pw.chromium.launch_calls == [
        {
            "channel": "chrome",
            "headless": False,
            "args": list(cc.BrowserSender.LAUNCH_ARGS),
        }
    ]
    browser = fake_pw.chromium.launch_result
    assert browser.calls == [
        (
            "browser",
            "new_context",
            (),
            {
                "viewport": {
                    "width": cc.BrowserSender.WIDTH,
                    "height": cc.BrowserSender.HEIGHT,
                }
            },
        )
    ]
    assert ctx is browser.new_context_result


def test_launch_uses_a_fresh_browser_when_no_profile_dir_is_configured(
    monkeypatch, fake_pw, make_sender
) -> None:
    monkeypatch.setattr(cc, "PROFILE_DIR", None)
    sender = make_sender()

    sender._launch(fake_pw)

    assert fake_pw.chromium.launch_persistent_context_calls == []
    assert len(fake_pw.chromium.launch_calls) == 1


# ---------------------------------------------------------------------------
# _open -- cookie export (unchanged), the new page, blocked routes
# ---------------------------------------------------------------------------


def test_open_exports_the_cookie_jar_unchanged_with_nothing_pinned(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender()  # effort="" model="" search=False
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert ctx.cookies == enter_env.cookies.canned
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_exports_the_cookie_jar_unchanged_even_with_everything_pinned(
    enter_env, fake_pw
) -> None:
    """Measured 2026-09-20: rewriting oai-last-model-config pinned neither
    the send nor the composer's own label (SKILL.md, "Reasoning effort").
    _open must hand the context exactly the jar chatgpt_cookies.export()
    returned, whatever effort/model/search are set to; pinning them is
    entirely the job of the send-body rewrite (test_client_rewrite.py)."""
    enter_env.cookies.canned = [
        _session_cookie(),
        {
            "name": "oai-last-model-config",
            "value": quote(
                '{"model": "gpt-5-6-thinking", "effort": "standard"}', safe=""
            ),
            "domain": "chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        },
    ]
    sender = cc.BrowserSender(effort="max", model="gpt-6-pro", search=True)
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert ctx.cookies == enter_env.cookies.canned
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_sets_the_page_from_the_launched_context(enter_env, fake_pw) -> None:
    sender = cc.BrowserSender()
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert sender.page is ctx.new_page_result
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_blocks_the_routes_a_scripted_send_never_reads(enter_env, fake_pw) -> None:
    sender = cc.BrowserSender()
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert [pattern for pattern, _ in ctx.routes] == [
            "**/backend-api/**",
            "**/ces/v1/**",
        ]
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_adds_the_helpers_directory_to_sys_path_if_missing(
    enter_env, fake_pw, monkeypatch
) -> None:
    """chatgpt_cookies is a sibling file, not a package; a worktree that
    imports chatgpt_client without scripts/ already on sys.path (this test
    suite always has it, from conftest.py and this file's own top) must
    still be able to import it."""
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(cc.HELPERS)])
    sender = cc.BrowserSender()
    try:
        sender._open()
        assert str(cc.HELPERS) in sys.path
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# block_unused -- the module helper only _open calls
# ---------------------------------------------------------------------------


def test_block_unused_aborts_requests_a_scripted_send_never_reads() -> None:
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False)

    for path in cc.UNUSED_ON_SEND:
        route = ctx.trigger_route(f"https://chatgpt.com{path}?x=1")
        assert route.aborted is True
        assert route.continued is False


def test_block_unused_lets_the_send_endpoint_through() -> None:
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False)

    route = ctx.trigger_route("https://chatgpt.com/backend-api/f/conversation")
    assert route.continued is True
    assert route.aborted is False


def test_block_unused_allows_the_project_sidebar_when_composing_in_a_project() -> None:
    """allow_project=True must let the gizmo reads through so the project's
    own page can render; without it the composer never appears."""
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=True)

    route = ctx.trigger_route(
        "https://chatgpt.com/backend-api/gizmos/snorlax/sidebar?owned_only=true"
    )
    assert route.continued is True
    assert route.aborted is False


def test_block_unused_still_blocks_gizmo_reads_outside_a_project() -> None:
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False)

    route = ctx.trigger_route(
        "https://chatgpt.com/backend-api/gizmos/snorlax/sidebar?owned_only=true"
    )
    assert route.aborted is True


# ---------------------------------------------------------------------------
# __exit__ -- always closes, even after an exception or a lifetime kill
# ---------------------------------------------------------------------------


def test_enter_and_exit_open_and_close_the_shared_profile_context(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender()
    entered = sender.__enter__()
    try:
        assert entered is sender
        assert sender.page is not None
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert not ctx.closed
    finally:
        sender.__exit__(None, None, None)
    assert ctx.closed
    assert sender.page is None


def test_exit_closes_the_context_even_when_the_with_block_raised(
    enter_env, fake_pw
) -> None:
    """A crash inside `with sender:` must not leak the browser context."""
    sender = cc.BrowserSender()

    def _crash_inside_the_with_block() -> None:
        with sender:
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _crash_inside_the_with_block()

    assert fake_pw.chromium.launch_persistent_context_result.closed
    assert sender.page is None


def test_exit_completes_its_teardown_after_a_lifetime_ceiling_kill(
    monkeypatch, make_sender
) -> None:
    """The watchdog's SIGKILL unblocks a hung Playwright call so the owner
    thread is free; __exit__'s own teardown must still run to completion
    regardless of whether that kill already happened."""
    sender = make_sender()
    ctx = fp.BrowserContext()
    sender._stack.callback(ctx.close)  # what _launch would have registered
    sender._own_pids = {4242}
    sender._closed = FakeEvent(closed=False)
    monkeypatch.setattr(cc, "scripted_browser_pids", lambda: {4242, 999})
    killed = []
    monkeypatch.setattr(cc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    sender._watchdog(deadline=-1.0)  # already past its ceiling: fires at once
    assert killed == [(4242, signal.SIGKILL)]

    sender.__exit__(None, None, None)
    assert ctx.closed
    assert sender.page is None
    assert sender._closed.is_set()


def test_exit_does_not_mask_a_body_exception_when_closing_the_stack_fails(
    enter_env, fake_pw
) -> None:
    """A window the watchdog already killed can raise while __exit__ closes
    it; that failure must not replace the exception the with-block body
    raised (chatgpt_client.py, the BrowserSender.__exit__ guard)."""
    sender = cc.BrowserSender()
    sender._stack.close = _raise_with(RuntimeError("already dead"))

    def _crash_inside_the_with_block() -> None:
        with sender:
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _crash_inside_the_with_block()


def test_exit_swallows_a_close_failure_when_the_body_did_not_raise(
    enter_env, fake_pw
) -> None:
    """The window can die on its own (killed by the watchdog) between the
    send finishing and __exit__ running; closing an already-dead context
    must not turn a successful send into a crash."""
    sender = cc.BrowserSender()
    sender._stack.close = _raise_with(RuntimeError("already dead"))

    with sender:
        pass

    assert sender.page is None
    assert sender._closed.is_set()


def test_exit_still_closes_cleanly_when_nothing_is_wrong(enter_env, fake_pw) -> None:
    """The guard must not swallow anything when there is nothing to swallow:
    a normal close still closes the launched context."""
    sender = cc.BrowserSender()
    with sender:
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert not ctx.closed
    assert ctx.closed
    assert sender.page is None


# ---------------------------------------------------------------------------
# _watchdog -- SIGKILL, and only processes carrying the Playwright marker
# ---------------------------------------------------------------------------


def test_watchdog_kills_only_pids_that_carry_the_playwright_profile_marker(
    monkeypatch, make_sender
) -> None:
    """Only processes carrying Playwright's temporary profile are ever
    killed, never the user's own Chrome (SKILL.md: "The browser is
    budgeted")."""
    sender = make_sender()
    sender._own_pids = {111, 222}
    sender._closed = FakeEvent(closed=False)
    # scripted_browser_pids() returns 222 (ours) and 333 (some other run's);
    # 111 is ours but not currently a scripted process, and 999 is nobody's.
    monkeypatch.setattr(cc, "scripted_browser_pids", lambda: {222, 333})
    monkeypatch.setattr(cc.time, "monotonic", lambda: 10_000.0)
    killed = []
    monkeypatch.setattr(cc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    sender._watchdog(deadline=0.0)  # already exceeded

    assert killed == [(222, signal.SIGKILL)]


def test_watchdog_does_not_kill_before_its_deadline(monkeypatch, make_sender) -> None:
    """A window still within its budget must never lose its memory to the
    watchdog while a send could still finish normally."""
    sender = make_sender()
    sender._own_pids = {111}
    monkeypatch.setattr(cc, "scripted_browser_pids", lambda: {111})
    killed = []
    monkeypatch.setattr(cc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    # wait() reports "closed" on the second call, before any deadline check
    # would ever fire, simulating a send that finished and called __exit__.
    event = FakeEvent(closed=False)
    calls = {"n": 0}

    def _wait(timeout=None):
        calls["n"] += 1
        return calls["n"] >= 2

    event.wait = _wait
    sender._closed = event
    monkeypatch.setattr(cc.time, "monotonic", lambda: 0.0)

    sender._watchdog(deadline=1_000_000.0)

    assert killed == []


def test_watchdog_does_nothing_once_the_window_already_closed(
    monkeypatch, make_sender
) -> None:
    sender = make_sender()
    sender._own_pids = {111}
    sender._closed = FakeEvent(closed=True)  # __exit__ already ran
    monkeypatch.setattr(cc, "scripted_browser_pids", lambda: {111})
    killed = []
    monkeypatch.setattr(cc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    sender._watchdog(deadline=-1.0)

    assert killed == []


# ---------------------------------------------------------------------------
# _check_rate_limit_dialog -- proactive dismissal, before a send is tried
# ---------------------------------------------------------------------------


def test_check_rate_limit_dialog_clicks_a_dismiss_button_when_one_is_visible(
    make_sender,
) -> None:
    """SKILL.md: "clear it and try the send" -- dismissing it leaves ChatGPT
    working, confirmed from the user's own browser."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        DIALOG_SELECTOR, count=1, texts=["Too many requests. Please try later."]
    )
    page.set_locator(fp.key_for_role("button", "Got it", True), count=1, visible=True)

    seen = sender._check_rate_limit_dialog()

    assert "too many requests" in seen.lower()
    assert _calls(page, "click", fp.key_for_role("button", "Got it", True))
    assert not _page_calls(page, "keyboard.press")


def test_check_rate_limit_dialog_presses_escape_when_no_button_matches(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(DIALOG_SELECTOR, count=1, texts=["Too many requests right now."])
    # No "Got it" / "OK" / "Okay" / "Close" button is configured as visible.

    seen = sender._check_rate_limit_dialog()

    assert seen
    presses = _page_calls(page, "keyboard.press")
    assert [p[2] for p in presses] == [("Escape",)]
    assert not any(c[0] == "locator" and c[2] == "click" for c in page.calls)


def test_check_rate_limit_dialog_skips_a_dialog_it_cannot_read(make_sender) -> None:
    """One dialog whose text cannot be read (a stale DOM node, mid-repaint)
    must not stop the scan from finding the real notice behind it -- the
    same "one bad one must not hide the good ones" principle as the cookie
    decryptor (failure-atlas.md)."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        DIALOG_SELECTOR,
        count=2,
        texts=["unused", "Too many requests. Please wait and try again."],
        raises=fp.sequence(fp.PlaywrightTimeoutError("stale node"), None),
    )

    seen = sender._check_rate_limit_dialog()

    assert "too many requests" in seen.lower()


def test_check_rate_limit_dialog_ignores_an_ordinary_dialog(make_sender) -> None:
    """This method only recognises the rate-limit notice by its own text;
    an unrelated dialog must be left for _dismiss_overlay instead."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(DIALOG_SELECTOR, count=1, texts=["A new version is available."])

    seen = sender._check_rate_limit_dialog()

    assert seen == ""
    assert _never_dismissed(page)


# ---------------------------------------------------------------------------
# _rate_limited_now -- read after a failed send, and never dismissed
# ---------------------------------------------------------------------------


def test_rate_limited_now_detects_the_modal_and_never_dismisses_it(
    make_sender,
) -> None:
    """failure-atlas.md, "Dismissing the rate-limit modal": the fix keeps
    this specific check read-only, so a 429 is reported, not retried faster
    by something that quietly clicked it away."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        cc.BrowserSender.RATE_LIMIT_MODAL,
        count=1,
        texts=["Too many requests. Access to conversation history is limited."],
    )

    seen = sender._rate_limited_now()

    assert "too many requests" in seen.lower()
    assert _never_dismissed(page)  # a read only: no click, no press


def test_rate_limited_now_is_empty_when_the_modal_is_absent(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page

    assert sender._rate_limited_now() == ""
    assert page.calls == []


# ---------------------------------------------------------------------------
# _dismiss_overlay -- Escape for ordinary modals, never a button click
# ---------------------------------------------------------------------------


def test_dismiss_overlay_presses_escape_only_for_an_ordinary_dialog(
    make_sender,
) -> None:
    """SKILL.md: "Other modals get Escape and only Escape: a dialog's
    buttons could accept terms or change a setting, and this runs
    unattended." """
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(DIALOG_SELECTOR, count=1, texts=["Your session will expire soon."])

    seen = sender._dismiss_overlay()

    assert "session will expire" in seen
    assert not any(c[0] == "locator" and c[2] == "click" for c in page.calls)
    presses = _page_calls(page, "keyboard.press")
    assert [p[2] for p in presses] == [("Escape",)]


def test_dismiss_overlay_clears_a_rate_limit_notice_via_its_own_button_first(
    make_sender,
) -> None:
    """_dismiss_overlay defers to _check_rate_limit_dialog for the one
    dialog that means "back off", instead of Escaping it like any other."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        DIALOG_SELECTOR, count=1, texts=["Too many requests. Please wait."]
    )
    page.set_locator(fp.key_for_role("button", "Got it", True), count=1, visible=True)

    sender._dismiss_overlay()

    assert _calls(page, "click", fp.key_for_role("button", "Got it", True))


# ---------------------------------------------------------------------------
# _focus_composer -- click, clear an overlay and retry, then force
# ---------------------------------------------------------------------------


def test_focus_composer_clicks_directly_when_nothing_covers_it(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    composer = page.locator(cc.COMPOSER_SELECTOR)

    sender._focus_composer(composer)

    clicks = _calls(page, "click", cc.COMPOSER_SELECTOR)
    assert len(clicks) == 1
    assert clicks[0][4]["force"] is False
    assert not _page_calls(page, "keyboard.press")


def test_focus_composer_clears_a_covering_overlay_and_retries(make_sender) -> None:
    """The composer had no overlay fallback before this; a modal turned a
    send into a 30 s timeout and a full retry cycle."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        raises=fp.sequence(fp.PlaywrightTimeoutError("covered"), None),
    )
    page.set_locator(DIALOG_SELECTOR, count=1, texts=["Update available"])
    composer = page.locator(cc.COMPOSER_SELECTOR)

    sender._focus_composer(composer)

    clicks = _calls(page, "click", cc.COMPOSER_SELECTOR)
    assert len(clicks) == 2
    assert [c[4]["force"] for c in clicks] == [False, False]
    presses = _page_calls(page, "keyboard.press")
    assert [p[2] for p in presses] == [("Escape",)]


def test_focus_composer_forces_the_click_after_the_retry_also_times_out(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        cc.COMPOSER_SELECTOR,
        raises=fp.sequence(
            fp.PlaywrightTimeoutError("covered"),
            fp.PlaywrightTimeoutError("still covered"),
            None,
        ),
    )
    composer = page.locator(cc.COMPOSER_SELECTOR)

    sender._focus_composer(composer)

    clicks = _calls(page, "click", cc.COMPOSER_SELECTOR)
    assert len(clicks) == 3
    assert clicks[-1][4]["force"] is True
    assert clicks[-1][4]["timeout"] == 30_000
    shots = _page_calls(page, "screenshot")
    assert len(shots) == 1
    assert "chatgpt-composer-blocked.png" in shots[0][3]["path"]


# ---------------------------------------------------------------------------
# _composer -- found by its selector, or a clear failure
# ---------------------------------------------------------------------------


def test_composer_returns_the_locator_once_it_is_visible(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page

    composer = sender._composer()

    assert composer.selector == cc.COMPOSER_SELECTOR
    waits = _calls(page, "wait_for", cc.COMPOSER_SELECTOR)
    assert waits and waits[0][4]["state"] == "visible"


def test_composer_screenshots_and_raises_when_it_never_appears(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.url = "https://chatgpt.com/"
    page.set_locator(cc.COMPOSER_SELECTOR, raises=fp.PlaywrightTimeoutError("gone"))

    with pytest.raises(cc.TransportError, match="composer did not appear"):
        sender._composer()

    shots = _page_calls(page, "screenshot")
    assert "chatgpt-composer-missing.png" in shots[0][3]["path"]


def test_probe_composer_runs_its_work_on_the_owner_thread(make_sender) -> None:
    """Playwright's sync objects belong to the thread that created them, the
    owner thread. A public method that ran its work on the caller's thread
    is what broke preflight --browser until 2026-09-20: "Cannot switch to a
    different thread", reported as a login problem."""
    sender = make_sender()
    seen: list[str] = []

    def record() -> str:
        seen.append(threading.current_thread().name)
        return "https://chatgpt.com/"

    sender._probe_composer = record
    assert sender.probe_composer() == "https://chatgpt.com/"
    assert seen and seen[0].startswith("chatgpt-send"), seen
    assert seen[0] != threading.current_thread().name


def test_probe_composer_loads_the_new_chat_page_then_waits_for_the_composer(
    make_sender,
) -> None:
    """The window opens on about:blank; a probe that never navigated waited
    60 s for a composer that could not exist (the second half of the same
    2026-09-20 defect). It must load new_chat_url() -- the project page when
    one is set -- and only then wait, returning where the composer was."""
    sender = make_sender(project="g-p-abc")
    page = fp.Page()
    sender.page = page
    page.url = "https://chatgpt.com/g/g-p-abc/project"

    assert sender._probe_composer() == "https://chatgpt.com/g/g-p-abc/project"

    gotos = _page_calls(page, "goto")
    assert gotos and gotos[0][2] == ("https://chatgpt.com/g/g-p-abc/project",)
    assert gotos[0][3]["wait_until"] == "domcontentloaded"
    waits = _calls(page, "wait_for", cc.COMPOSER_SELECTOR)
    assert waits and waits[0][4]["state"] == "visible"


@pytest.mark.parametrize(
    ("n_chars", "expected_ms"),
    [
        (0, 120_000),  # the floor: even an empty prompt gets two minutes
        (10_000, 120_000),  # 11 kB * 6 s = 66 s, still under the floor
        (50_000, 306_000),  # 51 kB * 6 s
        (172_000, 900_000),  # the round-2 review prompt hits the cap
        (10**6, 900_000),  # nothing exceeds it
    ],
)
def test_fill_budget_is_six_seconds_per_kb_between_its_floor_and_cap(
    n_chars: int, expected_ms: int
) -> None:
    assert cc.fill_budget_ms(n_chars) == expected_ms


def test_fill_composer_runs_its_work_on_the_owner_thread(make_sender) -> None:
    sender = make_sender()
    seen: list[str] = []

    def record(text: str) -> tuple[float, float]:
        seen.append(threading.current_thread().name)
        return (1.0, 2.0)

    sender._fill_composer = record
    assert sender.fill_composer("hello") == (1.0, 2.0)
    assert seen and seen[0].startswith("chatgpt-send"), seen


def test_fill_composer_loads_focuses_and_fills_under_the_send_budget(
    make_sender, monkeypatch
) -> None:
    """The measurement is honest only if it is the send's own path: the
    same navigation, the same focus, the same fill budget. Until 2026-09-20
    measure_window.py filled a composer it had never navigated to, with a
    flat 900 s budget of its own."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.url = "https://chatgpt.com/"
    focused: list[object] = []
    monkeypatch.setattr(
        sender, "_focus_composer", lambda composer: focused.append(composer)
    )
    text = "y" * 50_000

    ready, fill = sender._fill_composer(text)

    assert ready >= 0.0 and fill >= 0.0
    gotos = _page_calls(page, "goto")
    assert gotos and gotos[0][2] == ("https://chatgpt.com/",)
    assert len(focused) == 1
    fills = _calls(page, "fill", cc.COMPOSER_SELECTOR)
    assert fills and fills[0][3] == (text,)
    assert fills[0][4]["timeout"] == cc.fill_budget_ms(len(text)) == 306_000


def test_fill_composer_wraps_a_playwright_error_as_transport_error(
    make_sender,
) -> None:
    sender = make_sender()

    def boom(text: str) -> tuple[float, float]:
        raise ValueError("Target page, context or browser has been closed")

    sender._fill_composer = boom
    with pytest.raises(cc.TransportError, match="composer fill failed"):
        sender.fill_composer("hello")


def test_probe_composer_wraps_a_playwright_error_as_transport_error(
    make_sender,
) -> None:
    sender = make_sender()

    def boom() -> str:
        raise ValueError("Target page, context or browser has been closed")

    sender._probe_composer = boom
    with pytest.raises(cc.TransportError, match="composer probe failed"):
        sender.probe_composer()


def test_probe_composer_passes_a_transport_error_through_unchanged(
    make_sender,
) -> None:
    """The "logged out or challenged" message is what preflight keys its fix
    line on; wrapping it again would bury the words."""
    sender = make_sender()

    def missing() -> str:
        raise cc.TransportError(
            "composer did not appear at x (logged out or challenged)"
        )

    sender._probe_composer = missing
    with pytest.raises(cc.TransportError, match="logged out or challenged"):
        sender.probe_composer()


# ---------------------------------------------------------------------------
# _attach_prompt -- the large-prompt path: upload, name, and confirm it
# ---------------------------------------------------------------------------


def test_attach_prompt_uploads_through_the_primary_input_and_names_the_file(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    page.set_locator("input#upload-files", count=1)
    page.set_locator(_upload_chip_key("mytask"), count=1, visible=True)

    expected_path = Path(tempfile.gettempdir()) / "rp-prompt-mytask.md"
    try:
        sender._attach_prompt(page, "hello world", "mytask")

        assert expected_path.read_text(encoding="utf-8") == "hello world"
        uploads = _calls(page, "set_input_files", "input#upload-files")
        assert len(uploads) == 1
        assert uploads[0][3] == (str(expected_path),)
    finally:
        expected_path.unlink(missing_ok=True)


def test_attach_prompt_falls_back_to_the_generic_file_input(make_sender) -> None:
    """input#upload-files has moved before; the generic selector is the
    fallback that keeps an attachment working when it does again."""
    sender = make_sender()
    page = fp.Page()
    fallback = 'input[type="file"]:not([accept*="image"])'
    page.set_locator("input#upload-files", count=0)
    page.set_locator(fallback, count=1)
    page.set_locator(_upload_chip_key("t2"), count=1, visible=True)

    path = Path(tempfile.gettempdir()) / "rp-prompt-t2.md"
    try:
        sender._attach_prompt(page, "x", "t2")

        assert _calls(page, "set_input_files", "input#upload-files") == []
        assert len(_calls(page, "set_input_files", fallback)) == 1
    finally:
        path.unlink(missing_ok=True)


def test_attach_prompt_raises_when_the_chip_never_appears(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    page.set_locator("input#upload-files", count=1)
    page.set_locator(
        _upload_chip_key("t3"), raises=fp.PlaywrightTimeoutError("no chip")
    )

    path = Path(tempfile.gettempdir()) / "rp-prompt-t3.md"
    try:
        with pytest.raises(cc.TransportError, match="never appeared"):
            sender._attach_prompt(page, "x", "t3")
    finally:
        path.unlink(missing_ok=True)


def test_attach_prompt_polls_until_the_uploading_indicator_clears(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    page.set_locator("input#upload-files", count=1)
    page.set_locator(_upload_chip_key("t4"), count=1, visible=True)
    page.set_locator(UPLOADING_SELECTOR, count=fp.sequence(1, 1, 0))

    path = Path(tempfile.gettempdir()) / "rp-prompt-t4.md"
    try:
        sender._attach_prompt(page, "x", "t4")

        waits = _page_calls(page, "wait_for_timeout")
        # two polls while "uploading" was still true, plus the final settle
        assert len(waits) == 3
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _click_send -- politely, then forced, then the next selector
# ---------------------------------------------------------------------------


def test_click_send_clicks_the_first_visible_enabled_button(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    _wire_click_send_success(page)

    sent = sender._click_send(page, budget=120_000)

    assert sent is True
    clicks = _calls(page, "click", cc.BrowserSender.SEND_BUTTONS[0])
    assert len(clicks) == 1
    assert clicks[0][4] == {"timeout": 30_000, "force": False}


def test_click_send_skips_a_selector_that_does_not_exist(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    _wire_click_send_success(page, cc.BrowserSender.SEND_BUTTONS[1])

    sent = sender._click_send(page, budget=120_000)

    assert sent is True
    assert _calls(page, "click", cc.BrowserSender.SEND_BUTTONS[0]) == []
    assert len(_calls(page, "click", cc.BrowserSender.SEND_BUTTONS[1])) == 1


def test_click_send_reports_failure_when_no_button_ever_enables(make_sender) -> None:
    """references/failure-atlas.md, "Generic backoff on a 429": a send that
    cannot even find its button must fail cleanly, not hang for a timeout
    on a button that was never going to be there."""
    sender = make_sender()
    page = fp.Page()
    for selector in cc.BrowserSender.SEND_BUTTONS:
        page.set_locator(selector, count=1, visible=True, enabled=False)

    sent = sender._click_send(page, budget=120_000)

    assert sent is False
    assert not any(c[0] == "locator" and c[2] == "click" for c in page.calls)


def test_click_send_forces_the_click_when_the_polite_attempt_times_out(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    selector = cc.BrowserSender.SEND_BUTTONS[0]
    page.set_locator(
        selector,
        count=1,
        visible=True,
        enabled=True,
        raises=fp.sequence(fp.PlaywrightTimeoutError("busy"), None),
    )

    sent = sender._click_send(page, budget=120_000)

    assert sent is True
    clicks = _calls(page, "click", selector)
    assert [c[4]["force"] for c in clicks] == [False, True]
    assert clicks[1][4]["timeout"] == 120_000


def test_click_send_moves_to_the_next_selector_when_forcing_also_fails(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    first, second = cc.BrowserSender.SEND_BUTTONS[0], cc.BrowserSender.SEND_BUTTONS[1]
    page.set_locator(
        first,
        count=1,
        visible=True,
        enabled=True,
        raises=fp.PlaywrightTimeoutError("stuck"),
    )
    _wire_click_send_success(page, second)

    sent = sender._click_send(page, budget=120_000)

    assert sent is True
    assert len(_calls(page, "click", first)) == 2
    assert len(_calls(page, "click", second)) == 1


# ---------------------------------------------------------------------------
# send / _send -- the whole path: fill budget, confirm, chat= vs. new
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size_kb", "expected_timeout"),
    [
        (0, 120_000),  # below the floor
        (50, 306_000),  # 6000 * (50 // 1000 + 1)
        (200, 900_000),  # above the ceiling
    ],
)
def test_send_fills_the_composer_within_a_budget_scaled_to_its_size(
    make_sender, size_kb, expected_timeout
) -> None:
    """failure-atlas.md: the fill budget is 6 s/kB, capped at 600 s
    (900_000 ms here as measured against BROWSER_MAX_SECONDS); a fixed
    2.5 s/kB budget is what let a busy host blow through it twice."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)
    text = "x" * (size_kb * 1000)

    result = sender._send(text, chat=None, name="task")

    assert result == REAL_ID
    fills = _calls(page, "fill", cc.COMPOSER_SELECTOR)
    assert fills[0][4]["timeout"] == expected_timeout


def test_send_returns_the_real_conversation_id_once_confirmed(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert page.last_goto_url == "https://chatgpt.com/"


def test_send_into_an_existing_chat_navigates_to_its_url(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)

    result = sender._send("hello", chat=REAL_ID, name="task")

    assert result == REAL_ID
    assert page.last_goto_url == REAL_URL


def test_send_returns_the_provisional_id_when_the_real_one_never_resolves(
    make_sender,
) -> None:
    """send()'s docstring: composing returns "any thread"; a still-WEB: id
    is exactly what resolve_new_conversation is for, outside this class."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(PROVISIONAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == PROVISIONAL_ID


def test_send_raises_when_no_new_turn_or_url_ever_appears(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    # USER_TURN_SELECTOR left unconfigured: count() stays 0 forever.

    with pytest.raises(cc.TransportError, match="message was not posted"):
        sender._send("hello", chat=None, name="task")

    assert _page_calls(page, "screenshot")


def test_send_falls_back_to_enter_when_the_send_button_never_enables(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    # No SEND_BUTTONS selector is configured, so _click_send returns False.
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    presses = _page_calls(page, "keyboard.press")
    assert ("Enter",) in [p[2] for p in presses]


def test_send_attaches_large_prompts_instead_of_filling_them_directly(
    make_sender,
) -> None:
    """Above ATTACH_ABOVE_BYTES the prompt is uploaded, because minutes of
    ProseMirror re-rendering is what failed the largest review prompts."""
    sender = make_sender()
    sender.ATTACH_ABOVE_BYTES = 10
    page = fp.Page()
    sender.page = page
    page.set_locator("input#upload-files", count=1)
    page.set_locator(_upload_chip_key("task"), count=1, visible=True)
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    path = Path(tempfile.gettempdir()) / "rp-prompt-task.md"
    try:
        result = sender._send("x" * 1000, chat=None, name="task")

        assert result == REAL_ID
        assert _calls(page, "set_input_files", "input#upload-files")
        cover_fill = _calls(page, "fill", cc.COMPOSER_SELECTOR)
        assert cover_fill[0][3] == (cc.BrowserSender.ATTACH_COVER,)
    finally:
        path.unlink(missing_ok=True)


def test_send_toggles_the_chat_surface_when_unchecked(make_sender) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)
    toggle_key = fp.key_for_role("radio", re.compile(r"^Chat$", re.I), False)
    page.set_locator(
        toggle_key, count=1, visible=True, attributes={"aria-checked": "false"}
    )

    sender._send("hello", chat=None, name="task")

    assert len(_calls(page, "click", toggle_key)) == 1


def test_send_wraps_a_fill_timeout_as_a_rate_limit_when_the_modal_is_present(
    make_sender,
) -> None:
    """The public send() converts a failure into the 429 the dispatcher's
    ladder needs -- but only once the send has actually been tried
    (SKILL.md, "Rate limits": "Only a send that then fails while the
    notice is still on the page counts as a 429")."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    # Only fill() must fail here: wait_for() must still find the composer,
    # or the failure would be "composer did not appear", not a fill timeout.
    page.set_locator(
        cc.COMPOSER_SELECTOR, raises={"fill": fp.PlaywrightTimeoutError("timed out")}
    )
    page.set_locator(
        cc.BrowserSender.RATE_LIMIT_MODAL, count=1, texts=["Too many requests."]
    )

    with pytest.raises(cc.TransportError) as excinfo:
        sender.send("hello")

    assert excinfo.value.status == 429
    assert "rate-limiting" in str(excinfo.value)
    assert _page_calls(page, "screenshot")


def test_send_wraps_a_generic_failure_without_the_rate_limit_modal(
    make_sender,
) -> None:
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    page.set_locator(
        cc.COMPOSER_SELECTOR, raises={"fill": fp.PlaywrightTimeoutError("timed out")}
    )
    # RATE_LIMIT_MODAL left unconfigured: absent.

    with pytest.raises(cc.TransportError) as excinfo:
        sender.send("hello")

    assert excinfo.value.status == 0
    assert str(excinfo.value).startswith("browser send failed:")


def test_send_reraises_a_transport_error_from_the_send_path_unchanged(
    make_sender,
) -> None:
    """A TransportError _send already raised (e.g. "never posted") must
    pass straight through send(), not be re-wrapped into a different,
    less specific message."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    # USER_TURN_SELECTOR left unconfigured: never posts.

    with pytest.raises(cc.TransportError, match="^message was not posted") as excinfo:
        sender.send("hello")

    assert excinfo.value.status == 0


def test_send_runs_on_the_owner_thread_not_the_callers(make_sender) -> None:
    """Playwright's sync objects may only be used from the thread that
    created them; send() must hand _send to the owner executor rather than
    calling it inline on the caller's own thread."""
    sender = make_sender()
    page = fp.Page()
    sender.page = page
    _wire_click_send_success(page)
    _wire_post_confirmed(page)
    page.url = fp.sequence(REAL_URL)

    seen: list[threading.Thread] = []
    real_send = sender._send

    def _spy(*a, **kw):
        seen.append(threading.current_thread())
        return real_send(*a, **kw)

    sender._send = _spy  # instance attribute shadows the bound method

    result = sender.send("hello")

    assert result == REAL_ID
    assert seen[0] is not threading.current_thread()
    assert seen[0].name.startswith("chatgpt-send")
