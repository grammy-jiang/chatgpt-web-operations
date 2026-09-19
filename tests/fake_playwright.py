"""A synchronous, in-memory fake of the Playwright ``sync_api`` surface.

Nothing here touches a real browser, a real profile, or the network: every
object is a plain Python stand-in that records what was called on it and
returns whatever a test configured beforehand. It exists so
``tests/test_client_browser.py`` can drive ``chatgpt_client.BrowserSender``
without Playwright or Chrome, and so later test files for
``create_project.py``, ``discover_endpoints.py`` and ``measure_window.py``
can reuse the same fake instead of inventing their own.

Installing the fake
--------------------
``chatgpt_client.py`` never imports Playwright at module level; every site
does a *lazy* ``from playwright.sync_api import sync_playwright`` or
``from playwright.sync_api import TimeoutError as PWTimeout`` inside the
function that needs it. That is the seam this fake uses::

    driver = fake_playwright.install(monkeypatch)

installs a fake ``playwright`` / ``playwright.sync_api`` module pair into
``sys.modules`` (restored automatically when the test ends, because it is
done through ``monkeypatch.setitem``) and returns the ``Playwright`` driver
instance that ``sync_playwright()`` will hand back. Configure
``driver.chromium`` *before* the code under test runs, e.g.::

    driver.chromium.launch_persistent_context_raises = PlaywrightTimeoutError("x")

``install()`` also makes ``playwright.sync_api.TimeoutError`` resolve to
this module's ``PlaywrightTimeoutError``, so
``from playwright.sync_api import TimeoutError as PWTimeout`` inside
``chatgpt_client.py`` catches exactly the exceptions this fake raises.

Object graph
------------
``sync_playwright()`` (context manager) -> ``Playwright`` (``.chromium`` is
a ``BrowserType``) -> ``BrowserType.launch()`` -> ``Browser``
(``.new_context()`` -> ``BrowserContext``), or
``BrowserType.launch_persistent_context()`` -> ``BrowserContext`` directly.
``BrowserContext.new_page()`` -> ``Page``. ``Page.locator(selector)`` (and
``get_by_role`` / ``get_by_text``, which key into the same registry, see
``key_for_role`` / ``key_for_text``) -> ``Locator``.

Every ``Browser`` / ``BrowserContext`` / ``Page`` keeps its own ordered
``calls`` list of ``(kind, selector_or_name, method, args, kwargs)`` tuples,
and every ``Locator`` obtained from a page appends its calls to *that
page's* list, so a test can assert an exact sequence of what happened,
e.g. ``[c for c in page.calls if c[2] == "click"]``. Only actions that do
something are recorded (``click``, ``fill``, ``press``, ``wait_for``,
``set_input_files``, ``get_attribute``, ``inner_text``, plus the ``Page``
actions ``goto``, ``screenshot``, ``close``, ``evaluate``,
``wait_for_timeout``); ``count()``, ``is_visible()`` and ``is_enabled()``
are silent reads, since their answers are already controlled by
``set_locator(...)`` and are more directly asserted from there.

Configuring a locator
----------------------
``page.set_locator(selector, count=1, texts=["hi"], visible=True,
enabled=True, attributes={"aria-checked": "true"}, raises=None)`` declares
what ``page.locator(selector)`` will report. Any field may instead be a
zero-argument callable, so behaviour can change across repeated reads (a
button that is disabled for the first two polls and enabled on the third,
an element that is not there yet and then is). ``sequence(*values)``
builds such a callable: it yields ``values`` in order and repeats the last
one forever once exhausted -- use it for ``count``, ``visible``,
``enabled``, or ``raises``. ``texts`` and ``attributes`` may also be a
plain list/dict (uniform across indices) or a list of lists/dicts (one
entry per ``.nth(i)`` index); ``raises`` may be a single exception
*instance* (never a bare class -- classes are callable and would be treated
as a dynamic callable), ``None``, or ``{"click": exc, "fill": None}`` to
vary by method name.

``PlaywrightTimeoutError`` / ``PlaywrightError`` are this fake's exception
types, shaped like ``playwright.sync_api``'s; raise instances of them from
a ``raises=`` field to simulate a timeout the way the real driver would.

Routing
-------
``context.route(pattern, handler)`` is recorded in ``context.routes``.
``context.trigger_route(url, method="GET", post_data=None)`` builds a fake
``Request`` and ``Route``, finds the first registered pattern that glob-
matches (Playwright's ``**``/``*`` syntax) the URL, calls its handler, and
returns the ``Route`` so a test can assert ``.aborted`` / ``.continued`` /
``.fulfilled``.

Events
------
``page.on(event, handler)`` / ``context.on(event, handler)`` record
handlers; ``page.emit_request(url, ...)`` and
``page.emit_response(url, status=200, text="...", ...)`` build a fake
``Request`` / ``Response`` and invoke every handler registered for
``"request"`` / ``"response"``.

File uploads
------------
``page.expect_file_chooser()`` is a context manager whose yielded object
has ``.value``, a ``FileChooser`` with ``set_files(path)`` (recorded, and
kept on ``.files``) -- for code that waits on the real file-chooser event.
Code that instead calls ``.set_input_files(path)`` directly on a
``Locator`` (as ``chatgpt_client.BrowserSender._attach_prompt`` does) needs
no such wrapper; that call is just another recorded ``Locator`` action.
"""

