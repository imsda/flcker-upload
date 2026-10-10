"""End-to-end processing pipeline."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .calendar import GoogleCalendarClient
from .config import Settings
from .database import TERMINAL_STATUSES, Database
from .flickr import PHOTOSET_NOT_FOUND, FlickrAPIError, FlickrClient, drive_id_tag
from .matcher import EventMatcher, normalize_album_name
from .metadata import detect_media_kind, extract_metadata
from .models import CalendarEvent, DriveFile, Status
from .sources import MediaSource

LOGGER = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
# With NO_EVENT_ACTION=skip, look for a matching calendar event again after this long.
NO_EVENT_RETRY_DELAY = timedelta(hours=1)


def retry_delay(attempts: int) -> timedelta:
    """Exponential backoff: 1, 2, 4, 8... minutes, capped at an hour."""
    return timedelta(seconds=min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1)))


class Processor:
    def __init__(self, settings: Settings, db: Database, source: MediaSource, calendar: GoogleCalendarClient, flickr: FlickrClient | None) -> None:
        self.settings = settings
        self.db = db
        self.source = source
        self.calendar = calendar
        self.flickr = flickr
        self.matcher = EventMatcher(buffer_before=timedelta(minutes=settings.buffer_before_minutes), buffer_after=timedelta(minutes=settings.buffer_after_minutes), require_flickr_marker=settings.require_flickr_marker, default_privacy=settings.flickr_default_privacy, global_tags=settings.global_tags, unassigned_album=settings.unassigned_album)
        self._calendar_cache: dict[date, list[CalendarEvent]] = {}
        self._calendar_lock = threading.Lock()
        # Serialises album lookup/creation so parallel uploads never create the same album twice.
        self._photoset_lock = threading.Lock()

    def scan(self, *, dry_run: bool = False, should_stop: Callable[[], bool] = lambda: False) -> int:
        """Process every file that is new or due for retry. Returns how many were attempted."""
        files = self.source.list_media_files()
        states = self.db.file_states()
        current = datetime.now(UTC)
        due = [f for f in files if self._is_due(f, states.get(f.id), current)]
        self._calendar_cache = {}
        if not due:
            return 0
        LOGGER.info("Processing %d file(s) from %s", len(due), self.source.label)

        def work(drive_file: DriveFile) -> None:
            if not should_stop():
                self.process_file(drive_file, dry_run=dry_run, previous=states.get(drive_file.id))

        workers = 1 if dry_run else self.settings.worker_concurrency
        if workers <= 1 or len(due) == 1:
            for drive_file in due:
                work(drive_file)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media") as pool:
                list(pool.map(work, due))
        return len(due)

    def _is_due(self, drive_file: DriveFile, state: sqlite3.Row | None, current: datetime) -> bool:
        if detect_media_kind(Path(drive_file.name), drive_file.mime_type) is None:
            return False
        if state is not None:
            if state["status"] in TERMINAL_STATUSES:
                return False
            if state["status"] == Status.RETRY and state["next_retry_at"]:
                if datetime.fromisoformat(state["next_retry_at"]) > current:
                    return False
        if self._too_new(drive_file, current):
            LOGGER.debug("Skipping file that may still be changing", extra={"drive_file_id": drive_file.id, "filename": drive_file.name})
            return False
        return True

    def _too_new(self, drive_file: DriveFile, current: datetime | None = None) -> bool:
        age = (current or datetime.now(UTC)) - drive_file.modified_at.astimezone(UTC)
        return age.total_seconds() < self.settings.minimum_file_age_seconds

    def _status(self, file_id: str, status: Status, dry_run: bool) -> None:
        if not dry_run:
            self.db.update_status(file_id, status)

    def process_file(self, drive_file: DriveFile, *, dry_run: bool = False, previous: sqlite3.Row | None = None) -> None:
        kind = detect_media_kind(Path(drive_file.name), drive_file.mime_type)
        if kind is None:
            return
        if previous is None:
            previous = self.db.get_file(drive_file.id)
        if not dry_run:
            self.db.upsert_discovered(drive_file)
        local_path: Path | None = None
        try:
            known_photo_id = previous["flickr_photo_id"] if previous is not None else None
            known_captured = previous["media_captured_at"] if previous is not None else None
            media_tags: tuple[str, ...] = ()
            if known_photo_id and known_captured:
                # Already on Flickr from an earlier attempt; only the album step is left,
                # so there is no need to download the file again.
                captured_at = datetime.fromisoformat(known_captured)
            else:
                self._status(drive_file.id, Status.DOWNLOADING, dry_run)
                local_path = self.source.fetch(drive_file, self.settings.staging_dir)
                self._status(drive_file.id, Status.DOWNLOADED, dry_run)
                metadata = extract_metadata(local_path, kind, self.settings.timezone, drive_file)
                captured_at = metadata.captured_at
                media_tags = metadata.tags
            self._status(drive_file.id, Status.MATCHING_CALENDAR, dry_run)
            event, reason = self.matcher.select_event(captured_at, self._events_for(captured_at))
            LOGGER.info("Calendar selection: %s", reason, extra={"drive_file_id": drive_file.id, "event_id": event.id if event else None})
            plan = self.matcher.build_plan(event)
            all_tags = tuple(dict.fromkeys((*plan.tags, *media_tags, drive_id_tag(drive_file.id))))
            if dry_run:
                action = plan.album_name if event or self.settings.no_event_action == "unassigned" else self.settings.no_event_action.upper()
                print(f"{drive_file.name}\t{captured_at.isoformat()}\t{event.title if event else 'NO EVENT'}\t{action}\t{plan.privacy}\t{', '.join(all_tags)}")
                return
            if event is None and self.settings.no_event_action == "skip":
                self.db.schedule_retry(drive_file.id, NO_EVENT_RETRY_DELAY, "No calendar event matched; will check the calendar again later")
                return
            if event is None and self.settings.no_event_action == "manual-review":
                self.db.update_status(drive_file.id, Status.MANUAL_REVIEW, "No calendar event matched")
                return
            if self.flickr is None:
                raise RuntimeError("Flickr client is required unless dry-run is enabled")
            photo_id = known_photo_id
            if photo_id is None:
                self.db.update_status(drive_file.id, Status.UPLOADING)
                if previous is not None and (previous["attempts"] or previous["status"] != Status.DISCOVERED):
                    # An earlier attempt may have reached Flickr before failing or crashing.
                    photo_id = self.flickr.find_uploaded_by_drive_id(drive_file.id)
                if photo_id is None:
                    if local_path is None:
                        local_path = self.source.fetch(drive_file, self.settings.staging_dir)
                    photo_id = self.flickr.upload(local_path, title=Path(drive_file.name).stem, description=plan.description, tags=all_tags, privacy=plan.privacy, date_taken=captured_at.isoformat())
            self.db.mark_uploaded(drive_file.id, captured_at, event.id if event else None, event.title if event else None, photo_id)
            photoset_id = self.assign_to_album(plan.album_name, photo_id, event.id if event else None)
            self.db.mark_complete(drive_file.id, photoset_id)
            LOGGER.info("Published %s to album %s", drive_file.name, plan.album_name, extra={"drive_file_id": drive_file.id, "photo_id": photo_id})
            if self.settings.drive_success_action == "move":
                self._move(drive_file, self._success_destination())
        except Exception as exc:  # noqa: BLE001
            if dry_run:
                LOGGER.error("Dry run could not process %s: %s", drive_file.name, exc, extra={"drive_file_id": drive_file.id})
            else:
                self._handle_failure(drive_file, exc)
        finally:
            if local_path is not None and self.source.staged:
                shutil.rmtree(local_path.parent, ignore_errors=True)

    def _handle_failure(self, drive_file: DriveFile, exc: Exception) -> None:
        attempts = self.db.increment_attempts(drive_file.id, str(exc))
        if attempts >= self.settings.max_attempts:
            self.db.update_status(drive_file.id, Status.FAILED, str(exc))
            LOGGER.error("Processing failed permanently: %s", exc, extra={"drive_file_id": drive_file.id, "retry_count": attempts})
            self._move(drive_file, self._failed_destination())
            return
        delay = retry_delay(attempts)
        self.db.schedule_retry(drive_file.id, delay, str(exc))
        LOGGER.warning("Processing failed; will retry in %s: %s", delay, exc, extra={"drive_file_id": drive_file.id, "retry_count": attempts})

    def _success_destination(self) -> str | None:
        if self.settings.source_type == "local":
            return str(self.settings.local_success_folder) if self.settings.local_success_folder else None
        if self.settings.source_type == "smb":
            return self.settings.smb_success_folder or None
        return self.settings.drive_success_folder

    def _failed_destination(self) -> str | None:
        if self.settings.source_type == "local":
            return str(self.settings.local_failed_folder) if self.settings.local_failed_folder else None
        if self.settings.source_type == "smb":
            return self.settings.smb_failed_folder or None
        return self.settings.drive_failed_folder

    def _move(self, drive_file: DriveFile, destination: str | None) -> None:
        if not destination:
            return
        try:
            self.source.move_file(drive_file.id, destination)
        except Exception as exc:  # noqa: BLE001
            # The upload outcome is already recorded; a failed tidy-up must not change it.
            LOGGER.warning("Could not move %s to %s: %s", drive_file.name, destination, exc, extra={"drive_file_id": drive_file.id})

    def _events_for(self, captured_at: datetime) -> list[CalendarEvent]:
        """Calendar events near ``captured_at``, fetched once per local day per scan."""
        day = captured_at.astimezone(self.settings.timezone).date()
        with self._calendar_lock:
            cached = self._calendar_cache.get(day)
        if cached is None:
            cached = self.calendar.events_for_day(self.settings.google_calendar_id, day)
            with self._calendar_lock:
                self._calendar_cache[day] = cached
        return cached

    def assign_to_album(self, album_name: str, photo_id: str, event_id: str | None) -> str:
        if self.flickr is None:
            raise RuntimeError("Flickr client missing")
        normalized = normalize_album_name(album_name)
        with self._photoset_lock:
            cached = self.db.get_photoset(normalized)
            if cached:
                try:
                    self.flickr.add_photo_to_photoset(cached, photo_id)
                    return cached
                except FlickrAPIError as exc:
                    if exc.code != PHOTOSET_NOT_FOUND:
                        raise
                    LOGGER.warning("Album %r no longer exists on Flickr; it will be recreated", album_name)
                    self.db.delete_photoset(normalized)
            existing = self.flickr.list_photosets().get(normalized)
            if existing:
                photoset_id = existing[0]
                self.flickr.add_photo_to_photoset(photoset_id, photo_id)
            else:
                # Flickr adds the primary photo to a new album, so no separate addPhoto call.
                photoset_id = self.flickr.create_photoset(album_name, photo_id)
            self.db.save_photoset(normalized, photoset_id, album_name, event_id)
            return photoset_id

    def reconcile(self) -> int:
        """Record Flickr photo IDs for files that were uploaded but not marked as such."""
        if self.flickr is None:
            raise RuntimeError("Flickr client missing")
        found = 0
        for file_id in self.db.unfinished_without_photo():
            photo_id = self.flickr.find_uploaded_by_drive_id(file_id)
            if photo_id:
                self.db.set_photo_id(file_id, photo_id)
                found += 1
        LOGGER.info("Reconcile matched %d unfinished file(s) to existing Flickr photos", found)
        return found

    def cleanup_staging(self) -> None:
        """Remove staged downloads left behind by a crash. Only call when no scan is running."""
        staging = self.settings.staging_dir
        if not staging.is_dir():
            return
        for child in staging.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
