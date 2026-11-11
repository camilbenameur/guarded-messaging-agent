"""MCP server exposing the store to MCP clients over streamable HTTP.

Transport: streamable HTTP on port 8808 (stdio doesn't fit a
container). Built with the official Python MCP SDK (`mcp` package,
`mcp.server.fastmcp.FastMCP`). The FastMCP-produced ASGI app is extended with
a plain `/health` route so the DoD's `curl localhost:8808/health` has
something to hit even though the MCP SDK's app doesn't expose one natively.

Tool classes:
- read (no side effects): `list_chats`, `list_messages`, `search_messages`,
  `get_message_context`, `get_media`.
- propose (no side effects, return text): `draft_reply`, `analyze_conversation`,
  `summarize_chat` — backed by a `DraftClient` (see `mcp_server.draft_client`).
- act (gated, always): `send_message` — every call flows through the
  `mcp_server.gate.SendGate` (policy evaluation + allowlist + rate limit +
  human approval queue + kill switch + audit). Nothing else in this package
  touches the gate's send path or `Bridge.send_text` — the gate module is
  the only sanctioned caller of the bridge.

`create_mcp_server` takes `store`/`draft_client`/`bridge` as constructor-style
arguments (dependency injection) so
tests can bind a fresh `Store` + `MockDraft` + `FakeBridge` per run instead of
touching a process-global. With `bridge=None` (no `BRIDGE` configured) the gate
fails closed: nothing can auto-send, and queued sends can't be released.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from shared.bridge import Bridge
from shared.bridge_factory import build_bridge
from shared.store import DEFAULT_DB_PATH, Store
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server.draft_client import DraftClient, build_default_draft_client
from mcp_server.gate import SendGate

logger = logging.getLogger(__name__)

SERVER_NAME = "messaging-agent-mcp"
STREAMABLE_HTTP_PATH = "/mcp"


def create_mcp_server(
    store: Store,
    draft_client: DraftClient | None = None,
    bridge: Bridge | None = None,
) -> FastMCP:
    """Build the `FastMCP` instance with every tool bound to `store` (and
    `draft_client`, defaulting to the env-selected client when omitted) via closure.
    `bridge` feeds the send gate only — no other tool can reach it.
    """
    if draft_client is None:
        draft_client = build_default_draft_client()
    send_gate = SendGate(store, bridge=bridge)

    mcp = FastMCP(
        SERVER_NAME,
        stateless_http=True,
        streamable_http_path=STREAMABLE_HTTP_PATH,
    )

    # -- read tools (no side effects) ----------------------------------------

    @mcp.tool()
    def list_chats() -> list[dict[str, Any]]:
        """List every known chat (chat_id, name, is_group, policy). Call this
        first when you don't already have a chat_id — e.g. to let a user pick
        which conversation to work on, or to enumerate chats for a digest.
        """
        return store.list_chats()

    @mcp.tool()
    def list_messages(
        chat_id: str, before: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch the most recent messages in one chat, newest first. Call this
        to read/catch up on a conversation once you have its chat_id (e.g.
        from list_chats or a search hit). Pass `before` (a message ts) to page
        further back into history.
        """
        return store.list_messages(chat_id, before=before, limit=limit)

    @mcp.tool()
    def search_messages(
        query: str, chat_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Full-text search over every ingested message (raw text, voice-note
        transcripts, and image captions alike). Call this when you need to
        find where a topic, name, or keyword was mentioned — across all chats,
        or scoped to one via `chat_id` — instead of paging through
        list_messages looking for it by hand.
        """
        return store.search(query, chat_id=chat_id, limit=limit)

    @mcp.tool()
    def get_message_context(message_id: str, radius: int = 3) -> list[dict[str, Any]]:
        """Fetch `radius` messages immediately before and after one message in
        its chat, chronologically ordered. Call this right after
        search_messages or list_messages surfaces a message whose meaning
        depends on what was said around it (e.g. a bare "yes" or "confirmed").
        """
        return store.get_context(message_id, radius=radius)

    @mcp.tool()
    def get_media(message_id: str) -> dict[str, Any]:
        """Return the stored media for one message: its on-disk path and
        best-effort metadata (duration, mime, dims). Call this when a
        message's modality is audio/image/doc/video and you need the raw file
        itself, not just its derived content_text (transcript/caption) —
        e.g. to re-listen to a voice note or inspect an image directly.
        """
        message = store.get_message(message_id)
        if message is None:
            raise ValueError(f"no message with id {message_id!r}")
        media_path = message.get("media_path")
        if media_path is None:
            raise ValueError(f"message {message_id!r} has no attached media")
        return {
            "message_id": message_id,
            "media_path": media_path,
            "media_meta": message.get("media_meta") or {},
        }

    # -- propose tools (no side effects, return text) ------------------------

    @mcp.tool()
    def draft_reply(chat_id: str, instructions: str, limit: int = 20) -> str:
        """Propose a reply for a chat WITHOUT sending it. Call this when asked
        to suggest, compose, or draft a response that a human will review and
        send themselves — never as a way to actually deliver a message (that
        is send_message, which is gated). Returns the proposed text only; the
        attempt is recorded in the audit log.
        """
        thread = store.list_messages(chat_id, limit=limit)
        draft = draft_client.draft_reply(thread, instructions)
        store.append_audit(
            "draft", chat_id, {"instructions": instructions, "draft": draft.text}
        )
        return draft.text

    @mcp.tool()
    def analyze_conversation(chat_id: str, limit: int = 30) -> str:
        """Assess a chat's tone, urgency, and open questions/action items
        WITHOUT taking any action. Call this when asked to judge how a
        conversation is going, or whether it needs attention, before deciding
        whether to draft a reply, summarize it, or escalate.
        """
        thread = store.list_messages(chat_id, limit=limit)
        analysis = draft_client.analyze(thread)
        store.append_audit("analyze", chat_id, {"analysis": analysis.text})
        return analysis.text

    @mcp.tool()
    def summarize_chat(chat_id: str, limit: int = 50) -> str:
        """Summarize a chat's recent history into a short digest. Call this
        for "what did I miss" or daily-digest style requests instead of
        re-reading every message from list_messages yourself.
        """
        thread = store.list_messages(chat_id, limit=limit)
        summary = draft_client.summarize(thread)
        store.append_audit("summarize", chat_id, {"summary": summary.text})
        return summary.text

    # -- act tools (gated, always) -------------------------------------------

    @mcp.tool()
    def send_message(chat_id: str, text: str) -> str:
        """Send a text message to a chat. GATED — every call flows through
        the send gate: per-chat policy (mode + allow_when/never_when),
        allowlist, rate limit, kill switch, and a human approval queue. Only
        an explicit auto-mode policy pass sends immediately; anything else is
        parked for a human to release via `python -m mcp_server.approve <id>`
        — a queued result is success ("awaiting approval"), not an error.
        Every outcome is audit-logged inside the gate. This is the ONLY
        sanctioned path to sending; never try to reach the bridge directly.
        """
        result = send_gate.request_send(chat_id, text)
        if result.status == "sent":
            return f"sent: message_id={result.message_id}"
        if result.status == "queued":
            return (
                f"queued for human approval: queue_id={result.queue_id} "
                f"(reason: {result.reason})"
            )
        raise RuntimeError(f"send denied: {result.reason}")

    return mcp


def build_asgi_app(
    store: Store | None = None,
    draft_client: DraftClient | None = None,
    bridge: Bridge | None = None,
) -> Starlette:
    """Build the full ASGI app: the MCP streamable-HTTP app plus a `/health`
    route. Used both by tests (with an injected tmp `Store`/`MockDraft`) and
    by the real entrypoint (`build_asgi_app` with no args, run via
    `uvicorn mcp_server.server:build_asgi_app --factory` — see the
    Dockerfile). Building lazily behind a factory, rather than a module-level
    `app` object, avoids touching the store's default `/store` path as a
    side effect of merely importing this module (e.g. during test collection).
    """
    if store is None:
        store = Store(os.environ.get("STORE_DB_PATH", DEFAULT_DB_PATH))
        if bridge is None:  # real entrypoint only: tests inject their own store/bridge
            bridge = build_bridge()

    mcp = create_mcp_server(store, draft_client, bridge)
    app = mcp.streamable_http_app()

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # Inserted ahead of the MCP app's own routes so it's matched first.
    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    return app
