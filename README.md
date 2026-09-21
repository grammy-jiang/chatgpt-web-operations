# chatgpt-web-operations

A [Claude Code](https://claude.com/claude-code) skill that drives
chatgpt.com from this machine: send prompts, collect replies, search and
list conversations, manage projects, run Deep research, inspect scheduled
tasks and installed skills, and check the account's health -- as plain
HTTP calls wherever the site allows it, with a scripted browser only for
the one thing that is gated, the send.

This repository is a backup of a working tree that is still moving. Read
in this order:

1. `HANDOVER.md` -- the current state, measured, and the decisions behind it.
2. `SKILL.md` -- the command table, the hard rules, and the house facts.
3. `ROADMAP.md`, `TESTING.md`, `references/endpoint-discovery.md`,
   `references/failure-atlas.md`, `VENDORED.md`.

Everything here was measured against one ChatGPT Pro account on a
Raspberry Pi 5; endpoint shapes, timings and limits are dated in the
docs and may have changed since. It reads the logged-in session from
this machine's own Chrome and keeps a renewed copy of the session token
in the system keyring; nothing in it stores a credential in this tree.

`bootstrap.sh` builds the `.venv`; `make test` runs the offline suite
and the per-module coverage gate; the live tiers (`make live-read` and
the others) are opt-in and fenced to a sandbox project, see
`TESTING.md`.
