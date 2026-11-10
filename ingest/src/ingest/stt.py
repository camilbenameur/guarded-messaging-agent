"""Speech-to-text for voice notes: `WhisperSTT`, the real implementation of
`ingest.worker.SttHook` protocol. Runs `faster-whisper` (CTranslate2)
fully locally inside the `ingest` container — no audio ever leaves the
machine.

Pipeline: media bytes ->
ffmpeg subprocess normalizes to 16kHz mono PCM -> faster-whisper transcribes
the raw samples directly (no second decode) -> `Transcript(text, language,
confidence, duration)`.

Robustness: this module must *never* raise out of `transcribe()`. Empty
input, a corrupt/undecodable clip, a model load failure, or a transcription
that comes back empty (e.g. silence) all degrade to a single sentinel,
`FAILURE_TEXT`, so the ingestion worker's derivation pass always marks the
row `processed = 1` instead of poisoning `process_pending()` on a retry loop
(robustness requirement).
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import subprocess
import wave
from typing import TYPE_CHECKING, Any

from ingest.worker import Transcript

if TYPE_CHECKING:
    import numpy as np
    from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "small"
_ENV_MODEL = "WHISPER_MODEL"

#: Sentinel written to `content_text` for audio we could not transcribe at
#: all (empty bytes, ffmpeg can't decode it, or the model errors out).
FAILURE_TEXT = "[voice note: transcription failed]"

_FAILURE = Transcript(text=FAILURE_TEXT, language=None, confidence=0.0, duration=0.0)


def _pick_device() -> tuple[str, str]:
    """`(device, compute_type)`: CUDA + float16 if a GPU is visible to
    CTranslate2, else CPU + int8 (CPU int8 is the default, CUDA
    if available). The CUDA probe is best-effort — any failure (no GPU, no
    CUDA runtime in the image, etc.) just falls back to CPU.
    """
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:  # pragma: no cover - environment-dependent, not exercised in CI
        pass
    return "cpu", "int8"


def _ffmpeg_to_pcm16k_mono(audio_bytes: bytes) -> bytes:
    """Run the ffmpeg subprocess step: arbitrary input audio
    (Telegram voice notes are OGG/Opus) -> 16kHz mono 16-bit PCM WAV bytes.
    ffmpeg is installed in the `ingest` image (see `ingest/Dockerfile`).
    Raises `OSError`/`subprocess.CalledProcessError` on anything ffmpeg
    can't decode — callers turn that into the failure sentinel.
    """
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "pipe:0",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            "pipe:1",
        ],
        input=audio_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _pcm_wav_to_float32(wav_bytes: bytes) -> np.ndarray:
    """Decode the mono 16-bit PCM WAV produced by `_ffmpeg_to_pcm16k_mono`
    into the `float32` array shape faster-whisper expects for an in-memory
    `audio` argument (samples in `[-1.0, 1.0]`). No second ffmpeg/av decode
    needed — we already normalized the format ourselves.
    """
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        sampwidth = wf.getsampwidth()
    if sampwidth != 2:  # pragma: no cover - ffmpeg -f wav always emits 16-bit PCM here
        raise ValueError(f"unexpected WAV sample width: {sampwidth}")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return audio


def _confidence_from_segments(segments: list[Any]) -> float:
    """faster-whisper doesn't expose one overall confidence score, only a
    per-segment `avg_logprob` (mean log-probability of the segment's
    tokens). `exp(avg_logprob)` maps that back into a `(0, 1]` pseudo-
    probability; averaging across segments gives a reasonable single
    transcript-level `confidence` for `media_meta`.
    """
    probs = [math.exp(seg.avg_logprob) for seg in segments if seg.avg_logprob is not None]
    if not probs:
        return 0.0
    return max(0.0, min(1.0, sum(probs) / len(probs)))


class WhisperSTT:
    """`faster-whisper`-backed implementation of `ingest.worker.SttHook`.

    Model name comes from the `WHISPER_MODEL` env var (default `"small"`,
    by design), overridable via the constructor for tests. The
    CTranslate2 model is lazy-loaded on first `transcribe()` call, once per
    process/instance — constructing a `WhisperSTT` (or a `Worker` with one
    configured) never pays the load cost or requires model files to already
    be present.

    Model weights are downloaded from Hugging Face Hub on first use and
    cached under the `huggingface_hub` cache dir (`HF_HOME`, set to `/models`
    in `ingest/Dockerfile`, backed by the `./data/models` volume in
    `docker-compose.yml`) so a container restart doesn't re-download them.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.environ.get(_ENV_MODEL, DEFAULT_MODEL)
        self._model: WhisperModel | None = None

    def _load_model(self) -> WhisperModel:
        if self._model is None:
            from faster_whisper import WhisperModel

            device, compute_type = _pick_device()
            logger.info(
                "stt.load_model",
                extra={"model": self.model_name, "device": device, "compute_type": compute_type},
            )
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
        return self._model

    def transcribe(self, audio_bytes: bytes, media_meta: dict[str, Any]) -> Transcript:
        """media bytes -> `Transcript(text, language, confidence, duration)`.
        Never raises: any failure along the way (empty input, undecodable
        audio, model error, or a transcript that comes back empty) returns
        the `FAILURE_TEXT` sentinel instead.
        """
        if not audio_bytes:
            logger.warning("stt.empty_audio")
            return _FAILURE

        try:
            wav_bytes = _ffmpeg_to_pcm16k_mono(audio_bytes)
            audio = _pcm_wav_to_float32(wav_bytes)
        except (OSError, subprocess.CalledProcessError, ValueError, wave.Error) as exc:
            logger.warning("stt.normalize_failed", extra={"error": str(exc)})
            return _FAILURE

        if audio.size == 0:
            logger.warning("stt.empty_normalized_audio")
            return _FAILURE

        try:
            model = self._load_model()
            segments_iter, info = model.transcribe(audio, vad_filter=True)
            segments = list(segments_iter)
        except Exception as exc:  # faster-whisper/ctranslate2 failure: degrade, don't poison loop
            logger.warning("stt.transcribe_failed", extra={"error": str(exc)})
            return _FAILURE

        text = " ".join(seg.text.strip() for seg in segments).strip()
        duration = getattr(info, "duration", None)
        language = getattr(info, "language", None)

        if not text:
            logger.warning("stt.empty_transcript", extra={"language": language})
            return Transcript(
                text=FAILURE_TEXT, language=language, confidence=0.0, duration=duration
            )

        return Transcript(
            text=text,
            language=language,
            confidence=_confidence_from_segments(segments),
            duration=duration,
        )
