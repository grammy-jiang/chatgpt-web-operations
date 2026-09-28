# Failure atlas: what went wrong, what it actually was

Every entry is a real failure from driving chatgpt.com on this host. The
right-hand column is the cheap check that would have settled it, which is the
part worth remembering.

For first use or an immediate fix, start with [setup and recovery](setup.md).
It maps current errors to actions and verification commands. Platform
credential failures have their own [tunnel recovery table](tunnels.md#recovery).
The dated entries below explain the evidence behind those instructions.

## Verification findings, 2026-09-25

- Four connector commands existed but were absent from the main command
  table and had no offline coverage. Their separate endpoint document was
  omitted by the consistency test. Run the current tests instead of trusting
  a historical green count.
- `connect_connector.py` returned zero after a failed privacy PATCH.
  It now requires the requested value in the response and returns one on a
  failure or mismatch. The dry run includes that PATCH.
- `delete_connector.py` could uninstall an app after a link deletion failed.
  It now stops before app deletion. A failed initial lookup also stops.
- The health live test still used an old authentication signature and
  expected fourteen checks. It now accepts the current options and checks
  the fifteenth item, the skills inventory.
- HTTP authentication using a newer keyring cookie repeatedly received a
  Cloudflare challenge while the browser composer and Chrome-cookie HTTP
  authentication worked. A later keyring request worked too: neither expiry
  nor an account block was established. Session creation now tries the
  existing browser cookie once after a keyring authentication failure and
  saves renewals only from authenticated responses. No challenge solver is
  involved. `VERIFICATION.md` records the tests and remaining limits.
- A status read immediately after Deep research start found no widget and
  incorrectly said the research had used the wrong start method. The widget
  appeared later and the report completed. The message now recommends
  `status --wait` without inferring the start method.
- A completed widget report failed the export command's legacy title check.
  `export --force` produced valid DOCX and PDF files after widget status was
  DONE. The help and skill now explain this existing route and its guard.
- Exact search markers initially had zero hits. Both later matched their
  test chats as content hits. An immediate empty search is not proof that a
  conversation or its contents are missing.

## Wrong diagnoses

Three in one session on 2026-09-16, each stated confidently before measuring.
The user corrected two of them.

| Claimed | Actually | What settled it |
|---------|----------|-----------------|
| The account is rate-limiting me | binnacle's cookie decryptor exiting over an *irrelevant* analytics cookie | decrypt each cookie, print which one fails |
| My new Chromium flags slowed the composer fill | host contention: a full-repository pre-commit scan had the CPU. The trimmed flags were the **fastest** of four sets | fill the real prompt under each flag set, time it |
| A send-path 429 means the account is unusable for ~30 min | reads answered normally and two finished shard replies were waiting to be collected over HTTP | read the conversation, ask whether the turn finished |

The user's words: *"Probably the way you are checking it is not correct."*
And later: *"I still can visit my chatgpt, through chrome browser, so I think
it is not blocking me."*

A fourth on 2026-09-20, caught before the user had to: `create_project.py`
reported "no control matching button[aria-label=\"New project\"]; the sidebar
wording changed". The control and its label were unchanged (checked in the
user's Chrome). The script paused a fixed 8 s after `domcontentloaded`, and
at load average 6 (six test agents running) the sidebar was not rendered
yet. Fix: wait for the control itself, up to 60 s (32 s end to end on the
retry). A fixed pause on a loaded host reads exactly like changed wording.

And a fifth the same day, in the new send path: `send_prompt.py --title`
renamed the chat before waiting for the reply, and ChatGPT's own title
("Reply PONG") overwrote it when the first reply landed. The sandbox sweep
finds test chats by their `rp-test` title, so a lost rename is a chat that
never gets cleaned up. The rename now happens after the wait.

And the largest one, found the same evening by recording a real send: the
`oai-last-model-config` cookie rewrite, which `with_effort` and the
orchestrator's `TASK_EFFORT` relied on since 2026-09-16, never changed the
effort a send carried. Two sends made with the cookie set to `standard`
posted `thinking_effort: max`, the account's server-side
`last_used_model_config`. Nothing had verified the mechanism: the docs said
a transcript does not record the effort, and it does (assistant message
`metadata.thinking_effort`). Record the wire body and read the reply's
metadata; a setting that is "far steadier" but unmeasured is a guess.

One more, small, from fixing it: the first version of the send-body
recorder wrote "sent" from the page's `request` event, assuming the route
had already rewritten the body. Playwright fires that event before any
route runs, so the record said `sent == original` while the reply's
metadata proved the rewrite had gone out. The record is now written by the
route handler, which is the only place that knows both bodies. Order of
events is a measurement, not a guess.

And one from the attachment path: the first send with a file was ignored
by the page ("message was not posted") because the upload wait checked
"busy right now" 2 s after the input was set, and the busy indicator only
appears ~2.5 s later. A wait that can return before the thing it waits for
has started is not a wait. Poll for the indicator to appear and then clear.

And the last one of the day, self-inflicted: the Deep research probe was
declared "not started" from the reply's metadata (`gpt-5-6-instant`, no
search, no citations) before its text was read. The text said "Deep
Research has started working on your query and will update you with the
report". Read the reply before judging it; metadata describes the turn
that exists, not the one still coming.

