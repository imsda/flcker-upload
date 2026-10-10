"""SQLite persistence."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import DriveFile, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_files (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 google_drive_file_id TEXT NOT NULL UNIQUE,
 google_drive_name TEXT NOT NULL,
 google_drive_path TEXT,
 checksum TEXT,
 drive_created_at TEXT,
 drive_modified_at TEXT,
 media_captured_at TEXT,
 calendar_event_id TEXT,
 calendar_event_title TEXT,
 flickr_photo_id TEXT,
 flickr_photoset_id TEXT,
 status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photosets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 normalized_name TEXT NOT NULL UNIQUE,
 flickr_photoset_id TEXT NOT NULL,
 display_name TEXT NOT NULL,
 calendar_event_id TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 google_drive_file_id TEXT,
 status TEXT NOT NULL,
 message TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
 state TEXT PRIMARY KEY,
 provider TEXT NOT NULL,
 verifier TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_files_status ON processed_files(status);
CREATE INDEX IF NOT EXISTS idx_processed_files_updated_at ON processed_files(updated_at);
"""

# Statuses the scanner never picks up on its own; an operator queues them for retry.
TERMINAL_STATUSES = frozenset({Status.COMPLETE, Status.FAILED, Status.MANUAL_REVIEW})


def now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # The web UI and worker share this file; wait for locks instead of failing fast.
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(processed_files)")}
            if "next_retry_at" not in columns:
                conn.execute("ALTER TABLE processed_files ADD COLUMN next_retry_at TEXT")

    def is_processed(self, drive_file_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM processed_files WHERE google_drive_file_id=?", (drive_file_id,)).fetchone()
            return bool(row and row["status"] == Status.COMPLETE)

    def file_states(self) -> dict[str, sqlite3.Row]:
        """Load every tracked file's processing state in one query for a scan."""
        with self.connect() as conn:
            rows = conn.execute("SELECT google_drive_file_id, status, attempts, next_retry_at, flickr_photo_id, media_captured_at FROM processed_files").fetchall()
        return {row["google_drive_file_id"]: row for row in rows}

    def get_file(self, file_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row: sqlite3.Row | None = conn.execute("SELECT * FROM processed_files WHERE google_drive_file_id=?", (file_id,)).fetchone()
            return row

    def upsert_discovered(self, f: DriveFile) -> None:
        ts = now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO processed_files (google_drive_file_id, google_drive_name, google_drive_path, checksum, drive_created_at, drive_modified_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(google_drive_file_id) DO UPDATE SET google_drive_name=excluded.google_drive_name, checksum=excluded.checksum, drive_modified_at=excluded.drive_modified_at, updated_at=excluded.updated_at""",
                (f.id, f.name, f.path, f.checksum, f.created_at.isoformat(), f.modified_at.isoformat(), Status.DISCOVERED, ts, ts),
            )

    def update_status(self, file_id: str, status: Status, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET status=?, last_error=?, updated_at=? WHERE google_drive_file_id=?", (status, error, now(), file_id))
            conn.execute("INSERT INTO processing_log (google_drive_file_id, status, message, created_at) VALUES (?, ?, ?, ?)", (file_id, status, error, now()))

    def increment_attempts(self, file_id: str, error: str) -> int:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET attempts=attempts+1, status=?, last_error=?, updated_at=? WHERE google_drive_file_id=?", (Status.RETRY, error, now(), file_id))
            row = conn.execute("SELECT attempts FROM processed_files WHERE google_drive_file_id=?", (file_id,)).fetchone()
            return int(row["attempts"])

    def schedule_retry(self, file_id: str, delay: timedelta, message: str | None = None) -> None:
        """Mark a file for retry no earlier than ``delay`` from now."""
        retry_at = (datetime.now(UTC) + delay).isoformat()
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET status=?, next_retry_at=?, last_error=?, updated_at=? WHERE google_drive_file_id=?", (Status.RETRY, retry_at, message, now(), file_id))
            conn.execute("INSERT INTO processing_log (google_drive_file_id, status, message, created_at) VALUES (?, ?, ?, ?)", (file_id, Status.RETRY, message, now()))

    def set_photo_id(self, file_id: str, photo_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET flickr_photo_id=?, updated_at=? WHERE google_drive_file_id=?", (photo_id, now(), file_id))

    def unfinished_without_photo(self) -> list[str]:
        """Files that may have reached Flickr without the upload being recorded."""
        with self.connect() as conn:
            rows = conn.execute("SELECT google_drive_file_id FROM processed_files WHERE flickr_photo_id IS NULL AND status NOT IN (?, ?) AND (attempts > 0 OR status=?)", (Status.COMPLETE, Status.DISCOVERED, Status.UPLOADING)).fetchall()
        return [row["google_drive_file_id"] for row in rows]

    def queue_retry(self, file_id: str) -> bool:
        retryable = (Status.RETRY, Status.FAILED, Status.MANUAL_REVIEW)
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE processed_files SET status=?, attempts=0, last_error=NULL, next_retry_at=NULL, updated_at=? WHERE google_drive_file_id=? AND status IN (?,?,?)",
                (Status.RETRY, now(), file_id, *retryable),
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO processing_log (google_drive_file_id, status, message, created_at) VALUES (?, ?, ?, ?)",
                    (file_id, Status.RETRY, "Manual retry queued from web UI", now()),
                )
            return bool(cursor.rowcount)

    def queue_all_failed(self) -> int:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT google_drive_file_id FROM processed_files WHERE status=?",
                (Status.FAILED,),
            ).fetchall()
            if not rows:
                return 0
            timestamp = now()
            conn.execute(
                "UPDATE processed_files SET status=?, attempts=0, last_error=NULL, next_retry_at=NULL, updated_at=? WHERE status=?",
                (Status.RETRY, timestamp, Status.FAILED),
            )
            conn.executemany(
                "INSERT INTO processing_log (google_drive_file_id, status, message, created_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        row["google_drive_file_id"],
                        Status.RETRY,
                        "Bulk retry queued from web UI",
                        timestamp,
                    )
                    for row in rows
                ],
            )
            return len(rows)

    def mark_uploaded(self, file_id: str, captured: datetime, event_id: str | None, event_title: str | None, photo_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET media_captured_at=?, calendar_event_id=?, calendar_event_title=?, flickr_photo_id=?, status=?, updated_at=? WHERE google_drive_file_id=?", (captured.isoformat(), event_id, event_title, photo_id, Status.UPLOADED, now(), file_id))

    def mark_complete(self, file_id: str, photoset_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET flickr_photoset_id=?, status=?, last_error=NULL, next_retry_at=NULL, updated_at=? WHERE google_drive_file_id=?", (photoset_id, Status.COMPLETE, now(), file_id))

    def get_photoset(self, normalized_name: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT flickr_photoset_id FROM photosets WHERE normalized_name=?", (normalized_name,)).fetchone()
            return None if row is None else str(row["flickr_photoset_id"])

    def save_photoset(self, normalized_name: str, photoset_id: str, display_name: str, event_id: str | None = None) -> None:
        ts = now()
        with self.connect() as conn:
            conn.execute("""INSERT INTO photosets (normalized_name, flickr_photoset_id, display_name, calendar_event_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(normalized_name) DO UPDATE SET flickr_photoset_id=excluded.flickr_photoset_id, updated_at=excluded.updated_at""", (normalized_name, photoset_id, display_name, event_id, ts, ts))

    def delete_photoset(self, normalized_name: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM photosets WHERE normalized_name=?", (normalized_name,))

    def save_oauth_state(self, state: str, provider: str, verifier: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO oauth_states (state, provider, verifier, created_at) VALUES (?, ?, ?, ?)", (state, provider, verifier, now()))

    def pop_oauth_state(self, state: str, provider: str) -> tuple[bool, str | None]:
        with self.connect() as conn:
            row = conn.execute("SELECT verifier FROM oauth_states WHERE state=? AND provider=?", (state, provider)).fetchone()
            conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
            return (False, None) if row is None else (True, row["verifier"])

    def status_counts(self) -> list[tuple[str, int]]:
        with self.connect() as conn:
            return [(r["status"], r["count"]) for r in conn.execute("SELECT status, COUNT(*) count FROM processed_files GROUP BY status")]
