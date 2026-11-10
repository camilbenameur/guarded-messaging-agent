"""FakeBridge: fixture-replay implementation of the `Bridge` protocol.

Every test suite in this repo (ingestion, STT, vision, MCP) exercises code
against this instead of a real Telegram connection ("no network in tests").

Scenario file format (`tests/fixtures/scenario.json`): a JSON list of objects
with the same fields as `BridgeEvent`, listed in chronological order. Events
referencing media set `media_ref` to a path relative to the fixtures
directory (default: the directory containing the scenario file), e.g.
`"audio/fr_meeting.ogg"` or `"images/injection.png"`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.bridge import BridgeEvent


class UnknownCursorError(ValueError):
    """Raised by `poll(after=...)` when `after` doesn't match any event id."""


class FakeBridge:
    """Replays a recorded scenario as a `Bridge`.

    - `poll(after)` returns the events strictly after the event whose `id`
      equals `after`, preserving scenario order. `after=None` returns the
      whole scenario from the start. Passing the id of the last event
      returned by a previous `poll` call resumes correctly with only the
      events appended after it.
    - `download_media(media_ref)` reads bytes from the fixtures directory.
    - `send_text(chat_id, text)` never touches a network: it appends to the
      in-memory `outbox` list and returns a synthetic message id.
    """

    def __init__(
        self,
        scenario_path: str | Path,
        fixtures_dir: str | Path | None = None,
    ) -> None:
        self.scenario_path = Path(scenario_path)
        self.fixtures_dir = (
            Path(fixtures_dir) if fixtures_dir is not None else self.scenario_path.parent
        )
        raw: list[dict[str, Any]] = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        self.events: list[BridgeEvent] = [_event_from_dict(item) for item in raw]
        self.outbox: list[dict[str, str]] = []
        self._sent_counter = 0

    def poll(self, after: str | None) -> list[BridgeEvent]:
        if after is None:
            return list(self.events)
        for index, event in enumerate(self.events):
            if event.id == after:
                return list(self.events[index + 1 :])
        raise UnknownCursorError(f"no event with id {after!r} in scenario {self.scenario_path}")

    def download_media(self, media_ref: str) -> bytes:
        return (self.fixtures_dir / media_ref).read_bytes()

    def send_text(self, chat_id: str, text: str) -> str:
        self._sent_counter += 1
        message_id = f"fake-out-{self._sent_counter}"
        self.outbox.append({"id": message_id, "chat_id": chat_id, "text": text})
        return message_id


def _event_from_dict(data: dict[str, Any]) -> BridgeEvent:
    return BridgeEvent(
        id=data["id"],
        chat_id=data["chat_id"],
        sender_id=data["sender_id"],
        ts=data["ts"],
        direction=data["direction"],
        modality=data["modality"],
        text=data.get("text"),
        media_ref=data.get("media_ref"),
        media_meta=data.get("media_meta") or {},
    )
