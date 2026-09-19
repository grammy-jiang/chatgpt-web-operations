# Test plan: chatgpt-web-operations

Written 2026-09-20. The bar the user set: every core module at 95% line
coverage or more and every other module at 90% or more, measured per module
and never as an average; several kinds of tests, not only unit tests; and no
test may touch the user's ChatGPT workspace unless it was opted in, in which
case it removes what it created.

## 1. Tiers: what a test may touch

| Tier | Marker | Opt-in variable | May touch | In `make test` |
|------|--------|-----------------|-----------|----------------|
| T0 | none | — | nothing outside this directory: no network, no browser, no cookie DB, no keyring | yes |
| T1 | `live_read` | `CHATGPT_LIVE=read` | the real account, `GET` only | no |
| T2 | `live_write` | `CHATGPT_LIVE=write` | the sandbox project only: its own gizmo id and conversations inside it | no |
| T3 | `live_browser` | `CHATGPT_LIVE=browser` | one Chrome window on the sandbox project; the composer is filled, send is never clicked | no |
| T4 | `live_send` | `CHATGPT_LIVE=send` | real sends inside the sandbox project, deleted in teardown, at most `CHATGPT_LIVE_SENDS` per run (default 1) | no |

Enforcement, not promises:

- `tests/conftest.py` has an autouse fixture that replaces `socket.socket`
  with one that raises for any test without a `live_*` marker. A T0 test
  that reaches the network fails.
- A live marker without its `CHATGPT_LIVE` value skips and prints why. Both
  are needed, so `pytest` alone can never reach the account.
- `tests/live/guard.py` wraps `session.session.call`. In T1 every non-`GET`
  raises before the request is made. In T2 to T4 a non-`GET` is allowed only
  when its path names the sandbox gizmo or a `conversation/<id>` whose id the
  guard has seen in `gizmos/<sandbox>/conversations` or created itself during
  the test. Everything else raises. The guard has its own T0 tests over fake
  paths.
- The sandbox project is `rp-test-sandbox`, created once with
  `create_project.py` and kept; its id lives in `tests/live/sandbox.json`.
  Before any T2 to T4 test runs, the suite reads `gizmos/<id>` and refuses to
  continue unless the name is exactly `rp-test-sandbox`, so a stale id can
  never point the tests at a real project.
- Teardown deletes every conversation a test created, in `finally`. A
  session-scoped fixture sweeps the sandbox at the end and deletes anything
  titled `rp-test …` that remains. `--keep-sandbox-chats` disables both and
  prints what stayed.
- T3 and T4 refuse to start when the rate-limit modal or a 429 is present,
  when `pgrep -f "chatgpt_researc[h].py"` finds a run in flight, or when
  available memory is under `RP_MIN_AVAILABLE_MB`. The browser budget
  variables bind here as everywhere.
- Fixtures recorded from the account go through `tests/record_fixture.py`,
  which replaces ids, emails, names and instruction texts before writing. A
  T0 test scans `tests/fixtures/` for `@`, `user-`, `org-`, any `g-p-` id
  outside an allowlist, and the account's email, and fails on a hit.

## 2. Kinds of tests

