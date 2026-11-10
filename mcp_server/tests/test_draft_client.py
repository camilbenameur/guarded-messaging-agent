"""`MockDraft` is deterministic and network-free — the default in tests and
wherever `ANTHROPIC_API_KEY` is unset. `build_default_draft_client` picks it
whenever the env var is absent.
"""

from __future__ import annotations

from mcp_server.draft_client import MockDraft, build_default_draft_client

_THREAD = [
    {"ts": 1, "sender_id": "alice", "direction": "in", "content_text": "hi"},
    {"ts": 2, "sender_id": "me", "direction": "out", "content_text": "hello"},
]


def test_mock_draft_reply_is_deterministic() -> None:
    client = MockDraft()
    first = client.draft_reply(_THREAD, "be polite")
    second = client.draft_reply(_THREAD, "be polite")
    assert first == second
    assert "be polite" in first.text
    assert str(len(_THREAD)) in first.text


def test_mock_analyze_and_summarize_are_deterministic() -> None:
    client = MockDraft()
    assert client.analyze(_THREAD) == client.analyze(_THREAD)
    assert client.summarize(_THREAD) == client.summarize(_THREAD)


def test_build_default_draft_client_is_mock_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(build_default_draft_client(), MockDraft)
