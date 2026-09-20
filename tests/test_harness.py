"""Tests for the test harness itself: TESTING.md sections 1 and 3.

Five things are proved here: a T0 test cannot reach the network; a live_*
test cannot run on its marker alone; the coverage gate's verdict is correct
per module; the live-tier guard enforces the sandbox; minting a throwaway
sandbox chat registers it with that guard and never skips that step; and
record_fixture's sanitizer never leaves an id or an email recoverable.
Every test names the failure it defends against, same as
tests/test_commands.py.
"""

from __future__ import annotations

import socket
from pathlib import Path

import conftest
import coverage_gate
import pytest
import record_fixture
from live import conftest as live_conftest
from live import guard as live_guard
from live import minting as live_minting
from live import test_browser_upload as live_test_browser_upload

# ---------------------------------------------------------------------------
# The T0 network guard (tests/conftest.py's autouse fixture)
# ---------------------------------------------------------------------------


def test_socket_socket_is_blocked_in_a_t0_test() -> None:
    """A T0 test must never be able to open a real socket (TESTING.md §1)."""
    with pytest.raises(RuntimeError, match="tier T0 test tried to open a socket"):
        socket.socket()


def test_socket_create_connection_is_blocked_in_a_t0_test() -> None:
    with pytest.raises(RuntimeError, match="tier T0 test tried to open a socket"):
        socket.create_connection(("198.51.100.1", 80))


# ---------------------------------------------------------------------------
# The live-tier gate: marker + CHATGPT_LIVE, never the marker alone
# ---------------------------------------------------------------------------


class _FakeItem:
    """A minimal stand-in for a pytest Item: only get_closest_marker."""

    def __init__(self, markers: set[str]) -> None:
        self._markers = markers

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None


def test_is_live_marked_true_only_for_the_four_live_markers() -> None:
    assert conftest.is_live_marked(_FakeItem({"live_write"})) is True
    assert conftest.is_live_marked(_FakeItem({"parametrize"})) is False
    assert conftest.is_live_marked(_FakeItem(set())) is False


def test_missing_live_var_names_the_variable_a_marker_still_needs() -> None:
    item = _FakeItem({"live_send"})
    assert conftest.missing_live_var(item, environ={}) == "send"
    assert conftest.missing_live_var(item, environ={"CHATGPT_LIVE": "send"}) is None


def test_missing_live_var_is_none_for_a_plain_t0_test() -> None:
    assert conftest.missing_live_var(_FakeItem(set()), environ={}) is None


def test_the_wrong_chatgpt_live_value_still_counts_as_missing() -> None:
    """CHATGPT_LIVE=write must not also unlock a live_read test."""
    item = _FakeItem({"live_read"})
    assert conftest.missing_live_var(item, environ={"CHATGPT_LIVE": "write"}) == "read"


CONFTEST_PATH = Path(__file__).resolve().parent / "conftest.py"


def _install_real_conftest(pytester: pytest.Pytester) -> None:
    """Load the real tests/conftest.py by path, so this exercises it
    unmodified rather than a rewritten copy of its skip logic."""
    pytester.makeconftest(
        f"""
        import importlib.util

        _spec = importlib.util.spec_from_file_location(
            "conftest_under_test", {str(CONFTEST_PATH)!r}
        )
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        pytest_collection_modifyitems = _real.pytest_collection_modifyitems
        """
    )


