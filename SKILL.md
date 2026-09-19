---
name: chatgpt-web-operations
description: Use when driving chatgpt.com from this machine — sending prompts, collecting replies, listing or cleaning up worker conversations, diagnosing a stalled or failing ChatGPT run, judging whether the account is really blocked, or rediscovering ChatGPT's endpoints after they change. Self-contained: it bundles the chatgpt.com client and its own .venv, so it works from any project. Do NOT use for designing research topics or gates; that is the research-pipeline skill.
---

# Operating ChatGPT from this machine

Everything here was measured, most of it after getting it wrong first. The
commands are small and single-purpose so they can be combined; none of them
is a do-everything entry point.

```bash
S=~/.claude/skills/chatgpt-web-operations/scripts   # wherever this SKILL.md sits, plus /scripts
```

## Where this lives, and what it needs

This directory is self-contained and can be moved as a whole: every path in
it is resolved from the scripts' own location, and no command imports
anything from a repository checkout. It carries its own copy of the
chatgpt.com client (`scripts/chatgpt_client.py`), the cookie helpers
(`chatgpt_session.py`, `chatgpt_cookies.py`) and the round bookkeeping
(`round_state.py`); `VENDORED.md` records where each came from. Since
2026-09-20 they are owned and changed here.

The commands run in the skill's own virtual environment, `.venv`, and switch
to it by themselves: `python3 $S/<command>` works from any directory, and
tells you to run `bootstrap.sh` when the venv is missing. Create it once:

```bash
bash ~/.claude/skills/chatgpt-web-operations/bootstrap.sh
```

`bootstrap.sh` builds `.venv` with `--system-site-packages` on purpose:
decrypting Chrome's cookies goes through the GNOME keyring over D-Bus, and
`python3-dbus` is an apt package that pip cannot build cleanly, so the venv
borrows it from the system and the script refuses to continue if it is not
there. Playwright and `cryptography` are installed into the venv itself.
Sending and the three browser commands also need Google Chrome and Xvfb on
the host.

The orchestrator that produces the runs these commands inspect stays in the
research-pipeline repository (`.github/scripts/chatgpt_research.py`); this
skill does not import it.

## The commands

Each does one kind of interaction. Four change something: `clean_chats.py`
and `project_settings.py` need `--apply`, `create_project.py` creates a
project, and `send_prompt.py` posts a message.

| Command | Purpose | Exit code means |
|---------|---------|-----------------|
| `probe_account.py` | Does the account answer at all? Auth, `/me`, one listing, plan window and credits. | 0 reads work |
| `probe_cookies.py` | Which cookies decrypt. Never prints a value. | 0 session cookie readable |
| `probe_send_gates.py` | What a send requires right now: proof-of-work, Turnstile, `so`. | 0 no browser needed |
| `model_settings.py` | Which model and effort a send will use: the Power slider's presets, and the profile's cookie resolved to one. | 0 cookie resolves |
| `profile_context.py` | The hidden inputs of a run: custom instructions, memory usage, model and effort from cookie and server, one project's instructions and files. `--json` keeps them beside a run. | 0 every read answered |
| `list_chats.py` | Recent conversations by title substring; `--pinned`, `--archived`, `--no-project-chats`; flags project / pinned / archived. | 0 always |
| `list_projects.py` | Every project (paged), one project's full instructions and files, and the chats inside one. | 0 found, 1 no such `--id` |
| `create_project.py` | Create a project by driving the UI, and report the call that did it. | 0 created |
| `project_settings.py` | Set a project's instructions and memory scope (`--memory project-only` keeps its chats out of your memory). Dry run unless `--apply`. | 0 dry run or verified, 1 apply failed, 2 refused |
| `send_prompt.py` | Send a prompt: a new chat (in a project with `--project`) or a continuing one with `--chat`; `--effort` and `--model` pin the composer's cookie, `--title` renames once the id resolves, `--json` records the send; waits for the reply unless `--no-wait`. `--attach` is recorded, not uploaded yet. | 0 sent and replied, 1 send, resolve or wait failed, 2 bad arguments |
| `read_chat.py` | One conversation: is the turn finished, and what did it say? | 0 turn finished |
| `clean_chats.py` | Archive or delete worker chats. Dry run unless `--apply`. | 0 always |
| `round_status.py` | A research round: admitted, read, written off, unread. | 0 nothing unread |
| `review_topic.py` | A finished topic, offline: six integrity invariants per round, review verdicts, gaps, cost. | 0 nothing wrong |
| `measure_window.py` | What a send window costs in memory and fill time. | 0 always |
| `discover_endpoints.py` | Record what endpoints the page calls, and with `--bodies` what they sent. | 0 always |

