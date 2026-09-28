PY := .venv/bin/python

.PHONY: test lint fmt replay loopback refresh-dom-fixtures contract refresh-shapes read-paths live-read live-write live-browser live-send live-local all

test:
	$(PY) -m pytest tests -q -m "not live_read and not live_write and not live_browser and not live_send and not live_local and not replay and not loopback" --cov=scripts --cov-report=json --cov-report=term-missing:skip-covered
	$(PY) tests/coverage_gate.py

# Tier R: the client's page functions on recorded DOM fixtures in a real
# headless Chrome, no network (TESTING.md section 6, P1). Needs Chrome.
# FIXTURES=<dir> overlays a fresh recording (a run's dom/) on the committed
# fixtures: the offline acceptance gate for a fix, before promotion.
replay:
	$(if $(FIXTURES),RP_DOM_FIXTURES=$(FIXTURES)) $(PY) -m pytest tests/replay -q -m replay

# Tier L: the real HTTP client (urllib, the retry and authentication
# ladders, a synthetic cookie jar, command mains, one subprocess) against a
# fake chatgpt.com on 127.0.0.1 (TESTING.md section 6, P6). No account, no
# network beyond loopback, no keyring.
loopback:
	$(PY) -m pytest tests/loopback -q -m loopback

# Promote a recorded DOM snapshot into tests/fixtures/dom/ after re-sanitizing
# it: FROM=<dir> selects the snapshot directory (default: the newest daily
# run's), NAME=<name> the snapshot (default composer; the weekly send records
# conversation and composer-filled). Never from cron: a fixture change is a
# reviewed commit.
refresh-dom-fixtures:
	$(PY) tests/refresh_dom_fixtures.py $(FROM) $(if $(NAME),--name $(NAME))

# The API contracts (TESTING.md section 6, P5): every read command over
# payloads synthesized from the recorded response shapes. Part of make test;
# SHAPES=<dir> overlays a fresh recording (a run's http/) first, the offline
# acceptance gate for a fix after the daily check reported a changed shape.
contract:
	$(PY) -m pytest tests/test_api_contracts.py -q $(if $(SHAPES),--api-shapes=$(SHAPES))

# Merge the newest recorded shapes (FROM=<dir> for another recording) into
# tests/fixtures/http; REPLACE=1 replaces instead of merging. Never from cron.
refresh-shapes:
	$(PY) tests/refresh_api_shapes.py $(FROM) $(if $(REPLACE),--replace)

# Regenerate tests/fixtures/http/read_paths.json: the fields the contracts
# read, which are the only ones the daily check warns about.
read-paths:
	$(PY) tests/refresh_read_paths.py

lint:
	uvx ruff@0.14 check . && uvx ruff@0.14 format --check .

fmt:
	uvx ruff@0.14 format .

# Tier TL: this machine's real cookie DB (a copy) and keyring, no network
# (TESTING.md section 6, P7). Daily from the wrapper, before health.py.
live-local:
	CHATGPT_LIVE=local $(PY) -m pytest tests/local -q -m live_local

live-read:
	CHATGPT_LIVE=read $(PY) -m pytest tests/live -q -m live_read

live-write:
	CHATGPT_LIVE=write $(PY) -m pytest tests/live -q -m live_write

live-browser:
	CHATGPT_LIVE=browser $(PY) -m pytest tests/live -q -m live_browser

live-send:
	CHATGPT_LIVE=send $(PY) -m pytest tests/live -q -m live_send

all: lint test
