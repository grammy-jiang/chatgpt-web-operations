# Finding ChatGPT's endpoints again when they change

The transport map in `SKILL.md` is a snapshot. ChatGPT changes its backend
without notice, and when it does, the map is rebuilt with the method below
rather than guessed at. The discovery command observes a logged-in page
and does not post a message. Later sections also record explicitly performed
mutations and sends; this file is a dated investigation log.

Current behavior is defined by `SKILL.md`, `ROADMAP.md` and
`VERIFICATION.md`. Earlier hypotheses below can be superseded by later
captures. In particular, current Deep research start uses a browser send
and widget-based HTTP collection; the older MCP-only experiments describe
the legacy export path, not the default workflow.

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
| `GET /backend-api/tasks` | **not** scheduled tasks (corrected 2026-09-21): the account's background-task history -- 51 records measured, 49 of the old Deep research mechanism (`task_id` `deepresch_…`, 2025-03 to 2026-02) and 2 image generations (`imagegen_…`); `{"tasks": [50 per page], "cursor": "<opaque>"}`, `?cursor=` pages, `?limit=` is ignored, `final_message` always null, `prompt` mostly set. No command reads it. Real scheduled tasks are `/backend-api/automations`, see "Seen on 2026-09-21" below and `list_automations.py` |
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
| `GET /backend-api/ps/plugins/installed?limit=1000`, `GET /backend-api/hazelnuts?include_permissions=true&scope=installed` | installed apps/connectors and the user's own uploaded skills, respectively; item shapes enriched 2026-09-21, see "Seen on 2026-09-21" below and `list_skills.py` |

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
| `GET /backend-api/system_hints?mode=plugins&suggestions=true` | `plugin:connector_openai_deep_research` (Deep research: persists between messages, `requires_personalization`, not allowed in temporary chats) and one entry per installed connector. Outgoing `system_hints` are captured and rewritten by `send_prompt.py`; see the later 2026-09-20 captures and current `VERIFICATION.md` |
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

This 2026-09-19 inventory was superseded by the captures below. Pin/unpin,
archive/unarchive, project instructions and project memory scope now have
commands. Move-to-project and dedicated automation create/pause commands
remain absent. The generic `tasks` system hint is a separate send route;
see `SKILL.md` and `VERIFICATION.md` for current capability boundaries.

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

**The send body, recorded 2026-09-20** with `send_prompt.py
--record-send-body` from the skill's own window (`POST
/backend-api/f/conversation`, `application/json`): top-level `action`,
`client_contextual_info`, `client_prepare_state`, `conversation_mode`
(`{"kind": "gizmo_interaction", "gizmo_id": …}` inside a project),
`enable_message_followups`, `force_parallel_switch`, `local_function_names`,
`messages` (`messages[0].metadata.selected_sources: []`, `submission_mode:
"manual_send"`), `model`, `model_response_contracts`,
`paragen_cot_summary_display_override`, `parent_message_id`,
`supported_encodings`, `supports_buffering`, `system_hints` (`[]`),
`thinking_effort`, `timezone`, `timezone_offset_min`. The value of
`thinking_effort` came from the account's `last_used_model_config`, not
from the cookie. The reply's assistant messages carry
`metadata.thinking_effort`, `model_slug`, `resolved_model_slug`,
`search_result_groups`, `citations`, `reasoning_start_time` and
`reasoning_end_time`.

**Captured 2026-09-20, project create and delete**, from the user's own
Chrome with the fetch hook, on a throwaway project `rp-test-sandbox-2`
that was deleted in the same minute, each verified over HTTP:

```
POST   /backend-api/projects            {"instructions": "", "name": "<name>", "memory_scope": "unset"}
                                        -> 200 {"resource": {"gizmo": {"id": "g-p-…", "short_url": …, …}}}
