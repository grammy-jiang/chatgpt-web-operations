# Re-validation after ChatGPT's page change — 2026-10-01

The owner asked for every feature to be re-checked after ChatGPT changed its
site. Measured on this Raspberry Pi 5 against the same Pro account, tree
`0b44c42` plus this day's fixes, between 09:37 and 10:40 local time. The
2026-09-25 record below stays the acceptance baseline; this section records
what changed since, what was re-run, and what was found.

## What the page change did to the skill

Two things had broken while every suite stayed green, both found by reading
the daily check's evidence before changing anything (the order and the
sources are in `references/failure-atlas.md`, "Re-validation after the Work
surface, 2026-10-01"):

| Found | Evidence | Fixed by |
|-------|----------|----------|
| The home page can open on the new **Work** surface, and a send typed there never posts: every send of 2026-09-30 on the shared profile reported "not posted" with no `f/conversation` request. The Chat/Work toggle the client pressed since 2026-09-27 (a radio named "Chat") is now a group of `aria-pressed` buttons, so the press found nothing | the 2026-09-30 18:35 failure screenshot (Work pressed, "What should we work on?", model "GPT-6.1 Sol Light"); `events.jsonl` (`post_requests: 0`); the user's own Chrome through Claude in Chrome (toggle DOM, composer labels "Ask ChatGPT" / "Work with ChatGPT", the requests a switch fires) | `chatgpt_client.ensure_chat_surface`, run by every path that loads a composer; events `surface-switched` and `work-surface`; preflight's `WORK_FIX`; `page_snapshot` records the toggle and the daily comparison warns on `composer.aria_label` and `surface.found` (F-2026-10-01-20) |
| The daily browser check read "composer did not appear (logged out or challenged)" on 2026-09-29, 09-30 and 10-01 while the page was Cloudflare's "Verify you are human" interstitial; the shared profile `/tmp/rp-browser-profile` was challenged on every open, a fresh profile was not | the composer-missing screenshot; `preflight.py --browser` on the shared profile (challenged, 09:42) and with `RP_BROWSER_PROFILE=` (GO, 09:44); HTTP reads ok in the same runs; the user's Chrome not challenged | `find_composer` names the challenge page (`challenge_page`), event `challenge-page`, preflight's `CHALLENGE_FIX`; nothing solves or evades it (F-2026-10-01-21) |
| The first promotion of a page carrying the toggle was refused by the fixture hygiene rule for the "@" in a Tailwind class | `make refresh-dom-fixtures` | `sanitize_html` drops class tokens that start with "@" (F-2026-10-01-22) |

Everything else the selectors read is unchanged on 2026-10-01: the composer
(`div.ProseMirror[contenteditable][role=textbox]`, in a form), the user-turn
bubble, the three file inputs (only the "Attach files" one has no image
filter), the send button appearing only once text is in the composer, and
the upload chip's "Remove <name>" label. The five Power presets, the four
effort levels, the three send gates and the server's `last_used_model_config`
read exactly as on 2026-09-25; the Work surface's own model catalog
(`tpp/models/`, the `-wm` slugs) is separate and out of scope. The new
endpoints the page calls are listed in `references/endpoint-discovery.md`,
"Seen on 2026-10-01".

Each fix was removed once in a throwaway worktree of `0b44c42` with the new
tests copied in: the 17 named tests failed there (`tests/findings.json`,
`removed_and_failed`).

## What was run

| Check | Result |
| --- | --- |
| `make lint` | Ruff clean, 132 files. |
| `make test` (T0 and the coverage gate) | 2,425 passed, every module above its gate (95 % core / 90 % other); 2,407 before this day's tests. |
| `make replay` (tier R) | 40 passed, on the re-recorded `composer` fixture (home page, Chat pressed, the toggle in the markup) and the hand-written `composer-work-synthetic` page. |
| `make loopback` (tier L) | 43 passed. |
| `make live-local` (TL) | 4 passed. |
| `make live-read` (T1) | 39 passed in 166 s. |
| `make live-write` (T2) | 4 passed in 67 s: project create/delete, instructions and memory-scope round trips, the sandbox plugin's new release (0.1.1790814504), install, skill switch, uninstall. |
| `make live-browser` (T3, fresh profile) | 1 passed in 20 s: one upload, the chip's "Remove rp-test-…" label, the send button enabled; `composer-attached` recorded with the new facts (composer "New chat in rp-test-sandbox", no toggle on a project page). |
| `make live-send` (T4, fresh profiles) | **2 failed, 1 error in 814 s.** Every send's handshake was refused and the client took the page's bubble for a post (F-2026-10-01-23); the teardown then PATCHed a placeholder (F-2026-10-01-25). Both fixed; the tier was not re-run, see below. |
| HTTP probes by hand | `probe_account.py` CLEAR (`/me` 200); `probe_send_gates.py` three gates required; `model_settings.py` five presets and the server record unchanged; `probe_cookies.py` 33 of 33 cookies readable, session 90 days. |
| `manage_tunnels.py` by hand | dry run, create (`tunnel_6abda946ace88191a4006cee9715830e`), get, update preview, update `--apply` (read back), delete preview, delete `--confirm` (verified), get 404. The original tunnel untouched. |
| Connector commands by hand | `create_connector.py` dry run and create (`asdk_app_6abda94ec61c8191929b85ccb2723511` on the existing tunnel); `connect_connector.py` (`link_6abda95c…`, six tools, `full_access`); `list_connectors.py --match` (1 link, 1 app); `refresh_connector.py` (six tools); `delete_connector.py` dry run and `--confirm` (link, then app); the listing and the plugin list empty afterwards. |
| Other reads by hand | `profile_context.py --project`, `project_settings.py --show`, `manage_plugins.py list` (6), `list_chats.py --match`, `list_projects.py --id --chats`, `list_automations.py --filter all` (31), `list_skills.py --apps` (3 skills, 22 apps), `search_chats.py` (0 hits for a minute-old marker: the indexing lag of 2026-09-25 again). |
| `preflight.py --browser` by hand | shared profile: challenged (the interstitial), 09:42; `RP_BROWSER_PROFILE=`: GO at 09:44 and 09:58, the second recording promoted as `tests/fixtures/dom/composer.*`. |
| Scripted sends by hand | 10:19 one logged send (`--record-send-body`): 120 s waiting for a reply stream, no `f/conversation` request; 10:23 an instrumented send: `sentinel/chat-requirements/prepare` 403, `conversation/init` 403 twice, `f/conversation/prepare` 403, the bubble drawn, "Unknown error" under it; 10:33 one more on a fresh profile: the sidebar rendered, the composer never did (`composer-missing`). Cloudflare refuses the scripted window's requests in every shape it has today. |
| The user's own Chrome | one send into the sandbox at 10:24 (`f/conversation` 503, then the page's retry), the reply in 8 s; the chat was deleted by the T2 sandbox sweep at 10:28. |

