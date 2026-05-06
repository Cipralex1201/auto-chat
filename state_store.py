from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


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

    last_attempt_incoming_fingerprint: str | None
    last_attempt_utc: str | None
    attempt_count: int


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
        add_col("last_attempt_incoming_fingerprint", "TEXT")
        add_col("last_attempt_utc", "TEXT")
        add_col("attempt_count", "INTEGER NOT NULL DEFAULT 0")

        for stmt in migrations:
            self.conn.execute(stmt)
        if migrations:
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
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count
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
                last_attempt_incoming_fingerprint=None,
                last_attempt_utc=None,
                attempt_count=0,
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
            last_attempt_incoming_fingerprint=row["last_attempt_incoming_fingerprint"],
            last_attempt_utc=row["last_attempt_utc"],
            attempt_count=int(row["attempt_count"] or 0),
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
        last_attempt_incoming_fingerprint: str | None = None,
        last_attempt_utc: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        # Keep attempt_count stable unless explicitly set.
        if attempt_count is None:
            attempt_count = self.get_thread_state(thread_url).attempt_count
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
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_url) DO UPDATE SET
                last_seen_fingerprint = excluded.last_seen_fingerprint,
                last_seen_text = excluded.last_seen_text,
                last_activity_utc = excluded.last_activity_utc,
                first_reply_sent = excluded.first_reply_sent,
                last_replied_incoming_fingerprint = excluded.last_replied_incoming_fingerprint,
                last_replied_incoming_text = excluded.last_replied_incoming_text,
                last_reply_utc = excluded.last_reply_utc,
                last_attempt_incoming_fingerprint = excluded.last_attempt_incoming_fingerprint,
                last_attempt_utc = excluded.last_attempt_utc,
                attempt_count = excluded.attempt_count,
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
                last_attempt_incoming_fingerprint,
                last_attempt_utc,
                attempt_count,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
