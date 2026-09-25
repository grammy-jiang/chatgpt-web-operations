---
name: chatgpt-web-operations
description: >-
  Use when driving chatgpt.com from this machine — sending prompts, collecting
  replies, listing or cleaning up worker conversations, diagnosing a stalled
  or failing ChatGPT run, managing OpenAI tunnels for local MCP connectors,
  or rediscovering ChatGPT's endpoints after they change. Self-contained: it
  bundles the chatgpt.com client and its own .venv, so it works from any project.
  Do NOT use for designing research topics or gates; that is the research-pipeline
  skill.
---

# Operating ChatGPT from this machine

Use the command for the requested operation. ChatGPT account operations and
OpenAI Platform tunnel operations use different credentials.

```bash
S=~/.claude/skills/chatgpt-web-operations/scripts   # wherever this SKILL.md sits, plus /scripts
```

The Codex and shared-agent skill paths are symlinks to this same directory.
The current verification record is `VERIFICATION.md`. Use its dated results
to distinguish live verification from implemented but unmeasured options.

## Start here

On first use, explain which access the task needs and check the existing
configuration before asking the user to configure anything. Report the
missing prerequisite, the action that fixes it, and the read-only check that
will confirm the fix. Reuse working configuration on later runs.

| Task | Required access | First check |
| --- | --- | --- |
| Read chats, manage projects or manage ChatGPT connectors | This desktop user's signed-in ChatGPT session in Chrome and unlocked GNOME keyring | `python3 "$S/preflight.py"`; add `--browser` before sending or using browser diagnostics |
| Create, list, update or delete Platform tunnels | Platform management credential and the intended organization; separate from ChatGPT login | Follow [tunnel authentication and its read-only check](references/tunnels.md#credentials) |
| Inspect one tunnel | Platform management credential or the configured runtime key | `manage_tunnels.py get` with the exact tunnel id |
| Expose a new local MCP server end to end | Server, runtime key, forwarding daemon, Platform tunnel and ChatGPT connector | Use the adjacent `chatgpt-mcp-onboarding` skill |

If `.venv` is missing, run `bootstrap.sh` as described below. For a new
installation or a failed check, read [setup and recovery](references/setup.md).
The admin key comes from the exported `OPENAI_ADMIN_KEY` first, then
`~/.config/tunnel-client/admin.env`. The command does not load `.bashrc`.
Automatic authentication can also select an explicitly configured dashboard
token; the exact order is in the tunnel reference. Never ask the user to
paste a credential into chat.
For missing access, give the user the applicable
[credential creation page and required permissions](references/tunnels.md#obtain-the-keys-or-session).
ChatGPT access comes from normal browser sign-in; no token copy is required.

When a check fails, identify the failing path: local tooling, ChatGPT login,
Platform authentication, tunnel forwarding or the MCP server. Apply an
available fix within the task's scope, then repeat the relevant read-only
check. If the user must sign in, unlock the keyring or supply access, state
that specific action. A failed request alone does not establish an account
block. After a timeout on a create or send, inspect the recorded resource or
conversation before repeating the mutation.

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
Sending and browser diagnostics also need Google Chrome and Xvfb on
the host. See [setup and recovery](references/setup.md) for installation
checks and fixes; `bootstrap.sh` does not install those host packages.

The orchestrator that produces the runs these commands inspect stays in the
research-pipeline repository (`.github/scripts/chatgpt_research.py`); this
skill does not import it.

## The commands

There are 29 command scripts and five support modules. `clean_chats.py`,
`project_settings.py`, `pin_chat.py` and `delete_project.py` need `--apply`.
`create_project.py`, `create_connector.py` and `connect_connector.py` act
unless `--dry-run` is supplied. `delete_connector.py` needs `--confirm`.
`send_prompt.py` and `deep_research.py start` post messages.
`round_status.py --collect --apply` writes local evidence and its ledger.
`manage_tunnels.py create` acts unless `--dry-run`; its `update` needs
`--apply`, and its `delete` needs `--confirm`.

Browser paths are sending, Deep research start, `preflight.py --browser`,
`health.py --browser`, `measure_window.py`, and `discover_endpoints.py`.
Other account operations use HTTP. Read-only searches and connector lookups
use POST; HTTP method alone does not determine whether an operation writes.

| Command | Purpose | Exit code means |
|---------|---------|-----------------|
| `preflight.py` | One go/no-go check before a run starts: host, link, account and run over one session; `--browser` adds a composer check, `--project` and `--workdir` add their own. | 0 GO, 1 DO NOT START (n blocking), 2 GO WITH WARNINGS (n) |
| `health.py` | The daily health check's HTTP half, for a cron wrapper: preflight's host, link, account and run groups, plus health checks and the skills-inventory read over the same session (session-token expiry from the cookie jar, the live-test sandbox's identity and cleanliness, two reads preflight never makes; a skill's status -- blocked, disabled or missing -- never fails the check, only a failed skills read does). `--browser` adds preflight's composer check; `--json PATH` writes the verdict plus a `facts` block; `--skills-json` saves the redacted inventory and `--diagnostics` saves credential-free transport events. | 0 GO, 1 DO NOT START (n blocking), 2 GO WITH WARNINGS (n) |
| `probe_account.py` | Does the account answer at all? Auth, `/me`, one listing, plan window and credits. | 0 reads work |
| `probe_cookies.py` | Which cookies decrypt, and when the session token expires (this probe does not renew cookies; session creation can renew the keyring copy). Never prints a value. `--json PATH` for a caller that wants the number. | 0 session cookie readable |
| `probe_send_gates.py` | What a send requires right now: proof-of-work, Turnstile, `so`. | 0 no browser needed |
| `model_settings.py` | Which model and effort a send will use: the Power slider's presets, the API levels, and the account's `last_used_model_config` resolved to a preset. | 0 record resolves |
| `profile_context.py` | The hidden inputs of a run: custom instructions, memory usage, model and effort from cookie and server, one project's instructions and files. `--json` keeps them beside a run. | 0 every read answered |
| `list_chats.py` | Recent conversations by title substring; `--pinned`, `--archived`, `--no-project-chats`; flags project / pinned / archived. | 0 always |
| `search_chats.py` | Global search by content, not just title (`list_chats.py --match` cannot see inside a chat): `--limit` (server cap 40), `--pages` follows the `cursor`, `--json PATH`. Conversations only, never project or library sources. | 0 at least one hit, 1 no hit or a page failed, 2 bad argument |
| `list_projects.py` | Every project (paged), one project's full instructions and files, and the chats inside one. | 0 found, 1 no such `--id` |
| `list_automations.py` | ChatGPT's scheduled tasks ("automations", `chatgpt.com/scheduled`): `--filter scheduled\|paused\|finished\|all` (default scheduled; `all` fetches all three and adds a state column), `--prompts` shows the task's own instruction text, `--json PATH`. A non-null `cursor` is reported, never followed (the paging parameter is unknown). | 0 every filter read, 1 a filter's read failed, 2 bad `--filter` |
| `list_skills.py` | Skills and apps installed on the account: skills from `hazelnuts`, `--apps` adds installed apps and connectors from `ps/plugins/installed`. `--expect NAME` (repeatable) checks a skill is installed and enabled. `safety_check_status` and version fields print verbatim, never interpreted. `--json PATH`. | 0 listed and every `--expect` met, 1 a read failed or an `--expect` unmet, 2 bad arguments |
| `list_connectors.py` | List accessible links and custom MCP apps; filter names or ids, inspect details, list available tunnels, or print JSON. Read-only HTTP, including lookup POSTs. | 0 listed, 1 a read failed, 2 bad arguments |
| `create_connector.py` | Create a custom MCP app on an existing tunnel with No Auth. `--dry-run` previews the request. | 0 created or dry run, 1 create failed or name taken, 2 bad arguments |
| `connect_connector.py` | Connect a No Auth custom MCP app and discover its tools. Optionally set `--apps-privacy full_access`. `--dry-run` previews both requests. | 0 connected or dry run, 1 connect or privacy update failed, 2 bad arguments |
| `delete_connector.py` | Delete a custom MCP app's links first, then its app; or delete one link. Stops if a link deletion fails. Dry run unless `--confirm`. | 0 deleted or dry run, 1 lookup or deletion failed, 2 bad arguments |
| `manage_tunnels.py` | OpenAI Platform tunnel list/get/create/update/delete over HTTP. Uses a separate Platform management credential; runtime credentials permit get only. Create/update read back the fields, and delete requires a 404. Update/delete default to previews. See `references/tunnels.md`. | 0 completed or previewed, 1 request/read-back failed, 2 arguments/credentials/name guard refused |
| `create_project.py` | Create a project over HTTP; `--memory project-only` from the start; `--dry-run` prints the body and sends nothing. | 0 created and read back, 1 create or read-back failed, 2 refused |
| `delete_project.py` | Delete a project and every chat in it over HTTP, after refusing a name mismatch or a non-`rp-test` name without `--force`. Dry run unless `--apply`. | 0 dry run or deleted and verified, 1 read or verify failed, 2 refused |
| `project_settings.py` | Set a project's instructions and memory scope (`--memory project-only` keeps its chats out of your memory). Dry run unless `--apply`. | 0 dry run or verified, 1 apply failed, 2 refused |
| `send_prompt.py` | Send one or more prompts: new chats (`--project` targets one) or a continuing one with `--chat`; `--effort`, `--model`, `--search` and `--system-hint HINT` (any composer "+" item's id, for example `plugin:connector_openai_deep_research`) pin the send by rewriting the `f/conversation` POST body in flight; `--record-send-body PATH` records it and the response stream (`PATH.stream.txt`, where a Deep research `session_id` is read back into `--json`; the window then stays open until the reply stream has ended), `--attach FILE ...` uploads before the fill, `--title` renames once the reply arrived, `--json` records the send. Several PROMPT_FILEs share one browser window and are waited on after it closes; `--no-wait` skips the wait. | 0 every prompt sent and replied, 1 a send, resolve, wait or rename failed, 2 bad arguments |
| `deep_research.py` | Start a Deep research chat with the page's own hint (`start`, no MCP call), poll its widget state over plain HTTP (`status`, `--wait`), and collect native Markdown plus sources (`fetch`). `export` returns DOCX/PDF through MCP; completed widget runs need `--force` after `status --wait` confirms DONE. | `start` 0 written, 1 send failed, 2 bad argument; `status` and `fetch` 0 done or written, 3 running or not finished, 1 failed read; `export` 0 written, 3 legacy completion check not met, 1 failed, 2 bad argument |
| `read_chat.py` | One conversation: is the turn finished, and what did it say? | 0 turn finished |
| `pin_chat.py` | Pin or unpin a chat (`is_starred`). Dry run unless `--apply`. | 0 dry run or verified, 1 apply failed, 2 bad id |
| `clean_chats.py` | Archive, delete or unarchive worker chats; `--project g-p-<id>` selects from the project's own listing (every chat when `--match` is absent), so a chat ChatGPT renamed is still found. Dry run unless `--apply`. | 0 always, 2 refused |
| `round_status.py` | A research round: admitted, read, written off, unread; `--collect [--apply]` classifies every entry stuck at "sent" (resumable, superseded, collectable, lost) and archives a collectable reply. | 0 nothing unread; with `--collect` 0 nothing to do or all collected, 1 a fetch or write failed, 2 refused |
| `review_topic.py` | A finished topic, offline: six integrity checks, a seventh warning about the profile snapshot, review verdicts, gaps, and cost. | 0 nothing wrong |
| `measure_window.py` | What a send window costs in memory and fill time. | 0 always |
| `discover_endpoints.py` | Record what endpoints the page calls, and with `--bodies` what they sent. | 0 always |

`_common.py` holds only session bootstrap and table formatting.

Search indexing can lag behind a completed reply. During the 2026-09-25
checks, unique markers initially returned no hits and later matched the
same project chats. Use `read_chat.py` with the recorded id for recent work.

## Composer modes and adjacent tools

`send_prompt.py --system-hint HINT` is a generic composer-mode selector.
The live `GET /backend-api/system_hints?mode=basic` catalog on 2026-09-25
listed `picture_v2` (Create image), `search`, `tasks`, `tatertot` (Study),
`canvas`, and `sketch`. These modes already have a send path. The absence
of a dedicated script does not mean they cannot be invoked. Mode-specific
completion, artifact download, scheduling changes, and cleanup need their
own verification; the ordinary reply collector handles assistant text.

There is no dedicated CLI for moving an existing chat into a project,
renaming a project, editing account-wide memory/custom instructions,
temporary chats, message edits/regeneration/branches, voice, or sharing.
Task creation can be requested through the `tasks` hint, but this skill
has no dedicated automation create/pause/delete command. Its dedicated
automation interface is the read-only `list_automations.py`.

`chatgpt-refresh` is installed on this machine and refreshes a connector's
cached tool list. It belongs to binnacle's `chatgpt-mcp-dev` scripts, outside
this self-contained skill. The adjacent `chatgpt-project` command also reads
and updates project instructions. Neither command adds project rename/move
or automation lifecycle flags. This skill owns Platform tunnel CRUD and
ChatGPT app/link operations. `chatgpt-mcp-onboarding` owns server setup,
local tunnel-client profiles/daemons, and the order of the complete setup.

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

**Before a run starts, check everything at once.** `preflight.py [--workdir
DIR] [--project g-p-<id>] [--browser]` runs the host, link, account and run
checks over one session and prints `GO`, `GO WITH WARNINGS (n)` or
`DO NOT START (n blocking)`. It reads the wireless link before opening any
session, so a dead link is never reported as a blocked account. `--browser`
also confirms the composer appears, which costs a browser slot and about
twenty seconds; `--json PATH` records every check.

**Before a run starts.** `profile_context.py --project g-p-<id> --json
<workdir>/chatgpt/profile_context.json` records what the workers will
inherit: custom instructions, memory, the model and effort preset, the
project's instructions. Without it the archive cannot say which profile
produced a round. The summary prints lengths and counts only; the text goes
to the JSON. `review_topic.py` warns, without failing, when a topic has no
such file or the newest one predates the newest round.

**A run was interrupted.** Run `round_status.py <workdir>`. An entry stuck
at "sent" in the round the run is on needs nothing: the orchestrator's own
`--resume` collects it. For older rounds it never will, so
`round_status.py <workdir> --collect` classifies each one: *superseded*
means a retry already succeeded, *collectable* means the reply is still
there, *lost* means the conversation is gone. `--collect --apply` archives
a collectable reply beside the orchestrator's own transcripts and marks the
entry; it refuses while a run is in flight, because the orchestrator
rewrites that ledger wholesale and would drop the change.

**ChatGPT changed something.** `discover_endpoints.py`, then
`references/endpoint-discovery.md` for how to read the output.

## Connectors: apps, links and tunnels

Measured 2026-09-25 while onboarding a throwaway custom MCP connector (the
full procedure, tunnel included, is the `chatgpt-mcp-onboarding` skill, which
calls these commands for every ChatGPT-side step). The captured request
shapes are in `references/connectors.md`.
Since the plugins era a connector made through "Create MCP App" is an *app*
(`asdk_app_<32hex>`, a private plugin release) that is not usable until it
is *connected*, which creates the user's *link* (`link_<32hex>`) and
discovers the tool names through the tunnel. `chatgpt-refresh --list` and
`links/list_accessible` show links only.

```bash
python3 $S/list_connectors.py [--match TEXT] [--tunnels] [--detail ID ...] [--json]
python3 $S/create_connector.py --name NAME --tunnel tunnel_<id>        # POST aip/connectors/mcp -> asdk_app_<id>; 409 = name taken
python3 $S/connect_connector.py asdk_app_<id> --name NAME --apps-privacy full_access   # POST links/noauth -> link_<id> + tools
chatgpt-refresh NAME                                                    # re-read the tools after the server changed
python3 $S/delete_connector.py asdk_app_<id> --confirm                  # DELETE its links (aip/connectors/links/<id>), then the app (aip/connectors/<id>)
```

The server behind the tunnel must be up when creating and connecting:
ChatGPT probes it and the link's `actions` are what it found. To pin a send
to one connector, pass `--system-hint plugin:plugin_<asdk_app id>` to
`send_prompt.py`; the server then sees the call from client
`openai-mcp(Codex)` (the pre-plugin connector reports `openai-mcp`). Only
the tunnel + No Auth shape was captured; "Server URL" and OAuth
connectors are not covered by these commands. Deleting the app alone (what
the UI's Uninstall does) leaves the user's link behind as an ACTIVE link
with no connector; `delete_connector.py` removes the links first.

### Manage the Platform tunnel from this skill

The Platform tunnel is a separate resource from the ChatGPT app and link.
`manage_tunnels.py` manages it without visiting the Platform page. Read
[references/tunnels.md](references/tunnels.md) for credential selection,
first-use checks, error recovery and the current verification status.

```bash
python3 $S/manage_tunnels.py list --organization <org-id>
python3 $S/manage_tunnels.py create "Local MCP" --organization <org-id> --workspace <workspace-id>
python3 $S/manage_tunnels.py get tunnel_<id>
python3 $S/manage_tunnels.py update tunnel_<id> --name "Local MCP renamed" --apply
python3 $S/manage_tunnels.py delete tunnel_<id> --expect-name "Local MCP renamed" --confirm
```

Creation returns a verified id and metadata; `--id-only` prints just the id.
Allow 25–30 seconds before using a new tunnel. The command does not start
the local forwarding daemon. Use a dedicated tunnel for each independently
served MCP endpoint. To retire an endpoint, remove its connector links/app,
stop its forwarding daemon, then delete that tunnel. A tunnel delete does
not perform those other actions or infer which existing resources to remove.

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
python3 $S/create_project.py "msgloom research workers" --memory project-only   # --dry-run to rehearse
```

`create_project.py` posts `POST /backend-api/projects` directly (body
captured 2026-09-20) and reads `gizmos/<new-id>` back to confirm the
instructions and memory scope took: no browser, no browser slot, no Xvfb.
Until 2026-09-20 it drove the sidebar's `button[aria-label="New project"]`
in a Playwright window, which is how the endpoint was found.
`delete_project.py g-p-<id> --expect-name NAME` deletes one the same way
(`DELETE /backend-api/gizmos/<id>`), refusing a name mismatch or a
non-`rp-test` name without `--force`; `--apply` sends it and requires a 404
back. Deleting a project deletes every chat in it and cannot be undone.

There is no dedicated command to move an existing chat into a project.
Create the worker chat with `--project` to place it there from the start.

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

Every send carries a `thinking_effort` and a `model` in its
`POST /backend-api/f/conversation` body. Where they come from, measured on
2026-09-20 with recorded sends:

- **A send inherits the server's record**: `GET /backend-api/settings/user`
  → `settings.last_used_model_config`, `slugs` for the last model per surface
  and `juices` for the last effort per model per surface; the send path
  uses the `web` surface. `model_settings.py` and `profile_context.py` print
  it resolved to a preset. Change it in the web UI and every send without an
  explicit choice silently follows.
- **The `oai-last-model-config` cookie is not what a send carries.**
  Rewriting it pinned neither the body nor the composer's label. The
  mechanism the orchestrator relied on from 2026-09-16 (`with_effort` in the
  repository's client, `TASK_EFFORT`) therefore never worked: every run used
  the profile's setting, `max` at the time. The neighbouring
  `oai-tpp-model-settings` cookie belongs to another surface.
- **What pins it now**: the client rewrites the body in flight, inside the
  skill's own window, before it leaves the browser (`rewrite_send_body`, a
  route that `BrowserSender._open` registers only when effort, model or
  search is set): `thinking_effort`, `model` and `system_hints` (`"search"`
  for Web search). No account setting is touched, so the user's own default
  stays as it is. `send_prompt.py --effort standard --search` produced a
  reply whose metadata says `thinking_effort: standard`,
  `search_result_groups` filled and a cited answer, where two sends before
  it had posted `max` and no search.
- **A transcript records what was used**: each assistant message's
  `metadata` carries `thinking_effort`, `model_slug`, `resolved_model_slug`,
  `search_result_groups` and `citations`; `read_chat.py --effort` prints
  them per turn. `send_prompt.py --record-send-body PATH` keeps `original`
  (what the page built) and `sent` (what left the browser).

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
the *model*; `--model gpt-6-pro` selects it the same way as an effort, and
stays opt-in per run (`ROADMAP.md`, rule 4).

**"Ultra" is not an effort level.** Settings → General has "Enable Ultra
effort", stored as `model_picker_persists_ultra_effort` in
`/backend-api/settings/user`. The models payload contains no `ultra` value
and no Ultra preset, so do not add it to `EFFORTS`. What the picker sends
with Ultra engaged is unknown until a send is captured with
`discover_endpoints.py --bodies`.

**Which level each research step should use** is decided in the
research-pipeline repository's `docs/chatgpt-research-effort-levels.md`:
`max` for the four steps whose mistakes cannot be recovered later (admission,
per-paper analysis, synthesis, review) and `extended` for the four that
restate or bound settled work. Raising every step is not free, because longer
turns mean more polling and polling volume is what earns this account its
rate limits.

## Deep research is an ordinary chat that runs longer

A Deep research run is not a separate connector session: it is an ordinary
chat that runs longer. `start` sends the prompt the way the page does, one
message carrying the system hint
`plugin:connector_openai_deep_research`. That send is the browser's only
part (19 seconds in the 2026-09-20 sample); nothing stays open. ChatGPT attaches a widget to
that same conversation and stores the widget's whole state server-side on
one of the conversation's own messages, a tool message whose
`metadata.chatgpt_sdk.widget_state` is a JSON string. `status` reads it
back later over plain HTTP (`GET /backend-api/conversations/<id>`, plural,
no `/messages`): the plan and its steps, `research_started_at` and
`research_stopped_at`, and `status`, which reads `completed` when the
report is ready. The sample was readable 194 seconds after the send.

The widget may appear after `start` returns. Use `status --wait` immediately
after starting. A one-time status check returns 1 if the widget is absent;
that does not prove that the send used the wrong start method.

The report is `widget_state.report_message.content.parts[0]`, **native
Markdown**, and `fetch` writes it out byte for byte. Nothing is converted,
because nothing needs to be: it is what ChatGPT wrote. `--sources` writes
the search result groups beside it.

`export` returns DOCX or PDF through the connector's MCP tool. It also
supports the older MCP-only start path, which has no widget report.
Its default completion check still looks for a legacy "Generated report"
title. A completed widget run may lack that title. Confirm DONE with
`status --wait`, then use `export --force` to skip the legacy check. Both
formats were verified on 2026-09-25. See `references/endpoint-discovery.md`.

```bash
python3 $S/deep_research.py start prompt.md --project g-p-<id> --run run.json
python3 $S/deep_research.py status --run run.json --wait --timeout 1800
python3 $S/deep_research.py fetch --run run.json --out report.md --sources sources.txt
python3 $S/deep_research.py export --run run.json --out report.docx --force
python3 $S/deep_research.py export --run run.json --out report.pdf --type pdf --force
```

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

- [Setup and recovery](references/setup.md) — first use, required access,
  missing dependencies, login failures and read-only verification.
- [Platform tunnels](references/tunnels.md) — admin-key environment/file
  setup, management commands and recovery from tunnel errors.
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

### Session token renewal

`Session.__init__` (`chatgpt_session.py`) renews the persistent session
token on every session it builds. Measured 2026-09-21: `GET
/api/auth/session` answers 200 and re-issues
`__Secure-next-auth.session-token.*` with `Max-Age=7776000` (90 days)
every single time, so the client compares whatever it just sent (Chrome's
own jar, or an earlier renewal) against what the server just re-issued and
stores the later one in this machine's own keyring -- service
`org.freedesktop.secrets`, attributes `{"application":
"chatgpt-web-operations", "purpose": "chatgpt-session-token"}`, findable
and deletable by those two attributes alone.

Because every session build renews it, **no new cron job was added for
this**: the existing daily health check (`health.py`) already builds one
session a day and can renew the token while the server accepts the login.
A future expiry does not guarantee continued authentication. Chrome's own
cookie database is only ever read, never written, exactly as everywhere
else in this skill. Set `CHATGPT_SESSION_STORE=0` to disable the keyring
side entirely and fall back to Chrome's own jar, as before this existed.

Expiry is not proof that a login remains valid. On 2026-09-25 the keyring
path repeatedly met an authentication challenge while the existing Chrome
cookie authenticated successfully. Session creation now tries that browser
cookie once after a failed keyring authentication (incomplete response or
HTTP 401/403). It stores renewed cookies only after successful authentication.
This changes neither Chrome's cookie database nor the account's settings.
The diagnostic event is `auth_browser_fallback`; no credential is logged.
Network failures, 429 and 5xx retain the existing retry behavior. If both
login copies fail, the command still fails and reports the request path.