**Not verified today, because every scripted window's requests were
refused:** the batch send (two prompts, one window), a continuation with
two attachments, `--search`, `--effort` and `--model` pins with the recorded
body and stream, a connector tool call through `--system-hint`, Deep
research start/status/fetch/export, `round_status.py --collect` on a live
reply, `measure_window.py`, `discover_endpoints.py` and `health.py
--browser`. All of them passed on 2026-09-25, their code did not change
except for the surface and handshake checks above, and their offline tests
pass. Run them when a scripted window gets through again (one window at a
time, `RP_BROWSER_PROFILE=`), in this order: `make live-send`, then the
sends of `SKILL.md`'s "Combining them" with `--json` records, then Deep
research, then `measure_window.py`, `discover_endpoints.py` and `health.py
--browser --record-shapes`, then `clean_chats.py --project <sandbox>
--delete --backup DIR --apply`.

## The follow-up, same day (F-2026-10-01-26)

The owner asked for the shared-profile problem to be fixed. What changed,
committed the same day:

- A fresh profile per window is the default; `RP_BROWSER_PROFILE=<dir>` opts
  into a persistent one. The daily and weekly wrappers in `~/.local/bin` set
  no profile, so they follow the default without a change. The stale shared
  profile was renamed `/tmp/rp-browser-profile.stale-20261001` (174 MB; the
  owner may delete it).
- After a refusal (`challenge-page`, `handshake-refused`, or a logged-in
  page whose composer never renders) no scripted window opens for
  `RP_BROWSER_COOLDOWN_SECONDS` (1800 s; 0 disables). `BrowserSender`
  refuses with the time the cool-down ends and status 429; `preflight.py`
  and `health.py` report it as the host check "browser cool-down"; the
  record is `~/.local/state/chatgpt-web-operations/browser-refusal.json`.
- `find_composer` names the third shape of the refusal, a logged-in page
  without a composer, instead of "logged out or challenged".

