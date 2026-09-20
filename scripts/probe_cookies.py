#!/usr/bin/env python3
"""Which chatgpt.com cookies decrypt, which do not, and when they expire.
Read-only.

    probe_cookies.py [--browser chrome] [--json PATH]

Run this first when a session will not open. A failure here looks exactly
like an account block from the outside, and once did: the decryptor used to
exit the process over an *irrelevant* analytics cookie, so a run died with
"could not decode a decrypted cookie value" while the session cookie itself
was perfectly readable.

Values are never printed. Names, lengths and expiry dates only.

The expiry column is the other way a session dies. This reads Chrome's own
jar, which nothing in this skill writes. Since 2026-09-21 the client also
keeps a renewed copy of the session token in the keyring: every
authentication re-issues it for 90 days (``chatgpt_session.store_session``),
so the horizon that matters for the scripts is the later of the two, and
``health.py`` reports both. What this command shows is Chrome's copy.

Measured 2026-09-20 by removing cookies from the jar in memory: only
``__Secure-next-auth.session-token`` (issued for 90 days) is load-bearing, for
the HTTP reads and for the compose page alike; ``_puid`` (7 days) and
``__Secure-oai-is`` (30 days) are reissued by the page when missing and change
nothing. So the one horizon worth a warning is the session token's, and
``session_horizon`` reads it from the jar without decrypting anything. One
caveat: the scripted browser's shared profile keeps its own copy of the jar
and may outlive this one; this reads the source. ``--json PATH`` writes the
counts and that horizon for a caller that wants a number (the daily health
check); the text output says the same.

Exit 0 when the session cookie is readable, 1 when it is not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _common import ensure_venv, load_client, table

SESSION_PREFIX = "__Secure-next-auth.session-token"
# Chrome stores expires_utc in microseconds since 1601-01-01 (the Windows
# FILETIME epoch); 0 means a session cookie, gone when the browser closes.
WEBKIT_EPOCH_DELTA_S = 11_644_473_600


def read_jar(db: Path) -> list[tuple[str, str, bytes, int]]:
    """(name, host, encrypted_value, expires_utc) for every chatgpt.com cookie."""
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        shutil.copy2(db, tmp.name)  # the live DB is locked by the browser
        con = sqlite3.connect(tmp.name)
        rows = con.execute(
            "SELECT name, host_key, encrypted_value, expires_utc FROM cookies "
            "WHERE host_key LIKE '%chatgpt.com' ORDER BY name"
        ).fetchall()
        con.close()
    return [(name, host, enc, int(exp or 0)) for name, host, enc, exp in rows]


def expiry_epoch(expires_utc: int) -> float | None:
    """Unix time a cookie expires, or None for a session cookie (0)."""
    if not expires_utc:
        return None
    return expires_utc / 1_000_000 - WEBKIT_EPOCH_DELTA_S


def expiry_label(expires_utc: int, now: float) -> str:
    """ "session", or "YYYY-MM-DD (N.N d)" -- negative when already expired."""
    epoch = expiry_epoch(expires_utc)
    if epoch is None:
        return "session"
    day = datetime.fromtimestamp(epoch, UTC).date().isoformat()
    return f"{day} ({(epoch - now) / 86400:.1f} d)"


def session_horizon(
    rows: list[tuple[str, str, bytes, int]], now: float
) -> tuple[str, float] | None:
    """(expiry date, days left) of the session token, from the jar alone --
    no decryption needed, expiry is not a secret. None when the jar holds
    no session token, or only a session-scoped one. The earliest of the
    token's chunks (``.0``, ``.1``) is the one that matters."""
    epochs = [
        epoch
        for name, _host, _enc, exp in rows
        if name.startswith(SESSION_PREFIX) and (epoch := expiry_epoch(exp)) is not None
    ]
    if not epochs:
        return None
    soonest = min(epochs)
    day = datetime.fromtimestamp(soonest, UTC).date().isoformat()
    return day, (soonest - now) / 86400


def classify(
    rows: list[tuple[str, str, bytes, int]], decrypt, now: float | None = None
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(readable, unreadable) table rows. Never returns a cookie's value."""
    now = time.time() if now is None else now
    good: list[tuple[str, ...]] = []
    bad: list[tuple[str, ...]] = []
    for name, host, enc, exp in rows:
        try:
            value = decrypt(enc)
        except Exception as exc:
            bad.append((name, host, type(exc).__name__))
            continue
        good.append((name, host, f"{len(value)} chars", expiry_label(exp, now)))
    return good, bad


def report(
    good: list[tuple[str, ...]],
    bad: list[tuple[str, ...]],
    horizon: tuple[str, float] | None,
) -> dict[str, Any]:
    """What ``--json`` writes: counts, and the session token's horizon."""
    return {
        "readable": len(good),
        "unreadable": len(bad),
        "session_readable": any(row[0].startswith(SESSION_PREFIX) for row in good),
        "session_expires": horizon[0] if horizon else None,
        "session_days_left": round(horizon[1], 2) if horizon else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome")
    ap.add_argument(
        "--json",
        default="",
        metavar="PATH",
        help="also write counts and the session token's expiry here",
    )
    args = ap.parse_args(argv)

    cc = load_client()
    cs = cc._helpers()
    db, app = cs.BROWSERS[args.browser]
    if not Path(db).exists():
        print(f"no cookie DB for {args.browser} at {db}")
        return 1

    now = time.time()
    rows = read_jar(Path(db))
    good, bad = classify(rows, cc._tolerant_decryptor(cs, app), now)
    horizon = session_horizon(rows, now)
    print(table(good, ("cookie", "host", "value", "expires")))
    if bad:
        print("\nunreadable:")
        print(table(bad, ("cookie", "host", "why")))

    doc = report(good, bad, horizon)
    if args.json:
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    print(f"\n{len(good)} readable, {len(bad)} unreadable")
    if horizon:
        print(f"session token expires {horizon[0]} ({horizon[1]:.1f} days left)")
    if not doc["session_readable"]:
        print("The session cookie is NOT readable. That is the fatal one.")
        return 1
    print("The session cookie is readable, so an unreadable analytics cookie")
    print("here is noise: it must never end a run.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
