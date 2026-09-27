# Test plan: chatgpt-web-operations

Updated 2026-09-27 (section 6: the audit and the plan for the next test
work). Current results are in `VERIFICATION.md`. The bar the user set: every core module at 95% line
coverage or more and every other module at 90% or more, measured per module
and never as an average; several kinds of tests, not only unit tests; and no
test may touch the user's ChatGPT workspace unless it was opted in, in which
case it removes what it created.

## 1. Tiers: what a test may touch

| Tier | Marker | Opt-in variable | May touch | In `make test` |
|------|--------|-----------------|-----------|----------------|
| T0 | none | — | nothing outside this directory: no network, no browser, no cookie DB, no keyring | yes |
| T1 | `live_read` | `CHATGPT_LIVE=read` | the real account, `GET` and the allowlisted read-only search POST; also this machine's own keyring (session-token renewal, `chatgpt_session.py` "Session token renewal") -- the one local write any live tier makes, tests/live/test_read_session_renewal.py | no |
| T2 | `live_write` | `CHATGPT_LIVE=write` | the sandbox project only: its own gizmo id and conversations inside it | no |
| T3 | `live_browser` | `CHATGPT_LIVE=browser` | one Chrome window on the sandbox project; the composer is filled, send is never clicked | no |
| T4 | `live_send` | `CHATGPT_LIVE=send` | real sends inside the sandbox project, deleted in teardown, at most `CHATGPT_LIVE_SENDS` per run (default 4) | no |
| R | `replay` | none; `make replay` | a real headless Chrome on the recorded pages in `tests/fixtures/dom`, served through a Playwright route; no account, no network (the Python side is held to loopback by `tests/conftest.py`) | no |

Enforcement, not promises:

- `tests/conftest.py` has an autouse fixture that replaces `socket.socket`
  with one that raises for any test without a `live_*` marker. A T0 test
  that reaches the network fails.
- A live marker without its `CHATGPT_LIVE` value skips and prints why. Both
  are needed, so `pytest` alone can never reach the account.
- `tests/live/guard.py` wraps `session.session.call`. In T1 mutations raise before the request is made. The allowlist also
  permits `POST /backend-api/global/search`, which only reads. In T2 to T4 a non-`GET` is allowed only
  when its path names the sandbox gizmo or a `conversation/<id>` whose id the
  guard has seen in `gizmos/<sandbox>/conversations` or created itself during
  the test. Everything else raises. The guard has its own T0 tests over fake
  paths.
- The live suite reuses one authenticated HTTP transport per tier. Each
  test gets a separate guard. Token-renewal tests explicitly create their
  own sessions when that behavior is the subject of the test.
- The sandbox project is `rp-test-sandbox`, created once with
  `create_project.py` and kept; its id lives in `tests/live/sandbox.json`.
  Before any T2 to T4 test runs, the suite reads `gizmos/<id>` and refuses to
  continue unless the name is exactly `rp-test-sandbox`, so a stale id can
  never point the tests at a real project.
- Teardown deletes every conversation a test created, in `finally`. A
  session-scoped fixture then sweeps the sandbox: it pages through
  `gizmos/<sandbox_id>/conversations`, notes every id with the guard and
  PATCHes each one `is_visible: false`, whatever its title (an earlier
  version kept only `rp-test …` titles, and ChatGPT's own auto-title after
  the first reply let renamed chats escape it). It prints what it deleted;
  `--keep-sandbox-chats` disables it and prints what stayed.
- Every T4 test makes exactly one send, so the send cap is counted in
  tests: an autouse fixture in `tests/live/conftest.py` counts each
  `live_send` test before its body runs and **fails** the run past the cap.
  It fails rather than skips, because a test that quietly does not run
  reads as a pass -- which is exactly how `test_send_chat_flags.py` went
  unrun. A `CHATGPT_LIVE_SENDS` that is not a positive whole number falls
  back to the default, so a typo can never lift the cap.
- Browser send paths use the client's process/memory budget. A history
  rate-limit notice alone is not a send failure. Tests must not claim an
  account block merely because that notice is present. Check for an active
  research dispatch before running a suite. `discover_endpoints.py` uses a
  separate diagnostic browser; run it when no send is in flight.
