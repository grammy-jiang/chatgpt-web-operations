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
# rewrite_send_body -- hints, the generic form of --search (ROADMAP.md,
# Stage 3 item 4: "Deep research: --system-hint
# plugin:connector_openai_deep_research")
# ---------------------------------------------------------------------------


def test_hints_alone_create_the_key_and_touch_nothing_else() -> None:
    body = _body()
    del body["system_hints"]
    out = cc.rewrite_send_body(
        body,
        effort="",
        model="",
        search=False,
        hints=("plugin:connector_openai_deep_research",),
    )
    assert out["system_hints"] == ["plugin:connector_openai_deep_research"]
    assert out["thinking_effort"] == "max"
    assert out["model"] == "gpt-5-6-thinking"


def test_hints_alone_are_appended_after_any_existing_hints_in_order() -> None:
    out = cc.rewrite_send_body(
        _body(system_hints=["existing"]),
        effort="",
        model="",
        search=False,
        hints=("first", "second"),
    )
    assert out["system_hints"] == ["existing", "first", "second"]


def test_hints_alone_is_not_the_identity_case_a_copy_is_made() -> None:
    """Unlike blank everything, a non-empty hints alone is something to
    rewrite: the returned body must not be the same object."""
    body = _body()
    out = cc.rewrite_send_body(
        body, effort="", model="", search=False, hints=("tasks",)
    )
    assert out is not body
    assert body["system_hints"] == []  # the original is untouched


def test_hints_plus_search_do_not_duplicate_the_search_hint() -> None:
    """search=True and "search" repeated in hints: only one "search" ends
    up in system_hints, in the position the search flag put it."""
    out = cc.rewrite_send_body(
        _body(), effort="", model="", search=True, hints=("search", "extra")
    )
    assert out["system_hints"] == ["search", "extra"]


def test_duplicate_hints_collapse_to_one_occurrence_each() -> None:
    out = cc.rewrite_send_body(
        _body(), effort="", model="", search=False, hints=("a", "a", "b", "a")
    )
    assert out["system_hints"] == ["a", "b"]


def test_a_hint_already_on_the_body_is_not_duplicated() -> None:
    out = cc.rewrite_send_body(
        _body(system_hints=["plugin:connector_openai_deep_research"]),
        effort="",
        model="",
        search=False,
        hints=("plugin:connector_openai_deep_research",),
    )
    assert out["system_hints"] == ["plugin:connector_openai_deep_research"]


def test_hints_effort_model_and_search_all_together() -> None:
    out = cc.rewrite_send_body(
        _body(),
        effort="extended",
        model="gpt-6-pro",
        search=True,
        hints=("plugin:connector_openai_deep_research",),
    )
    assert out["thinking_effort"] == "extended"
    assert out["model"] == "gpt-6-pro"
    assert out["system_hints"] == ["search", "plugin:connector_openai_deep_research"]


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


