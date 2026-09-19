# Roadmap: what this skill does not do yet, and in which order

Written 2026-09-19 from a survey of chatgpt.com through Chrome and of the
endpoints the page actually calls (see `references/endpoint-discovery.md`,
"Additions seen on 2026-09-19"). Revisit when ChatGPT changes its UI or when
a research run needs something listed here.

## Rules that order the work

1. **Evidence before code.** A read is added once its endpoint has been seen
   in the page's own traffic. A mutation is added only after one real action
   has been recorded with `discover_endpoints.py --bodies`; a guessed POST
   body against a live account is junk that looks like a changed API.
2. **Reads first, writes second, the send path last.** Reads are free of
   risk. Writes act on the user's account and default to a dry run. Anything
   in the send path lives in `BrowserSender`, is fragile, and costs a
   browser window per test.
3. **The vendored client changes at its origin.** `chatgpt_client.py` is a
   copy of the research-pipeline repository's file (`VENDORED.md`). Send-path
   work is done there, tested there, then re-vendored; commands here never
   fork the client.
4. **Nothing that spends the user's limits or exposes their data is
   automated.** Pro lane, Ultra, Deep research and sharing are opt-in per
   run, never defaults.

## Inventory

Value is for the research runs this skill serves. Cost is the work to add
it, including capture. "Evidence" says whether the endpoint is already known.

| # | Feature | Kind | Evidence | Value | Cost | Stage |
|---|---------|------|----------|-------|------|-------|
| R1 | Record a run's hidden inputs: custom instructions, memory state, model + effort preset, project instructions | read | endpoints seen | high: makes runs auditable and repeatable | small | done 2026-09-20 |
| R2 | Project details: instructions and files of one project (`gizmos/<g-p-id>`) | read | seen | medium | small | 1 |
| R3 | Credits and plan limit window in `probe_account.py` (`wham/usage`) | read | seen; note the weekly window excludes chat | medium | tiny | 1 |
| R4 | Scheduled tasks list (`tasks`) | read | seen | low | small | deferred |
| R5 | Installed plugins and skills (`ps/plugins/installed`, `hazelnuts`) | read | seen | low: checks the research-pipeline skill is installed on chatgpt.com | small | deferred |
| R6 | Global search of chats by content | read | not captured; needs a typed query | medium | small once captured | deferred |
| M2 | Pin and unpin a chat (`is_starred`), unarchive | write | not captured; likely the conversation PATCH family | medium: mark a run's report chat | one recorded action each | 2 |
| M3 | Set or update a project's instructions | write | not captured (`gizmos/<id>` read is known) | high: house rules for workers become explicit per run | one recorded action | 2 |
| M4 | Memory isolation for worker chats: per-project memory setting, or `is_do_not_remember` on the conversation | write / send | not captured; conversation items expose `memory_scope` and `is_do_not_remember`; projects expose `memory_enabled` and `memory_scope` (all `global` on 2026-09-20); Project settings offers "Project-only memory" (read 2026-09-20, PATCH not captured); the account-wide "Enable memory" switch has no identified key in `settings/user` | high: stops runs from reading or writing the user's memory | investigate first | 2 |
| M5 | Move a chat into a project; rename or delete a project | write | not captured; deliberately unimplemented so far | low | one recorded action each | deferred |
| M6 | Memory entries and custom instructions: edit | write | not captured | low, and it changes the user's global profile | one recorded action | not planned |
| M7 | Scheduled task create or pause; share links | write | not captured | low | one recorded action | not planned |
| B1 | Model preset per step through the cookie (`oai-last-model-config` carries `model`) | send | cookie seen; whether the composer honours `gpt-6-pro` from it is untested | medium | small experiment, no send needed to read the label | 3 |
| B2 | Web search on or off per step | send | UI item seen; the send body hint not captured | medium: search steps on, analysis steps off | medium | 3 |
| B3 | Deep research as an optional step type | send | UI item seen; not captured | medium, uncertain: long turns, different output shape | large | 3 |
| B4 | Attach arbitrary files to a send, such as a paper PDF | send | `_attach_prompt` exists for prompts | uncertain: attachments are retrieval-backed and may truncate | medium; measure first | 3 |
| B5 | Temporary chat for workers | send | button seen | low: a temporary chat may not be readable afterwards, which breaks collection | small | not planned |
| B6 | Regenerate, branch, edit, read aloud, canvas, voice, images | send | seen | none for runs | — | not planned |

## Stage 1: reads that close the audit gap

Goal: every hidden input of a run can be printed and saved before the first
send. All endpoints are already observed; no browser, no writes.

1. `profile_context.py`: prints and, with `--json`, writes the custom
   instructions text, whether memory is on and how many entries it has, the
   model and effort preset the profile will use (reuses `model_settings.py`),
   and, with `--project g-p-…`, that project's instructions and files.
   Acceptance: the orchestrator can call it once per round and store the
   output next to `chatgpt/conversations.json`.
   **Done 2026-09-20**: `profile_context.py [--project g-p-…] [--json PATH]`,
   ten tests over a fake session, verified on the live account. Memory is
   reported as usage (tokens, entry count, per-project scope) because the
   account-wide switch has no identified key; the model comes from both the
   cookie and the server's `last_used_model_config`. The once-per-round call
   is repository work in `chatgpt_research.py`, still to do.
2. `list_projects.py --id … --files`: full instructions and the file list
   from `gizmos/<g-p-id>` instead of the sidebar's truncated copy.
3. `probe_account.py`: two more lines from `wham/usage`, credits balance and
   the plan window, labelled as not covering chat sends.

Exit criterion: a round's archive states what the profile looked like when
the round started.

## Stage 2: writes, one captured action each

Goal: a run can shape its own project and leave the user's account tidy,
with a dry run before every change.

1. Project instructions set or update (M3). Capture by editing the
   instructions of the run's own project once in the recorded window.
2. Pin and unpin, unarchive (M2). Capture by pinning one worker chat.
3. Memory isolation (M4). First read what Project settings offers and what
   the conversation's `memory_scope` values mean; then decide between a
   project-level setting and a per-send flag. No code before that reading.
Exit criterion: each command has a dry run, a test over a fake session, and
its endpoint recorded in `references/endpoint-discovery.md`.

## Stage 3: the send path, through the repository

Goal: the orchestrator chooses model, search and attachments per step the
way it already chooses effort.

1. Model preset per step (B1): extend `with_effort` to `with_model`; verify
   by opening a composer with the cookie set and reading the label, no send.
2. Web search per step (B2): capture the send body with and without the
   composer's Web search item, then reproduce the difference.
3. Deep research (B3) and file attachments (B4): measure on one real paper
   before designing anything, because both change the shape and the timing
   of what comes back.

Exit criterion: `TASK_EFFORT` gains sibling tables for model and search, and
the tests in the repository cover them.

## Not planned

Archive all, delete all, export data, account, billing and security
settings, cloud-browser permissions, the Work surface and Sites, GPTs,
sharing links. These stay manual.