Not changed, and still the owner's: whether Cloudflare's refusal of this
host's scripted Chrome is per window or per host (one `preflight.py
--browser` after a quiet hour, then one a day, settles it: the events file
and the refusal record keep the answers); `binnacle-jobs`, which failed
every send on 2026-09-30 for the reasons above and now gets a fast,
named failure plus the cool-down instead of eleven retries.

Probe after the change, 11:21-11:23 (one fresh window each): `preflight.py
--browser` GO (the page and its composer load), then one real send into the
sandbox refused in 40 s with the reason named -- `403 on conversation/init,
403 on sentinel/chat-requirements/prepare, 403 on conversation/init`, no
`f/conversation` request -- and the cool-down set until 11:53. So at 11:23
the browser send path is still refused on this host while every HTTP path
works; the client now says so at once instead of after five minutes.

## The dedicated browser window (2026-10-01, afternoon)

The owner's decision after the morning: a browser only for what HTTP cannot
do, and that browser is one the owner launched and logged in to. The
experiment before any code changed, 11:38-11:45: a Chrome started by hand
with its own profile (`~/.local/share/chatgpt-web-operations/chrome`) and
`--remote-debugging-port=9222`, the owner logged in to chatgpt.com in it,
and a scratch Playwright script attached over CDP, opened a tab on the
sandbox project, filled the composer with the skill's own page functions
and clicked send:

| What | Result |
| --- | --- |
| `navigator.webdriver` in that tab | false (a normally launched browser) |
| `conversation/init`, `sentinel/chat-requirements/prepare`, `finalize`, `f/conversation/prepare` | all 200 (the scripted window: all 403 at 11:22) |
| `f/conversation` | 200; the reply `RP-CDP-c5183fa9` in 10 s; the address `/c/6abdbaea-…` |
| read back, backup, delete over HTTP | `read_chat.py`: the turn finished (`gpt-5-6`, no effort); `clean_chats.py --id --delete --backup --apply`; 404 afterwards |

Then the integration (`dedicated_browser.py`; `BrowserSender._open_attached`;
`RP_BROWSER_CDP`; preflight's "dedicated browser" check; T3
`tests/live/test_browser_dedicated.py`), with the offline gates and the
live tiers below.

| Check through the dedicated window | Result |
| --- | --- |
| `preflight.py --browser` | GO, "through the dedicated browser at http://127.0.0.1:9222"; the window's own tab untouched. |
| `send_prompt.py` (one prompt, sandbox) | posted with a real id at once, reply in 73 s, renamed, deleted, 404. |
| `make live-browser` (T3) | 2 passed: the upload dry run (chip "Remove rp-test-…", send enabled) and the window check; `composer-attached` recorded. |
| `make live-send` (T4), first run | 1 passed (pin/unpin/archive/unarchive on a minted chat), 2 failed: the effort pin on the window's "Auto" model recorded no effort (F-2026-10-01-30), and the round trip's conversation snapshot waited on a chat page's hidden composer (F-2026-10-01-27). |
| `make live-send` (T4), after the fixes | 2 passed (the chat flags and the CLI round trip with its attachment, backup and 404; `conversation` and `composer-filled` recorded and promoted), 1 failed: the effort pin, whose cause was found and fixed the same evening (F-2026-10-01-31, the table below). |
| batch send, two prompts in one window | both posted with real ids, both replies exact, both renamed. |
| continuation with two attachments | failed on the chat page's hidden composer (F-2026-10-01-27); fixed, not re-run. |
| `--search --effort standard --record-send-body` | posted; the reply used web search (`search=yes`) and the recorded body carried `search` and `standard`; the model stayed `gpt-5-6`, so no effort was recorded (F-2026-10-01-30); the stream wait never ended and the outer 20-minute timeout killed the command (F-2026-10-01-28). |
| `--model gpt-5-6-instant` | the reply recorded `gpt-5-6-instant`. |
| connector tool call (`--system-hint plugin:plugin_<app>`) | the MCP server ran `run_command echo RP-CONN-…` at 12:52:31 (`client=openai-mcp`); the recorded body carried the hint; the stream wait hung as above. |
| Deep research `start`, `status --wait`, `fetch`, `export --force` | DONE in about 3 min; 4,456 Markdown characters and 160 sources; DOCX 14,274 bytes; PDF 39,505 bytes. |
| `round_status.py --collect [--apply]` on a crafted ledger | the orphan reply classified collectable, archived, the ledger entry marked collected; `review_topic.py`: no integrity problem. |
| `measure_window.py`, `health.py --browser`, `preflight.py --browser` | pass whenever the home page is not already open in the owner's tab; from 13:54 all three failed on a tab bounced back to `about:blank` by the duplicate-tab blocker that Chrome sync had installed in the profile (F-2026-10-01-32: measured 14:05, a navigation to the home page sent back in one second, a project page kept). The failure now names the bounce; the extensions are the owner's to remove.; measured again 15:46-15:48 after the owner called it a one-off: four checks, two passed (`preflight` twice) and two bounced (`preflight`, `health`), the blocker still enabled in the profile (`disable_reasons: []`). |
| `discover_endpoints.py` | its own diagnostic browser; 4 endpoints in 20 s. |
| cleanup | the disposable connector's link and app deleted; 6 sandbox chats backed up and deleted; the window keeps only the owner's tab. |

A note the owner must read: logging the dedicated profile in to the Google
account signed that Chrome in too, and Chrome synced the owner's 23 Web
Store extensions into the profile (AdBlock, MetaMask, 2FAS, Bitwarden, the
Claude and Gemini extensions, a duplicate-tab blocker among them). Every
send's tab runs them, and the duplicate-tab blocker is what broke the
browser checks from 13:54 (F-2026-10-01-32). Sign the profile out of Chrome
(not out of chatgpt.com) or remove the extensions; `dedicated_browser.py
status` shows when the profile is clean. The skill was not changed to work
around them.

### The effort pin, the same evening (F-2026-10-01-31)

The owner asked where the effort is configured. Measured through the
dedicated window, four sandbox sends in all, each deleted afterwards, the
account's record read before and after each and unchanged
(`gpt-5-6-thinking` / `extended` for `web`):

| Step | Result |
| --- | --- |
| the picker on the sandbox project page (probe tab, no send) | "Instant", Power slider position 0 of 0-4; opening the menu makes no request. |
| the slider moved to "High" | `PATCH /backend-api/settings/user_last_used_model_config?model_slug=gpt-5-6-thinking&thinking_effort=extended`; the on-load `prepare` then carried `gpt-5-6-thinking` / `extended`; the picker kept "High" across a reload; no cookie or localStorage key changed. |
| S1: `effort=standard` pinned on the High body | original `extended`, sent `standard`, reply recorded `standard`; the body carried no `requested_default_model` / `one_off_model_override`. |
| S2a: the morning's shape reproduced (`one_off_model_override: true`, `requested_default_model` added before the pin) | reply recorded no effort -- the cause. |
| S3: the Instant shape (`model: gpt-5-6`, no effort, both fields) through the fixed rewrite | sent `gpt-5-6-thinking` / `standard` without the fields; reply recorded `standard`. |
| `make live-send`, `tests/live/test_send_effort.py` | 1 passed in 62.8 s. |
| fix removed (worktree at the previous commit) | the new rewrite test fails, as the ledger requires. |
| offline gates with the fix | lint clean; 2,497 passed; replay 40; loopback 43. |

### The owner's default and the window's extensions, 16:19-16:52

| Step | Result |
| --- | --- |
| the web default set to "Extra High" | `PATCH /backend-api/settings/user_last_used_model_config?model_slug=gpt-5-6-thinking&thinking_effort=max` over HTTP at 16:19: 200 `{"success": true}`; `settings/user` then read `web` and `default` = `max` (`ios_app` already `max`, `windows_app` kept `extended`); a probe tab in the dedicated window read "Extra High" in its picker. |
| `model_settings.py` | the report ends "rule 'below-pro' (one preset below Pro): preset 'Extra High' (model=gpt-5-6-thinking effort=max); the record matches it". |
| the rule in the skill | `below_pro_preset`, `--default below-pro [--apply]`, preflight's warn on drift; unit tests over the recorded catalog and hand-built presets (a renamed Pro found by lane, an effort-less neighbour skipped); T1 `test_model_settings_resolves_the_account_s_preset` and `test_run_group_resolves_what_a_send_inherits`: 2 passed. |
| the window restarted without extensions | `dedicated_browser.py stop` at 16:34 (the 16:30 attempt had missed the browser: Chrome's rewritten one-string cmdline, fixed by `argv_tokens`), the unit's restart 1, port back in 12 s; `status`: `extensions_disabled` true, 23 Web Store extensions in the profile; `/json`: 0 extension targets (11 before); picker "Extra High"; `preflight --browser` three times: composer appeared, no bounce. |
| `make live-browser` (T3) | 1 passed, 1 failed at 16:36 and again at 16:40 (the upload dry run: no chip, no send button); 2 passed at 16:52. The client's own `attach_files` at 16:50 showed the upload requests and the chip in 8.4 s (atlas, "Seen, not measured"). |
| offline gates | lint clean; 2,515 passed once the slider's write endpoint was excused from the recorded-shapes rule; replay 40; loopback 43. |

### The skill's own tab, 17:15-17:18

The owner asked why a tab cannot be specified. `RP_BROWSER_TAB` (`own` by
default, `url:<prefix>`, `new`) now says which tab of the window a send
drives; measured with the default:

| Step | Result |
| --- | --- |
| `preflight --browser`, no own tab yet | GO; the window went from one page (the owner's) to two; `dedicated-tab.json` written with the new tab's target id; `status`: "the skill's own tab is open at https://chatgpt.com/". |
| a sandbox send, `--effort extended` pinned | the own tab reused (still two pages), the reply recorded `extended`, the chat deleted; `status` then named the own tab at the chat's address. |
| `preflight --browser` again | the own tab reused (two pages), composer found; the run's verdict was DO NOT START because the HTTP read path answered 403 and then "rate limited" -- the day's request volume on the read side, not the browser. |
| two `preflight --browser` at once | both found the composer (one GO, one blocked by the same read-path limit); the lock's fallback to a fresh tab is covered by the unit test and was not caught in the act. |
| offline gates | lint clean; 2,525 passed; replay 40; loopback 43. |

The read-path 403/429 at 17:17 is a reminder that the live tiers and the
probes of one afternoon add up; the skill's own cool-down and preflight's
block are the right answer, and nothing else was run against the account
until 22:5x, when `/backend-api/me` answered 200 again.

### Detect and report a page change, 22:50-23:25

The owner's second ask. The monitoring that exists lives in `~/.local/bin`
(daily browser health with the DOM comparison at 05:25, the weekly send at
Sunday 05:40, the heal agent, mails to the local mailbox); what the skill
gained and how it was checked:

| Step | Result |
| --- | --- |
| page facts on a composer-missing message, event and `chatgpt-composer-missing.json` | unit tests over a faked page: the message ends `[page: challenge=no, login=no, composers=0 (0 hidden), toggle=yes, attach-menu=no, picker=no, title=chatgpt]`, the event carries the compact facts, the JSON beside the screenshot carries no markup. |
| ten more drift facts, and the stale-fixture note | `preflight --browser` at 23:13 through the dedicated window: "the page matches the fixture recorded 2026-09-30T23:58:56Z; 10 newer fact(s) not in the fixture yet (composers.visible, ..., page.login): re-record and promote". The recording: 1 composer (0 hidden), 3 file inputs (`#upload-files` gone, one accepts any file), attach menu and picker present, no challenge, no login route. |
| `composer-attached` re-recorded | T3 upload dry run at 23:13 with `RP_SNAPSHOT_DIR`: 1 passed in 44 s; the recording carries one attachment chip. |
| promotion | `make refresh-dom-fixtures` for `composer` and `composer-attached`; hygiene 3 passed; replay 43 passed (three new cases prove the attach menu, the picker, the file inputs and the flags on every recorded composer). |
| the send body's shape | `tests/fixtures/send/send_body.shape.json` from the evening's recorded High body (5,180 bytes, no value in it); the morning's Instant body against it reads "send_body: thinking_effort removed" plus two added fields -- the change of 2026-10-01, as the Sunday check would now report it. |
| wrapper edits | `chatgpt-ops-send-check.sh` compares the shape in its drift step; `chatgpt-ops-heal.sh` puts the page facts in the evidence and tells the agent a repeated "transient" is not one; both parse (`bash -n`), both backed up as `*.bak-20261001`. Not exercised by cron yet: 05:25 and Sunday 05:40 are the first runs. |
| offline gates | lint clean; the unit suite, replay and loopback green (the numbers in the gate log of this commit). |

