"""End-to-end MCP server tests: a real `mcp` SDK client talks to the real
ASGI app (`mcp_server.server.build_asgi_app`) over streamable HTTP, exactly
as a real client would. No network beyond localhost, no real Claude API
call (the server under test is built with `MockDraft` — see
`conftest.mcp_server_url`).
"""

from __future__ import annotations

import pytest
from conftest import SEARCH_MARKER
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from shared.store import Store

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "list_chats",
    "list_messages",
    "search_messages",
    "get_message_context",
    "get_media",
    "draft_reply",
    "analyze_conversation",
    "summarize_chat",
    "send_message",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _text(result) -> str:
    """Concatenate every text content block of a `CallToolResult`."""
    return "".join(block.text for block in result.content if block.type == "text")


async def test_lists_all_tools(mcp_server_url: str) -> None:
    async with streamable_http_client(f"{mcp_server_url}/mcp") as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert EXPECTED_TOOLS <= names


async def test_search_messages_returns_seeded_rows(mcp_server_url: str) -> None:
    async with streamable_http_client(f"{mcp_server_url}/mcp") as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("search_messages", {"query": SEARCH_MARKER})
            assert result.isError is not True
            body = _text(result)
            assert SEARCH_MARKER in body
            assert "msg-1" in body


async def test_draft_reply_returns_mock_text_and_writes_audit_row(
    mcp_server_url: str, store: Store
) -> None:
    async with streamable_http_client(f"{mcp_server_url}/mcp") as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "draft_reply", {"chat_id": "chat-1", "instructions": "keep it short"}
            )
            assert result.isError is not True
            body = _text(result)
            assert "keep it short" in body

    audit_rows = store.list_audit(chat_id="chat-1")
    draft_rows = [row for row in audit_rows if row["action"] == "draft"]
    assert len(draft_rows) == 1
    assert draft_rows[0]["payload"]["instructions"] == "keep it short"
    assert "keep it short" in draft_rows[0]["payload"]["draft"]


async def test_send_message_is_gated_queued_and_never_sent(
    mcp_server_url: str, store: Store
) -> None:
    async with streamable_http_client(f"{mcp_server_url}/mcp") as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "send_message", {"chat_id": "chat-1", "text": "please send this"}
            )
            # Send gate: chat-1 has no policy, so the request parks in the
            # human-approval queue — a successful (non-error) result that
            # sends nothing.
            assert result.isError is not True
            assert "queued for human approval" in _text(result)

    pending = store.list_queued_sends()
    assert len(pending) == 1
    assert pending[0]["text"] == "please send this"
    assert pending[0]["status"] == "pending"

    audit_rows = store.list_audit(chat_id="chat-1")
    queued_rows = [row for row in audit_rows if row["action"] == "send_queued"]
    assert len(queued_rows) == 1
    assert queued_rows[0]["payload"]["text"] == "please send this"
    # No actual send was audited — nothing left the system (the server was
    # built with bridge=None; even an auto policy couldn't have sent).
    assert not [row for row in audit_rows if row["action"] == "send"]
