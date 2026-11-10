"""Shared fixtures for the MCP server test suite.

`store` seeds a fresh tmp-path `Store` with two chats and a handful of
messages, including one with a unique marker token for `search_messages` to
find. `mcp_server_url` boots the real ASGI app (`mcp_server.server.build_asgi_app`,
bound to `store` + a `MockDraft` — never a real network call) on a background
uvicorn server and yields its base URL, so tests exercise the actual
streamable-HTTP transport end to end via an MCP client.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from shared.store import Message, Store

from mcp_server.draft_client import MockDraft
from mcp_server.gate import KILL_SWITCH_ENV
from mcp_server.server import build_asgi_app

SEARCH_MARKER = "unique-marker-xyz"


@pytest.fixture(autouse=True)
def _kill_switch_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray `SEND_KILL_SWITCH` in a developer's `.env` (compose passes it
    through) must not flip unrelated tests. Tests that exercise the switch
    set it explicitly with `monkeypatch.setenv`, which runs after this.
    """
    monkeypatch.delenv(KILL_SWITCH_ENV, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "messages.db")
    s.upsert_chat("chat-1", name="Alice", is_group=False)
    s.upsert_chat("chat-2", name="Team Group", is_group=True)
    s.upsert_message(
        Message(
            id="msg-1",
            chat_id="chat-1",
            sender_id="alice",
            ts=1_700_000_000,
            direction="in",
            modality="text",
            content_text=f"hey, remember the {SEARCH_MARKER} from earlier?",
            processed=1,
        )
    )
    s.upsert_message(
        Message(
            id="msg-2",
            chat_id="chat-1",
            sender_id="me",
            ts=1_700_000_100,
            direction="out",
            modality="text",
            content_text="yes I remember, all good",
            processed=1,
        )
    )
    s.upsert_message(
        Message(
            id="msg-3",
            chat_id="chat-2",
            sender_id="bob",
            ts=1_700_000_200,
            direction="in",
            modality="image",
            content_text="[image] a photo of a whiteboard",
            media_path=str(tmp_path / "fake-image.png"),
            media_meta={"mime": "image/png", "width": 640, "height": 480},
            processed=1,
        )
    )
    return s


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def mcp_server_url(store: Store) -> Iterator[str]:
    app = build_asgi_app(store=store, draft_client=MockDraft())
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    ready = False
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    if not ready:
        raise RuntimeError("mcp server did not become healthy in time")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
