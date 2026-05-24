from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


_UNSET = object()


@dataclass(frozen=True)
class ThreadMessage:
    id: int
    thread_url: str
    direction: str
    text: str
    observed_at_utc: str


@dataclass
class ThreadState:
    thread_url: str
    last_seen_fingerprint: str | None
    last_seen_text: str | None
    last_activity_utc: str | None
    first_reply_sent: int

    # Robust dedupe fields
    last_replied_incoming_fingerprint: str | None
    last_replied_incoming_text: str | None
    last_reply_utc: str | None

    # Stable dedupe based on persisted message history IDs
    last_replied_incoming_msg_id: int | None

    last_attempt_incoming_fingerprint: str | None
    last_attempt_utc: str | None
    attempt_count: int

    last_attempt_incoming_msg_id: int | None


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_state (
                thread_url TEXT PRIMARY KEY,
                last_seen_fingerprint TEXT,
                last_seen_text TEXT,
                last_activity_utc TEXT,
                first_reply_sent INTEGER NOT NULL DEFAULT 0,
                updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self.conn.commit()

        # Lightweight migration: add new columns when missing.
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(thread_state)").fetchall()}
        migrations: list[str] = []

        def add_col(name: str, decl: str) -> None:
            if name not in existing:
                migrations.append(f"ALTER TABLE thread_state ADD COLUMN {name} {decl}")

        add_col("last_replied_incoming_fingerprint", "TEXT")
        add_col("last_replied_incoming_text", "TEXT")
        add_col("last_reply_utc", "TEXT")
        add_col("last_replied_incoming_msg_id", "INTEGER")
        add_col("last_attempt_incoming_fingerprint", "TEXT")
        add_col("last_attempt_utc", "TEXT")
        add_col("attempt_count", "INTEGER NOT NULL DEFAULT 0")
        add_col("last_attempt_incoming_msg_id", "INTEGER")

        for stmt in migrations:
            self.conn.execute(stmt)
        if migrations:
            self.conn.commit()

        # Message history table (append-only).
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_url TEXT NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_thread_messages_thread_id
            ON thread_messages(thread_url, id)
            """
        )
        self.conn.commit()

    def get_thread_state(self, thread_url: str) -> ThreadState:
        row = self.conn.execute(
            """
            SELECT
                thread_url,
                last_seen_fingerprint,
                last_seen_text,
                last_activity_utc,
                first_reply_sent,
                last_replied_incoming_fingerprint,
                last_replied_incoming_text,
                last_reply_utc,
                last_replied_incoming_msg_id,
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count,
                last_attempt_incoming_msg_id
            FROM thread_state
            WHERE thread_url = ?
            """,
            (thread_url,),
        ).fetchone()

        if row is None:
            return ThreadState(
                thread_url=thread_url,
                last_seen_fingerprint=None,
                last_seen_text=None,
                last_activity_utc=None,
                first_reply_sent=0,
                last_replied_incoming_fingerprint=None,
                last_replied_incoming_text=None,
                last_reply_utc=None,
                last_replied_incoming_msg_id=None,
                last_attempt_incoming_fingerprint=None,
                last_attempt_utc=None,
                attempt_count=0,
                last_attempt_incoming_msg_id=None,
            )

        return ThreadState(
            thread_url=row["thread_url"],
            last_seen_fingerprint=row["last_seen_fingerprint"],
            last_seen_text=row["last_seen_text"],
            last_activity_utc=row["last_activity_utc"],
            first_reply_sent=int(row["first_reply_sent"]),
            last_replied_incoming_fingerprint=row["last_replied_incoming_fingerprint"],
            last_replied_incoming_text=row["last_replied_incoming_text"],
            last_reply_utc=row["last_reply_utc"],
            last_replied_incoming_msg_id=(int(row["last_replied_incoming_msg_id"]) if row["last_replied_incoming_msg_id"] is not None else None),
            last_attempt_incoming_fingerprint=row["last_attempt_incoming_fingerprint"],
            last_attempt_utc=row["last_attempt_utc"],
            attempt_count=int(row["attempt_count"] or 0),
            last_attempt_incoming_msg_id=(int(row["last_attempt_incoming_msg_id"]) if row["last_attempt_incoming_msg_id"] is not None else None),
        )

    def upsert_thread_state(
        self,
        thread_url: str,
        last_seen_fingerprint: str | None,
        last_seen_text: str | None,
        last_activity_utc: str | None,
        first_reply_sent: int,
        last_replied_incoming_fingerprint: str | None = None,
        last_replied_incoming_text: str | None = None,
        last_reply_utc: str | None = None,
        last_replied_incoming_msg_id: int | None | object = _UNSET,
        last_attempt_incoming_fingerprint: str | None = None,
        last_attempt_utc: str | None = None,
        last_attempt_incoming_msg_id: int | None | object = _UNSET,
        attempt_count: int | None = None,
    ) -> None:
        # Keep some fields stable unless explicitly set.
        current_state = self.get_thread_state(thread_url)
        if attempt_count is None:
            attempt_count = current_state.attempt_count

        # Preserve dedupe IDs unless explicitly provided.
        if last_replied_incoming_msg_id is _UNSET:
            last_replied_incoming_msg_id = current_state.last_replied_incoming_msg_id
        if last_attempt_incoming_msg_id is _UNSET:
            last_attempt_incoming_msg_id = current_state.last_attempt_incoming_msg_id
        self.conn.execute(
            """
            INSERT INTO thread_state (
                thread_url,
                last_seen_fingerprint,
                last_seen_text,
                last_activity_utc,
                first_reply_sent,
                last_replied_incoming_fingerprint,
                last_replied_incoming_text,
                last_reply_utc,
                last_replied_incoming_msg_id,
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count,
                last_attempt_incoming_msg_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_url) DO UPDATE SET
                last_seen_fingerprint = excluded.last_seen_fingerprint,
                last_seen_text = excluded.last_seen_text,
                last_activity_utc = excluded.last_activity_utc,
                first_reply_sent = excluded.first_reply_sent,
                last_replied_incoming_fingerprint = excluded.last_replied_incoming_fingerprint,
                last_replied_incoming_text = excluded.last_replied_incoming_text,
                last_reply_utc = excluded.last_reply_utc,
                last_replied_incoming_msg_id = excluded.last_replied_incoming_msg_id,
                last_attempt_incoming_fingerprint = excluded.last_attempt_incoming_fingerprint,
                last_attempt_utc = excluded.last_attempt_utc,
                attempt_count = excluded.attempt_count,
                last_attempt_incoming_msg_id = excluded.last_attempt_incoming_msg_id,
                updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (
                thread_url,
                last_seen_fingerprint,
                last_seen_text,
                last_activity_utc,
                first_reply_sent,
                last_replied_incoming_fingerprint,
                last_replied_incoming_text,
                last_reply_utc,
                last_replied_incoming_msg_id,
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count,
                last_attempt_incoming_msg_id,
            ),
        )
        self.conn.commit()

    def get_latest_incoming_message(self, thread_url: str) -> ThreadMessage | None:
        row = self.conn.execute(
            """
            SELECT id, thread_url, direction, text, observed_at_utc
            FROM thread_messages
            WHERE thread_url = ?
              AND direction = 'incoming'
            ORDER BY id DESC
            LIMIT 1
            """,
            (thread_url,),
        ).fetchone()
        if row is None:
            return None
        return ThreadMessage(
            id=int(row["id"]),
            thread_url=str(row["thread_url"]),
            direction=str(row["direction"]),
            text=str(row["text"]),
            observed_at_utc=str(row["observed_at_utc"]),
        )

    def get_latest_outgoing_message(self, thread_url: str) -> ThreadMessage | None:
        row = self.conn.execute(
            """
            SELECT id, thread_url, direction, text, observed_at_utc
            FROM thread_messages
            WHERE thread_url = ?
              AND direction = 'outgoing'
            ORDER BY id DESC
            LIMIT 1
            """,
            (thread_url,),
        ).fetchone()
        if row is None:
            return None
        return ThreadMessage(
            id=int(row["id"]),
            thread_url=str(row["thread_url"]),
            direction=str(row["direction"]),
            text=str(row["text"]),
            observed_at_utc=str(row["observed_at_utc"]),
        )

    def append_thread_messages(
        self,
        thread_url: str,
        messages: list[tuple[str, str, str]],
        *,
        max_per_thread: int | None = None,
    ) -> int:
        """Append messages to the per-thread history.

        messages: list of (direction, text, observed_at_utc)
        Returns number of inserted rows.
        """

        rows = [
            (thread_url, (direction or "unknown"), (text or "").strip(), observed_at_utc)
            for direction, text, observed_at_utc in messages
            if (text or "").strip()
        ]
        if not rows:
            return 0

        self.conn.executemany(
            """
            INSERT INTO thread_messages (thread_url, direction, text, observed_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        inserted = len(rows)
        self.conn.commit()

        if max_per_thread is not None and max_per_thread > 0:
            # Keep only the most recent max_per_thread rows.
            self.conn.execute(
                """
                DELETE FROM thread_messages
                WHERE thread_url = ?
                  AND id NOT IN (
                    SELECT id FROM thread_messages
                    WHERE thread_url = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (thread_url, thread_url, int(max_per_thread)),
            )
            self.conn.commit()

        return inserted

    def get_recent_thread_messages(self, thread_url: str, limit: int) -> list[ThreadMessage]:
        if limit <= 0:
            return []
        rows = self.conn.execute(
            """
            SELECT id, thread_url, direction, text, observed_at_utc
            FROM thread_messages
            WHERE thread_url = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_url, int(limit)),
        ).fetchall()

        msgs = [
            ThreadMessage(
                id=int(r["id"]),
                thread_url=str(r["thread_url"]),
                direction=str(r["direction"]),
                text=str(r["text"]),
                observed_at_utc=str(r["observed_at_utc"]),
            )
            for r in rows
        ]
        msgs.reverse()  # chronological
        return msgs

    @staticmethod
    def _norm_message_key(direction: str, text: str) -> tuple[str, str]:
        d = (direction or "unknown").strip().lower()
        # Collapse whitespace for robust matching.
        t = " ".join((text or "").strip().split()).lower()
        return d, t

    def update_history_from_tail(
        self,
        thread_url: str,
        tail_messages: list[tuple[str, str]],
        observed_at_utc: str,
        *,
        max_per_thread: int | None = None,
    ) -> int:
        """Idempotently append unseen tail messages based on sequence overlap.

        This avoids relying on unstable DOM geometry for message identity.
        """

        tail = [(d, (t or "").strip()) for d, t in (tail_messages or []) if (t or "").strip()]
        if not tail:
            return 0

        tail_keys = [self._norm_message_key(d, t) for d, t in tail]
        m = len(tail_keys)

        # Load last m messages already stored for this thread (chronological).
        stored_rows = self.get_recent_thread_messages(thread_url, limit=m)
        stored_keys = [self._norm_message_key(r.direction, r.text) for r in stored_rows]

        max_k = min(len(stored_keys), m)
        overlap = 0
        for k in range(max_k, -1, -1):
            if k == 0:
                overlap = 0
                break
            if stored_keys[-k:] == tail_keys[:k]:
                overlap = k
                break

        new_tail = tail[overlap:]
        if not new_tail:
            return 0

        return self.append_thread_messages(
            thread_url,
            [(d, t, observed_at_utc) for d, t in new_tail],
            max_per_thread=max_per_thread,
        )

    def close(self) -> None:
        self.conn.close()
