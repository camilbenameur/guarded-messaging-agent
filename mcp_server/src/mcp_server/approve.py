"""Console approval path for the send queue — the human half of the gate.

Usage (inside the `mcp` container, or any env with the packages installed):

    python -m mcp_server.approve --list          # show pending queued sends
    python -m mcp_server.approve <id>            # release one send through the bridge
    python -m mcp_server.approve --reject <id>   # discard one send (nothing is sent)

The store comes from `STORE_DB_PATH` (default `/store/messages.db`). The
bridge comes from `BRIDGE` (`telegram`, or `fake` with `BRIDGE_SCENARIO` for
demo/test); with neither configured, approvals are
refused fail-closed and the row stays pending. A full approval UI is
explicitly out of scope for the MVP —
this console entry plus `SendGate.approve()` (callable from tests) is the
whole surface.
"""

from __future__ import annotations

import argparse
import os
import sys

from shared.bridge_factory import BridgeConfigError, build_bridge
from shared.store import DEFAULT_DB_PATH, Store

from mcp_server.gate import SendGate


def _build_bridge():
    """The bridge selected by `BRIDGE` (`fake` + `BRIDGE_SCENARIO` for demos,
    `telegram` for the real transport); None when neither is configured, in
    which case approvals are refused fail-closed.
    """
    return build_bridge()


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
    try:
        bridge = _build_bridge()
    except BridgeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gate = SendGate(store, bridge=bridge)

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
