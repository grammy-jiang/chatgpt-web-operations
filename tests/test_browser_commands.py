"""Tests for the two commands that drive a real browser page directly:
``discover_endpoints.py`` and ``measure_window.py``.

Nothing here touches a real browser, Xvfb, the GNOME keyring, a real Chrome
profile, or the network. ``fake_playwright.install()`` puts a fake
``playwright`` / ``playwright.sync_api`` module pair in ``sys.modules``
before ``discover_endpoints.py``'s lazy ``from playwright.sync_api import
sync_playwright`` import runs; ``cc._helpers``, ``cc.browser_slot``,
``cc.virtual_display`` and the module-level ``chatgpt_cookies`` import are
replaced with small fakes the same way ``tests/test_client_browser.py``
does for ``BrowserSender`` (its ``enter_env`` fixture). ``measure_window.py``
never reaches a page at all, so its ``cc.BrowserSender`` is replaced outright
by a small dedicated fake (see ``FakeBrowserSender``) instead.

``create_project.py`` moved to plain HTTP on 2026-09-20 (the request body
was captured on a throwaway project) and no longer drives a browser; its
tests live in ``tests/test_create_project.py`` now, over a fake session
rather than a fake page.

Every test names the failure it is defending against, the same rule
``tests/test_commands.py`` uses.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import discover_endpoints  # noqa: E402
import fake_playwright as fp  # noqa: E402
import measure_window  # noqa: E402

# ---------------------------------------------------------------------------
# shared plumbing for discover_endpoints.py
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _noop_cm(*_a: object, **_kw: object):
    """Stands in for cc.browser_slot() / cc.virtual_display(visible)."""
    yield


def _install_client_fakes(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Wire ``module.load_client()`` to the real ``chatgpt_client``, with its
    browser plumbing, cookie helper and the module-level ``chatgpt_cookies``
    import all faked -- the same pattern ``tests/test_client_browser.py``
    uses for ``BrowserSender``, reused here so ``discover_endpoints.py``
    never touches a real browser slot, Xvfb, or the Chrome cookie database.
    """
    monkeypatch.setattr(module, "load_client", lambda: cc)
    monkeypatch.setattr(cc, "browser_slot", _noop_cm)
    monkeypatch.setattr(cc, "virtual_display", _noop_cm)
    monkeypatch.setattr(
        cc,
        "_helpers",
        lambda: types.SimpleNamespace(pick_browser=lambda choice: "chrome"),
    )

    cookies_mod = types.ModuleType("chatgpt_cookies")
    cookies_mod._rp_tolerant = True  # short-circuits cc._patch_cookie_export
    cookies_mod.export = lambda browser: [
        {"name": "session", "value": "tok", "domain": "chatgpt.com", "path": "/"}
    ]
    monkeypatch.setitem(sys.modules, "chatgpt_cookies", cookies_mod)


def _wire_page(driver: fp.Playwright) -> tuple[fp.Browser, fp.BrowserContext, fp.Page]:
    """Pre-seed the driver so ``pw.chromium.launch()`` etc. all return one
    known browser/context/page trio a test can configure beforehand."""
    browser = fp.Browser()
    driver.chromium.launch_result = browser
    ctx = fp.BrowserContext()
    browser.new_context_result = ctx
    page = fp.Page()
    ctx.new_page_result = page
    return browser, ctx, page


@pytest.fixture
def de_page(monkeypatch: pytest.MonkeyPatch):
    """A wired fake browser/context/page for ``discover_endpoints.py``'s
    ``main()``, with the client plumbing faked."""
    driver = fp.install(monkeypatch)
    _install_client_fakes(monkeypatch, discover_endpoints)
    browser, ctx, page = _wire_page(driver)
    return types.SimpleNamespace(driver=driver, browser=browser, ctx=ctx, page=page)


