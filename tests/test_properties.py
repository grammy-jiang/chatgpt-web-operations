"""T0 property-based tests (TESTING.md section 6, P8).

Each property states its oracle in its docstring: the rule the function
promises, checked on inputs hypothesis generates and shrinks. The default
profile, ``daily``, is derandomized (the same examples every run, so the
daily ``make test`` is reproducible) and writes no example database;
``HYPOTHESIS_PROFILE=explore`` searches more widely with a random seed and
keeps its database under ``.hypothesis/`` (ignored by git). A failure prints
the shrunk example and a reproduction blob.
"""

from __future__ import annotations

import json
import os
import re
import string
from typing import Any

import api_shapes
import chatgpt_client as cc
import chatgpt_session
import coverage_gate
import pytest
import record_fixture
import review_topic
import round_state
from _common import table
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from test_fixture_hygiene import leaks_in

settings.register_profile(
    "daily",
    derandomize=True,
    max_examples=120,
    deadline=None,
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("explore", max_examples=1000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "daily"))


def _derive(password: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
    ).derive(password)


def _encrypt(version: bytes, key: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return version + enc.update(padded) + enc.finalize()


V10 = _derive(b"peanuts")
V11_PASSWORD = b"keyring-password-for-properties"

# Text a cookie value may hold: valid UTF-8 without control characters
# (chatgpt_session._make_decryptor's rule since 007c3d5).
cookie_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters="".join(map(chr, [*range(32), 127])),
    ),
    max_size=64,
)


@pytest.fixture
def decryptor(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(chatgpt_session, "_keyring_password", lambda app: V11_PASSWORD)
    return chatgpt_session._make_decryptor("chrome")


# 1 ---------------------------------------------------------------------------
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(value=cookie_text, version=st.sampled_from([b"v10", b"v11"]))
def test_decoding_is_the_identity_on_cookie_text(decryptor, value, version) -> None:
    """Oracle: ``plain(encrypt(v))`` is ``v`` for every value Chrome may
    store, empty included, under either key."""
    key = V10 if version == b"v10" else _derive(V11_PASSWORD)
    assert decryptor.plain(_encrypt(version, key, value.encode())) == value


# 2 ---------------------------------------------------------------------------
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    version=st.sampled_from([b"v10", b"v11", b"v12", b""]), body=st.binary(max_size=80)
)
def test_decoding_never_raises_on_any_bytes(decryptor, version, body) -> None:
    """Oracle: ``plain`` answers a string or None for anything a cookie
    database may hold, however damaged; one unreadable cookie must never end
    a session (failure-atlas, "Cookie decryptor exits the process")."""
    result = decryptor.plain(version + body)
    assert result is None or isinstance(result, str)


# 3 ---------------------------------------------------------------------------
expiry = st.one_of(st.none(), st.floats(0, 5e9, allow_nan=False))
record = st.one_of(
    st.none(), st.builds(lambda e: {"cookies": {}, "expires": e}, expiry)
)


@given(chrome=record, stored=record)
def test_choose_session_keeps_the_later_known_expiry(chrome, stored) -> None:
    """Oracle: the keyring's copy wins only with a known expiry strictly
    later than Chrome's (or Chrome's unknown); otherwise Chrome's."""
    chosen, source = chatgpt_session.choose_session(chrome, stored)
    if chrome is None and stored is None:
        assert (chosen, source) == (None, "none")
        return
    c = chrome.get("expires") if chrome else None
    s = stored.get("expires") if stored else None
    keyring_wins = stored is not None and (
        chrome is None or (s is not None and (c is None or s > c))
    )
    assert source == ("keyring" if keyring_wins else "chrome")
    assert chosen is (stored if keyring_wins else chrome)


# 4 ---------------------------------------------------------------------------
@given(
    ages=st.lists(st.integers(1, 10**9), min_size=1, max_size=4),
    now=st.floats(1e9, 3e9, allow_nan=False),
)
def test_renewed_session_expires_with_its_earliest_chunk(ages, now) -> None:
    """Oracle: every session-token chunk is kept, and the record expires at
    ``now`` plus the smallest ``Max-Age``; other cookies are ignored."""
    lines = [
        f"{chatgpt_session.SESSION_COOKIE_PREFIX}.{i}=v{i}; Max-Age={age}; Path=/"
        for i, age in enumerate(ages)
    ]
    lines.append("_puid=x; Max-Age=1; Path=/")
    got = chatgpt_session.renewed_session(lines, now)
    assert got is not None
    assert sorted(got["cookies"]) == sorted(
        f"{chatgpt_session.SESSION_COOKIE_PREFIX}.{i}" for i in range(len(ages))
    )
    assert got["expires"] == pytest.approx(now + min(ages))


# 5 ---------------------------------------------------------------------------
json_scalar = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=12))
body_strategy = st.dictionaries(
    st.text(min_size=1, max_size=10).filter(
        lambda k: k not in ("thinking_effort", "model", "system_hints")
    ),
    json_scalar,
    max_size=6,
).flatmap(
    lambda extra: st.fixed_dictionaries(
        {},
        optional={
            "system_hints": st.lists(st.text(max_size=8), max_size=4),
            "model": st.text(max_size=8),
            "thinking_effort": st.text(max_size=8),
        },
    ).map(lambda known: {**extra, **known})
)