- Fixtures recorded from the account go through `tests/record_fixture.py`,
  which replaces ids, emails, names and instruction texts before writing. A
  T0 test scans `tests/fixtures/` for `@`, `user-`, `org-`, any `g-p-` id
  outside an allowlist, and the account's email, and fails on a hit.

## 2. Kinds of tests

| Kind | Tier | What it proves | Example |
|------|------|----------------|---------|
| unit | T0 | one function's decision over data, with fakes for session, page and clock | the 77 tests in `tests/test_commands.py` |
| fixture (contract) | T0 | the parsers read the real payload shapes, recorded and sanitized | `user_system_messages`, a sidebar page with a `cursor`, `wham/usage`, a finished and an unfinished conversation |
| CLI | T0 | commands with argparse have `--help` that exits 0; `review_topic.py` accepts topic folders directly; `ensure_venv` re-executes once and only once; `python3 scripts/<cmd> --help` works from another directory (subprocess) | catches `probe_send_gates.py` running the probe on `--help` |
| consistency | T0 | `SKILL.md`'s command table matches `scripts/*.py`; every ChatGPT/Platform tunnel endpoint constant appears in `references/endpoint-discovery.md`, `references/connectors.md`, `references/tunnels.md`, or `SKILL.md`; no code outside `chatgpt_client.py` reaches into a sender's private members (docstrings and comments excepted) | a command cannot be added without its row |
| regression | T0 | one test per `failure-atlas.md` entry, named after it | "one unreadable cookie must not end the session" |
| safety | T0 | the properties the user relies on: a dry run never mutates; read commands issue no mutations (read-only POSTs are allowed); `BrowserSender` dismisses the conversation-history rate-limit notice and attempts the send, and sends Escape only to other modals; the live guard rejects an id outside the sandbox; the fixture scanner finds a planted email | |
| robustness | T0 | every parser survives `{}`, `None` fields, wrong types and half-written messages | parametrized |
| smoke | T1 | each read command exits as documented against the real account; assertions on shape, never on the user's data | `profile_context.py` exits 0; the slider has 5 positions; `tests/live/test_read_preflight.py`: preflight's account and run groups over the guarded session, and `main()` exiting as documented with its eleven default checks in order |
| round trip | T2 | each write command: act, read back, revert; the cleanup is verified by a read. A round trip that needs a conversation to act on mints one (`tests/live/minting.py`) and is therefore T4, not T2: a test that waits for a chat an earlier run left never runs, because the sweep deletes them | set instructions on the sandbox, read `gizmos/<id>`, restore |
| browser dry run | T3 | the send path opens: window, cookies, composer found, upload works; never sends | `tests/live/test_browser_upload.py`: `BrowserSender.attach_files` uploads one file, the composer shows its `Remove file …` chip and the send button stays enabled, never clicked |
| measured send | T4 | the send path end to end, one message per feature, timings recorded in `failure-atlas.md` | search on and off, one attachment, one deep research; `tests/live/test_send_effort.py`, which pins a level the account is **not** already using and asserts the reply recorded it -- the check that would have caught the two-month effort regression; `tests/live/test_send_chat_flags.py`, which mints a chat and round-trips pin, unpin, archive and unarchive on it; and `tests/live/test_send_cli_roundtrip.py`, the scripted end-to-end send through `send_prompt.main` itself (posted, replied with the nonce, deleted and 404), which also records the `conversation` and `composer-filled` pages for tier R and runs weekly from cron (`~/.local/bin/chatgpt-ops-send-check.sh`, Sunday 05:40) |
| recorded page | R | the client's page functions (`find_composer`, `composer_state`, `chat_load_failure`, `page_snapshot`, `text_taken` on a fill) run by a real Chrome against the DOM ChatGPT actually served, as recorded by the daily browser check and promoted into `tests/fixtures/dom`; a UI change fails here offline once the new recording is promoted | `tests/replay/test_dom_replay.py`: the composer selector matches exactly one element on every recorded and synthetic composer; a fill is read back; the "could not load" notice is detected; a snapshot of a snapshot keeps every fact the selectors depend on |

## 3. Coverage gate

