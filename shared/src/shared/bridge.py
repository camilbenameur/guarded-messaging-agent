"""Bridge protocol: the transport-agnostic seam between the messaging transport
(Telegram/Telethon) and the ingestion worker.

This is a deliberately small interface: both transports and every test suite
depend on it, so extend it with care.

Implementations:
- `FakeBridge` (`shared.fake_bridge`) — fixture replay, used by every
  test suite in this repo.
- `TelethonBridge` — real Telegram transport (not implemented yet).

`send_text` is only ever called from the gated send path (`mcp_server.gate`); nothing else
in this codebase should import it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class BridgeEvent:
    """One inbound or outbound message, normalized to a transport-agnostic shape."""

    id: str  # transport message id (unique)
    chat_id: str
    sender_id: str
    ts: int  # epoch seconds
    direction: Literal["in", "out"]
    modality: Literal["text", "audio", "image", "doc", "video"]
    text: str | None  # set for text messages
    media_ref: str | None  # opaque ref the bridge can resolve to bytes
    media_meta: dict  # duration, mime, dims... best-effort from transport


class Bridge(Protocol):
    """The interface the ingestion worker (and gated send path) code against.

    Never call a transport SDK (Telethon, etc.) directly outside an
    implementation of this protocol.
    """

    def poll(self, after: str | None) -> list[BridgeEvent]: ...

    def download_media(self, media_ref: str) -> bytes: ...

    def send_text(self, chat_id: str, text: str) -> str: ...  # returns sent message id
