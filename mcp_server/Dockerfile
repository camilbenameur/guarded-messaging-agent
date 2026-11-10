# Build context is the repo root (see docker-compose.yml) so this can COPY the
# sibling `shared` package alongside `mcp_server`.
FROM python:3.12-slim

WORKDIR /app

COPY shared/ ./shared/
COPY mcp_server/ ./mcp_server/
# Committed test media (tests/fixtures/) — pytest's testpaths pulls in
# ../shared/tests, which needs these to exist relative to /app (mirrors the
# same fix in ingest/Dockerfile).
COPY tests/ ./tests/

RUN pip install --no-cache-dir -e ./shared \
    && pip install --no-cache-dir -e "./mcp_server[dev]"

WORKDIR /app/mcp_server

EXPOSE 8808

# --factory: build_asgi_app() runs at server start, not at import time, so a
# plain `import mcp_server.server` (e.g. during test collection) never has
# the side effect of touching the default /store path.
CMD ["uvicorn", "mcp_server.server:build_asgi_app", "--factory", "--host", "0.0.0.0", "--port", "8808"]