Core, 95% or more each: `chatgpt_client.py`, `chatgpt_session.py`,
`chatgpt_cookies.py`, `_common.py`, `round_state.py`, the bundled library
every command depends on. Other, 90% or more each: every command in
`scripts/`. `tests/` and `bootstrap.sh` are not measured.

`pytest --cov=scripts --cov-report=json`, then `tests/coverage_gate.py`,
which fails on the first module under its threshold and prints all of them.
`.coveragerc` excludes only `if __name__ == "__main__":` blocks: they run
the venv switch and are exercised by the subprocess CLI tests, which
coverage cannot see without process tracking. `make test` runs T0 and the
gate, `make lint` runs ruff, and `make live-read`, `make live-write`,
`make live-browser`, `make live-send` set the variable and the marker;
`make fmt` formats with ruff and `make all` runs lint then test. `make
replay` runs tier R (excluded from `make test`, because it needs Chrome),
and `make refresh-dom-fixtures` promotes the newest recorded page into
`tests/fixtures/dom` (`FROM=<dir>` for another recording).

## 4. Current verification procedure

1. Confirm that no research dispatch or scripted send is in flight.
2. Run `make test` and `make lint`. Fix actual failures before treating the
   offline baseline as passing. `make fmt` formats; `make all` combines the
   lint and test targets.
3. Run `make live-read`, `make live-write`, `make live-browser` and
   `make live-send` sequentially. Inspect skips and errors; a collected or
   skipped test is not a live pass.
4. Check CLI composition beyond the existing live suite: batch sends,
   continuation, explicit model selection, multiple attachments, web search,
   request/stream records, Deep research and research-round collection.
5. For connector acceptance, create a uniquely named disposable custom app
   on an existing authorized tunnel. Connect it, verify tools/privacy and
   listing/details, perform a harmless nonce call, then delete its links and
   app. Verify both listings are clear. The permanent live pytest guard does
   not allow connector mutations; use an explicit run-owned object ledger.
6. Record each result and all unverified limits in `VERIFICATION.md`.
   Keep raw account payloads, cookies and credentials out of tracked files.

The regular T4 suite currently has two tests, each with one send. Additional
manual acceptance sends are counted separately in the verification record;
the T4 test-count budget must not be described as covering those sends.

## 5. Coverage and regression expectations

Added 2026-09-27 (`PLAN-2026-09-27.md`, section 5 B):

- **Real-path boundary tests:** no injected fake at an external boundary.
  The first one is the cookie-jar test with real AES values (`7364386`).
- **Recorded fixtures:** refreshed by the daily browser check.
- **Scripted end-to-end send:** into `rp-test-sandbox`.
- **Flaky tests:** fixed or quarantined within a week.

The owner's coverage rule is per module, never averaged: 95 % for each core
module and 90 % for each other module.

The four connector modules have offline CLI and failure-path tests in
`tests/test_connectors.py`. They include failed privacy writes, failed link
cleanup, non-mutating previews, malformed responses, lookup errors and
CLI discovery. A failed link delete must prevent app deletion. A failed or
mismatched privacy update must return a nonzero exit status.

The health live test accepts the production authentication options and
checks all fifteen current checks, including the skills inventory. Historic
comments saying that the live tests were never run have been removed.

The 2026-09-20 baseline counts and coverage figures remain in git history.
They are not the current coverage result. `make test` and
`tests/coverage_gate.py` are authoritative for the current tree.

`tests/test_tunnels.py` covers Platform tunnel management. It checks
read-back verification, single-attempt writes, default previews, name guards,
credential sources, owner-only env files, and non-disclosure of failed token
provider output. It also checks that HTTP redirects cannot forward the
Authorization header. Tunnel mutation acceptance must use one newly created
disposable tunnel and record its id before the update/delete checks. The
regular ChatGPT live tiers do not authorize or perform Platform mutations.

## 6. Audit of 2026-09-27 and the plan for the next test work

Measured on 2026-09-27 (evening) on tree `57094f3`, after the owner asked
for a status check and a plan with more test cases and more kinds of tests.
Every number below comes from a command run that evening; nothing is
carried over from an older record.

### 6.1 What exists