DELETE /backend-api/gizmos/<g-p-id>     (no body) -> the project is gone; GET gizmos/<id> answers 404
```

The Create dialog also offers the memory choice, so `memory_scope` may be
`"project_v2"` at creation instead of `"unset"` (the PATCH values are
`project_v2` and `global`). Deleting a project deletes every chat in it;
the dialog says so and it cannot be undone. With the body known,
`create_project.py` no longer needs a browser.

**Attachments, measured 2026-09-20** with `send_prompt.py --attach` on a
73-byte Markdown file in the sandbox: the composer uploads through
`input#upload-files` (`POST /backend-api/files`, a storage PUT, then
`POST /backend-api/files/process_upload_stream`, about 10 s end to end for
that file), and the send body then carries the file in
`messages[0].metadata.attachments`:

```
{"id": "file_…", "size": 73, "name": "rp-test-attach.md", "mime_type": "text/plain",
 "source": "local", "library_persistence_result": "temporary", "is_big_paste": false}
```

The reply quoted the file's third line exactly, so the content reached the
model. A real paper the same day (arXiv 1706.03762, 2.2 MB, 15 pages):
`size 2215244`, `mime_type application/pdf`, the same
`library_persistence_result: "temporary"`; the reply gave the exact title,
the abstract's first sentence and the page count with file citations, in
3 min 28 s end to end including the reply wait. Timing that matters: the
composer's busy indicator appears only
~2.5 s after the input is set; a send clicked before the upload finished
was silently ignored ("message was not posted"), so `_upload_files` waits
for busy to appear and clear.

**Deep research through `system_hints`, measured 2026-09-20**: a send
with `system_hints: ["plugin:connector_openai_deep_research"]` made the
model call the connector as a tool (assistant `code` message with
`recipient: api_tool.call_tool`, a `tool` message back, `thoughts`,
`reasoning_recap`, then a text acknowledgement, all from
`gpt-5-6-instant` within seconds). The conversation's `async_status` was
7 right after and `null` a minute later; no further message appeared in
30 minutes.

**The page's own Deep research send, captured 2026-09-20 in the skill's
window** ("+" → "Deep research", selected *after* the prompt was filled:
`Locator.fill()` clears an already selected item, which is why the earlier
"+" clicks never reached the wire). The body carries the hint twice, top
level and in the message: `system_hints: ["plugin:connector_openai_deep_research"]`,
`messages[0].metadata.system_hints` the same, plus
`deep_research_version: "standard"`, `venus_model_variant: "standard"`,
`caterpillar_selected_sources: []`, `selected_mcp_sources: []`, and
`serialization_metadata.custom_symbol_offsets` marking an
`ecosystemMention` for the text `@Deep research ` that the composer appends
to the message. The reply is the same acknowledgement as with the injected
hint. **What happens next is driven by the page**: every ~60 s it posts
`/backend-api/ecosystem/call_mcp` with `{"app_uri":
"connectors://connector_openai_deep_research", "method": "tools/call",
"params": {"name": "get_state", "arguments": {"session_id": "…"}},
"conversation_id": …, "message_id": …}`, and from about two minutes it
also posts `f/conversation/prepare` for the conversation; the sidebar
listing is refreshed every ~30 s. In seven minutes with the window open
no report had landed. So a Deep research step needs the window kept open
until the connector reports done, and how the report is written back (a
follow-up turn through the gated send, most likely, given the `prepare`
calls) is the one thing still unobserved.

