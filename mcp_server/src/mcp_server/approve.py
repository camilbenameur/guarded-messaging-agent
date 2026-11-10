"""Console approval path for the send queue — the human half of the gate.

Usage (inside the `mcp` container, or any env with the packages installed):

    python -m mcp_server.approve --list          # show pending queued sends
    python -m mcp_server.approve <id>            # release one send through the bridge
    python -m mcp_server.approve --reject <id>   # discard one send (nothing is sent)

The store comes from `STORE_DB_PATH` (default `/store/messages.db`). The
bridge comes from `BRIDGE_SCENARIO` (a `FakeBridge` scenario path — demo/test
only); with neither that nor a real transport configured, approvals are
refused fail-closed and the row stays pending. A full approval UI is
explicitly out of scope for the MVP —
this console entry plus `SendGate.approve()` (callable from tests) is the
whole surface.
"""

from __future__ import annotations

import argparse
import os
import sys

from shared.store import DEFAULT_DB_PATH, Store

from mcp_server.gate import SendGate


def _build_bridge():
    """`FakeBridge` when `BRIDGE_SCENARIO` points at a scenario file (demo /
    manual testing), else None — a real Telethon bridge would be swapped in here.
    """
    scenario = os.environ.get("BRIDGE_SCENARIO")
    if scenario:
        from shared.fake_bridge import FakeBridge

        return FakeBridge(scenario)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_server.approve",
        description="List, release, or reject sends parked in the human-approval queue.",
    )
    parser.add_argument("queue_id", nargs="?", type=int, help="queue id to act on")
    parser.add_argument("--list", action="store_true", help="list pending queued sends")
    parser.add_argument(
        "--reject", action="store_true", help="reject the queued send instead of releasing it"
    )
    args = parser.parse_args(argv)

    store = Store(os.environ.get("STORE_DB_PATH", DEFAULT_DB_PATH))
    gate = SendGate(store, bridge=_build_bridge())

    if args.list or args.queue_id is None:
        pending = gate.list_pending()
        if not pending:
            print("send queue: no pending sends")
            return 0
        for row in pending:
            print(
                f"[{row['id']}] chat={row['chat_id']} queued_ts={row['created_ts']} "
                f"reason={row['reason']}"
            )
            print(f"      text: {row['text']}")
        return 0

    result = gate.reject(args.queue_id) if args.reject else gate.approve(args.queue_id)
    suffix = f" (message_id={result.message_id})" if result.message_id else ""
    print(f"{result.status}: {result.reason}{suffix}")
    return 0 if result.status in ("sent", "rejected") else 1


if __name__ == "__main__":
    sys.exit(main())
