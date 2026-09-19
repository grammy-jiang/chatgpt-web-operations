# Vendored modules

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
