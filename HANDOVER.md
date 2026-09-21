# Handover: chatgpt-web-operations, 2026-09-20

Written for the Claude Code session that starts in this directory, by the
session that ran in `~/Projects/research-pipeline-chatgpt` on 2026-09-19 and
2026-09-20. Read this first, then `SKILL.md`, `ROADMAP.md`,
`references/endpoint-discovery.md`, `references/failure-atlas.md` and
`VENDORED.md`. Everything below was measured on the user's live ChatGPT Pro
account or in this directory; nothing is assumed.

## 0. Update, 2026-09-20, end of the second session

Read this section, then `ROADMAP.md` ("Decisions of 2026-09-20" and the
stage notes), `TESTING.md` and `SKILL.md`; sections 1 onward are the first
session's handover and are kept as history. Where this section and a later
one disagree, this one is what was measured last.

**State.** This directory is a git repository on `main`, 62 commits, tree
clean. 24 commands in `scripts/`, 1,529 offline tests, a per-module
coverage gate (95% core, 90% other) and four opt-in live tiers that pass:
22 read, 3 write, 1 browser, 2 send. A daily health check runs from cron
since 2026-09-21 (below).

**The user's standing decisions.** All work stays in this skill. The agent
performs every recorded action itself and never asks the user to click
through ChatGPT. Live tests touch only the sandbox project and delete their
conversations afterwards. Everything that can be plain HTTP is plain HTTP;
the browser is for the gated send and nothing else (three gates, re-probed
the same day: proof-of-work, Turnstile, the behavioural `so`; never build a
solver). Only the Chat surface is in scope: never Work, Sites or Codex, and
the file library is out of this skill entirely.

**Transport per command.** Plain HTTP, no browser: `preflight.py` (unless
`--browser`), `probe_account.py`, `probe_cookies.py` (local only),
`probe_send_gates.py`, `model_settings.py`, `profile_context.py`,
`list_chats.py`, `list_projects.py`, `create_project.py`,
`project_settings.py`, `delete_project.py`, `pin_chat.py`, `read_chat.py`,
`clean_chats.py`, `round_status.py`, `deep_research.py` except its `start`,
`review_topic.py` (offline, reads a run directory). Browser:
`send_prompt.py` for the send itself, `deep_research.py start` through it,
`preflight.py --browser` for one composer check, and the two measuring
commands `measure_window.py` and `discover_endpoints.py`.

**Three commands were added or rewritten this session.**

- `preflight.py`, new. One go/no-go verdict before a run starts, over one
  reused session, in four groups: host, link, account, run. Link is checked
  before account on purpose, so a dead wireless interface is never reported
  as a blocked account. Exit 0 GO, 2 GO WITH WARNINGS, 1 DO NOT START. It
  was built from a census of eleven real failures across eight topic
  ledgers on this account, none of which the orchestrator checked for.
- `deep_research.py`, rewritten around what a Deep research run actually
  is: an ordinary chat that runs longer. `start` sends the prompt the way
  the page does, one message carrying the system hint
  `plugin:connector_openai_deep_research`, and ChatGPT attaches a widget to
  that same conversation. Nothing has to stay open and no MCP call is made.
  `status` and `fetch` then read it back over plain HTTP from
  `GET /backend-api/conversations/<id>`, where exactly one message with
  `author.role == "tool"` carries `metadata.chatgpt_sdk.widget_state` -- a
  JSON string whose `report_message.content.parts[0]` is the finished
  report as **native Markdown**, written out byte for byte. No format
  conversion anywhere. The browser pays about 19 s for the send; the
  measured run was readable 194 s later.
- `round_status.py --collect [--apply]`, new. A ledger entry stuck at
  status "sent" means four different things, and the orchestrator could
  not tell them apart: resumable, superseded, collectable, lost. This
  classifies each one and, with `--apply`, archives a collectable reply and
  reclassifies a superseded or lost entry. It never touches a resumable
  entry and refuses while an orchestrator is running.

