"""Domain models for drive-to-flickr."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class Status(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    MATCHING_CALENDAR = "matching_calendar"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    ALBUM_ASSIGNED = "album_assigned"
    COMPLETE = "complete"
    RETRY = "retry"
    MANUAL_REVIEW = "manual_review"
    FAILED = "failed"


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    created_at: datetime
    modified_at: datetime
    checksum: str | None = None
    path: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class MediaMetadata:
    captured_at: datetime
    source: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventSettings:
    flickr: bool | None = None
    album: str | None = None
    privacy: str | None = None
    tags: tuple[str, ...] = ()
    description: str | None = None
    buffer_before: timedelta | None = None
    buffer_after: timedelta | None = None


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    created_at: datetime
    all_day: bool = False
    settings: EventSettings = field(default_factory=EventSettings)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def album_name(self) -> str:
        return self.settings.album or self.title


@dataclass(frozen=True)
class AlbumPlan:
    album_name: str
    privacy: str
    tags: tuple[str, ...]
    event: CalendarEvent | None
    description: str | None = None


@dataclass(frozen=True)
class UploadResult:
    photo_id: str


@dataclass(frozen=True)
class DownloadedFile:
    drive_file: DriveFile
    path: Path
    kind: MediaKind