def _sleep_that_emits(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    page: fp.Page,
    events: list[tuple[str, str, str | None]],
) -> list[int]:
    """Make ``discover_endpoints.py``'s ``time.sleep(args.seconds)`` (the
    watch window) also fire the given requests, since request handlers are
    already registered by the time it is called. Each event is
    ``(url, method, post_data)``. Returns the list of recorded sleep
    durations, so a test can also check ``--seconds`` was honoured.
    """
    calls: list[int] = []

    def fake_sleep(seconds: int) -> None:
        calls.append(seconds)
        for url, method, post_data in events:
            page.emit_request(url, method=method, post_data=post_data)

    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    return calls


# ---------------------------------------------------------------------------
# discover_endpoints.py — summarise() itself
# ---------------------------------------------------------------------------


def test_summarise_strips_query_strings_so_paging_collapses_to_one_row() -> None:
    """Paged listing calls must count as one endpoint, not one per page."""
    rows = discover_endpoints.summarise(
        [
            ("GET", "https://chatgpt.com/backend-api/conversations?offset=0&limit=28"),
            ("GET", "https://chatgpt.com/backend-api/conversations?offset=28&limit=28"),
        ]
    )
    assert rows == [("GET", "https://chatgpt.com/backend-api/conversations", "2")]


def test_summarise_collapses_uuids_so_traffic_does_not_swamp_the_report() -> None:
    """One conversation's traffic must not read as many distinct endpoints."""
    rows = discover_endpoints.summarise(
        [
            (
                "GET",
                "https://chatgpt.com/backend-api/conversation/"
                "6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8",
            ),
            (
                "GET",
                "https://chatgpt.com/backend-api/conversation/"
                "6aaa139a-b7fc-83ec-841b-14931c7f7f56",
            ),
        ]
    )
    assert rows == [("GET", "https://chatgpt.com/backend-api/conversation/<id>", "2")]


def test_summarise_collapses_long_hex_hashes_too() -> None:
    """A content hash in an asset path must collapse the same way an id does."""
    long_hash = "a" * 40
    rows = discover_endpoints.summarise(
        [("GET", f"https://chatgpt.com/assets/{long_hash}.js")]
    )
    assert rows == [("GET", "https://chatgpt.com/assets/<hash>.js", "1")]


def test_summarise_orders_the_busiest_endpoint_first() -> None:
    """The report is read by people; the noisiest endpoint belongs on top."""
    rows = discover_endpoints.summarise(
        [("GET", "https://a/x")] * 3 + [("POST", "https://a/y")]
    )
    assert rows[0] == ("GET", "https://a/x", "3")
    assert rows[1] == ("POST", "https://a/y", "1")


def test_summarise_keeps_get_and_patch_of_the_same_path_distinct() -> None:
    """A read and a write to the same path are different endpoints."""
    rows = discover_endpoints.summarise(
        [("GET", "https://a/conversation/x"), ("PATCH", "https://a/conversation/x")]
    )
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# discover_endpoints.py — main()
# ---------------------------------------------------------------------------


def test_only_requests_matching_match_are_counted(monkeypatch, de_page, capsys) -> None:
    """The default --match must keep an unrelated static-asset request out
    of both the table and the total, or the report drowns in noise."""
    page = de_page.page
    _sleep_that_emits(
        monkeypatch,
        discover_endpoints,
        page,
        [
            ("https://chatgpt.com/backend-api/conversations", "GET", None),
            ("https://chatgpt.com/static/app.js", "GET", None),
        ],
    )

    assert discover_endpoints.main([]) == 0

    out = capsys.readouterr().out
    assert "1 matching request(s)" in out
    assert "backend-api/conversations" in out
    assert "static/app.js" not in out


def test_a_custom_match_narrows_the_capture_further(
    monkeypatch, de_page, capsys
) -> None:
    """--match is a substring over the whole URL; a narrower value than the
    default must exclude other backend-api calls too."""
    page = de_page.page
    _sleep_that_emits(
        monkeypatch,
        discover_endpoints,
        page,
        [
            ("https://chatgpt.com/backend-api/gizmos/snorlax", "GET", None),
            ("https://chatgpt.com/backend-api/conversations", "GET", None),
        ],
    )

    assert discover_endpoints.main(["--match", "gizmos"]) == 0

    out = capsys.readouterr().out
    assert "1 matching request(s)" in out
    assert "gizmos" in out
    assert "conversations" not in out


