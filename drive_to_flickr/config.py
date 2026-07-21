"""Configuration loading and validation."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False

VALID_PRIVACY = {"public", "private", "friends", "family", "friends-and-family"}
VALID_NO_EVENT = {"unassigned", "skip", "manual-review"}
VALID_DRIVE_ACTION = {"leave", "move"}


@dataclass(frozen=True)
class Settings:
    timezone: ZoneInfo
    poll_interval_seconds: int
    database_path: Path
    staging_dir: Path
    google_credentials_file: Path
    google_token_file: Path
    google_drive_folder_id: str
    google_calendar_id: str
    flickr_api_key: str
    flickr_api_secret: str
    flickr_oauth_token: str
    flickr_oauth_token_secret: str
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    require_flickr_marker: bool = False
    no_event_action: str = "unassigned"
    unassigned_album: str = "Unassigned Uploads"
    drive_success_action: str = "leave"
    drive_success_folder: str | None = None
    drive_failed_folder: str | None = None
    minimum_file_age_seconds: int = 60
    flickr_default_privacy: str = "private"
    global_tags: tuple[str, ...] = ()
    log_level: str = "INFO"
    max_attempts: int = 5
    dry_run: bool = False
    web_bind: str = "127.0.0.1"
    web_port: int = 8080
    web_secret_key: str = ""
    admin_username: str = "admin"
    admin_password_hash: str = ""
    secret_store_path: Path = Path("/etc/drive-to-flickr/secrets.json")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_settings(env_file: Path | None = None, *, require_credentials: bool = True) -> Settings:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()
    db_values: dict[str, str] = {}
    secret_values: dict[str, str] = {}
    db_path = Path(os.getenv("DATABASE_PATH", "/var/lib/drive-to-flickr/state.sqlite"))
    try:
        from .database import Database
        from .settings_store import SettingsStore
        db_values = SettingsStore(Database(db_path)).all_public()
        from .secrets import SecretStore
        ss = SecretStore(Path(os.getenv("SECRET_STORE_PATH", "/etc/drive-to-flickr/secrets.json")))
        secret_values = {
            "FLICKR_API_SECRET": ss.get("flickr_api_secret"),
            "FLICKR_OAUTH_TOKEN": ss.get("flickr_oauth_token"),
            "FLICKR_OAUTH_TOKEN_SECRET": ss.get("flickr_oauth_token_secret"),
        }
    except Exception:
        db_values = {}

    def cfg(name: str, default: str = "") -> str:
        return os.getenv(name) or secret_values.get(name) or db_values.get(name) or default

    try:
        tz = ZoneInfo(cfg("TIMEZONE", "UTC"))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("TIMEZONE is not a valid IANA timezone") from exc
    def required(name: str) -> str:
        value = cfg(name)
        if require_credentials and not value:
            raise ValueError(f"Missing required setting: {name}")
        return value or ""
    settings = Settings(
        timezone=tz,
        poll_interval_seconds=int(cfg("POLL_INTERVAL_SECONDS", "120")),
        database_path=db_path,
        staging_dir=Path(os.getenv("STAGING_DIR", "/var/lib/drive-to-flickr/staging")),
        google_credentials_file=Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "/etc/drive-to-flickr/google-client.json")),
        google_token_file=Path(os.getenv("GOOGLE_TOKEN_FILE", "/etc/drive-to-flickr/google-token.json")),
        google_drive_folder_id=required("GOOGLE_DRIVE_FOLDER_ID"),
        google_calendar_id=required("GOOGLE_CALENDAR_ID"),
        flickr_api_key=required("FLICKR_API_KEY"),
        flickr_api_secret=required("FLICKR_API_SECRET"),
        flickr_oauth_token=required("FLICKR_OAUTH_TOKEN"),
        flickr_oauth_token_secret=required("FLICKR_OAUTH_TOKEN_SECRET"),
        buffer_before_minutes=int(cfg("BUFFER_BEFORE_MINUTES", "0")),
        buffer_after_minutes=int(cfg("BUFFER_AFTER_MINUTES", "0")),
        require_flickr_marker=_bool(cfg("REQUIRE_FLICKR_MARKER", "false"), False),
        no_event_action=cfg("NO_EVENT_ACTION", "unassigned"),
        unassigned_album=cfg("UNASSIGNED_ALBUM", "Unassigned Uploads"),
        drive_success_action=cfg("DRIVE_SUCCESS_ACTION", "leave"),
        drive_success_folder=cfg("DRIVE_SUCCESS_FOLDER") or None,
        drive_failed_folder=cfg("DRIVE_FAILED_FOLDER") or None,
        minimum_file_age_seconds=int(cfg("MINIMUM_FILE_AGE_SECONDS", "60")),
        flickr_default_privacy=cfg("FLICKR_DEFAULT_PRIVACY", "private"),
        global_tags=_csv(cfg("GLOBAL_TAGS")),
        log_level=cfg("LOG_LEVEL", "INFO"),
        max_attempts=int(cfg("MAX_ATTEMPTS", "5")),
        web_bind=os.getenv("WEB_BIND", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        web_secret_key=os.getenv("WEB_SECRET_KEY", ""),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
        secret_store_path=Path(os.getenv("SECRET_STORE_PATH", "/etc/drive-to-flickr/secrets.json")),
    )
    if settings.no_event_action not in VALID_NO_EVENT:
        raise ValueError("NO_EVENT_ACTION must be unassigned, skip, or manual-review")
    if settings.drive_success_action not in VALID_DRIVE_ACTION:
        raise ValueError("DRIVE_SUCCESS_ACTION must be leave or move")
    if settings.flickr_default_privacy not in VALID_PRIVACY:
        raise ValueError("FLICKR_DEFAULT_PRIVACY is invalid")
    return settings
