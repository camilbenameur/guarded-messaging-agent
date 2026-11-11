"""A stand-in for `telethon.TelegramClient`, for tests. No network, ever.

It implements only the calls `TelethonBridge` is allowed to make. Any other
attribute access (for example `send_read_acknowledge`) is recorded in
`forbidden_calls` and raises, so a test can prove nothing marks messages read.
`calls` logs every allowed call by name. `flood` maps a method name to a list
of wait seconds; each call to that method pops one and raises a real
`telethon.errors.FloodWaitError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any


def ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


@dataclass
class FakeMessage:
    id: int
    ts: int
    message: str = ""
    out: bool = False
    sender_id: int | None = None
    action: Any = None
    voice: Any = None
    photo: Any = None
    video: Any = None
    video_note: Any = None
    gif: Any = None
    document: Any = None
    file: Any = None
    payload: bytes = b""

    @property
    def date(self) -> datetime:
        return ts_to_dt(self.ts)


def file_info(**kw: Any) -> SimpleNamespace:
    base = {
        "mime_type": None,
        "size": None,
        "duration": None,
        "width": None,
        "height": None,
        "name": None,
    }
    return SimpleNamespace(**{**base, **kw})


@dataclass
class FakeChat:
    id: int
    messages: list[FakeMessage] = field(default_factory=list)
    is_user: bool = True
    is_group: bool = False
    is_channel: bool = False

    @property
    def entity(self) -> int:
        return self.id

    @property
    def date(self) -> datetime | None:
        return ts_to_dt(max(m.ts for m in self.messages)) if self.messages else None


class FakeTelegramClient:
    def __init__(
        self,
        chats: list[FakeChat],
        *,
        authorized: bool = True,
        flood: dict[str, list[int]] | None = None,
        next_message_id: int = 9000,
        clock: int = 1_800_000_000,
    ) -> None:
        self.chats = {c.id: c for c in chats}
        self.authorized = authorized
        self.flood = flood or {}
        self.calls: list[str] = []
        self.forbidden_calls: list[str] = []
        self.sent: list[tuple[int, str]] = []
        self.connected = False
        self.connects = 0
        self._next_id = next_message_id
        self._now = clock
        self.parse_mode: Any = "unset"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        self.forbidden_calls.append(name)
        raise AttributeError(f"FakeTelegramClient: {name} is not an allowed call")

    def _maybe_flood(self, method: str) -> None:
        self.calls.append(method)
        if self.flood.get(method):
            from telethon.errors import FloodWaitError

            raise FloodWaitError(request=None, capture=self.flood[method].pop(0))

    async def connect(self) -> None:
        self.calls.append("connect")
        self.connected = True
        self.connects += 1

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False

    async def is_user_authorized(self) -> bool:
        self.calls.append("is_user_authorized")
        return self.authorized

    async def iter_dialogs(self):
        self._maybe_flood("iter_dialogs")
        for chat in self.chats.values():
            yield chat

    async def iter_messages(self, entity, offset_date=None, reverse=False, limit=None):
        self._maybe_flood("iter_messages")
        msgs = sorted(self.chats[entity].messages, key=lambda m: (m.ts, m.id))
        if offset_date is not None:
            msgs = [m for m in msgs if (m.date > offset_date if reverse else m.date < offset_date)]
        if not reverse:
            msgs.reverse()
        for m in msgs[:limit]:
            yield m

    async def get_input_entity(self, peer: int) -> int:
        self.calls.append("get_input_entity")
        return peer

    async def get_messages(self, entity, ids: int):
        self._maybe_flood("get_messages")
        return next((m for m in self.chats[entity].messages if m.id == ids), None)

    async def download_media(self, message: FakeMessage, file=bytes):
        self._maybe_flood("download_media")
        return message.payload

    async def send_message(self, entity, text: str, link_preview: bool = True) -> FakeMessage:
        self._maybe_flood("send_message")
        self._next_id += 1
        self.sent.append((entity, text))
        return FakeMessage(id=self._next_id, ts=self._now, message=text, out=True)
