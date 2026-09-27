# Fixtures

Real backend-api payloads, sanitized before they ever land here. Record one
by hand, never from an agent or a test:

    .venv/bin/python tests/record_fixture.py <backend-api path> <name>

That GETs `<path>`, runs the body through `record_fixture.sanitize()`
(emails, `user-`/`org-`/`g-p-` ids, and free-text fields all replaced), and
writes `tests/fixtures/<name>.json`.

The rule: `tests/test_fixture_hygiene.py` scans every file here on each
test run and fails the suite if an unsanitized email, id, or the account
holder's name is still in it. That check reads what is on disk, not what
`sanitize()` claims to have done, so a hand-edited fixture is still caught.

`--keep KEY` leaves a normally redacted key as text when its values are
public; `--redact KEY` adds a key for one fixture. Recorded so far
(2026-09-20): `user_system_messages`, `memories_summary`
(`memories?include_memory_entries=false`), `gizmo_sandbox`
(`gizmos/<the rp-test-sandbox id>`), `settings_user`, `models` (with
`--keep title`, the preset titles are public), `system_hints_basic`;
`wham_usage` was written by hand from a live shape with the values changed.

## `dom/`: recorded pages for the replay tier

`dom/<name>.html` is a page ChatGPT served, reduced to what a selector can
see (`chatgpt_client.sanitize_html`: tag tree, `id`, `class`, `role`,
`aria-*`, `data-*` and a few form attributes; no text node, no URL, no
script, no image). `dom/<name>.json` holds the facts recorded with it
(`page_snapshot`: which selector alternatives matched, the send button, the
turn counts, the URL, when). `tests/replay` loads the `.html` into a real
headless Chrome and runs the client's page functions on it; `preflight.py
--browser` with `RP_SNAPSHOT_DIR` records a fresh pair every day and warns
when the facts drift from `dom/composer.json`.

Promote a recording with `make refresh-dom-fixtures` (default: the newest
daily run's `dom/`; `FROM=<dir>` for another), which re-sanitizes it and
refuses anything the hygiene test would fail. Never from cron. Files whose
name ends in `-synthetic` are hand-written, not recorded, and are the only
ones allowed to carry text: `composer-legacy-synthetic` is the pre-2026-09-26
composer, `chat-load-failed-synthetic` the "Could not load this ChatGPT
conversation" notice. `composer.*` is the daily check's recording;
`conversation.*` and `composer-filled.*` come from the weekly send
(TESTING.md section 6, P3); `composer-attached.*` is the composer after a
real upload (2026-09-27), the page the T3 upload test's `verify()` reads.