**The run with the window kept open for 23 minutes (the third Deep research
send of the day)** did not settle it: the page polled `call_mcp get_state`
seven times in the first 2.5 minutes and then stopped (a push channel is
the likely successor: the page fetches `/backend-api/celsius/ws/user` at
load), refreshed the sidebar listing every ~30 s, and no follow-up
`f/conversation` POST and no new turn appeared. **`get_state` works over
plain HTTP with the bearer token** (`POST /backend-api/ecosystem/call_mcp`
with the captured body: `app_uri`, `method: "tools/call"`, `params.name:
"get_state"`, `params.arguments.session_id`, `conversation_id`,
`message_id`); it answers 200 with an MCP tool result (`content: []`,
`structuredContent: null`, `isError: false`, `result: ""`) whose `_meta`
carries `deep_research_widget_messages` (thoughts with summaries,
`web.run` tool messages with `search_model_queries`, assistant text
messages whose `parts` stayed empty, `reasoning_recap` such as "Worked
for 49s") and `source_searches` (79 entries). Nothing in it changed
between minute 25 and minute 55 after the send, and the conversation kept
its six turns. So the progress is readable headlessly, but where and when
the finished report lands was not observed in three runs; the session id
comes from the page's own `call_mcp` calls, which a headless run does not
see unless it records them from the window that sent.

**Runs 4 to 6, later the same day, all over HTTP after the send:**

- The connector is an MCP app; `tools/list` on a live conversation (body
  `{"app_uri": "connectors://connector_openai_deep_research", "method":
  "tools/list", "params": {}, "conversation_id": …, "message_id": <the
  assistant's tool-call message>}`) lists `start(user_query)`,
  `steer(user_query)`, `get_state(session_id, shared_conversation_id?)`,
  `subscribe(session_id)`, `pause`, `skip_sleep`, `stop`, `export(session_id,
  export_type: pdf | docx)` and `get_inline_images`; every `session_id` is
  described as the "Deep research session (backing conversation) ID". On a
  deleted conversation the same call answers `mcp_not_allowed_in_shared_conversation`.
- The model's tool call is `{"path": "/Deep Research App/start", "args":
  {"user_query": …}}`; the tool reply in the conversation JSON is `{}`, and
  its metadata `connector_tool_payload` is `"{}"` too. The session id
  appears only in the send's SSE stream, inside the tool reply's JSON text
  (`{"session_id": "<backing id>", "connector_settings": {…}}`), which is
  why `--record-send-body` records the stream (`PATH.stream.txt`; the
  framing is `event: delta_encoding` / `data: "v1"`, a
  `resume_conversation_token`, then `event: delta` frames whose `data` is
  a full message object or a JSON-patch style `{"p", "o", "v"}` operation,
  then `message_stream_complete`, `title_generation`,
  `conversation_detail_metadata`, `[DONE]`). Recording it means keeping the
  window open until the stream ends (`expect_response` + `finished()`).
- With the session id: `get_state` showed "Generated report on …" as the
  last `reasoning_title` about 40 s after the send, but the assistant text
  messages in the state keep empty `parts`; `GET
  /backend-api/conversation/<session_id>` answers 404; the front
  conversation still had its six turns 48 minutes after the send. So where
  the finished report is served remains unobserved; `export` (pdf, docx)
  and `subscribe` are the two untried tools.
- Polling three endpoints every 30 s for ten minutes earned the
  conversation-read 429 ("Too many requests"); a collector must poll once a
  minute at most and back off on 429.
- **The report is served by `export`.** `tools/call export {"session_id":
  …, "export_type": "docx"}` (also `pdf`) answers 200 with
  `_meta.content_disposition` (`attachment; filename="<title>.docx"; …`)
  and `_meta.encoded_data`, the file in base64 (the observed docx: 12 kB,
  2,890 characters of report with an executive summary, sections and
  numbered citations, decoded with `zipfile` + `word/document.xml`), and
  `structuredContent {"ok": true}`. `subscribe {"session_id": …}` answers
  with `_meta.websocket_url`, a `wss://ws.chatgpt.com/…/ws/user/<user id>`
  URL carrying a per-user token: the page's push channel, never to be
  stored. So after the send everything is plain HTTP: `get_state` for
  progress (done when a `reasoning_title` starts with "Generated report"),
  `export` for the report. The front conversation never receives it.
  `deep_research.py` implements `get_state` and `export`; `subscribe` is
  deliberately not implemented.
- **The whole thing is plain HTTP: the send is not needed.** `tools/call
  start {"user_query": "<prompt>"}` on a conversation the user owns answers
  200 with `structuredContent.session_id` and starts the research at once,
  without adding a single message to that conversation (a six-turn chat
  still had six turns afterwards; only its title was regenerated). So a
  Deep research run costs no browser window and passes no send gate.
