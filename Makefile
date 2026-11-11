.PHONY: up build test lint demo

up:
	docker compose up --build

build:
	docker compose build

# Runs pytest inside both service containers (ingest, mcp). Each also collects
# the shared/tests placeholder via its pyproject.toml testpaths (see
# ingest/pyproject.toml, mcp_server/pyproject.toml).
test:
	docker compose run --rm ingest pytest
	docker compose run --rm mcp pytest

# ruff check on ingest+shared (via the ingest image, which has both installed)
# and mcp_server (via the mcp image).
lint:
	docker compose run --rm ingest ruff check .
	docker compose run --rm ingest ruff check ../shared
	docker compose run --rm mcp ruff check .

# One-command trial: no Docker, no API key, no Whisper download (ingest is NOT
# installed). Needs Python 3.12+ and network access for pip.
PYTHON ?= python3
demo:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -q ./shared "./mcp_server[dev]"
	cd mcp_server && ../.venv/bin/python -m pytest -q -m redteam
	.venv/bin/python scripts/demo.py
