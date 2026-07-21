"""SQLite persistence."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
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
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def is_processed(self, drive_file_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM processed_files WHERE google_drive_file_id=?", (drive_file_id,)).fetchone()
            return bool(row and row["status"] == Status.COMPLETE)

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

    def mark_uploaded(self, file_id: str, captured: datetime, event_id: str | None, event_title: str | None, photo_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET media_captured_at=?, calendar_event_id=?, calendar_event_title=?, flickr_photo_id=?, status=?, updated_at=? WHERE google_drive_file_id=?", (captured.isoformat(), event_id, event_title, photo_id, Status.UPLOADED, now(), file_id))

    def mark_complete(self, file_id: str, photoset_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE processed_files SET flickr_photoset_id=?, status=?, updated_at=? WHERE google_drive_file_id=?", (photoset_id, Status.COMPLETE, now(), file_id))

    def get_photoset(self, normalized_name: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT flickr_photoset_id FROM photosets WHERE normalized_name=?", (normalized_name,)).fetchone()
            return None if row is None else str(row["flickr_photoset_id"])

    def save_photoset(self, normalized_name: str, photoset_id: str, display_name: str, event_id: str | None = None) -> None:
        ts = now()
        with self.connect() as conn:
            conn.execute("""INSERT INTO photosets (normalized_name, flickr_photoset_id, display_name, calendar_event_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(normalized_name) DO UPDATE SET flickr_photoset_id=excluded.flickr_photoset_id, updated_at=excluded.updated_at""", (normalized_name, photoset_id, display_name, event_id, ts, ts))

    def status_counts(self) -> list[tuple[str, int]]:
        with self.connect() as conn:
            return [(r["status"], r["count"]) for r in conn.execute("SELECT status, COUNT(*) count FROM processed_files GROUP BY status")]