@pytest.mark.parametrize(
    ("chatgpt_live", "outcome"), [(None, "skipped"), ("read", "passed")]
)
def test_a_live_read_test_needs_the_env_var_not_just_the_marker(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    chatgpt_live: str | None,
    outcome: str,
) -> None:
    """End to end, against the real conftest.py, in an isolated subprocess."""
    pytester.makeini("[pytest]\nmarkers =\n    live_read: needs CHATGPT_LIVE=read\n")
    _install_real_conftest(pytester)
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.live_read
        def test_needs_live():
            assert True
        """
    )
    if chatgpt_live is None:
        monkeypatch.delenv("CHATGPT_LIVE", raising=False)
    else:
        monkeypatch.setenv("CHATGPT_LIVE", chatgpt_live)
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(**{outcome: 1})


# ---------------------------------------------------------------------------
# coverage_gate.verdicts -- a module absent from the report must not pass
# ---------------------------------------------------------------------------


def _report(files: dict) -> dict:
    return {"files": files}


def _summary(percent: float, statements: int = 10) -> dict:
    return {"summary": {"num_statements": statements, "percent_covered": percent}}


def test_a_module_above_its_bar_is_ok() -> None:
    report = _report({"scripts/_common.py": _summary(96.0)})
    rows = coverage_gate.verdicts(report, ["_common.py"])
    assert rows == [("_common.py", 10, 96.0, 95.0, "OK")]


def test_a_module_exactly_at_its_bar_is_ok_not_low() -> None:
    """ "95% or more" includes exactly 95%; off-by-one here is a false alarm."""
    report = _report({"scripts/round_state.py": _summary(95.0)})
    rows = coverage_gate.verdicts(report, ["round_state.py"])
    assert rows[0][4] == "OK"


def test_a_core_module_under_95_is_low_even_if_it_would_pass_the_other_bar() -> None:
    """The 90% bar must never leak onto a core module."""
    report = _report({"scripts/chatgpt_client.py": _summary(92.0, statements=724)})
    rows = coverage_gate.verdicts(report, ["chatgpt_client.py"])
    assert rows == [("chatgpt_client.py", 724, 92.0, 95.0, "LOW")]


def test_an_other_module_uses_the_90_bar_not_95() -> None:
    report = _report({"scripts/list_chats.py": _summary(91.0)})
    rows = coverage_gate.verdicts(report, ["list_chats.py"])
    assert rows[0][3] == 90.0
    assert rows[0][4] == "OK"


def test_a_module_missing_from_the_report_is_low_not_skipped() -> None:
    """A module nobody imports must not pass by being absent."""
    rows = coverage_gate.verdicts({"files": {}}, ["probe_account.py"])
    assert rows == [("probe_account.py", 0, 0.0, 90.0, "LOW")]


def test_a_passing_set_reports_every_module_ok() -> None:
    report = _report(
        {
            "scripts/_common.py": _summary(95.0),
            "scripts/list_chats.py": _summary(90.0),
        }
    )
    rows = coverage_gate.verdicts(report, ["_common.py", "list_chats.py"])
    assert [status for *_rest, status in rows] == ["OK", "OK"]


def test_bar_for_uses_95_only_for_the_named_core_modules() -> None:
    assert coverage_gate.bar_for("round_state.py") == 95.0
    assert coverage_gate.bar_for("clean_chats.py") == 90.0


# ---------------------------------------------------------------------------
# live/guard.py -- GuardedSession, one test per rule
# ---------------------------------------------------------------------------


class _FakeInner:
    """Records every call, so a violation can be proven to never reach it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call(self, path, method="GET", payload=None, raw=False, retries=3):
        self.calls.append((method, path))
        return 200, {"items": [{"id": "conv-1"}, {"id": "conv-2"}]}


def test_read_tier_allows_get() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "read")
    status, _body = guarded.call("/backend-api/me")
    assert status == 200
    assert inner.calls == [("GET", "/backend-api/me")]


def test_read_tier_refuses_every_non_get_before_reaching_inner() -> None:
    """The point of tier read: a write must never reach the account."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "read")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/conversation/x", method="PATCH")
    assert inner.calls == []


def test_write_tier_allows_a_patch_on_the_sandbox_gizmo() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call("/backend-api/gizmos/g-p-sand", method="PATCH")
    assert inner.calls == [("PATCH", "/backend-api/gizmos/g-p-sand")]


def test_write_tier_allows_a_gizmo_sub_path() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call("/backend-api/gizmos/g-p-sand/conversations", method="POST")
    assert inner.calls == [("POST", "/backend-api/gizmos/g-p-sand/conversations")]


def test_write_tier_refuses_a_sibling_gizmo_that_shares_a_text_prefix() -> None:
    """A naive startswith(base) would wrongly let g-p-sandwich through."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/gizmos/g-p-sandwich", method="PATCH")
    assert inner.calls == []


