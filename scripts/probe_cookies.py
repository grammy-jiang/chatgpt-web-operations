#!/usr/bin/env python3
"""Which chatgpt.com cookies decrypt, and which do not. Read-only.

    probe_cookies.py [--browser chrome]

Run this first when a session will not open. A failure here looks exactly
like an account block from the outside, and once did: the decryptor used to
exit the process over an *irrelevant* analytics cookie, so a run died with
"could not decode a decrypted cookie value" while the session cookie itself
was perfectly readable.

Values are never printed. Names and lengths only.

Exit 0 when the session cookie is readable, 1 when it is not.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from _common import ensure_venv, load_client, table

SESSION_PREFIX = "__Secure-next-auth.session-token"


def read_jar(db: Path) -> list[tuple[str, str, bytes]]:
    """(name, host, encrypted_value) for every chatgpt.com cookie."""
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        shutil.copy2(db, tmp.name)  # the live DB is locked by the browser
        con = sqlite3.connect(tmp.name)
        rows = con.execute(
            "SELECT name, host_key, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%chatgpt.com' ORDER BY name"
        ).fetchall()
        con.close()
    return rows


def classify(
    rows: list[tuple[str, str, bytes]], decrypt
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(readable, unreadable) table rows. Never returns a cookie's value."""
    good: list[tuple[str, ...]] = []
    bad: list[tuple[str, ...]] = []
    for name, host, enc in rows:
        try:
            value = decrypt(enc)
        except Exception as exc:
            bad.append((name, host, type(exc).__name__))
            continue
        good.append((name, host, f"{len(value)} chars"))
    return good, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="chrome")
    args = ap.parse_args(argv)

    cc = load_client()
    cs = cc._helpers()
    db, app = cs.BROWSERS[args.browser]
    if not Path(db).exists():
        print(f"no cookie DB for {args.browser} at {db}")
        return 1

    good, bad = classify(read_jar(Path(db)), cc._tolerant_decryptor(cs, app))
    print(table(good, ("cookie", "host", "value")))
    if bad:
        print("\nunreadable:")
        print(table(bad, ("cookie", "host", "why")))

    have_session = any(name.startswith(SESSION_PREFIX) for name, _, _ in good)
    print(f"\n{len(good)} readable, {len(bad)} unreadable")
    if not have_session:
        print("The session cookie is NOT readable. That is the fatal one.")
        return 1
    print("The session cookie is readable, so an unreadable analytics cookie")
    print("here is noise: it must never end a run.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
