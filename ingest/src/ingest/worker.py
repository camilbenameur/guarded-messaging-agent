"""Ingestion worker: turns `BridgeEvent`s into unified `messages` rows where
`content_text` is always populated — the "agent is never blind" invariant.

Routing per modality:
- `text`          -> `content_text = event.text`, `processed = 1` immediately.
- `audio`/`image` -> `content_text` = a placeholder (`"[voice note]"` /
                     `"[image]"`); media bytes are downloaded through the
                     bridge and saved to `<media_dir>/<event.id>`;
                     `processed = 0` until a derivation pass runs.
- `doc`/`video`   -> `content_text = "[attachment: <name>]"`, `processed = 1`
                     immediately (no derivation needed).

`Worker` is constructed with the `Bridge` and `Store`, plus two
*optional* hooks, `stt` and `vision`, that `process_pending()` calls if
configured. Neither hook is implemented here — real STT lives in `ingest.stt`
(faster-whisper), real vision captioning in `ingest.vision` (Claude). With no hook
configured for a modality, matching pending rows are simply left
`processed = 0` until one is wired in.
"""

from __future__ import annotations

import io
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from shared.bridge import Bridge, BridgeEvent
from shared.store import Message, Modality, Store

logger = logging.getLogger(__name__)

DEFAULT_MEDIA_DIR = "/media"
DEFAULT_CURSOR_KEY = "default"

#: Cost guard: cap how many images `process_pending()` will caption
#: (across the lifetime of a `Worker` instance) before it starts leaving
#: image rows pending instead of calling the vision hook. `None` = no cap.
#: See `Worker.__init__`'s `max_caption_images` param and `_process_image_row`.
_ENV_MAX_CAPTION_IMAGES = "MAX_CAPTION_IMAGES"


def _default_max_caption_images() -> int | None:
    raw = os.environ.get(_ENV_MAX_CAPTION_IMAGES)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("worker.invalid_max_caption_images_env", extra={"value": raw})
        return None

# Placeholder `content_text` for modalities whose real text is derived later
# by an STT/vision hook (see `Worker.process_pending`).
_PLACEHOLDER_TEXT: dict[str, str] = {
    "audio": "[voice note]",
    "image": "[image]",
}
_DERIVED_MODALITIES = frozenset(_PLACEHOLDER_TEXT)  # {"audio", "image"}
_ATTACHMENT_MODALITIES = frozenset({"doc", "video"})


@dataclass(frozen=True)
class Transcript:
    """Expected return shape of an injected STT hook's `transcribe()`. The
    real faster-whisper client (`ingest.stt.WhisperSTT`) returns this. `duration` is
    needed for the `media_meta = {duration, language, confidence,
    kind: "audio"}` shape stored on the row.
    """

    text: str
    language: str | None = None
    confidence: float | None = None
    duration: float | None = None


@dataclass(frozen=True)
class Caption:
    """Mirrors `VisionClient.caption()`'s return shape:
    `Caption(text, ocr, confidence)`.
    """

    text: str
    ocr: str | None = None
    confidence: float | None = None


class SttHook(Protocol):
    def transcribe(self, audio_bytes: bytes, media_meta: dict[str, Any]) -> Transcript: ...


class VisionHook(Protocol):
    def caption(self, image_bytes: bytes, context: str) -> Caption: ...


