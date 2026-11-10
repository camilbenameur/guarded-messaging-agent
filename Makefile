.PHONY: up build test lint

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
