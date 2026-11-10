"""Tests for `FakeBridge` against the committed scenario fixture.

Fixture layout: `tests/fixtures/scenario.json` + `tests/fixtures/{audio,images}/`.
Path resolution below mirrors the repo-root layout both on the host and
inside the `ingest` container, where `shared/`, `ingest/`, and `tests/` are
copied as siblings under `/app` (see `ingest/Dockerfile`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.bridge import BridgeEvent
from shared.fake_bridge import FakeBridge, UnknownCursorError

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SCENARIO_PATH = FIXTURES_DIR / "scenario.json"


def _make_bridge() -> FakeBridge:
    return FakeBridge(SCENARIO_PATH)


def test_scenario_fixture_exists() -> None:
    assert SCENARIO_PATH.exists(), f"missing {SCENARIO_PATH}"


def test_poll_replays_full_scenario_in_order() -> None:
    bridge = _make_bridge()
    events = bridge.poll(after=None)

    assert len(events) >= 10  # mixes text/audio/image across several chats
    assert all(isinstance(event, BridgeEvent) for event in events)
    # Order must match the committed scenario file, never be re-sorted.
    assert [event.id for event in events] == sorted(event.id for event in events)
    assert len({event.chat_id for event in events}) >= 2, "scenario should span multiple chats"
    assert {"text", "audio", "image"} <= {event.modality for event in events}


def test_poll_after_resumes_incrementally() -> None:
    bridge = _make_bridge()
    full = bridge.poll(after=None)
    midpoint = len(full) // 2

    first_batch = full[: midpoint + 1]
    second_batch = bridge.poll(after=first_batch[-1].id)

    assert first_batch + second_batch == full
    assert [event.id for event in second_batch] == [event.id for event in full[midpoint + 1 :]]


def test_poll_after_last_event_returns_empty() -> None:
    bridge = _make_bridge()
    full = bridge.poll(after=None)
    assert bridge.poll(after=full[-1].id) == []


def test_poll_unknown_cursor_raises() -> None:
    bridge = _make_bridge()
    with pytest.raises(UnknownCursorError):
        bridge.poll(after="does-not-exist")


def test_download_media_matches_fixture_bytes() -> None:
    bridge = _make_bridge()
    events_with_media = [event for event in bridge.poll(after=None) if event.media_ref]
    assert events_with_media, "scenario should include at least one media event"

    for event in events_with_media:
        expected = (FIXTURES_DIR / event.media_ref).read_bytes()
        actual = bridge.download_media(event.media_ref)
        assert actual == expected
        assert len(actual) > 0


def test_scenario_covers_audio_and_image_media() -> None:
    bridge = _make_bridge()
    events = bridge.poll(after=None)
    audio_refs = [event.media_ref for event in events if event.modality == "audio"]
    image_refs = [event.media_ref for event in events if event.modality == "image"]

    assert audio_refs and all(ref.endswith(".ogg") for ref in audio_refs)
    assert image_refs and all(ref.endswith(".png") for ref in image_refs)


def test_send_text_appends_to_outbox_and_returns_id() -> None:
    bridge = _make_bridge()
    assert bridge.outbox == []

    message_id = bridge.send_text("chat_alice", "hello from the agent")

    assert bridge.outbox == [
        {"id": message_id, "chat_id": "chat_alice", "text": "hello from the agent"}
    ]

    second_id = bridge.send_text("chat_bob", "another message")
    assert second_id != message_id
    assert len(bridge.outbox) == 2


def test_bridge_event_is_frozen() -> None:
    event = BridgeEvent(
        id="x",
        chat_id="c",
        sender_id="s",
        ts=0,
        direction="in",
        modality="text",
        text="hi",
        media_ref=None,
        media_meta={},
    )
    with pytest.raises(AttributeError):
        event.text = "changed"  # type: ignore[misc]