| Measure | Value on 2026-09-27 |
| --- | --- |
| `make test` | 1,792 passed in 11.5 s; 28 live tests deselected |
| Coverage gate | every module OK. Core: `_common.py` 100 %, `chatgpt_client.py` 99.5 %, `chatgpt_cookies.py` 100 %, `chatgpt_session.py` 98.4 %, `round_state.py` 100 %. Lowest other module: `review_topic.py` 91.8 % |
| `make lint` | clean, 91 files |
| Live tests | T1: 8 files. T2: 2 files. T3: 1 file. T4: 2 files, one send each |
| Daily canary | `~/.local/bin/chatgpt-ops-check.sh --browser` at 05:25 runs `health.py --browser` and `make test`; 11 evidence directories since 2026-09-24, every `tests.log` green |
| Recorded fixtures | 7 recorded HTTP payloads and 1 hand-written one; no DOM snapshot |
| Test libraries | pytest and pytest-cov only |

The kinds in section 2 all exist. The harness has 74 tests of its own.

### 6.2 What the suite could not see

Each gap names the measurement behind it.

1. **The browser boundary is faked end to end offline.** `tests/fake_playwright.py`
   answers whatever `set_locator(...)` was told. `COMPOSER_SELECTOR`,
   `USER_TURN_SELECTOR`, `SEND_BUTTONS`, `COMPOSER_STATE_JS` and the
   chat-load notice regex meet real DOM only in T3 (opt-in) and in the daily
   `--browser` probe, which checks one thing: a composer appears. The
   composer change of 2026-09-26 passed every offline test.
2. **Fifteen endpoints have no recorded fixture.** Seven payloads are
   recorded (`tests/fixtures/README.md`). The conversations list and detail,
   `pins`, `hazelnuts`, `ps/plugins/installed`, `automations`,
   `global/search`, `links/list_accessible`, `connectors/batch`,
   `mcp/tunnels`, `me` and `api/auth/session` are parsed against dicts
   written by hand in the tests. No test says which endpoints lack one.
3. **A submit failure leaves no durable evidence.** `BrowserSender._send`
   writes `chatgpt-send-fail.png` to `/tmp`, which does not survive a
   reboot, and raises. Nothing counts "not posted". One occurrence
   (2026-09-27) is on record.
4. **The real send path has no scheduled run.** T4 is opt-in and manual.
   The daily check never sends, by design. Between manual runs the send
   path is unmeasured.
5. **Local boundaries have no tier.** T0 forbids the cookie DB and the
   keyring, correctly. Only T1, which needs the network, reaches them for
   real. "The session cookie decrypts on this machine" needs no network and
   has no home.
6. **Suite hygiene.** No random test order, no per-test timeout, no
   property-based tests, no mutation score, and no rule that lists which
   command has no live test (the preflight lesson of 2026-09-20: 31 tested
   functions had never met the account).

### 6.3 The plan

Three phases. Each item names its kind, its tier, what it proves, its exit
criterion and its cost. Phase 1 closes the gaps behind the incidents of
2026-09-26 and 2026-09-27. Phase 2 adds kinds of tests. Phase 3 belongs to
the one-client work in `PLAN-2026-09-27.md`, section 5 A.

#### Phase 1: close the incident gaps

