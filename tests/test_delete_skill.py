"""Personal Skill deletion contracts over the existing skills session fake."""

import sys

import delete_skill
import pytest
from test_list_skills import _FakeSkillsSession, _skill

NAME = "fake-skill"
ID = "hz-fake-skill"
LIST = "/backend-api/hazelnuts?include_permissions=true&scope=installed"
DELETE = f"/backend-api/hazelnuts/{ID}"
SECRET = "fake-private-token"
SIGNED_URL = f"https://example.invalid/file?sig={SECRET}"


def inventory(*items):
    return 200, {"hazelnuts": list(items)}


def wire(monkeypatch, *replies):
    session = _FakeSkillsSession(list(replies))
    monkeypatch.setattr(delete_skill, "open_session", lambda *_: session)
    return session.session


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--confirm", "--dry-run"]])
def test_dry_run_reads_exact_name_and_never_deletes(monkeypatch, capsys, flags):
    backend = wire(monkeypatch, inventory(_skill(), _skill("other")))
    assert delete_skill.main([NAME, "--id", ID, *flags]) == 0
    assert backend.calls == [("GET", LIST, None)]
    out = capsys.readouterr().out
    assert "dry run" in out
    assert NAME in out and ID in out and "unchecked" in out and "1/1" in out


@pytest.mark.parametrize("guard", [[], ["--id", ID]])
def test_confirm_deletes_only_resolved_id_then_verifies_fresh_list(
    monkeypatch, capsys, guard
):
    other = _skill("other")
    backend = wire(
        monkeypatch,
        inventory(other, _skill()),
        (200, {"deleted": True, "url": SIGNED_URL}),
        inventory(other),
    )
    original = backend.call

    def call(path, method="GET", **kwargs):
        if method == "DELETE":
            assert kwargs["retries"] == 1  # Never replay a destructive request.
            audit = capsys.readouterr().out
            assert NAME in audit and ID in audit  # Audit precedes mutation.
        return original(path, method=method, **kwargs)

    monkeypatch.setattr(backend, "call", call)
    assert delete_skill.main([NAME, "--confirm", *guard]) == 0
    assert backend.calls == [
        ("GET", LIST, None),
        ("DELETE", DELETE, None),
        ("GET", LIST, None),
    ]
    out = capsys.readouterr().out
    assert "deleted and verified" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    "args", [[], ["--confirm"], [NAME, "another-name"], [NAME, "--con"]]
)
def test_requires_exactly_one_name_argument(monkeypatch, args):
    backend = wire(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        delete_skill.main(args)
    assert exc.value.code == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "args",
    [[""], [" "], ["a\nb"], ["a" * 201], [SIGNED_URL], [NAME, "--id", "../plugins"]],
)
def test_invalid_identity_refuses_before_session(monkeypatch, args):
    def forbidden(*_):
        pytest.fail("invalid identity must not open a session")

    monkeypatch.setattr(delete_skill, "open_session", forbidden)
    assert delete_skill.main(args) == 2


@pytest.mark.parametrize(
    ("items", "args"),
    [
        ([], [NAME]),
        ([_skill("fake-skill-extra")], [NAME]),
        ([_skill()], ["FAKE-SKILL"]),
        ([_skill()], [f" {NAME}"]),
        ([_skill(), _skill()], [NAME, "--id", ID]),
        ([_skill(), {**_skill("other"), "id": ID}], [NAME]),
        ([_skill()], [NAME, "--id", "hz-other"]),
    ],
)
def test_missing_ambiguous_or_mismatched_identity_never_deletes(
    monkeypatch, items, args
):
    backend = wire(monkeypatch, inventory(*items))
    assert delete_skill.main([*args, "--confirm"]) == 2
    assert backend.calls == [("GET", LIST, None)]


@pytest.mark.parametrize(
    "permissions",
    [
        None,
        [],
        {},
        {"can_read": False, "can_delete": True},
        {"can_read": True},
        {"can_read": True, "can_delete": "true"},
    ],
)
def test_requires_explicit_read_and_delete_permissions(monkeypatch, permissions):
    backend = wire(monkeypatch, inventory({**_skill(), "permissions": permissions}))
    assert delete_skill.main([NAME, "--confirm"]) == 2
    assert backend.calls == [("GET", LIST, None)]


BAD_READS = [
    (403, {"error": SECRET, "url": SIGNED_URL}),
    (200, None),
    (200, {}),
    (200, {"hazelnuts": None}),
    (200, {"hazelnuts": {}}),
    inventory(None),
    inventory({"name": NAME}),
    inventory({"id": ID}),
    inventory({"id": "../plugins", "name": NAME}),
    inventory({"id": 123, "name": NAME}),
    inventory({"id": ID, "name": [NAME]}),
    inventory(_skill(), {}),
]


@pytest.mark.parametrize("reply", BAD_READS)
def test_unreadable_inventory_fails_without_delete(monkeypatch, capsys, reply):
    backend = wire(monkeypatch, reply)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    assert backend.calls == [("GET", LIST, None)]
    out = capsys.readouterr().out
    assert "read failed" in out
    assert SECRET not in out and SIGNED_URL not in out


FAILED_DELETE_READBACKS = [
    (
        inventory(),
        "target is absent on read-back but the DELETE response was not successful",
    ),
    (inventory(_skill()), "remains installed"),
    ((503, {"error": SIGNED_URL}), "final state could not be established"),
    ((200, {}), "final state could not be established"),
]


