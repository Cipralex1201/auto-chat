#!/usr/bin/env python3
"""One-time utility to wipe polluted history from state.sqlite3.

Creates a timestamped backup before deleting rows.

Usage:
  python reset_history.py --all
  python reset_history.py --thread-url "https://www.instagram.com/direct/t/.../"

Optional:
  python reset_history.py --all --vacuum
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Counts:
    thread_messages: int
    thread_state: int


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _get_counts(conn: sqlite3.Connection) -> Counts:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM thread_messages")
    msg = int(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM thread_state")
    st = int(cur.fetchone()[0])
    return Counts(thread_messages=msg, thread_state=st)


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backups_dir / f"{db_path.name}.{_utc_stamp()}.bak"
    shutil.copy2(db_path, backup_path)
    return backup_path


def _wipe_all(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM thread_messages")
    cur.execute("DELETE FROM thread_state")
    cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('thread_messages')")


def _wipe_thread(conn: sqlite3.Connection, thread_url: str) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM thread_messages WHERE thread_url = ?", (thread_url,))
    cur.execute("DELETE FROM thread_state WHERE thread_url = ?", (thread_url,))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup and wipe history from state.sqlite3")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Wipe all stored thread history")
    scope.add_argument("--thread-url", type=str, help="Wipe only this thread_url")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("state.sqlite3"),
        help="Path to sqlite DB (default: state.sqlite3)",
    )
    parser.add_argument(
        "--backups-dir",
        type=Path,
        default=Path("logs") / "db_backups",
        help="Where to write backups (default: logs/db_backups)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM after deletion (can take a moment)",
    )
    args = parser.parse_args()

    db_path: Path = args.db
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    backup_path = _backup_db(db_path, args.backups_dir)
    print(f"Backup created: {backup_path}")

    conn = sqlite3.connect(db_path)
    try:
        before = _get_counts(conn)
        print(f"Before: thread_messages={before.thread_messages}, thread_state={before.thread_state}")

        conn.execute("BEGIN")
        if args.all:
            _wipe_all(conn)
            print("Wiped: ALL threads")
        else:
            _wipe_thread(conn, args.thread_url)
            print(f"Wiped: thread_url={args.thread_url}")
        conn.commit()

        if args.vacuum:
            conn.execute("VACUUM")

        after = _get_counts(conn)
        print(f"After:  thread_messages={after.thread_messages}, thread_state={after.thread_state}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
