# Vendored modules

This skill imports nothing from a repository checkout. The modules below are
copies; when the origin changes, diff and re-copy rather than editing both.

| File | Origin | Taken | sha256 of the origin file |
|------|--------|-------|---------------------------|
| `scripts/chatgpt_client.py` | research-pipeline `.github/scripts/chatgpt_client.py`, branch `feat/chatgpt-orchestrator` | commit `00b5e1a0`, 2026-09-19 | `6f0ec336b0e2a7d24024edc9d82888ce6cea1559a7dca5b4a84492f0d9608688` |
| `scripts/chatgpt_session.py` | binnacle `.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_session.py` (untracked in that repository) | 2026-09-19 | `c00fb681645093cd9a680a8d1dd96b1979c6c4facdb983afc25e57647947e828` |
| `scripts/chatgpt_cookies.py` | binnacle `.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_cookies.py` (untracked in that repository) | 2026-09-19 | `1c172761abca0fd19c6a1905a59802bd87df9c6bbc01b542cf4a0b38539c37f1` |
| `scripts/round_state.py` | research-pipeline `.github/scripts/chatgpt_research.py`: `base_id`, `read_jsonl`, `paper_id_of`, `ANALYSIS_SUFFIX`, `admitted_ids`, `analysed_ids`, `skipped_papers`, `accounted_ids` | commit `00b5e1a0`, 2026-09-19 | `4bef6b11ba3366ac45ff0701f91dccecfd29b3772fab0527eec26923f0d1d7bb` |

## Local changes

- `chatgpt_client.py`: only the `HELPERS` and `PW_PYTHON` defaults differ. They
  point at this directory and its `.venv` instead of the binnacle checkout and
  the shared `pwvenv`; the `CHATGPT_HELPERS_DIR` / `CHATGPT_SEND_PYTHON`
  overrides still work. One docstring word changed ("binnacle's" to "bundled").
- `chatgpt_session.py`, `chatgpt_cookies.py`: byte-identical.
- `round_state.py`: the listed functions, byte-identical, in origin order.

## Re-syncing

Diff against the origin file and expect exactly the hunks listed above;
anything else is drift on one side or the other.

```bash
diff <research-pipeline checkout>/.github/scripts/chatgpt_client.py scripts/chatgpt_client.py
cmp  <binnacle checkout>/.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_session.py scripts/chatgpt_session.py
cmp  <binnacle checkout>/.claude/skills/chatgpt-mcp-dev/scripts/chatgpt_cookies.py scripts/chatgpt_cookies.py
```

For `round_state.py`, compare each listed function with its definition in
`chatgpt_research.py`. The tests in `tests/` exercise the commands over fakes;
run them after a re-sync with `.venv/bin/python -m pytest tests -q`.
