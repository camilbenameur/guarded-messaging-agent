"""Long-running ingest worker: `python -m ingest`.

Bridge from `BRIDGE` (`fake` + `BRIDGE_SCENARIO`, or `telegram`), store from
`STORE_DB_PATH`, media under `MEDIA_DIR` (default `/media`). Voice notes go
through local faster-whisper; images through Claude vision when
`ANTHROPIC_API_KEY` is set, a deterministic mock otherwise.
`INGEST_INTERVAL` (seconds, default 30) is the poll period.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from shared.bridge_factory import BridgeConfigError, build_bridge
from shared.store import DEFAULT_DB_PATH, Store

from ingest.worker import Worker

logger = logging.getLogger("ingest")


def run(worker: Worker, interval: float, *, sleep=time.sleep, max_loops: int | None = None) -> None:
    """Poll forever. A transport error (flood wait, network, expired session)
    never kills the loop and never advances the cursor; it backs off and retries.
    """
    from shared.telethon_bridge import BridgeRateLimited

    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        delay = interval
        try:
            n = worker.run_once()
            worker.process_pending()
            logger.info("ingest.tick ingested=%s", n)
        except BridgeRateLimited as exc:
            delay = max(interval, exc.retry_after)
            logger.warning("ingest.rate_limited retry_in=%.0fs", delay)
        except Exception:
            logger.exception("ingest.tick_failed")
            delay = max(interval, 60.0)
        sleep(delay)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        bridge = build_bridge()
    except BridgeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if bridge is None:
        print(
            "error: set BRIDGE=telegram, or BRIDGE=fake with BRIDGE_SCENARIO=<scenario.json>.",
            file=sys.stderr,
        )
        return 2

    from ingest.stt import WhisperSTT
    from ingest.vision import ClaudeVision, MockVision

    vision = ClaudeVision() if os.environ.get("ANTHROPIC_API_KEY") else MockVision()
    store = Store(os.environ.get("STORE_DB_PATH", DEFAULT_DB_PATH))
    worker = Worker(
        bridge,
        store,
        stt=WhisperSTT(),
        vision=vision,
        media_dir=os.environ.get("MEDIA_DIR", "/media"),
    )
    run(worker, float(os.environ.get("INGEST_INTERVAL", "30")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