### Triage, reproduction and approval, 2026-10-01 23:30 - 2026-10-02 00:10

| Step | Result |
| --- | --- |
| `triage.py` over the three real alert runs, in order, scratch registry | 09-29: escalate (unknown: the old client gave no page facts, streak 1); 09-30: escalate-repeat (streak 2); 10-01: escalate-repeat (class known-transient because the read path answered 403 that morning, exact-signature streak 1, browser streak 3). Three mornings, one incident. |
| `chatgpt-ops-repro.sh` on a copy of the 10-01 run | one read-only `preflight --browser` through the dedicated window, 63 s; the composer appeared; a second call refuses ("already done for this run"). |
| what the reproduction taught | its row was a warn: `controls.picker` differed from the fixture promoted at 23:13 -- the home page drew the model picker inside the composer on one load and not on the next, nothing else changed. The fact is recorded but no longer compared; a reproduction whose page drifts now escalates as a page change. |
| `triage.py` with that reproduction | digest (known-transient, 403); with the picker rule gone the same reproduction reads "composer ok". |
| unit tests | `tests/test_triage.py`: 35 tests, 99% of the module; classes, signatures, streaks, the ok reset, the reproduction's three effects, exit codes. |
| `chatgpt-ops-approve last` | shows the run's status, triage, reproduction, heal gates, patch stat and report; `--merge`, `--promote` and `--discard` ask before changing anything (not exercised: no heal branch to merge tonight). |
| wrappers | `chatgpt-ops-check.sh` and `chatgpt-ops-send-check.sh` reproduce, triage and call the agent only on exit 2, and record clean runs with `--ok`; `chatgpt-ops-heal.sh` carries the triage and reproduction in its evidence and allows `chatgpt-ops-repro.sh`; all parse (`bash -n`); backups `*.bak-20261002`. First scheduled proof: 05:25 on 2026-10-02. |