class Worker:
    """Poll -> route -> persist loop from `BridgeEvent` to `messages` rows."""

    def __init__(
        self,
        bridge: Bridge,
        store: Store,
        stt: SttHook | None = None,
        vision: VisionHook | None = None,
        media_dir: str | Path = DEFAULT_MEDIA_DIR,
        cursor_key: str = DEFAULT_CURSOR_KEY,
        max_caption_images: int | None = None,
    ) -> None:
        self.bridge = bridge
        self.store = store
        self.stt = stt
        self.vision = vision
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        # Cost guard: explicit param
        # wins over `MAX_CAPTION_IMAGES` env (mirrors `WhisperSTT`'s
        # `model_name` override pattern) -- `None` here means "read the env",
        # not "unlimited"; leave both unset for genuinely unlimited backfills.
        # Counts images captioned across this Worker instance's lifetime
        # (every `process_pending()`/`run_forever()` call it makes), so a cap
        # reached mid-backfill stays enforced across subsequent calls on the
        # same instance -- only a fresh `Worker` (e.g. a process restart or a
        # deliberately-raised cap) resets the count.
        self.max_caption_images = (
            max_caption_images if max_caption_images is not None else _default_max_caption_images()
        )
        self._captioned_count = 0
        self.cursor_key = cursor_key

    # -- ingest loop ----------------------------------------------------------

    def run_once(self) -> int:
        """Poll the bridge for events after the persisted cursor, route and
        persist each one, then advance the cursor. Returns the number of
        events ingested (0 if nothing new).

        Safe to call repeatedly, including across process restarts: the
        cursor is read from and written back to the store, and
        `Store.upsert_message` is a no-op for an id already present, so a
        stale/replayed poll never duplicates rows.
        """
        cursor = self.store.get_cursor(self.cursor_key)
        events = self.bridge.poll(cursor)
        for event in events:
            self._ingest_event(event)
        if events:
            self.store.set_cursor(self.cursor_key, events[-1].id)
        return len(events)

    def run_forever(self, interval: float) -> None:
        """Call `run_once()` in a loop, sleeping `interval` seconds between
        polls. Runs until the process is interrupted/killed.
        """
        while True:
            ingested = self.run_once()
            logger.info("ingest.run_once", extra={"ingested": ingested})
            time.sleep(interval)

    def _ingest_event(self, event: BridgeEvent) -> None:
        self.store.upsert_chat(event.chat_id)
        media_meta = event.media_meta or None

        if event.modality == "text":
            self.store.upsert_message(
                Message(
                    id=event.id,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    ts=event.ts,
                    direction=event.direction,
                    modality=event.modality,
                    content_text=event.text or "",
                    media_meta=media_meta,
                    processed=1,
                )
            )
        elif event.modality in _DERIVED_MODALITIES:
            media_path = self._download_media(event)
            self.store.upsert_message(
                Message(
                    id=event.id,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    ts=event.ts,
                    direction=event.direction,
                    modality=event.modality,
                    content_text=_PLACEHOLDER_TEXT[event.modality],
                    media_path=str(media_path) if media_path is not None else None,
                    media_meta=media_meta,
                    processed=0,
                )
            )
        elif event.modality in _ATTACHMENT_MODALITIES:
            self.store.upsert_message(
                Message(
                    id=event.id,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    ts=event.ts,
                    direction=event.direction,
                    modality=event.modality,
                    content_text=f"[attachment: {_attachment_name(event)}]",
                    media_meta=media_meta,
                    processed=1,
                )
            )
        else:
            raise ValueError(f"unknown modality: {event.modality!r}")

    def _download_media(self, event: BridgeEvent) -> Path | None:
        if event.media_ref is None:
            return None
        data = self.bridge.download_media(event.media_ref)
        path = self.media_dir / event.id
        path.write_bytes(data)
        return path

    # -- derivation pass --------------------------------------------------------

    def process_pending(self, modality: Modality | None = None) -> int:
        """Run the configured STT/vision hook over every `processed = 0` row
        (optionally scoped to one `modality`), updating `content_text` and
        `media_meta`, then marking the row `processed = 1`. Reads media
        bytes back from disk (`media_path`), not from the bridge, so this
        pass needs no network/bridge access and can run standalone as a
        backfill.

        Idempotent: rows already `processed = 1` are never revisited, so a
        second call finds nothing left to do. With no hook configured for a
        modality, its rows are simply left pending — safe to call again once
        a hook is wired in.

        Image rows honor `self.max_caption_images` (cost guard): once
        that many images have been captioned by this `Worker` instance,
        `_process_image_row` stops calling the vision hook and leaves the
        remaining image rows pending (unchanged placeholder `content_text`,
        `processed = 0`) rather than erroring — cheap to pick up in a later
        `process_pending()` call (this instance once the cap is raised, or a
        fresh instance/process) without re-captioning already-processed rows.

        Returns the number of rows updated.
        """
        updated = 0
        for row in self.store.list_pending(modality=modality):
            if row["modality"] == "audio" and self.stt is not None:
                self._process_audio_row(row)
                updated += 1
            elif row["modality"] == "image" and self.vision is not None:
                if self._process_image_row(row):
                    updated += 1
            # else: no hook configured for this row's modality — stays pending.
        return updated

    def reprocess(self, modality: Modality) -> int:
        """Reset every row of `modality` back to `processed = 0` so the next
        `process_pending()` call recomputes its derived text (e.g. after a
        prompt or model change). Returns the number of rows reset.
        """
        return self.store.reset_processed(modality)

    def _process_audio_row(self, row: dict[str, Any]) -> None:
        assert self.stt is not None
        audio_bytes = self._read_media(row)
        original_meta = row.get("media_meta") or {}
        result = self.stt.transcribe(audio_bytes, original_meta)
        # `kind: "audio"` is always stamped (the media_meta shape),
        # which also guarantees this dict is never empty, so the derived
        # fields below always get persisted via `finish_processing`.
        media_meta = {**original_meta, "kind": "audio"}
        if result.language is not None:
            media_meta["language"] = result.language
        if result.confidence is not None:
            media_meta["confidence"] = result.confidence
        if result.duration is not None:
            media_meta["duration"] = result.duration
        self.store.finish_processing(row["id"], result.text, media_meta or None)

    def _process_image_row(self, row: dict[str, Any]) -> bool:
        """Caption one pending image row, unless `max_caption_images` has
        already been reached for this `Worker` instance. Returns whether the
        row was actually processed (`False` means it was left pending by the
        cost guard, per `process_pending()`'s docstring).
        """
        assert self.vision is not None
        if self.max_caption_images is not None and self._captioned_count >= self.max_caption_images:
            logger.info(
                "vision.max_caption_images_reached",
                extra={"cap": self.max_caption_images, "row_id": row["id"]},
            )
            return False

        image_bytes = self._read_media(row)
        original_meta = row.get("media_meta") or {}
        result = self.vision.caption(image_bytes, context="")
        # `kind: "image"` is always stamped (mirrors the audio media_meta
        # shape), which also guarantees this dict is never empty, so the
        # derived fields below always get persisted via `finish_processing`.
        media_meta: dict[str, Any] = {**original_meta, "kind": "image"}
        dims = _image_dims(image_bytes)
        if dims is not None:
            media_meta["dims"] = list(dims)
        if result.ocr is not None:
            media_meta["ocr"] = result.ocr
        if result.confidence is not None:
            media_meta["confidence"] = result.confidence
        self.store.finish_processing(row["id"], f"[image] {result.text}", media_meta or None)
        self._captioned_count += 1
        return True

    @staticmethod
    def _read_media(row: dict[str, Any]) -> bytes:
        media_path = row.get("media_path")
        if not media_path:
            raise ValueError(f"message {row['id']!r} is pending but has no media_path")
        return Path(media_path).read_bytes()


def _attachment_name(event: BridgeEvent) -> str:
    """Best-effort filename for a doc/video attachment: prefer transport
    metadata, fall back to the media ref's basename, else a generic label.
    """
    meta = event.media_meta or {}
    name = meta.get("filename") or meta.get("name")
    if name:
        return str(name)
    if event.media_ref:
        return Path(event.media_ref).name
    return "attachment"


def _image_dims(image_bytes: bytes) -> tuple[int, int] | None:
    """`(width, height)` read directly from the image bytes via PIL --
    independent of (and a ground-truth check on) whatever `width`/`height`
    the transport already reported in `media_meta`. Best-effort: `None` on
    any failure (corrupt image, PIL unavailable), never raises -- dims are a
    nice-to-have `media_meta` field, not load-bearing for the derivation pass.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.size
    except Exception as exc:  # pragma: no cover - defensive, mirrors vision.py's degrade style
        logger.warning("worker.image_dims_failed", extra={"error": str(exc)})
        return None