from __future__ import annotations

import re
import sys
import types
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlaywrightError(Exception):
    """Stand-in for ``playwright.sync_api.Error``."""


class PlaywrightTimeoutError(PlaywrightError):
    """Stand-in for ``playwright.sync_api.TimeoutError``."""


def _is_exception(value: Any) -> bool:
    return isinstance(value, BaseException) or (
        isinstance(value, type) and issubclass(value, BaseException)
    )


# ---------------------------------------------------------------------------
# Scripting helper
# ---------------------------------------------------------------------------


def sequence(*values: Any):
    """A 0-arg callable that yields ``values`` in order, then repeats the last.

    ``sequence(0, 1)`` returns 0 the first time it is called and 1 on every
    call after that -- the shape a growing conversation-turn count needs.
    ``sequence(TimeoutError(...), None)`` fails once, then succeeds forever.
    """
    box = {"i": 0}
    values_list = list(values)

    def _next() -> Any:
        if not values_list:
            return None
        i = min(box["i"], len(values_list) - 1)
        box["i"] += 1
        return values_list[i]

    return _next


def _resolve(value: Any, index: int | None, default: Any) -> Any:
    """A configured field's current value: static, indexed, or scripted."""
    if callable(value) and not _is_exception(value):
        value = value()
    if isinstance(value, (list, tuple)):
        if not value:
            return default
        i = 0 if index is None else index
        i = min(i, len(value) - 1)
        return value[i]
    if value is None:
        return default
    return value


# ---------------------------------------------------------------------------
# Locator configuration + the Locator itself
# ---------------------------------------------------------------------------


class ElementState:
    """What a selector reports: shared by every ``Locator`` view of it."""

    def __init__(
        self,
        *,
        count: Any = 0,
        texts: Any = "",
        visible: Any = True,
        enabled: Any = True,
        attributes: Any = None,
        raises: Any = None,
    ) -> None:
        self.count = count
        self.texts = texts
        self.visible = visible
        self.enabled = enabled
        self.attributes = attributes
        self.raises = raises


def key_for_role(role: str, name: Any = None, exact: bool = False) -> str:
    """The selector key ``Page.get_by_role`` uses; ``name`` may be a regex."""
    pattern = getattr(name, "pattern", name)
    return f"role={role}|name={pattern!r}|exact={bool(exact)}"


def key_for_text(text: Any) -> str:
    """The selector key ``Page.get_by_text`` uses; ``text`` may be a regex."""
    pattern = getattr(text, "pattern", text)
    return f"text={pattern!r}"


