"""Shared plumbing for the chatgpt-web-operations commands.

Deliberately thin. Each command owns its own logic; this module only removes
the lines every one of them would otherwise repeat: run inside the skill's
own virtual environment, import the bundled client, open a session, and print
a table.

Everything is resolved from this file's own location, so the skill directory
can be moved as a whole. Nothing here knows where any repository is checked
out, and nothing needs to.

Nothing here talks to the network, so the commands stay unit-testable: they
are written as plain functions over data, with a ``main`` that supplies the
live session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent
VENV = SKILL_DIR / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
BOOTSTRAP = SKILL_DIR / "bootstrap.sh"
REEXEC_FLAG = "CHATGPT_WEB_OPS_REEXEC"


def ensure_venv() -> None:
    """Re-run the current command under the skill's own virtual environment.

    The commands need Playwright and the cookie decryptor's dependencies, which
    live in ``.venv`` beside ``scripts/`` rather than in whichever python
    happened to start the script. Call this first in every ``__main__`` block:
    it is a no-op inside the venv, and a clear instruction when the venv is
    missing. Tests import the modules without going through here.
    """
    if os.environ.get(REEXEC_FLAG):
        return  # re-executed once already; never loop
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    if not VENV_PYTHON.exists():
        raise SystemExit(
            f"no virtual environment at {VENV}\ncreate it once with: bash {BOOTSTRAP}"
        )
    env = dict(os.environ, **{REEXEC_FLAG: "1"})
    # exec replaces the process; anything still buffered would be lost.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(
        str(VENV_PYTHON),
        [str(VENV_PYTHON), os.path.abspath(sys.argv[0]), *sys.argv[1:]],
        env,
    )


def load_client() -> Any:
    """The bundled HTTP/browser client module."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import chatgpt_client  # type: ignore[import-not-found]

    return chatgpt_client


def open_session(browser: str = "chrome") -> Any:
    """An authenticated HTTP session, or exit with a clear reason."""
    cc = load_client()
    try:
        return cc.ChatGPTSession(browser)
    except Exception as exc:
        print(f"could not open a session: {str(exc)[:200]}")
        raise SystemExit(1) from exc


def table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    """A plain aligned table. Reports are read by people, not parsers."""
    if not rows:
        return "(nothing)"
    widths = [
        max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))
    ]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    out.append("  ".join("-" * w for w in widths))
    out.extend(
        "  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True))
        for row in rows
    )
    return "\n".join(out)


def shorten(text: str, limit: int = 70) -> str:
    """One line, bounded, for a table cell."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
