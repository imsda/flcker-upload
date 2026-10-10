"""Media timestamp extraction using ExifTool where available."""
from __future__ import annotations

import json
import logging
import mimetypes
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import DriveFile, MediaKind, MediaMetadata

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
PHOTO_KEYS = ("DateTimeOriginal", "CreateDate", "ModifyDate")
VIDEO_KEYS = ("MediaCreateDate", "TrackCreateDate", "CreateDate", "ModifyDate")
EXIFTOOL_TIMEOUT_SECONDS = 120

LOGGER = logging.getLogger(__name__)


def detect_media_kind(path: Path, mime_type: str = "") -> MediaKind | None:
    ext = path.suffix.lower()
    mime = mime_type or mimetypes.guess_type(path.name)[0] or ""
    if ext in PHOTO_EXT or mime.startswith("image/"):
        return MediaKind.PHOTO
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return MediaKind.VIDEO
    return None


def parse_exif_datetime(value: str, timezone: ZoneInfo) -> datetime:
    cleaned = value.strip().replace(":", "-", 2)
    if cleaned.endswith("Z"):
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        dt = datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone)
    return dt


def choose_metadata_timestamp(tags: dict[str, str], kind: MediaKind, timezone: ZoneInfo, drive_file: DriveFile) -> MediaMetadata:
    keys = PHOTO_KEYS if kind == MediaKind.PHOTO else VIDEO_KEYS
    for key in keys:
        value = tags.get(key)
        # Cameras without a clock write all-zero dates; skip those and unparsable values.
        if value and not str(value).startswith("0000"):
            try:
                return MediaMetadata(parse_exif_datetime(str(value), timezone), key)
            except ValueError:
                continue
    return MediaMetadata(drive_file.created_at or drive_file.modified_at, "drive_createdTime")


def read_exiftool(path: Path) -> dict[str, str]:
    """Read timestamp tags; returns {} so callers fall back to source timestamps on any failure."""
    try:
        result = subprocess.run(["exiftool", "-json", "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-MediaCreateDate", "-TrackCreateDate", "-Keywords", str(path)], check=False, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT_SECONDS)
    except FileNotFoundError:
        LOGGER.warning("ExifTool is not installed; using file timestamps instead of capture time")
        return {}
    except subprocess.TimeoutExpired:
        LOGGER.warning("ExifTool timed out reading %s; using file timestamps", path.name)
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return payload[0] if payload else {}


def extract_metadata(path: Path, kind: MediaKind, timezone: ZoneInfo, drive_file: DriveFile) -> MediaMetadata:
    tags = read_exiftool(path)
    metadata = choose_metadata_timestamp(tags, kind, timezone, drive_file)
    raw_keywords: object = tags.get("Keywords", [])
    keywords = tuple(raw_keywords if isinstance(raw_keywords, list) else [str(raw_keywords)]) if raw_keywords else ()
    return MediaMetadata(metadata.captured_at, metadata.source, keywords)