- **`get_state` and `export` are keyed by `conversation_id`, not by the
  `session_id` argument.** Calling `get_state` with conversation A's id and
  session B's id returns A's research; an unowned random id returns
  something unrelated. Treat the carrier conversation as the key, one
  research at a time per conversation, and always pass one the user owns.
- Measured: a three-paragraph research took about four minutes from `start`
  to a "Generated report" title; its export was a 15 kB docx holding 8,141
  characters with an executive summary, sections and numbered citations.
- **The research's topic comes from the carrier conversation, not from
  `user_query`.** A carrier whose only turn was "reply with the single word
  OK" produced a report titled "Handling 'Reply with the Single Word OK'
  Test Prompts" while `user_query` asked about tide gauges; three runs
  agree. So the prompt must be *in* the conversation: post it as an
  ordinary message first, then `start` on that conversation.
- **One research per carrier, for the life of that conversation.** A second
  `start` on a conversation that already hosts one returns a session id
  whose state is never reachable; `get_state` keeps returning the first
  research. A fresh random uuid as `conversation_id` is refused
  (`isError`), so the carrier must be a real conversation the user owns.
- Consequence for the skill: the send stays (it mints the carrier and
  states the topic), but nothing afterwards needs a browser, and the
  session id is never needed -- `conversation_id` is the key.

**The Deep research report, found 2026-09-20 after a long detour.** A Deep
research is an ordinary chat that runs longer. Start it the way the page
does -- one send carrying `system_hints:
["plugin:connector_openai_deep_research"]` -- and ChatGPT attaches a widget
whose entire state is stored server-side on a message, so the result is
fetched later over plain HTTP like any other chat:

```
GET /backend-api/conversations/<conversation-id>      (plural, no /messages)
  -> messages[N] with author.role == "tool"
     metadata.chatgpt_sdk.widget_state                (a JSON string, ~105 kB)
        .status                       "completed" when the research is done
        .plan                         {plan_id, version, title, steps[{id, text, status}]}
        .research_started_at / .research_stopped_at
        .report_message               a whole assistant message
           .content.parts[0]          THE REPORT, native Markdown
           .metadata.search_result_groups / .citations / .safe_urls
```

The browser is needed only for the send (about 19 s with `--no-wait`);
nothing stays open, and the sample was readable 194 s later. The report
needs no conversion of any kind: it is the Markdown ChatGPT wrote.

What made this take a whole afternoon, recorded so the next reader is
spared it: a research started by calling the connector's MCP `start` tool
directly attaches **no** widget, stores **no** report, and is reachable
only through `export` as docx or pdf. Every "the report is nowhere"
measurement on this page came from that path. The two clues that pointed
the right way were in hand early and were walked past: the page bundle's
`applyRemoteWidgetState$`, which stores widget state per message, and
`chatgpt_sdk` sitting in a tool message's own metadata key list.

## Seen on 2026-09-21

| Endpoint | Use |
|----------|-----|
| `POST /backend-api/global/search` | search conversations by content, not just title |
| `GET /backend-api/automations?filter=scheduled`, `?filter=paused`, `?filter=finished` | scheduled tasks ("automations"), the three tabs of `chatgpt.com/scheduled`; see `list_automations.py` |
| `GET /backend-api/suggested_automations` | three template suggestions for creating a *new* automation; not used by any command here |
| `GET /api/auth/session` | mints the bearer token from the session cookie; also re-issues `__Secure-next-auth.session-token.*` with a fresh 90-day `Max-Age` on every call -- see "session token renewal" below and `chatgpt_session.py` |