class Locator:
    """One element (``.nth(i)`` / ``.first``) or a whole matched set."""

    def __init__(
        self, page: Page, selector: str, state: ElementState, index: int | None = None
    ) -> None:
        self.page = page
        self.selector = selector
        self._state = state
        self._index = index

    def nth(self, i: int) -> Locator:
        return Locator(self.page, self.selector, self._state, index=i)

    @property
    def first(self) -> Locator:
        return self.nth(0)

    def count(self) -> int:
        return int(_resolve(self._state.count, None, 0))

    def is_visible(self) -> bool:
        return bool(_resolve(self._state.visible, self._index, True))

    def is_enabled(self) -> bool:
        return bool(_resolve(self._state.enabled, self._index, True))

    def get_attribute(self, name: str) -> str | None:
        self._record("get_attribute", (name,), {})
        self._maybe_raise("get_attribute")
        attrs = _resolve(self._state.attributes, self._index, None) or {}
        return attrs.get(name)

    def inner_text(self, timeout: int | None = None) -> str:
        self._record("inner_text", (), {"timeout": timeout})
        self._maybe_raise("inner_text")
        return _resolve(self._state.texts, self._index, "")

    def click(self, timeout: int | None = None, force: bool = False, **kw: Any) -> None:
        self._record("click", (), {"timeout": timeout, "force": force, **kw})
        self._maybe_raise("click")

    def fill(self, text: str, timeout: int | None = None, **kw: Any) -> None:
        self._record("fill", (text,), {"timeout": timeout, **kw})
        self._maybe_raise("fill")

    def press(self, key: str, **kw: Any) -> None:
        self._record("press", (key,), kw)
        self._maybe_raise("press")

    def wait_for(self, **kw: Any) -> None:
        self._record("wait_for", (), kw)
        self._maybe_raise("wait_for")

    def set_input_files(self, path: Any, **kw: Any) -> None:
        self._record("set_input_files", (path,), kw)
        self._maybe_raise("set_input_files")

    def all(self) -> list[Locator]:
        return [self.nth(i) for i in range(self.count())]

    def _record(self, method: str, args: tuple, kwargs: dict) -> None:
        self.page.calls.append(("locator", self.selector, method, args, kwargs))

    def _maybe_raise(self, method: str) -> None:
        raw = self._state.raises
        value = raw() if callable(raw) and not _is_exception(raw) else raw
        if isinstance(value, dict):
            value = value.get(method)
            if callable(value) and not _is_exception(value):
                value = value()
        if value is not None:
            raise value


# ---------------------------------------------------------------------------
# Requests, responses, routes, file choosers
# ---------------------------------------------------------------------------


class Request:
    def __init__(self, url: str, method: str = "GET", post_data: str | None = None):
        self.url = url
        self.method = method
        self.post_data = post_data


class Response:
    def __init__(self, request: Request, status: int = 200, body: str = ""):
        self.request = request
        self.url = request.url
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body


class Route:
    def __init__(self, request: Request):
        self.request = request
        self.aborted = False
        self.continued = False
        self.fulfilled = False
        self.fulfill_kwargs: dict | None = None

    def abort(self, *a: Any, **kw: Any) -> None:
        self.aborted = True

    def continue_(self, *a: Any, **kw: Any) -> None:
        self.continued = True

    def fulfill(self, **kw: Any) -> None:
        self.fulfilled = True
        self.fulfill_kwargs = kw


class FileChooser:
    def __init__(self, page: Page):
        self._page = page
        self.files: list[Any] = []

    def set_files(self, path: Any, **kw: Any) -> None:
        self._page.calls.append(("page", "file_chooser.set_files", (path,), kw))
        self.files.append(path)


class _FileChooserInfo:
    def __init__(self, page: Page):
        self.value = FileChooser(page)


