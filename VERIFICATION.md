# Verification record — 2026-09-25

The original 28 public commands were exercised on the installed Raspberry Pi 5 skill.
The checks found stale documentation, connector error-handling defects, and
misleading Deep research messages. Those were corrected. Live authentication
remains intermittent: some session requests receive an HTTP 403 Cloudflare
challenge, while later requests and browser sends work.

This is a dated acceptance result for the tested account and command paths.
It does not establish support for every ChatGPT UI feature or generic mode.
The five support modules are included in the offline suite.

Raw logs, account snapshots, request/stream captures, the object ledger, and
report exports are outside the repository in the private directory
`~/.local/state/chatgpt-web-operations/verification-20260925T024308Z/`.
No credentials or raw account payloads were added to tracked fixtures.

## Platform tunnel extension

`manage_tunnels.py` adds the 29th command: list/get/create/update/delete for
the OpenAI Platform tunnel resource. It uses an authorized dashboard token
or admin key for management; the existing runtime key can inspect a tunnel.
The onboarding skill's `tunnel-admin.sh` now delegates here. The server and
local forwarding daemon remain separate resources.

The 44 new offline tests passed with 100% line coverage for this command.
A live `get` succeeded with the existing runtime key. Vendor create/update/
delete/list wire shapes were captured with a synthetic credential against
a local recorder. Live create/list/get/update/delete then passed with the
local management key. The run also checked previews, a wrong-name refusal,
scope preservation, deletion read-back and cleanup. The original tunnel's
full record was unchanged. No credential value was copied into the
repository or output. `references/tunnels.md` documents credential setup
and command behavior. Raw acceptance logs are in the private directory
`~/.local/state/chatgpt-web-operations/tunnel-verification-20260925/`.

The existing admin key exported by the user's `.bashrc` was also verified
separately: a child Bash loaded it, and a read-only GET of the existing
tunnel returned HTTP 200. Only availability and status were reported.
The command itself does not load `.bashrc`; its admin lookup reads the
process environment before the private `admin.env` fallback.

The first-use instructions now link to `references/setup.md` and the
credential creation, selection and recovery sections in
`references/tunnels.md`. Official key-creation links and tunnel permissions
were checked against OpenAI documentation and installed `tunnel-client`
help on 2026-09-25. No new key was created for this documentation update.

## Test results

| Check | Result |
| --- | --- |
| Initial offline suite | 1,549 passed, five consistency failures. Four connector commands were absent from the main table, and their endpoint reference was omitted from the check. |
| Final `make all`, including tunnel extension | 1,660 passed; 28 live tests deselected as intended. All per-module gates passed: at least 95% for each core module and 90% for each command. Overall line coverage is 99.3%; the four connector commands and the tunnel command have 100%. |
| `make lint` | Passed: Ruff checks and formatting for 89 Python files. |
| Skill validation | Standard validator passed for both updated skills. Documentation/command consistency checks include the Platform tunnel endpoint reference. |
| First-use documentation | Checked 18 local links/anchors and the Bash syntax of eight setup/tunnel examples. Credential creation pages and permission guidance were checked against official OpenAI documentation and installed CLI help. |
| Live reads | A complete run passed all 22 tests. The final rerun passed 21 and hit an authentication challenge in the Chrome-cookie-only test; that isolated test passed on retry. The intermittent failure remains part of the result. |
| Live project writes | Three passed: create/delete, instruction update/restore, memory-scope update/restore. |
| Live browser upload | One passed: upload settled, attachment chip present, send enabled. |
| Live sends | Two passed: explicit effort and chat pin/unpin/archive/unarchive. |
| Additional acceptance sends | Six sends: two batch chats, one continuation, web search, Deep research, and a connector tool call. These are separate from the regular live-suite send cap. |
| Diagnostics | Browser preflight, health, memory/fill measurement, and endpoint/global discovery passed. Preflight and health returned the documented warning result for host CPU load. |

`make all` passed again after the setup documentation update. That log is
`~/.local/state/chatgpt-web-operations/commit-validation-20260925.log`.

