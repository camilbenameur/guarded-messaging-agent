"""TelethonBridge against a fake Telethon client. No network, no credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.bridge import BridgeEvent
from shared.fake_bridge import UnknownCursorError
from shared.fake_telethon import FakeChat, FakeMessage, FakeTelegramClient, file_info
from shared.telethon_bridge import (
    BridgeAuthError,
    BridgeRateLimited,
    TelethonBridge,
    make_event_id,
    parse_event_id,
)

NOW = 1_800_000_000
T0 = NOW - 3600


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_bridge(client: FakeTelegramClient, **kw) -> tuple[TelethonBridge, Sleeps]:
    sleeps = Sleeps()
    bridge = TelethonBridge(
        1,
        "hash",
        "unused.session",
        client_factory=lambda: client,
        sleep=sleeps,
        clock=lambda: NOW,
        **kw,
    )
    return bridge, sleeps


def scenario_chats() -> list[FakeChat]:
    alice = FakeChat(
        42,
        [
            FakeMessage(1, T0, "Salut !", sender_id=42),
            FakeMessage(
                2,
                T0 + 60,
                voice=object(),
                sender_id=42,
                file=file_info(mime_type="audio/ogg", duration=3, size=1234),
            ),
            FakeMessage(3, T0 + 120, "Parfait", out=True),
            FakeMessage(
                4,
                T0 + 180,
                "voici",
                photo=object(),
                sender_id=42,
                file=file_info(mime_type="image/jpeg", width=560, height=320, size=99),
            ),
            FakeMessage(5, T0 + 190, action=object()),  # service message: skipped
            FakeMessage(6, T0 + 200),  # empty / unsupported: skipped
        ],
    )
    group = FakeChat(
        -1001, [FakeMessage(1, T0 + 30, "hello group", sender_id=7)], is_user=False, is_group=True
    )
    channel = FakeChat(-1002, [FakeMessage(1, T0 + 40, "news")], is_user=False, is_channel=True)
    return [alice, group, channel]


def test_event_mapping_text_voice_photo():
    client = FakeTelegramClient(scenario_chats())
    bridge, _ = make_bridge(client)
    events = bridge.poll(None)

    assert all(isinstance(e, BridgeEvent) for e in events)
    assert [e.id for e in events] == [
        make_event_id(T0, "42", 1),
        make_event_id(T0 + 30, "-1001", 1),
        make_event_id(T0 + 60, "42", 2),
        make_event_id(T0 + 120, "42", 3),
        make_event_id(T0 + 180, "42", 4),
    ]  # broadcast channel, service and empty messages are skipped
    text, grp, voice, out, photo = events

    assert (text.modality, text.text, text.direction, text.sender_id, text.chat_id) == (
        "text",
        "Salut !",
        "in",
        "42",
        "42",
    )
    assert text.media_ref is None and text.media_meta == {}
    assert grp.chat_id == "-1001" and grp.sender_id == "7"

    assert voice.modality == "audio" and voice.text is None
    assert voice.media_ref == "42.2"
    assert voice.media_meta["mime"] == "audio/ogg" and voice.media_meta["duration_s"] == 3

    assert out.direction == "out" and out.sender_id == "me"

    assert photo.modality == "image" and photo.media_ref == "42.4"
    assert photo.media_meta["width"] == 560 and photo.media_meta["caption"] == "voici"


def test_mapping_matches_scenario_schema():
    scenario = json.loads((Path(__file__).parents[2] / "tests/fixtures/scenario.json").read_text())
    fields = set(scenario[0])
    ev = TelethonBridge(
        1,
        "h",
        "x",
        client_factory=lambda: FakeTelegramClient([FakeChat(1, [FakeMessage(1, T0, "a")])]),
        clock=lambda: NOW,
    ).poll(None)[0]
    assert set(ev.__dataclass_fields__) == fields
    assert {e["modality"] for e in scenario} >= {"text", "audio", "image"}


def test_other_media_become_doc_and_video():
    chat = FakeChat(
        5,
        [
            FakeMessage(
                1, T0, document=object(), file=file_info(name="a.pdf", mime_type="application/pdf")
            ),
            FakeMessage(2, T0 + 1, video=object(), file=file_info(duration=4)),
        ],
    )
    events = make_bridge(FakeTelegramClient([chat]))[0].poll(None)
    assert [e.modality for e in events] == ["doc", "video"]
    assert events[0].media_meta["filename"] == "a.pdf"


def test_include_channels_flag():
    client = FakeTelegramClient(scenario_chats())
    events = make_bridge(client, include_channels=True)[0].poll(None)
    assert "-1002" in {e.chat_id for e in events}


def test_first_poll_backfill_window():
    chat = FakeChat(
        1, [FakeMessage(1, NOW - 30 * 86400, "old"), FakeMessage(2, NOW - 3600, "recent")]
    )
    events = make_bridge(FakeTelegramClient([chat]), backfill_days=7)[0].poll(None)
    assert [e.text for e in events] == ["recent"]


def test_cursor_resume_returns_only_newer_events():
    client = FakeTelegramClient(scenario_chats())
    bridge, _ = make_bridge(client)
    first = bridge.poll(None)
    assert bridge.poll(first[-1].id) == []

    client.chats[42].messages.append(FakeMessage(7, T0 + 300, "later", sender_id=42))
    client.chats[-1001].messages.append(FakeMessage(2, T0 + 250, "later group", sender_id=7))
    again = bridge.poll(first[-1].id)
    assert [e.text for e in again] == ["later group", "later"]
    # resuming from the middle gives exactly the tail
    assert [e.id for e in bridge.poll(first[1].id)] == [e.id for e in first[2:] + again]


def test_same_second_messages_in_two_chats_are_not_lost():
    a = FakeChat(1, [FakeMessage(1, T0, "a")])
    b = FakeChat(2, [FakeMessage(1, T0, "b")])
    client = FakeTelegramClient([a, b])
    bridge, _ = make_bridge(client)
    events = bridge.poll(None)
    assert {e.text for e in events} == {"a", "b"}
    assert bridge.poll(events[0].id) == [events[1]]


def test_per_chat_limit_never_skips_a_chats_tail():
    busy = FakeChat(1, [FakeMessage(i, T0 + i, f"b{i}") for i in range(1, 11)])
    quiet = FakeChat(2, [FakeMessage(1, T0 + 100, "q")])
    bridge, _ = make_bridge(FakeTelegramClient([busy, quiet]), per_chat_limit=4)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        batch = bridge.poll(cursor)
        if not batch:
            break
        seen += [e.text for e in batch]
        cursor = batch[-1].id
    assert seen == [f"b{i}" for i in range(1, 11)] + ["q"]


def test_bad_cursor_raises():
    bridge, _ = make_bridge(FakeTelegramClient([]))
    with pytest.raises(UnknownCursorError):
        bridge.poll("evt-001")
    assert parse_event_id(make_event_id(5, "-100", 9)) == (5, "-100", 9)


def test_worker_cursor_persists_across_restart(tmp_path):
    ingest_worker = pytest.importorskip("ingest.worker")
    from shared.store import Store

    client = FakeTelegramClient(scenario_chats())
    db = tmp_path / "m.db"

    def new_worker():
        bridge, _ = make_bridge(client)
        return ingest_worker.Worker(bridge, Store(db), media_dir=tmp_path / "media")

    assert new_worker().run_once() == 5
    assert new_worker().run_once() == 0  # fresh Worker + Store: resumes from the saved cursor
    client.chats[42].messages.append(FakeMessage(8, T0 + 400, "new", sender_id=42))
    w = new_worker()
    assert w.run_once() == 1
    msgs = Store(db).list_messages("42", limit=50)
    assert (
        len(msgs) == 5
        and (tmp_path / "media" / make_event_id(T0 + 60, "42", 2)).read_bytes() == b""
    )


def test_download_media_returns_bytes_and_enforces_cap():
    voice = FakeMessage(2, T0, voice=object(), file=file_info(size=5), payload=b"OggS!")
    huge = FakeMessage(3, T0 + 1, voice=object(), file=file_info(size=10**9), payload=b"x")
    client = FakeTelegramClient([FakeChat(42, [voice, huge])])
    bridge, _ = make_bridge(client, max_media_bytes=1000)
    assert bridge.download_media("42.2") == b"OggS!"
    with pytest.raises(Exception, match="cap"):
        bridge.download_media("42.3")
    with pytest.raises(ValueError):
        bridge.download_media("../etc/passwd")


def test_flood_wait_backs_off_then_succeeds():
    client = FakeTelegramClient(
        scenario_chats(), flood={"iter_messages": [30, 5], "iter_dialogs": [2]}
    )
    bridge, sleeps = make_bridge(client)
    events = bridge.poll(None)
    assert len(events) == 5
    assert sleeps.calls == [3.0, 31.0, 6.0]  # Telegram's wait + 1s, each time


def test_flood_wait_gives_up_with_retry_after():
    client = FakeTelegramClient(scenario_chats(), flood={"iter_dialogs": [10, 10, 10, 10, 10]})
    bridge, sleeps = make_bridge(client, max_flood_retries=2)
    with pytest.raises(BridgeRateLimited) as ei:
        bridge.poll(None)
    assert ei.value.retry_after == 11.0 and len(sleeps.calls) == 2

    client = FakeTelegramClient(scenario_chats(), flood={"iter_dialogs": [5000]})
    bridge, sleeps = make_bridge(client)
    with pytest.raises(BridgeRateLimited):
        bridge.poll(None)
    assert sleeps.calls == []  # too long to block: report instead of sleeping


def test_flood_wait_on_send_retries_and_sends_once():
    client = FakeTelegramClient(
        [FakeChat(42, [FakeMessage(1, T0, "x")])], flood={"send_message": [4]}
    )
    bridge, sleeps = make_bridge(client)
    mid = bridge.send_text("42", "hi")
    assert client.sent == [(42, "hi")] and sleeps.calls == [5.0]
    assert parse_event_id(mid)[1:] == ("42", 9001)


def test_send_text_validates_input():
    client = FakeTelegramClient([FakeChat(42, [FakeMessage(1, T0, "x")])])
    bridge, _ = make_bridge(client)
    for chat, text in (("@someone", "hi"), ("chat_alice", "hi"), ("42", "  ")):
        with pytest.raises(ValueError):
            bridge.send_text(chat, text)
    assert client.sent == []


def test_nothing_marks_messages_read_and_only_allowed_calls_are_made():
    client = FakeTelegramClient(scenario_chats())
    client.chats[42].messages[1].payload = b"audio"
    bridge, _ = make_bridge(client)
    bridge.poll(None)
    bridge.download_media("42.2")
    bridge.send_text("42", "hello")
    assert client.forbidden_calls == []
    assert set(client.calls) <= {
        "connect",
        "disconnect",
        "is_user_authorized",
        "iter_dialogs",
        "iter_messages",
        "get_input_entity",
        "get_messages",
        "download_media",
        "send_message",
    }
    assert not any("read" in c or "typing" in c for c in client.calls)


def test_a_marking_call_would_be_detected():
    client = FakeTelegramClient([])
    with pytest.raises(AttributeError):
        client.send_read_acknowledge
    assert client.forbidden_calls == ["send_read_acknowledge"]


def test_connects_per_operation_and_disconnects():
    client = FakeTelegramClient(scenario_chats())
    bridge, _ = make_bridge(client)
    bridge.poll(None)
    assert not client.connected and client.connects == 1
    bridge.poll(None)
    assert client.connects == 2 and not client.connected


def test_unauthorized_session_fails_with_login_hint():
    bridge, _ = make_bridge(FakeTelegramClient([], authorized=False))
    with pytest.raises(BridgeAuthError, match="telegram_login"):
        bridge.poll(None)


def test_missing_session_file_fails_before_any_client_is_built(tmp_path):
    bridge = TelethonBridge(1, "h", tmp_path / "nope")
    with pytest.raises(BridgeAuthError, match="telegram_login"):
        bridge.poll(None)
    assert not (tmp_path / "nope.session").exists()


def test_works_when_called_from_a_running_event_loop():
    import asyncio

    client = FakeTelegramClient(scenario_chats())
    bridge, _ = make_bridge(client)

    async def inside() -> int:
        return len(bridge.poll(None))

    assert asyncio.run(inside()) == 5


def test_real_client_is_configured_safely_without_connecting(tmp_path):
    pytest.importorskip("telethon")
    bridge = TelethonBridge(1, "abc", tmp_path / "s")
    client = bridge._make_client()
    assert client.parse_mode is None
    assert client.flood_sleep_threshold == 0
    assert not client.is_connected()
