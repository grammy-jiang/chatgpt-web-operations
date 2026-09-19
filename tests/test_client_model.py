"""Tests for ``with_model`` and the parts of ``BrowserSender`` that use it.

``with_model`` pins the composer's default model the same way ``with_effort``
pins reasoning effort (``tests/test_client_core.py``, "EFFORTS /
EFFORT_COOKIE / with_effort"): both rewrite the ``oai-last-model-config``
cookie, and each leaves the other's field alone. This file also covers the
``model`` keyword ``BrowserSender.__init__`` gained alongside ``effort``,
and the guard on ``__exit__`` that stops a failure to close an already-dead
context (one the lifetime watchdog already killed) from masking whatever
exception the with-block body raised, or inventing one where the body
raised nothing.

``fake_playwright.install()`` puts a fake ``playwright`` / ``playwright.sync_api``
module pair in ``sys.modules`` for the ``BrowserSender`` tests, and
``enter_env`` fakes everything else ``_open`` touches -- exactly as
``tests/test_client_browser.py`` does. Both are duplicated here rather than
shared, since nothing outside this file may be edited to add a fixture.

Every test names the failure it defends against, matching ``test_commands.py``.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

# ---------------------------------------------------------------------------
# with_model -- pinning the model half of the oai-last-model-config cookie
# ---------------------------------------------------------------------------


def _decoded_model_cookie(cookies: list[dict]) -> dict:
    (cookie,) = [c for c in cookies if c["name"] == cc.EFFORT_COOKIE]
    return json.loads(unquote(cookie["value"]))


def test_a_blank_model_returns_the_same_list_unchanged() -> None:
    """Inheriting the profile's own model must not rewrite anything."""
    cookies = [{"name": "a", "value": "1"}]
    assert cc.with_model(cookies, "") is cookies


def test_a_missing_model_cookie_is_added() -> None:
    out = cc.with_model([{"name": "other", "value": "x"}], "gpt-6-pro")
    assert _decoded_model_cookie(out) == {"model": "gpt-6-pro"}
    assert {"name": "other", "value": "x"} in out


def test_an_existing_model_cookie_is_replaced_and_keeps_the_effort() -> None:
    existing = {
        "name": cc.EFFORT_COOKIE,
        "value": quote(json.dumps({"model": "gpt-5-6-thinking", "effort": "standard"})),
    }
    out = cc.with_model([existing, {"name": "other", "value": "x"}], "gpt-6-pro")
    assert len([c for c in out if c["name"] == cc.EFFORT_COOKIE]) == 1
    assert _decoded_model_cookie(out) == {"model": "gpt-6-pro", "effort": "standard"}


def test_other_cookies_are_left_untouched() -> None:
    other = {"name": "__Secure-next-auth.session-token.0", "value": "sess"}
    out = cc.with_model([other], "gpt-6-pro")
    assert other in out


def test_a_malformed_existing_cookie_value_is_tolerated_not_raised() -> None:
    """A cookie the browser has not written yet must not break every send."""
    broken = {"name": cc.EFFORT_COOKIE, "value": "not%20valid%20json"}
    out = cc.with_model([broken], "gpt-6-pro")
    assert _decoded_model_cookie(out) == {"model": "gpt-6-pro"}


def test_the_new_cookie_carries_the_documented_attributes() -> None:
    out = cc.with_model([], "gpt-6-pro")
    (cookie,) = out
    assert cookie["domain"] == "chatgpt.com"
    assert cookie["path"] == "/"
    assert cookie["secure"] is True
    assert cookie["httpOnly"] is False
    assert cookie["sameSite"] == "Lax"


def test_with_model_after_with_effort_keeps_both_fields() -> None:
    """The exact composition ``_open`` uses: with_model wraps with_effort."""
    cookies = cc.with_model(cc.with_effort([], "max"), "gpt-6-pro")
    assert _decoded_model_cookie(cookies) == {"model": "gpt-6-pro", "effort": "max"}


def test_with_effort_after_with_model_keeps_both_fields_the_other_order() -> None:
    cookies = cc.with_effort(cc.with_model([], "gpt-6-pro"), "max")
    assert _decoded_model_cookie(cookies) == {"model": "gpt-6-pro", "effort": "max"}


# ---------------------------------------------------------------------------
# fixtures duplicated from test_client_browser.py (see its own module
# docstring for the rationale each piece defends against)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_pw(monkeypatch):
    """Every test gets a fake playwright.sync_api; nothing real ever starts."""
    return fp.install(monkeypatch)


@pytest.fixture
def enter_env(monkeypatch, tmp_path):
    """Everything BrowserSender.__enter__/_open touch besides Playwright."""
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


# ---------------------------------------------------------------------------
# BrowserSender(model=...) -- the model reaches the exported cookie jar
# ---------------------------------------------------------------------------


def test_the_model_argument_is_stripped_like_effort_and_project() -> None:
    sender = cc.BrowserSender(model="  gpt-6-pro  ")
    try:
        assert sender.model == "gpt-6-pro"
    finally:
        sender._owner.shutdown(wait=True)


def test_open_applies_the_pinned_model_to_the_exported_cookie_jar(
    enter_env, fake_pw
) -> None:
    """--model must reach the browser's cookie jar via with_model, the same
    way --effort does (test_client_browser.py,
    test_open_applies_the_pinned_effort_to_the_exported_cookie_jar). Whether
    the composer honours ``gpt-6-pro`` from this cookie is untested
    (ROADMAP.md, B1); this only proves the cookie carries it."""
    enter_env.cookies.canned = [
        _session_cookie(),
        {
            "name": cc.EFFORT_COOKIE,
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
    sender = cc.BrowserSender(model="gpt-6-pro")
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert ctx.cookies == cc.with_model(enter_env.cookies.canned, "gpt-6-pro")
        assert _decoded_model_cookie(ctx.cookies) == {
            "model": "gpt-6-pro",
            "effort": "standard",
        }
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_applies_both_effort_and_model_together(enter_env, fake_pw) -> None:
    """A send can pin both at once; each setter must only touch its own
    field, whichever order _open composes them in."""
    sender = cc.BrowserSender(effort="max", model="gpt-6-pro")
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert _decoded_model_cookie(ctx.cookies) == {
            "model": "gpt-6-pro",
            "effort": "max",
        }
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_leaves_the_cookie_jar_unchanged_without_a_pinned_model(
    enter_env, fake_pw
) -> None:
    """Blank model means "inherit the profile's own"; with_model must be a
    no-op so the exported jar reaches the browser untouched."""
    sender = cc.BrowserSender()  # model=""
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert ctx.cookies == enter_env.cookies.canned
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# __exit__ -- closing an already-dead context must not mask the outcome
# ---------------------------------------------------------------------------


def test_exit_does_not_mask_a_body_exception_when_closing_the_stack_fails(
    enter_env, fake_pw
) -> None:
    """A window the watchdog already killed can raise while __exit__ closes
    it; that failure must not replace the exception the with-block body
    raised (finding from the previous test agent, chatgpt_client.py, the
    BrowserSender.__exit__ guard)."""
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
