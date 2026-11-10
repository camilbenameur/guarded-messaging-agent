"""Tests for `ingest.worker.Worker` — poll/route/persist loop, the
derivation pass, cursor resume, and reprocess/backfill. Exercised against a
`FakeBridge` replaying the committed scenario fixture and a real
`Store` rooted at tmp dirs — no network, no real STT/vision.

Fixture layout mirrors `shared/tests/test_fake_bridge.py`:
`tests/fixtures/scenario.json` + `tests/fixtures/{audio,images}/`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from shared.fake_bridge import FakeBridge
from shared.store import Store

from ingest.worker import Caption, Transcript, Worker

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SCENARIO_PATH = FIXTURES_DIR / "scenario.json"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "messages.db")


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge(SCENARIO_PATH)


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    d = tmp_path / "media"
    d.mkdir()
    return d


def _worker(bridge: FakeBridge, store: Store, media_dir: Path, **kwargs: object) -> Worker:
    return Worker(bridge=bridge, store=store, media_dir=media_dir, **kwargs)  # type: ignore[arg-type]


class _FakeStt:
    """Deterministic stand-in for the real faster-whisper client."""

    def transcribe(self, audio_bytes: bytes, media_meta: dict) -> Transcript:
        return Transcript(
            text=f"transcript of {len(audio_bytes)} bytes", language="fr", confidence=0.9
        )


class _FakeVision:
    """Deterministic stand-in for the real Claude vision client."""

    def caption(self, image_bytes: bytes, context: str) -> Caption:
        return Caption(text="a description", ocr="OCR TEXT", confidence=0.8)


# -- routing ------------------------------------------------------------------


def test_run_once_ingests_full_scenario(bridge: FakeBridge, store: Store, media_dir: Path) -> None:
    worker = _worker(bridge, store, media_dir)
    count = worker.run_once()
    assert count == len(bridge.events) == 13


def test_dod_no_blank_content_text_after_full_ingest(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    """Core invariant: after ingesting the full
    fixture scenario, no row has an empty/NULL content_text.
    """
    worker = _worker(bridge, store, media_dir)
    worker.run_once()

    conn = sqlite3.connect(store.db_path)
    try:
        (count,) = conn.execute(
            "SELECT count(*) FROM messages WHERE content_text = '' OR content_text IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert count == 0


def test_text_messages_processed_immediately_and_searchable(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()

    msg = store.get_message("evt-001")
    assert msg is not None
    assert msg["modality"] == "text"
    assert msg["content_text"] == "Salut ! Tu es dispo cette semaine ?"
    assert msg["processed"] == 1
    assert store.search("dispo") != []


def test_audio_and_image_persisted_with_placeholder_and_pending(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()

    audio_msg = store.get_message("evt-002")
    assert audio_msg is not None
    assert audio_msg["content_text"] == "[voice note]"
    assert audio_msg["processed"] == 0
    assert audio_msg["media_path"] is not None
    assert Path(audio_msg["media_path"]).read_bytes() == (
        FIXTURES_DIR / "audio" / "fr_meeting.ogg"
    ).read_bytes()

    image_msg = store.get_message("evt-004")
    assert image_msg is not None
    assert image_msg["content_text"] == "[image]"
    assert image_msg["processed"] == 0
    assert Path(image_msg["media_path"]).read_bytes() == (
        FIXTURES_DIR / "images" / "text_rdv.png"
    ).read_bytes()


def test_media_saved_to_media_dir_keyed_by_event_id(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()
    assert (media_dir / "evt-002").exists()
    assert (media_dir / "evt-004").exists()


def test_chat_upsert_for_every_chat_in_scenario(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()

    chat_ids = {c["chat_id"] for c in store.list_chats()}
    assert chat_ids == {"chat_alice", "chat_bob", "chat_team_ops"}


def test_doc_and_video_routing(tmp_path: Path, store: Store) -> None:
    scenario = [
        {
            "id": "evt-doc-1",
            "chat_id": "chat_x",
            "sender_id": "x",
            "ts": 1_700_000_000,
            "direction": "in",
            "modality": "doc",
            "text": None,
            "media_ref": None,
            "media_meta": {"filename": "rapport.pdf"},
        },
        {
            "id": "evt-video-1",
            "chat_id": "chat_x",
            "sender_id": "x",
            "ts": 1_700_000_060,
            "direction": "in",
            "modality": "video",
            "text": None,
            "media_ref": "clip.mp4",
            "media_meta": {},
        },
    ]
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    doc_bridge = FakeBridge(scenario_path)
    media_dir = tmp_path / "media"

    worker = Worker(bridge=doc_bridge, store=store, media_dir=media_dir)
    worker.run_once()

    doc_msg = store.get_message("evt-doc-1")
    assert doc_msg is not None
    assert doc_msg["content_text"] == "[attachment: rapport.pdf]"
    assert doc_msg["processed"] == 1

    video_msg = store.get_message("evt-video-1")
    assert video_msg is not None
    assert video_msg["content_text"] == "[attachment: clip.mp4]"
    assert video_msg["processed"] == 1


# -- cursor resume / idempotent re-ingest --------------------------------------


def test_run_once_twice_does_not_duplicate(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    first_count = worker.run_once()
    second_count = worker.run_once()

    assert first_count == 13
    assert second_count == 0

    conn = sqlite3.connect(store.db_path)
    try:
        (total,) = conn.execute("SELECT count(*) FROM messages").fetchone()
    finally:
        conn.close()
    assert total == 13


def test_cursor_value_is_last_event_id(bridge: FakeBridge, store: Store, media_dir: Path) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()
    assert store.get_cursor(worker.cursor_key) == bridge.events[-1].id


def test_cursor_persists_across_worker_restarts(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker1 = _worker(bridge, store, media_dir)
    worker1.run_once()

    # Fresh Worker instance (simulating a process restart), same store/bridge.
    worker2 = _worker(bridge, store, media_dir)
    count = worker2.run_once()
    assert count == 0


def test_ingest_event_is_idempotent_for_duplicate_event_id(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    event = bridge.events[0]
    worker._ingest_event(event)
    worker._ingest_event(event)  # simulate replaying an already-seen event

    rows = store.list_messages(event.chat_id, limit=50)
    assert len([m for m in rows if m["id"] == event.id]) == 1


# -- run_forever ----------------------------------------------------------------


def test_run_forever_polls_and_sleeps_in_a_loop(
    bridge: FakeBridge, store: Store, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(bridge, store, media_dir)
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("ingest.worker.time.sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        worker.run_forever(interval=0.01)

    assert sleeps == [0.01, 0.01]


# -- derivation pass (process_pending) -------------------------------------------


def test_process_pending_leaves_rows_pending_without_hooks(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir)
    worker.run_once()

    updated = worker.process_pending()
    assert updated == 0

    audio_msg = store.get_message("evt-002")
    assert audio_msg is not None
    assert audio_msg["processed"] == 0
    assert audio_msg["content_text"] == "[voice note]"


def test_process_pending_applies_configured_hooks_and_marks_processed(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir, stt=_FakeStt(), vision=_FakeVision())
    worker.run_once()

    audio_count = sum(1 for e in bridge.events if e.modality == "audio")
    image_count = sum(1 for e in bridge.events if e.modality == "image")

    updated = worker.process_pending()
    assert updated == audio_count + image_count

    audio_msg = store.get_message("evt-002")
    assert audio_msg is not None
    assert audio_msg["processed"] == 1
    assert audio_msg["content_text"].startswith("transcript of")
    assert audio_msg["media_meta"]["language"] == "fr"
    assert audio_msg["media_meta"]["confidence"] == 0.9
    # Original transport media_meta is preserved alongside derived fields.
    assert audio_msg["media_meta"]["mime"] == "audio/ogg"

    image_msg = store.get_message("evt-004")
    assert image_msg is not None
    assert image_msg["processed"] == 1
    assert image_msg["content_text"] == "[image] a description"
    assert image_msg["media_meta"]["ocr"] == "OCR TEXT"

    # Transcribed/captioned content must be searchable, per the content_text
    # contract (FTS sync trigger fires on UPDATE OF content_text).
    assert store.search("transcript") != []
    assert store.search("description") != []


def test_process_pending_is_idempotent(bridge: FakeBridge, store: Store, media_dir: Path) -> None:
    worker = _worker(bridge, store, media_dir, stt=_FakeStt(), vision=_FakeVision())
    worker.run_once()

    first = worker.process_pending()
    second = worker.process_pending()

    assert first > 0
    assert second == 0


def test_process_pending_can_be_scoped_to_one_modality(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir, stt=_FakeStt(), vision=_FakeVision())
    worker.run_once()

    audio_count = sum(1 for e in bridge.events if e.modality == "audio")
    updated = worker.process_pending(modality="audio")
    assert updated == audio_count

    # Images remain pending: process_pending was scoped to audio only.
    image_msg = store.get_message("evt-004")
    assert image_msg is not None
    assert image_msg["processed"] == 0


# -- reprocess / backfill --------------------------------------------------------


def test_reprocess_resets_processed_flag_for_modality(
    bridge: FakeBridge, store: Store, media_dir: Path
) -> None:
    worker = _worker(bridge, store, media_dir, stt=_FakeStt(), vision=_FakeVision())
    worker.run_once()
    worker.process_pending()

    audio_count = sum(1 for e in bridge.events if e.modality == "audio")
    reset_count = worker.reprocess("audio")
    assert reset_count == audio_count

    audio_msg = store.get_message("evt-002")
    assert audio_msg is not None
    assert audio_msg["processed"] == 0

    # Image rows are untouched by a reprocess scoped to "audio".
    image_msg = store.get_message("evt-004")
    assert image_msg is not None
    assert image_msg["processed"] == 1

    # Re-running process_pending recomputes the reset modality's content.
    updated = worker.process_pending()
    assert updated == audio_count
    assert store.get_message("evt-002")["processed"] == 1
