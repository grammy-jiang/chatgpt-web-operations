#!/usr/bin/env python3
"""Record which endpoints chatgpt.com calls, by watching a real page.

    discover_endpoints.py [--seconds 20] [--match backend-api] [--globals PATTERN]
                          [--visible]

This is how the transport map was built, and how to rebuild it when ChatGPT
changes. It opens a logged-in page with the profile's own cookies, records
every request the page makes, and reports the distinct endpoints with their
methods. Nothing is typed and nothing is sent, so it never posts a message.

``--globals`` also dumps the page's window keys matching a pattern, which is
how the ``__oai_so_*`` behavioural collector was found.

See references/endpoint-discovery.md for what to do with the output.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import time
from collections import defaultdict
from typing import Any

from _common import ensure_venv, load_client, table


def summarise(seen: list[tuple[str, str]]) -> list[tuple[str, ...]]:
    """(method, path, count) per distinct endpoint, query strings stripped.

    Ids are replaced by a placeholder so one conversation's traffic does not
    fill the report with near-duplicates.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for method, url in seen:
        path = url.split("?", 1)[0]
        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{20,}", "/<id>", path)
        path = re.sub(r"/[0-9a-f]{32,}", "/<hash>", path)
        counts[(method, path)] += 1
    return [
        (method, path, str(n))
        for (method, path), n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=20, help="how long to watch")
    ap.add_argument("--match", default="backend-api", help="substring of the URL")
    ap.add_argument("--url", default="https://chatgpt.com/")
    ap.add_argument("--globals", default="", help="regex over window keys")
    ap.add_argument(
        "--bodies",
        action="store_true",
        help="also print what POST/PATCH requests sent, which is what you "
        "need to reproduce a mutation such as creating a project",
    )
    ap.add_argument("--visible", action="store_true", help="on-screen, not Xvfb")
    args = ap.parse_args(argv)

    cc = load_client()
    cs = cc._helpers()
    sys.path.insert(0, str(cc.HELPERS))
    import chatgpt_cookies  # type: ignore[import-not-found]
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    cc._patch_cookie_export(cs, chatgpt_cookies)
    cookies = chatgpt_cookies.export(cs.pick_browser("chrome"))

    seen: list[tuple[str, str]] = []
    bodies: list[tuple[str, str, str]] = []
    globals_found: list[str] = []

    with cc.virtual_display(args.visible), sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome", headless=False, args=list(cc.BrowserSender.LAUNCH_ARGS)
        )
        ctx = browser.new_context(
            viewport={
                "width": cc.BrowserSender.WIDTH,
                "height": cc.BrowserSender.HEIGHT,
            }
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()

        def record(request: Any) -> None:
            if args.match not in request.url:
                return
            seen.append((request.method, request.url))
            # A body says what the page *sent*, which is what you need to
            # reproduce a mutation. Reading one costs a call into the driver,
            # so only the methods that carry one are asked.
            if args.bodies and request.method in ("POST", "PATCH", "PUT"):
                with contextlib.suppress(Exception):
                    body = request.post_data
                    if body:
                        bodies.append((request.method, request.url, body[:1200]))

        page.on("request", record)
        page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
        time.sleep(args.seconds)
        if args.globals:
            globals_found = page.evaluate(
                "(p) => Object.keys(window).filter(k => new RegExp(p, 'i').test(k))",
                args.globals,
            )
        browser.close()

    print(f"watched {args.url} for {args.seconds} s, matching {args.match!r}\n")
    print(table(summarise(seen), ("method", "endpoint", "calls")))
    print(f"\n{len(seen)} matching request(s)")
    if args.bodies:
        print(f"\n{len(bodies)} request(s) carried a body:")
        for method, url, body in bodies[:12]:
            print(f"\n  {method} {url}")
            print(f"    {body}")
    if args.globals:
        print(f"\nwindow keys matching {args.globals!r}: {len(globals_found)}")
        for key in globals_found[:40]:
            print(f"  {key}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