From the Deep research capture: selecting a composer item ("+" → Web
search or Deep research) and *then* filling the prompt sends a body with
`system_hints: []`, because `Locator.fill()` resets the composer's state
and the pill goes with it. Two earlier "the click did not work" readings
were this. Verify the state right before the click on send, not right
after the selection.

One transient worth recognising: `deep_research.py export` failed once with
"could not open a session: The read operation timed out" and worked on a
retry twenty seconds later. That is the session mint (`/api/auth/session`)
timing out, not the account and not the connector. Retry once before
diagnosing anything.

### The health check that exhausted its own timeout

On 2026-09-23 the 05:25 daily `chatgpt-ops` check mailed a failure after
`scripts/health.py` hit its 120 s outer timeout. The mail contained two
`/api/auth/session` HTTP 403 messages and the helper's generic
"session cookie may be expired" wording, so the first reading was that the
session had expired. A manual run that evening authenticated immediately,
returned `/backend-api/me` 200, and read a renewed session horizon of about
90 days. The persistent `check.log` could not settle the discrepancy: it had
kept only `exit=124` and the timeout verdict, while the two 403s survived only
in that one local-mail body; `last-health.json` was replaced by the later run.

The monitor and the production client had incompatible time budgets.
`ChatGPTSession` quite reasonably used a production authentication ladder of
30 s, 90 s and 180 s between retries. The monitor wrapped all of `health.py`
in `timeout 120`. Two transient 403s therefore produced exactly the observed
shape: first attempt fails, sleep 30 s, second attempt fails, sleep 90 s, and
the outer watchdog kills the process before the third attempt can begin. The
wording about an expired cookie described one failed handshake, not a proven
root cause.

The fix keeps the resilient 30/90/180 s ladder for real work and gives the
health probe its own bounded policy: 5/10 s authentication backoff, 15 s per
request and two low-level request attempts, under a 180 s last-resort wrapper
ceiling. More importantly, every cron run now gets an immutable evidence
directory with the raw health output, structured credential-free HTTP/auth
JSONL, stage timeline, health/skills JSON, test output, environment context
and final result; `check.log` carries the run id that points to it. Events are
written as the run proceeds, so a timeout cannot erase the evidence that led
to it. Cookie values, bearer tokens, request/response bodies and `Set-Cookie`
are intentionally excluded or redacted.

