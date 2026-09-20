#!/usr/bin/env python3
"""What one send window costs: memory, and time to a usable composer.

    measure_window.py [--fill-file PATH] [--visible]

Opens a window, optionally fills the composer with a real prompt, and closes
it. Nothing is sent. Use it before changing launch flags, and to re-check the
budget on a host whose other load has changed.

``--fill-file`` is the honest test: an idle composer costs far less than one
holding a large prompt, and it was the idle number that once understated a
real send by half a gigabyte.

Both paths go through the sender's public methods -- ``probe_composer`` to
load the page and wait for its composer, ``fill_composer`` to fill it the
way a send's paste path does, under the send's own budget. Until
2026-09-20 this called the sender's private ``_composer`` on a window that
nothing had navigated (``__enter__`` opens on about:blank; the navigation
lives in the send), so with ``--fill-file`` it waited 60 s for a composer
on an empty page and then reported a login problem, and without it the
"idle" number was a blank window's, not a composer's. Nobody had run it
since the navigation moved (references/failure-atlas.md).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

from _common import ensure_venv, load_client


def browser_rss_mb() -> float:
    """Total resident memory of every chrome and Xvfb process, in MB."""
    out = subprocess.run(["ps", "-eo", "rss=,args="], capture_output=True, text=True)
    total = 0
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and ("chrome" in parts[1][:150] or "Xvfb" in parts[1][:60]):
            total += int(parts[0])
    return total / 1024


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fill-file", default="", help="a prompt to type in")
    ap.add_argument("--visible", action="store_true")
    args = ap.parse_args(argv)

    cc = load_client()
    text = Path(args.fill_file).read_text(encoding="utf-8") if args.fill_file else ""

    base = browser_rss_mb()
    peak = [base]
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(0.4):
            peak[0] = max(peak[0], browser_rss_mb())

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    started = time.monotonic()
    fill_seconds = 0.0
    try:
        with cc.BrowserSender("chrome", visible=args.visible) as sender:
            launched = time.monotonic() - started
            if text:
                ready, fill_seconds = sender.fill_composer(text)
                opened = launched + ready
            else:
                sender.probe_composer()
                opened = time.monotonic() - started
                time.sleep(3)
    finally:
        stop.set()
        watcher.join(timeout=2)

    print(f"baseline           {base:8.1f} MB")
    print(f"peak               {peak[0]:8.1f} MB   (+{peak[0] - base:.1f})")
    print(f"composer ready in  {opened:8.1f} s")
    if text:
        rate = fill_seconds / max(len(text) / 1000, 1)
        budget = cc.fill_budget_ms(len(text)) / 1000
        print(f"filled {len(text):,} chars in {fill_seconds:.1f} s  ({rate:.2f} s/kB)")
        print(f"A send would give this fill {budget:.0f} s; compare against that.")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