## What the owner decides

- `/tmp/rp-browser-profile.stale-20261001` (174 MB) was deleted at the owner's request, 2026-10-01 15:4x.
- Removing the 23 synced extensions from the dedicated profile for good: sign the profile out of Chrome first, then remove them in that window. The window no longer runs them meanwhile (`--disable-extensions`).
- When to retry `make live-send` and the browser acceptance sends: after a
  quiet period, one window at a time, with `preflight.py --browser` first.

Raw evidence (logs, the instrumented send's request log and screenshots,
the recordings before promotion) was kept in the session's scratchpad under
`/tmp/claude-1000/…/scratchpad/`, which the host clears; the committed
facts are in `references/failure-atlas.md`, the events file
`~/.local/state/chatgpt-web-operations/events.jsonl` and the screenshots
beside it.


# Verification record — 2026-09-25

The original 28 public commands were exercised on the installed Raspberry Pi 5 skill.
The checks found stale documentation, connector error-handling defects, and
misleading Deep research messages. Those were corrected. Live authentication
remains intermittent: some session requests receive an HTTP 403 Cloudflare
challenge, while later requests and browser sends work.

This is a dated acceptance result for the tested account and command paths.
It does not establish support for every ChatGPT UI feature or generic mode.
The five support modules are included in the offline suite.

Raw logs, account snapshots, request/stream captures, the object ledger, and
report exports are outside the repository in the private directory
`~/.local/state/chatgpt-web-operations/verification-20260925T024308Z/`.
No credentials or raw account payloads were added to tracked fixtures.

## Platform tunnel extension

`manage_tunnels.py` adds the 29th command: list/get/create/update/delete for
the OpenAI Platform tunnel resource. It uses an authorized dashboard token
or admin key for management; the existing runtime key can inspect a tunnel.
The onboarding skill's `tunnel-admin.sh` now delegates here. The server and
local forwarding daemon remain separate resources.

The 44 new offline tests passed with 100% line coverage for this command.
A live `get` succeeded with the existing runtime key. Vendor create/update/
delete/list wire shapes were captured with a synthetic credential against
a local recorder. Live create/list/get/update/delete then passed with the
local management key. The run also checked previews, a wrong-name refusal,
scope preservation, deletion read-back and cleanup. The original tunnel's
full record was unchanged. No credential value was copied into the
repository or output. `references/tunnels.md` documents credential setup
and command behavior. Raw acceptance logs are in the private directory
`~/.local/state/chatgpt-web-operations/tunnel-verification-20260925/`.

The existing admin key exported by the user's `.bashrc` was also verified
separately: a child Bash loaded it, and a read-only GET of the existing
tunnel returned HTTP 200. Only availability and status were reported.
The command itself does not load `.bashrc`; its admin lookup reads the
process environment before the private `admin.env` fallback.

The first-use instructions now link to `references/setup.md` and the
credential creation, selection and recovery sections in
`references/tunnels.md`. Official key-creation links and tunnel permissions
were checked against OpenAI documentation and installed `tunnel-client`
help on 2026-09-25. No new key was created for this documentation update.

## Test results

