"""Persistent application settings stored in SQLite."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .config import MAX_WORKER_CONCURRENCY, VALID_DRIVE_ACTION, VALID_NO_EVENT, VALID_PRIVACY, VALID_SOURCE_TYPES
from .database import Database, now

DEFAULT_SETTINGS: dict[str, str] = {
    "PUBLIC_BASE_URL": "",
    "SOURCE_TYPE": "google_drive",
    "LOCAL_SOURCE_PATH": "",
    "LOCAL_SOURCE_RECURSIVE": "true",
    "LOCAL_SUCCESS_FOLDER": "",
    "LOCAL_FAILED_FOLDER": "",
    "WORKER_CONCURRENCY": "3",
    "SMB_SERVER": "",
    "SMB_SHARE": "",
    "SMB_FOLDER": "",
    "SMB_USERNAME": "",
    "SMB_PORT": "445",
    "SMB_RECURSIVE": "true",
    "SMB_SUCCESS_FOLDER": "",
    "SMB_FAILED_FOLDER": "",
    "TIMEZONE": "UTC",
    "POLL_INTERVAL_SECONDS": "120",
    "BUFFER_BEFORE_MINUTES": "0",
    "BUFFER_AFTER_MINUTES": "0",
    "REQUIRE_FLICKR_MARKER": "false",
    "NO_EVENT_ACTION": "unassigned",
    "UNASSIGNED_ALBUM": "Unassigned Uploads",
    "DRIVE_SUCCESS_ACTION": "leave",
    "DRIVE_SUCCESS_FOLDER": "",
    "DRIVE_SUCCESS_FOLDER_NAME": "",
    "DRIVE_FAILED_FOLDER": "",
    "DRIVE_FAILED_FOLDER_NAME": "",
    "MINIMUM_FILE_AGE_SECONDS": "60",
    "FLICKR_DEFAULT_PRIVACY": "private",
    "GLOBAL_TAGS": "",
    "LOG_LEVEL": "INFO",
    "MAX_ATTEMPTS": "5",
    "GOOGLE_DRIVE_FOLDER_ID": "",
    "GOOGLE_DRIVE_FOLDER_NAME": "",
    "GOOGLE_CALENDAR_ID": "",
    "GOOGLE_CALENDAR_NAME": "",
    "GOOGLE_ACCOUNT_EMAIL": "",
    "GOOGLE_LAST_API_CHECK": "",
    "FLICKR_API_KEY": "",
    "FLICKR_USERNAME": "",
    "FLICKR_LAST_API_CHECK": "",
    "WORKER_HEARTBEAT": "",
    "SETUP_LAST_TEST_STATUS": "",
    "SETUP_LAST_TEST_AT": "",
    "SETUP_TEST_RESULTS": "[]",
    "SETUP_COMPLETE": "false",
}
ENV_OVERRIDES = set(DEFAULT_SETTINGS) | {"DATABASE_PATH", "STAGING_DIR", "GOOGLE_CREDENTIALS_FILE", "GOOGLE_TOKEN_FILE"}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.ensure_defaults()
        self.import_legacy_environment()

    def ensure_defaults(self) -> None:
        with self.db.connect() as conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now()),
                )

    def import_legacy_environment(self) -> None:
        for key in ("GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_CALENDAR_ID"):
            if os.getenv(key) and not self.get(key):
                self.set(key, os.environ[key])

    def get(self, key: str, default: str = "") -> str:
        if key in ENV_OVERRIDES and os.getenv(key) not in (None, ""):
            return os.environ[key]
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def get_many(self, keys: list[str] | set[str] | tuple[str, ...]) -> dict[str, str]:
        """Read several settings with one query, applying the same env overrides as ``get``."""
        wanted = list(keys)
        with self.db.connect() as conn:
            placeholders = ",".join("?" for _ in wanted)
            rows = conn.execute(f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})", wanted).fetchall() if wanted else []
        stored = {row["key"]: str(row["value"]) for row in rows}
        result = {}
        for key in wanted:
            if key in ENV_OVERRIDES and os.getenv(key) not in (None, ""):
                result[key] = os.environ[key]
            else:
                result[key] = stored.get(key, "")
        return result

    def set(self, key: str, value: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now()),
            )

    def update(self, values: dict[str, str]) -> None:
        errors = validate_settings(self.all_public() | values)
        if errors:
            raise ValueError("; ".join(errors))
        for key, value in values.items():
            self.set(key, value)

    def all_public(self) -> dict[str, str]:
        return self.get_many(tuple(DEFAULT_SETTINGS))


def validate_settings(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    def int_at_least(key: str, minimum: int) -> None:
        try:
            if int(values.get(key, "0")) < minimum:
                errors.append(f"{key} must be at least {minimum}")
        except ValueError:
            errors.append(f"{key} must be an integer")
    try:
        ZoneInfo(values.get("TIMEZONE", "UTC"))
    except ZoneInfoNotFoundError:
        errors.append("TIMEZONE must be a recognized IANA timezone")
    int_at_least("POLL_INTERVAL_SECONDS", 1)
    int_at_least("MINIMUM_FILE_AGE_SECONDS", 0)
    int_at_least("MAX_ATTEMPTS", 1)
    int_at_least("BUFFER_BEFORE_MINUTES", 0)
    int_at_least("BUFFER_AFTER_MINUTES", 0)
    int_at_least("WORKER_CONCURRENCY", 1)
    try:
        if int(values.get("WORKER_CONCURRENCY", "1")) > MAX_WORKER_CONCURRENCY:
            errors.append(f"WORKER_CONCURRENCY must be at most {MAX_WORKER_CONCURRENCY}")
    except ValueError:
        pass
    source_type = values.get("SOURCE_TYPE") or "google_drive"
    if source_type not in VALID_SOURCE_TYPES:
        errors.append("SOURCE_TYPE is invalid")
    local_path = values.get("LOCAL_SOURCE_PATH", "").strip()
    if source_type == "local" and not local_path:
        errors.append("LOCAL_SOURCE_PATH is required when the media source is a local/NAS folder")
    if source_type == "smb":
        if not values.get("SMB_SERVER", "").strip():
            errors.append("SMB_SERVER is required when the media source is a network share")
        if not values.get("SMB_SHARE", "").strip():
            errors.append("SMB_SHARE is required when the media source is a network share")
    if any(c in values.get("SMB_SERVER", "") for c in "/\\ "):
        errors.append("SMB_SERVER must be just the NAS name or IP address, for example 192.168.1.20")
    if any(c in values.get("SMB_SHARE", "") for c in "/\\"):
        errors.append("SMB_SHARE must be a single shared folder name; put subfolders in SMB_FOLDER")
    for key in ("SMB_FOLDER", "SMB_SUCCESS_FOLDER", "SMB_FAILED_FOLDER"):
        if ".." in values.get(key, "").replace("\\", "/").split("/"):
            errors.append(f"{key} cannot contain '..'")
    try:
        if not 1 <= int(values.get("SMB_PORT") or "445") <= 65535:
            errors.append("SMB_PORT must be between 1 and 65535")
    except ValueError:
        errors.append("SMB_PORT must be an integer")
    for key in ("LOCAL_SOURCE_PATH", "LOCAL_SUCCESS_FOLDER", "LOCAL_FAILED_FOLDER"):
        if values.get(key, "").strip() and not Path(values[key].strip()).is_absolute():
            errors.append(f"{key} must be an absolute path, for example /mnt/nas/photos")
    if values.get("FLICKR_DEFAULT_PRIVACY") not in VALID_PRIVACY:
        errors.append("FLICKR_DEFAULT_PRIVACY is invalid")
    if values.get("NO_EVENT_ACTION") not in VALID_NO_EVENT:
        errors.append("NO_EVENT_ACTION is invalid")
    if values.get("DRIVE_SUCCESS_ACTION") not in VALID_DRIVE_ACTION:
        errors.append("DRIVE_SUCCESS_ACTION is invalid")
    if values.get("DRIVE_SUCCESS_ACTION") == "move":
        if source_type == "local" and not values.get("LOCAL_SUCCESS_FOLDER"):
            errors.append("LOCAL_SUCCESS_FOLDER is required when DRIVE_SUCCESS_ACTION is move")
        elif source_type == "smb" and not values.get("SMB_SUCCESS_FOLDER"):
            errors.append("SMB_SUCCESS_FOLDER is required when DRIVE_SUCCESS_ACTION is move")
        elif source_type == "google_drive" and not values.get("DRIVE_SUCCESS_FOLDER"):
            errors.append("DRIVE_SUCCESS_FOLDER is required when DRIVE_SUCCESS_ACTION is move")
    if values.get("TIMEZONE") and values.get("TIMEZONE") not in available_timezones():
        errors.append("TIMEZONE must be available on this system")
    public_base_url = values.get("PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        parsed = urlparse(public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("PUBLIC_BASE_URL must be a complete http:// or https:// URL")
    return errors