Live tiers used the fixed sandbox. Additional acceptance used a separate,
uniquely named disposable project with project-only memory and a disposable
connector on an existing authorized tunnel. Commands used their normal entry
points and real transports. Later manual checks reused one authenticated
HTTP session; they did not mock network responses.

## Command inventory and evidence

| Command | Exercised behavior |
| --- | --- |
| `preflight.py` | Host, account, project and browser composer checks; JSON verdict. |
| `health.py` | Fifteen default checks; JSON, skill inventory and transport diagnostics. |
| `probe_account.py` | Authentication, account, chat listing and plan/credit reads. Subject to the authentication caveat below. |
| `probe_cookies.py` | Decryption/expiry summary and JSON; no cookie values printed. |
| `probe_send_gates.py` | Current requirements; documented browser-required result. |
| `model_settings.py` | Model catalog, effort levels and current server selection. |
| `profile_context.py` | Instructions, memory counts, model settings and test-project context. |
| `list_chats.py` | Recent, title-filtered, non-project, pinned and archived listings. |
| `search_chats.py` | Two-page search included the test project chat. Later exact-marker queries returned content matches for user text and attachment/reply text. |
| `list_projects.py` | Name match, details, file listing and project chat listing. |
| `list_automations.py` | All three states, prompts and JSON. No automation was changed. |
| `list_skills.py` | Skills, apps, JSON and an expected installed skill. |
| `list_connectors.py` | Apps/links, name filter, details, tunnels and JSON. |
| `create_connector.py` | Dry run, actual tunnel-backed No Auth creation, duplicate-name rejection. |
| `connect_connector.py` | Dry run, connection, six discovered tools, per-link privacy confirmation, reconnection. |
| `delete_connector.py` | Dry run, link-only deletion and app-plus-link deletion; listings confirmed removal. |
| `create_project.py` | Dry run, creation with instructions and project-only memory; read-back confirmed settings. |
| `delete_project.py` | Name guard, dry run, deletion; final read returned 404. |
| `project_settings.py` | Instruction replacement/clear and default/project-only memory round trips. |
| `send_prompt.py` | Two chats in one window; text plus PDF attachments in each; exact tokens in both replies; explicit model/effort, continuation, titles, search and plugin hints; JSON, request and completed SSE capture. |
| `deep_research.py` | Start, delayed widget startup, status/wait, Markdown/source/JSON collection, DOCX/PDF export with `--force`, DOCX text extraction. See completion-check limit below. |
| `read_chat.py` | Continuation reply text and effort metadata. |
| `pin_chat.py` | Dry run, pin, listing confirmation and unpin. |
| `clean_chats.py` | Archive/unarchive with read-back; selected chat deletion followed by 404. |
| `round_status.py` | Collection preview and recovery of a real reply into an isolated local transcript/ledger. Both attachment tokens appeared in the recovered text. |
| `review_topic.py` | Offline inspection of the recovery folder. Nonempty-corpus and failure cases remain covered by the offline suite. |
| `measure_window.py` | Opened a composer and measured memory plus prompt fill time. |
| `discover_endpoints.py` | Opened the page and captured requests and matching page globals. |

Deep research produced 2,469 Markdown characters and 22 sources. The DOCX
was 12,636 bytes with 2,575 extracted text characters. The PDF was 35,003
bytes; `pdftotext` extracted 2,599 characters of the report.

The connector called the disposable app's `run_command` once with an `echo`
nonce in `/tmp`. Its captured SSE tool result showed exit code zero and the
expected output; the local command log matched. The stored chat did not
retain the tool-result text, so the assistant reply alone was not used as
proof. The adjacent `chatgpt-refresh` command also refreshed all six tools.

## Corrections made

- Fixed invalid YAML in the skill's description; its unquoted colon made
  the frontmatter fail the standard skill validator.
- Added four existing connector commands to `SKILL.md` and their canonical
  endpoint reference to the consistency check.
- Privacy writes now fail when the response does not confirm the requested
  value. Their dry run displays the privacy PATCH too.
- App deletion stops if any link deletion fails. Failed initial lookups
  also stop instead of being treated as an absent app.
- Connector commands now have testable `main(argv)` entry points and
  regression tests for successful operations and failure paths.