def test_write_tier_allows_the_sandbox_project_path_and_sub_paths() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call("/backend-api/projects/g-p-sand", method="PATCH")
    guarded.call("/backend-api/projects/g-p-sand/files", method="POST")
    assert len(inner.calls) == 2


def test_write_tier_allows_a_known_conversation_id() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(
        inner, "write", sandbox_id="g-p-sand", known_ids=["conv-1"]
    )
    guarded.call("/backend-api/conversation/conv-1", method="PATCH")
    assert inner.calls == [("PATCH", "/backend-api/conversation/conv-1")]


def test_write_tier_refuses_an_unknown_conversation_id() -> None:
    """An id the guard has never seen must not be actionable by guesswork."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/conversation/conv-9", method="PATCH")


def test_write_tier_refuses_a_conversation_sub_path() -> None:
    """Unlike gizmos and projects, a conversation id must match exactly."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(
        inner, "write", sandbox_id="g-p-sand", known_ids=["conv-1"]
    )
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/conversation/conv-1/extra", method="PATCH")


def test_an_empty_sandbox_id_refuses_every_write() -> None:
    """A guard built without a sandbox id must never authorize a write."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/gizmos/", method="PATCH")
    assert inner.calls == []


def test_the_query_string_is_stripped_before_matching() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call("/backend-api/projects/g-p-sand?expand=files", method="PATCH")
    assert inner.calls == [("PATCH", "/backend-api/projects/g-p-sand?expand=files")]


def test_refresh_learns_every_id_from_the_sandbox_listing() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.refresh()
    assert guarded.known == {"conv-1", "conv-2"}
    guarded.call("/backend-api/conversation/conv-2", method="PATCH")  # now known


def test_note_remembers_an_id_the_test_created_itself() -> None:
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.note("new-conv")
    guarded.call("/backend-api/conversation/new-conv", method="PATCH")
    assert ("PATCH", "/backend-api/conversation/new-conv") in inner.calls


@pytest.mark.parametrize("tier", ["write", "browser", "send"])
def test_every_non_read_tier_follows_the_same_sandbox_rule(tier: str) -> None:
    """Write, browser and send share one rule; only read is special-cased."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, tier, sandbox_id="g-p-sand")
    guarded.call("/backend-api/gizmos/g-p-sand", method="PATCH")
    assert inner.calls == [("PATCH", "/backend-api/gizmos/g-p-sand")]


# ---------------------------------------------------------------------------
# live/guard.py -- POST /backend-api/projects and DELETE gizmos/<id>, the
# two rules that let a T2 test create and delete its own throwaway project
# (tests/live/test_write_project_lifecycle.py) without ever being able to
# reach the sandbox that way
# ---------------------------------------------------------------------------


class _FakeProjectInner:
    """Like ``_FakeInner``, but a POST to /backend-api/projects answers with
    the captured response shape, carrying a fixed new project id."""

    def __init__(self, new_id: str = "g-p-newproject") -> None:
        self.new_id = new_id
        self.calls: list[tuple[str, str]] = []

    def call(self, path, method="GET", payload=None, raw=False, retries=3):
        self.calls.append((method, path))
        if method == "POST" and path == "/backend-api/projects":
            return 200, {
                "resource": {"gizmo": {"id": self.new_id}},
                "error": None,
                "sharing_targets": [],
            }
        return 200, {}


@pytest.mark.parametrize("tier", ["write", "browser", "send"])
def test_post_projects_is_allowed_when_the_name_starts_with_rp_test(
    tier: str,
) -> None:
    inner = _FakeProjectInner()
    guarded = live_guard.GuardedSession(inner, tier, sandbox_id="g-p-sand")
    status, _body = guarded.call(
        "/backend-api/projects", method="POST", payload={"name": "rp-test lifecycle"}
    )
    assert status == 200
    assert inner.calls == [("POST", "/backend-api/projects")]