**Captured 2026-09-21**, session token renewal. `GET /api/auth/session`
answers 200 and carries 5 `Set-Cookie` headers, among them
`__Secure-next-auth.session-token.0` and `.1`, both re-issued with
`Max-Age=7776000` (90 days) and `Expires` = now + 90 d. `GET
/backend-api/me` sets only `_cfuvid`, so this renewal is specific to the
auth handshake, not to every read. A re-issue does not invalidate an
older copy: Chrome's own cookie kept authenticating all day after a
scripted browser profile received a later one, and after this
handshake's own re-issue -- multiple valid copies of the token can
coexist. Only `__Secure-next-auth.session-token*` is load-bearing
(measured 2026-09-20 by removing cookies); `_account` and
`oai-client-session-epoch` are also re-issued here but nothing reads them
by name. The token is chunked (`.0`, `.1`); a future response may carry a
different number of chunks. `chatgpt_session.py`'s `Session.__init__`
reads these headers and stores the fresher of Chrome's jar or the
existing keyring copy into this machine's own keyring on every session it
builds (module docstring, "Session token renewal"; SKILL.md, the same
heading).

**Captured 2026-09-21** from the page's own search box and replayed over
plain HTTP: `POST /backend-api/global/search`, body `{"query": "<text>",
"limit": N, "source_requests": [{"type": "conversation"}]}`. The page also
sends `query_id` (a uuid) and `entrypoint: "global_search"`; both are
optional, and results are identical without them. `limit` must be 40 or
less: 41 and above answers HTTP 422 with a pydantic detail, "Input should
be less than or equal to 40". The page also asks for `{"type": "project"}`
and `{"type": "library", ...}` sources; `search_chats.py` never does --
projects have their own commands, and the file library is out of this
skill's scope entirely.

Response 200: `{"items": [...], "cursor": <str|null>, "partial_results":
bool, "source_statuses": [{"source_type": "conversation", "source_key":
"conversation", "status": "ok", "has_more": bool, "duration_ms": float,
"error_code": null}]}`. Each item: `{"id": "conversation:<uuid>",
"source_type": "conversation", "source_key": "conversation", "title": str,
"snippet": str|null (null on a title match), "update_time": float epoch
seconds, "match_kind": "title"|"content", "payload": {"kind":
"conversation", "conversation_id": "<uuid>", "message_id": "<uuid>",
"is_archived": bool, "is_starred": bool|null}}`. Paging follows `cursor`,
opaque base64 JSON, never built by hand: send the same body again plus
`"cursor": <the cursor string>` for the next page. No hits: `items: []`,
`cursor: null`, `has_more: false` (read from the one source status asked
for). Timing observed 0.7-2.1 s per page.

Project chats are included in the results -- confirmed by reading back
four of ten "worker" hits and finding a `gizmo_id` on each -- but the
payload itself carries no project id, so a hit cannot say *which* project;
`list_projects.py --id ... --chats` answers that. Whether an archived chat
can match is not verified: none happened to match during capture.

**Captured 2026-09-21**, scheduled tasks ("automations"). The page at
`https://chatgpt.com/scheduled` (`chatgpt.com/tasks` redirects there) lists
its "Active", "Paused" and "Finished" tabs from `GET
/backend-api/automations?filter=scheduled`, `?filter=paused` and
`?filter=finished`, reproduced over plain HTTP with the bearer token.
Response 200: `{"items": [...], "cursor": <str|null>}`; the cursor was
null in all three captures (7, 13 and 5 items) -- treat a non-null cursor
as "more exist, not followed" rather than guessing at the paging
parameter. 350-750 ms per filter.

