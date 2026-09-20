# Failure atlas: what went wrong, what it actually was

Every entry is a real failure from driving chatgpt.com on this host. The
right-hand column is the cheap check that would have settled it, which is the
part worth remembering.

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
