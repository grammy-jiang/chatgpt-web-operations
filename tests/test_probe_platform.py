"""probe_platform.py (SKILL.md, "Is the dedicated window still logged in to
platform.openai.com?"): the classification of a settled tab, the verdict
mapping the cron wrappers rely on (never more than a warn), the check.log
fields, the cookie-metadata reader (names and expiry only, from a copy; no
value column is ever read), the settle loop, the tab's open and close over
the debugging endpoint, and the command's main -- all over fakes: no Chrome,
no port, no real cookie DB."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import signal
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chatgpt_client as cc  # noqa: E402
import probe_platform as pp  # noqa: E402

REAL_PROBE = pp.probe  # main's tests replace pp.probe; the helpers keep this

WEBKIT_EPOCH_DELTA_S = 11_644_473_600
NOW = 1_791_750_000.0  # 2026-10-12, a fixed "now" for the expiry arithmetic
SECRET = "SECRET-COOKIE-VALUE-never-printed"


def webkit(days_from_now: float) -> int:
    """``expires_utc`` (microseconds since 1601) for a cookie that expires
    ``days_from_now`` after NOW."""
    return int((NOW + days_from_now * 86400 + WEBKIT_EPOCH_DELTA_S) * 1_000_000)


# ---------------------------------------------------------------------------
# classify: one fixture per state, from the shapes measured on 2026-10-11/12
# ---------------------------------------------------------------------------

CASES = [
    # VALID: the Platform app, not /login, not auth.openai.com
    ("https://platform.openai.com/", "OpenAI Platform", "VALID"),
    ("https://platform.openai.com/home", "Home - OpenAI API", "VALID"),
    ("https://platform.openai.com/chat/edit?models=x", "Playground", "VALID"),
    ("https://platform.openai.com/docs/guides/otpx", "Docs", "VALID"),
    # LOGIN_REQUIRED: /login (the account chooser) or the auth.openai.com log-in
    ("https://platform.openai.com/login", "OpenAI Platform", "LOGIN_REQUIRED"),
    ("https://platform.openai.com/login?next=%2Fhome", "", "LOGIN_REQUIRED"),
    ("https://platform.openai.com/signup", "", "LOGIN_REQUIRED"),
    ("https://auth.openai.com/log-in", "Log in - OpenAI", "LOGIN_REQUIRED"),
    ("https://auth.openai.com/log-in-or-create-account", "", "LOGIN_REQUIRED"),
    ("https://auth.openai.com/choose-account?x=1", "", "LOGIN_REQUIRED"),
    ("https://platform.openai.com/docs", "Log in | OpenAI", "LOGIN_REQUIRED"),
    # MFA_REQUIRED: the second factor, or any verify page
    (
        "https://auth.openai.com/mfa-challenge?state=abc",
        "Check your authenticator app",
        "MFA_REQUIRED",
    ),
    ("https://auth.openai.com/mfa-challenge/totp", "", "MFA_REQUIRED"),
    ("https://auth.openai.com/email-verification", "", "MFA_REQUIRED"),
    ("https://auth.openai.com/u/mfa-otp-challenge", "", "MFA_REQUIRED"),
    ("https://auth.openai.com/log-in", "Check your authenticator app", "MFA_REQUIRED"),
    ("https://platform.openai.com/verify", "", "MFA_REQUIRED"),
    # ERROR: a load failure, a challenge, a callback that never finished,
    # another site
    ("chrome-error://chromewebdata/", "platform.openai.com", "ERROR"),
    ("about:blank", "", "ERROR"),
    ("", "", "ERROR"),
    ("https://platform.openai.com/", "Just a moment...", "ERROR"),
    ("https://auth.openai.com/log-in", "Verify you are human", "ERROR"),
    ("https://platform.openai.com/auth/callback?code=x", "", "ERROR"),
    ("https://openai.com/api/", "OpenAI", "ERROR"),
]


@pytest.mark.parametrize(("url", "title", "state"), CASES)
def test_classify_names_the_state_of_each_page_shape(url, title, state) -> None:
    got, reason = pp.classify(url, title)
    assert got == state, reason
    assert reason
    assert "?" not in reason  # a reason never carries the query string


def test_every_state_has_a_fixture() -> None:
    assert {state for _u, _t, state in CASES} == set(pp.STATES)


@pytest.mark.parametrize(
    ("url", "short"),
    [
        ("https://platform.openai.com/login?next=%2F#x", "platform.openai.com/login"),
        (
            "https://Auth.OpenAI.com/mfa-challenge?state=s",
            "auth.openai.com/mfa-challenge",
        ),
        ("https://platform.openai.com", "platform.openai.com/"),
        ("chrome-error://chromewebdata/", "chrome-error://chromewebdata/"),
        ("about:blank", "about:blank"),
        ("", ""),
    ],
)
def test_short_url_keeps_host_and_path_only(url, short) -> None:
    assert pp.short_url(url) == short


# ---------------------------------------------------------------------------
# verdict and the check.log fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", pp.STATES)
@pytest.mark.parametrize("days", [None, -3, 0, 5, 6, 29, 399])
def test_a_platform_problem_is_never_more_than_a_warn(state, days) -> None:
    severity, finding = pp.verdict(state, days, "detail")
    assert severity in ("ok", "warn")
    # the daily wrapper hands a WARN to the repair loop only when a finding
    # says a page "differs from the fixture"; a Platform finding never does
    assert "differs from the fixture" not in finding
    assert (severity == "ok") == (finding == "")


def test_valid_with_time_left_is_ok() -> None:
    assert pp.verdict("VALID", 29) == ("ok", "")
    assert pp.verdict("VALID", None) == ("ok", "")
    assert pp.verdict("VALID", 6) == ("ok", "")


@pytest.mark.parametrize("days", [5, 1, 0, -2])
def test_valid_close_to_expiry_warns_with_the_owner_action(days) -> None:
    severity, finding = pp.verdict("VALID", days)
    assert severity == "warn"
    assert f"expire in {days} d" in finding
    assert pp.OWNER_ACTION in finding


@pytest.mark.parametrize("state", ["LOGIN_REQUIRED", "MFA_REQUIRED"])
def test_login_and_mfa_warn_naming_the_state_and_the_owner_action(state) -> None:
    severity, finding = pp.verdict(state, 29)
    assert severity == "warn"
    assert finding == f"platform.openai.com: {state}; {pp.OWNER_ACTION}"
    assert pp.OWNER_ACTION == (
        "owner action: log in to platform.openai.com in the dedicated Chrome (MFA)"
    )
    _sev, soon = pp.verdict(state, 3)
    assert "expire in 3 d" in soon


def test_error_warns_with_its_reason() -> None:
    severity, finding = pp.verdict("ERROR", 29, "browser slot busy")
    assert severity == "warn"
    assert finding == "platform.openai.com probe: ERROR (browser slot busy)"
    assert pp.verdict("ERROR", None)[1] == "platform.openai.com probe: ERROR"


def test_an_unknown_state_is_treated_as_an_error() -> None:
    assert pp.verdict("SOMETHING", 29)[0] == "warn"
    assert pp.log_fields("SOMETHING", 29) == "platform=ERROR platform_auth_days=29"


@pytest.mark.parametrize(
    ("state", "days", "fields"),
    [
        ("VALID", 29, "platform=VALID platform_auth_days=29"),
        ("MFA_REQUIRED", 0, "platform=MFA_REQUIRED platform_auth_days=0"),
        ("LOGIN_REQUIRED", -1, "platform=LOGIN_REQUIRED platform_auth_days=-1"),
        ("ERROR", None, "platform=ERROR platform_auth_days=-"),
    ],
)
def test_log_fields_are_two_space_free_key_value_tokens(state, days, fields) -> None:
    text = pp.log_fields(state, days)
    assert text == fields
    tokens = text.split(" ")
    assert [t.split("=", 1)[0] for t in tokens] == ["platform", "platform_auth_days"]


# ---------------------------------------------------------------------------
# the cookie-metadata reader: a copy, three columns, never a value
# ---------------------------------------------------------------------------


def make_cookie_db(path: Path, rows: list[tuple[str, str, int]]) -> Path:
    """A Chrome-shaped ``cookies`` table with a value in every row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE cookies (creation_utc INTEGER, host_key TEXT, name TEXT, "
        "value TEXT, encrypted_value BLOB, path TEXT, expires_utc INTEGER)"
    )
    con.executemany(
        "INSERT INTO cookies VALUES (0, ?, ?, ?, ?, '/', ?)",
        [(h, n, SECRET, SECRET.encode(), e) for h, n, e in rows],
    )
    con.commit()
    con.close()
    return path


