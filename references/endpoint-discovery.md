# Finding ChatGPT's endpoints again when they change

The transport map in `SKILL.md` is a snapshot. ChatGPT changes its backend
without notice, and when it does, the map is rebuilt with the method below
rather than guessed at. Everything here is read-only: it observes a real
logged-in page, and never posts a message.

## The principle

**Watch what the page does, then reproduce it over HTTP.** The page is the
reference implementation, and it is always current. Do not reason from what
the API "should" look like, and do not copy an endpoint list from the
internet: both were wrong about this account within a day.

## 1. Record the traffic

```bash
S=~/.claude/skills/chatgpt-web-operations/scripts   # this skill's scripts directory

python3 $S/discover_endpoints.py --seconds 30
```

It opens chatgpt.com with the profile's own cookies, records every
`backend-api` request, and prints distinct endpoints with call counts. Ids
and hashes are collapsed to `<id>` so one conversation does not fill the
report.

To see what a *specific* action costs, keep the window open longer and use
the machine normally in another window; the run records whatever the page
does. To watch a conversation instead of the home page, pass
`--url https://chatgpt.com/c/<id>`.

Widen the net when an endpoint is missing from the results:

```bash
python3 $S/discover_endpoints.py --match "" --seconds 30     # everything
python3 $S/discover_endpoints.py --match sentinel            # just the gates
```

## 2. Separate reads from the send

Try each candidate endpoint with plain HTTP and the session's bearer token.
If it answers, it is a read and belongs in the HTTP client. If it refuses
with a sentinel or challenge error, it is gated.

```python
# run under the skill's .venv/bin/python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/chatgpt-web-operations/scripts"))
import chatgpt_client as cc
session = cc.ChatGPTSession("chrome")
print(session.session.call("/backend-api/<candidate>"))
```

`call` returns `(status, parsed)`. A 200 means the read path is enough.

**Send an empty body first.** `POST /backend-api/sentinel/chat-requirements`
answers 200 with no body and returns 500 for `{"p": ...}`. That 500 reads
exactly like an account block and started a wrong diagnosis that cost hours.
When an endpoint 500s, retry it with no body before concluding anything.

## 3. Find what a gate actually measures

```bash
python3 $S/probe_send_gates.py                                  # which gates are on
python3 $S/discover_endpoints.py --globals "turnstile|sentinel|__oai" --seconds 20
```

The globals dump is how the `so` collector was identified: about forty
`__oai_so_*` window keys for keypress, pointer, scroll, input and timing.
Names like `_kp`, `_hpm`, `_sx0`, `_uin`, `_t0` say what is being measured,
and they said this one is **behavioural**, not a signature.

That distinction decides the only question that matters here. A signature can
be computed. A behavioural attestation cannot be reproduced without
fabricating human-interaction telemetry, which is defeating an anti-automation
control rather than speaking a protocol. If a future probe shows the gates
have become computable, say so with the evidence; until then the send needs a
real browser and no amount of Node changes that.

## 4. Confirm the whole flow

Watch a real send from another window while recording, and the order appears:

```
POST /backend-api/sentinel/chat-requirements/prepare
POST /backend-api/sentinel/chat-requirements/finalize
POST /backend-api/f/conversation/prepare
POST /backend-api/f/conversation          <- the message itself
```

## What the map looked like on 2026-09-16

Read over plain HTTP with the bearer token:

| Endpoint | Use |
|----------|-----|
| `GET /backend-api/conversations?offset&limit&order` | list |
| `GET /backend-api/conversation/<id>` | read, and poll a turn |
| `PATCH /backend-api/conversation/<id>` | rename, archive, delete (`is_visible: false`) |
| `GET /backend-api/me` | account, and a cheap liveness check |
| `GET /backend-api/files/<id>/download` | signed URL for an upload |
| `POST /backend-api/conversation/init` | model limits and defaults |
| `GET /backend-api/gizmos/snorlax/sidebar` | projects, with instructions and files |
| `GET /backend-api/gizmos/<g-p-id>/conversations` | the chats inside one project |
| `POST /backend-api/projects` | **create** a project; returns the new `g-p-…` record |

