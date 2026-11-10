"""The send gate: the single choke point every `send_message` flows through.

Every send request passes through this gate:

    request -> kill switch -> policy evaluation (mode + never_when/allow_when
    allowlist) -> rate limit -> human approval queue | bridge.send_text
    -> audit_log entry (approved_by = 'auto-policy' | 'human' | None)

Outcomes of `SendGate.request_send`:
- ``sent``   — auto-policy pass: the per-chat policy is in auto mode and every
  condition held; the message went to `bridge.send_text` and the audit row
  carries `approved_by='auto-policy'`.
- ``queued`` — everything else that isn't a hard refusal: parked in the
  `send_queue` table (see `shared.store`) until a human
  releases it with `SendGate.approve` / `python -m mcp_server.approve <id>`.
  Released sends audit `approved_by='human'`.
- ``denied`` — hard refusal: global kill switch (`SEND_KILL_SWITCH` env, the
  global circuit breaker — refuses ALL sends, auto and pre-approved alike)
  or a malformed policy (fail closed).

This module is the only place in the codebase allowed to call
`Bridge.send_text`. Every decision — sent, queued, denied,
rejected — writes an audit row; nothing happens silently.

Determinism for tests: the clock is injected (`clock=`), rate limiting counts
auto-sends off the audit log's timestamps (written with that same clock), and
the kill switch is re-read from the environment on every call.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from shared.bridge import Bridge
from shared.store import Store

from mcp_server.policy import Decision, SendContext, evaluate

logger = logging.getLogger(__name__)

#: Global circuit breaker ("global circuit breaker"). Documented in
#: `.env.example`. Truthy values refuse every send — auto-policy passes AND
#: human-approved releases — regardless of any per-chat policy.
KILL_SWITCH_ENV = "SEND_KILL_SWITCH"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

QUEUE_STATUS_APPROVED = "approved"
QUEUE_STATUS_REJECTED = "rejected"


def kill_switch_on() -> bool:
    """Read `SEND_KILL_SWITCH` fresh from the environment (never cached, so
    flipping it takes effect on the very next send attempt).
    """
    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class GateResult:
    """Outcome of one gate operation. `reason` is always populated and is the
    same text written to the audit log / queue row.
    """

    status: Literal["sent", "queued", "denied", "rejected"]
    reason: str
    queue_id: int | None = None
    message_id: str | None = None


class SendGate:
    """Policy + approval-queue + rate-limit + audit gate over `bridge.send_text`.

    `bridge=None` (no transport wired yet) is fully supported and fails
    closed: auto-policy passes are downgraded to the queue, and approvals are
    refused until a bridge exists. Nothing can leave the system without a
    bridge *and* a gate pass.
    """

    def __init__(
        self,
        store: Store,
        bridge: Bridge | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._clock = clock

    # -- request path ---------------------------------------------------------

    def request_send(
        self,
        chat_id: str,
        text: str,
        intent: str | None = None,
        confidence: float | None = None,
    ) -> GateResult:
        """Gate one send request. See the module docstring for the flow;
        every branch writes exactly one audit row.
        """
        now = int(self._clock())

        if kill_switch_on():
            return self._deny(chat_id, text, "kill switch active (SEND_KILL_SWITCH)", now)

        policy = self._store.get_policy(chat_id)
        context = self._build_context(chat_id, text, intent, confidence, now)
        decision: Decision = evaluate(policy, context)

        if decision.outcome == "deny":
            return self._deny(chat_id, text, decision.reason, now)
        if decision.outcome == "queue":
            return self._enqueue(chat_id, text, decision.reason, now)

        # decision.outcome == "allow" — auto-policy pass.
        if self._bridge is None:
            return self._enqueue(
                chat_id,
                text,
                decision.reason + " — but no bridge configured; queued for manual release",
                now,
            )
        message_id = self._bridge.send_text(chat_id, text)
        self._store.append_audit(
            "send",
            chat_id,
            {"text": text, "reason": decision.reason, "message_id": message_id},
            approved_by="auto-policy",
            ts=now,
        )
        logger.info("gate.sent_auto", extra={"chat_id": chat_id, "message_id": message_id})
        return GateResult(status="sent", reason=decision.reason, message_id=message_id)

    # -- approval path (console/tests — `python -m mcp_server.approve`) --------

    def approve(self, queue_id: int, approved_by: str = "human") -> GateResult:
        """Release one pending queued send through the bridge. Still subject
        to the kill switch — approval is not a bypass; the row stays pending
        so it can be released once the incident is over.
        """
        now = int(self._clock())
        row = self._store.get_queued_send(queue_id)
        if row is None:
            return GateResult(status="denied", reason=f"no queued send with id {queue_id}")
        if row["status"] != "pending":
            return GateResult(
                status="denied",
                reason=f"queued send {queue_id} already resolved ({row['status']})",
            )
        if kill_switch_on():
            return self._deny(
                row["chat_id"],
                row["text"],
                "kill switch active (SEND_KILL_SWITCH) — approved release refused",
                now,
                queue_id=queue_id,
            )
        if self._bridge is None:
            return GateResult(
                status="denied",
                reason="no bridge configured — cannot release queued sends yet",
                queue_id=queue_id,
            )

        message_id = self._bridge.send_text(row["chat_id"], row["text"])
        self._store.resolve_queued_send(
            queue_id,
            QUEUE_STATUS_APPROVED,
            approved_by=approved_by,
            sent_message_id=message_id,
            ts=now,
        )
        self._store.append_audit(
            "send",
            row["chat_id"],
            {"text": row["text"], "queue_id": queue_id, "message_id": message_id},
            approved_by=approved_by,
            ts=now,
        )
        logger.info(
            "gate.sent_released",
            extra={"chat_id": row["chat_id"], "queue_id": queue_id, "message_id": message_id},
        )
        return GateResult(
            status="sent",
            reason=f"released from queue by {approved_by}",
            queue_id=queue_id,
            message_id=message_id,
        )

    def reject(self, queue_id: int, rejected_by: str = "human") -> GateResult:
        """Discard one pending queued send. Nothing reaches the bridge."""
        now = int(self._clock())
        row = self._store.get_queued_send(queue_id)
        if row is None:
            return GateResult(status="denied", reason=f"no queued send with id {queue_id}")
        if not self._store.resolve_queued_send(
            queue_id, QUEUE_STATUS_REJECTED, approved_by=rejected_by, ts=now
        ):
            return GateResult(
                status="denied",
                reason=f"queued send {queue_id} already resolved ({row['status']})",
            )
        self._store.append_audit(
            "send_rejected",
            row["chat_id"],
            {"text": row["text"], "queue_id": queue_id},
            approved_by=rejected_by,
            ts=now,
        )
        return GateResult(status="rejected", reason=f"rejected by {rejected_by}", queue_id=queue_id)

    def list_pending(self) -> list[dict[str, Any]]:
        """Pending queue rows, oldest first (console `--list` view)."""
        return self._store.list_queued_sends(status="pending")

    # -- internals --------------------------------------------------------------

    def _build_context(
        self, chat_id: str, text: str, intent: str | None, confidence: float | None, now: int
    ) -> SendContext:
        chat = self._store.get_chat(chat_id)
        is_group = bool(chat["is_group"]) if chat and chat.get("is_group") is not None else False
        # "Brand-new contact" = zero message history with this chat (an
        # unknown chat row included). A first contact can never clear a
        # `never_when.first_message_from_contact` policy.
        is_first_contact = len(self._store.list_messages(chat_id, limit=1)) == 0
        auto_sends_last_hour = self._store.count_audit(
            "send", chat_id=chat_id, approved_by="auto-policy", since_ts=now - 3600
        )
        return SendContext(
            chat_id=chat_id,
            text=text,
            is_group=is_group,
            is_first_contact=is_first_contact,
            intent=intent,
            confidence=confidence,
            now=now,
            auto_sends_last_hour=auto_sends_last_hour,
        )

    def _enqueue(self, chat_id: str, text: str, reason: str, now: int) -> GateResult:
        queue_id = self._store.enqueue_send(chat_id, text, reason=reason, ts=now)
        self._store.append_audit(
            "send_queued",
            chat_id,
            {"text": text, "reason": reason, "queue_id": queue_id},
            approved_by=None,
            ts=now,
        )
        logger.info("gate.queued", extra={"chat_id": chat_id, "queue_id": queue_id})
        return GateResult(status="queued", reason=reason, queue_id=queue_id)

    def _deny(
        self, chat_id: str, text: str, reason: str, now: int, queue_id: int | None = None
    ) -> GateResult:
        payload: dict[str, Any] = {"text": text, "reason": reason}
        if queue_id is not None:
            payload["queue_id"] = queue_id
        self._store.append_audit("send_denied", chat_id, payload, approved_by=None, ts=now)
        logger.warning("gate.denied", extra={"chat_id": chat_id, "reason": reason})
        return GateResult(status="denied", reason=reason, queue_id=queue_id)