| Check | Result |
| --- | --- |
| Initial offline suite | 1,549 passed, five consistency failures. Four connector commands were absent from the main table, and their endpoint reference was omitted from the check. |
| Final `make all`, including tunnel extension | 1,660 passed; 28 live tests deselected as intended. All per-module gates passed: at least 95% for each core module and 90% for each command. Overall line coverage is 99.3%; the four connector commands and the tunnel command have 100%. |
| `make lint` | Passed: Ruff checks and formatting for 89 Python files. |
| Skill validation | Standard validator passed for both updated skills. Documentation/command consistency checks include the Platform tunnel endpoint reference. |
| First-use documentation | Checked 18 local links/anchors and the Bash syntax of eight setup/tunnel examples. Credential creation pages and permission guidance were checked against official OpenAI documentation and installed CLI help. |
| Live reads | A complete run passed all 22 tests. The final rerun passed 21 and hit an authentication challenge in the Chrome-cookie-only test; that isolated test passed on retry. The intermittent failure remains part of the result. |
| Live project writes | Three passed: create/delete, instruction update/restore, memory-scope update/restore. |
| Live browser upload | One passed: upload settled, attachment chip present, send enabled. |
| Live sends | Two passed: explicit effort and chat pin/unpin/archive/unarchive. |
| Additional acceptance sends | Six sends: two batch chats, one continuation, web search, Deep research, and a connector tool call. These are separate from the regular live-suite send cap. |
| Diagnostics | Browser preflight, health, memory/fill measurement, and endpoint/global discovery passed. Preflight and health returned the documented warning result for host CPU load. |

`make all` passed again after the setup documentation update. That log is
`~/.local/state/chatgpt-web-operations/commit-validation-20260925.log`.

Live tiers used the fixed sandbox. Additional acceptance used a separate,
uniquely named disposable project with project-only memory and a disposable
connector on an existing authorized tunnel. Commands used their normal entry
points and real transports. Later manual checks reused one authenticated
HTTP session; they did not mock network responses.

## Command inventory and evidence

| Command | Exercised behavior |
| --- | --- |
| `preflight.py` | Host, account, project and browser composer checks; JSON verdict. |
| `health.py` | Fifteen default checks; JSON, skill inventory and transport diagnostics. |
| `probe_account.py` | Authentication, account, chat listing and plan/credit reads. Subject to the authentication caveat below. |
| `probe_cookies.py` | Decryption/expiry summary and JSON; no cookie values printed. |
| `probe_send_gates.py` | Current requirements; documented browser-required result. |
| `model_settings.py` | Model catalog, effort levels and current server selection. |
| `profile_context.py` | Instructions, memory counts, model settings and test-project context. |
| `list_chats.py` | Recent, title-filtered, non-project, pinned and archived listings. |
| `search_chats.py` | Two-page search included the test project chat. Later exact-marker queries returned content matches for user text and attachment/reply text. |
| `list_projects.py` | Name match, details, file listing and project chat listing. |
| `list_automations.py` | All three states, prompts and JSON. No automation was changed. |
| `list_skills.py` | Skills, apps, JSON and an expected installed skill. |
| `list_connectors.py` | Apps/links, name filter, details, tunnels and JSON. |
| `create_connector.py` | Dry run, actual tunnel-backed No Auth creation, duplicate-name rejection. |
| `connect_connector.py` | Dry run, connection, six discovered tools, per-link privacy confirmation, reconnection. |
| `delete_connector.py` | Dry run, link-only deletion and app-plus-link deletion; listings confirmed removal. |
| `create_project.py` | Dry run, creation with instructions and project-only memory; read-back confirmed settings. |
| `delete_project.py` | Name guard, dry run, deletion; final read returned 404. |
| `project_settings.py` | Instruction replacement/clear and default/project-only memory round trips. |
| `send_prompt.py` | Two chats in one window; text plus PDF attachments in each; exact tokens in both replies; explicit model/effort, continuation, titles, search and plugin hints; JSON, request and completed SSE capture. |
| `deep_research.py` | Start, delayed widget startup, status/wait, Markdown/source/JSON collection, DOCX/PDF export with `--force`, DOCX text extraction. See completion-check limit below. |
| `read_chat.py` | Continuation reply text and effort metadata. |
| `pin_chat.py` | Dry run, pin, listing confirmation and unpin. |
| `clean_chats.py` | Archive/unarchive with read-back; selected chat deletion followed by 404. |
| `round_status.py` | Collection preview and recovery of a real reply into an isolated local transcript/ledger. Both attachment tokens appeared in the recovered text. |
| `review_topic.py` | Offline inspection of the recovery folder. Nonempty-corpus and failure cases remain covered by the offline suite. |
| `measure_window.py` | Opened a composer and measured memory plus prompt fill time. |
| `discover_endpoints.py` | Opened the page and captured requests and matching page globals. |

Deep research produced 2,469 Markdown characters and 22 sources. The DOCX
was 12,636 bytes with 2,575 extracted text characters. The PDF was 35,003
bytes; `pdftotext` extracted 2,599 characters of the report.

The connector called the disposable app's `run_command` once with an `echo`
nonce in `/tmp`. Its captured SSE tool result showed exit code zero and the
expected output; the local command log matched. The stored chat did not
retain the tool-result text, so the assistant reply alone was not used as
proof. The adjacent `chatgpt-refresh` command also refreshed all six tools.

## Corrections made

- Fixed invalid YAML in the skill's description; its unquoted colon made
  the frontmatter fail the standard skill validator.
- Added four existing connector commands to `SKILL.md` and their canonical
  endpoint reference to the consistency check.
- Privacy writes now fail when the response does not confirm the requested
  value. Their dry run displays the privacy PATCH too.
- App deletion stops if any link deletion fails. Failed initial lookups
  also stop instead of being treated as an absent app.
- Connector commands now have testable `main(argv)` entry points and
  regression tests for successful operations and failure paths.
- The health live test accepts current authentication options and expects
  the installed-skill check. Live tiers reuse an authenticated transport.
