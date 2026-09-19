PY := .venv/bin/python

.PHONY: test lint fmt live-read live-write live-browser live-send all

test:
	$(PY) -m pytest tests -q -m "not live_read and not live_write and not live_browser and not live_send" --cov=scripts --cov-report=json --cov-report=term-missing:skip-covered
	$(PY) tests/coverage_gate.py

lint:
	uvx ruff@0.14 check . && uvx ruff@0.14 format --check .

fmt:
	uvx ruff@0.14 format .

live-read:
	CHATGPT_LIVE=read $(PY) -m pytest tests/live -q -m live_read

live-write:
	CHATGPT_LIVE=write $(PY) -m pytest tests/live -q -m live_write

live-browser:
	CHATGPT_LIVE=browser $(PY) -m pytest tests/live -q -m live_browser

live-send:
	CHATGPT_LIVE=send $(PY) -m pytest tests/live -q -m live_send

all: lint test