def test_post_projects_is_refused_when_the_name_does_not_start_with_rp_test() -> None:
    """A throwaway project made through the guard must be nameable as one,
    or the sandbox sweep in tests/live/conftest.py would never find it."""
    inner = _FakeProjectInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call(
            "/backend-api/projects", method="POST", payload={"name": "my real idea"}
        )
    assert inner.calls == []


def test_post_projects_with_no_payload_is_refused_not_crashed_on() -> None:
    """A missing body must read as "no name", not raise its own AttributeError."""
    inner = _FakeProjectInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/projects", method="POST", payload=None)


def test_a_successful_project_create_is_remembered_in_created() -> None:
    inner = _FakeProjectInner(new_id="g-p-newproject")
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call(
        "/backend-api/projects", method="POST", payload={"name": "rp-test lifecycle"}
    )
    assert guarded.created == {"g-p-newproject"}


def test_delete_gizmo_is_allowed_for_an_id_the_guard_created() -> None:
    inner = _FakeProjectInner(new_id="g-p-newproject")
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.call(
        "/backend-api/projects", method="POST", payload={"name": "rp-test lifecycle"}
    )
    status, _body = guarded.call("/backend-api/gizmos/g-p-newproject", method="DELETE")
    assert status == 200
    assert ("DELETE", "/backend-api/gizmos/g-p-newproject") in inner.calls


