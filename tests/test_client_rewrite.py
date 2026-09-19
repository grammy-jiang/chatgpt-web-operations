"""Tests for Stage 3 items 1-2, the in-flight POST-body rewrite: the pure
``rewrite_send_body`` and the route ``BrowserSender._open`` registers to
apply it, offered to ``block_unused`` as its ``rewrite_send`` hook.

2026-09-20's two recorded sends (``send_prompt.py --effort standard
--record-send-body``) proved ``with_effort`` / ``with_model`` alone do not
pin what a send uses: the page takes ``thinking_effort`` and ``model`` from
the account's server-side ``last_used_model_config``, not from the
``oai-last-model-config`` cookie those two rewrite, so both recorded sends
carried ``"max"`` regardless of the cookie, and ``--search`` left
``system_hints: []`` untouched. Rewriting the body the page already built,
in flight, is what actually pins them.

``rewrite_send_body`` is plain data-in data-out and needs no Playwright.
``_rewrite_send_route`` is tested directly, by constructing
``fake_playwright.Request`` / ``Route`` objects (no context, no dispatch),
and through ``block_unused``'s own ``rewrite_send`` hook and, end to end,
through ``BrowserSender._open`` -- exactly how the other route/context
tests in ``tests/test_client_browser.py`` and ``tests/test_client_core.py``
already exercise ``block_unused`` itself: a bare context or a hand-built
route, never a real browser.

Why one route, not two: ``block_unused`` already registers on
``"**/backend-api/**"``, which covers the send endpoint too. Real Playwright
resolves two routes matching one URL in the order opposite to their
registration (the most recently registered runs first); this repository's
own test fake, ``tests/fake_playwright.py``, resolves the same case by
first registration instead (see its module docstring, "Routing"). A second,
separately registered route for the send endpoint could therefore be made
to fire in the fake or in real Playwright, never reliably both, so the
rewrite is instead one extra step inside ``block_unused``'s own route,
offered through the optional ``rewrite_send`` hook. That is what "the two
coexist" means here, and every test below proves it holds in both
directions: the usual blocked paths still abort, and everything else that
is not the send endpoint still just continues, unrewritten.

Every test names the failure or behaviour it defends, matching
``test_commands.py``.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import fake_playwright as fp  # noqa: E402

SEND_URL = "https://chatgpt.com/backend-api/f/conversation"
PREPARE_URL = "https://chatgpt.com/backend-api/f/conversation/prepare"


def _body(**extra: object) -> dict:
    """A send body shaped like SKILL.md's recorded example, minus the keys
    no test here reads."""
    base: dict = {
        "action": "next",
        "model": "gpt-5-6-thinking",
        "thinking_effort": "max",
        "system_hints": [],
        "parent_message_id": "p1",
        "timezone": "UTC",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# rewrite_send_body -- pure, no Playwright
# ---------------------------------------------------------------------------


def test_effort_alone_sets_thinking_effort_and_nothing_else() -> None:
    out = cc.rewrite_send_body(_body(), effort="standard", model="", search=False)
    assert out["thinking_effort"] == "standard"
    assert out["model"] == "gpt-5-6-thinking"
    assert out["system_hints"] == []


def test_model_alone_sets_model_and_nothing_else() -> None:
    out = cc.rewrite_send_body(_body(), effort="", model="gpt-6-pro", search=False)
    assert out["model"] == "gpt-6-pro"
    assert out["thinking_effort"] == "max"
    assert out["system_hints"] == []


def test_search_alone_appends_the_search_hint_without_dropping_others() -> None:
    out = cc.rewrite_send_body(
        _body(system_hints=["other"]), effort="", model="", search=True
    )
    assert out["system_hints"] == ["other", "search"]
    assert out["thinking_effort"] == "max"
    assert out["model"] == "gpt-5-6-thinking"


def test_search_does_not_duplicate_an_already_present_search_hint() -> None:
    out = cc.rewrite_send_body(
        _body(system_hints=["search"]), effort="", model="", search=True
    )
    assert out["system_hints"] == ["search"]


def test_a_none_system_hints_value_is_treated_as_empty() -> None:
    """The composer may itself set this key to ``None``, not just omit it."""
    out = cc.rewrite_send_body(
        _body(system_hints=None), effort="", model="", search=True
    )
    assert out["system_hints"] == ["search"]


def test_a_missing_system_hints_key_is_created_only_when_searching() -> None:
    body = _body()
    del body["system_hints"]
    out = cc.rewrite_send_body(body, effort="", model="", search=True)
    assert out["system_hints"] == ["search"]


def test_all_three_together() -> None:
    out = cc.rewrite_send_body(
        _body(), effort="extended", model="gpt-6-pro", search=True
    )
    assert out["thinking_effort"] == "extended"
    assert out["model"] == "gpt-6-pro"
    assert out["system_hints"] == ["search"]


def test_other_keys_are_left_untouched() -> None:
    out = cc.rewrite_send_body(_body(), effort="max", model="", search=False)
    assert out["parent_message_id"] == "p1"
    assert out["timezone"] == "UTC"
    assert out["action"] == "next"


def test_empty_settings_return_an_equal_body() -> None:
    """Nothing to rewrite: the exact same object comes back, not a copy."""
    body = _body()
    out = cc.rewrite_send_body(body, effort="", model="", search=False)
    assert out == body
    assert out is body


# ---------------------------------------------------------------------------
# BrowserSender._rewrite_send_route -- direct, over hand-built
# fake_playwright.Request/Route (no context, no dispatch)
# ---------------------------------------------------------------------------


def _route(url: str, method: str = "POST", post_data: str | None = None) -> fp.Route:
    return fp.Route(fp.Request(url, method, post_data))


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


def test_route_rewrites_a_post_to_the_send_endpoint(make_sender) -> None:
    sender = make_sender(effort="max", model="gpt-6-pro", search=True)
    route = _route(
        SEND_URL,
        post_data=json.dumps(
            {"thinking_effort": "standard", "model": "old", "system_hints": []}
        ),
    )

    handled = sender._rewrite_send_route(route)

    assert handled is True
    assert route.continued is True
    assert sender._last_sent_body is not None
    assert json.loads(sender._last_sent_body) == {
        "thinking_effort": "max",
        "model": "gpt-6-pro",
        "system_hints": ["search"],
    }


def test_route_leaves_the_prepare_sibling_alone(make_sender) -> None:
    """Exactly RECORD_ENDPOINT, never its /prepare handshake sibling."""
    sender = make_sender(effort="max")
    route = _route(PREPARE_URL, post_data=json.dumps({"thinking_effort": "standard"}))

    handled = sender._rewrite_send_route(route)

    assert handled is False
    assert route.continued is False  # not this function's job to finish it
    assert sender._last_sent_body is None


def test_route_leaves_a_get_to_the_send_endpoint_alone(make_sender) -> None:
    sender = make_sender(search=True)
    route = _route(SEND_URL, method="GET", post_data=None)

    assert sender._rewrite_send_route(route) is False
    assert sender._last_sent_body is None


def test_route_leaves_a_non_json_body_alone(make_sender) -> None:
    sender = make_sender(model="gpt-6-pro")
    route = _route(SEND_URL, post_data="not json")

    handled = sender._rewrite_send_route(route)

    assert handled is False
    assert route.continued is False
    assert sender._last_sent_body is None


def test_route_leaves_a_missing_body_alone(make_sender) -> None:
    sender = make_sender(model="gpt-6-pro")
    route = _route(SEND_URL, post_data=None)

    assert sender._rewrite_send_route(route) is False
    assert sender._last_sent_body is None


def test_route_leaves_a_json_array_body_alone(make_sender) -> None:
    """A JSON body that parses but is not an object is not rewritable."""
    sender = make_sender(effort="max")
    route = _route(SEND_URL, post_data=json.dumps([1, 2, 3]))

    assert sender._rewrite_send_route(route) is False
    assert sender._last_sent_body is None


def test_route_ignores_a_query_string_on_the_send_url(make_sender) -> None:
    sender = make_sender(effort="max")
    route = _route(
        SEND_URL + "?foo=bar", post_data=json.dumps({"thinking_effort": "standard"})
    )

    assert sender._rewrite_send_route(route) is True
    assert json.loads(sender._last_sent_body)["thinking_effort"] == "max"


# ---------------------------------------------------------------------------
# block_unused(..., rewrite_send=...) -- the two coexist on one route
# ---------------------------------------------------------------------------


def test_block_unused_uses_the_rewrite_hook_for_the_send_endpoint(make_sender) -> None:
    sender = make_sender(effort="max")
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False, rewrite_send=sender._rewrite_send_route)

    route = ctx.trigger_route(
        SEND_URL, method="POST", post_data=json.dumps({"thinking_effort": "standard"})
    )

    assert route.continued is True
    assert json.loads(sender._last_sent_body)["thinking_effort"] == "max"


def test_block_unused_still_aborts_every_unused_path_with_a_hook_set(
    make_sender,
) -> None:
    """The hook must never be offered a request block_unused was going to
    abort; UNUSED_ON_SEND stays authoritative."""
    sender = make_sender(effort="max")
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False, rewrite_send=sender._rewrite_send_route)

    for path in cc.UNUSED_ON_SEND:
        route = ctx.trigger_route(f"https://chatgpt.com{path}?x=1")
        assert route.aborted is True
        assert route.continued is False
    assert sender._last_sent_body is None


def test_block_unused_leaves_the_prepare_sibling_to_its_own_continue(
    make_sender,
) -> None:
    sender = make_sender(effort="max")
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False, rewrite_send=sender._rewrite_send_route)

    route = ctx.trigger_route(PREPARE_URL, method="POST", post_data="irrelevant")

    assert route.continued is True  # block_unused's own continue_, unrewritten
    assert sender._last_sent_body is None


def test_block_unused_without_a_hook_behaves_exactly_as_before() -> None:
    """``rewrite_send`` omitted: identical to the pre-existing behaviour
    tested in test_client_browser.py / test_client_core.py."""
    ctx = fp.BrowserContext()
    cc.block_unused(ctx, allow_project=False)

    route = ctx.trigger_route(SEND_URL)

    assert route.continued is True
    assert route.aborted is False


# ---------------------------------------------------------------------------
# fixtures duplicated from test_client_browser.py (see its own module
# docstring for the rationale; nothing outside this file may be edited to
# add a fixture)
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


# ---------------------------------------------------------------------------
# _open -- end to end: the route it registers, over the real launched context
# ---------------------------------------------------------------------------


def test_open_registers_no_extra_route_when_nothing_is_pinned(
    enter_env, fake_pw
) -> None:
    """No effort, model or search: block_unused's own two routes are the
    only ones on the context, and a POST to the send endpoint goes through
    with its body untouched -- "no route is registered when nothing is
    set" (TESTING.md-style contract for this change)."""
    sender = cc.BrowserSender()
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert [pattern for pattern, _ in ctx.routes] == [
            "**/backend-api/**",
            "**/ces/v1/**",
        ]
        route = ctx.trigger_route(
            SEND_URL, method="POST", post_data=json.dumps({"thinking_effort": "max"})
        )
        assert route.continued is True
        assert sender._last_sent_body is None
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_rewrites_the_send_body_through_the_registered_route(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender(effort="max", model="gpt-6-pro", search=True)
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        # block_unused's own two patterns still register; no third pattern
        # is added (the rewrite rides inside them, see block_unused).
        assert [pattern for pattern, _ in ctx.routes] == [
            "**/backend-api/**",
            "**/ces/v1/**",
        ]

        route = ctx.trigger_route(
            SEND_URL,
            method="POST",
            post_data=json.dumps(
                {"thinking_effort": "standard", "model": "old", "system_hints": []}
            ),
        )

        assert route.continued is True
        sent = json.loads(sender._last_sent_body)
        assert sent == {
            "thinking_effort": "max",
            "model": "gpt-6-pro",
            "system_hints": ["search"],
        }
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_rewrite_route_still_aborts_the_usual_blocked_paths(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender(effort="max")
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        route = ctx.trigger_route(f"https://chatgpt.com{cc.UNUSED_ON_SEND[0]}?x=1")
        assert route.aborted is True
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_open_rewrite_route_leaves_the_prepare_sibling_untouched(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender(search=True)
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        route = ctx.trigger_route(PREPARE_URL, method="POST", post_data="irrelevant")
        assert route.continued is True
        assert sender._last_sent_body is None
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# record_send_body -- "original" and "sent", or the old shape when nothing
# was rewritten
# ---------------------------------------------------------------------------


def test_record_send_body_carries_original_and_sent_when_rewriting(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(effort="max", record_send_body=str(target))
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        original = json.dumps({"thinking_effort": "standard"})
        # Real order, measured 2026-09-20: the page's request event fires
        # first with the pre-rewrite post_data, then the route runs. A
        # record written only on the event showed "sent" equal to
        # "original" while the reply's metadata proved the rewrite went out.
        sender.page.emit_request(SEND_URL, method="POST", post_data=original)
        ctx.trigger_route(SEND_URL, method="POST", post_data=original)

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["method"] == "POST"
        assert doc["url"] == SEND_URL
        assert doc["original"] == original
        assert json.loads(doc["sent"]) == {"thinking_effort": "max"}
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_sent_equals_original_when_the_body_was_not_rewritten(
    enter_env, fake_pw, tmp_path
) -> None:
    """Effort is pinned, but this particular request's body is not JSON
    (e.g. some other backend-api POST slipping through), so the hook
    declines it: "sent" falls back to the same text as "original"."""
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(effort="max", record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(SEND_URL, method="POST", post_data="not json")

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["original"] == "not json"
        assert doc["sent"] == "not json"
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_keeps_the_old_shape_when_nothing_is_pinned(
    enter_env, fake_pw, tmp_path
) -> None:
    """Backward compatibility: tests/test_client_search.py's own recorder
    test constructs a sender with no effort/model/search and asserts the
    file equals exactly {"method", "url", "post_data"}; this pins that
    contract from this file's side too, so it cannot be widened by
    accident for a sender that pins nothing."""
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(SEND_URL, method="POST", post_data='{"a": 1}')
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert set(doc) == {"method", "url", "post_data"}
        assert doc["post_data"] == '{"a": 1}'
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)