ROWS = [
    (".auth.openai.com", "oai-client-auth-info", webkit(29.6)),
    (".auth.openai.com", "unified_session_manifest", webkit(400.0)),
    ("auth.openai.com", "__Host-unified_session_manifest", webkit(399.5)),
    (".auth.openai.com", "cf_clearance", webkit(365.0)),
    (".chatgpt.com", "__Secure-next-auth.session-token", webkit(90.0)),
    (".platform.openai.com", "oai-client-auth-info", webkit(1.0)),
]


def spying_connect(reads: list[tuple[str, str]], paths: list[str]) -> Any:
    """``sqlite3.connect`` with an authorizer that records every column read
    and refuses the two value columns outright."""

    def connect(path: str) -> sqlite3.Connection:
        paths.append(path)
        con = sqlite3.connect(path)

        def authorizer(action, arg1, arg2, _db, _source):
            if action == sqlite3.SQLITE_READ:
                reads.append((arg1, arg2))
                if arg2 in ("value", "encrypted_value"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)
        return con

    return connect


def test_the_reader_reads_names_and_expiry_from_a_copy_and_never_a_value(
    tmp_path: Path,
) -> None:
    db = make_cookie_db(tmp_path / "profile" / "Default" / "Cookies", ROWS)
    before = db.read_bytes()
    reads: list[tuple[str, str]] = []
    paths: list[str] = []
    rows = pp.read_auth_cookies(db, connect=spying_connect(reads, paths))
    assert reads, "the authorizer saw no read at all"
    assert {column for _table, column in reads} <= {"host_key", "name", "expires_utc"}
    assert paths and paths[0] != str(db)  # a copy, never the live file
    assert not Path(paths[0]).exists()  # and the copy is gone afterwards
    assert db.read_bytes() == before
    assert sorted(name for _h, name, _e in rows) == [
        "__Host-unified_session_manifest",
        "oai-client-auth-info",
        "unified_session_manifest",
    ]
    assert all(host.endswith("auth.openai.com") for host, _n, _e in rows)
    assert SECRET not in repr(rows)


