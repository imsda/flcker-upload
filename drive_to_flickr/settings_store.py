"""Persistent application settings stored in SQLite."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from .config import VALID_DRIVE_ACTION, VALID_NO_EVENT, VALID_PRIVACY
from .database import Database, now

DEFAULT_SETTINGS: dict[str, str] = {
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

    def set(self, key: str, value: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now()),
            )

    def update(self, values: dict[str, str]) -> None:
        errors = validate_settings(values | {k: self.get(k) for k in DEFAULT_SETTINGS})
        if errors:
            raise ValueError("; ".join(errors))
        for key, value in values.items():
            self.set(key, value)

    def all_public(self) -> dict[str, str]:
        return {key: self.get(key) for key in DEFAULT_SETTINGS}


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
    if values.get("FLICKR_DEFAULT_PRIVACY") not in VALID_PRIVACY:
        errors.append("FLICKR_DEFAULT_PRIVACY is invalid")
    if values.get("NO_EVENT_ACTION") not in VALID_NO_EVENT:
        errors.append("NO_EVENT_ACTION is invalid")
    if values.get("DRIVE_SUCCESS_ACTION") not in VALID_DRIVE_ACTION:
        errors.append("DRIVE_SUCCESS_ACTION is invalid")
    if values.get("DRIVE_SUCCESS_ACTION") == "move" and not values.get("DRIVE_SUCCESS_FOLDER"):
        errors.append("DRIVE_SUCCESS_FOLDER is required when DRIVE_SUCCESS_ACTION is move")
    if values.get("TIMEZONE") and values.get("TIMEZONE") not in available_timezones():
        errors.append("TIMEZONE must be available on this system")
    return errors
