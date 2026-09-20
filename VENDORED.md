# Vendored modules

**Since 2026-09-20 the arrow points the other way.** The research-pipeline
repository no longer carries a copy of `chatgpt_client.py`: its
`chatgpt_research.py` imports this skill's client from
`~/.claude/skills/chatgpt-web-operations/scripts`, overridable through
`CHATGPT_SKILL_DIR`. Two copies is what let a broken mechanism run in
production for two months (`references/failure-atlas.md`), so there is now
one client, here, and the repository's 186 orchestrator tests run against
it. Anything below describes where the files came from, not a sync
obligation.

This skill imports nothing from a repository checkout. The modules below
started as copies.

**Since 2026-09-20 they are owned here.** The user decided that the
research-pipeline repository is not modified any more, so these files change
in this directory and are tested here; the repository's copies are history,
not an origin. The table records where each file came from.

| File | Origin | Taken | sha256 of the origin file |
|------|--------|-------|---------------------------|
| `scripts/chatgpt_client.py` | research-pipeline `.github/scripts/chatgpt_client.py`, branch `feat/chatgpt-orchestrator` | commit `00b5e1a0`, 2026-09-19 | `6f0ec336b0e2a7d24024edc9d82888ce6cea1559a7dca5b4a84492f0d9608688` |
| `scripts/chatgpt_session.py` | binnacle `.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_session.py` (untracked in that repository) | 2026-09-19 | `c00fb681645093cd9a680a8d1dd96b1979c6c4facdb983afc25e57647947e828` |
| `scripts/chatgpt_cookies.py` | binnacle `.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_cookies.py` (untracked in that repository) | 2026-09-19 | `1c172761abca0fd19c6a1905a59802bd87df9c6bbc01b542cf4a0b38539c37f1` |
| `scripts/round_state.py` | research-pipeline `.github/scripts/chatgpt_research.py`: `base_id`, `read_jsonl`, `paper_id_of`, `ANALYSIS_SUFFIX`, `admitted_ids`, `analysed_ids`, `skipped_papers`, `accounted_ids` | commit `00b5e1a0`, 2026-09-19 | `4bef6b11ba3366ac45ff0701f91dccecfd29b3772fab0527eec26923f0d1d7bb` |

## Local changes

- `chatgpt_client.py`, `chatgpt_session.py`, `chatgpt_cookies.py`: linted and
  formatted with this repository's ruff rules since 2026-09-20, so a diff
  against their origins is larger than the listed hunks.
- `round_state.py`: the listed functions, byte-identical, in origin order.
- 2026-09-20 (plain-HTTP-from-cron fix): `chatgpt_session.py` gained
  `ensure_desktop_env()`, called at the top of `_keyring_password()`, so a
  cron caller with no `DBUS_SESSION_BUS_ADDRESS` no longer dies trying to
  autolaunch a bus. This did not exist in the binnacle origin above; the
  sha256 in the table is still the 2026-09-19 origin snapshot and is not
  re-taken, per "owned here" above -- these files are not re-vendored, only
  changed and tested in place. `chatgpt_client.py`'s own `ensure_desktop_env`
  (previously the only copy, called from `virtual_display` alone) is now a
  thin delegate to the one above, so the HTTP read path and the browser path
  share one defaulting rule instead of the browser path being the only one
  that had it.
- 2026-09-21 (session token renewal): `chatgpt_session.py` gained a
  keyring-backed store (`load_stored_session`/`store_session`, over raw
  D-Bus like `_keyring_password`; service `org.freedesktop.secrets`,
  attributes `{"application": "chatgpt-web-operations", "purpose":
  "chatgpt-session-token"}`) and the pure functions that decide what to do
  with it (`renewed_session`, `choose_session`, `apply_session`,
  `apply_session_to_jar`, `chrome_session_record`). `_cookie_header` is now
  a thin wrapper over a new `_cookie_pairs`, which also reports Chrome's
  own session-token expiry; `Session.__init__` and `Session.call` share one
  HTTP path through a new `Session._request`, and `__init__` now renews the
  stored token as a side effect of authenticating (module docstring,
  "Session token renewal"; SKILL.md, the same heading). `chatgpt_cookies.py`'s
  `export()` and `chatgpt_client.py`'s `_patch_cookie_export` both apply the
  same choice through the new `apply_session_to_jar`, so the scripted
  browser and the plain-HTTP client never disagree about which session is
  live. `scripts/health.py`'s "session token" check now judges the later of
  Chrome's jar and the keyring copy. None of this existed in the binnacle
  origin above; the sha256 in the table is still the 2026-09-19 origin
  snapshot and is not re-taken, per "owned here" above.

## Provenance check

To see how far the repository's copy has drifted from this one, for
information only, nothing is synced:

```bash
diff <research-pipeline checkout>/.github/scripts/chatgpt_client.py scripts/chatgpt_client.py
cmp  <binnacle checkout>/.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_session.py scripts/chatgpt_session.py
cmp  <binnacle checkout>/.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_cookies.py scripts/chatgpt_cookies.py
```

For `round_state.py`, compare each listed function with its definition in
`chatgpt_research.py`.