`_common.py` holds only session bootstrap and table formatting.

## Combining them

**A run has stalled.** Is it blocked, or just slow?

```bash
python3 $S/probe_account.py || python3 $S/probe_cookies.py   # account, then the usual culprit
python3 $S/round_status.py <workdir>                     # what is outstanding
python3 $S/review_topic.py <workdir>                 # was the whole topic sound?
python3 $S/read_chat.py <chat-id>                        # is the reply already written?
```
A finished reply is collected over HTTP, so a send-path rate limit does not
stop it. That check turned one 30-minute idle wait into nothing.

**A send fails but reads work.** `probe_send_gates.py`, then
`measure_window.py --fill-file <the prompt>` to see whether the fill is
simply too slow on a loaded host.

**A run left conversations behind.** `list_chats.py --match "rp "` then
`clean_chats.py --match "rp " --delete` to review, and `--apply` to act.

**Keep them out of the main list in the first place.** See Projects below.

**Before a run starts.** `profile_context.py --project g-p-<id> --json
<workdir>/chatgpt/profile_context.json` records what the workers will
inherit: custom instructions, memory, the model and effort preset, the
project's instructions. Without it the archive cannot say which profile
produced a round. The summary prints lengths and counts only; the text goes
to the JSON. `review_topic.py` warns, without failing, when a topic has no
such file or the newest one predates the newest round.

**ChatGPT changed something.** `discover_endpoints.py`, then
`references/endpoint-discovery.md` for how to read the output.

## Projects keep a run's chats out of the user's list

A project groups conversations, carries its own instructions, and its chats
do not appear in the main conversation list. That is the better answer to
"do not pollute my chat list" than deleting workers afterwards: nothing has
to be cleaned up, and a failed run leaves its evidence somewhere findable
rather than gone.

```bash
python3 $S/list_projects.py                       # id, name, files, updated
python3 $S/list_projects.py --id g-p-<id> --chats # its URL, instructions, chats
```

Projects are **snorlax gizmos** in the backend, which is why the endpoints
say gizmo and nothing says project:

| Endpoint | Use |
|----------|-----|
| `GET /backend-api/gizmos/snorlax/sidebar?owned_only=true&limit=50` | projects, pinned first, with instructions and files. **Paged**: without `limit` it returns 5 and a `cursor`; `list_projects.py` walks every page since 2026-09-20, so its printed count is the true total |
| `GET /backend-api/gizmos/<g-p-id>/conversations` | the chats inside one |

A project's `short_url` gives its page: `https://chatgpt.com/g/<short_url>/project`.
**Composing there creates the chat inside the project**, so the browser send
path needs only to navigate to that URL instead of the home page. The
`gizmo_id` query parameter on `/backend-api/conversations` is accepted and
ignored, so do not filter that way; use the gizmo endpoint.

```bash
python3 $S/create_project.py "msgloom research workers"   # --dry-run to rehearse
```

`create_project.py` drives the same control a person would
(`button[aria-label="New project"]`) and reports the call that returned the
new `g-p-…` id, so the endpoint is recorded from evidence rather than
guessed. It takes a browser slot, so it cannot open a second window beside a
run's send, and it **refuses to act while the rate-limit modal is up**: that
modal is `[data-testid="modal-conversation-history-rate-limit"]`, it blocks
every click underneath it, and clicking past it earns a longer limit.

Moving an existing chat into a project is deliberately not implemented. It is
not a normal need here, since a worker created in the project is already
where it belongs.

Point a run at a project with `chatgpt_research.py --project g-p-<id>`.

## The transport split is the first thing to know

**Reads are plain HTTP. Only sending needs a browser.** Listing, reading,
polling, renaming, archiving and deleting are `GET`/`PATCH` on
`/backend-api/*` with the session bearer token.

`POST /backend-api/f/conversation` is gated by three controls, all
`required: true` on a live paid account (2026-09-16): proof-of-work,
Cloudflare Turnstile, and `so`, a **behavioural** collector backed by ~40
`__oai_so_*` page globals for keypress, pointer, scroll and input timing.

**Do not build a solver for these, in Python or Node.** Reproducing `so`
means fabricating human-interaction telemetry, which is defeating an
anti-automation control rather than speaking a protocol. Say so plainly, and
answer the memory question instead, which is the real reason anyone asks.
Re-check with `probe_send_gates.py` rather than trusting this paragraph.