- The health live test accepts current authentication options and expects
  the installed-skill check. Live tiers reuse an authenticated transport.
- Authentication tries the existing Chrome cookie once after a selected
  keyring login returns an incomplete response or HTTP 401/403. Failed
  authentication cannot overwrite the stored renewal. The fallback and
  negative cases are regression-tested; it does not solve a challenge.
- Deep research's missing-widget message now explains startup delay.
  Export help, comments and skill instructions describe the existing
  `--force` route for completed widget reports.
- Synchronized the inventory, roadmap, test procedure, handover pointer,
  and failure/endpoint references. Old counts and timings are historical.

## Capability corrections and limits

The earlier label “not implemented” was too broad. Pin/unpin,
archive/unarchive, project instructions/memory, content search, and all four
connector commands already had implementations. The generic
`send_prompt.py --system-hint` route also already existed. A live catalog
read returned `picture_v2`, `tasks`, `canvas`, `tatertot`, `sketch` and
`search`. Missing dedicated scripts do not mean these modes cannot be
invoked. Web search and plugin invocation were exercised here. Image,
Canvas, Study, Sketch, and scheduled-task creation/lifecycle were not tested
from start to finish; their artifacts and cleanup are not covered by the
ordinary text-reply collector.

Connector tools are refreshed by `refresh_connector.py`; the installed name
`chatgpt-refresh` points at it since 2026-09-29 (at the time of this run it
was binnacle's own tool). Project instructions are read and changed with
`project_settings.py --name NAME --show` and `--instructions`, which
replaced binnacle's `chatgpt-project` the same day. Neither renames or
moves a project. No dedicated automation lifecycle, account-memory
or custom-instruction editing, temporary-chat, message-edit/branch/regenerate,
voice, sharing, or library-management CLI was found.

Other observed limits:

- Authentication challenges occurred with both keyring and Chrome-cookie
  requests. The browser composer and later HTTP requests worked. This does
  not prove expired credentials or an account block. The fallback cannot
  guarantee authentication when both copies meet a challenge.
- A one-time Deep research status check can return 1 before the widget
  appears. Use `status --wait` after starting. Missing state does not prove
  that research was started through a different method.
- Export's default guard reads legacy “Generated report” titles. It returned
  3 for this completed widget report. Confirm DONE with `status --wait`,
  then use `export --force`. The older MCP-only start workflow was not
  rerun in this audit.
- Search indexing lagged. Both unique markers initially had zero hits and
  later matched their chats. Use recorded ids for immediate collection.
  Archived-chat search was not established.
- Automation and installed-app cursors are reported but not followed.
  Connector creation supports the captured tunnel/No Auth path, not direct
  server URL or OAuth setup.
- Batch request/stream captures share one path and retain the last send.
  Playwright emitted shutdown warnings in recorded sends; the captures
  still ended with `[DONE]`, and reply/tool evidence was complete.

## Cleanup

The selected disposable chat was deleted separately. The disposable project
and its four remaining chats were then deleted; the project read returned
404. Both connector links (the first and the reconnected link) and the app
were removed. The fixed sandbox was empty after its tests. Local prompts,
transcripts, exports and logs remain in the private evidence directory.
Server retention of uploaded attachment files was not independently
verified; this skill has no file-library deletion command.

A final independent read confirmed that the test project, app and links
were absent, the original MCP link remained, and the sandbox was empty.
The model configuration, custom instructions and memory summary matched
the initial profile snapshot.

## Uploaded research-pipeline skill deleted (2026-09-25, late evening)

At the user's request the `research-pipeline` skill uploaded to the account
(`hazelnuts`, latest version 14, `safety_check_status` `blocked`) was
deleted from the Skills page after a Download as backup (46 of 46 files;
the zip is kept outside the repository). Both requests are recorded in
`references/endpoint-discovery.md`; Delete has no confirmation dialog. A
read afterwards (`list_skills.py`) listed the three remaining skills and
no `research-pipeline`; the page showed it in neither section.

`health.py`'s skills inventory no longer requires a particular skill (it
had required this one since 2026-09-21), so the daily job does not turn
the deletion into a block. Offline suite: 1660 passed, coverage gate OK,
lint clean. Live `tests/live/test_read_skills.py`: 3 passed.