def test_the_cookie_query_names_no_value_column() -> None:
    query = pp.COOKIE_QUERY.lower()
    assert "value" not in query
    assert "*" not in query


def test_auth_days_takes_the_earliest_row_per_key_and_skips_session_cookies() -> None:
    rows = [
        (".auth.openai.com", "oai-client-auth-info", webkit(29.6)),
        (".auth.openai.com", "unified_session_manifest", webkit(400.0)),
        ("auth.openai.com", "__Host-unified_session_manifest", webkit(399.5)),
        ("auth.openai.com", "oai-client-auth-info", 0),  # session-scoped
        ("auth.openai.com", "something-else", webkit(1.0)),
    ]
    days = pp.auth_days(rows, NOW)
    assert days == {"oai_client_auth_info": 29.6, "unified_session": 399.5}
    assert pp.soonest_days(days) == 29


def test_soonest_days_rounds_down_and_knows_nothing_without_a_cookie() -> None:
    assert pp.soonest_days({"a": 4.9, "b": 400.0}) == 4
    assert pp.soonest_days({"a": -0.2, "b": None}) == -1
    assert pp.soonest_days({"a": None, "b": None}) is None
    assert pp.auth_days([], NOW) == {
        "oai_client_auth_info": None,
        "unified_session": None,
    }


def test_cookie_db_is_the_profile_s_default_jar(tmp_path: Path) -> None:
    assert pp.cookie_db(tmp_path) == tmp_path / "Default" / "Cookies"


# ---------------------------------------------------------------------------
# settle: the tab's address must stop changing
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def reader(urls: list[str | None]) -> Any:
    """One ``/json/list`` entry per poll; the last one repeats."""
    seq = list(urls)

    def read() -> dict[str, Any] | None:
        url = seq.pop(0) if len(seq) > 1 else seq[0]
        return None if url is None else {"url": url, "title": "t"}

    return read


def test_settle_waits_out_the_redirect_and_returns_the_final_address() -> None:
    clock = Clock()
    urls = ["about:blank", "https://platform.openai.com/"] * 1 + [
        "https://platform.openai.com/"
    ] * 3
    urls += ["https://auth.openai.com/authorize"] * 2
    urls += ["https://platform.openai.com/login"]
    last, settled = pp.settle(reader(urls), clock=clock, sleep=clock.sleep)
    assert settled
    assert last["url"] == "https://platform.openai.com/login"
    assert clock.t >= pp.MIN_WAIT_SECONDS