The first post-fix verification run exposed the same structural mistake one
level later. `health.py` authenticated successfully and showed about 90 days
left on the session, while its account read path reported rate limiting; the
wrapper then launched a separate `list_skills.py`, which opened a *second*
session, saw two `/api/auth/session` 403 responses, and was killed by its own
120 s timeout. That contradictory evidence was useful: a 403 clearly did not
mean the stored cookie had expired. The follow-up removed the second login
entirely. `health.py` now reads the skills inventory over its already-open
session, emits that endpoint in the same diagnostic JSONL, and writes a
redacted `skills.json` beside `health.json`. One monitor run therefore has one
authentication history and one causal timeline.

The isolated live validation of that follow-up produced the most useful
counterexample yet: `/api/auth/session` returned 200 on the first attempt, a
later sidebar GET returned 403, the low-level 2 s retry returned 200, the
subsequent hazelnuts/skills GET returned 200, and the whole health check ended
`GO`. The session never changed. A lone 403 is therefore transport evidence to
record and retry, not proof of an expired cookie.

The lesson is twofold: a monitor's retry budget is a diagnostic policy, not
the production recovery policy; and an incident log must preserve the events
*before* the failure, not only the final exit code.

## The effort that was never set

From 2026-09-16 to 2026-09-20 every research send asked for a per-step
reasoning effort through `TASK_EFFORT`, and every one of them ran at the
profile's own setting instead. The mechanism was a cookie rewrite
(`with_effort` on `oai-last-model-config`); the page takes model and effort
from the account's server-side `last_used_model_config`. Four of the eight
steps asked for `extended` and ran at `max`: slower turns, more polling,
and polling volume is what earns this account its rate limits.

| What was believed | What was true | What settled it |
|-------------------|---------------|-----------------|
| The cookie pins the effort | The page ignores it | A send asking for `standard` recorded `thinking_effort: max`, run through the production client itself |
| A transcript does not record the effort | Every assistant message does | `metadata.thinking_effort`, `read_chat.py --effort` |
| The skill's fix reached production | The repository ran its own stale copy | Two copies of `chatgpt_client.py`, only one fixed |

Three lessons, in the order they bite. A setting you never read back is a
wish: the cookie was written, and nothing ever asked what the send
actually carried. A second copy of a file is a second place for a bug to
survive a fix; the repository now imports this skill's client and keeps
none of its own. And a test that asks for the value the profile already
uses proves nothing, which is why the live check picks a level that
differs from the account's current default.

## What actually failed, across eight finished topics

Mined 2026-09-20 from the ledgers of the real msgloom runs
(`*/chatgpt/conversations.json`, 353 entries, 340 done): eleven failures
and two replies that were generated and never collected.

| What failed | Times | Caught before a run starts? |
|-------------|------:|-----------------------------|
| Rate limit: a 429 on the conversations listing, and the account notice | 2 | yes, a cheap read says so |
| Host contention: a composer click timing out, a 600 s fill timing out | 2 | yes, memory and load |
| "message was not posted (no new user turn / conversation URL)" | 2 | partly; one cause was found on 2026-09-20 to be a send clicked before an upload finished |
| "The read operation timed out" | 2 | no, transient; retry once |
| "composer did not appear (logged out or challenged)" | 1 | yes, but only by opening a window |
| The assistant never finished within 1500 s | 1 | no, it is a run-time budget |
| A reply missing its required block | 1 | no, that is the model |

Two entries sit at `status: "sent"` to this day: the work was done and the
reply was never fetched. That is free evidence lying on the floor, and the
reason a preflight reports it with the command that collects it.

The orchestrator checks none of this at startup: it parses arguments,
makes the workdir, re-execs into Playwright and dispatches. Every
precondition is discovered by crashing into it, which is what
`preflight.py` exists to end.

## Looking for something in the wrong place all afternoon

The Deep research report was hunted through `get_state`, the carrier
conversation, `/conversations/<id>/messages`, the backing conversation, MCP
`resources/list`, four unsupported `export` types, a hand-written websocket
client, and the page's own JavaScript bundles, before it turned up in
`metadata.chatgpt_sdk.widget_state` on a tool message in the conversation
-- as native Markdown, over plain HTTP, exactly where the user had said a
normal chat keeps its results.

