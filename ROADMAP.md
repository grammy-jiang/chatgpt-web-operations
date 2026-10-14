# Capability status and remaining work

Updated 2026-09-25. This is the current implementation inventory.
`VERIFICATION.md` records dated live and offline results. A command's
existence, a successful live request, and complete output support are
separate claims.

## Working rules

1. Record a new endpoint's actual request before implementing a mutation.
2. Keep the client and commands in this skill. Do not recreate a second
   client in any repository: a consumer runs this skill's commands or imports
   its modules (`CHATGPT_SKILL_DIR`, `CHATGPT_WEB_OPS_DIR`), and a second copy
   found anywhere is deleted, not synced.
3. Use HTTP for reads and account changes. Use the scripted browser for
   messages and browser diagnostics. Do not build send-gate solvers.
4. Test mutations on disposable `rp-test` objects. Record their ids, check
   results by reading them back, and verify cleanup.
5. Pro, Deep research, additional composer modes and sharing remain explicit
   choices. A generic hint selector does not establish complete support for
   every mode's outputs or lifecycle.
6. Preserve the account's default model and effort. Per-send choices rewrite
   the outgoing request. Cookie-based model/effort selection was removed.

## Stage 1: reads and run context — implemented

| Item | Current implementation |
| --- | --- |
| R1: profile context | `profile_context.py`: custom instructions, memory usage, model/effort, project instructions and files. Optional JSON snapshot. |
| R2: project details | `list_projects.py`: paged project listing, direct details, files and chats. |
| R3: account/plan information | `probe_account.py`: authentication, reads, plan window and credits. The displayed usage window does not measure chat sends. |
| R4: scheduled tasks | `list_automations.py`: scheduled, paused and finished tasks, prompts, JSON. Returned cursors are reported but not followed. |
| R5: installed skills/apps | `list_skills.py`: installed skills, expected-skill checks and optional apps. Status labels are reported without interpreting their policy meaning. |
| R6: chat content search | `search_chats.py`: conversation search, bounded pagination and JSON. |
| Readiness and health | `preflight.py`, `health.py`, cookie/send-gate probes and `model_settings.py`; optional browser checks and diagnostic records. |

Memory usage is readable. The account-wide Enable memory toggle has no
identified read/write contract here. Project-only memory is implemented
separately.

## Stage 2: projects and chat changes — implemented

| Item | Current implementation |
| --- | --- |
| M2: pin/unpin, archive/unarchive | `pin_chat.py`; `clean_chats.py --archive/--unarchive`. |
| M3: project instructions | `project_settings.py`: text/file input, update and clear. |
| M4: project memory isolation | `project_settings.py --memory project-only/default`; creation also accepts the memory choice. |
| M5: project lifecycle | `create_project.py` and `delete_project.py`, with read-back verification. |
| Chat cleanup | `clean_chats.py --delete`, selected by project and/or title match. |
| Research recovery | `round_status.py --collect [--apply]` saves outstanding replies locally; `review_topic.py` audits topic folders offline. |

The earlier M2/M3 “command pending” labels were stale. These commands were
implemented on 2026-09-20 and have live round-trip tests.

## Stage 3: sending — implemented, with mode-specific limits

`send_prompt.py` provides new chats, project chats, continued chats, batches
of separate chats in one window, model/effort choices, web search, arbitrary
system hints, attachments, titles, optional waiting and JSON records.
`--record-send-body` also captures the response stream. In a batch, each
send overwrites the same capture path; only the last capture remains.

`deep_research.py start` invokes the page's Deep research hint. `status` and
`fetch` read the conversation widget and save native Markdown and sources.
`export` uses the MCP DOCX/PDF route. Both formats also work for completed
widget runs with `--force`, after `status --wait` confirms DONE. Its default
completion check still reads legacy titles and can refuse a finished widget
report. This is not a general export facility for ordinary chats.

Browser cost varies with navigation, uploads and stream recording. The
batch's 180-second-per-prompt estimate is a conservative planning allowance,
not a measured startup time.

## Connector operations — implemented

`list_connectors.py`, `create_connector.py`, `connect_connector.py` and
`delete_connector.py` manage custom MCP apps and links. Creation uses an
existing OpenAI tunnel with No Auth. Direct server URL and OAuth creation
are not exposed by these commands. `list_connectors.py --detail` accepts
connector ids, not link ids.

`refresh_connector.py` provides tool-list refresh (2026-09-29; the installed
`chatgpt-refresh` name points to it). `manage_tunnels.py`
now provides Platform tunnel list/get/create/update/delete in this skill.
It uses separate Platform credentials and verifies mutations by reading
them back. The onboarding skill delegates cloud tunnel operations here
and retains server setup and local daemon management. Live create, list,
get, update and delete passed on 2026-09-25, with cleanup and the original
tunnel's unchanged state verified; see `references/tunnels.md`.

## Composer modes available through the generic route

The live basic system-hint catalog on 2026-09-25 lists `picture_v2`,
`search`, `tasks`, `tatertot`, `canvas` and `sketch`. These can be supplied to
`--system-hint`. Images, task creation, Canvas, Study and Sketch must not be
labeled wholly unimplemented merely because they have no dedicated command.
Mode-specific completion, artifact collection and cleanup still need their
own measured acceptance cases. See `VERIFICATION.md`.