## Reasoning effort is a setting; know which one a send will use

Every send carries a `thinking_effort`. The orchestrator pins it per step
(`TASK_EFFORT` in `chatgpt_research.py`) by rewriting the cookie before each
send. Anything that sends without an `effort` inherits whatever the browser
profile last used, which happened to be `max` during Topics 01-04 by luck:
change it in the web UI and every such send silently follows, with nothing in
the archive recording what was used.

**Where it lives.** A top-level field in the send body, and one cookie:

```
POST /backend-api/f/conversation
{"model": "gpt-5-6-thinking", "thinking_effort": "max", …}

oai-last-model-config   {"model": "gpt-5-6-thinking", "effort": "max"}
```

`oai-last-model-config` is what the chat composer reads and what
`with_effort` rewrites. The neighbouring `oai-tpp-model-settings` cookie
belongs to a different surface: its `*-wm` model slugs never appear in the
chat composer, so do not read a run's setting from it.

The server keeps its own record in `GET /backend-api/settings/user`, under
`settings.last_used_model_config`: `slugs` is the last model per surface
(`web`, `ios_app`, `windows_app`) and `juices` the last effort per model per
surface (2026-09-20: `web` had `gpt-5-6-thinking: max`, the same as the
cookie). Whether the composer restores from it when the cookie is absent was
not tested, so `with_effort` keeps rewriting the cookie. `profile_context.py`
prints both records, so a drift between browser and server is visible.

**The UI control** (checked 2026-09-19) is the **Power** slider in the
composer's model menu. Its five positions are the `intelligence_presets` of
the selected version in `GET /backend-api/models`, not five effort levels:

| Position | Preset | What it sends |
|----------|--------|---------------|
| 1 | Instant | `gpt-5-6-instant`, no effort |
| 2 | Medium | `gpt-5-6-thinking`, `standard` |
| 3 | High | `gpt-5-6-thinking`, `extended` |
| 4 | Extra High | `gpt-5-6-thinking`, `max` |
| 5 | Pro | `gpt-6-pro`, no effort field |

**The API levels are unchanged**, from a model's `thinking_efforts` when
`configurable_thinking_effort` is true:

| Value | Label | What OpenAI says |
|-------|-------|------------------|
| `min` | Light thinking | Quick, thoughtful answers |
| `standard` | Thinking | Balanced thinking and speed |
| `extended` | Extended thinking | Thinks longer for complex questions |
| `max` | Heavy thinking | Even longer analysis for harder questions |

`min` is accepted by the API and by `--effort` although the slider skips it.
Pro models start at `standard` and offer fewer levels. Position 5 changes
the *model*, which `with_effort` never does: a run cannot select Pro today.

**"Ultra" is not an effort level.** Settings → General has "Enable Ultra
effort", stored as `model_picker_persists_ultra_effort` in
`/backend-api/settings/user`. The models payload contains no `ultra` value
and no Ultra preset, so do not add it to `EFFORTS`. What the picker sends
with Ultra engaged is unknown until a send is captured with
`discover_endpoints.py --bodies`.

**Check what a send will use** with `model_settings.py`: it prints the
presets, the API levels per model, and the profile's cookie resolved to a
preset. A transcript records the model slug but not the effort, so the
effort is only knowable at send time unless it is logged.

**Which level each research step should use** is decided in the
research-pipeline repository's `docs/chatgpt-research-effort-levels.md`:
`max` for the four steps whose mistakes cannot be recovered later (admission,
per-paper analysis, synthesis, review) and `extended` for the four that
restate or bound settled work. Raising every step is not free, because longer
turns mean more polling and polling volume is what earns this account its
rate limits.

## Memory is a hidden input too

Workers read the account's memory unless something turns it off, and nothing
in a transcript says whether it did. What the reads expose (2026-09-20):

- `GET /backend-api/memories?include_memory_entries=false` gives the usage,
  `memory_num_tokens` of `memory_max_tokens`; with `=true` it returns the
  entries themselves. `profile_context.py` counts them and keeps no content.
- Each project carries `memory_enabled` and `memory_scope`, both in the
  sidebar and in `gizmos/<g-p-id>`; every project read `global` that day.
