#!/usr/bin/env python3
"""Create a ChatGPT project, and report the API call that did it.

    create_project.py "msgloom research workers" [--visible] [--dry-run]

A project keeps a run's worker conversations out of the user's main chat
list. There is no documented endpoint for creating one, so this drives the
same control a person would and records what the page sent: the captured
request is evidence, where a hand-written POST body would be a guess.

It takes a browser slot, so it can never open a second window beside a run's
send. It refuses to act while ChatGPT's rate-limit modal is up, because that
modal means stop and clicking past it is what earns a longer limit.

``--dry-run`` opens the flow and reports what it found without creating
anything.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

from _common import ensure_venv, load_client

RATE_LIMIT_MODAL = '[data-testid="modal-conversation-history-rate-limit"]'
NEW_PROJECT = 'button[aria-label="New project"]'
# The dialog has no role; its name field is the only visible text input.
NAME_FIELD = 'input[type="text"]:visible, input:not([type]):visible'
SUBMIT_LABELS = ("Create project", "Create", "Confirm", "Done")


def mutations(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The non-GET backend-api calls, which is where a creation shows up."""
    return [c for c in calls if c["method"] != "GET" and "backend-api" in c["url"]]


def created_project(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The call that returned a new ``g-p-…`` id, if one did."""
    for call in mutations(calls):
        body = call.get("response") or ""
        if "g-p-" in body and call.get("status") in (200, 201):
            return call
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", help="the project's name")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cc = load_client()
    cs = cc._helpers()
    sys.path.insert(0, str(cc.HELPERS))
    import chatgpt_cookies  # type: ignore[import-not-found]
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    cc._patch_cookie_export(cs, chatgpt_cookies)
    cookies = chatgpt_cookies.export(cs.pick_browser("chrome"))
    calls: list[dict[str, Any]] = []

    def on_request(req: Any) -> None:
        if "backend-api" not in req.url:
            return
        body = None
        if req.method in ("POST", "PATCH", "PUT"):
            try:
                body = req.post_data
            except Exception:
                body = None
        calls.append(
            {"method": req.method, "url": req.url, "body": body, "status": None}
        )

    def on_response(res: Any) -> None:
        for call in reversed(calls):
            if call["url"] == res.url and call["status"] is None:
                call["status"] = res.status
                try:
                    if "json" in (res.headers.get("content-type") or ""):
                        call["response"] = json.dumps(res.json())[:800]
                except Exception:
                    pass
                return

    created: str = ""
    with cc.browser_slot(), cc.virtual_display(args.visible), sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome", headless=False, args=list(cc.BrowserSender.LAUNCH_ARGS)
        )
        ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(
            "https://chatgpt.com/", wait_until="domcontentloaded", timeout=120_000
        )
        page.wait_for_timeout(8_000)

        if page.locator(RATE_LIMIT_MODAL).count():
            browser.close()
            print(
                "ChatGPT's rate-limit modal is up. That modal means stop, and\n"
                "clicking past it earns a longer limit. Try again in a few minutes."
            )
            return 2

        opener = page.locator(NEW_PROJECT)
        # The sidebar renders after its own API calls, and on a loaded host
        # that takes longer than any fixed pause: at load average 6 the
        # control was absent after 8 s and the report read as "the sidebar
        # wording changed". Wait for the control itself.
        with contextlib.suppress(Exception):
            opener.first.wait_for(state="attached", timeout=60_000)
        if not opener.count():
            browser.close()
            print(f"no control matching {NEW_PROJECT}; the sidebar wording changed")
            return 1
        # The control is a trailing button on the sidebar's Projects header,
        # carrying can-hover:opacity-0. A plain click is intercepted and the
        # app never sees it, which looks exactly like a changed flow; a forced
        # click opens the dialog. Hover first so the button is painted.
        with contextlib.suppress(Exception):
            opener.first.hover(timeout=10_000)
            page.wait_for_timeout(400)
        opener.first.click(timeout=30_000, force=True)
        page.wait_for_timeout(4_000)

        # The modal carries no role="dialog"; looking for one finds nothing
        # and reads as "the flow changed". Target the name field directly: it
        # is the only visible text input, the composer being a textarea and
        # the upload controls being type=file.
        box = page.locator(NAME_FIELD).first
        if not box.count():
            with contextlib.suppress(Exception):
                page.screenshot(path="/tmp/create-project-no-field.png")
            browser.close()
            print(
                "no project-name field appeared. A screenshot is at "
                "/tmp/create-project-no-field.png; look at it rather than "
                "guessing at selectors."
            )
            return 1
        box.fill(args.name, timeout=30_000)
        page.wait_for_timeout(800)

        if args.dry_run:
            browser.close()
            print(f"dry run: would create {args.name!r}")
            return 0

        for label in SUBMIT_LABELS:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count() and button.first.is_enabled():
                button.first.click(timeout=30_000)
                break
        else:
            browser.close()
            print(f"no submit button among {SUBMIT_LABELS}")
            return 1

        page.wait_for_timeout(6_000)
        created = page.url
        browser.close()

    print(f"landed on: {created}")
    call = created_project(calls)
    if call:
        print(f"\nthe call that created it:\n  {call['method']} {call['url']}")
        if call.get("body"):
            print(f"  body: {call['body'][:400]}")
        print(f"  -> {call['status']}  {str(call.get('response'))[:200]}")
    else:
        print("\nno call returned a g-p- id; the mutations seen were:")
        for c in mutations(calls):
            print(f"  {c['method']} {c['url']} -> {c['status']}")
    return 0


if __name__ == "__main__":
    ensure_venv()
    raise SystemExit(main(sys.argv[1:]))