@given(
    body=body_strategy,
    effort=st.sampled_from(["", "standard", "extended", "max"]),
    model=st.sampled_from(["", "gpt-5-6-thinking"]),
    search=st.booleans(),
    hints=st.lists(st.sampled_from(["search", "plugin:x", "canvas"]), max_size=3),
)
def test_rewrite_send_body_is_idempotent_and_keeps_unknown_keys(
    body, effort, model, search, hints
) -> None:
    """Oracle: rewriting twice equals rewriting once; every key the rewrite
    does not own survives unchanged; a hint is never duplicated; the page's
    own hints keep their order ahead of the appended ones."""
    once = cc.rewrite_send_body(body, effort, model, search, hints)
    assert cc.rewrite_send_body(once, effort, model, search, hints) == once
    for key, value in body.items():
        if key not in ("thinking_effort", "model", "system_hints"):
            assert once[key] == value
    if "system_hints" in once and (search or hints):
        after = once["system_hints"]
        before = list(body.get("system_hints") or [])
        assert after[: len(before)] == before
        added = after[len(before) :]
        assert len(added) == len(set(added))
        assert not set(added) & set(before)


# 6 ---------------------------------------------------------------------------
json_value = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(st.text(max_size=6), inner, max_size=3),
    ),
    max_leaves=8,
)
garbage = st.sampled_from(
    ["", "event: delta", ": keep-alive", "data: [DONE]", "data: {oops", "id: 7"]
)


@given(
    payloads=st.lists(json_value, max_size=6),
    noise=st.lists(garbage, max_size=6),
    ascii_only=st.booleans(),
)
# Found by the explore profile on 2026-09-29: a raw U+0085 split the line.
@example(payloads=[{"\x85": None}, "a\u2028b", ["\u2029"]], noise=[], ascii_only=False)
def test_stream_events_returns_every_payload_through_the_noise(
    payloads, noise, ascii_only
) -> None:
    """Oracle: each ``data: <json>`` line comes back as its value, in
    order, whatever event lines, comments, blank lines, ``[DONE]`` and
    unparseable data lines surround it, and whether or not the server
    escaped non-ASCII text."""
    lines: list[str] = []
    for i, value in enumerate(payloads):
        if i < len(noise):
            lines.append(noise[i])
        lines.append("data: " + json.dumps(value, ensure_ascii=ascii_only))
    lines.extend(noise[len(payloads) :])
    assert cc.stream_events("\n".join(lines) + "\n") == payloads


# 7 ---------------------------------------------------------------------------
@given(
    chat=st.uuids(),
    project=st.one_of(
        st.none(), st.text(string.hexdigits.lower(), min_size=32, max_size=32)
    ),
    prefix=st.sampled_from(["", "WEB:", "local-chatgpt:", "local-chatgpt%3A"]),
    query=st.sampled_from(["", "?model=gpt-5", "#settings"]),
)
def test_chat_id_and_is_provisional_on_generated_urls(
    chat, project, prefix, query
) -> None:
    """Oracle: the id in a conversation URL, with or without a project
    path, a query or a fragment, is the chat's own; provisional exactly when
    the page has not been given the real id yet."""
    base = (
        f"https://chatgpt.com/g/g-p-{project}-slug"
        if project
        else "https://chatgpt.com"
    )
    url = f"{base}/c/{prefix}{chat}{query}"
    expected = prefix.replace("%3A", ":") + str(chat)
    assert cc.chat_id(url) == expected
    assert cc.chat_id(f"  {expected}  ") == expected
    assert cc.is_provisional(url) is bool(prefix)


# 8 ---------------------------------------------------------------------------
benign = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="@"),
    max_size=12,
).filter(lambda t: not re.search(r"user-|org-|g-p-|grammy", t, re.I))
identifier = st.one_of(
    st.from_regex(r"[a-z0-9._]{1,8}@[a-z0-9]{1,6}\.[a-z]{2,4}", fullmatch=True),
    st.from_regex(r"user-[A-Za-z0-9]{1,24}", fullmatch=True),
    st.from_regex(r"org-[A-Za-z0-9]{1,24}", fullmatch=True),
    st.text(string.hexdigits, min_size=32, max_size=32).map(lambda h: f"g-p-{h}"),
    st.uuids().map(str),
)
tainted = st.lists(st.one_of(benign, identifier), min_size=1, max_size=5).map("".join)
payload = st.recursive(
    tainted,
    lambda inner: st.one_of(
        st.lists(inner, max_size=3),
        st.dictionaries(
            st.sampled_from(["id", "url", "owner", "x"]), inner, max_size=3
        ),
    ),
    max_leaves=6,
)


