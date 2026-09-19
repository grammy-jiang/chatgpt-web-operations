# Handover: chatgpt-web-operations, 2026-09-20

Written for the Claude Code session that starts in this directory, by the
session that ran in `~/Projects/research-pipeline-chatgpt` on 2026-09-19 and
2026-09-20. Read this first, then `SKILL.md`, `ROADMAP.md`,
`references/endpoint-discovery.md`, `references/failure-atlas.md` and
`VENDORED.md`. Everything below was measured on the user's live ChatGPT Pro
account or in this directory; nothing is assumed.

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
`delete`, transcript helpers.

## 4. Findings that changed the docs

- **Effort.** The API has four `thinking_effort` values (`min`, `standard`,
  `extended`, `max`) and the client's `EFFORTS` is correct. The UI's Power
  slider is five *presets* from `versions[].intelligence_presets`: Instant,
  Medium (`standard`), High (`extended`), Extra High (`max`), Pro (switches
  the model to `gpt-6-pro`). "Ultra" is a setting
  (`model_picker_persists_ultra_effort`), not a level; do not add it. The
  orchestrator already pins effort per step through `TASK_EFFORT`. The
  `SKILL.md` section "Reasoning effort" was rewritten from this evidence.
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