def test_the_url_argument_controls_where_the_page_navigates(
    monkeypatch, de_page, capsys
) -> None:
    """--url must reach page.goto verbatim; watching the wrong page silently
    produces an empty, misleading capture."""
    page = de_page.page
    _sleep_that_emits(monkeypatch, discover_endpoints, page, [])

    target = "https://chatgpt.com/g/g-p-abc123/project"
    assert discover_endpoints.main(["--url", target]) == 0

    assert page.last_goto_url == target
    assert target in capsys.readouterr().out


def test_globals_dumps_window_keys_matching_the_pattern(
    monkeypatch, de_page, capsys
) -> None:
    """--globals is how the __oai_so_* behavioural collector was found; a
    scripted page.evaluate() result must be summarised and listed."""
    page = de_page.page
    _sleep_that_emits(monkeypatch, discover_endpoints, page, [])
    page.set_evaluate_result("Object.keys(window)", ["__oai_so_a", "__oai_so_b"])

    assert discover_endpoints.main(["--globals", "__oai_so_"]) == 0

    out = capsys.readouterr().out
    assert "window keys matching '__oai_so_': 2" in out
    assert "__oai_so_a" in out
    assert "__oai_so_b" in out


def test_without_globals_no_window_key_section_is_printed(
    monkeypatch, de_page, capsys
) -> None:
    """An empty --globals (the default) must skip page.evaluate entirely,
    not print a spurious 'matching 0 keys' section."""
    page = de_page.page
    _sleep_that_emits(monkeypatch, discover_endpoints, page, [])

    assert discover_endpoints.main([]) == 0

    out = capsys.readouterr().out
    assert "window keys matching" not in out
    assert not any(c[0] == "page" and c[1] == "evaluate" for c in page.calls)


def test_bodies_prints_post_and_patch_payloads_but_never_get(
    monkeypatch, de_page, capsys
) -> None:
    """--bodies is what you need to reproduce a mutation; a GET must never
    appear there even though it matches --match, and a request with no
    payload must be skipped rather than printed as blank evidence."""
    page = de_page.page
    _sleep_that_emits(
        monkeypatch,
        discover_endpoints,
        page,
        [
            ("https://chatgpt.com/backend-api/gizmos", "POST", '{"name": "a"}'),
            (
                "https://chatgpt.com/backend-api/projects/1",
                "PATCH",
                '{"memory_scope": "global"}',
            ),
            ("https://chatgpt.com/backend-api/conversations", "GET", None),
            ("https://chatgpt.com/backend-api/gizmos", "POST", None),
        ],
    )

    assert discover_endpoints.main(["--bodies"]) == 0

    out = capsys.readouterr().out
    assert "2 request(s) carried a body:" in out
    assert "POST https://chatgpt.com/backend-api/gizmos" in out
    assert '{"name": "a"}' in out
    assert "PATCH https://chatgpt.com/backend-api/projects/1" in out
    assert '{"memory_scope": "global"}' in out


def test_bodies_flag_off_never_prints_the_body_section(
    monkeypatch, de_page, capsys
) -> None:
    """Without --bodies, a POST payload must never leak into the report --
    it is opt-in evidence, not the default."""
    page = de_page.page
    _sleep_that_emits(
        monkeypatch,
        discover_endpoints,
        page,
        [("https://chatgpt.com/backend-api/gizmos", "POST", '{"name": "a"}')],
    )

    assert discover_endpoints.main([]) == 0

    out = capsys.readouterr().out
    assert "carried a body" not in out
    assert '{"name": "a"}' not in out


def test_an_empty_capture_still_prints_a_clean_report(
    monkeypatch, de_page, capsys
) -> None:
    """Nothing observed must say so plainly (the table's own "(nothing)"),
    not print an empty table or crash summarising zero rows."""
    page = de_page.page
    sleep_calls = _sleep_that_emits(monkeypatch, discover_endpoints, page, [])

    assert discover_endpoints.main([]) == 0

    out = capsys.readouterr().out
    assert "(nothing)" in out
    assert "0 matching request(s)" in out
    assert sleep_calls == [20]  # the default --seconds


