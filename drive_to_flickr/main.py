"""Command-line interface."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .calendar import GoogleCalendarClient
from .config import Settings, load_settings
from .database import Database
from .drive import GoogleDriveClient
from .flickr import FlickrClient
from .google_ui import credentials as stored_google_credentials
from .logging_config import configure_logging
from .matcher import EventMatcher
from .processor import Processor
from .health import record_worker_heartbeat
from .settings_store import SettingsStore
from .secrets import SecretStore
from .sources import GoogleDriveSource, LocalFolderSource, MediaSource, SMBSource

STOP = False
HEARTBEAT_INTERVAL_SECONDS = 15
LOGGER = logging.getLogger(__name__)


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True


def _heartbeat_loop(store: SettingsStore, stopped: threading.Event) -> None:
    record_worker_heartbeat(store)
    while not stopped.wait(HEARTBEAT_INTERVAL_SECONDS):
        record_worker_heartbeat(store)


def google_credentials_for(settings: Settings):
    """Credentials saved by the web UI, or None to fall back to the legacy token file."""
    secret_store = SecretStore(settings.secret_store_path)
    return stored_google_credentials(secret_store) if secret_store.has("google_token_json") else None


def smb_source_from(server: str, share: str, folder: str, username: str, password: str, port: int, recursive: bool = True, success_folder: str = "", failed_folder: str = "") -> SMBSource:
    # Move-to folders are relative to the share root; exclude them from scanning when they
    # sit inside the watched folder so moved files are not picked up again.
    return SMBSource(server, share, folder, username=username, password=password, port=port, recursive=recursive, exclude=(success_folder, failed_folder))


def build_source(settings: Settings, google_credentials=None) -> MediaSource:
    if settings.source_type == "local":
        if settings.local_source_path is None:
            raise ValueError("Missing required setting: LOCAL_SOURCE_PATH")
        exclude = tuple(p for p in (settings.local_success_folder, settings.local_failed_folder) if p)
        return LocalFolderSource(settings.local_source_path, recursive=settings.local_source_recursive, exclude=exclude)
    if settings.source_type == "smb":
        return smb_source_from(settings.smb_server, settings.smb_share, settings.smb_folder, settings.smb_username, settings.smb_password, settings.smb_port, settings.smb_recursive, settings.smb_success_folder, settings.smb_failed_folder)
    drive = GoogleDriveClient(settings.google_credentials_file, settings.google_token_file, google_credentials)
    return GoogleDriveSource(drive, settings.google_drive_folder_id)


def build_processor(require_credentials: bool = True, flickr_required: bool = True) -> tuple[Processor, Database]:
    settings = load_settings(require_credentials=require_credentials)
    configure_logging(settings.log_level)
    db = Database(settings.database_path)
    google_credentials = google_credentials_for(settings)
    source = build_source(settings, google_credentials)
    calendar = GoogleCalendarClient(settings.google_credentials_file, settings.google_token_file, settings.timezone, google_credentials)
    flickr = FlickrClient(settings.flickr_api_key, settings.flickr_api_secret, settings.flickr_oauth_token, settings.flickr_oauth_token_secret) if flickr_required else None
    return Processor(settings, db, source, calendar, flickr), db


def cmd_auth_google(_: argparse.Namespace) -> int:
    settings = load_settings(require_credentials=False)
    GoogleCalendarClient.authorize(settings.google_credentials_file, settings.google_token_file)
    print(f"Wrote Google token to {settings.google_token_file}")
    return 0


def cmd_auth_flickr(_: argparse.Namespace) -> int:
    settings = load_settings(require_credentials=False)
    token, secret = FlickrClient.authorize(settings.flickr_api_key, settings.flickr_api_secret)
    print("Add these to /etc/drive-to-flickr/drive-to-flickr.env with mode 0600:")
    print(f"FLICKR_OAUTH_TOKEN={token}")
    print(f"FLICKR_OAUTH_TOKEN_SECRET={secret}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    proc, _ = build_processor(flickr_required=not args.dry_run)
    proc.scan(dry_run=args.dry_run)
    return 0


def cmd_retry(_: argparse.Namespace) -> int:
    proc, database = build_processor()
    queued = database.queue_all_failed()
    print(f"Queued {queued} failed file(s) for retry")
    proc.scan()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    proc, database = build_processor(flickr_required=not args.dry_run)
    proc.cleanup_staging()
    settings_store = SettingsStore(database)
    heartbeat_stopped = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(settings_store, heartbeat_stopped),
        name="worker-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        while not STOP:
            try:
                proc.scan(dry_run=args.dry_run, should_stop=lambda: STOP)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Scan failed; will try again next poll")
            for _ in range(proc.settings.poll_interval_seconds):
                if STOP:
                    break
                time.sleep(1)
            if STOP:
                break
            try:
                # Reload so changes saved in the web UI apply without restarting the worker.
                proc, database = build_processor(flickr_required=not args.dry_run)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Could not reload settings; keeping the previous configuration")
    finally:
        heartbeat_stopped.set()
        heartbeat_thread.join(timeout=2)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    database = Database(load_settings(require_credentials=False).database_path)
    for status, count in database.status_counts():
        print(f"{status}: {count}")
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    built = build_processor()
    proc = built[0]
    proc.reconcile()
    return 0


def cmd_test_calendar(args: argparse.Namespace) -> int:
    settings = load_settings()
    calendar = GoogleCalendarClient(settings.google_credentials_file, settings.google_token_file, settings.timezone, google_credentials_for(settings))
    captured = datetime.fromisoformat(args.datetime)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=settings.timezone)
    events = calendar.events_around(settings.google_calendar_id, captured)
    matcher = EventMatcher(buffer_before=timedelta(minutes=settings.buffer_before_minutes), buffer_after=timedelta(minutes=settings.buffer_after_minutes), require_flickr_marker=settings.require_flickr_marker, default_privacy=settings.flickr_default_privacy, global_tags=settings.global_tags, unassigned_album=settings.unassigned_album)
    event, reason = matcher.select_event(captured, events)
    print(reason)
    print(event.album_name if event else "NO MATCH")
    return 0


def cmd_test_source(_: argparse.Namespace) -> int:
    settings = load_settings(require_credentials=False)
    source = build_source(settings, google_credentials_for(settings) if settings.source_type == "google_drive" else None)
    print(f"Found {len(source.list_media_files())} files in {source.label}")
    return 0


def cmd_web(_: argparse.Namespace) -> int:
    from .web import main as web_main
    web_main()
    return 0


def cmd_test_flickr(_: argparse.Namespace) -> int:
    settings = load_settings()
    flickr = FlickrClient(settings.flickr_api_key, settings.flickr_api_secret, settings.flickr_oauth_token, settings.flickr_oauth_token_secret)
    print(f"Found {len(flickr.list_photosets())} Flickr photosets")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drive-to-flickr")
    sub = parser.add_subparsers(required=True)
    command_map: dict[str, Callable[[argparse.Namespace], int]] = {"scan": cmd_scan, "dry-run": cmd_scan, "run": cmd_run}
    for name, fn in command_map.items():
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true", default=name == "dry-run")
        p.set_defaults(func=fn)
    simple_commands: dict[str, Callable[[argparse.Namespace], int]] = {"status": cmd_status, "retry": cmd_retry, "reconcile": cmd_reconcile, "test-drive": cmd_test_source, "test-source": cmd_test_source, "test-flickr": cmd_test_flickr, "auth-google": cmd_auth_google, "auth-flickr": cmd_auth_flickr, "web": cmd_web}
    for name, fn in simple_commands.items():
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
    p = sub.add_parser("test-calendar")
    p.add_argument("datetime")
    p.set_defaults(func=cmd_test_calendar)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
