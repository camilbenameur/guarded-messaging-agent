"""Tests for `ingest.stt.WhisperSTT` — the real faster-whisper STT hook.

Exercises actual transcription against the committed OGG fixtures + their
ground-truth `.txt` files (`tests/fixtures/audio/`), plus the end-to-end
headline path: ingest scenario -> `Worker.process_pending()` -> search finds
a message whose only text anywhere comes from a transcribed voice note.

Model-dependent tests share a module-scoped `loaded_stt` fixture that loads
the `WHISPER_MODEL` (default `small`, see `.env.example`) once — real
network download on first run, cached after under `HF_HOME=/models`
(`ingest/Dockerfile`, `./data/models` volume in `docker-compose.yml`). If
the model genuinely can't be loaded (e.g. no network in a CI sandbox), the
whole module is skipped rather than failing — but this
must not happen silently on a dev machine with network access; these tests
are expected to actually pass there.

Robustness tests (empty/corrupt audio) use a plain, unloaded `WhisperSTT` —
they exercise the early-return failure paths that never touch the model, so
they must run regardless of model availability.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from shared.fake_bridge import FakeBridge
from shared.store import Store

from ingest.stt import FAILURE_TEXT, WhisperSTT
from ingest.worker import Worker

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
AUDIO_DIR = FIXTURES_DIR / "audio"
SCENARIO_PATH = FIXTURES_DIR / "scenario.json"


def _normalize(text: str) -> str:
    """lowercase + strip accents, for fuzzy substring checks against real
    ASR output (normalize case/accents; fuzzy — assert key
    tokens, not exact strings).
    """
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


_WORD_RE = re.compile(r"\w+")


@pytest.fixture()
def stt_instance() -> WhisperSTT:
    """An unloaded `WhisperSTT` — for the failure paths that return before
    ever touching ffmpeg/the model, so these tests run even if the model
    can't be downloaded.
    """
    return WhisperSTT()


@pytest.fixture(scope="module")
def loaded_stt() -> WhisperSTT:
    client = WhisperSTT()
    try:
        client._load_model()
    except Exception as exc:  # pragma: no cover - environment-dependent (no network/CI)
        pytest.skip(f"faster-whisper model {client.model_name!r} unavailable: {exc}")
    return client


# -- real transcription quality, against committed fixtures + ground truth -----


def test_transcribes_french_rdv_clip_with_key_tokens(loaded_stt: WhisperSTT) -> None:
    """Ground truth (`fr_meeting.txt`): "On se voit demain à 15h pour le
    rendez-vous." — the headline "rdv demain 15h" clip.
    """
    audio_bytes = (AUDIO_DIR / "fr_meeting.ogg").read_bytes()
    result = loaded_stt.transcribe(audio_bytes, {})

    assert result.text != FAILURE_TEXT
    normalized = _normalize(result.text)
    assert "demain" in normalized
    assert "15" in normalized
    assert result.language == "fr"


def test_transcribes_french_dossier_clip(loaded_stt: WhisperSTT) -> None:
    """Ground truth (`fr_dossier.txt`): "Le dossier a été envoyé au client
    ce matin."
    """
    audio_bytes = (AUDIO_DIR / "fr_dossier.ogg").read_bytes()
    result = loaded_stt.transcribe(audio_bytes, {})

    assert result.text != FAILURE_TEXT
    assert "dossier" in _normalize(result.text)
    assert result.language == "fr"


def test_transcribes_english_report_clip(loaded_stt: WhisperSTT) -> None:
    """Ground truth (`en_report.txt`): "Can you send me the report before
    Friday morning?"
    """
    audio_bytes = (AUDIO_DIR / "en_report.ogg").read_bytes()
    result = loaded_stt.transcribe(audio_bytes, {})

    assert result.text != FAILURE_TEXT
    assert "report" in _normalize(result.text)
    assert result.language == "en"


def test_transcribes_english_meeting_clip_with_duration_and_confidence(
    loaded_stt: WhisperSTT,
) -> None:
    """Ground truth (`en_meeting.txt`): "The meeting has been moved to
    three PM." Also covers the `(text, language, duration, confidence)`
    return shape end to end.
    """
    audio_bytes = (AUDIO_DIR / "en_meeting.ogg").read_bytes()
    result = loaded_stt.transcribe(audio_bytes, {})

    assert result.text != FAILURE_TEXT
    assert "meeting" in _normalize(result.text)
    assert result.language == "en"
    assert result.duration is not None
    assert result.duration > 0
    assert result.confidence is not None
    assert 0.0 < result.confidence <= 1.0


# -- robustness: empty/corrupt audio never poisons the loop --------------------


def test_empty_audio_returns_failure_sentinel(stt_instance: WhisperSTT) -> None:
    result = stt_instance.transcribe(b"", {})
    assert result.text == FAILURE_TEXT
    assert result.confidence == 0.0


def test_corrupt_audio_returns_failure_sentinel(stt_instance: WhisperSTT) -> None:
    garbage = b"not a real audio container, just garbage bytes" * 20
    result = stt_instance.transcribe(garbage, {})
    assert result.text == FAILURE_TEXT
    assert result.confidence == 0.0


# -- end-to-end headline test: ingest -> process_pending -> search -------------


def test_headline_ingest_process_pending_search_finds_voice_note(
    loaded_stt: WhisperSTT, tmp_path: Path
) -> None:
    """The differentiator: a keyword spoken *only* in a voice note (never
    typed as text anywhere in `scenario.json`) is searchable once the
    derivation pass runs. This is the headline demo test: searching for
    a keyword spoken only in a voice note returns that message.
    """
    store = Store(tmp_path / "messages.db")
    bridge = FakeBridge(SCENARIO_PATH)
    media_dir = tmp_path / "media"
    worker = Worker(bridge=bridge, store=store, stt=loaded_stt, media_dir=media_dir)

    worker.run_once()
    updated = worker.process_pending(modality="audio")
    assert updated > 0

    # Sanity: "rendez" (from "rendez-vous", fr_meeting.ogg / evt-002) never
    # appears in any scenario text message — only in the transcribed audio.
    for event in bridge.events:
        if event.text:
            assert "rendez" not in _normalize(event.text)

    results = store.search("rendez")
    assert any(r["id"] == "evt-002" for r in results)

    msg = store.get_message("evt-002")
    assert msg is not None
    assert msg["processed"] == 1
    assert msg["content_text"] != "[voice note]"
    assert "demain" in _normalize(msg["content_text"])
    assert msg["media_meta"]["kind"] == "audio"
    assert msg["media_meta"]["language"] == "fr"
    assert isinstance(msg["media_meta"]["confidence"], float)
    assert isinstance(msg["media_meta"]["duration"], float)
    # Original transport media_meta (from scenario.json) survives alongside
    # the derived STT fields.
    assert msg["media_meta"]["mime"] == "audio/ogg"