# ---------------------------------------------------------------------------
# measure_window.py — browser_rss_mb()
# ---------------------------------------------------------------------------


def _ps_result(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ps", "-eo", "rss=,args="], returncode=0, stdout=stdout, stderr=""
    )


def test_browser_rss_mb_sums_only_chrome_and_xvfb_processes(monkeypatch) -> None:
    """An unrelated process's RSS must never inflate the browser budget."""
    stdout = "\n".join(
        [
            "102400 /usr/lib/chrome/chrome --type=renderer --field-trial-handle=1",
            " 20480 Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp",
            "  5120 /usr/bin/bash -c sleep 10",
        ]
    )
    monkeypatch.setattr(
        measure_window.subprocess, "run", lambda *a, **k: _ps_result(stdout)
    )

    assert measure_window.browser_rss_mb() == pytest.approx((102400 + 20480) / 1024)


def test_browser_rss_mb_ignores_blank_and_single_token_lines(monkeypatch) -> None:
    """A blank trailing line or a line with no args field (ps output is not
    guaranteed tidy) must not crash the budget check."""
    stdout = "102400 /usr/lib/chrome/chrome --type=renderer\n\nzombie\n"
    monkeypatch.setattr(
        measure_window.subprocess, "run", lambda *a, **k: _ps_result(stdout)
    )

    assert measure_window.browser_rss_mb() == pytest.approx(102400 / 1024)


def test_browser_rss_mb_is_zero_when_nothing_matches(monkeypatch) -> None:
    """No chrome or Xvfb process running must report 0, not crash on an
    empty ps listing."""
    monkeypatch.setattr(
        measure_window.subprocess, "run", lambda *a, **k: _ps_result("")
    )

    assert measure_window.browser_rss_mb() == 0.0


# ---------------------------------------------------------------------------
# measure_window.py — main()
# ---------------------------------------------------------------------------


class FakeBrowserSender:
    """Stands in for ``cc.BrowserSender``, exposing ONLY the public surface
    measure_window.py may use: the context-manager protocol,
    ``probe_composer`` and ``fill_composer``.

    Driving the real class would need the full fake Playwright stack for
    behaviour this command never touches (the real ``__enter__`` opens a
    browser slot, starts a watchdog thread, and launches Chrome), so a small
    dedicated fake is used instead. Until 2026-09-20 this fake also offered
    the private ``_composer``, ``_focus_composer`` and ``_owner``, which let
    measure_window.py pass here while filling a page it had never
    navigated to (references/failure-atlas.md);
    ``test_the_fake_sender_offers_nothing_the_real_sender_does_not`` keeps
    it honest now.
    """

    instances: list[FakeBrowserSender] = []
    ready_seconds = 1.0
    fill_seconds = 1.0

    def __init__(
        self, browser: str = "chrome", visible: bool = False, **kw: object
    ) -> None:
        self.browser = browser
        self.visible = visible
        self.probes = 0
        self.fill_calls: list[str] = []
        FakeBrowserSender.instances.append(self)

    def __enter__(self) -> FakeBrowserSender:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def probe_composer(self) -> str:
        self.probes += 1
        return "https://chatgpt.com/"

    def fill_composer(self, text: str) -> tuple[float, float]:
        self.fill_calls.append(text)
        return (self.ready_seconds, self.fill_seconds)


