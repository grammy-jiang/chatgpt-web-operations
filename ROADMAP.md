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
3. **The bundled client is owned here** (decision 2 below; before
   2026-09-20 it was re-vendored from the research-pipeline repository, see
   `VENDORED.md`). Send-path work is done in `scripts/chatgpt_client.py` and
   tested here; the repository is not modified any more.
4. **Nothing that spends the user's limits or exposes their data is
   automated.** Pro lane, Ultra, Deep research and sharing are opt-in per
   run, never defaults.

## Decisions of 2026-09-20

The user approved the staged plan and answered its open points:

1. Every recorded action (the Stage 2 captures, the Stage 3 sends) is
   performed by the agent through Chrome, one action at a time, announced
   before it is made, on the sandbox project or a worker chat only; never on
   the user's own chats or global settings.
2. The research-pipeline repository and its worktree
   `~/Projects/research-pipeline-chatgpt` are not touched any more; all work
   stays in this skill. Consequences: Stage 1 item 4 (the orchestrator's
   once-per-round call) becomes a pre-run step documented in `SKILL.md` plus
   a `review_topic.py` warning when a topic lacks a fresh
   `chatgpt/profile_context*.json`; Stage 3 changes the skill's own client
   and gets its own send entry point, `send_prompt.py`, instead of tables in
   `chatgpt_research.py`; `VENDORED.md` is provenance only.
3. A failed profile read warns and continues; the document records the
   failures.
4. Order: 1.2 → 1.3 → 1.4 → 2.1 (M3) → 2.2 (M4) → 2.3 (M2) → 3.1 (B1) →
   3.2 (B2) → 3.3 (B4) → 3.4 (B3).
5. Stage 3 may spend one or two real messages per item, inside the sandbox
   project, and every test conversation is deleted afterwards. The test
   tiers, their guards and the coverage gates (95% per core module, 90% per
   other module) are in `TESTING.md`; the harness comes first, then the
   existing modules are brought to the bar, then the stages.

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
| M3 | Set or update a project's instructions | write | captured 2026-09-20: `PATCH /backend-api/projects/<g-p-id>` with the full body (name, instructions, emoji, theme) | high: house rules for workers become explicit per run | command pending | 2 |
| M4 | Memory isolation for worker chats: per-project memory setting, or `is_do_not_remember` on the conversation | write / send | not captured; conversation items expose `memory_scope` and `is_do_not_remember`; projects expose `memory_enabled` and `memory_scope` (all `global` on 2026-09-20); Project settings' "Project-only memory" captured 2026-09-20: the same PATCH with `memory_scope` `project_v2` or `global`; the account-wide "Enable memory" switch has no identified key in `settings/user` | high: stops runs from reading or writing the user's memory | investigate first | 2 |
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
   would have been repository work; by decision 2 it became item 4.
2. `list_projects.py --id … --files`: full instructions and the file list
   from `gizmos/<g-p-id>` instead of the sidebar's truncated copy.
   **Done 2026-09-20**: `--id` now reads `gizmos/<id>` directly with the full
   instructions and memory scope, `--files` lists scalar file fields, and the
   bare listing pages every sidebar page so its count is the true total.
3. `probe_account.py`: two more lines from `wham/usage`, credits balance and
   the plan window, labelled as not covering chat sends.
   **Done 2026-09-20**: added, plus a third line stating plainly that the
   window and credits exclude chat sends; a failed or missing usage read is
   reported and never turns CLEAR into NOT CLEAR.
4. The pre-run step, in the skill: `SKILL.md` documents
   `profile_context.py --project … --json <workdir>/chatgpt/profile_context.json`
   before a run, and `review_topic.py` gains a seventh invariant that warns
   when a topic has no such file or its `captured_at` is older than the
   newest round. A per-round capture would need the orchestrator, which
   stays untouched.
   **Done 2026-09-20**: `SKILL.md` documents the pre-run call, and the
   seventh invariant warns (without changing the exit code) when a topic has
   no `chatgpt/profile_context*.json` or the newest one predates the start of
   the newest round.

Exit criterion: a topic's archive states what the profile looked like when
the run started, and `review_topic.py` says so when it does not.

## Stage 2: writes, one captured action each

Goal: a run can shape its own project and leave the user's account tidy,
with a dry run before every change.

1. Project instructions set or update (M3). Captured 2026-09-20 on the
   sandbox project and reproduced over HTTP (`references/endpoint-discovery.md`,
   "Captured 2026-09-20"); the command is next.
2. Memory isolation (M4). Project settings offers "Default memory" or
   "Project-only memory", and both directions were captured 2026-09-20 in
   the same PATCH (`memory_scope` `project_v2` / `global`). The sandbox now
   runs project-only. Still to verify: what a chat created inside such a
   project reports as its own `memory_scope`.
3. Pin and unpin, unarchive (M2). Capture by pinning one sandbox chat.
Exit criterion: each command has a dry run, a test over a fake session, and
its endpoint recorded in `references/endpoint-discovery.md`.

## Stage 3: the send path, in the skill's own client

Goal: a send can choose model, search and attachments the way it already
chooses effort, through the skill's own entry point `send_prompt.py`
(`--project`, `--effort`, `--model`, `--search`, `--attach`); the
orchestrator is not modified.

1. Model preset per step (B1): extend `with_effort` to `with_model`; verify
   by opening a composer with the cookie set and reading the label, no send.
2. Web search per step (B2): capture the send body with and without the
   composer's Web search item, then reproduce the difference.
3. File attachments (B4), then Deep research (B3): measure each on one real
   paper before designing anything, because both change the shape and the
   timing of what comes back. Attachments come first because they feed the
   existing analysis steps; Deep research is a new kind of step.

Exit criterion: `send_prompt.py` exposes the four choices, the client's
tests cover them offline, and one measured send per feature is recorded in
`references/failure-atlas.md`.

## Not planned

Archive all, delete all, export data, account, billing and security
settings, cloud-browser permissions, the Work surface and Sites, GPTs,
sharing links. These stay manual.
