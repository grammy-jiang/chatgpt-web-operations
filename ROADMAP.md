# Capability status and remaining work

Updated 2026-09-25. This is the current implementation inventory.
`VERIFICATION.md` records dated live and offline results. A command's
existence, a successful live request, and complete output support are
separate claims.

## Working rules

1. Record a new endpoint's actual request before implementing a mutation.
2. Keep the client and commands in this skill. Do not recreate a second
   client in the research-pipeline repository.
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

`chatgpt-refresh` provides tool-list refresh as an adjacent installed
command owned by binnacle's `chatgpt-mcp-dev` skill. `manage_tunnels.py`
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

## Historical decisions

The original stages and abandoned experiments remain in git history,
`HANDOVER.md`, `VENDORED.md` and the dated endpoint/failure records.
Cookie rewriting and composer-menu clicking were replaced by outgoing
request rewriting. The first MCP-only Deep research design was replaced
by the page/widget workflow. Historical notes do not override this inventory
or the current verification record.