A cron-style run of the daily wrapper at 23:48 read the inventory as
"3 installed" (ok). The same run's conversations listing got HTTP 429 and
then a Cloudflare challenge (403) while another project's trial was
sending and polling through this skill, so that run's verdict was DO NOT
START for the conversations read, not for the skills change.

## Mutation testing pilot (2026-09-29)

`TESTING.md` P10. `make mutate MODULE=<file>` runs `tests/mutate.py`: every
mutation site of one module, found in its AST (comparisons, `and`/`or`, a
dropped `not`, `+`/`-`, `True`/`False`, an integer plus one, a compared or
indexed string, a returned value made `None`, a call statement made a
no-op), one at a time, in a throwaway git worktree of HEAD, at nice 19 and
one process, never while a send is in flight. A baseline run under per-test
coverage decides which tests each mutant runs: the tests that executed its
line. A mutant is killed when a test fails, times out, or cannot run because
the module no longer imports. mutmut 3.8 was tried first and dropped: it
imports its instrumented copies by package path, and these tests import each
command as a top-level module from `scripts/`.

| Module | Mutants | Killed, first run | Killed now | Time now |
| --- | --- | --- | --- | --- |
| `round_state.py` | 17 | 15 (88.2 %) | 17 (100 %) | 49 s |
| `chatgpt_cookies.py` | 24 | 21 (87.5 %) | 22 (91.7 %) | 70 s |
| `_common.py` | 37 | 30 (81.1 %) | 33 (89.2 %) | 2.5 min |
| `chatgpt_session.py` | 298 | 239 (80.2 %) | 293 (98.3 %) | 11.6 min |

The target, 80 % or more per core module, holds for all four. "Now" is after
the tests the survivors asked for: `tests/test_round_state.py` (a skipped
paper without a reason, malformed entries), `tests/test_cookies.py` (the
command names itself in its errors), `tests/test_common_edges.py` (`shorten`
at exactly its limit and at its default, a table row with an extra cell),
and `tests/test_session_mutants.py` (the login failure reason by status;
damaged plaintexts are None, never a cut value; a domain hash before an
empty value; the keyring's copy and the jar a browser gets; the renewal's
one-minute threshold; retry counts; the diagnostic events' outcome, status,
elapsed time and attempt numbers). Two existing tests were weaker than their
names and were corrected: the `CHATGPT_SESSION_STORE=0` test raised inside
functions that swallow every exception, so it passed with the bus opened;
and the fake Secret Service ignored the variant level of `OpenSession`'s
argument, which the real one checks.

The survivors left have no test because no test can see them:

- `chatgpt_cookies.py` line 46 and `chatgpt_session.py` lines 701 and 771,
  `con.close()` removed: the connection is to a temporary copy of the cookie
  database, deleted when the block ends.
- `chatgpt_cookies.py` line 47, the SameSite key 1 changed to 2: equivalent;
  a missing key falls back to "Lax", the value key 1 held.
- `_common.py` lines 51-52, the flushes before `os.execv` removed: every
  command calls `ensure_venv()` before it prints anything.
- `_common.py` line 63, `sys.path.insert(0, ...)` made `insert(1, ...)`:
  equivalent while nothing earlier on the path shadows a script's name.
- `_common.py` line 86, `strict=True` made `False` in the header row's `zip`:
  equivalent; the widths are computed from the headers.
- `chatgpt_session.py` line 311, the pad of an empty plaintext made 1:
  equivalent; an empty plaintext stays empty.
- `chatgpt_session.py` line 834, `range(2)` made `range(3)`: equivalent; the
  login loop always leaves at its second pass.
- `chatgpt_session.py` line 1039, a 200-character cut made 201 on "the
  answer was not JSON (...)": equivalent; that message is far shorter.

Cost: the first `chatgpt_session.py` run ran every chosen test file for
every mutant, about a minute each, five hours in all; it was stopped, and
the per-test coverage selection brought the same 298 mutants to under
twelve minutes. Run monthly by hand, one core module at a time.