def _glob_match(pattern: str, url: str) -> bool:
    """Playwright-style ``**``/``*`` glob match (``**`` crosses ``/``)."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.match("".join(out) + "$", url) is not None


# ---------------------------------------------------------------------------
# Keyboard, Page
# ---------------------------------------------------------------------------


class Keyboard:
    def __init__(self, page: Page):
        self._page = page

    def press(self, key: str, **kw: Any) -> None:
        self._page.calls.append(("page", "keyboard.press", (key,), kw))


class Page:
    """One tab. ``.calls`` also carries every ``Locator`` action taken on it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._url: Any = "about:blank"
        self.last_goto_url: str | None = None
        self.goto_raises: BaseException | None = None
        self.keyboard = Keyboard(self)
        self.closed = False
        self._locator_states: dict[str, ElementState] = {}
        self._script_results: dict[str, Any] = {}
        self._event_handlers: dict[str, list] = {}

    # -- navigation ----------------------------------------------------
    @property
    def url(self) -> str:
        return self._url() if callable(self._url) else self._url

    @url.setter
    def url(self, value: Any) -> None:
        self._url = value

    def goto(self, url: str, **kw: Any) -> None:
        self.calls.append(("page", "goto", (url,), kw))
        self.last_goto_url = url
        if self.goto_raises is not None:
            raise self.goto_raises
        if not callable(self._url):
            self._url = url

    # -- locators --------------------------------------------------------
    def set_locator(self, selector: str, **kw: Any) -> ElementState:
        state = self._locator_states.setdefault(selector, ElementState())
        for key, value in kw.items():
            setattr(state, key, value)
        return state

    def locator(self, selector: str) -> Locator:
        state = self._locator_states.setdefault(selector, ElementState())
        return Locator(self, selector, state)

    def get_by_role(
        self, role: str, name: Any = None, exact: bool = False, **kw: Any
    ) -> Locator:
        return self.locator(key_for_role(role, name, exact))

    def get_by_text(self, text: Any, **kw: Any) -> Locator:
        return self.locator(key_for_text(text))

    def query_selector(self, selector: str) -> Locator | None:
        state = self._locator_states.get(selector)
        if state is not None and _resolve(state.count, None, 0) > 0:
            return Locator(self, selector, state, index=0)
        return None

    def wait_for_selector(self, selector: str, **kw: Any) -> Locator:
        loc = self.locator(selector)
        loc.wait_for(**kw)
        return loc

    # -- scripting / misc --------------------------------------------------
    def set_evaluate_result(self, substring: str, value: Any) -> None:
        """``page.evaluate(js)`` returns ``value`` when ``substring`` is in ``js``."""
        self._script_results[substring] = value

    def evaluate(self, js: str, *args: Any) -> Any:
        self.calls.append(("page", "evaluate", (js, *args), {}))
        for substring, value in self._script_results.items():
            if substring in js:
                return value
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.calls.append(("page", "wait_for_timeout", (ms,), {}))

    def screenshot(self, **kw: Any) -> None:
        self.calls.append(("page", "screenshot", (), kw))

    def close(self) -> None:
        self.calls.append(("page", "close", (), {}))
        self.closed = True

    def on(self, event: str, handler: Any) -> None:
        self.calls.append(("page", "on", (event,), {}))
        self._event_handlers.setdefault(event, []).append(handler)

    def emit_request(
        self, url: str, method: str = "GET", post_data: str | None = None
    ) -> Request:
        request = Request(url, method, post_data)
        for handler in self._event_handlers.get("request", []):
            handler(request)
        return request

    def emit_response(
        self,
        url: str,
        status: int = 200,
        text: str = "",
        method: str = "GET",
        post_data: str | None = None,
    ) -> Response:
        response = Response(Request(url, method, post_data), status, text)
        for handler in self._event_handlers.get("response", []):
            handler(response)
        return response

    def expect_file_chooser(self, **kw: Any):
        self.calls.append(("page", "expect_file_chooser", (), kw))
        return _ExpectFileChooserCM(self)


