# chatgpt-web-operations

A shared local skill for Claude Code and Codex that drives
chatgpt.com from this machine: send prompts, collect replies, search and
list conversations, manage projects, run Deep research, inspect scheduled
tasks and installed skills, manage custom MCP apps and Platform tunnels,
and check the account's health. Account operations use HTTP. Sending and
browser diagnostics use a scripted browser.

Start with [setup and recovery](references/setup.md). It explains the host
dependencies, ChatGPT login, separate Platform credentials, first checks,
and fixes for missing access. An admin key exported in the process
environment takes precedence over the optional `admin.env` fallback file.
Scripts do not source `.bashrc` automatically.

Then use these references:

1. [SKILL.md](SKILL.md) -- the starting workflow and all 29 commands.
2. [VERIFICATION.md](VERIFICATION.md) -- test results and measured limits.
3. `ROADMAP.md`, `TESTING.md`, `HANDOVER.md`, `references/endpoint-discovery.md`,
   `references/failure-atlas.md`, `VENDORED.md`.

Everything here was measured against one ChatGPT Pro account on a
Raspberry Pi 5; endpoint shapes, timings and limits are dated in the
docs and may have changed since. It reads the logged-in session from
this machine's own Chrome and keeps a renewed copy of the session token
in the system keyring; nothing in it stores a credential in this tree.
Platform tunnel management uses an admin key or configured dashboard token.
The local forwarding daemon uses its separate runtime key.

`bootstrap.sh` builds the `.venv`; `make test` runs the offline suite
and the per-module coverage gate; the live tiers (`make live-read` and
the others) are opt-in and fenced to a sandbox project, see
`TESTING.md`.