Browser only: `POST /backend-api/f/conversation`, gated by `proofofwork`,
`turnstile` and `so`, all `required: true`.

## Additions seen on 2026-09-19

Recorded from the live page through Claude in Chrome's network log, then
reproduced with the bearer token. All reads.

| Endpoint | Use |
|----------|-----|
| `GET /backend-api/conversations?…&is_archived=false&is_starred=false&hide_snorlax=true` | the main list hides project chats with `hide_snorlax`; archived and starred are filters |
| `GET /backend-api/pins` | pinned chats and projects |
| `GET /backend-api/gizmos/<g-p-id>` | one project: instructions, files |
| `GET /backend-api/tasks` | scheduled tasks |
| `GET /backend-api/memories?include_memory_entries=false` | memory |
| `GET /backend-api/user_system_messages` | custom instructions |
| `GET /backend-api/models?…&supports_model_picker_upgrade_presets=true` | models, `thinking_efforts`, and `versions[].intelligence_presets`, which are the composer's Power slider |
| `GET /backend-api/settings/user` | settings, including `model_picker_persists_ultra_effort` |
| `GET /backend-api/wham/usage` | plan rate limit window, credits balance, `rate_limit_reached_type` |
| `GET /backend-api/wham/rate-limit-reset-credits` | the "Full reset" credits |
| `GET /backend-api/accounts/<account-id>/remaining_balance` | credits |
| `GET /backend-api/files/library/storage/usage` | storage totals by file type and by source |
| `POST /backend-api/files/library` | the library listing: body `{}` or `{"limit": 100, "cursor": "…"}`; a page is `{"items": […], "cursor": "…"}` and the cursor is null on the last page; no body at all is a 422 |
| `GET /backend-api/files/library/directories/path` | library folders (one root) |
| `GET /backend-api/ps/plugins/installed?limit=1000`, `GET /backend-api/hazelnuts?include_permissions=true&scope=installed` | installed plugins and skills |

## Seen on 2026-09-20

Reproduced with the bearer token while building `profile_context.py`; the
Personalization tab was watched through Claude in Chrome. All reads.

| Endpoint | What it carries |
|----------|-----------------|
| `GET /backend-api/user_system_messages` | custom instructions: `enabled`, `name_user_message`, `role_user_message`, `traits_model_message`, `other_user_message`, `personality_type_selection`, `traits_enabled`, `disabled_tools`; `about_user_message` and `about_model_message` repeat the last two texts under their old names |
| `GET /backend-api/memories?include_memory_entries=false` | `memory_num_tokens`, `memory_max_tokens`, empty `memories`; this is the call the Personalization tab makes |
| `GET /backend-api/memories?include_memory_entries=true` | the same plus every entry: `id`, `content`, `updated_at`, `gizmo_id`, `status`, `conversation_id`, `created_timestamp`, `labels`; `profile_context.py` counts them and stores nothing else |
| `GET /backend-api/settings/user` | `settings.last_used_model_config` (`slugs` per surface, `juices` = effort per model per surface), `default_model_config`, `model_sticky_for_new_chats`, `model_picker_persists_ultra_effort`, `wingman_thinking_effort`; the memory switch is not among the named keys |
| `GET /backend-api/gizmos/<g-p-id>` | `gizmo.instructions`, `memory_enabled`, `memory_scope`, `context_stuffing_budget`, `model` / `default_model`, `files`, `product_features.attachments` (retrieval-backed, 82 mime types) |

The Personalization tab's own calls on open: `personality_types`,
`personality_trait_types`, `personality_settings_impression` (POST),
`memories?include_memory_entries=false`, `memories/about_you/summary/stream`
(POST) and `.../checksum`, `pageConfigs/usage_limits`, `ps/plugins/installed`,
`composer/items`, `apps/content`, `aip/connectors/links/list_accessible`,
`pets`, `accounts/mfa_info`, `trusted_contact/enabled`.