def test_settle_never_settles_on_a_blank_tab() -> None:
    clock = Clock()
    last, settled = pp.settle(
        reader(["about:blank"]), timeout=20, clock=clock, sleep=clock.sleep
    )
    assert not settled
    assert last["url"] == "about:blank"
    assert clock.t >= 20


def test_settle_gives_up_on_an_address_that_keeps_changing() -> None:
    clock = Clock()
    counter = iter(range(10_000))

    def read() -> dict[str, Any]:
        return {"url": f"https://platform.openai.com/{next(counter)}"}

    last, settled = pp.settle(read, timeout=15, clock=clock, sleep=clock.sleep)
    assert not settled
    assert last is not None


def test_settle_reports_a_tab_that_went_away() -> None:
    clock = Clock()
    assert pp.settle(
        reader(["https://platform.openai.com/", None]), clock=clock, sleep=clock.sleep
    ) == (None, False)


# ---------------------------------------------------------------------------
# the tab over the debugging endpoint: PUT /json/new, GET /json/list and
# /json/close, nothing else
# ---------------------------------------------------------------------------


class FakeBrowser:
    """The three endpoints the probe may use; anything else fails the test."""

    def __init__(
        self,
        urls: list[str | None],
        title: str = "OpenAI Platform",
        new_body: Any = None,
        list_error: Exception | None = None,
        close_fails: int = 0,
    ) -> None:
        self.urls = list(urls)
        self.title = title
        self.new_body = {"id": "T1", "type": "page"} if new_body is None else new_body
        self.list_error = list_error
        self.close_fails = close_fails
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        method = request.get_method()
        path = request.full_url.removeprefix("http://127.0.0.1:9222")
        self.calls.append((method, path))
        if path.startswith("/json/new?"):
            assert method == "PUT"
            assert path == "/json/new?https://platform.openai.com/"
            body: Any = self.new_body
        elif path == "/json/list":
            assert method == "GET"
            if self.list_error is not None:
                raise self.list_error
            url = self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]
            body = [{"id": "OTHER", "url": "https://chatgpt.com/", "type": "page"}]
            if url is not None:
                body.append({"id": "T1", "url": url, "title": self.title})
        elif path.startswith("/json/close/"):
            assert method == "GET"
            if self.close_fails:
                self.close_fails -= 1
                raise OSError("missed")
            return contextlib.nullcontext(io.BytesIO(b"Target is closing"))
        else:  # pragma: no cover - the assertion is the point
            raise AssertionError(f"unexpected endpoint {method} {path}")
        return contextlib.nullcontext(io.BytesIO(json.dumps(body).encode()))


ENDPOINT = "http://127.0.0.1:9222"


def run_navigate(fake: FakeBrowser, timeout: float = 60) -> dict[str, Any]:
    clock = Clock()
    return pp.navigate(
        ENDPOINT, timeout=timeout, opener=fake, clock=clock, sleep=clock.sleep
    )


def test_navigate_opens_one_tab_classifies_and_closes_it() -> None:
    fake = FakeBrowser(["https://platform.openai.com/home?x=1"])
    result = run_navigate(fake)
    assert result == {
        "state": "VALID",
        "detail": "the Platform app loaded at platform.openai.com/home",
        "final_url": "platform.openai.com/home",
        "tab_closed": True,
    }
    paths = [p for _m, p in fake.calls]
    assert paths.count("/json/new?https://platform.openai.com/") == 1
    assert paths[-1] == "/json/close/T1"
    assert pp._open_tab == {}


def test_navigate_reports_mfa_and_retries_a_missed_close() -> None:
    fake = FakeBrowser(
        ["https://platform.openai.com/", "https://auth.openai.com/mfa-challenge"],
        title="Check your authenticator app",
        close_fails=1,
    )
    result = run_navigate(fake)
    assert result["state"] == "MFA_REQUIRED"
    assert result["final_url"] == "auth.openai.com/mfa-challenge"
    assert result["tab_closed"] is True
    assert [p for _m, p in fake.calls].count("/json/close/T1") == 2


def test_navigate_reports_a_timeout_and_a_vanished_tab_as_errors() -> None:
    clock_urls = [f"https://platform.openai.com/{i}" for i in range(200)]
    result = run_navigate(FakeBrowser(clock_urls), timeout=12)
    assert result["state"] == "ERROR"
    assert "did not settle within 12 s" in result["detail"]
    gone = run_navigate(FakeBrowser(["https://platform.openai.com/", None]))
    assert gone["state"] == "ERROR"
    assert gone["detail"] == "the tab went away before it settled"


