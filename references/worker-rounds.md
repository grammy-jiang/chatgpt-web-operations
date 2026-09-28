# Worker rounds driven by an agent

Lessons from the P10.4 provider-hardening rounds of 2026-09-28 and
2026-09-29. A Claude Code coordinator drove about 30 worker chats in one
project: 13 parallel review lanes, repair patches, closure reviews and
independent reviews, most with zip attachments. It did not use
`chatgpt_research.py`. Each item below cost hours once; the cheap check or
rule that prevents it is written next to it.

## Design the task so a turn can finish

- **Ask for one compact JSON object.** Long Markdown answers died with the
  final message stuck `in_progress` and the turn never finished. Ask for
  "one JSON object and nothing else", give the schema, and cap the size
  (2500 characters worked).
- **Split large reviews.** At most four checklist cells per turn. Put a
  marker such as `"chunk": 3` in the required answer. The coordinator merges
  the chunks mechanically, then asks the worker to confirm the merged record
  in a short turn.
- **Give a finite contract.** Name the cells to check, the allowed verdicts
  and the output schema. The worker then knows when it is done.
- **Mark every task.** Make the first prompt line `TASK_ID: <unique id>`. The
  id finds the chat later, even after ChatGPT renames it.
- **Attach at most eight files.** Put large evidence in a zip with an
  `INVENTORY.json` that gives each member's sha256, so the worker can verify
  the bytes in its sandbox. Ask it to answer only
  `ATTACHMENTS_MISSING: <names>` when a file is not visible: one lane lost
  its `.py` and `.toml` attachments and had to be sent again.
- **Parse fences with attributes.** Workers wrote ` ```json id="..." `. Use
  a pattern such as `r'```json[^\n]*\n\s*(\{.*?\})\s*```'` and parse the last
  block. When a reply holds several blocks, ask for the one to parse last.

## Send

- One `send_prompt.py` call per chat:
  `--project g-p-<id> --effort extended --no-wait --attach ... --json <file>`.
- **Use a fresh browser profile per window when windows run in parallel:**
  `RP_BROWSER_PROFILE=` (empty). On 2026-09-29 the shared profile
  `/tmp/rp-browser-profile` stayed in a Cloudflare Turnstile loop and the
  user's clicks on the check did not help; fresh profiles sent at once. The
  shared profile is safe only with `RP_MAX_BROWSERS=1`. Never try to solve
  the check automatically.
- `RP_MAX_BROWSERS=3 RP_BROWSER_WAIT_SECONDS=3600` let three sends run while
  another session held slots. With the default cap a send waited forever
  behind the other session.
- Run `probe_account.py` before each send. An HTTP 401 `token_expired`
  cleared with a new session.
- **A client error does not prove the message was not posted.** The sender
  sometimes reported "not posted" for a posted message. Before sending
  again, compare the chat's message count before and after, or look in the
  project for a chat whose text carries the TASK_ID and adopt it. Otherwise
  the worker gets the same task twice.

## Wait and collect

- Collect over HTTP with `read_chat.py`. Never open a browser to read.
- **Poll slowly.** Every 4 to 20 minutes per chat is enough. A poller, a
  stall sampler and manual reads at the same time caused HTTP 429 and failed
  probes.
- **A max-effort turn can stop without an error.** The message count stays
  flat and the turn never finishes. Detect it by sampling message counts.
  The recovery that worked: a standard-effort continuation in the same chat,
  "FINISH NOW: output only the JSON". One review finished 12 minutes after
  it.
- **A chunk driver must check the chunk marker.** Without the check it took
  an older finished turn as the new answer.
- **The coordinator must keep a live wait.** Keep a background command that
  exits when the reply is ready, or a monitor that prints on a state change,
  and re-arm it when it expires. On 2026-09-28 a coordinator ended its turn
  "until notified" with nothing armed and sat idle for six hours.
- Persist every reply (text and parsed JSON, with the prompt and attachment
  hashes) as soon as it is collected. Cleanup deletes the chats.

## Clean up

- ChatGPT renames worker chats ("Classcell Repair Task"). Match chats by the
  recorded conversation id or by the TASK_ID, never by title.
- `gizmos/<id>/conversations` rejects `limit` above 50 with HTTP 422
  (2026-09-29). Page with the cursor; `list_projects.py --chats` and
  `clean_chats.py --project` both do (F-2026-09-29-16).
- `delete_project.py g-p-<id> --expect-name NAME --force --apply` deletes
  the project and every chat in it. Run it only after every reply is
  persisted.
- **Kill only exact PIDs.** A `pkill -f` or `pgrep -f` pattern that also
  occurs in the killer's own command line killed the coordinator's shell
  twice and once killed another session's send in flight.

## Division of work

- Workers review statically and return verdicts or patches. The
  coordinator runs every test, owns every shared file, and never takes a
  worker's output as the truth.
- Give each worker only its lane's files and the contract. A worker that
  must execute code to decide a cell marks it for the coordinator instead of
  guessing.
