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
