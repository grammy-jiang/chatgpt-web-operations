"""Read-only macOS Host MCP ChatGPT Skill readiness check.

macOS cannot run the Linux /proc, Xvfb or GNOME D-Bus checks. Validate the
actual dedicated Chrome CDP, the authenticated HTTP session, and optionally
a Chat Mode composer, without storing or printing credentials.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from macos_cdp_auth import endpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--browser-name", default="chrome")
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    checks: list[dict[str, str]] = []

    def record(group: str, name: str, ok: bool, detail: str) -> None:
        checks.append(
            {
                "group": group,
                "name": name,
                "state": "ok" if ok else "block",
                "detail": detail,
            }
        )

    record(
        "host",
        "platform",
        sys.platform == "darwin",
        f"{platform.system()} {platform.machine()}",
    )
    record(
        "host",
        "python",
        sys.version_info >= (3, 12),
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
    )
    if args.browser_name != "chrome":
        record("host", "browser", False, "Only dedicated macOS Chrome supported")
    try:
        url = endpoint()
        if not url:
            raise ValueError("CDP not configured")
        with urllib.request.urlopen(url + "/json/version", timeout=5) as res:
            metadata = json.load(res)
        browser_version = metadata.get("Browser", "")
        record(
            "host",
            "Chrome CDP",
            bool(browser_version.startswith("Chrome/")),
            browser_version,
        )
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        url = None
        record("host", "Chrome CDP", False, type(error).__name__)

    if args.workdir:
        p = Path(args.workdir).expanduser().resolve()
        ancestor = p
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        free = shutil.disk_usage(ancestor).free
        record(
            "host", "disk", free >= 4 * 1024**3, f"available GiB: {free // (1024**3)}"
        )

    if args.project and not re.fullmatch(r"g-p-[A-Za-z0-9_-]{8,128}", args.project):
        record("account", "project format", False, "Invalid project id format")

    if url:
        try:
            import chatgpt_session as cs

            cs.set_prog("mac-preflight")
            session = cs.Session(
                browser="chrome", request_timeout=14, default_retries=0
            )
            record(
                "account",
                "ChatGPT auth",
                bool(session.token and session.user_id),
                "Authenticated HTTP session",
            )
        except (OSError, RuntimeError, ValueError, SystemExit) as error:
            record("account", "ChatGPT auth", False, type(error).__name__)

        if args.browser:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(url, timeout=12000)
                    page = None
                    try:
                        if len(browser.contexts) != 1:
                            raise RuntimeError("Wrong Chrome context count")
                        page = browser.contexts[0].new_page()
                        page.goto(
                            "https://chatgpt.com/",
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                        composer = page.get_by_role("textbox")
                        composer.first.wait_for(timeout=18000)
                        active = page.url.startswith("https://chatgpt.com/")
                        record(
                            "browser",
                            "Chat Mode composer",
                            active and composer.count() >= 1,
                            "Authenticated ChatGPT input available",
                        )
                    finally:
                        if page is not None:
                            page.close()
                        browser.close()
            except Exception as error:
                record("browser", "Chat Mode composer", False, type(error).__name__)

    verdict = "GO" if all(c["state"] == "ok" for c in checks) else "DO NOT START"
    for group in ["host", "account", "browser"]:
        chosen = [c for c in checks if c["group"] == group]
        if not chosen:
            continue
        print(f"== {group} ==")
        for row in chosen:
            print(f"  [{row['state']:5s}] {row['name']:<23s} {row['detail']}")
    print(verdict)
    if args.json:
        output = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "verdict": verdict,
            "exit_code": 0 if verdict == "GO" else 1,
            "checks": checks,
            "credential_values_saved": False,
        }
        p = Path(args.json).expanduser()
        if not p.parent.is_dir():
            raise SystemExit("mac-preflight: output directory must exist")
        p.write_text(json.dumps(output, indent=2) + "\n")
        p.chmod(0o600)
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
