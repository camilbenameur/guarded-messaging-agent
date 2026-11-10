"""Tests for shared.store.Store — schema, migrations, FTS5 sync triggers, DAL."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.store import Message, Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "messages.db")


def _msg(**overrides: object) -> Message:
    base: dict[str, object] = dict(
        id="msg-1",
        chat_id="chat-1",
        sender_id="user-1",
        ts=1_700_000_000,
        direction="in",
        modality="text",
        content_text="hello world",
    )
    base.update(overrides)
    return Message(**base)  # type: ignore[arg-type]


# -- schema / migrations -----------------------------------------------------


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "messages.db"
    store1 = Store(db_path)
    store1.upsert_message(_msg())

    # Re-running init (fresh Store instance against the same file) must not
    # error, drop tables, or lose data.
    store1.init()
    store2 = Store(db_path)
    store2.init()

    assert store2.get_message("msg-1") is not None


def test_migration_runner_does_not_duplicate_triggers(store: Store) -> None:
    # Calling init() repeatedly on the same instance must stay a no-op even
    # though the trigger/table DDL runs again (IF NOT EXISTS everywhere).
    store.init()
    store.init()
    store.upsert_message(_msg())
    assert store.search("hello") != []


# -- messages + FTS5 search --------------------------------------------------


def test_search_finds_text_audio_and_image_messages(store: Store) -> None:
    store.upsert_message(
        _msg(id="t1", modality="text", content_text="let's grab coffee tomorrow")
    )
    store.upsert_message(
        _msg(
            id="a1",
            modality="audio",
            content_text="transcript: I will call you about the coffee order",
            media_meta={"duration": 12, "lang": "en"},
        )
    )
    store.upsert_message(
        _msg(
            id="i1",
            modality="image",
            content_text="[image] a photo of a coffee cup on a table",
            media_meta={"width": 800, "height": 600},
        )
    )

    results = store.search("coffee")
    ids = {r["id"] for r in results}
    assert ids == {"t1", "a1", "i1"}
    for r in results:
        assert "snippet" in r
        assert r["content_text"]  # never blank, per the content_text contract


def test_search_deserializes_media_meta(store: Store) -> None:
    store.upsert_message(
        _msg(
            id="a1",
            modality="audio",
            content_text="talking about dogs",
            media_meta={"duration": 5},
        )
    )
    results = store.search("dogs")
    assert results[0]["media_meta"] == {"duration": 5}


def test_search_can_filter_by_chat(store: Store) -> None:
    store.upsert_message(_msg(id="m1", chat_id="chat-a", content_text="unique-marker-xyz"))
    store.upsert_message(_msg(id="m2", chat_id="chat-b", content_text="unique-marker-xyz"))

    assert {r["id"] for r in store.search("unique-marker-xyz")} == {"m1", "m2"}
    assert {r["id"] for r in store.search("unique-marker-xyz", chat_id="chat-a")} == {"m1"}


def test_update_content_text_then_search_finds_new_text(store: Store) -> None:
    """Proves the AFTER UPDATE OF content_text trigger: a voice note ingested
    with a placeholder, then updated once transcription completes, must
    become findable under the *new* text (and not the old one).
    """
    store.upsert_message(
        _msg(id="voice-1", modality="audio", content_text="[audio] pending transcription")
    )
    assert store.search("pending") != []
    assert store.search("bonjour") == []

    store.update_content_text("voice-1", "bonjour comment vas-tu")

    assert store.search("bonjour") != []
    assert store.search("pending") == []
    assert store.get_message("voice-1")["content_text"] == "bonjour comment vas-tu"


def test_duplicate_upsert_message_is_noop(store: Store) -> None:
    first = _msg(id="dup-1", content_text="original content")
    second = _msg(id="dup-1", content_text="this should never be stored")

    inserted_first = store.upsert_message(first)
    inserted_second = store.upsert_message(second)

    assert inserted_first is True
    assert inserted_second is False
    assert store.get_message("dup-1")["content_text"] == "original content"
    assert store.search("original") != []
    assert store.search("never") == []


def test_mark_processed(store: Store) -> None:
    store.upsert_message(_msg(id="p1", processed=0))
    assert store.get_message("p1")["processed"] == 0
    store.mark_processed("p1", 1)
    assert store.get_message("p1")["processed"] == 1


def test_list_messages_pagination(store: Store) -> None:
    for i in range(5):
        store.upsert_message(_msg(id=f"m{i}", ts=1_700_000_000 + i))

    newest_two = store.list_messages("chat-1", limit=2)
    assert [m["id"] for m in newest_two] == ["m4", "m3"]

    older = store.list_messages("chat-1", before=1_700_000_002, limit=10)
    assert [m["id"] for m in older] == ["m1", "m0"]


def test_get_context_returns_radius_around_anchor(store: Store) -> None:
    for i in range(7):
        store.upsert_message(_msg(id=f"c{i}", ts=1_700_000_000 + i))

    context = store.get_context("c3", radius=2)
    assert [m["id"] for m in context] == ["c1", "c2", "c3", "c4", "c5"]


def test_get_context_missing_message_returns_empty(store: Store) -> None:
    assert store.get_context("does-not-exist") == []


# -- chats + policy -----------------------------------------------------------


def test_list_chats_and_upsert_chat(store: Store) -> None:
    store.upsert_chat("chat-1", name="Alice", is_group=False)
    store.upsert_chat("chat-2", name="Family Group", is_group=True)

    chats = store.list_chats()
    assert {c["chat_id"] for c in chats} == {"chat-1", "chat-2"}
    alice = next(c for c in chats if c["chat_id"] == "chat-1")
    assert alice["name"] == "Alice"
    assert alice["is_group"] is False


def test_get_and_set_policy(store: Store) -> None:
    assert store.get_policy("chat-1") is None

    store.set_policy("chat-1", {"auto_reply": False, "rate_limit_per_hour": 3})
    assert store.get_policy("chat-1") == {"auto_reply": False, "rate_limit_per_hour": 3}

    # set_policy again must update, not duplicate the chat row.
    store.set_policy("chat-1", {"auto_reply": True})
    assert store.get_policy("chat-1") == {"auto_reply": True}
    assert len(store.list_chats()) == 1


# -- audit log ----------------------------------------------------------------


def test_append_and_list_audit_round_trip(store: Store) -> None:
    audit_id = store.append_audit(
        action="draft",
        chat_id="chat-1",
        payload={"text": "proposed reply"},
        approved_by=None,
    )
    assert isinstance(audit_id, int)

    entries = store.list_audit(chat_id="chat-1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "draft"
    assert entry["chat_id"] == "chat-1"
    assert entry["payload"] == {"text": "proposed reply"}
    assert entry["approved_by"] is None


def test_list_audit_filters_by_chat(store: Store) -> None:
    store.append_audit("send", "chat-1", {"text": "hi"}, approved_by="human")
    store.append_audit("send", "chat-2", {"text": "hey"}, approved_by="human")

    assert len(store.list_audit(chat_id="chat-1")) == 1
    assert len(store.list_audit()) == 2