- Authentication tries the existing Chrome cookie once after a selected
  keyring login returns an incomplete response or HTTP 401/403. Failed
  authentication cannot overwrite the stored renewal. The fallback and
  negative cases are regression-tested; it does not solve a challenge.
- Deep research's missing-widget message now explains startup delay.
  Export help, comments and skill instructions describe the existing
  `--force` route for completed widget reports.
- Synchronized the inventory, roadmap, test procedure, handover pointer,
  and failure/endpoint references. Old counts and timings are historical.

## Capability corrections and limits

The earlier label “not implemented” was too broad. Pin/unpin,
archive/unarchive, project instructions/memory, content search, and all four
connector commands already had implementations. The generic
`send_prompt.py --system-hint` route also already existed. A live catalog
read returned `picture_v2`, `tasks`, `canvas`, `tatertot`, `sketch` and
`search`. Missing dedicated scripts do not mean these modes cannot be
invoked. Web search and plugin invocation were exercised here. Image,
Canvas, Study, Sketch, and scheduled-task creation/lifecycle were not tested
from start to finish; their artifacts and cleanup are not covered by the
ordinary text-reply collector.

Connector tools are refreshed by `refresh_connector.py`; the installed name
`chatgpt-refresh` points at it since 2026-09-29 (at the time of this run it
was binnacle's own tool). Project instructions are read and changed with
`project_settings.py --name NAME --show` and `--instructions`, which
replaced binnacle's `chatgpt-project` the same day. Neither renames or
moves a project. No dedicated automation lifecycle, account-memory
or custom-instruction editing, temporary-chat, message-edit/branch/regenerate,
voice, sharing, or library-management CLI was found.

Other observed limits:

- Authentication challenges occurred with both keyring and Chrome-cookie
  requests. The browser composer and later HTTP requests worked. This does
  not prove expired credentials or an account block. The fallback cannot
  guarantee authentication when both copies meet a challenge.
- A one-time Deep research status check can return 1 before the widget
  appears. Use `status --wait` after starting. Missing state does not prove
  that research was started through a different method.
- Export's default guard reads legacy “Generated report” titles. It returned
  3 for this completed widget report. Confirm DONE with `status --wait`,
  then use `export --force`. The older MCP-only start workflow was not
  rerun in this audit.
- Search indexing lagged. Both unique markers initially had zero hits and
  later matched their chats. Use recorded ids for immediate collection.
  Archived-chat search was not established.
- Automation and installed-app cursors are reported but not followed.
  Connector creation supports the captured tunnel/No Auth path, not direct
  server URL or OAuth setup.
- Batch request/stream captures share one path and retain the last send.
  Playwright emitted shutdown warnings in recorded sends; the captures
  still ended with `[DONE]`, and reply/tool evidence was complete.

## Cleanup

The selected disposable chat was deleted separately. The disposable project
and its four remaining chats were then deleted; the project read returned
404. Both connector links (the first and the reconnected link) and the app
were removed. The fixed sandbox was empty after its tests. Local prompts,
transcripts, exports and logs remain in the private evidence directory.
Server retention of uploaded attachment files was not independently
verified; this skill has no file-library deletion command.

A final independent read confirmed that the test project, app and links
were absent, the original MCP link remained, and the sandbox was empty.
The model configuration, custom instructions and memory summary matched
the initial profile snapshot.

## Uploaded research-pipeline skill deleted (2026-09-25, late evening)

At the user's request the `research-pipeline` skill uploaded to the account
(`hazelnuts`, latest version 14, `safety_check_status` `blocked`) was
deleted from the Skills page after a Download as backup (46 of 46 files;
the zip is kept outside the repository). Both requests are recorded in
`references/endpoint-discovery.md`; Delete has no confirmation dialog. A
read afterwards (`list_skills.py`) listed the three remaining skills and
no `research-pipeline`; the page showed it in neither section.

`health.py`'s skills inventory no longer requires a particular skill (it
had required this one since 2026-09-21), so the daily job does not turn
the deletion into a block. Offline suite: 1660 passed, coverage gate OK,
lint clean. Live `tests/live/test_read_skills.py`: 3 passed.

A cron-style run of the daily wrapper at 23:48 read the inventory as
"3 installed" (ok). The same run's conversations listing got HTTP 429 and
then a Cloudflare challenge (403) while another project's trial was
sending and polling through this skill, so that run's verdict was DO NOT
START for the conversations read, not for the skills change.

## Mutation testing pilot (2026-09-29)

`TESTING.md` P10. `make mutate MODULE=<file>` runs `tests/mutate.py`: every
mutation site of one module, found in its AST (comparisons, `and`/`or`, a
dropped `not`, `+`/`-`, `True`/`False`, an integer plus one, a compared or
indexed string, a returned value made `None`, a call statement made a
no-op), one at a time, in a throwaway git worktree of HEAD, at nice 19 and
one process, never while a send is in flight. A baseline run under per-test
coverage decides which tests each mutant runs: the tests that executed its
line. A mutant is killed when a test fails, times out, or cannot run because
the module no longer imports. mutmut 3.8 was tried first and dropped: it
imports its instrumented copies by package path, and these tests import each
command as a top-level module from `scripts/`.