Two clues had been in hand from the start. The page bundle contained
`applyRemoteWidgetState$`, which stores a widget's state per message; it
was read and passed over. And when a tool-call message's metadata keys
were printed, `chatgpt_sdk` was in the list beside `connector_tool_payload`;
only the second was opened.

The lesson is not "look harder". It is that every one of those searches ran
against researches started by calling the connector's API directly, which
never attach a widget and never store a report. The question "where is the
report" had no answer for that path, and no amount of searching would have
produced one. When a search keeps coming up empty, check that the thing
being searched for was ever put there.

## Measurements worth keeping

Composer fill, the real 89 kB review prompt, on an idle host:

| Launch flags | Fill | Peak RSS |
|--------------|------|----------|
| original | 68.6 s | +2345 MB |
| trimmed (shipped) | 64.2 s | +1756 MB |
| trimmed, no JS heap cap | 63.1 s | +1935 MB |
| harmless trims only | 65.7 s | +2260 MB |

The trimmed set is the fastest and the lightest, so the flags were kept. A
fill that timed out at 225 s did so because the host was busy, not because of
a flag. The budget is now 6 s/kB capped at 600 s: a timeout costs a whole
retry cycle, waiting longer costs nothing when the fill was going to finish.

Sentinel requirements, live paid account, empty request body:

```
proofofwork: {"required": true, "seed": "...", "difficulty": "077a12"}
turnstile:   {"required": true, "dx": "..."}
so:          {"required": true, "collector_dx": "..."}
```

The page's send flow: `sentinel/chat-requirements/prepare` → `finalize` →
`f/conversation/prepare` → `f/conversation`.

## The silent round

Topic 04 round 2 admitted 29 papers, read 13, and synthesised, reviewed and
reported as though nothing were missing. Its report was round 1's evidence
rewritten, and the run started round 3 on the strength of it.

The trigger was one interrupt: the process was killed while analysis shards
were in flight. What made it silent was that **four** independent places each
accepted a cheap proxy for the thing that mattered.

| Seam | Accepted | Should have asked | Fix |
|------|----------|-------------------|-----|
| orchestrator completion | "an analysis file exists" — satisfied by the previous round's files, which `merge_prior_corpus` copies in | are the admitted papers read | 26ac03ea |
| runner auto-accept | a `delegated` task whose artifact "exists and parses" | did the worker actually finish — here the orchestrator *is* the worker | b30d5909 |
| `min_new_papers` | the mid-round shortlist count | the count recorded at the end of the round | 26ac03ea |
| shard skip | "a result file of that name exists" — names restart at shard-1 each pass | are this shard's papers read | 31c00c91 |

The fourth was in code written specifically to prevent the first three. That
is the lesson: a completion check that tests an easy proxy will eventually
pass on the wrong evidence, whatever layer it sits in.

Refusing to synthesise on a partial corpus is right; abandoning the round is
not. `paper-analyzer-web` re-shards the unread remainder and stops only when
a pass reads nothing new (ac4fe826), which is the honest signal.

## Fixed bugs worth not re-introducing

- **"Could not load this ChatGPT conversation."** Under read-path
  throttling (2026-09-26/27) the page of an existing chat showed this notice
  and a Retry button instead of the messages, and a follow-up send into the
  chat failed on it. `BrowserSender._await_chat` now runs after every load of
  a `/c/<id>` page: it waits (at most `RP_CHAT_LOAD_SETTLE_MS`, 15 s) for a
  turn or the notice; on the notice it presses Retry, or reloads when there is
  no button, after 5/10/20/40/60 s (+/-20 % jitter), up to
  `RP_CHAT_LOAD_RETRIES` (5) times, each retry logged (so in `RP_LOG_FILE`),
  then raises a `TransportError` naming the chat and the number of loads. A
  normal load clicks and waits for nothing; a new-chat page is never checked.
