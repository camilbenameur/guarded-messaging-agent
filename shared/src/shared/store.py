"""SQLite-backed persistence layer for the messaging-agent MVP.

`messages.content_text` is NOT NULL and always
populated (raw text | transcript | caption | placeholder) so the agent is
never "blind" to a modality it can't read directly.

`messages_fts` is an FTS5 *external-content* table over `messages`. External
content tables do not stay in sync automatically — the three triggers below
(AFTER INSERT / AFTER UPDATE OF content_text / AFTER DELETE, delete+reinsert
on update) are load-bearing: without them `search()` silently returns nothing.

Connection strategy: one short-lived `sqlite3.Connection` per call (opened,
used, closed), serialized by an in-process lock, with WAL journaling enabled.
This sidesteps `sqlite3`'s same-thread default without sharing a connection
across threads, at the cost of a little overhead — an acceptable trade for
this MVP's write volume.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_DB_PATH = "/store/messages.db"

Direction = Literal["in", "out"]
Modality = Literal["text", "audio", "image", "doc", "video"]


@dataclass(frozen=True)
class Message:
    """One row of `messages`. `content_text` must always be populated."""

    id: str
    chat_id: str
    sender_id: str
    ts: int
    direction: Direction
    modality: Modality
    content_text: str
    media_path: str | None = None
    media_meta: dict[str, Any] | None = None
    processed: int = 0


# Ordered list of migration scripts. Each entry is applied exactly once,
# tracked via `PRAGMA user_version` (index + 1 == the version it produces).
# `init()` is safe to call repeatedly: only migrations above the current
# version are ever (re-)applied, and every DDL statement is defensively
# idempotent (`IF NOT EXISTS`) in case a partial run left the pragma stale.
_MIGRATIONS: list[str] = [
    # v1 — initial schema: messages, chats, audit_log, messages_fts + sync triggers.
    """
    CREATE TABLE IF NOT EXISTS messages (
        id            TEXT PRIMARY KEY,
        chat_id       TEXT NOT NULL,
        sender_id     TEXT NOT NULL,
        ts            INTEGER NOT NULL,
        direction     TEXT NOT NULL,
        modality      TEXT NOT NULL,
        content_text  TEXT NOT NULL,
        media_path    TEXT,
        media_meta    JSON,
        processed     INTEGER DEFAULT 0
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
        USING fts5(content_text, content='messages', content_rowid='rowid');

    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content_text) VALUES (new.rowid, new.content_text);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content_text)
            VALUES ('delete', old.rowid, old.content_text);
    END;
    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content_text ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content_text)
            VALUES ('delete', old.rowid, old.content_text);
        INSERT INTO messages_fts(rowid, content_text) VALUES (new.rowid, new.content_text);
    END;

    CREATE TABLE IF NOT EXISTS chats (
        chat_id     TEXT PRIMARY KEY,
        name        TEXT,
        is_group    INTEGER,
        policy      JSON
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          INTEGER,
        action      TEXT,
        chat_id     TEXT,
        payload     JSON,
        approved_by TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);
    CREATE INDEX IF NOT EXISTS idx_audit_log_chat_id ON audit_log(chat_id);
    """,
    # v2 — ingestion cursor storage. Lets `ingest.Worker` persist "last event id
    # seen" per cursor key so a restart resumes instead of re-polling from
    # scratch (the bridge itself is stateless about what's already ingested).
    """
    CREATE TABLE IF NOT EXISTS ingest_cursor (
        key            TEXT PRIMARY KEY,
        last_event_id  TEXT NOT NULL
    );
    """,
    # v3 — human-approval queue for gated sends. Every `send_message` that doesn't
    # clear auto-policy is parked here as `status='pending'` until a human
    # releases (`approved`) or rejects it via `mcp_server.gate.SendGate` /
    # `python -m mcp_server.approve`.
    """
    CREATE TABLE IF NOT EXISTS send_queue (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id          TEXT NOT NULL,
        text             TEXT NOT NULL,
        reason           TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        created_ts       INTEGER NOT NULL,
        resolved_ts      INTEGER,
        approved_by      TEXT,
        sent_message_id  TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_send_queue_status ON send_queue(status);
    """,
]


class Store:
    """Typed DAL over the SQLite store. Not thread-per-connection: every
    public method opens, uses, and closes its own connection under a lock.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init()

    # -- connection / migrations ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                yield cur
                conn.commit()
            finally:
                conn.close()

    def init(self) -> None:
        """Bring the database up to the latest schema version. Idempotent —
        safe to call on every process start, including against a database
        already at the latest version.
        """
        with self._lock:
            conn = self._connect()
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                target = len(_MIGRATIONS)
                for version in range(current, target):
                    conn.executescript(_MIGRATIONS[version])
                    conn.execute(f"PRAGMA user_version = {version + 1}")
                conn.commit()
            finally:
                conn.close()

    # -- row <-> dict helpers -----------------------------------------------

    @staticmethod
    def _message_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("media_meta") is not None:
            d["media_meta"] = json.loads(d["media_meta"])
        return d

    @staticmethod
    def _chat_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("policy") is not None:
            d["policy"] = json.loads(d["policy"])
        if d.get("is_group") is not None:
            d["is_group"] = bool(d["is_group"])
        return d

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("payload") is not None:
            d["payload"] = json.loads(d["payload"])
        return d

    # -- messages -------------------------------------------------------------

    def upsert_message(self, message: Message) -> bool:
        """Insert a message keyed by its (transport-unique) id. A message
        with an id that already exists is a no-op — re-ingesting the same
        bridge event must not duplicate rows or clobber existing content.
        Returns True if a new row was inserted, False if it already existed.
        """
        media_meta_json = json.dumps(message.media_meta) if message.media_meta is not None else None
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, chat_id, sender_id, ts, direction, modality,
                     content_text, media_path, media_meta, processed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.chat_id,
                    message.sender_id,
                    message.ts,
                    message.direction,
                    message.modality,
                    message.content_text,
                    message.media_path,
                    media_meta_json,
                    message.processed,
                ),
            )
            return cur.rowcount > 0

    def mark_processed(self, message_id: str, processed: int = 1) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE messages SET processed = ? WHERE id = ?", (processed, message_id))

    def update_content_text(self, message_id: str, content_text: str) -> None:
        """Overwrite `content_text` (e.g. once a voice note's transcript or an
        image's caption becomes available after initial ingestion). Relies on
        the `messages_au` trigger to keep `messages_fts` in sync.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE messages SET content_text = ? WHERE id = ?",
                (content_text, message_id),
            )

    def finish_processing(
        self, message_id: str, content_text: str, media_meta: dict[str, Any] | None = None
    ) -> None:
        """Atomically set the derived `content_text` (transcript or caption),
        optionally refresh `media_meta`, and mark the row `processed = 1`.
        Used by the ingestion worker's derivation pass once a
        configured STT/vision hook produces a result. Relies on the `messages_au` trigger
        for FTS sync, same as `update_content_text`; omitting `media_meta`
        leaves the existing value untouched rather than blanking it.
        """
        with self._cursor() as cur:
            if media_meta is not None:
                cur.execute(
                    "UPDATE messages SET content_text = ?, media_meta = ?, processed = 1 "
                    "WHERE id = ?",
                    (content_text, json.dumps(media_meta), message_id),
                )
            else:
                cur.execute(
                    "UPDATE messages SET content_text = ?, processed = 1 WHERE id = ?",
                    (content_text, message_id),
                )

    def list_pending(
        self, modality: Modality | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Rows with `processed = 0`, oldest first, optionally scoped to one
        `modality`. Used by the ingestion worker's derivation pass to
        find audio/image rows still awaiting STT/vision.
        """
        sql = "SELECT * FROM messages WHERE processed = 0"
        params: list[Any] = []
        if modality is not None:
            sql += " AND modality = ?"
            params.append(modality)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._message_row(r) for r in rows]

    def reset_processed(self, modality: Modality) -> int:
        """Reset every row of `modality` back to `processed = 0` — backs the
        ingestion worker's `--reprocess` backfill: recompute derived
        text for a modality after a prompt/model change. Returns the number
        of rows touched.
        """
        with self._cursor() as cur:
            cur.execute("UPDATE messages SET processed = 0 WHERE modality = ?", (modality,))
            return cur.rowcount

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._message_row(row) if row is not None else None

    def list_messages(
        self, chat_id: str, before: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Most recent messages in a chat, newest first, optionally paginated
        with `before` (exclusive upper bound on `ts`).
        """
        sql = "SELECT * FROM messages WHERE chat_id = ?"
        params: list[Any] = [chat_id]
        if before is not None:
            sql += " AND ts < ?"
            params.append(before)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._message_row(r) for r in rows]

    def get_context(self, message_id: str, radius: int = 3) -> list[dict[str, Any]]:
        """`radius` messages before and after `message_id` in the same chat,
        chronologically ordered, including the anchor message itself. Empty
        list if `message_id` doesn't exist.
        """
        with self._cursor() as cur:
            anchor = cur.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if anchor is None:
                return []
            chat_id, ts = anchor["chat_id"], anchor["ts"]
            before_rows = cur.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND (ts < ? OR (ts = ? AND id < ?))
                ORDER BY ts DESC, id DESC LIMIT ?
                """,
                (chat_id, ts, ts, message_id, radius),
            ).fetchall()
            after_rows = cur.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND (ts > ? OR (ts = ? AND id > ?))
                ORDER BY ts ASC, id ASC LIMIT ?
                """,
                (chat_id, ts, ts, message_id, radius),
            ).fetchall()
        ordered = list(reversed(before_rows)) + [anchor] + list(after_rows)
        return [self._message_row(r) for r in ordered]

    _FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

    @classmethod
    def _fts_query(cls, raw_query: str) -> str | None:
        """Turn arbitrary user text into a safe FTS5 MATCH expression.

        FTS5's query syntax gives special meaning to characters like `-`,
        `:`, `*`, and `"`, so passing search text straight through raises
        `OperationalError` on anything containing punctuation (hyphenated
        words, URLs, ...). Splitting into word tokens and quoting each one
        individually keeps "match all these terms" semantics while treating
        the input as inert data instead of query syntax. Returns None if the
        query has no tokenizable content.
        """
        tokens = cls._FTS_TOKEN_RE.findall(raw_query)
        if not tokens:
            return None
        return " ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)

    def search(
        self, query: str, chat_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Full-text search over `content_text` (raw text, transcripts, and
        image captions alike) via the `messages_fts` external-content index.
        Includes a `snippet` field highlighting the match.
        """
        fts_query = self._fts_query(query)
        if fts_query is None:
            return []
        sql = (
            "SELECT m.*, snippet(messages_fts, 0, '[', ']', ' … ', 10) AS snippet "
            "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            "WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [fts_query]
        if chat_id is not None:
            sql += " AND m.chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY messages_fts.rank LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        results = []
        for row in rows:
            d = self._message_row(row)
            d["snippet"] = row["snippet"]
            results.append(d)
        return results

    # -- chats ------------------------------------------------------------------

    def upsert_chat(
        self, chat_id: str, name: str | None = None, is_group: bool | None = None
    ) -> None:
        """Create or refresh a chat's identity fields. Never touches `policy`
        (use `set_policy` for that) and never blanks a field the caller
        didn't supply.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO chats (chat_id, name, is_group) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    name = COALESCE(excluded.name, chats.name),
                    is_group = COALESCE(excluded.is_group, chats.is_group)
                """,
                (chat_id, name, None if is_group is None else int(is_group)),
            )

    def list_chats(self) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT chat_id, name, is_group, policy FROM chats ORDER BY chat_id"
            ).fetchall()
        return [self._chat_row(r) for r in rows]

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        """One chat row (or None). The send gate needs a chat's `is_group` flag without paging
        through `list_chats`.
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT chat_id, name, is_group, policy FROM chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return self._chat_row(row) if row is not None else None

    def get_policy(self, chat_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT policy FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None or row["policy"] is None:
            return None
        return json.loads(row["policy"])

    def set_policy(self, chat_id: str, policy: dict[str, Any]) -> None:
        """Store the automation policy JSON for a chat (storage only — policy
        *evaluation* lives in `mcp_server.policy`). Creates the chat row if needed.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO chats (chat_id, policy) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET policy = excluded.policy
                """,
                (chat_id, json.dumps(policy)),
            )

    # -- audit log ----------------------------------------------------------

    def append_audit(
        self,
        action: str,
        chat_id: str | None,
        payload: dict[str, Any] | None,
        approved_by: str | None = None,
        ts: int | None = None,
    ) -> int:
        """Record an agent action. Every action the agent takes is traced
        here — 'draft' | 'send' | 'read_batch' | ... — with who approved it
        ('human' | 'auto-policy' | None).
        """
        row_ts = int(time.time()) if ts is None else ts
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (ts, action, chat_id, payload, approved_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row_ts,
                    action,
                    chat_id,
                    json.dumps(payload) if payload is not None else None,
                    approved_by,
                ),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None  # INSERT always assigns a rowid
            return lastrowid

    def list_audit(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if chat_id is not None:
            sql += " WHERE chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._audit_row(r) for r in rows]

    def count_audit(
        self,
        action: str,
        chat_id: str | None = None,
        approved_by: str | None = None,
        since_ts: int | None = None,
    ) -> int:
        """Count audit rows matching the filters. Backs the send gate's `max_auto_per_hour`
        rate limiting — auto-approved sends are counted straight off the
        audit trail (`action='send'`, `approved_by='auto-policy'`,
        `ts >= since_ts`), so the limit survives restarts and stays
        deterministic under an injected clock.
        """
        sql = "SELECT COUNT(*) FROM audit_log WHERE action = ?"
        params: list[Any] = [action]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if approved_by is not None:
            sql += " AND approved_by = ?"
            params.append(approved_by)
        if since_ts is not None:
            sql += " AND ts >= ?"
            params.append(since_ts)
        with self._cursor() as cur:
            return int(cur.execute(sql, params).fetchone()[0])

    # -- send queue (see `send_queue` migration above) ----------

    def enqueue_send(
        self, chat_id: str, text: str, reason: str | None = None, ts: int | None = None
    ) -> int:
        """Park a send request as `status='pending'` and return its queue id.
        Only `mcp_server.gate.SendGate` writes here — the queue is the
        human-approval half of the send gate.
        """
        row_ts = int(time.time()) if ts is None else ts
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO send_queue (chat_id, text, reason, status, created_ts)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (chat_id, text, reason, row_ts),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None  # INSERT always assigns a rowid
            return lastrowid

    def get_queued_send(self, queue_id: int) -> dict[str, Any] | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM send_queue WHERE id = ?", (queue_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_queued_sends(
        self, status: str | None = "pending", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Queued sends, oldest first, filtered to one `status` by default
        (`'pending'`); pass `status=None` for the full history.
        """
        sql = "SELECT * FROM send_queue"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def resolve_queued_send(
        self,
        queue_id: int,
        status: str,
        approved_by: str | None = None,
        sent_message_id: str | None = None,
        ts: int | None = None,
    ) -> bool:
        """Transition one *pending* queue row to `status` ('approved' |
        'rejected'). The `AND status = 'pending'` guard makes the transition
        atomic — a row can only ever be resolved once; returns False if it
        was already resolved (or the id is unknown).
        """
        row_ts = int(time.time()) if ts is None else ts
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE send_queue
                SET status = ?, approved_by = ?, sent_message_id = ?, resolved_ts = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, approved_by, sent_message_id, row_ts, queue_id),
            )
            return cur.rowcount > 0

    # -- ingest cursor (see `ingest_cursor` migration above) ----

    def get_cursor(self, key: str = "default") -> str | None:
        """Last bridge event id the ingestion worker has consumed for `key`
        (one worker/bridge pairing per key; `"default"` covers the MVP's
        single bridge). `None` means "poll from the start".
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT last_event_id FROM ingest_cursor WHERE key = ?", (key,)
            ).fetchone()
        return row["last_event_id"] if row is not None else None

    def set_cursor(self, key: str, last_event_id: str) -> None:
        """Persist `last_event_id` as the resume point for `key`.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_cursor (key, last_event_id) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET last_event_id = excluded.last_event_id
                """,
                (key, last_event_id),
            )
