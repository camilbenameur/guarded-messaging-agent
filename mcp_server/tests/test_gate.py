"""Unit tests for `mcp_server.gate.SendGate` mechanics: queue lifecycle
(enqueue -> approve/reject), fail-closed behavior without a bridge, and the
audit trail every branch writes. Adversarial coverage (injection, kill
switch, rate limits, ...) lives in `test_redteam.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from shared.fake_bridge import FakeBridge
from shared.store import Message, Store

from mcp_server.gate import SendGate

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SCENARIO_PATH = FIXTURES_DIR / "scenario.json"

BASE_TS = 1_751_871_600


def auto_policy() -> dict[str, Any]:
    return {
        "mode": "auto_validé",
        "auto_reply": {"enabled": True, "allow_when": {}, "never_when": {}},
        "rate_limit": {"max_auto_per_hour": 10},
    }


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "messages.db")
    s.upsert_chat("chat-1", name="Alice", is_group=False)
    s.upsert_message(
        Message(
            id="m-1",
            chat_id="chat-1",
            sender_id="alice",
            ts=BASE_TS,
            direction="in",
            modality="text",
            content_text="hello",
            processed=1,
        )
    )
    return s


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge(SCENARIO_PATH)


@pytest.fixture
def gate(store: Store, bridge: FakeBridge) -> SendGate:
    return SendGate(store, bridge=bridge, clock=lambda: float(BASE_TS + 1000))


def test_queue_then_approve_reaches_bridge_once(gate: SendGate, bridge: FakeBridge) -> None:
    queued = gate.request_send("chat-1", "hi alice")  # no policy -> queue
    assert queued.status == "queued" and queued.queue_id is not None
    assert bridge.outbox == []

    pending = gate.list_pending()
    assert [row["id"] for row in pending] == [queued.queue_id]
    assert pending[0]["text"] == "hi alice"

    released = gate.approve(queued.queue_id)
    assert released.status == "sent"
    assert [entry["text"] for entry in bridge.outbox] == ["hi alice"]
    assert gate.list_pending() == []


def test_reject_discards_without_sending(
    gate: SendGate, store: Store, bridge: FakeBridge
) -> None:
    queued = gate.request_send("chat-1", "never mind")
    assert queued.queue_id is not None

    rejected = gate.reject(queued.queue_id)
    assert rejected.status == "rejected"
    assert bridge.outbox == []
    row = store.get_queued_send(queued.queue_id)
    assert row is not None and row["status"] == "rejected"
    audit = store.list_audit(chat_id="chat-1")
    assert any(r["action"] == "send_rejected" for r in audit)


def test_approve_unknown_and_already_resolved_ids_denied(
    gate: SendGate, bridge: FakeBridge
) -> None:
    assert gate.approve(9999).status == "denied"

    queued = gate.request_send("chat-1", "double release?")
    assert queued.queue_id is not None
    assert gate.approve(queued.queue_id).status == "sent"
    second = gate.approve(queued.queue_id)  # a row can only resolve once
    assert second.status == "denied"
    assert "already resolved" in second.reason
    assert len(bridge.outbox) == 1  # not sent twice


def test_no_bridge_fails_closed_auto_pass_queues_and_approve_refuses(
    store: Store,
) -> None:
    gate = SendGate(store, bridge=None, clock=lambda: float(BASE_TS + 1000))
    store.set_policy("chat-1", auto_policy())

    result = gate.request_send("chat-1", "auto without a bridge")
    assert result.status == "queued"  # allow downgraded: nothing to send through
    assert "no bridge" in result.reason
    assert result.queue_id is not None

    released = gate.approve(result.queue_id)
    assert released.status == "denied"
    assert "no bridge" in released.reason
    row = store.get_queued_send(result.queue_id)
    assert row is not None and row["status"] == "pending"  # still releasable later


def test_every_branch_writes_an_audit_row(gate: SendGate, store: Store) -> None:
    queued = gate.request_send("chat-1", "audited?")
    assert queued.queue_id is not None
    gate.approve(queued.queue_id)

    audit = store.list_audit(chat_id="chat-1")
    actions = [row["action"] for row in audit]
    assert "send_queued" in actions
    assert "send" in actions
    send_row = next(row for row in audit if row["action"] == "send")
    assert send_row["approved_by"] == "human"
    assert send_row["payload"]["queue_id"] == queued.queue_id
    queued_row = next(row for row in audit if row["action"] == "send_queued")
    assert queued_row["approved_by"] is None
    assert queued_row["payload"]["reason"]
