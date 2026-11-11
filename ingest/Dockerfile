# Build context is the repo root (see docker-compose.yml) so this can COPY the
# sibling `shared` package alongside `ingest`.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY shared/ ./shared/
COPY ingest/ ./ingest/
# Committed test media (tests/fixtures/) — FakeBridge and the STT/vision
# suites resolve fixture paths relative to /app, mirroring the repo-root
# layout (see shared/tests/test_fake_bridge.py).
COPY tests/ ./tests/

RUN pip install --no-cache-dir -e ./shared \
    && pip install --no-cache-dir -e "./ingest[dev]"

# faster-whisper (huggingface_hub) downloads model weights here on
# first use. Backed by the `./data/models` volume in docker-compose.yml so
# the model isn't re-downloaded on every container start/restart.
ENV HF_HOME=/models
RUN mkdir -p /models

WORKDIR /app/ingest

# Long-running worker (BRIDGE=fake needs BRIDGE_SCENARIO; BRIDGE=telegram needs the
# session volume). Tests: `docker compose run --rm ingest pytest`.
CMD ["python", "-m", "ingest"]