@given(obj=payload)
# Found on 2026-09-29: two addresses run together left a stray "@", and a
# "user-" followed by a non-id character was left as it was.
@example(obj="0@0.aaa@0.aa")
@example(obj={"owner": "user-\u00e9", "x": ["org-", "@handle"]})
def test_sanitize_leaves_nothing_the_hygiene_check_refuses(obj) -> None:
    """Oracle: ``tests/test_fixture_hygiene.leaks_in``, the check every
    committed fixture must pass, finds nothing in a sanitized payload of
    emails, user- and org- ids, g-p- ids and UUIDs in any mixture."""
    text = json.dumps(record_fixture.sanitize(obj), ensure_ascii=False)
    assert leaks_in(text) == []


# 9 ---------------------------------------------------------------------------
@given(
    low=st.floats(0, 100, allow_nan=False),
    high=st.floats(0, 100, allow_nan=False),
    module=st.sampled_from(["chatgpt_client.py", "_common.py", "list_chats.py"]),
)
def test_a_coverage_verdict_is_monotone_in_the_percentage(low, high, module) -> None:
    """Oracle: OK exactly when the percentage reaches the module's bar, so
    raising a module's coverage can never turn OK into LOW."""
    assume(low <= high)

    def status(percent: float) -> str:
        report = {
            "files": {f"scripts/{module}": {"summary": {"percent_covered": percent}}}
        }
        (row,) = coverage_gate.verdicts(report, [module])
        return row[4]

    bar = coverage_gate.bar_for(module)
    assert status(low) == ("OK" if low >= bar else "LOW")
    if status(low) == "OK":
        assert status(high) == "OK"


# 10 --------------------------------------------------------------------------
cell = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp")), max_size=8
)


@given(
    width=st.integers(1, 4).flatmap(
        lambda n: st.tuples(
            st.tuples(*[cell] * n),
            st.lists(st.tuples(*[cell] * n), min_size=1, max_size=5),
        )
    )
)
def test_table_columns_line_up(width) -> None:
    """Oracle: every cell of every line starts where its column starts, and
    a column is as wide as its widest cell or header."""
    headers, rows = width
    lines = table(rows, headers).split("\n")
    assert len(lines) == len(rows) + 2
    widths = [
        max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))
    ]
    for line, cells in zip([lines[0], *lines[2:]], [headers, *rows], strict=True):
        start = 0
        for text, w in zip(cells, widths, strict=True):
            assert line[start : start + len(text)] == text
            start += w + 2
    assert lines[1] == "  ".join("-" * w for w in widths)


# 11 --------------------------------------------------------------------------
@given(
    statuses=st.lists(st.integers(100, 599), max_size=4),
    new_chat=st.booleans(),
    real=st.booleans(),
    before=st.integers(0, 6),
    now=st.integers(0, 7),
)
def test_post_evidence_says_request_exactly_when_a_send_was_answered_2xx(
    statuses, new_chat, real, before, now
) -> None:
    """Oracle: "request" iff some status is 2xx; "url" iff a new chat has a
    real /c/ address; "turn" iff the count grew; and a 2xx added later never
    takes evidence away (F-2026-09-29-10)."""
    url = (
        "https://chatgpt.com/c/"
        + ("" if real else "WEB:")
        + "0" * 8
        + "-0000-4000-8000-"
        + "0" * 12
    )
    args = {"new_chat": new_chat, "url": url, "turns_before": before, "turns_now": now}
    found = cc.post_evidence(statuses=statuses, **args)
    assert ("request" in found) is any(200 <= s < 300 for s in statuses)
    assert ("url" in found) is (new_chat and real)
    assert ("turn" in found) is (now > before)
    assert set(found) <= set(cc.post_evidence(statuses=[*statuses, 200], **args))


# 12 --------------------------------------------------------------------------
@given(a=json_value, b=json_value)
def test_shape_merge_is_idempotent_and_commutative(a, b) -> None:
    """Oracle: merging a shape with itself changes nothing, the order of two
    observations does not matter, and a payload never drifts from its own
    shape (api_shapes, P5)."""
    sa, sb = api_shapes.infer(a), api_shapes.infer(b)
    assert api_shapes.merge(sa, sa) == sa
    assert api_shapes.merge(sa, sb) == api_shapes.merge(sb, sa)
    assert api_shapes.drift(sa, sa) == []


# 13 --------------------------------------------------------------------------
record_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=10)


@given(
    rows=st.lists(
        st.dictionaries(
            st.sampled_from(["id", "title", "note"]), record_text, min_size=1
        ),
        max_size=5,
    ),
    ascii_only=st.booleans(),
)
@example(rows=[{"title": "a\u2028b"}, {"note": "\x85"}], ascii_only=False)
def test_jsonl_readers_return_every_record_written(
    rows, ascii_only, tmp_path_factory
) -> None:
    """Oracle: records written one per line, whether or not non-ASCII is
    escaped, come back whole and in order from both JSONL readers, and the
    line count is the record count."""
    path = tmp_path_factory.mktemp("jsonl") / "rows.jsonl"
    text = "".join(json.dumps(r, ensure_ascii=ascii_only) + "\n" for r in rows)
    path.write_text(text, encoding="utf-8")
    assert round_state.read_jsonl(path) == rows
    assert review_topic.read_jsonl(path) == rows
    assert review_topic.count_lines(path) == len(rows)
