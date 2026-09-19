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