- **P1. Recorded DOM snapshots and a replay tier.** Kind: recorded page.
  Tier: new, `replay`, in `make replay`; real Playwright and headless Chrome,
  no Xvfb, no network.
  - The client gets a public `BrowserSender.snapshot_page(dir)`. It saves
    `composer.html`, the outerHTML of the composer's form subtree after a
    sanitizer that drops text nodes, `src`, `href` and `value` and keeps
    tag names, `id`, `class`, `role`, `aria-*`, `data-*`, `contenteditable`,
    `placeholder`, `type` and `disabled`; and `snapshot.json`: the URL, which
    alternative of `COMPOSER_SELECTOR` matched, the send button's test id or
    label, the `COMPOSER_STATE_JS` result and the Chrome version.
  - `preflight.browser_composer_check` calls it when `RP_SNAPSHOT_DIR` is
    set; the daily wrapper passes its run directory. The daily run compares
    `snapshot.json` with `tests/fixtures/dom/composer.json` and reports WARN
    naming the keys that differ. `make refresh-dom-fixtures` promotes a
    snapshot into `tests/fixtures/dom/` (sanitize, then the hygiene test).
    A person or an agent commits it; cron never does.
  - The conversation-page snapshot (`conversation.html`, with turns) comes
    from the weekly send (P3), which owns a chat for a minute. A
    `chat-load-failed.html` is recorded when the notice is ever seen again.
  - `tests/replay/`: `page.route("https://chatgpt.com/**")` serves the
    fixture and aborts everything else; the T0 socket guard stays on
    (Playwright drives Chrome over pipes). The tests run the real code:
    `probe_composer()` finds exactly one composer; `COMPOSER_STATE_JS`
    finds the send button; `fill_composer()` puts a text into the ProseMirror
    snapshot and reads it back; `USER_TURN_SELECTOR` and
    `CHAT_TURN_SELECTOR` count the turns of `conversation.html`;
    `_chat_load_failure()` detects the notice fixture. The pre-2026-09-26
    composer (`#prompt-textarea`) stays as a second fixture, so both
    generations pass.
  - Limit: a static snapshot cannot emulate posting. The app's JavaScript
    creates the user bubble and the `/c/` URL. Posting stays with P3.
  - Exit: a fixture with renamed attributes fails `make replay`; the daily
    run writes a snapshot and compares it; `make replay` runs in the daily
    wrapper after `make test`. Cost: 20-30 s per run.
  - Done 2026-09-27 (evening): `snapshot_page`, `page_snapshot`,
    `sanitize_html`, `snapshot_drift` and the page functions in
    `chatgpt_client.py`; `preflight.browser_composer_check` records under
    `RP_SNAPSHOT_DIR` and compares with `tests/fixtures/dom/composer.json`;
    tier R (`tests/replay`, `make replay`); `make refresh-dom-fixtures`;
    the hygiene test covers `dom/`. The conversation-page fixture waits for
    P3's first run.
- **P2. Submit robustness and an event ledger** (`PLAN` B5). Kind: unit
  over fake pages, plus a durable record. Tier: T0.
  - After `fill`, read the composer text back (`textContent`, length and
    prefix) and read `send_enabled` from `COMPOSER_STATE_JS`. On a mismatch:
    select all, delete, `keyboard.insert_text`, read back again. Log which
    path was taken.
  - Every composer-missing, chat-load-failed, fill-fallback and not-posted
    becomes one line in `~/.local/state/chatgpt-web-operations/events.jsonl`
    (`RP_EVENTS_FILE`): time, kind, chat or URL, screenshot path, attempt.
    `screenshot_dir` moves from `/tmp` to
    `~/.local/state/chatgpt-web-operations/screenshots/` (`RP_SCREENSHOT_DIR`),
    so the evidence survives a reboot. `chatgpt-ops-check.sh --summary`
    counts events per kind per week.
  - Exit: on a fake page that swallows the click, `send_prompt.py` writes
    one event and takes the fallback; each branch has a test; the weekly
    summary prints the count. Cost: none on the account.
- **P3. The scripted end-to-end send, weekly** (`PLAN` B4). Kind: measured
  send. Tier: T4, one send.
  - `tests/live/test_send_cli_roundtrip.py` runs `send_prompt.main([...])`
    with `open_session` patched to the guarded session, into the sandbox,
    with `--json`. Three facts: exit 0 and a chat id in the JSON; `read_chat
    --text` prints the reply to a nonce; teardown deletes the chat and
    `GET conversation/<id>` returns 404. The test also records
    `conversation.html` for P1.
  - Schedule: `chatgpt-ops-check.sh --send`, Sunday 05:40, `CHATGPT_LIVE=send
    CHATGPT_LIVE_SENDS=1`, mails on failure. Rule: run `make live-send` after
    any change to `BrowserSender` or `send_prompt.py`, before the commit.
  - Exit: the test passes once by hand and once from cron. Cost: one message
    per week and one per sender change. Default cadence is weekly; revisit
    if the account shows a rate limit on Sundays.
  - Done 2026-09-27 (evening): `tests/live/test_send_cli_roundtrip.py`
    passed by hand in 115 s and recorded `conversation` and
    `composer-filled` for tier R; the weekly wrapper is
    `~/.local/bin/chatgpt-ops-send-check.sh` (Sunday 05:40 through
    `cron-report`; WARN when a recorded page drifts from its fixture, ALERT
    when the send fails). Its first recording showed that `COMPOSER_STATE_JS`
    could not see the 2026-09-26 send button (`references/failure-atlas.md`,
    "Fixed bugs"); fixed the same evening.