**The session token renews itself, 2026-09-21.** `GET /api/auth/session`,
the call every session build makes for its bearer token, re-issues
`__Secure-next-auth.session-token` for 90 days on every call, and older
copies stay valid. `chatgpt_session.Session` now keeps the renewed copy
in the keyring (Secret Service item `application=chatgpt-web-operations`,
`purpose=chatgpt-session-token`), sends whichever of Chrome's copy and
the keyring's expires later, and the browser jar gets the same choice
through one helper. Chrome's cookie database is never written. So the
daily health check is the renewal: as long as it runs, the token never
reaches its expiry; the one way to lose the session is a logout or
password change, which that check reports as an ALERT the next morning.
`CHATGPT_SESSION_STORE=0` turns the keyring off. The earlier statement
in this file that "nothing automated can keep the cookies alive" was
wrong: it was written before the one request that settles it was made.

**Three reads closed on 2026-09-21 (ROADMAP R4-R6).** `search_chats.py`
searches chat *content* through the page's own search, `POST
/backend-api/global/search`, conversations only (never the file library,
never projects); the live guard lists that one POST as a read.
`list_automations.py` lists ChatGPT's scheduled tasks: they are called
automations, the page is chatgpt.com/scheduled, and the read is
`GET /backend-api/automations?filter=scheduled|paused|finished` -- while
`GET /backend-api/tasks`, which the roadmap had filed as "scheduled
tasks", is the account's background-task history (old Deep research runs
and image generations). `list_skills.py` lists the skills uploaded to
chatgpt.com (`hazelnuts`) and the installed apps; the research-pipeline
skill read `safety_check_status: blocked` with label `safeguard_evasion`
and default version 1 against latest 14 that day -- printed verbatim,
meaning unverified. The daily wrapper now runs `list_skills.py --expect
research-pipeline` and mails WARN when any skill's safety status, labels
or version changes.

**The daily health check, 2026-09-21.** `scripts/health.py` is the HTTP
half: preflight's four groups by calling preflight's own functions, plus a
"health" group (session-token expiry read from the jar without decrypting;
the sandbox answering to its name and holding no leftover chat; the
sidebar and pins reads), and `--json` with a `facts` block whose keys are
the same every run. `~/.local/bin/chatgpt-ops-check.sh` (its own git repo)
wraps it the house way -- state in `~/.local/state/chatgpt-ops`, silent on
OK, `--browser` and `--summary` modes -- and runs from the crontab: daily
05:25 (Friday's run is the browser one), summary Friday 03:40. The slot
was chosen from the backup logs: backup.sh 03:30 runs up to 11 min, apt
~04:08, r2-sync 04:30-04:47, r2-check 05:00-05:16. Cadence rests on
measured rot: Google Chrome, the send path's browser, upgrades every
~4.5 days (dpkg log; a major version every ~13), which only a browser run
sees; the session token is issued for 90 days and is the only cookie that
matters -- removing `_puid` (7 d) or `__Secure-oai-is` (30 d) changed
nothing, the page reissues them. Two things made cron possible:
`chatgpt_session.ensure_desktop_env()` now defaults the D-Bus address
before cookie decryption (it was on the browser path only; four scripts in
`~/.local/bin` had exported the variables themselves), and preflight has a
real "keyring bus" check instead of an import that always said ok.

**The private-seam audit, 2026-09-20.** Three callers reached into
`BrowserSender`'s private members and all three were broken or had
silently stopped working: `preflight.py --browser` (wrong thread, and a
page nothing had navigated -- it had never worked), `measure_window.py`
(the navigation half of the same defect), and the T3 upload test (worked
only because it navigated itself). Each fake was kinder than the real
object. The sender now has public `probe_composer`, `fill_composer` and
`attach_files`, `fill_budget_ms` is shared with `_send`, and a
consistency test refuses `sender._<name>` in any code outside the client.
The rule: a new need is a new public method on the sender, never a reach.

**Two observations on the host.** Short-lived Playwright Chromes
(fresh `playwright_chromiumdev_profile`) come from binnacle's
`chatgpt-mcp-dev` skill (`Projects/binnacle`); preflight counts them as
in-flight browsers, the wrapper treats that as WARN. And on 2026-09-20 at
22:59-23:00 four tracked files were modified by no session or agent that
could be identified (reviewed on content, committed as `af7dbbf`).

**The defect that shaped the session.** For two months the research
orchestrator pinned a send's reasoning effort by rewriting the
`oai-last-model-config` cookie. That never worked: two recorded sends
posted `thinking_effort: max` while the cookie said `standard`, because the
page follows the account's own `last_used_model_config`, not that cookie.
Effort, model, search and system hints are now pinned by rewriting the
outgoing `f/conversation` POST body in flight (`rewrite_send_body`), which
is the only thing that reaches the server, and the reply's own metadata is
what verifies it. The root cause was two copies of the client drifting
apart, so there is now exactly one: the repository's
`.github/scripts/chatgpt_client.py` was deleted and
`chatgpt_research.py` imports this skill's copy through `CHATGPT_SKILL_DIR`
(repository commit `91dfe062`, its 186 tests pass). **Rule: one client. If
a second copy appears, delete it rather than sync it.**

**Tests.** `make test` runs the offline suite and the coverage gate;
`make lint`; `make live-read` / `live-write` / `live-browser` /
`live-send` for the opt-in tiers, each needing both its marker and a
matching `CHATGPT_LIVE`. A live test that needs a conversation mints one
(`tests/live/minting.py`) and deletes it; it must never wait for one an
earlier run left, because the session-end sweep empties the sandbox. That
mistake made the pin and archive test skip every run from the day it was
written until it was fixed, and a skip reads like a pass. The T4 send cap
`CHATGPT_LIVE_SENDS` (default 4) is enforced now and fails rather than
skips, for the same reason. `scripted_browser_pids()` requires argv[0] to be a
browser binary: a shell script that merely mentions "playwright" and
"chrome" once held the in-flight block for an hour.

**Sandbox.** `rp-test-sandbox` = `g-p-6aaea9da2bc881918d6f9eb5177cf904`
(`tests/live/sandbox.json`), project-only memory, left empty. The live
tiers (`tests/live/guard.py`) can write only inside it, create only
`rp-test` projects and delete only the ones they created; a session-end
sweep deletes every conversation in it, whatever its title, because
ChatGPT renames a chat itself once the first reply lands.

**Facts that cost time, so they are written down.** The first click after a
page load is often swallowed, so click again. The extension's network log
never shows a request body; hook `fetch` and read `input.clone().text()`,
because the page passes bodies inside `Request` objects. The composer's
"+" click never reaches the send body; `system_hints` does. An attachment
must be waited for, and the busy indicator appears about 2.5 s after the
input is set, so waiting for it to clear without first waiting for it to
appear returns too early. `--title` must be applied after the reply
arrives, or ChatGPT's own auto-title overwrites it. When killing a
background script, use the bracket form (`"deep_follow_onl[y].py"`) or
`pkill -f` matches its own shell.

**Where the wrong turns are recorded, so they are not repeated.**
`references/failure-atlas.md` has one entry per real failure and
`references/endpoint-discovery.md` one row per endpoint, both with the date
measured. The Deep research hunt cost most of an afternoon searching MCP
`get_state`, a carrier conversation, `/messages`, MCP resources, four
export types, a hand-written websocket client and the page bundles, all
empty, because they targeted researches started through the MCP `start`
tool, which attach no widget. Two clues to the right answer were in hand
early and walked past: `applyRemoteWidgetState$` in the page bundle, and
`chatgpt_sdk` in a tool message's metadata keys.

## 1. What was done

**The skill moved here and became self-contained.**

- It used to live in the research-pipeline repository at
  `.github/skills/chatgpt-web-operations` on branch `feat/chatgpt-orchestrator`
  (worktree `~/Projects/research-pipeline-chatgpt`). Eleven of its twelve
  commands imported the orchestrator by walking up to the repository root, so
  a plain copy did not work anywhere else.
- Now `~/.claude/skills/chatgpt-web-operations` is a real directory (not a
  symlink) with its own copies of the client and helpers, its own `.venv`,
  and its tests. Commit `e9d2ab27` in the repository removed the old copy.
  Codex and Copilot see this directory through symlinks in
  `~/.agents/skills` and `~/.copilot/skills`.
- Vendored modules, all recorded with origin and sha256 in `VENDORED.md`:
  `scripts/chatgpt_client.py` (from the repository's `.github/scripts`, only
  the `HELPERS` and `PW_PYTHON` defaults changed), `chatgpt_session.py` and
  `chatgpt_cookies.py` (from binnacle, byte-identical), `round_state.py`
  (eight helpers extracted verbatim from `chatgpt_research.py`). Rule: change
  the origin, then re-vendor by diff; never edit both sides. (Superseded
  2026-09-20: the copies are owned here and the repository is not modified
  any more; see `ROADMAP.md`, "Decisions of 2026-09-20".)
- `.venv` is built by `bootstrap.sh` with `--system-site-packages` because
  the cookie decryptor needs the apt package `python3-dbus`, which pip cannot
  build cleanly. Playwright 1.62.0, cryptography and pytest are installed in
  the venv. Every command calls `ensure_venv()` and re-executes itself under
  `.venv/bin/python`, so `python3 scripts/<command>` works from any directory.
- Quality gates: `.venv/bin/python -m pytest tests -q` (67 pass) and
  `uvx ruff@0.14 check .` / `uvx ruff@0.14 format --check .` (clean;
  `ruff.toml` mirrors the repository's rules and exempts the vendored files).

**The repository side.** The orchestrator (`.github/scripts/chatgpt_research.py`)
stays in the repository and keeps its own `chatgpt_client.py`; the two
copies can drift. The worktree also holds the user's unrelated in-progress
work (gap schema changes) with one failing repository test,
`test_schema_check.py::test_the_canonical_gap_id_conforms`, caused by that
work, not by the move.

## 2. The survey of chatgpt.com

Done on 2026-09-19 through Claude in Chrome on the user's logged-in browser,
read-only: menus and settings pages were opened and read; nothing was typed
into the composer, sent, pinned, deleted or changed. Only the Chat surface
was covered. The Work surface, Sites and Codex are out of scope for this
skill and must not be proposed.

**Sidebar.** New chat; global search dialog; Library (the account's file
library); Scheduled (tasks: a "Schedule a task" composer, pause, filter);
Plugins (installed apps and connectors, Create app, Public / Personal) with a
Skills tab at `/skills` (create, search, per-skill actions; the user's
"Research Pipeline" skill is installed there); More → Images, GPTs; Pinned
chats and projects; Projects; a Temporary chat toggle.

**Conversation page.** Header: Share, More → View files in chat, Pin chat,
Archive, Delete, Move to project. Per response: Copy, Share, Switch model →
Ask to change response, Try again, Use Pro mode, Don't search the web;
More actions → View sources, Open new branch, Read aloud.

**Composer.** "+" → Add photos & files, Add from library, Create image, Web
search, Deep research, Sketch, Google Drive, GitHub, Gmail, and a search box
over plugins, files, folders and skills. The model menu is a five-position
"Power" slider plus versions Latest / GPT-5.6 Sol / GPT-5.5 (GPT-5.5 leaves
on 14 October). Dictation and voice buttons.

**Projects.** Sidebar menu: Share project, Rename project, Project settings,
Project home, Pin project, Delete project. Project page: Share, a "…" menu,
a "New chat in <project>" composer, tabs Chats and Sources.

**Settings dialog**, deep-linkable as `#settings/<Tab>` (for example
`#settings/DataControls`, `#settings/Usage`, `#settings/Personalization`).
Tabs: General, Notifications, Personalization, Plugins, Voice, Billing,
Usage, Analytics, Data controls, Cloud browser, Storage, Safety, Security and
login, Parental controls, Trusted contact, Account. Contents that matter:

- General: Enable Ultra effort, Enable Dictation.
- Personalization: base style and tone, characteristics, Fast answers,
  Suggested prompts, Custom instructions, About you, Memory (enable, manage).
- Plugins: permission level (Allow low-risk actions), per-plugin toggles,
  Browse plugins, Developer mode.
- Usage: a weekly limit shared across Codex, Work, Workspace Agents and
  ChatGPT for Excel, which explicitly does not include chat conversations;
  two "Full reset" credits; a credits balance with auto-reload.
- Data controls: Improve the model, Location, Information shared with apps,
  Shared links, Archived chats, Archive all, Delete all, Export data.
- Cloud browser: default permission, site permissions, cookies.

**Endpoints the page calls**, recorded from its own traffic and reproduced
with the bearer token, are tabulated in `references/endpoint-discovery.md`
under "Additions seen on 2026-09-19": pins, the conversations filters
(`is_archived`, `is_starred`, `hide_snorlax`), `gizmos/<g-p-id>`, tasks,
memories, user_system_messages, models with `intelligence_presets`,
settings/user, wham/usage, rate-limit-reset-credits, remaining_balance,
installed plugins. The page pre-warms `sentinel/chat-requirements/prepare`
and `finalize` on load.

**Chrome rules that held.** Run `find` immediately before every click on
chatgpt.com; a stale reference once submitted a task. `browser_batch` is
denied there. Never type into the composer. Closing the last tab of the
extension's tab group removes the group; recreate it with
`tabs_context_mcp` and `createIfEmpty`.