def test_navigate_closes_the_tab_when_the_listing_fails() -> None:
    fake = FakeBrowser(["x"], list_error=OSError("refused"))
    with pytest.raises(OSError):
        run_navigate(fake)
    assert fake.calls[-1] == ("GET", "/json/close/T1")
    assert pp._open_tab == {}


def test_open_tab_refuses_an_answer_without_a_target_id() -> None:
    with pytest.raises(RuntimeError, match="no target id"):
        pp.open_tab(ENDPOINT, FakeBrowser(["x"], new_body={"type": "page"}))
    with pytest.raises(RuntimeError, match="no target id"):
        pp.open_tab(ENDPOINT, FakeBrowser(["x"], new_body=["not", "a", "dict"]))


def test_close_tab_gives_up_after_two_misses() -> None:
    fake = FakeBrowser(["x"], close_fails=2)
    assert pp.close_tab(ENDPOINT, "T1", fake) is False
    assert len(fake.calls) == 2


def test_read_target_survives_a_listing_that_is_not_a_list() -> None:
    fake = FakeBrowser(["x"])
    assert pp.read_target(ENDPOINT, "T1", fake)["url"] == "x"
    assert pp.read_target(ENDPOINT, "NOPE", fake) is None

    def text_opener(request: Any, timeout: float) -> Any:
        return contextlib.nullcontext(io.BytesIO(b"not json"))

    assert pp.read_target(ENDPOINT, "T1", text_opener) is None