| Module | Mutants | Killed, first run | Killed now | Time now |
| --- | --- | --- | --- | --- |
| `round_state.py` | 17 | 15 (88.2 %) | 17 (100 %) | 49 s |
| `chatgpt_cookies.py` | 24 | 21 (87.5 %) | 22 (91.7 %) | 70 s |
| `_common.py` | 37 | 30 (81.1 %) | 33 (89.2 %) | 2.5 min |
| `chatgpt_session.py` | 298 | 239 (80.2 %) | 293 (98.3 %) | 11.6 min |

The target, 80 % or more per core module, holds for all four. "Now" is after
the tests the survivors asked for: `tests/test_round_state.py` (a skipped
paper without a reason, malformed entries), `tests/test_cookies.py` (the
command names itself in its errors), `tests/test_common_edges.py` (`shorten`
at exactly its limit and at its default, a table row with an extra cell),
and `tests/test_session_mutants.py` (the login failure reason by status;
damaged plaintexts are None, never a cut value; a domain hash before an
empty value; the keyring's copy and the jar a browser gets; the renewal's
one-minute threshold; retry counts; the diagnostic events' outcome, status,
elapsed time and attempt numbers). Two existing tests were weaker than their
names and were corrected: the `CHATGPT_SESSION_STORE=0` test raised inside
functions that swallow every exception, so it passed with the bus opened;
and the fake Secret Service ignored the variant level of `OpenSession`'s
argument, which the real one checks.

The survivors left have no test because no test can see them:

- `chatgpt_cookies.py` line 46 and `chatgpt_session.py` lines 701 and 771,
  `con.close()` removed: the connection is to a temporary copy of the cookie
  database, deleted when the block ends.
- `chatgpt_cookies.py` line 47, the SameSite key 1 changed to 2: equivalent;
  a missing key falls back to "Lax", the value key 1 held.
- `_common.py` lines 51-52, the flushes before `os.execv` removed: every
  command calls `ensure_venv()` before it prints anything.
- `_common.py` line 63, `sys.path.insert(0, ...)` made `insert(1, ...)`:
  equivalent while nothing earlier on the path shadows a script's name.
- `_common.py` line 86, `strict=True` made `False` in the header row's `zip`:
  equivalent; the widths are computed from the headers.
- `chatgpt_session.py` line 311, the pad of an empty plaintext made 1:
  equivalent; an empty plaintext stays empty.
- `chatgpt_session.py` line 834, `range(2)` made `range(3)`: equivalent; the
  login loop always leaves at its second pass.
- `chatgpt_session.py` line 1039, a 200-character cut made 201 on "the
  answer was not JSON (...)": equivalent; that message is far shorter.

Cost: the first `chatgpt_session.py` run ran every chosen test file for
every mutant, about a minute each, five hours in all; it was stopped, and
the per-test coverage selection brought the same 298 mutants to under
twelve minutes. Run monthly by hand, one core module at a time.

## Project chat listing limit (2026-09-29, morning)

Measured read-only on the P10.4 worker project (30 chats) with the
authenticated HTTP session:

| Request | Result |
|---------|--------|
| `gizmos/<id>/conversations?cursor=0&limit=20` | 200, 20 items and a cursor |
| `...&limit=50` | 200, 30 items, cursor null |
| `...&limit=51` and `...&limit=80` | 422, no items |

Before the fix `list_projects.py --id <id> --chats --limit 80` printed
"0 conversation(s)". After `645cbb5` the same command lists the 30 chats and
exits 0 (live read, 2026-09-29). The four new offline tests fail with the
fix removed (F-2026-09-29-16). The worker-round rules learned on the same
project are in `references/worker-rounds.md`; they are operating guidance
from real runs, not a separately verified feature.

## Plugin management (2026-09-29, morning)

`manage_plugins.py` was built from one captured action per request (Claude
in Chrome, the user's own Chrome, a throwaway plugin `rp-test-plugin`;
`references/endpoint-discovery.md`, "Seen on 2026-09-29: plugins").

| Check | Result |
|-------|--------|
| capture: upload, install, download, new version, skill off and on, uninstall | every request recorded with its body; the page's route table read from its scripts |
| delete | not available for a personal account: no control, no route, `DELETE .../public/plugins/workspace/<id>` 400 and `DELETE .../ps/plugins/<id>` 404 on the throwaway plugin |
| `list`, `show`, `download` by hand | exit 0; 4 own plugins listed with install state; a 1,179-byte zip downloaded |
| T1 `test_manage_plugins_lists_shows_downloads_and_previews` | passed, 6.6 s |
| T2 `test_plugin_round_trip_on_the_sandbox_plugin`, first run | failed: the blob PUT answered 403 "error code: 1010" (F-2026-09-29-17) |
| T2, second run | failed: the release answered 400 "Plugin upload name must match the existing plugin" for a manifest without `$schema` (F-2026-09-29-18) |
| T2, after both fixes | passed, 19.5 s: new release, install, skill off and on, download of the new version, uninstall, each read back |

The sandbox plugin is kept, uninstalled, as `tests/live/sandbox.json`
names it. Debugging the two defects added two releases to it by hand
(0.0.2 again and 0.1.2), and the passing T2 run one more (0.1.1790638260);
each T2 run adds a release, and a plugin's releases cannot be removed
either.

## Reading a deleted conversation (2026-09-29, afternoon)

After the P10.4 worker project was deleted, `read_chat.py` on two of its
recorded chat ids got HTTP 404 with the reason "Conversation has been
deleted. Start a new chat." and ended in a traceback. After `827a3dd` the
same live read prints that reason in one line and exits 3. The three new
offline tests fail with the fix removed (F-2026-09-29-19).