## No dedicated CLI at present

- Move an existing chat into a project or rename a project.
- Create, pause, update or delete an automation through a direct lifecycle
  command. The generic `tasks` composer route is separate.
- Edit account-wide memories or custom instructions.
- Create/manage shared links, or change billing/security settings.
- Select temporary-chat mode; edit, regenerate or branch an existing
  message; control voice/read-aloud sessions.
- Install/update ChatGPT skills or use the account file library.

The adjacent `chatgpt-project` command was also checked. It supports project
listing/details/instructions but has no rename/move flags. A generic HTTP
session that can send arbitrary requests is not a maintained feature CLI.

## Stage 4: one client for every consumer, and test hardening -- planned 2026-09-27

Two parts, both in `PLAN-2026-09-27.md`, section 5:

- **One client.** Done 2026-09-29: binnacle's `chatgpt-refresh`,
  `chatgpt-chats` and `chatgpt-project` and its copies of `chatgpt_session.py`
  and `chatgpt_cookies.py` are deleted (binnacle commit `640b906`); this skill
  gained `refresh_connector.py`, `clean_chats.py --id/--backup/--track`,
  `project_settings.py --name/--show`, and binnacle's benchmark harness calls
  them. `chatgpt-send` moved on 2026-09-27. One stale copy remains outside
  the owner's active work: research-pipeline's `feat/chatgpt-skill` branch
  (last change 2026-09-16) carries its own `chatgpt_client.py`, which imports
  the session modules from binnacle's old path; that repository is not
  modified any more (owner decision, 2026-09-20), and running that branch
  needs `CHATGPT_HELPERS_DIR` pointed at this skill's `scripts/`.
- **Test hardening.** Real-path boundary tests, fixtures recorded by the
  daily browser check, a scripted end-to-end send, a count of "not posted"
  events, and a flaky-test rule.

Working rule 2 applies to every repository, not only research-pipeline.

## Decisions of 2026-10-01

1. **A browser is used only for what cannot be done over HTTP**: a send,
   Deep research's start, and the browser diagnostics. Every read and every
   other write stays on `/backend-api/*` with the session token (the owner's
   rule, 2026-10-01).
2. **The send attaches to the dedicated browser window** -- a Chrome with
   its own profile, a debugging port on 127.0.0.1 and a login the owner
   made by hand, kept alive by a systemd user unit (`dedicated_browser.py`)
   -- and drives one tab there: its own kept tab by default, a tab named
   by address with `RP_BROWSER_TAB=url:<prefix>`, or one per window with
   `new` (the owner's question of the evening, "why can't you specify which
   tab"). The scripted Xvfb window stays as the fallback, with a fresh
   profile per window and a cool-down after a refusal (atlas
   F-2026-10-01-23 and -26). Measured the same day: the scripted window's
   handshake refused by Cloudflare, the dedicated window's accepted.
3. **No challenge is ever solved or evaded by this skill.** A challenge in
   the dedicated window is answered by the owner, in that window; until
   then a send reports the refusal and waits.
4. **The account's default effort is the preset one position below Pro**
   (the owner's rule, 2026-10-01 evening): Pro has a limited quota, so the
   highest thinking preset below it -- "Extra High", `gpt-5-6-thinking` /
   `max`, today -- is the default. The rule is derived from the catalog each
   time, never named (`model_settings.below_pro_preset`);
   `model_settings.py --default below-pro --apply` sets it the way the
   slider does, and preflight warns when the record drifts.
5. **The dedicated window runs no extensions** (`--disable-extensions`,
   2026-10-01 evening): what Chrome sync put into its profile stays
   installed but inert, which the owner accepts; removing it, if ever, is
   the owner's step, with the sync trap named (F-2026-10-01-32).
6. **A page change is named, not guessed at** (the owner's ask, 2026-10-01
   evening): a composer that does not appear says what the page is instead
   (challenge, login, hidden composers, missing controls); the daily
   recording compares the facts the day's changes hid behind, and a fixture
   that predates a fact says so; the weekly send records the POST body's
   shape and the check warns on a pinned or server-read field going. Where
   the report is delivered stays the owner's decision.
7. **Rules before the agent, the agent before the owner** (the owner's
   layered design, approved 2026-10-01 evening): a failed check is
   reproduced the same way, read-only (`chatgpt-ops-repro.sh`), then
   classified and counted by `triage.py` (no model); the repair agent runs
   only on an escalate decision and proposes on a branch; the owner merges
   or promotes with `chatgpt-ops-approve`. A cheap-model layer for novel
   signatures may come later, without the power to drop an issue.

## Historical decisions

The original stages and abandoned experiments remain in git history,
`HANDOVER.md`, `VENDORED.md` and the dated endpoint/failure records.
Cookie rewriting and composer-menu clicking were replaced by outgoing
request rewriting. The first MCP-only Deep research design was replaced
by the page/widget workflow. Historical notes do not override this inventory
or the current verification record.
