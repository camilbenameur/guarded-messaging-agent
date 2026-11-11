"""TelethonBridge: the real Telegram implementation of the `Bridge` protocol.

It talks MTProto as the owner's *user* account (a session created once by
`python -m shared.telegram_login`), because the project reads the owner's own
chats, which a bot cannot see.

Design points
-------------
- **Sync facade over async Telethon.** `Bridge` is synchronous. Telethon is
  asyncio. The bridge runs a private event loop in a daemon thread and blocks on
  `run_coroutine_threadsafe(...)`, so it also works when called from inside
  another running loop (the MCP server's tool handlers).
- **Connect per operation.** Every `poll` / `download_media` / `send_text`
  connects, does its work and disconnects, under an inter-process file lock
  (`<session>.lock`). The ingest worker and the MCP server share one session
  file; this keeps them from using the same auth key and the same SQLite
  session at the same time. Pass `keep_connected=True` to hold the connection.
- **Read-only except `send_text`.** Only these Telethon calls are ever made:
  `connect`, `is_user_authorized`, `disconnect`, `iter_dialogs`, `iter_messages`,
  `get_messages`, `get_input_entity`, `download_media`, `send_message`. Nothing
  marks anything as read (no `send_read_acknowledge`, no read-history or
  read-contents request), no typing indicator, no online status
  (`receive_updates=False`).
- **Flood-wait.** Telethon's own auto-sleep is disabled (`flood_sleep_threshold=0`)
  so every `FloodWaitError` reaches `_retry`, which sleeps the wait Telegram asked
  for (plus one second) and retries up to `max_flood_retries` times. A wait longer
  than `max_flood_wait` or too many retries raises `BridgeRateLimited`
  carrying `retry_after`; nothing is lost because the cursor only advances for
  events that were returned.
- **Event ids encode the cursor.** The worker persists the id of the last event as
  its cursor, and Telegram message ids are only unique per chat, so an id is
  `tg.<unix ts>.<chat id>.<message id>`. `poll(after)` returns events whose
  `(ts, chat, message id)` is strictly greater than the cursor's, oldest first.
  A malformed cursor raises `UnknownCursorError`.
- **First run does not backfill the whole account.** With no cursor it looks back
  `backfill_days` (default 7) and at most `per_chat_limit` messages per chat.

Event mapping: text -> `text`; voice note -> `audio`; photo -> `image`; video,
video note, GIF -> `video`; any other file -> `doc`. A caption goes in
`media_meta["caption"]`. Service messages and unsupported content (polls,
locations...) are skipped. Own messages have `sender_id == "me"` and direction
`out`. Broadcast channels are skipped unless `include_channels=True`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.bridge import BridgeEvent
from shared.fake_bridge import UnknownCursorError

try:  # POSIX inter-process lock; absent on Windows (then no lock is taken).
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^tg\.(\d+)\.(-?\d+)\.(\d+)$")
_REF_RE = re.compile(r"^(-?\d+)\.(\d+)$")

DEFAULT_BACKFILL_DAYS = 7
DEFAULT_PER_CHAT_LIMIT = 200
DEFAULT_MAX_EVENTS = 500
DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_FLOOD_WAIT = 900.0
DEFAULT_MAX_FLOOD_RETRIES = 3

Key = tuple[int, str, int]  # (ts, chat_id, message_id)


class BridgeError(RuntimeError):
    """Base class for TelethonBridge failures."""


class BridgeAuthError(BridgeError):
    """No usable Telegram session. Fix: run `python -m shared.telegram_login`."""


class BridgeRateLimited(BridgeError):
    """Telegram asked us to wait longer than we are willing to block."""

    def __init__(self, retry_after: float, message: str | None = None) -> None:
        super().__init__(message or f"Telegram flood wait: retry in {retry_after:.0f}s")
        self.retry_after = retry_after


def make_event_id(ts: int, chat_id: str, message_id: int) -> str:
    return f"tg.{ts}.{chat_id}.{message_id}"


def parse_event_id(event_id: str) -> Key:
    m = _ID_RE.match(event_id or "")
    if not m:
        raise UnknownCursorError(f"not a TelethonBridge event id: {event_id!r}")
    return (int(m.group(1)), m.group(2), int(m.group(3)))


def _flood_error_type() -> type[BaseException]:
    from telethon.errors import FloodWaitError

    return FloodWaitError


def _file_obj(msg: Any) -> Any:
    return getattr(msg, "file", None)


def _msg_ts(msg: Any) -> int:
    return int(msg.date.timestamp())


def message_to_event(msg: Any, chat_id: str, max_caption: int = 4000) -> BridgeEvent | None:
    """Map one Telethon message to a `BridgeEvent`, or None to skip it."""
    if getattr(msg, "action", None) is not None:  # service message (join, pin...)
        return None
    ts = _msg_ts(msg)
    out = bool(getattr(msg, "out", False))
    sender = "me" if out else str(getattr(msg, "sender_id", None) or chat_id)
    text = getattr(msg, "message", None) or None
    f = _file_obj(msg)
    ref = f"{chat_id}.{msg.id}"

    def meta(**extra: Any) -> dict:
        d: dict[str, Any] = {}
        if f is not None:
            if getattr(f, "mime_type", None):
                d["mime"] = f.mime_type
            if getattr(f, "size", None) is not None:
                d["size"] = f.size
        d.update({k: v for k, v in extra.items() if v is not None})
        if text:
            d["caption"] = text[:max_caption]
        return d

    if getattr(msg, "voice", None):
        modality, m = "audio", meta(duration_s=getattr(f, "duration", None))
    elif getattr(msg, "photo", None):
        modality, m = (
            "image",
            meta(width=getattr(f, "width", None), height=getattr(f, "height", None)),
        )
    elif (
        getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "gif", None)
    ):
        modality, m = (
            "video",
            meta(duration_s=getattr(f, "duration", None), filename=getattr(f, "name", None)),
        )
    elif getattr(msg, "document", None):
        modality, m = "doc", meta(filename=getattr(f, "name", None))
    elif text:
        return BridgeEvent(
            id=make_event_id(ts, chat_id, msg.id),
            chat_id=chat_id,
            sender_id=sender,
            ts=ts,
            direction="out" if out else "in",
            modality="text",
            text=text,
            media_ref=None,
            media_meta={},
        )
    else:
        return None  # poll, location, contact, empty...
    return BridgeEvent(
        id=make_event_id(ts, chat_id, msg.id),
        chat_id=chat_id,
        sender_id=sender,
        ts=ts,
        direction="out" if out else "in",
        modality=modality,  # type: ignore[arg-type]
        text=None,
        media_ref=ref,
        media_meta=m,
    )


class TelethonBridge:
    """`Bridge` over a Telethon user session. See the module docstring."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str | Path,
        *,
        client_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
        backfill_days: float = DEFAULT_BACKFILL_DAYS,
        per_chat_limit: int = DEFAULT_PER_CHAT_LIMIT,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
        max_flood_wait: float = DEFAULT_MAX_FLOOD_WAIT,
        max_flood_retries: int = DEFAULT_MAX_FLOOD_RETRIES,
        include_channels: bool = False,
        keep_connected: bool = False,
        use_lock: bool = True,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        session = Path(session_path)
        self.session_path = (
            session
            if session.suffix == ".session"
            else session.with_name(session.name + ".session")
        )
        self._client_factory = client_factory
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self.backfill_days = backfill_days
        self.per_chat_limit = per_chat_limit
        self.max_events = max_events
        self.max_media_bytes = max_media_bytes
        self.max_flood_wait = max_flood_wait
        self.max_flood_retries = max_flood_retries
        self.include_channels = include_channels
        self.keep_connected = keep_connected
        self._use_lock = use_lock and client_factory is None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._client: Any = None

    # -- sync <-> async plumbing ------------------------------------------------

    def _run(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        with self._op_lock:
            if self._loop is None:
                with self._start_lock:
                    if self._loop is None:
                        loop = asyncio.new_event_loop()
                        t = threading.Thread(
                            target=loop.run_forever, name="telethon-bridge", daemon=True
                        )
                        t.start()
                        self._loop, self._thread = loop, t

            async def runner() -> Any:
                return await coro_factory()

            return asyncio.run_coroutine_threadsafe(runner(), self._loop).result()

    def close(self) -> None:
        """Disconnect (if held) and stop the private loop."""
        if self._loop is None:
            return
        with suppress(Exception):
            self._run(self._disconnect)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = self._thread = None

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from telethon import TelegramClient

        client = TelegramClient(
            str(self.session_path),
            self.api_id,
            self.api_hash,
            receive_updates=False,  # no update stream, no "online" side effects
            flood_sleep_threshold=0,  # surface every FloodWaitError to _retry
        )
        client.parse_mode = None  # send text verbatim, no markdown reinterpretation
        return client

    async def _disconnect(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()

    def _acquire_lock(self) -> Any:
        if not (self._use_lock and fcntl is not None):
            return None
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.session_path.with_name(self.session_path.name + ".lock"), "a")  # noqa: SIM115
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    async def _with_client(self, work: Callable[[Any], Awaitable[Any]]) -> Any:
        lock = self._acquire_lock()
        try:
            if self._client is None:
                if self._client_factory is None and not self.session_path.exists():
                    raise BridgeAuthError(
                        f"no Telegram session at {self.session_path}. "
                        "Fix: run `python -m shared.telegram_login` once."
                    )
                client = self._make_client()
                await client.connect()
                self._client = client
            try:
                if not await self._client.is_user_authorized():
                    raise BridgeAuthError(
                        "Telegram session is not authorized (expired or revoked). "
                        "Fix: run `python -m shared.telegram_login` again."
                    )
                return await work(self._client)
            finally:
                if not self.keep_connected:
                    await self._disconnect()
        finally:
            if lock is not None:
                lock.close()  # releases the flock

    async def _retry(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        flood = _flood_error_type()
        attempt = 0
        while True:
            try:
                return await factory()
            except flood as exc:
                wait = float(getattr(exc, "seconds", 0)) + 1.0
                attempt += 1
                if wait > self.max_flood_wait or attempt > self.max_flood_retries:
                    raise BridgeRateLimited(wait) from exc
                logger.warning("telegram.flood_wait", extra={"seconds": wait, "attempt": attempt})
                await self._sleep(wait)

    # -- Bridge protocol ----------------------------------------------------------

    def poll(self, after: str | None) -> list[BridgeEvent]:
        cursor = parse_event_id(after) if after is not None else None
        return self._run(lambda: self._with_client(lambda c: self._poll(c, cursor)))

    def download_media(self, media_ref: str) -> bytes:
        m = _REF_RE.match(media_ref or "")
        if not m:
            raise ValueError(f"not a TelethonBridge media ref: {media_ref!r}")
        chat_id, message_id = int(m.group(1)), int(m.group(2))
        return self._run(
            lambda: self._with_client(lambda c: self._download(c, chat_id, message_id))
        )

    def send_text(self, chat_id: str, text: str) -> str:
        try:
            chat = int(chat_id)
        except (TypeError, ValueError):
            raise ValueError(
                f"chat_id must be a numeric Telegram peer id, got {chat_id!r}"
            ) from None
        if not text or not text.strip():
            raise ValueError("refusing to send an empty message")
        return self._run(lambda: self._with_client(lambda c: self._send(c, chat, text)))

    # -- implementations ------------------------------------------------------------

    async def _poll(self, client: Any, cursor: Key | None) -> list[BridgeEvent]:
        since = cursor[0] if cursor else int(self._clock() - self.backfill_days * 86400)
        offset = datetime.fromtimestamp(since - 1, UTC)  # exclusive bound, keep same-second msgs

        async def list_dialogs() -> list[Any]:
            return [d async for d in client.iter_dialogs()]

        dialogs = await self._retry(list_dialogs)
        collected: list[tuple[Key, BridgeEvent]] = []
        horizon: Key | None = None  # lowest "last key" among chats that hit the per-chat limit
        for dialog in dialogs:
            if getattr(dialog, "is_channel", False) and not getattr(dialog, "is_group", False):
                if not self.include_channels:
                    continue
            if dialog.date is not None and int(dialog.date.timestamp()) < since:
                continue
            chat_id = str(dialog.id)

            async def fetch(entity=dialog.entity) -> list[Any]:
                return [
                    m
                    async for m in client.iter_messages(
                        entity, offset_date=offset, reverse=True, limit=self.per_chat_limit
                    )
                ]

            msgs = await self._retry(fetch)
            last_seen: Key | None = None
            for msg in msgs:
                key: Key = (_msg_ts(msg), chat_id, int(msg.id))
                last_seen = key
                if cursor is not None and key <= cursor:
                    continue
                ev = message_to_event(msg, chat_id)
                if ev is not None:
                    collected.append((key, ev))
            if len(msgs) >= self.per_chat_limit and last_seen is not None:
                horizon = last_seen if horizon is None else min(horizon, last_seen)

        collected.sort(key=lambda kv: kv[0])
        if horizon is not None:  # a limited chat may have more: don't skip past its tail
            collected = [kv for kv in collected if kv[0] <= horizon]
        return [ev for _, ev in collected[: self.max_events]]

    async def _fetch_message(self, client: Any, chat_id: int, message_id: int) -> Any:
        async def get() -> Any:
            entity = await client.get_input_entity(chat_id)
            return await client.get_messages(entity, ids=message_id)

        msg = await self._retry(get)
        if msg is None:
            raise BridgeError(f"message {chat_id}.{message_id} not found (deleted?)")
        return msg

    async def _download(self, client: Any, chat_id: int, message_id: int) -> bytes:
        msg = await self._fetch_message(client, chat_id, message_id)
        size = getattr(_file_obj(msg), "size", None)
        if size is not None and size > self.max_media_bytes:
            raise BridgeError(
                f"media {chat_id}.{message_id} is {size} bytes, over the {self.max_media_bytes} cap"
            )
        data = await self._retry(lambda: client.download_media(msg, file=bytes))
        if data is None:
            raise BridgeError(f"message {chat_id}.{message_id} has no downloadable media")
        return bytes(data)

    async def _send(self, client: Any, chat_id: int, text: str) -> str:
        async def send() -> Any:
            entity = await client.get_input_entity(chat_id)
            return await client.send_message(entity, text, link_preview=False)

        sent = await self._retry(send)  # a flood-wait means "not sent", so retrying is safe
        return make_event_id(_msg_ts(sent), str(chat_id), int(sent.id))
