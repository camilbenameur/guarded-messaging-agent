"""The `python -m ingest` poll loop survives transport errors and rate limits."""

from __future__ import annotations

from shared.telethon_bridge import BridgeRateLimited

from ingest.__main__ import run


class FlakyWorker:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.pending_runs = 0

    def run_once(self):
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def process_pending(self):
        self.pending_runs += 1


def test_loop_backs_off_and_keeps_going():
    worker = FlakyWorker([BridgeRateLimited(300), RuntimeError("network"), 2])
    sleeps: list[float] = []
    run(worker, 30, sleep=sleeps.append, max_loops=3)
    assert sleeps == [300, 60.0, 30]
    assert worker.pending_runs == 1  # only the successful tick derives text
