# Flaky tests

A test that fails without a code change is a defect (`PLAN-2026-09-27.md`
B6, `TESTING.md` P9). It gets a row here the day it is seen. Within a week
it is fixed, or quarantined with a deadline:

```python
@pytest.mark.quarantine(until="2026-10-06", reason="one line: what fails and why")
```

A quarantined test still runs, as a non-strict xfail, and fails again once
`until` has passed (`tests/conftest.py`, `apply_quarantine`), so a
quarantine cannot outlive its deadline unnoticed. `tests/test_flaky_ledger.py`
checks that every quarantine in the suite has a row here and that every
open row's deadline is at most a week after the day it was seen.

The record is each daily run's `tests.log`
(`~/.local/state/chatgpt-ops/runs/<run>/tests.log`). The suite runs in a
new random order every time (pytest-randomly); the last line of the log
names the order, and `--randomly-seed=<n>` runs it again in that order.

| Seen | Test | Symptom | Cause | Fix or quarantine | Deadline |
| --- | --- | --- | --- | --- | --- |
| 2026-09-27 | three fake-sender tests in `tests/test_client_browser.py` | failed under the daily wrapper, passed by hand | the wrapper exported `RP_SNAPSHOT_DIR` for `health.py --browser` and the same shell ran `make test`, so the tests recorded snapshots they did not expect | fixed in `f23cad1`: `tests/conftest.py` clears the recording settings for every non-live test; `tests/test_harness_replay.py` runs an inner pytest with them leaked | closed 2026-09-28 |