| Kind | Tier | What it proves | Example |
|------|------|----------------|---------|
| unit | T0 | one function's decision over data, with fakes for session, page and clock | the 77 tests in `tests/test_commands.py` |
| fixture (contract) | T0 | the parsers read the real payload shapes, recorded and sanitized | `user_system_messages`, a sidebar page with a `cursor`, `wham/usage`, a finished and an unfinished conversation |
| CLI | T0 | every command has argparse and `--help` exits 0; `ensure_venv` re-executes once and only once; `python3 scripts/<cmd> --help` works from another directory (subprocess) | catches `probe_send_gates.py` running the probe on `--help` |
| consistency | T0 | `SKILL.md`'s command table matches `scripts/*.py`; every endpoint constant in the code appears in `references/endpoint-discovery.md` | a command cannot be added without its row |
| regression | T0 | one test per `failure-atlas.md` entry, named after it | "one unreadable cookie must not end the session" |
| safety | T0 | the properties the user relies on: a dry run never mutates; read commands never issue a non-`GET` (a spy session); `BrowserSender` refuses under the rate-limit modal and sends Escape only to other modals; the live guard rejects an id outside the sandbox; the fixture scanner finds a planted email | |
| robustness | T0 | every parser survives `{}`, `None` fields, wrong types and half-written messages | parametrized |
| smoke | T1 | each read command exits as documented against the real account; assertions on shape, never on the user's data | `profile_context.py` exits 0; the slider has 5 positions |
| round trip | T2 | each write command: act, read back, revert; the cleanup is verified by a read | set instructions on the sandbox, read `gizmos/<id>`, restore |
| browser dry run | T3 | the send path opens: window, cookies, composer found; never sends | none written yet; the model-label check was removed 2026-09-20 because the cookie steers neither the send nor the label |
| measured send | T4 | the send path end to end, one message per feature, timings recorded in `failure-atlas.md` | search on and off, one attachment, one deep research |

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

## 4. Baseline, measured 2026-09-20

In this directory, `.venv/bin/python -m pytest tests --cov=scripts`, 77
tests:

| Module | Statements | Covered | Bar |
|--------|-----------:|--------:|----:|
| `chatgpt_cookies.py` | 35 | 0.0% | 95% |
| `chatgpt_session.py` | 130 | 0.0% | 95% |
| `measure_window.py` | 58 | 0.0% | 90% |
| `probe_account.py` | 23 | 0.0% | 90% |
| `chatgpt_client.py` | 724 | 15.6% | 95% |
| `create_project.py` | 115 | 18.3% | 90% |
| `discover_endpoints.py` | 72 | 26.4% | 90% |
| `probe_send_gates.py` | 36 | 36.1% | 90% |
| `list_projects.py` | 49 | 38.8% | 90% |
| `probe_cookies.py` | 53 | 43.4% | 90% |
| `model_settings.py` | 73 | 45.2% | 90% |
| `read_chat.py` | 34 | 47.1% | 90% |
| `_common.py` | 45 | 55.6% | 95% |
| `round_status.py` | 49 | 65.3% | 90% |
| `list_chats.py` | 47 | 66.0% | 90% |
| `review_topic.py` | 153 | 88.9% | 90% |
| `clean_chats.py` | 40 | 90.0% | 90% |
| `round_state.py` | 32 | 90.6% | 95% |
| `profile_context.py` | 108 | 97.2% | 90% |

Total: 1876 statements, 34.7%. Only `profile_context.py` and
`clean_chats.py` meet their bar today.

After the first wave, the same day: 497 T0 tests; every module at 100%
except `profile_context.py` (99%) and `review_topic.py` (92%), and the three
browser commands still to do (`create_project.py` 17.5%,
`discover_endpoints.py` 26%, `measure_window.py` 0%), which need the fake
Playwright that the BrowserSender tests brought (`tests/fake_playwright.py`).
Lesson from the merge: a T0 test must never reach the real `cc._helpers()`,
because it patches the `chatgpt_session` module in place and every later
cookie test then runs against the patched functions.

End of the same day: 795 T0 tests, every module at or above its bar
(`make test` green), `make lint` clean; live tiers exercised by hand:
`make live-read` 4 passed, `make live-write` 3 passed plus the project
lifecycle round trip, and six measured sends (T4) through
`send_prompt.py` inside the sandbox, all deleted afterwards. The browser
part of a send is about 18 s when the reply is not awaited; the earlier
2.5-minute figures were mostly the reply wait.

## 5. Reaching the bar on what exists

Ordered so the safety net exists before any live tier runs.

1. Harness: `tests/conftest.py` network guard, `.coveragerc`,
   `tests/coverage_gate.py`, `Makefile`, `tests/record_fixture.py` and the
   sanitizer scan, `tests/live/guard.py` with its T0 tests. Nothing live yet.