## 3. What the skill supports now

**Superseded by section 0.** This table is the first session's list of
twelve commands. There are twenty now, and `SKILL.md`'s own table is the
current one; a T0 test fails if that table and `scripts/*.py` disagree, so
`SKILL.md` cannot go stale the way this one did.

| Command | Transport | Does |
|---------|-----------|------|
| `probe_account.py` | HTTP | auth, `/backend-api/me`, one listing; exit 0 when reads work |
| `probe_cookies.py` | local | which chatgpt.com cookies decrypt; never prints a value |
| `probe_send_gates.py` | HTTP | proof-of-work, Turnstile, `so` status from the sentinel; **has no argparse, so `--help` runs the probe** |
| `model_settings.py` | HTTP + local cookie | new: the Power slider's presets, the API's effort levels per model, the profile's cookie resolved to a preset |
| `profile_context.py` | HTTP + local cookie | added 2026-09-20 (Stage 1 item 1): custom instructions, memory usage, model + effort from cookie and server, one project's instructions and files; `--json PATH` for a run's archive |
| `list_chats.py` | HTTP | extended: `--match`, `--pinned`, `--archived`, `--no-project-chats`, a flags column (project / pinned / archived / temporary) |
| `list_projects.py` | HTTP | projects, instructions, chats inside one |
| `create_project.py` | browser | drives the UI, records `POST /backend-api/projects` |
| `read_chat.py` | HTTP | one conversation: turn finished, last reply |
| `clean_chats.py` | HTTP | archive or delete chats by title match; dry run unless `--apply` |
| `round_status.py` | local + HTTP | a research round: admitted, read, written off, unread, pending replies |
| `review_topic.py` | offline | six integrity invariants per round over a topic folder |
| `measure_window.py` | browser | memory and fill time of a send window |
| `discover_endpoints.py` | browser | record what the page calls; `--bodies` for mutations |