- **Cookie decryptor exits the process.** It accepted a value only when every
  character was printable ASCII. Empty, tab and non-ASCII values all fail
  that, and Chrome clears analytics cookies (`_dd_s`, `g_state`) constantly.
  One unreadable cookie must never end the session; only a missing session
  cookie is fatal (dcce571e).
- **Dismissing the rate-limit modal.** A fallback added to clear a dialog
  covering the composer also cleared the rate-limit dialog and retried,
  replacing the 429 backoff with a faster retry. Escape is for ordinary
  modals; the rate limit raises (f2ca38f7).
- **Review budget counted accepted verdicts.** Re-opening a round whose
  review had passed started at attempt 2, so the first rejection exceeded a
  budget nothing had spent and aborted the run. Count rejections only; a
  rerun is not a repair (4efb66da).
- **Generic backoff on a 429.** 60/120/240 s spends the budget in seven
  minutes while the limit is still in force (5e00de20).
- **A check that could never have worked, and a fake that let it pass.**
  `preflight.py --browser` called the sender's private `_composer` from the
  calling thread, on a page nothing had navigated. Either defect alone
  failed it every time -- "Cannot switch to a different thread", or 60 s
  waiting for a composer on about:blank -- and the record blamed the login.
  Its offline test passed because the fake `BrowserSender` offered
  `_composer` as a plain method: a fake kinder than the real object. Found
  2026-09-20, the first time anyone ran it. Fixed by giving the sender a
  public `probe_composer` (owner thread, navigates first) and by fakes that
  may expose only public methods the real class has
  (`test_the_fake_senders_offer_nothing_the_real_sender_does_not`). The
  wider lesson: preflight was in no live tier, so 31 offline-tested
  functions had never met the real account; `tests/live/test_read_preflight.py`
  is that.
  `measure_window.py` had the navigation half of the same defect, with the
  same kind of fake hiding it: with `--fill-file` it filled a composer it
  had never navigated to, and without one its "idle" number was a blank
  window's. It now goes through `probe_composer` and `fill_composer`, and
  the fill budget it compares against is the send's own (`fill_budget_ms`).
  `tests/live/test_browser_upload.py` was the third caller on the private
  seam; it worked only because it navigated itself. It now goes through
  `attach_files`, and a consistency test refuses any code outside
  `chatgpt_client.py` that reaches into `sender._<name>`. The rule that
  came out of all three: a new need is a new public method on the sender,
  never a reach past it.
- **(F-2026-09-27-1) The upload path's send-button check went blind with the
  2026-09-26 page, and nothing said so.** `COMPOSER_STATE_JS`, which `attach_files`
  and the T3 upload test read `send_exists` / `send_enabled` from, looked
  for `button[data-testid="send-button"]` or `aria-label="Send prompt"`.
  The page ChatGPT shipped on 2026-09-26 labels the button "Send" and gives
  it no test id, so the check answered "no send button" on a composer that
  had one. Found on 2026-09-27 by the first replay run over a recorded,
  filled composer (`tests/fixtures/dom/composer-filled.html`, recorded by
  the weekly send test): `composer_state()` said `send_exists: False` while
  the snapshot's own facts said the button was there. The script now tries
  the same alternatives `_click_send` tries (`SEND_BUTTONS`). The lesson is
  the reason tier R exists: two scripts read the same DOM for the same
  fact, and only a real page can show them disagreeing.
- **(F-2026-09-27-2) The daily wrapper's recording setting failed the offline
  suite it ran next, under cron only.** On 2026-09-27 `chatgpt-ops-check.sh --browser`
  began exporting `RP_SNAPSHOT_DIR` for `health.py`, and the `make test`
  it runs afterwards inherited it: three T0 tests with fake senders (which
  offer no `snapshot_page`) failed, while the same suite was green in every
  terminal. Found by running the wrapper by hand the same evening (one
  ALERT line in `check.log`). `tests/conftest.py` now clears the
  `RP_SNAPSHOT_*` settings and points `RP_EVENTS_FILE` and
  `RP_SCREENSHOT_DIR` into `tmp_path` for every test that is not live, so
  the suite reads nothing from the shell it happens to run in. The lesson
  is the old one with a new face: a test's environment is an input, and an
  input nobody pinned will change under cron first.