2. `chatgpt_session.py` (130 statements, 0%): a fake `dbus` module placed in
   `sys.modules`; `_make_decryptor` against a temp key with real AES; a temp
   sqlite cookie DB; `Session.call` over a fake `urlopen` covering 200, the
   403 retry, 429, 5xx, `URLError` and the retry sleeps with a fake clock.
3. `chatgpt_cookies.py` (35, 0%): the same temp DB; the expiry conversion;
   the missing-session failure.
4. `_common.py` (45, 56%): the four `ensure_venv` branches with a fake
   `os.execve`; the `open_session` failure path.
5. `chatgpt_client.py` (724, 16%), the largest item: the parsers and
   transcript helpers as pure unit tests; `with_effort`; `ChatGPTSession`
   over a fake helper module; `wait_for_reply` over scripted turn states and
   a fake sleep; `browser_slot` and `new_chat_lock` over a temp slot
   directory with `available_mb` and `scripted_browser_pids` faked;
   `_tolerant_decryptor` and the cookie patches over a fake `cs`;
   `virtual_display` over a fake `subprocess`; `BrowserSender` over a fake
   Playwright (page, locator, context, browser, file chooser) that records
   every call: rate-limit detection, overlay dismissal, composer focus and
   fill, attachment, click send, the watchdog kill with a fake `os.kill`.
   Roughly 90 to 120 tests.
6. The commands below the bar: `probe_account`, `probe_cookies`,
   `probe_send_gates`, `model_settings`, `list_chats`, `list_projects`,
   `read_chat`, `round_status`, `create_project`, `discover_endpoints`,
   `measure_window` (a fake `ps` output and a fake sender), `review_topic`,
   `clean_chats`, `round_state`: each `main` over a fake session or a fake
   page, the pattern `profile_context` already uses.
7. The T1 smoke suite, run once by hand and its result recorded.

Estimate: T0 grows from 77 to roughly 350 tests; T1 about 15; T2 to T4 a
handful each.

## 6. What each planned stage item adds

| Item | T0 | Live |
|------|----|------|
| 1.2 `list_projects.py` paging and `--files` | paging over two fixture pages with a cursor; `--id` reads `gizmos/<id>`; file rows keep scalars only | T1: the count exceeds one page |
| 1.3 `probe_account.py` usage lines | a `wham/usage` fixture; missing fields; the "not chat sends" label is printed | T1: exit 0 |
| 1.4 pre-run step and the `review_topic.py` invariant | a topic with and without `chatgpt/profile_context*.json`; a file older than the newest round warns | — |
| 2.1 project instructions (M3) | a dry run never calls; `--apply` sends the captured body shape, a fixture from the recording; an id that is not `g-p-` is refused | T2: set on the sandbox, read back, restore |
| 2.2 project-only memory (M4) | the same pattern | T2: switch the sandbox, read `memory_scope`, switch back |
| 2.3 pin, unpin, unarchive (M2) | the same pattern | T2: pin one sandbox chat, `pins` lists it, unpin |
| 3.1 effort and model per send (B1) | `rewrite_send_body` over fake routes: only the pinned fields change, other requests fall through | T4: one recorded send, the reply's `metadata.thinking_effort` checked (done 2026-09-20) |
| 3.2 search per send (B2) | the rewrite adds `"search"` to `system_hints` and keeps other hints | T4: one recorded send, `search_result_groups` filled (done 2026-09-20) |
| 3.3 attachments (B4) | the fake file chooser receives the path | T4: one PDF, the reply checked for the paper's title |
| 3.4 deep research (B3) | reply collection handles the new shape, a fixture from the measurement | T4: one measured run, timings recorded |
| `send_prompt.py`, the skill's own send entry point | a fake sender: effort, model, search, attachment, project routing | T4 shares its sends |

Every stage item ships with its T0 tests at its bar, and a stage item is not
done until `make test` passes the gate.