Two things about the method:

- After a reload, `settings/user`, `me`, `models` and `conversations` did
  **not** appear in the tab's request log: they arrive with the document, so
  a network log alone under-reports what the page knows. Probe those with
  the bearer token as well.
- The extension's JavaScript tool refuses a result that looks like cookie or
  query-string data; a search that returns fetched script text is blocked.
  Read DOM state (`aria-checked`, `aria-labelledby`) instead, which worked.

Later the same day, while planning the next stages. All reads; nothing was
changed.

| Endpoint | What it carries |
|----------|-----------------|
| `GET /backend-api/gizmos/snorlax/sidebar` | **paged**: without parameters it returns 5 items and a `cursor`; the page asks for `?owned_only=true&conversations_per_gizmo=5&limit=20`; `?owned_only=true&limit=50` returned all 32 projects, pinned ones first. `list_projects.py` pages with cursor since 2026-09-20. With `conversations_per_gizmo` the items are shaped differently, not inspected |
| `GET /backend-api/pins` | a list of `{"item_type": "feature" / "conversation" / "project", "item": {…}, "pinned_at": …}` |
| `GET /backend-api/system_hints?mode=basic` | the composer's "+" items: `search` (Search, category `source`, persists between messages, allowed in temporary chats), `picture_v2`, `tasks`, `tatertot` (Study), `canvas`, `sketch` |
| `GET /backend-api/system_hints?mode=plugins&suggestions=true` | `plugin:connector_openai_deep_research` (Deep research: persists between messages, `requires_personalization`, not allowed in temporary chats) and one entry per installed connector. Whether a send carries `system_hints` in its body is not captured yet (Stage 3, B2 and B3) |
| `GET /backend-api/wham/usage` | `plan_type`, `rate_limit.primary_window` (`used_percent`, `limit_window_seconds` = 604800, `reset_at`), `credits` (`balance`, `has_credits`, `approx_local_messages`, `approx_cloud_messages`), `rate_limit_reset_credits.available_count`, `model_usage`; `wham/rate-limit-reset-credits` lists the credits with `reset_type: codex_rate_limits`. None of it is about chat sends |
| `GET /backend-api/conversations?…` items | `memory_scope` was `global_enabled` on all 100 most recent conversations, two of them inside projects; `is_do_not_remember` was `false` or absent. No other value has been observed yet |

The project page (`/g/<short_url>/project`) calls `gizmos/<id>`,
`gizmos/<id>/conversations?cursor=0`, `conversation/init` (POST) and the
sentinel `prepare` / `finalize` pair on load, plus one `gizmos/<id>` per
project shown in the sidebar.