- **(F-2026-09-27-4) The upload chip's remove button was renamed on 2026-09-26,
  and the one tier that reads it had not run since 2026-09-25.** The T3 dry run
  (`tests/live/test_browser_upload.py`) uploads a file and asks
  `COMPOSER_STATE_JS` for the chip's remove button, which it found by the
  prefix "Remove file" ("Remove file 1: <name>", measured 2026-09-20). The
  2026-09-26 page labels it "Remove <name>" (and "Remove <name>(1).md" for a
  second upload of the same name), so the list came back empty and T3
  failed the first time it was run after the change, on 2026-09-27
  evening, when the owner asked whether the day's fixes were real. The
  page also lost `input#upload-files`; the fallback selector had been
  carrying every upload since. The recording is
  `tests/fixtures/dom/composer-attached.html`; tier R now asserts the label
  and the input on it, the weekly send runs T3 first and attaches a file to
  its one message, and `tests/test_dom_coverage.py` refuses a page selector
  that is not a named constant proven by tier R or excused by name. The
  lesson: a tier that runs only by hand has already broken; every tier gets
  a schedule.
  (F-2026-09-28-6) A footnote from the same night: the first weekly-script run after the fix
  reported "T3 failed" while the T3 test had passed. The session-scoped
  sandbox sweep opened an HTTP session in teardown for the browser tier too,
  and that one handshake met four Cloudflare 403 challenges in a row (the
  production ladder, 30/90/180 s: an 8-minute error on a green test). `health.py`
  authenticated normally a minute later. The sweep now runs only after the
  write and send tiers, the ones that can leave a conversation behind.
- **(F-2026-09-27-3) Promoting a recorded page rewrote a selector as an id.**
  `make refresh-dom-fixtures` ran the JSON facts through the generic fixture
  sanitizer, whose `user-<id>` rule turned the recorded selector
  `[data-user-message-bubble="true"]` into `[data-user-XXXXXXXX-bubble="true"]`;
  the first `make replay` over the promoted `conversation` fixture failed on
  it (2026-09-27). Only the URL field is scrubbed now, and the promotion test
  asserts a selector survives. The tier that compares live-computed facts
  with the fixture is what caught it, the same evening it was introduced.
- **(F-2026-09-28-5) The sanitizer dropped the `accept` attribute, and every
  file input on a recorded page matched the upload fallback selector.**
  `UPLOAD_INPUT_FALLBACK_SELECTOR` is `input[type="file"]:not([accept*="image"])`;
  with `accept` gone from the recording, three inputs matched where the live
  page matches one. Found by tier R the first time a test looked for the
  input (2026-09-28); `accept` is kept, and all four recorded pages were
  re-recorded. An attribute a selector reads must survive the sanitizer, and
  the only way to know which ones is to run every selector on the recording.