- Project settings (the project's "…" menu) offers "Default memory" or
  "Project-only memory": with the second, the project's chats use only their
  own memory and it stays hidden from outside chats. That is the lever for
  isolating worker chats. Captured 2026-09-20: `PATCH
  /backend-api/projects/<g-p-id>` with `memory_scope` `project_v2` (project
  only) or `global` (default), alongside name and instructions; see
  `references/endpoint-discovery.md`. A chat created inside such a project
  reports `memory_scope: project_v2` itself; elsewhere chats read
  `global_enabled`. `project_settings.py --memory project-only` sets it.
- The account-wide switch is **Enable memory** at `#settings/Personalization`
  (the older "Reference saved memories / chat history" pair is gone). Its
  state is not carried by `memories` or by any named key in `settings/user`,
  whose booleans are codenamed (`moonshine`, `m3m`, `sunshine`,
  `golden_hour`) and tied to nothing by evidence. Read the page for it;
  capturing the PATCH when it is toggled is Stage 2 work (`ROADMAP.md`, M4).

## Name the path that failed, never "the account"

Sending, reading, polling and authenticating fail independently.

- **The user's own browser working is evidence.** It rules out an account
  block immediately. They have said so twice; believe it.
- A send-path 429 does **not** stop reads.
- A remembered rule ("let it settle 30 minutes") is a prior, not a
  measurement. Check the current state before idling anything.
- When suspecting your own change, run the old and new side by side on the
  real input. That has twice saved a correct change from being reverted.

## Never interrupt a dispatch

Killing the orchestrator while shards are in flight is the most expensive
thing you can do here. One interrupt produced a research round whose report
was the previous round's evidence rewritten.

- Safe points are **between gates**: after a `<task>: done` line and before
  the next `dispatching` line.
- A shard recorded in `chatgpt/conversations.json` as `sent` is recoverable;
  `resume_job` collects it instead of re-sending. One killed before that
  record is written is lost.
- After any interrupt, run `round_status.py` before trusting the round.

## Ask the real question, not a cheap proxy

Four separate places accepted work as finished because they tested something
easy instead of something true: "an analysis file exists", "the artifact
parses", "a result file of that name is present", "the shortlist had new
papers". Together they let a round report on 13 of 29 papers.

When writing a completion check, ask what you actually care about, and make
it name what is missing. The log line is what makes the next failure legible.

## The browser is budgeted

A window costs ~1.26 GB idle and ~1.76 GB with a large prompt in the
composer, on a 16 GB host that also runs the user's Chrome (~4.4 GB). Limits
are environment variables so they bind **every** process that opens one — a
probe script run by hand is how a second window appears.

    RP_MAX_BROWSERS=1  RP_BROWSER_MAX_SECONDS=1500
    RP_MIN_AVAILABLE_MB=4000  RP_BROWSER_WAIT_SECONDS=900

Refusing to open is correct: a window opened into swap makes every Playwright
call time out, so the send fails anyway and the host suffers. The lifetime
ceiling is enforced by SIGKILL, not `close()`, because a hung Playwright call
blocks its owner thread. Only processes carrying Playwright's temporary
profile are ever killed.

## Rate limits

A 429 has its own ladder, 300/900/1800 s, and its own counter; the generic
60/120/240 s spends the whole error budget while the limit is still in force.
Tune with `--rate-limit-backoff`.

**The rate-limit notice is not a send block.** Its test id is
`modal-conversation-history-rate-limit` and its text says access to
*conversations* is limited: it is about the history the sidebar reads, which
polling hammers, not about posting. Dismissing it leaves ChatGPT working —
the user confirmed that from their own browser, and refusing to send on sight
of it aborted a run five minutes into a limit that clears in about thirty.

So clear it and try the send. Only a send that then fails while the notice is
still on the page counts as a 429, which is when the ladder should run. That
way every occurrence measures the block instead of assuming it.

Other modals get Escape and only Escape: a dialog's buttons could accept
terms or change a setting, and this runs unattended.

## Do not compete with a send for the CPU

Filling ProseMirror is CPU work. A 172 kB review prompt blew its 600 s budget
at load average 5 on this four-core host and took the round with it; the same
size filled fine minutes earlier. Before starting a scan or a test suite,
check whether a send is in flight (`dispatching` with no matching `done`).

## Further reading

- `references/failure-atlas.md` — every failure, what it actually was, and
  the one command that would have settled it.
- `references/endpoint-discovery.md` — how to rebuild the transport map when
  ChatGPT changes.
- `ROADMAP.md` — what is not supported yet, staged by evidence, risk and
  value; start there before adding a command.

## House facts

- Cookies come from the bundled `chatgpt_session.py` (binnacle's helper,
  vendored). One unreadable cookie must never end the session.
- Kill with `pkill -f "chatgpt_researc[h].py"` so the pattern does not match
  the calling shell.
- Report progress as position/total (`14/18`), never a bare stage name.