- **P4. Live coverage per command.** Kind: consistency. Tier: T0 for the
  rule, T1 for the new tests.
  - A table in `tests/test_consistency.py`: every command maps to a live
    test function or to one reason: `manual-acceptance` (connectors, Deep
    research, discovery), `platform-credentials` (`manage_tunnels.py`),
    `offline-only` (`review_topic.py`), `browser-diagnostic`
    (`measure_window.py`). A T0 test fails on a missing command or on a
    named function that does not exist (checked with `ast`, not by
    collecting).
  - New T1 tests run `main()` over the guarded session: `delete_skill.py`
    (an unknown name exits 2 after one GET; an existing name dry-runs with
    exit 0 and no DELETE), `list_connectors.py --json`, `list_chats.py`,
    `list_projects.py --match rp-test`, `probe_account.py`,
    `probe_cookies.py`, `model_settings.py`, `probe_send_gates.py`,
    `profile_context.py --project <sandbox>`, `clean_chats.py --project
    <sandbox>` dry run. `read_chat.py` runs inside P3's minted chat.
  - Exit: all 30 commands have a row; no row says TODO.
- **P5. A recorded fixture for every endpoint, with shape-drift detection**
  (`PLAN` B2, HTTP half). Kind: contract. Tier: T0 for the tests; the daily
  HTTP run records.
  - `record_fixture.py --from-session`: the daily run records each read
    endpoint into `<run>/http/<name>.json` after sanitizing; `make
    refresh-fixtures` promotes into `tests/fixtures/http/`. Cron never
    commits.
  - A T0 consistency test: every endpoint constant in `scripts/*.py` (the
    set the endpoint-documentation test already computes) has a fixture and
    a contract test that parses it. Today 15 are missing (6.2, item 2).
  - Drift: the daily run compares the key set of the fresh payload with the
    committed fixture, values dropped, and reports WARN "shape drift" with
    the added and removed keys.
  - Exit: zero endpoints without a fixture; the drift check runs daily.

#### Phase 2: new kinds of tests

- **P6. Loopback replay: the real HTTP client against a local fake
  server.** Kind: integration. Tier: new, `loopback`, in `make loopback`;
  sockets to 127.0.0.1 only.
  - `chatgpt_session.BASE` reads `CHATGPT_BASE_URL`, documented as
    test-only; the default stays `https://chatgpt.com`.
  - `tests/loopback/server.py`: `http.server` on 127.0.0.1 serving the
    recorded fixtures by path, with a fault script: a 403 challenge once
    then 200; 429; 424; truncated JSON; a 20 s stall; `Set-Cookie` with
    `Max-Age=7776000` on `/api/auth/session`.
  - What runs for real: `Session._request` (urllib), the production retry
    ladder (30/90/180 s) and the health policy (5/10 s), the cookie header
    from a synthetic jar, renewal parsing, every command's `main()` with
    its exit code, and one subprocess run of `python3 scripts/list_chats.py`
    (venv switch, argv, exit code).
  - Exit: the health timeout of 2026-09-23 (two 403s, then the ladder) is
    reproduced offline in under 10 s under both policies.
- **P7. A local real-path tier.** Kind: real-path boundary (`PLAN` B1).
  Tier: new, `live_local`, `CHATGPT_LIVE=local`, `make live-local`;
  read-only on this machine, no network.
  - Chrome's cookie DB opens (a copy, read-only) and the session cookie
    decrypts through the real `_keyring_password()` over D-Bus and real AES;
    `probe_cookies.py main()` exits 0; `load_stored_session()` returns a
    record with an expiry. No value is printed.
  - The daily wrapper runs it before `health.py`.
  - Exit: the empty `_dd_s` jar of 2026-09-27, replayed as a T0 fixture,
    fails the old decoder and passes the current one; the live tier passes
    daily.
