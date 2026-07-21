"""End-to-end processing pipeline."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .calendar import GoogleCalendarClient
from .config import Settings
from .database import Database
from .drive import GoogleDriveClient
from .flickr import FlickrClient, drive_id_tag
from .matcher import EventMatcher, normalize_album_name
from .metadata import detect_media_kind, extract_metadata
from .models import DriveFile, Status

LOGGER = logging.getLogger(__name__)

RETRYABLE = (TimeoutError, ConnectionError)


class Processor:
    def __init__(self, settings: Settings, db: Database, drive: GoogleDriveClient, calendar: GoogleCalendarClient, flickr: FlickrClient | None) -> None:
        self.settings = settings
        self.db = db
        self.drive = drive
        self.calendar = calendar
        self.flickr = flickr
        self.matcher = EventMatcher(buffer_before=timedelta(minutes=settings.buffer_before_minutes), buffer_after=timedelta(minutes=settings.buffer_after_minutes), require_flickr_marker=settings.require_flickr_marker, default_privacy=settings.flickr_default_privacy, global_tags=settings.global_tags, unassigned_album=settings.unassigned_album)

    def scan(self, *, dry_run: bool = False) -> None:
        for drive_file in self.drive.list_media_files(self.settings.google_drive_folder_id):
            if self._too_new(drive_file):
                LOGGER.info("Skipping unstable file", extra={"drive_file_id": drive_file.id, "filename": drive_file.name})
                continue
            if detect_media_kind(Path(drive_file.name), drive_file.mime_type) is None:
                LOGGER.info("Ignoring unsupported file", extra={"drive_file_id": drive_file.id, "filename": drive_file.name})
                continue
            if self.db.is_processed(drive_file.id):
                continue
            self.process_file(drive_file, dry_run=dry_run)

    def _too_new(self, drive_file: DriveFile) -> bool:
        age = datetime.now(UTC) - drive_file.modified_at.astimezone(UTC)
        return age.total_seconds() < self.settings.minimum_file_age_seconds

    def process_file(self, drive_file: DriveFile, *, dry_run: bool = False) -> None:
        self.db.upsert_discovered(drive_file)
        kind = detect_media_kind(Path(drive_file.name), drive_file.mime_type)
        if kind is None:
            return
        destination = self.settings.staging_dir / drive_file.id / drive_file.name
        try:
            self.db.update_status(drive_file.id, Status.DOWNLOADING)
            self.drive.download(drive_file.id, destination)
            self.db.update_status(drive_file.id, Status.DOWNLOADED)
            metadata = extract_metadata(destination, kind, self.settings.timezone, drive_file)
            self.db.update_status(drive_file.id, Status.MATCHING_CALENDAR)
            events = self.calendar.events_around(self.settings.google_calendar_id, metadata.captured_at)
            event, reason = self.matcher.select_event(metadata.captured_at, events)
            LOGGER.info("Calendar selection: %s", reason, extra={"drive_file_id": drive_file.id, "event_id": event.id if event else None})
            if event is None and self.settings.no_event_action == "skip":
                self.db.update_status(drive_file.id, Status.RETRY, "No calendar event matched; configured to skip and retry")
                return
            if event is None and self.settings.no_event_action == "manual-review":
                self.db.update_status(drive_file.id, Status.MANUAL_REVIEW, "No calendar event matched")
                return
            plan = self.matcher.build_plan(event)
            all_tags = tuple(dict.fromkeys((*plan.tags, *metadata.tags, drive_id_tag(drive_file.id))))
            if dry_run:
                print(f"{drive_file.name}\t{metadata.captured_at.isoformat()}\t{event.title if event else 'NO EVENT'}\t{plan.album_name}\t{plan.privacy}\t{', '.join(all_tags)}")
                return
            if self.flickr is None:
                raise RuntimeError("Flickr client is required unless dry-run is enabled")
            self.db.update_status(drive_file.id, Status.UPLOADING)
            existing_photo_id = self.flickr.find_uploaded_by_drive_id(drive_file.id)
            photo_id = existing_photo_id or self.flickr.upload(destination, title=Path(drive_file.name).stem, description=plan.description, tags=all_tags, privacy=plan.privacy, date_taken=metadata.captured_at.isoformat())
            self.db.mark_uploaded(drive_file.id, metadata.captured_at, event.id if event else None, event.title if event else None, photo_id)
            photoset_id = self.ensure_photoset(plan.album_name, photo_id, event.id if event else None)
            self.flickr.add_photo_to_photoset(photoset_id, photo_id)
            self.db.mark_complete(drive_file.id, photoset_id)
            if self.settings.drive_success_action == "move" and self.settings.drive_success_folder:
                self.drive.move_file(drive_file.id, self.settings.drive_success_folder)
        except Exception as exc:  # noqa: BLE001
            attempts = self.db.increment_attempts(drive_file.id, str(exc))
            if attempts >= self.settings.max_attempts:
                self.db.update_status(drive_file.id, Status.FAILED, str(exc))
                if self.settings.drive_failed_folder:
                    self.drive.move_file(drive_file.id, self.settings.drive_failed_folder)
            else:
                delay = min(3600, 2 ** attempts)
                LOGGER.warning("Processing failed; will retry after backoff", extra={"drive_file_id": drive_file.id, "retry_count": attempts, "delay": delay})
                time.sleep(delay)

    def ensure_photoset(self, album_name: str, primary_photo_id: str, event_id: str | None) -> str:
        normalized = normalize_album_name(album_name)
        cached = self.db.get_photoset(normalized)
        if cached:
            return cached
        if self.flickr is None:
            raise RuntimeError("Flickr client missing")
        existing = self.flickr.list_photosets().get(normalized)
        photoset_id = existing[0] if existing else self.flickr.create_photoset(album_name, primary_photo_id)
        self.db.save_photoset(normalized, photoset_id, album_name, event_id)
        return photoset_id

    def reconcile(self) -> None:
        LOGGER.info("Reconcile searches Flickr for drive-id machine tags before retries to reduce duplicates")
