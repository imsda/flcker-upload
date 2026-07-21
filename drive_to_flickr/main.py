"""Command-line interface."""
from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from .calendar import GoogleCalendarClient
from .config import load_settings
from .database import Database
from .drive import GoogleDriveClient
from .flickr import FlickrClient
from .logging_config import configure_logging
from .matcher import EventMatcher
from .processor import Processor

STOP = False


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True


def build_processor(require_credentials: bool = True, flickr_required: bool = True) -> tuple[Processor, Database]:
    settings = load_settings(require_credentials=require_credentials)
    configure_logging(settings.log_level)
    db = Database(settings.database_path)
    drive = GoogleDriveClient(settings.google_credentials_file, settings.google_token_file)
    calendar = GoogleCalendarClient(settings.google_credentials_file, settings.google_token_file, settings.timezone)
    flickr = FlickrClient(settings.flickr_api_key, settings.flickr_api_secret, settings.flickr_oauth_token, settings.flickr_oauth_token_secret) if flickr_required else None
    return Processor(settings, db, drive, calendar, flickr), db


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


def cmd_run(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    proc, _ = build_processor(flickr_required=not args.dry_run)
    while not STOP:
        proc.scan(dry_run=args.dry_run)
        for _ in range(proc.settings.poll_interval_seconds):
            if STOP:
                break
            time.sleep(1)
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    built = build_processor(flickr_required=False)
    database = built[1]
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
    calendar = GoogleCalendarClient(settings.google_credentials_file, settings.google_token_file, settings.timezone)
    captured = datetime.fromisoformat(args.datetime)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=settings.timezone)
    events = calendar.events_around(settings.google_calendar_id, captured)
    matcher = EventMatcher(buffer_before=timedelta(minutes=settings.buffer_before_minutes), buffer_after=timedelta(minutes=settings.buffer_after_minutes), require_flickr_marker=settings.require_flickr_marker, default_privacy=settings.flickr_default_privacy, global_tags=settings.global_tags, unassigned_album=settings.unassigned_album)
    event, reason = matcher.select_event(captured, events)
    print(reason)
    print(event.album_name if event else "NO MATCH")
    return 0


def cmd_test_drive(_: argparse.Namespace) -> int:
    settings = load_settings()
    drive = GoogleDriveClient(settings.google_credentials_file, settings.google_token_file)
    print(f"Found {len(drive.list_media_files(settings.google_drive_folder_id))} Drive files")
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
    simple_commands: dict[str, Callable[[argparse.Namespace], int]] = {"status": cmd_status, "retry": cmd_scan, "reconcile": cmd_reconcile, "test-drive": cmd_test_drive, "test-flickr": cmd_test_flickr, "auth-google": cmd_auth_google, "auth-flickr": cmd_auth_flickr, "web": cmd_web}
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