class _FakeThread:
    """Stands in for ``threading.Thread`` so the watcher never really runs.

    Patching ``threading.Event`` instead (to make ``stop.wait(0.4)`` return
    at once) looked simpler, but ``Thread.__init__`` itself creates a real
    ``Event`` for its own bookkeeping (``self._started``), so faking the
    class globally breaks every thread in the process, including this one
    before it can even start. Replacing ``Thread`` outright avoids that: its
    target (the watcher loop, reading the stubbed ``browser_rss_mb()``) adds
    nothing a test needs to see, so ``start()``/``join()`` are no-ops.
    """

    def __init__(self, *a: object, **kw: object) -> None:
        pass

    def start(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass


def _install_fake_clock(monkeypatch: pytest.MonkeyPatch, module: object) -> list[float]:
    """A deterministic, strictly increasing ``time.monotonic()`` and a
    ``time.sleep()`` that never really sleeps, so ``main()``'s elapsed-time
    math is exact instead of depending on real wall-clock scheduling."""
    from itertools import count

    ticks = count(0.0, 1.0)
    sleep_calls: list[float] = []
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


BASE_MB = 100.0


@pytest.fixture
def fake_sender(monkeypatch: pytest.MonkeyPatch) -> type[FakeBrowserSender]:
    """``cc.BrowserSender`` replaced with ``FakeBrowserSender`` (see its own
    docstring for why); ``measure_window.load_client`` wired to the real
    ``chatgpt_client`` module so the substitution is visible to it."""
    FakeBrowserSender.instances.clear()
    monkeypatch.setattr(measure_window, "load_client", lambda: cc)
    monkeypatch.setattr(cc, "BrowserSender", FakeBrowserSender)
    return FakeBrowserSender


def test_main_without_fill_file_reports_baseline_and_idles_three_seconds(
    monkeypatch, fake_sender, capsys
) -> None:
    """No --fill-file must skip the fill entirely and just idle for 3 s, so
    a bare ``measure_window.py`` still reports a clean baseline/peak pair
    and never calls composer.fill()."""
    monkeypatch.setattr(measure_window.threading, "Thread", _FakeThread)
    sleep_calls = _install_fake_clock(monkeypatch, measure_window)
    monkeypatch.setattr(measure_window, "browser_rss_mb", lambda: BASE_MB)

    assert measure_window.main(["--visible"]) == 0

    out = capsys.readouterr().out
    assert f"baseline           {BASE_MB:8.1f} MB" in out
    assert f"peak               {BASE_MB:8.1f} MB   (+0.0)" in out
    # composer ready in: launch (tick 1) plus the page load probe (tick 2).
    # Before 2026-09-20 this read 1.0, the window alone -- no page was loaded.
    assert f"{2.0:8.1f} s" in out
    assert "filled" not in out
    assert sleep_calls == [3]
    sender = fake_sender.instances[-1]
    assert sender.visible is True
    # the idle number is a loaded composer's, not a blank window's
    assert sender.probes == 1
    assert sender.fill_calls == []


def test_main_with_fill_file_measures_the_fill_and_prints_the_rate(
    monkeypatch, fake_sender, capsys, tmp_path: Path
) -> None:
    """--fill-file is the honest test: it must drive the real fill path,
    focus the composer first, and report chars/seconds/rate against the
    6 s/kB budget instead of silently taking the idle branch."""
    monkeypatch.setattr(measure_window.threading, "Thread", _FakeThread)
    sleep_calls = _install_fake_clock(monkeypatch, measure_window)
    monkeypatch.setattr(measure_window, "browser_rss_mb", lambda: BASE_MB)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("x" * 1000, encoding="utf-8")

    assert measure_window.main(["--fill-file", str(prompt)]) == 0

    out = capsys.readouterr().out
    assert f"baseline           {BASE_MB:8.1f} MB" in out
    assert "filled 1,000 chars in 1.0 s  (1.00 s/kB)" in out
    assert "A send would give this fill 120 s; compare against that." in out
    assert f"{2.0:8.1f} s" in out  # composer ready in: launch 1.0 + ready 1.0
    assert sleep_calls == []  # the fill path never takes the idle nap
    sender = fake_sender.instances[-1]
    assert sender.fill_calls == ["x" * 1000]
    assert sender.probes == 0  # fill_composer loads the page itself; no second load


def test_the_fake_sender_offers_nothing_the_real_sender_does_not() -> None:
    """Every public name FakeBrowserSender offers must be a public method of
    the real BrowserSender, and it may offer no private one: a fake kinder
    than the real object is how measure_window.py passed for as long as it
    was broken (references/failure-atlas.md)."""
    names = sorted(n for n in vars(FakeBrowserSender) if not n.startswith("__"))
    assert names == [
        "fill_composer",
        "fill_seconds",
        "instances",
        "probe_composer",
        "ready_seconds",
    ]
    for name in ("fill_composer", "probe_composer"):
        assert callable(getattr(cc.BrowserSender, name, None)), name