def test_delete_gizmo_is_refused_for_an_id_the_guard_never_created() -> None:
    """A guessed or hard-coded id must not be deletable just by asking."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/gizmos/g-p-unknown", method="DELETE")
    assert inner.calls == []


def test_delete_gizmo_is_refused_for_the_sandbox_id_even_if_marked_created() -> None:
    """The one id that must never be deletable through the guard, even if
    it ended up in self.created some other way."""
    inner = _FakeInner()
    guarded = live_guard.GuardedSession(inner, "write", sandbox_id="g-p-sand")
    guarded.created.add("g-p-sand")
    with pytest.raises(live_guard.GuardViolation):
        guarded.call("/backend-api/gizmos/g-p-sand", method="DELETE")
    assert inner.calls == []


# ---------------------------------------------------------------------------
# live/conftest.py -- sweep_targets, the pure half of the sandbox teardown
# ---------------------------------------------------------------------------


def test_sweep_targets_of_an_empty_listing_is_empty() -> None:
    assert live_conftest.sweep_targets([]) == []


def test_sweep_targets_reads_a_single_page() -> None:
    page = {
        "items": [
            {"id": "conv-1", "title": "rp-test send 1"},
            {"id": "conv-2", "title": "Reply PONG"},
        ],
        "cursor": None,
    }
    assert live_conftest.sweep_targets([page]) == [
        ("conv-1", "rp-test send 1"),
        ("conv-2", "Reply PONG"),
    ]


def test_sweep_targets_merges_every_page_walked_via_cursor() -> None:
    """The fixture walks the cursor and hands every page it collected here;
    sweep_targets must merge all of them, not just the first."""
    page_one = {"items": [{"id": "conv-1", "title": "a"}], "cursor": "opaque-2"}
    page_two = {"items": [{"id": "conv-2", "title": "b"}], "cursor": None}
    assert live_conftest.sweep_targets([page_one, page_two]) == [
        ("conv-1", "a"),
        ("conv-2", "b"),
    ]


def test_sweep_targets_includes_every_title_not_only_rp_test_ones() -> None:
    """The bug this replaces: ChatGPT overwrites a chat's title once the
    first reply lands, so a title filter let a renamed chat escape the
    sweep and it had to be deleted by hand (TESTING.md §1). The fix is no
    title filter at all -- every conversation in the sandbox is a target."""
    page = {
        "items": [
            {"id": "conv-1", "title": "Python Release Schedule Summary"},
            {"id": "conv-2", "title": ""},
            {"id": "conv-3"},  # title missing entirely
        ],
        "cursor": None,
    }
    assert live_conftest.sweep_targets([page]) == [
        ("conv-1", "Python Release Schedule Summary"),
        ("conv-2", ""),
        ("conv-3", ""),
    ]


def test_sweep_targets_skips_an_item_with_no_id() -> None:
    """An id-less entry must not become a PATCH to .../conversation/."""
    page = {"items": [{"title": "no id here"}, {"id": "conv-1", "title": "ok"}]}
    assert live_conftest.sweep_targets([page]) == [("conv-1", "ok")]


def test_sweep_targets_tolerates_a_malformed_page() -> None:
    """A page that is not the expected shape must not crash the sweep."""
    assert live_conftest.sweep_targets([None, {}, {"items": None}, "oops"]) == []


# ---------------------------------------------------------------------------
# live/conftest.py -- the T4 send cap (CHATGPT_LIVE_SENDS)
# ---------------------------------------------------------------------------


def test_the_send_budget_allows_up_to_its_limit() -> None:
    budget = live_conftest.SendBudget(2)
    assert budget.spend("test_one") == ""
    assert budget.spend("test_two") == ""


def test_the_send_budget_refuses_the_send_past_its_limit() -> None:
    """Over the cap must produce a refusal naming the test and the cap, so
    a run that grew a send storm says which test crossed the line."""
    budget = live_conftest.SendBudget(1)
    budget.spend("test_one")
    refusal = budget.spend("test_two")
    assert "test_two" in refusal
    assert "cap of 1" in refusal
    assert "CHATGPT_LIVE_SENDS" in refusal


def test_the_send_budget_default_applies_when_the_variable_is_absent() -> None:
    assert live_conftest.budget_limit({}) == live_conftest.DEFAULT_LIVE_SENDS


@pytest.mark.parametrize("raw", ["", "  ", "two", "0", "-3", "2.5", "1e3"])
def test_a_bad_send_budget_value_falls_back_to_the_default(raw: str) -> None:
    """A typo must never lift the cap, and must never set it to zero
    either -- both would change what a run is allowed to do by accident."""
    assert (
        live_conftest.budget_limit({live_conftest.SENDS_VAR: raw})
        == live_conftest.DEFAULT_LIVE_SENDS
    )


def test_a_valid_send_budget_value_is_used() -> None:
    assert live_conftest.budget_limit({live_conftest.SENDS_VAR: " 7 "}) == 7


# ---------------------------------------------------------------------------
# live/minting.py -- minting a throwaway sandbox chat, over a fake client
# ---------------------------------------------------------------------------


class _FakeSender:
    """Stands in for chatgpt_client.BrowserSender: a context manager whose
    ``send`` returns whatever id the fake client was built with."""

    def __init__(self, new_id: str, opened: list[dict]) -> None:
        self._new_id = new_id
        self._opened = opened

    def __enter__(self) -> _FakeSender:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def send(self, prompt: str, name: str = "") -> str:
        self._opened.append({"prompt": prompt})
        return self._new_id


class _FakeClient:
    """The two names minting.mint_sandbox_chat uses from chatgpt_client."""

    def __init__(self, new_id: str, resolved: str = "") -> None:
        self.new_id = new_id
        self.resolved = resolved
        self.opened: list[dict] = []
        self.kwargs: dict = {}

    def BrowserSender(self, browser: str, **kwargs: object) -> _FakeSender:
        self.kwargs = dict(kwargs, browser=browser)
        return _FakeSender(self.new_id, self.opened)

    @staticmethod
    def is_provisional(chat: str) -> bool:
        return chat.startswith("WEB:")

    def resolve_new_conversation(
        self, session: object, known_ids: set, *, since: float = 0.0
    ) -> str:
        return self.resolved


class _FakeGuarded:
    """Enough of GuardedSession for minting: ``call`` and ``note``."""

    def __init__(self) -> None:
        self.known: set[str] = set()
        self.calls: list[tuple[str, str, object]] = []

    def call(
        self, path: str, method: str = "GET", payload: object = None, **_kw: object
    ) -> tuple[int, object]:
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/backend-api/conversations?"):
            return 200, {"items": [{"id": "old-1"}, {"id": "old-2"}]}
        return 200, {}

    def note(self, conversation_id: str) -> None:
        self.known.add(conversation_id)


def test_minting_resolves_a_provisional_id_and_notes_it() -> None:
    """The WEB: id the page hands back must be resolved, and the real id
    registered with the guard -- without that note, every PATCH the test
    then makes would be refused (tests/live/guard.py)."""
    cc = _FakeClient("WEB:pending", resolved="conv-new")
    guarded = _FakeGuarded()
    chat_id = live_minting.mint_sandbox_chat(cc, guarded, "g-p-sand", "hello")
    assert chat_id == "conv-new"
    assert guarded.known == {"conv-new"}


def test_minting_sends_into_the_sandbox_project_headless() -> None:
    """A minted chat must land inside the sandbox, never in the sidebar,
    and must not open a visible window in an unattended run."""
    cc = _FakeClient("conv-new")
    live_minting.mint_sandbox_chat(cc, _FakeGuarded(), "g-p-sand", "hello")
    assert cc.kwargs["project"] == "g-p-sand"
    assert cc.kwargs["visible"] is False
    assert cc.opened == [{"prompt": "hello"}]


def test_minting_keeps_a_real_id_without_resolving() -> None:
    """A send that already returned a real id must not go looking for a
    'new' conversation -- that search could pick up another run's chat."""
    cc = _FakeClient("conv-real", resolved="conv-wrong")
    assert (
        live_minting.mint_sandbox_chat(cc, _FakeGuarded(), "g-p-sand", "hi")
        == "conv-real"
    )