@pytest.mark.parametrize("status", [0, 204, 400, 403, 404, 429, 500])
@pytest.mark.parametrize(("after", "observed"), FAILED_DELETE_READBACKS)
def test_delete_http_failure_is_failure(monkeypatch, capsys, status, after, observed):
    backend = wire(
        monkeypatch, inventory(_skill()), (status, {"error": SIGNED_URL}), after
    )
    original = backend.call

    def call(path, method="GET", **kwargs):
        if backend.calls:
            assert kwargs["retries"] == 1
        return original(path, method=method, **kwargs)

    monkeypatch.setattr(backend, "call", call)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    assert backend.calls == [
        ("GET", LIST, None),
        ("DELETE", DELETE, None),
        ("GET", LIST, None),
    ]
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "DELETE failed" in out and observed in out
    assert "deleted and verified" not in out and SECRET not in out


@pytest.mark.parametrize("error_type", [RuntimeError, SystemExit])
@pytest.mark.parametrize(("after", "observed"), FAILED_DELETE_READBACKS)
def test_delete_exception_still_reads_observed_state(
    monkeypatch, capsys, error_type, after, observed
):
    backend = wire(monkeypatch, inventory(_skill()), after)
    original = backend.call

    def call(path, method="GET", payload=None, **kwargs):
        if backend.calls:
            assert kwargs["retries"] == 1
        if method == "DELETE":
            backend.calls.append((method, path, payload))
            raise error_type(SIGNED_URL)
        return original(path, method=method, payload=payload, **kwargs)

    monkeypatch.setattr(backend, "call", call)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    assert backend.calls == [
        ("GET", LIST, None),
        ("DELETE", DELETE, None),
        ("GET", LIST, None),
    ]
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "DELETE request failed" in out and observed in out
    assert "deleted and verified" not in out and SECRET not in out


@pytest.mark.parametrize("delete_raises", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, SystemExit])
def test_failed_delete_and_readback_exception_report_unknown_state(
    monkeypatch, capsys, delete_raises, error_type
):
    backend = wire(monkeypatch, inventory(_skill()))
    original = backend.call

    def call(path, method="GET", payload=None, **kwargs):
        if not backend.calls:
            return original(path, method=method, payload=payload, **kwargs)
        assert kwargs["retries"] == 1
        backend.calls.append((method, path, payload))
        if method == "DELETE" and not delete_raises:
            return 500, {"error": SIGNED_URL}
        raise error_type(SIGNED_URL)

    monkeypatch.setattr(backend, "call", call)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    assert backend.calls == [
        ("GET", LIST, None),
        ("DELETE", DELETE, None),
        ("GET", LIST, None),
    ]
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "final state could not be established" in out
    assert "deleted and verified" not in out and SECRET not in out


@pytest.mark.parametrize(
    "after",
    [
        inventory(_skill()),
        inventory({**_skill(), "name": "renamed"}),
        inventory({**_skill(), "id": "hz-replacement"}),
        *BAD_READS,
    ],
)
def test_http_200_is_not_success_without_verified_absence(monkeypatch, capsys, after):
    backend = wire(monkeypatch, inventory(_skill()), (200, {"deleted": True}), after)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    assert backend.calls == [
        ("GET", LIST, None),
        ("DELETE", DELETE, None),
        ("GET", LIST, None),
    ]
    out = capsys.readouterr().out
    assert "deletion could not be verified" in out
    assert "deleted and verified" not in out and SECRET not in out


@pytest.mark.parametrize("stage", ["session", "read", "verify"])
@pytest.mark.parametrize("error_type", [RuntimeError, SystemExit])
def test_exceptions_fail_without_exposing_details(
    monkeypatch, capsys, stage, error_type
):
    backend = wire(monkeypatch, inventory(_skill()), (200, {"deleted": True}))
    original = backend.call

    def fail(*_args, **_kwargs):
        if stage == "session":
            print(SECRET)
            print(SIGNED_URL, file=sys.stderr)
        raise error_type(SIGNED_URL)

    if stage == "session":
        monkeypatch.setattr(delete_skill, "open_session", fail)
    else:

        def call(path, method="GET", **kwargs):
            if len(backend.calls) == {"read": 0, "verify": 2}[stage]:
                fail()
            return original(path, method=method, **kwargs)

        monkeypatch.setattr(backend, "call", call)
    assert delete_skill.main([NAME, "--confirm"]) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    if stage in ("session", "read"):
        assert backend.calls == []
    if stage == "verify":
        assert "deletion could not be verified" in captured.out


def test_audit_omits_secrets_urls_and_unbounded_metadata(monkeypatch, capsys):
    item = {
        **_skill(),
        "enabled": {"cookie": SECRET},
        "default_version_no": SIGNED_URL,
        "latest_version_no": "9" * 1000,
        "safety_check_status": SIGNED_URL,
        "description": SECRET,
        "auth_token": SECRET,
        "icon_small": SIGNED_URL,
        "files": {SECRET: {"url": SIGNED_URL}},
        "safety_scan": {"justification": SECRET},
    }
    wire(monkeypatch, inventory(item))
    assert delete_skill.main([NAME]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert SECRET not in out and SIGNED_URL not in out
    assert "?/?" in out and len(out) < 1000


def test_disabled_skill_can_be_deleted(monkeypatch):
    wire(
        monkeypatch,
        inventory(_skill(enabled=False)),
        (200, {"deleted": True}),
        inventory(),
    )
    assert delete_skill.main([NAME, "--confirm"]) == 0