- **P8. Property-based tests.** Kind: property. Tier: T0. Adds `hypothesis`
  to `requirements.txt`.
  - `tests/test_properties.py`, under 20 s, each property with an explicit
    oracle: cookie decoding never raises and is the identity on printable
    UTF-8; `choose_session` and `renewed_session` pick the later expiry for
    any pair; `rewrite_send_body` is idempotent and preserves unknown keys;
    `stream_events` accepts any concatenation of valid frames with garbage
    between them; `chat_id` and `is_provisional` on generated URLs;
    `record_fixture.sanitize` leaves no email, `user-`, `org-` or `g-p-`
    pattern (the hygiene regexes are the oracle); `coverage_gate.verdicts`
    is monotone in the percentage; `table()` column widths.
  - Exit: ten properties, a fixed seed in the daily run, a shrunk example
    printed on failure.
- **P9. Order independence, timeouts and the flaky ledger** (`PLAN` B6).
  Kind: hygiene. Tier: T0.
  - `pytest-randomly` (random order per run; `-p no:randomly` reproduces)
    and `pytest-timeout` (60 s per T0 test, 900 s per live test) in
    `requirements.txt`; `make test-repeat N=10`.
  - `tests/FLAKY.md`: date, test, symptom, cause, fix or quarantine,
    deadline one week. Quarantine is `@pytest.mark.quarantine(until=...,
    reason=...)`, and a T0 test fails once `until` has passed. The daily
    `tests.log` files are the record; 11 runs are green since 2026-09-24.
  - Exit: ten randomized repeats pass; the ledger exists, empty or not.
- **P10. Mutation testing pilot.** Kind: mutation score. Tier: by hand, never
  in cron.
  - `mutmut` in the venv, `make mutate MODULE=...`, first on
    `chatgpt_cookies.py`, `_common.py` and `round_state.py`, then
    `chatgpt_session.py`. Target: a kill rate of 80 % or more on core
    modules. Each surviving mutant gets a test or a one-line reason in
    `VERIFICATION.md`. Monthly, when no send is in flight.
  - Exit: the pilot's report is in `VERIFICATION.md` with the surviving
    mutants named. Cost: hours of CPU on the Pi.

#### Phase 3: tests that come with the one-client work

- **P11.** `refresh_connector.py` (`PLAN` A1): offline tests for name
  resolution (exact, unique substring, ambiguous, none), the 424 ladder
  with a fake clock, `--no-retry` and `--json`; a T1 test for `--list` and
  resolution (`POST links/list_accessible` joins `READ_POSTS`, it only
  reads); the real refresh in the manual connector acceptance (section 4,
  step 5) and in the weekly run of P3 when the tunnel is up.
- **P12.** `clean_chats.py --id ... --backup DIR` and `project_settings.py
  --name NAME` (`PLAN` A2): offline tests, and one T4 round trip: mint,
  back up, delete, 404.
- **P13.** Once binnacle's `chatgpt-*` names point at this skill (`PLAN`
  A3), the weekly run calls them by their installed names, because entry
  points are what break (`PLAN` B3).

### 6.4 Rules that come with the plan

- Cron never commits a fixture. Promotion is a reviewed step with a `make`
  target and the hygiene test.
- A test that cannot run fails or is deselected by its marker. It never
  skips silently. This rule applies to every new tier.
- Every new tier gets a marker in `pytest.ini`, a `make` target, a row in
  sections 1 and 2, and a line in the daily wrapper when it runs daily.
- New dependencies go through `requirements.txt` and `bootstrap.sh`.
- Quota: the plan adds one real send per week plus one per change to the
  sender. Every other item costs no account traffic beyond the daily reads.

### 6.5 Order and rough effort

P1 (1-2 days), P2 (half a day), P3 (half a day plus the cron line), P4 (half
a day), P5 (1 day); then P6 (1 day), P7 (half a day), P8 (half a day), P9
(half a day), P10 (half a day for the pilot, then monthly); P11-P13 with
the one-client work.