def test_the_sigterm_handler_closes_the_open_tab_and_exits(monkeypatch) -> None:
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(pp, "close_tab", lambda e, t: closed.append((e, t)) or True)
    pp._open_tab.update(endpoint=ENDPOINT, target_id="T9")
    with pytest.raises(SystemExit) as exc:
        pp._close_on_sigterm(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM
    assert closed == [(ENDPOINT, "T9")]
    assert pp._open_tab == {}
    with pytest.raises(SystemExit):
        pp._close_on_sigterm(signal.SIGTERM, None)  # nothing open: just exits
    assert len(closed) == 1


def test_the_module_has_no_way_to_click_type_or_open_the_keys_page() -> None:
    """Read-only by construction: no browser automation library, no page
    script, no input event; the only address it opens is the Platform root."""
    source = (SCRIPTS / "probe_platform.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "playwright" not in imported
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"click", "fill", "type", "press", "goto", "evaluate"}
    assert "api-keys" not in source
    assert "Input." not in source and "Runtime.evaluate" not in source


# ---------------------------------------------------------------------------
# probe(): every failure is an ERROR document, never an exception
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def free_slot():
    yield 0


def fake_cookies(_db: Path) -> list[tuple[str, str, int]]:
    return [
        (".auth.openai.com", "oai-client-auth-info", webkit(29.6)),
        ("auth.openai.com", "__Host-unified_session_manifest", webkit(399.5)),
    ]


def run_probe(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "profile": Path("/nonexistent"),
        "slot": free_slot,
        "alive": lambda url: {"Browser": "Chrome/155"},
        "nav": lambda endpoint, timeout: {
            "state": "VALID",
            "detail": "the Platform app loaded at platform.openai.com/home",
            "final_url": "platform.openai.com/home",
            "tab_closed": True,
        },
        "read_cookies": fake_cookies,
        "now": lambda: NOW,
    }
    kwargs.update(overrides)
    return REAL_PROBE(**kwargs)


def test_probe_document_for_a_valid_session() -> None:
    doc = run_probe()
    assert doc["state"] == "VALID"
    assert doc["final_url"] == "platform.openai.com/home"
    assert doc["auth_days"] == {"oai_client_auth_info": 29.6, "unified_session": 399.5}
    assert doc["platform_auth_days"] == 29
    assert doc["severity"] == "ok" and doc["finding"] == ""
    assert doc["log_fields"] == "platform=VALID platform_auth_days=29"
    assert doc["checked_at"].endswith("+00:00")
    assert pp.exit_code(doc) == 0
    assert "cookie_error" not in doc


def test_probe_reports_a_missing_browser_without_opening_anything() -> None:
    def nav(*_a: Any, **_k: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("no tab without a browser")

    doc = run_probe(alive=lambda url: None, nav=nav)
    assert doc["state"] == "ERROR"
    assert "does not answer at http://127.0.0.1:9222" in doc["detail"]
    assert doc["severity"] == "warn"
    assert pp.exit_code(doc) == 1


def test_probe_reports_a_busy_slot_and_never_forces_it() -> None:
    @contextlib.contextmanager
    def busy():
        raise cc.TransportError("all 1 send window(s) are busy after 60 s")
        yield  # pragma: no cover

    doc = run_probe(slot=busy)
    assert doc["state"] == "ERROR"
    assert "busy after 60 s" in doc["detail"]
    assert doc["finding"].startswith("platform.openai.com probe: ERROR (")


def test_probe_holds_the_slot_around_the_tab() -> None:
    events: list[str] = []

    @contextlib.contextmanager
    def slot():
        events.append("acquire")
        yield 0
        events.append("release")

    def nav(endpoint: str, timeout: float) -> dict[str, Any]:
        events.append("tab")
        return {"state": "MFA_REQUIRED", "detail": "d", "final_url": "u"}

    doc = run_probe(slot=slot, nav=nav)
    assert events == ["acquire", "tab", "release"]
    assert doc["state"] == "MFA_REQUIRED"
    assert pp.exit_code(doc) == 2


def test_probe_survives_an_unreadable_cookie_db() -> None:
    def broken(_db: Path) -> list[tuple[str, str, int]]:
        raise FileNotFoundError("no Cookies")

    doc = run_probe(read_cookies=broken)
    assert doc["platform_auth_days"] is None
    assert doc["cookie_error"] == "FileNotFoundError: no Cookies"
    assert doc["log_fields"] == "platform=VALID platform_auth_days=-"
    assert any("cookie DB not read" in line for line in pp.summary_lines(doc))


def test_probe_warns_before_expiry() -> None:
    doc = run_probe(
        read_cookies=lambda _db: [
            (".auth.openai.com", "oai-client-auth-info", webkit(4.2))
        ]
    )
    assert doc["severity"] == "warn"
    assert doc["platform_auth_days"] == 4
    assert pp.exit_code(doc) == 2


def test_probe_end_to_end_over_a_fake_profile_never_carries_a_value(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "chrome"
    make_cookie_db(pp.cookie_db(profile), ROWS)
    doc = run_probe(profile=profile, read_cookies=pp.read_auth_cookies)
    assert doc["auth_days"]["oai_client_auth_info"] is not None
    assert SECRET not in json.dumps(doc)
    assert SECRET not in "\n".join(pp.summary_lines(doc))


def test_probe_uses_the_client_s_defaults(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cc, "dedicated_browser_version", lambda url: None)
    doc = pp.probe(
        read_cookies=lambda db: seen.setdefault("db", db) and [], now=lambda: NOW
    )
    assert cc.DEDICATED_URL in doc["detail"]
    assert seen["db"] == pp.cookie_db(cc.DEDICATED_PROFILE_DIR)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_prints_the_summary_and_writes_the_document(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    seen: dict[str, Any] = {}

    def fake_probe(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return run_probe(
            nav=lambda e, timeout: {
                "state": "LOGIN_REQUIRED",
                "detail": "the Platform log-in or chooser at platform.openai.com/login",
                "final_url": "platform.openai.com/login",
                "tab_closed": True,
            }
        )

    monkeypatch.setattr(pp, "probe", fake_probe)
    before = signal.getsignal(signal.SIGTERM)
    out = tmp_path / "run" / "platform.json"
    code = pp.main(["--json", str(out), "--port", "9333", "--timeout", "30"])
    assert code == 2
    assert signal.getsignal(signal.SIGTERM) == before
    assert seen["endpoint"] == "http://127.0.0.1:9333"
    assert seen["timeout"] == 30
    assert seen["warn_days"] == pp.WARN_DAYS
    text = capsys.readouterr().out
    assert text.startswith("platform: LOGIN_REQUIRED at platform.openai.com/login;")
    assert pp.OWNER_ACTION in text
    doc = json.loads(out.read_text())
    assert doc["state"] == "LOGIN_REQUIRED"
    assert doc["log_fields"] == "platform=LOGIN_REQUIRED platform_auth_days=29"


def test_main_json_dash_prints_the_document(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pp, "probe", lambda **_k: run_probe())
    assert pp.main(["--json", "-"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) >= {
        "checked_at",
        "state",
        "final_url",
        "auth_days",
        "platform_auth_days",
        "severity",
        "finding",
        "log_fields",
    }


def test_main_without_json_prints_only_the_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pp, "probe", lambda **_k: run_probe(alive=lambda u: None))
    assert pp.main([]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("platform: ERROR at -;")
