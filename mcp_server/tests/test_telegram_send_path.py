"""Send gate with the real TelethonBridge over a fake Telethon client (no network)."""

from __future__ import annotations

import pytest
from shared.fake_telethon import FakeChat, FakeMessage, FakeTelegramClient
from shared.store import Message, Store
from shared.telethon_bridge import TelethonBridge

from mcp_server.gate import SendGate

BASE_TS = 1_751_871_600


class _NoSleep:
    async def __call__(self, seconds: float) -> None:
        pass


@pytest.fixture
def client() -> FakeTelegramClient:
    return FakeTelegramClient([FakeChat(42, [FakeMessage(1, BASE_TS, "hi", sender_id=42)])])


@pytest.fixture
def bridge(client: FakeTelegramClient) -> TelethonBridge:
    return TelethonBridge(1, "h", "x", client_factory=lambda: client, sleep=_NoSleep())


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "m.db")
    s.upsert_chat("42", name="Alice", is_group=False)
    s.upsert_message(
        Message(
            id="m-1",
            chat_id="42",
            sender_id="42",
            ts=BASE_TS,
            direction="in",
            modality="text",
            content_text="hello",
            processed=1,
        )
    )
    return s


def auto_policy() -> dict:
    return {
        "mode": "auto",
        "auto_reply": {"enabled": True, "allow_when": {}, "never_when": {}},
        "rate_limit": {"max_auto_per_hour": 10},
    }


def make_gate(store, bridge) -> SendGate:
    return SendGate(store, bridge=bridge, clock=lambda: float(BASE_TS + 1000))


def test_queued_is_not_sent_then_human_approval_sends(store, bridge, client):
    gate = make_gate(store, bridge)  # no policy: manual -> queue
    queued = gate.request_send("42", "hello")
    assert queued.status == "queued" and client.sent == [] and client.calls == []
    released = gate.approve(queued.queue_id)
    assert released.status == "sent" and client.sent == [(42, "hello")]
    assert client.forbidden_calls == []


def test_approved_auto_policy_sends_through_telegram(store, bridge, client):
    store.set_policy("42", auto_policy())
    res = make_gate(store, bridge).request_send("42", "ok merci")
    assert res.status == "sent" and client.sent == [(42, "ok merci")]
    assert res.message_id.startswith("tg.")


def test_rejected_never_reaches_telegram(store, bridge, client):
    gate = make_gate(store, bridge)
    q = gate.request_send("42", "nope")
    assert gate.reject(q.queue_id).status == "rejected"
    assert client.sent == [] and client.calls == []


def test_kill_switch_blocks_the_bridge(store, bridge, client, monkeypatch):
    monkeypatch.setenv("SEND_KILL_SWITCH", "1")
    store.set_policy("42", auto_policy())
    res = make_gate(store, bridge).request_send("42", "x")
    assert res.status == "denied" and client.sent == [] and client.calls == []


def test_flood_wait_too_long_leaves_the_row_pending_and_retryable(store, client):
    client.flood = {"send_message": [5000]}
    bridge = TelethonBridge(1, "h", "x", client_factory=lambda: client, sleep=_NoSleep())
    gate = make_gate(store, bridge)
    queued = gate.request_send("42", "hello")
    res = gate.approve(queued.queue_id)
    assert res.status == "denied" and "BridgeRateLimited" in res.reason
    assert store.get_queued_send(queued.queue_id)["status"] == "pending"
    assert client.sent == []
    client.flood = {}
    assert gate.approve(queued.queue_id).status == "sent"
    assert client.sent == [(42, "hello")]


def test_auto_send_bridge_failure_falls_back_to_queue(store, client):
    client.flood = {"send_message": [5000]}
    bridge = TelethonBridge(1, "h", "x", client_factory=lambda: client, sleep=_NoSleep())
    store.set_policy("42", auto_policy())
    res = make_gate(store, bridge).request_send("42", "ok")
    assert res.status == "queued" and client.sent == []