Every item, in all three filters, has exactly these keys: `can_delete`,
`complexity`, `content_type`, `conversation_id`, `created_by_display_name`,
`current_user_role`, `default_timezone`, `display_emoji`,
`display_schedule`, `display_title`, `email_enabled`, `executor`,
`external_channel`, `id`, `is_enabled`, `last_edited_at`,
`last_edited_by_display_name`, `last_run_time`, `next_run_times`,
`notifications_enabled`, `prompt`, `schedule`, `schedule_components`,
`schedule_time_of_day`, `source_conversation_is_work_mode`,
`target_time_utc`, `team_id`, `thread_mode`, `timing_mode`, `title`,
`updated_at`, `webhook_triggers`. `schedule` is an iCalendar string, for
example `"BEGIN:VEVENT\nDTSTART:20260830T075934Z\nRRULE:FREQ=HOURLY\n
END:VEVENT"`. `schedule_components` is a dict with keys `by_day`,
`by_hour`, `by_minute`, `by_month`, `by_month_day`, `by_second`,
`by_year_day`, `frequency`, `start_time` (each null or a string, for
example `frequency` `"hourly"`, `by_minute` `"59"`). `timing_mode` values
seen: `"condition_watch"` (a monitor that polls), `"exact_schedule"`,
`"flexible_schedule"`. `display_schedule` is `"Monitoring"` for
`condition_watch` items and null otherwise. `next_run_times` is a list of
ISO strings with their own UTC offset, for example
`"2026-09-21T08:59:34+10:00"` (empty for finished items); `last_run_time`
is an ISO UTC string or null. `is_enabled` is true for scheduled, false
for paused and finished. `executor` is `"cloud"` and `thread_mode` is
`"existing_chat"` on every item seen. `conversation_id` is where the task
posts its results; `prompt` is the task's own instruction text, the
user's content. `list_automations.py` implements this.

`GET /backend-api/suggested_automations` returns a list of 3 template
suggestions: `title`, `description`, `user_message`,
`suggested_automation_type`, `system_hint`, `emoji`, `icon_url`,
`announcement_id`. No command uses it: it proposes a *new* automation to
create, and this skill's automations command is read-only.

**Captured 2026-09-21**, skills and apps, enriching the 2026-09-19 row.
`GET /backend-api/hazelnuts?include_permissions=true&scope=installed` ->
`{"hazelnuts": [...]}`: the user's own uploaded skills (4 on this account
that day). Keys per item: `base_sediment_id`, `brand_color`,
`check_sum_hash`, `creator_id`, `creator_name`, `default_version_no`,
`description`, `display_name`, `enabled`, `files` (a dict, path ->
`{"start", "length"}` byte ranges, never content), `icon_small`,
`iconography`, `id`, `in_my_list`, `last_updated_at`, `latest_version_no`,
`name`, `permissions` (`can_read`, `can_write`, `can_export`,
`can_share`, `can_share_workspace`, `can_enable_share`, `can_delete`),
`safety_check_status` (seen: `"blocked"`, `"unchecked"`), `safety_scan`
(`{"risk_score": int, "justification": str, "labels": [str]}`, for
example `labels` `["safeguard_evasion"]` on one skill), `sample_prompts`,
`sediment_id`, `short_description`, `surfaces` (for example `["tpp"]`),
`updated_at`. That day: `research-pipeline` was `enabled`,
`default_version_no` `"1"`, `latest_version_no` `"14"`,
`safety_check_status` `"blocked"`, `labels` `["safeguard_evasion"]`, 46
files. The meaning of `safety_check_status` and of default versus latest
version is **not verified**: `list_skills.py` prints them verbatim, never
interpreted.

`GET /backend-api/ps/plugins/installed?limit=1000` ->
`{"plugins": [...], "pagination": {"limit", "next_page_token"}}`: apps
and connectors (16 that day). Per item: `id`, `name`, `created_at`,
`scope` (`"GLOBAL"`/`"USER"`), `status` (`"ENABLED"` seen), `enabled`,
`installed_at`, `creator_name` (`"OpenAI"` or the user), `connector_id`,
`canonical_app_id`, `release` (`{version, display_name, description,
interface {...}, skills, ...}`), `installation_policy`,
`authentication_policy`, `disabled_reason`, `disabled_skill_names`.
`next_page_token` was null; a non-null token means more exist and is not
followed, the paging parameter being unknown, same as `automations`'
`cursor`. `list_skills.py --apps` implements this.

## Re-run this when

- a send starts failing in a way `probe_account.py` says is not the account
- a read returns 404 or 400 where it used to work
- someone proposes moving the send to HTTP, including a future me
- before trusting any endpoint list in this repo that is more than a few
  months old