def test_route_rewrites_a_post_using_hints_alone(make_sender) -> None:
    """A hint set with no effort/model/search still rewrites the body
    (ROADMAP.md, Stage 3 item 4)."""
    sender = make_sender(hints=("plugin:connector_openai_deep_research",))
    route = _route(
        SEND_URL,
        post_data=json.dumps({"thinking_effort": "max", "system_hints": []}),
    )

    handled = sender._rewrite_send_route(route)

    assert handled is True
    assert route.continued is True
    assert json.loads(sender._last_sent_body) == {
        "thinking_effort": "max",
        "system_hints": ["plugin:connector_openai_deep_research"],
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


def test_open_registers_the_rewrite_route_for_a_hint_set_alone(
    enter_env, fake_pw
) -> None:
    """No effort, model or search -- only a system hint: the route must
    still register (ROADMAP.md, Stage 3 item 4), the same contract as
    ``test_open_rewrites_the_send_body_through_the_registered_route`` above,
    proven with ``hints`` as the only thing pinned."""
    sender = cc.BrowserSender(hints=("plugin:connector_openai_deep_research",))
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        assert [pattern for pattern, _ in ctx.routes] == [
            "**/backend-api/**",
            "**/ces/v1/**",
        ]

        route = ctx.trigger_route(
            SEND_URL,
            method="POST",
            post_data=json.dumps({"thinking_effort": "max", "system_hints": []}),
        )

        assert route.continued is True
        sent = json.loads(sender._last_sent_body)
        assert sent == {
            "thinking_effort": "max",
            "system_hints": ["plugin:connector_openai_deep_research"],
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
    """Backward compatibility: a sender with no effort/model/search must
    still write exactly {"method", "url", "post_data", "stream_file"} --
    the shape ``_record_request`` wrote before any rewrite hook existed,
    plus the one field ``_write_record`` now always adds -- so it cannot be
    widened any further by accident for a sender that pins nothing. Pinned
    by the section below too, moved here from the old
    tests/test_client_search.py."""
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(SEND_URL, method="POST", post_data='{"a": 1}')
        doc = json.loads(target.read_text(encoding="utf-8"))
        assert set(doc) == {"method", "url", "post_data", "stream_file"}
        assert doc["post_data"] == '{"a": 1}'
        assert doc["stream_file"] == f"{target}.stream.txt"
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# record_send_body -- the request listener attached in _open, moved from
# tests/test_client_search.py (ROADMAP.md, Stage 3 item 2 / B2): that file's
# other tests covered the composer's "+" menu, which no longer exists --
# search reaches a send only through rewrite_send_body now, tested above --
# so only its recorder tests survive, here
# ---------------------------------------------------------------------------


def test_record_send_body_writes_the_f_conversation_post_body(
    enter_env, fake_pw, tmp_path
) -> None:
    # A nested, not-yet-existing directory: _record_request must create it.
    target = tmp_path / "sub" / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation",
            method="POST",
            post_data='{"system_hints": ["search"]}',
        )

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc == {
            "method": "POST",
            "url": "https://chatgpt.com/backend-api/f/conversation",
            "post_data": '{"system_hints": ["search"]}',
            "stream_file": f"{target}.stream.txt",
        }
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_ignores_the_prepare_handshake(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation/prepare",
            method="POST",
            post_data="irrelevant",
        )

        assert not target.exists()
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_ignores_gets(enter_env, fake_pw, tmp_path) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(
            "https://chatgpt.com/backend-api/f/conversation", method="GET"
        )

        assert not target.exists()
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_record_send_body_off_by_default_attaches_no_listener(
    enter_env, fake_pw
) -> None:
    sender = cc.BrowserSender()  # record_send_body=""
    try:
        sender._open()

        assert sender.page._event_handlers.get("request", []) == []
        assert sender.page._event_handlers.get("response", []) == []
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


# ---------------------------------------------------------------------------
# stream_events -- pure parser over a recorded SSE stream (module docstring,
# WHY): event: lines, blank lines, and data: lines whose payload is any
# JSON type, ending in the stream's own data: [DONE]. The shape below is
# synthetic, modelled on the real framing recorded 2026-09-20 (delta_encoding
# "v1", a resume token, a run of delta frames, trailing message_stream_complete
# / title_generation / conversation_detail_metadata, then [DONE]) -- not the
# real stream itself, which is not read here.
# ---------------------------------------------------------------------------

REALISTIC_STREAM = (
    "event: delta_encoding\n"
    'data: "v1"\n'
    "\n"
    'data: {"type": "resume_conversation_token", "kind": "topic", '
    '"token": "resume-token-fake"}\n'
    "\n"
    "event: delta\n"
    'data: {"v": {"message": {"id": "m1"}, "conversation_id": "c1", '
    '"error": null}, "c": 0}\n'
    "event: delta\n"
    'data: {"p": "/message/content/parts/0", "o": "append", "v": "hi"}\n'
    "event: delta\n"
    'data: {"o": "patch", "v": [{"p": "/message/status", "o": "replace", '
    '"v": "finished_successfully"}]}\n'
    "event: delta\n"
    'data: {"o": "add", "p": "/some/path", "v": 1}\n'
    'data: {"type": "message_stream_complete", "conversation_id": "c1"}\n'
    'data: {"type": "title_generation", "title": "Hi"}\n'
    'data: {"type": "conversation_detail_metadata", "banner_info": null}\n'
    "data: [DONE]\n"
)


def test_stream_events_parses_every_payload_type_in_stream_order() -> None:
    """A bare JSON string first (``"v1"``), then a resume-token object, a
    run of delta frames (a full message object, an append, and a
    patch-list), the trailing message_stream_complete / title_generation /
    conversation_detail_metadata objects, and finally ``data: [DONE]``,
    which contributes nothing -- ``event: `` lines and blank lines skipped
    throughout."""
    assert cc.stream_events(REALISTIC_STREAM) == [
        "v1",
        {
            "type": "resume_conversation_token",
            "kind": "topic",
            "token": "resume-token-fake",
        },
        {
            "v": {"message": {"id": "m1"}, "conversation_id": "c1", "error": None},
            "c": 0,
        },
        {"p": "/message/content/parts/0", "o": "append", "v": "hi"},
        {
            "o": "patch",
            "v": [
                {
                    "p": "/message/status",
                    "o": "replace",
                    "v": "finished_successfully",
                }
            ],
        },
        {"o": "add", "p": "/some/path", "v": 1},
        {"type": "message_stream_complete", "conversation_id": "c1"},
        {"type": "title_generation", "title": "Hi"},
        {"type": "conversation_detail_metadata", "banner_info": None},
    ]


def test_stream_events_skips_event_lines_blank_lines_done_and_bad_json() -> None:
    sse = (
        'event: delta\ndata: {"a": 1}\n\ndata: {"b": 2}\ndata: not-json\ndata: [DONE]\n'
    )
    assert cc.stream_events(sse) == [{"a": 1}, {"b": 2}]


def test_stream_events_returns_a_list_or_number_payload_unwrapped() -> None:
    """Any JSON type comes back exactly as parsed, not only objects and
    strings (module docstring: "an object, a list, ... or a number")."""
    assert cc.stream_events("data: [1, 2, 3]\ndata: 42\n") == [[1, 2, 3], 42]


def test_stream_events_over_empty_text_is_an_empty_list() -> None:
    assert cc.stream_events("") == []


# ---------------------------------------------------------------------------
# find_session_id / find_key -- recursive search through dicts, lists and
# strings: a session id can sit inside a string that is itself JSON text (a
# tool call's code, or a tool reply's own JSON), never at a fixed path
# (module docstring, WHY)
# ---------------------------------------------------------------------------


def test_find_session_id_inside_a_tool_call_code_string() -> None:
    """A composer tool call's code is JSON text carried as a plain string
    value; the session id sits inside it, not as a dict key at that level."""
    events = [
        {
            "v": {
                "message": {
                    "content": {
                        "content_type": "code",
                        "text": (
                            '{"path": "/Deep Research App/start", "args": '
                            '{"session_id": "abc12345-dead-beef-0000-111122223333"}}'
                        ),
                    }
                }
            }
        }
    ]
    assert cc.find_session_id(events) == "abc12345-dead-beef-0000-111122223333"


def test_find_session_id_inside_a_patch_lists_json_string_value() -> None:
    """A ``{"o": "patch", "v": [...]}`` delta's own patch "v" can itself be
    a JSON string carrying the session id, e.g. a tool reply's payload."""
    events = [
        {
            "o": "patch",
            "v": [
                {
                    "p": "/message/content/parts/0",
                    "o": "replace",
                    "v": '{"session_id": "sess-xyz-789", "ok": true}',
                }
            ],
        }
    ]
    assert cc.find_session_id(events) == "sess-xyz-789"


def test_find_session_id_nested_several_levels_deep() -> None:
    events = [
        {"type": "delta", "v": {"other": 1}},
        {"type": "tool", "output": {"deep": {"session_id": "sess-123"}}},
    ]
    assert cc.find_session_id(events) == "sess-123"


def test_find_session_id_inside_a_list() -> None:
    events = [{"items": [{"x": 1}, {"session_id": "abc"}]}]
    assert cc.find_session_id(events) == "abc"


def test_find_session_id_absent_returns_none() -> None:
    events = [{"a": 1}, {"b": [1, 2, {"c": 3}]}]
    assert cc.find_session_id(events) is None


def test_find_session_id_returns_the_first_occurrence_in_stream_order() -> None:
    events = [{"session_id": "first"}, {"session_id": "second"}]
    assert cc.find_session_id(events) == "first"


def test_find_session_id_an_empty_string_value_does_not_win() -> None:
    """A dict's own "session_id" key must hold a *non-empty* string to win
    outright; otherwise the search still walks the rest of the dict,
    including that same key's own (non-winning) value."""
    events = [{"session_id": "", "nested": {"session_id": "real"}}]
    assert cc.find_session_id(events) == "real"


def test_find_session_id_falls_back_to_regex_when_the_string_is_not_clean_json() -> (
    None
):
    """A string can carry "session_id" without being valid JSON on its own
    -- e.g. a fragment wrapped in surrounding text -- and the regex
    fallback still finds it."""
    events = [
        {"note": 'garbled text before "session_id": "0123456789abcdef0123" after'}
    ]
    assert cc.find_session_id(events) == "0123456789abcdef0123"


def test_find_session_id_regex_fallback_with_no_match_returns_none() -> None:
    """The string contains the key's own text but not in the quoted-value
    shape the regex expects (``json.loads`` also fails on it): no match."""
    events = [{"note": "the session_id field is not set for this run"}]
    assert cc.find_session_id(events) is None


def test_find_session_id_events_of_plain_strings_or_numbers_never_raise() -> None:
    """``stream_events``'s own return type can include bare strings and
    numbers alongside objects; the search must not assume a dict, a list,
    or even a string."""
    assert cc.find_session_id(["v1", 42, 3.14, None, True]) is None


def test_find_key_searches_for_any_key_not_just_session_id() -> None:
    """``find_session_id`` is ``find_key(events, "session_id")``; the
    helper itself is not hardcoded to one key."""
    events = [{"conversation_id": "conv-1", "session_id": "sess-1"}]
    assert cc.find_key(events, "conversation_id") == "conv-1"
    assert cc.find_key(events, "session_id") == "sess-1"


# ---------------------------------------------------------------------------
# _is_send_stream_response -- the predicate _send's own page.expect_response
# watches with, replacing the removed page.on("response", ...) handler
# ---------------------------------------------------------------------------


def test_is_send_stream_response_accepts_a_post_to_the_send_endpoint(
    make_sender,
) -> None:
    sender = make_sender()
    response = fp.Response(fp.Request(SEND_URL, "POST", "{}"))

    assert sender._is_send_stream_response(response) is True


def test_is_send_stream_response_rejects_the_prepare_sibling(make_sender) -> None:
    sender = make_sender()
    response = fp.Response(fp.Request(PREPARE_URL, "POST", "irrelevant"))

    assert sender._is_send_stream_response(response) is False


def test_is_send_stream_response_rejects_a_get(make_sender) -> None:
    sender = make_sender()
    response = fp.Response(fp.Request(SEND_URL, "GET"))

    assert sender._is_send_stream_response(response) is False


# ---------------------------------------------------------------------------
# _send -- capturing the reply stream synchronously, via page.expect_response
# / Response.finished(), instead of the removed page.on("response", ...)
# handler: WHAT WENT WRONG LIVE (2026-09-20) was that handler firing on the
# response object before its SSE body had finished streaming, then racing
# the caller's window close -- the suppressed exception hid it, and a real
# send produced the JSON record but no sibling ".stream.txt" file.
# ---------------------------------------------------------------------------

REAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REAL_URL = f"https://chatgpt.com/c/{REAL_ID}"


def _wire_confirmed_send(page: fp.Page) -> None:
    """Everything ``_send()`` needs to click the send button and see the
    post confirmed with a real conversation id, so the tests below can
    isolate what the stream capture does; the click/post-confirm wiring
    itself is already covered on its own by test_client_browser.py."""
    page.set_locator(
        cc.BrowserSender.SEND_BUTTONS[0], count=1, visible=True, enabled=True
    )
    page.set_locator('[data-message-author-role="user"]', count=fp.sequence(0, 1))
    page.url = fp.sequence(REAL_URL)


def test_send_writes_the_stream_file_and_the_record_names_it(
    make_sender, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = make_sender(record_send_body=str(target))
    page = fp.Page()
    sender.page = page
    _wire_confirmed_send(page)
    sse = 'data: {"session_id": "abc"}\n\ndata: [DONE]\n'
    page.set_next_response(
        fp.Response(fp.Request(SEND_URL, "POST", "{}"), status=200, body=sse)
    )
    sender._record_request(fp.Request(SEND_URL, "POST", "{}"))  # what _open wires

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert Path(f"{target}.stream.txt").read_text(encoding="utf-8") == sse
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["stream_file"] == f"{target}.stream.txt"


def test_send_records_the_stream_on_the_provisional_id_return_too(
    make_sender, tmp_path
) -> None:
    """_send has two success returns (a resolved id, and a still-``WEB:``
    provisional one); both must record the stream, not just the first."""
    target = tmp_path / "body.json"
    sender = make_sender(record_send_body=str(target))
    page = fp.Page()
    sender.page = page
    page.set_locator(
        cc.BrowserSender.SEND_BUTTONS[0], count=1, visible=True, enabled=True
    )
    page.set_locator('[data-message-author-role="user"]', count=fp.sequence(0, 1))
    provisional_id = f"WEB:{REAL_ID}"
    page.url = fp.sequence(f"https://chatgpt.com/c/{provisional_id}")
    sse = "data: {}\n\ndata: [DONE]\n"
    page.set_next_response(
        fp.Response(fp.Request(SEND_URL, "POST", "{}"), status=200, body=sse)
    )

    result = sender._send("hello", chat=None, name="task")

    assert result == provisional_id
    assert Path(f"{target}.stream.txt").read_text(encoding="utf-8") == sse


def test_send_records_nothing_when_the_response_text_raises(
    make_sender, tmp_path
) -> None:
    """The capture is wrapped in contextlib.suppress(Exception): a failure
    there must record nothing and never fail a send that already went
    through."""
    target = tmp_path / "body.json"
    sender = make_sender(record_send_body=str(target))
    page = fp.Page()
    sender.page = page
    _wire_confirmed_send(page)

    class _FailingTextResponse:
        def __init__(self, request: fp.Request) -> None:
            self.request = request

        def finished(self) -> None:
            return None

        def text(self) -> str:
            raise RuntimeError("boom")

    page.set_next_response(_FailingTextResponse(fp.Request(SEND_URL, "POST", "{}")))

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert not Path(f"{target}.stream.txt").exists()


def test_send_without_record_send_body_never_calls_expect_response(
    make_sender,
) -> None:
    sender = make_sender()  # record_send_body="" (default)
    page = fp.Page()
    sender.page = page
    _wire_confirmed_send(page)

    result = sender._send("hello", chat=None, name="task")

    assert result == REAL_ID
    assert [c for c in page.calls if c[1] == "expect_response"] == []


def test_write_record_includes_the_stream_file_path(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(record_send_body=str(target))
    try:
        sender._open()
        sender.page.emit_request(SEND_URL, method="POST", post_data='{"a": 1}')

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["stream_file"] == f"{target}.stream.txt"
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)


def test_write_record_includes_the_stream_file_path_when_rewriting_too(
    enter_env, fake_pw, tmp_path
) -> None:
    target = tmp_path / "body.json"
    sender = cc.BrowserSender(effort="max", record_send_body=str(target))
    try:
        sender._open()
        ctx = fake_pw.chromium.launch_persistent_context_result
        original = json.dumps({"thinking_effort": "standard"})
        ctx.trigger_route(SEND_URL, method="POST", post_data=original)

        doc = json.loads(target.read_text(encoding="utf-8"))
        assert doc["stream_file"] == f"{target}.stream.txt"
    finally:
        sender._stack.close()
        sender._owner.shutdown(wait=True)
