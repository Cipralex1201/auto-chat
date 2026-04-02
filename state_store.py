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

    def get_thread_state(self, thread_url: str) -> ThreadState:
        row = self.conn.execute(
            "SELECT thread_url, last_seen_fingerprint, last_seen_text, last_activity_utc, first_reply_sent FROM thread_state WHERE thread_url = ?",
            (thread_url,),
        ).fetchone()

        if row is None:
            return ThreadState(
                thread_url=thread_url,
                last_seen_fingerprint=None,
                last_seen_text=None,
                last_activity_utc=None,
                first_reply_sent=0,
            )

        return ThreadState(
            thread_url=row["thread_url"],
            last_seen_fingerprint=row["last_seen_fingerprint"],
            last_seen_text=row["last_seen_text"],
            last_activity_utc=row["last_activity_utc"],
            first_reply_sent=int(row["first_reply_sent"]),
        )

    def upsert_thread_state(
        self,
        thread_url: str,
        last_seen_fingerprint: str | None,
        last_seen_text: str | None,
        last_activity_utc: str | None,
        first_reply_sent: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO thread_state (thread_url, last_seen_fingerprint, last_seen_text, last_activity_utc, first_reply_sent)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_url) DO UPDATE SET
                last_seen_fingerprint = excluded.last_seen_fingerprint,
                last_seen_text = excluded.last_seen_text,
                last_activity_utc = excluded.last_activity_utc,
                first_reply_sent = excluded.first_reply_sent,
                updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (thread_url, last_seen_fingerprint, last_seen_text, last_activity_utc, first_reply_sent),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