- **(F-2026-09-28-7) The shared-profile window carried no "playwright" in its
  arguments, so the in-flight check and the lifetime watchdog never saw the
  window a send uses by default.** `scripted_browser_pids()` required
  `PW_MARKER` ("playwright", Playwright's temporary profile path) in the
  arguments; since the shared profile `/tmp/rp-browser-profile` became the
  default, that string was absent from the default send window. Preflight
  said "no in-flight browser process" beside a running send, and the
  watchdog's kill set, `_own_pids & scripted_browser_pids()`, was empty, so
  the 1500 s lifetime ceiling was never enforced on the default path. Found
  2026-09-28 while looking for the window the owner had seen on the desktop.
  `scripted_markers()` now names the shared profile too, and
  `is_scripted_browser` is tested on the real argument vectors.
- **(F-2026-09-28-8) Thirty orphaned Xvfb servers.** `virtual_display()`
  stopped its Xvfb in `finally`, which never runs when the Python process is
  killed (a `timeout`, a SIGKILL, a parent that died first); 30 displays from
  2026-09-25 to 2026-09-27 were still running on 2026-09-28, each holding
  memory and an X display number, and no check counted them. Xvfb now dies
  with its parent (`prctl(PR_SET_PDEATHSIG)` in `_die_with_parent`), is
  killed when it ignores SIGTERM, and preflight's host group warns
  ("virtual displays") when more than `RP_MAX_BROWSERS + 1` are running,
  with the one-line fix.
- **(F-2026-09-28-9) A scripted window could reach the desktop.** On
  2026-09-28 the owner reported a Chrome window opening in front of every
  other window and taking the focus while they dictated. The send's window
  is meant to live on an Xvfb display, but nothing pinned Chrome to it:
  `virtual_display()` removed `WAYLAND_DISPLAY` and left `XDG_SESSION_TYPE`
  and `GDK_BACKEND` alone, and Chrome's platform choice follows those
  variables and its own release defaults. Not reproduced, and whether the
  window the owner saw was this skill's is not established (the Claude in
  Chrome extension raises the owner's own Chrome whenever an agent uses it).
  What changed so the question answers itself next time: Chrome is launched
  with `--ozone-platform=x11`, the block pins `GDK_BACKEND` and
  `XDG_SESSION_TYPE` to X11, and right after launch `_desktop_check` records
  a `window-on-desktop` event when a window of ours holds a socket to the
  Wayland compositor (`compositor_clients`, from `ss -xp` peer inodes) or
  sits on the desktop's `DISPLAY`. The first version of that check read
  the process's memory maps for `libwayland-client` and cried wolf twice
  the same night: GTK maps that library under X11 too. The probe that
  settled it: the send window on `:99`, five established connections to
  its Xvfb, no socket shared with `labwc`, `--ozone-platform=x11` in its
  arguments. An empty events file after a popup means the popup was not
  ours.
- **(F-2026-09-29-10) A posted message read as not posted.** The send
  decided "posted" by counting user turns on the page (`USER_TURN_SELECTOR`)
  and by nothing else. The events file's first two days (2026-09-27 and
  2026-09-28, runs of two other projects: ThreadDeck's phase workers and the
  python-migration-atlas P10.4 workers) held 42 `not-posted` events, and 40
  of them were messages the server had stored. 30 were new chats whose
  address had already become `/g/g-p-.../c/<id>` while the count read 0;
  ChatGPT gives a chat that id only once the message is stored, and the six
  read back over HTTP answered `conversation_deleted` (they existed, and
  were deleted later). 10 were follow-ups in two long atlas chats where the
  count read 4 or 5 before and the same or one fewer after; the
  conversation JSON holds every one of those ten messages, each posted two
  minutes before its event, and one of them twice (six minutes apart, the
  second after the first was reported not posted). The
  last 2 are undecided (the address was still the project page). Each
  false event failed the send (exit 1), and the callers went on from a
  failure. Why the count missed is not established: the scripted browser
  met a Cloudflare challenge when this was found, so the page could not be
  recorded; a long thread that renders only part of its turns fits the
  follow-ups, and a thread that did not render after the address changed
  fits the new chats. What changed: `post_evidence` accepts three things,
  strongest first: the page's own `f/conversation` POST answered 2xx (the
  sender hooks the page's request and response events and reads only what
  came after its own click), a new chat's real `/c/<id>` address, and one
  more user turn. A post the count did not see is a `post-unseen` event
  with a screenshot, so a drifting selector is counted instead of retried;
  a POST on the wire also ends the 20 s wait before the Enter fallback,
  which could otherwise submit a second time; and a stored message whose
  chat address never showed returns a provisional id that the caller
  resolves from the listing (`posted-no-id`). The events predate the
  display change of F-2026-09-28-9. The lesson: judge a send by the request
  that carries it, not by what the page happens to render.