The bundled client (`chatgpt_client.py`) also provides the send path used by
the orchestrator: `BrowserSender` (new chat, continue, inside a project,
effort pinned through the `oai-last-model-config` cookie, large prompts as
an attached file, budgeted window), `wait_for_reply`, `rename`, `archive`,
`delete`, transcript helpers. **The cookie part is wrong and was removed**
(section 0): it never pinned anything, and `rewrite_send_body` does the job
now. `with_effort`, `with_model` and `EFFORT_COOKIE` no longer exist.

## 4. Findings that changed the docs

- **Effort.** The API has four `thinking_effort` values (`min`, `standard`,
  `extended`, `max`) and the client's `EFFORTS` is correct. The UI's Power
  slider is five *presets* from `versions[].intelligence_presets`: Instant,
  Medium (`standard`), High (`extended`), Extra High (`max`), Pro (switches
  the model to `gpt-6-pro`). "Ultra" is a setting
  (`model_picker_persists_ultra_effort`), not a level; do not add it. The
  orchestrator already pins effort per step through `TASK_EFFORT` -- which
  reached the wire for the first time on 2026-09-20; before that the cookie
  it used was ignored (section 0). The `SKILL.md` section "Reasoning
  effort" was rewritten from this evidence.
- **Pinned is `is_starred`.** `GET /backend-api/pins` lists pinned chats;
  the same chats carry `is_starred: true` in the conversations listing.
  `hide_snorlax=true` hides project chats, which is what the sidebar does.