**Project settings**, read from the dialog on 2026-09-20 without changing
anything: Project name, Instructions (a textarea), Memory as a two-way
choice, "Default memory" ("This project can access memory from outside
chats, and vice versa") or "Project-only memory" ("This project can only
access its own memory. Its memory is hidden from outside chats. Work mode
isn't available for this type of project"), and Delete project. Nothing is
saved until the dialog's Save button, which appears once a field changed;
Close discards. The PATCH is captured below.

A `?query` appended to a project URL gives an error page in Chrome; navigate
to the canonical URL.

Not captured yet, so not implemented: pinning, starring, moving a chat
into a project, editing project instructions, creating or pausing a
scheduled task. Each needs one real action recorded
with `--bodies`.

## Capturing a mutation

A read can be probed until it answers. A mutation cannot: guessing a POST
body against a live account creates junk, and a wrong guess is indistinguishable
from a changed API. Record what the page sends instead.

```bash
python3 $S/discover_endpoints.py --bodies --visible --seconds 60
```

Perform the action yourself in the window that opens. Every POST, PATCH and
PUT it makes is printed with its body, which is exactly what to reproduce.
This is how creating a project and moving a chat into one should be added;
they are deliberately absent until someone captures them.

**Naming is not a guide, and it is not even consistent.** Projects are read
through `gizmos/snorlax` and created through `projects`. Neither name predicts
the other, so a reasoned guess at the creation endpoint would have been wrong
whichever of the two you started from. Search the recorded traffic for the id
you can see in the UI's URL, not for the word you expect.

**Captured 2026-09-16** by driving the flow:

```
POST /backend-api/projects  -> 200
{"resource": {"gizmo": {"id": "g-p-…", "short_url": "g-p-…-msgloom-research-workers", …}}}
```

Three things cost an hour before that worked, all of them UI facts no amount
of reasoning would have produced:

- The sidebar's **New project** control is `button[aria-label="New project"]`
  with `can-hover:opacity-0`. A plain click is intercepted and the app never
  sees it; `force=True` opens the dialog. A silently ignored click looks
  exactly like a removed feature.
- The dialog carries **no `role="dialog"`**. Looking for one finds nothing and
  reads as "the flow changed". Its name field is the only visible text input,
  the composer being a `textarea` and the upload controls `type=file`.
- **Take a screenshot and look at it.** Four runs were spent inferring the
  page state from selector failures. One screenshot showed the dialog open
  with its field and button, and the fix was immediate.

**Captured 2026-09-20** on the sandbox project `rp-test-sandbox`, from the
user's own Chrome through Claude in Chrome: a `window.fetch` hook stored
every non-GET `backend-api` call, the Project settings dialog was driven by
hand, and each body was then reproduced over HTTP with the bearer token and
verified with `GET gizmos/<id>`:

```
PATCH /backend-api/projects/<g-p-id>          content-type: application/json
{"name": "<name>", "instructions": "<text>", "emoji": null, "theme": null}
{"name": ..., "instructions": ..., "emoji": null, "theme": null, "memory_scope": "project_v2"}
{"name": ..., "instructions": ..., "emoji": null, "theme": null, "memory_scope": "global"}
-> 200 {"resource": {"gizmo": {...}}, "error": null, "sharing_targets": [...]}
```

- One endpoint carries name, instructions and memory; the page always sends
  the whole set, so a command should read the gizmo first and resend it with
  the changed field. Name and instructions live under `gizmo.instructions`
  and `gizmo.display.name`; `emoji` and `theme` under `gizmo.display`.
- "Project-only memory" is `memory_scope: "project_v2"`; "Default memory"
  is `memory_scope: "global"`. `memory_enabled` follows: `false` for
  `project_v2`, `true` for `global`. Read them back from `gizmos/<id>` (the
  sidebar item shows the same values).
- Two capture facts worth an hour: the page passes its body inside a
  `Request` object, not in `fetch`'s `init`, so a hook must read
  `input.clone().text()`; and the extension's network log shows method, URL
  and status but never a body, so the hook is the only way to see one from
  the user's own Chrome (`discover_endpoints.py --bodies` remains the
  Playwright way).

**Captured 2026-09-20, later** on a chat inside the sandbox project, the
same way (the conversation header's More menu, and Data controls → Archived
chats → Manage for the unarchive), each verified with `GET conversation/<id>`:

```
PATCH /backend-api/conversation/<id>   {"is_starred": true}     Pin chat
PATCH /backend-api/conversation/<id>   {"is_starred": false}    Unpin chat
PATCH /backend-api/conversation/<id>   {"is_archived": true}    Archive
PATCH /backend-api/conversation/<id>   {"is_archived": false}   Unarchive
```

So pin and unarchive are the conversation PATCH family the client already
uses for rename, archive and delete. Two more facts from the same session:
a chat created inside a project set to "Project-only memory" reports
`memory_scope: project_v2` itself (the sandbox chat did; chats elsewhere
read `global_enabled`), and ChatGPT titles a new chat by itself once the
first reply lands, which overwrote a rename done before the wait
(`send_prompt.py` now renames after the reply).

## Re-run this when

- a send starts failing in a way `probe_account.py` says is not the account
- a read returns 404 or 400 where it used to work
- someone proposes moving the send to HTTP, including a future me
- before trusting any endpoint list in this repo that is more than a few
  months old