class _ExpectFileChooserCM:
    """``with page.expect_file_chooser() as fc: ...; fc.value.set_files(p)``."""

    def __init__(self, page: Page):
        self._page = page
        self.info = _FileChooserInfo(page)

    def __enter__(self) -> _FileChooserInfo:
        return self.info

    def __exit__(self, *exc: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# BrowserContext, Browser, BrowserType, Playwright
# ---------------------------------------------------------------------------


class BrowserContext:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.pages: list[Page] = []
        self.cookies: list[dict] | None = None
        self.routes: list[tuple[str, Any]] = []
        self.closed = False
        self.new_page_result: Page | None = None
        self._event_handlers: dict[str, list] = {}

    def new_page(self) -> Page:
        page = self.new_page_result or Page()
        self.new_page_result = page
        self.pages.append(page)
        self.calls.append(("context", "new_page", (), {}))
        return page

    def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies = cookies
        self.calls.append(("context", "add_cookies", (cookies,), {}))

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))
        self.calls.append(("context", "route", (pattern,), {}))

    def on(self, event: str, handler: Any) -> None:
        self._event_handlers.setdefault(event, []).append(handler)
        self.calls.append(("context", "on", (event,), {}))

    def close(self) -> None:
        self.closed = True
        self.calls.append(("context", "close", (), {}))

    def trigger_route(
        self, url: str, method: str = "GET", post_data: str | None = None
    ) -> Route:
        """Invoke the first registered handler whose pattern matches ``url``."""
        request = Request(url, method, post_data)
        route = Route(request)
        for pattern, handler in self.routes:
            if _glob_match(pattern, url):
                handler(route)
                break
        return route


class Browser:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self.new_context_result: BrowserContext | None = None

    def new_context(self, **kw: Any) -> BrowserContext:
        self.calls.append(("browser", "new_context", (), kw))
        ctx = self.new_context_result or BrowserContext()
        self.new_context_result = ctx
        return ctx

    def close(self) -> None:
        self.calls.append(("browser", "close", (), {}))
        self.closed = True


class BrowserType:
    """``pw.chromium`` (or ``.firefox`` / ``.webkit``, shaped the same)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.launch_calls: list[dict] = []
        self.launch_persistent_context_calls: list[tuple[str, dict]] = []
        self.launch_result: Browser | None = None
        self.launch_persistent_context_result: BrowserContext | None = None
        self.launch_raises: BaseException | None = None
        self.launch_persistent_context_raises: BaseException | None = None

    def launch(self, **kw: Any) -> Browser:
        self.launch_calls.append(kw)
        if self.launch_raises is not None:
            raise self.launch_raises
        self.launch_result = self.launch_result or Browser()
        return self.launch_result

    def launch_persistent_context(
        self, user_data_dir: str, **kw: Any
    ) -> BrowserContext:
        self.launch_persistent_context_calls.append((user_data_dir, kw))
        if self.launch_persistent_context_raises is not None:
            raise self.launch_persistent_context_raises
        self.launch_persistent_context_result = (
            self.launch_persistent_context_result or BrowserContext()
        )
        return self.launch_persistent_context_result


class Playwright:
    """The object ``with sync_playwright() as pw:`` yields."""

    def __init__(self) -> None:
        self.chromium = BrowserType("chromium")
        self.firefox = BrowserType("firefox")
        self.webkit = BrowserType("webkit")
        self.stopped = False


class _SyncPlaywrightContextManager:
    def __init__(self, driver: Playwright):
        self._driver = driver

    def __enter__(self) -> Playwright:
        return self._driver

    def __exit__(self, *exc: Any) -> bool:
        self._driver.stopped = True
        return False


# ---------------------------------------------------------------------------
# Installing the fake as ``playwright.sync_api``
# ---------------------------------------------------------------------------


def install(monkeypatch: Any) -> Playwright:
    """Install a fake ``playwright`` / ``playwright.sync_api`` in sys.modules.

    Returns the ``Playwright`` driver that ``sync_playwright()`` will yield
    (every call returns the same driver), so a test can pre-configure
    ``driver.chromium`` before the code under test triggers the lazy
    ``from playwright.sync_api import sync_playwright`` / ``TimeoutError``.
    Restored automatically by ``monkeypatch`` at the end of the test.
    """
    driver = Playwright()

    def _sync_playwright() -> _SyncPlaywrightContextManager:
        return _SyncPlaywrightContextManager(driver)

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _sync_playwright  # type: ignore[attr-defined]
    sync_api.TimeoutError = PlaywrightTimeoutError  # type: ignore[attr-defined]
    sync_api.Error = PlaywrightError  # type: ignore[attr-defined]

    playwright_pkg = types.ModuleType("playwright")
    playwright_pkg.sync_api = sync_api  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "playwright", playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return driver