def test_minting_renames_only_when_a_title_was_asked_for() -> None:
    guarded = _FakeGuarded()
    live_minting.mint_sandbox_chat(
        _FakeClient("conv-new"), guarded, "g-p-sand", "hi", title="rp-test flags"
    )
    assert (
        "PATCH",
        "/backend-api/conversation/conv-new",
        {"title": "rp-test flags"},
    ) in guarded.calls

    quiet = _FakeGuarded()
    live_minting.mint_sandbox_chat(_FakeClient("conv-new"), quiet, "g-p-sand", "hi")
    assert [c for c in quiet.calls if c[0] == "PATCH"] == []


def test_deleting_a_minted_chat_is_the_ui_patch() -> None:
    guarded = _FakeGuarded()
    live_minting.delete_sandbox_chat(guarded, "conv-new")
    assert guarded.calls == [
        ("PATCH", "/backend-api/conversation/conv-new", {"is_visible": False})
    ]


@pytest.mark.parametrize("chat_id", ["", "WEB:pending"])
def test_deleting_skips_an_id_that_never_became_a_conversation(chat_id: str) -> None:
    """A finally block runs however far the send got. An empty or still
    provisional id must not become a PATCH to a path that means nothing."""
    guarded = _FakeGuarded()
    live_minting.delete_sandbox_chat(guarded, chat_id)
    assert guarded.calls == []


# ---------------------------------------------------------------------------
# live/test_browser_upload.py -- verify(), the T3 upload probe's own check
# ---------------------------------------------------------------------------


def test_browser_upload_verify_passes_a_matching_chip_and_enabled_send() -> None:
    info = {
        "remove_label": "Remove file 1: rp-test-browser-upload.txt",
        "send_exists": True,
        "send_enabled": True,
    }
    assert live_test_browser_upload.verify(info, "rp-test-browser-upload.txt") == []


def test_browser_upload_verify_flags_a_missing_remove_chip() -> None:
    info = {"remove_label": None, "send_exists": True, "send_enabled": True}
    mismatches = live_test_browser_upload.verify(info, "rp-test-browser-upload.txt")
    assert len(mismatches) == 1
    assert "Remove file" in mismatches[0]


def test_browser_upload_verify_flags_a_chip_naming_the_wrong_file() -> None:
    """A 'Remove file' chip that names a different file must not pass just
    because some upload succeeded."""
    info = {
        "remove_label": "Remove file 1: some-other-file.txt",
        "send_exists": True,
        "send_enabled": True,
    }
    mismatches = live_test_browser_upload.verify(info, "rp-test-browser-upload.txt")
    assert len(mismatches) == 1
    assert "rp-test-browser-upload.txt" in mismatches[0]


