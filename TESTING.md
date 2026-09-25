# Test plan: chatgpt-web-operations

Updated 2026-09-25. Current results are in `VERIFICATION.md`. The bar the user set: every core module at 95% line
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
| measured send | T4 | the send path end to end, one message per feature, timings recorded in `failure-atlas.md` | search on and off, one attachment, one deep research; `tests/live/test_send_effort.py`, which pins a level the account is **not** already using and asserts the reply recorded it -- the check that would have caught the two-month effort regression; and `tests/live/test_send_chat_flags.py`, which mints a chat and round-trips pin, unpin, archive and unarchive on it |

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
`make fmt` formats with ruff and `make all` runs lint then test.

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