- **Usage.** The weekly limit and `wham/usage` describe the Codex / Work
  window and credits, not chat sends. A chat-side 429 still shows only on
  the send path and in the rate-limit modal.
- **The file library is out of scope.** The Storage page in the user's
  ChatGPT account showed 1366 items; a census found only four were the
  orchestrator's `rp-prompt-*.md` attachments. The user ruled the whole
  topic out of this skill: no listing, no cleanup, no mention. A command
  written for it was removed. Do not reopen it.

## 5. The plan

`ROADMAP.md` stages every unsupported feature by evidence, risk and value:

- Stage 1, reads that close the audit gap: `profile_context.py` (custom
  instructions, memory state, model and effort preset, project instructions,
  saved next to a run), `list_projects.py --id --files`, credits and plan
  window in `probe_account.py`.
- Stage 2, writes with one captured action each: project instructions,
  pin / unpin / unarchive, memory isolation for worker chats (investigate
  first).
- Stage 3, the send path through the repository: model preset per step via
  the cookie, web search per step, then Deep research and attachments after
  measuring them.
- Deferred and not-planned lists are in the file.

**Progress.** The user said "start" on 2026-09-20 and Stage 1 item 1 is
built (`profile_context.py`; details and what it could not read are in
`ROADMAP.md` and `SKILL.md`, "Memory is a hidden input too"). Next: Stage 1
items 2 and 3, then the once-per-round call in the repository's
`chatgpt_research.py`.

## 6. Working conventions the user expects

These come from the user's own corrections in earlier sessions and are not
in this directory's memory, so they are repeated here.

- Reply in Simplified Chinese. Keep code, commands, paths and commit
  messages in English.
- Say where data lives before giving a number: "your ChatGPT account's
  storage page shows…", "in this directory…", "on the Pi…". A bare count
  was read as files downloaded to the Pi and caused real anger.
- Measure before blaming: the user's own browser working is evidence; check
  the specific failing path; three wrong causes were named in one day.
- Report progress as position/total, never a bare stage name.
- Never kill the orchestrator mid-dispatch; kill only between gates. After
  a host crash, harvest the reply over HTTP before re-sending.
- Check the Pi's wireless link before calling a service blocked.
- Detach long runs with `setsid`, not `nohup`.
- Do not ask questions the user's words or the project docs already answer;
  state a default with a revisit trigger.
- Verify ChatGPT facts against live account pages, not memory.
- The shell's working directory resets between commands in Claude Code;
  use absolute paths or start the session in the directory you need, which
  is what this session did.