def test_browser_upload_verify_flags_a_missing_send_button() -> None:
    info = {
        "remove_label": "Remove file 1: rp-test-browser-upload.txt",
        "send_exists": False,
        "send_enabled": False,
    }
    mismatches = live_test_browser_upload.verify(info, "rp-test-browser-upload.txt")
    assert mismatches == [
        "no send button (neither [data-testid=send-button] nor "
        '[aria-label="Send prompt"])'
    ]


def test_browser_upload_verify_flags_a_disabled_send_button() -> None:
    info = {
        "remove_label": "Remove file 1: rp-test-browser-upload.txt",
        "send_exists": True,
        "send_enabled": False,
    }
    assert live_test_browser_upload.verify(info, "rp-test-browser-upload.txt") == [
        "send button is present but not enabled"
    ]


# ---------------------------------------------------------------------------
# record_fixture.sanitize -- nothing identifying may survive it
# ---------------------------------------------------------------------------


def test_an_email_shaped_string_is_replaced() -> None:
    assert record_fixture.sanitize("someone@example.com") == "user@example.invalid"


def test_an_email_embedded_in_a_longer_string_is_still_caught() -> None:
    out = record_fixture.sanitize("contact someone@example.com for access")
    assert "gmail.com" not in out
    assert "user@example.invalid" in out


def test_a_user_id_is_replaced_with_a_fixed_placeholder() -> None:
    assert record_fixture.sanitize("user-AbC123xyz") == "user-XXXXXXXX"


def test_an_org_id_is_replaced_with_a_fixed_placeholder() -> None:
    assert record_fixture.sanitize("org-9zZzZ999") == "org-XXXXXXXX"


def test_a_gizmo_id_gets_a_stable_numbered_placeholder() -> None:
    real = "g-p-" + "ab" * 16  # 32 hex chars
    assert record_fixture.sanitize(real) == "g-p-00000000000000000000000000000001"


def test_the_same_gizmo_id_maps_to_the_same_placeholder_twice() -> None:
    """A fixture must stay internally consistent: one project, one id."""
    real = "g-p-" + "cd" * 16
    out = record_fixture.sanitize({"a": real, "b": {"c": real}})
    assert out["a"] == out["b"]["c"] == "g-p-00000000000000000000000000000001"


def test_two_distinct_gizmo_ids_get_two_distinct_placeholders_in_order() -> None:
    first, second = "g-p-" + "11" * 16, "g-p-" + "22" * 16
    out = record_fixture.sanitize([first, second, first])
    assert out[0] == out[2] != out[1]
    assert out[0].endswith("000001")
    assert out[1].endswith("000002")


def test_a_uuid_shaped_string_gets_a_stable_placeholder() -> None:
    real = "6aa9e952-f2dc-83ec-a3cb-39ed2ff14db8"
    out = record_fixture.sanitize({"id": real, "again": real})
    assert out["id"] == out["again"] == "00000000-0000-4000-8000-000000000001"


def test_a_redacted_key_keeps_only_its_length() -> None:
    doc = {"about_user_message": "I am a software engineer."}
    assert record_fixture.sanitize(doc) == {"about_user_message": "<redacted 25 chars>"}


def test_a_redacted_key_with_an_empty_value_stays_empty() -> None:
    assert record_fixture.sanitize({"instructions": ""}) == {"instructions": ""}


def test_a_redacted_key_recurses_normally_when_its_value_is_not_a_string() -> None:
    """``content`` is usually a dict; length-redaction only applies to text."""
    doc = {"content": {"content_type": "text"}}
    assert record_fixture.sanitize(doc) == doc


def test_numbers_booleans_and_null_pass_through_unchanged() -> None:
    doc = {"n": 5000000, "ok": True, "missing": None}
    assert record_fixture.sanitize(doc) == doc


def test_lists_and_dicts_are_recursed() -> None:
    doc = {"files": [{"id": "user-AbCdEfGh", "size": 12}]}
    out = record_fixture.sanitize(doc)
    assert out == {"files": [{"id": "user-XXXXXXXX", "size": 12}]}
