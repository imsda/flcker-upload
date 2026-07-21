from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from drive_to_flickr.database import Database
from drive_to_flickr.matcher import EventMatcher, normalize_album_name, parse_event_description
from drive_to_flickr.metadata import parse_exif_datetime, choose_metadata_timestamp
from drive_to_flickr.models import CalendarEvent, DriveFile, MediaKind
from drive_to_flickr.health import record_worker_heartbeat, worker_health
from drive_to_flickr.settings_store import SettingsStore

TZ = ZoneInfo("America/Chicago")


def event(id="1", title="Camp", start=None, end=None, created=None, desc=""):
    start = start or datetime(2026, 6, 2, tzinfo=TZ)
    end = end or datetime(2026, 6, 8, tzinfo=TZ)
    created = created or datetime(2026, 1, 1, tzinfo=TZ)
    return CalendarEvent(id, title, start, end, created, True, parse_event_description(desc))


def matcher(require=False):
    return EventMatcher(buffer_before=timedelta(), buffer_after=timedelta(), require_flickr_marker=require, default_privacy="private", global_tags=("global",), unassigned_album="Unassigned Uploads")


def test_exif_date_selection_priority():
    drive = DriveFile("id", "a.jpg", "image/jpeg", datetime(2026, 1, 1, tzinfo=TZ), datetime(2026, 1, 2, tzinfo=TZ))
    md = choose_metadata_timestamp({"CreateDate": "2026:06:03 12:00:00", "DateTimeOriginal": "2026:06:04 12:00:00"}, MediaKind.PHOTO, TZ, drive)
    assert md.source == "DateTimeOriginal"
    assert md.captured_at.hour == 12


def test_timezone_conversion_naive_exif():
    assert parse_exif_datetime("2026:06:04 19:42:00", TZ).tzinfo == TZ


def test_all_day_multi_day_matching():
    ev = event(title="Camp Meeting 2026")
    selected, _ = matcher().select_event(datetime(2026, 6, 4, 19, 42, tzinfo=TZ), [ev])
    assert selected and selected.album_name == "Camp Meeting 2026"


def test_event_buffers():
    ev = event(start=datetime(2026, 6, 2, 12, tzinfo=TZ), end=datetime(2026, 6, 2, 13, tzinfo=TZ), desc="Buffer Before: 60")
    selected, _ = matcher().select_event(datetime(2026, 6, 2, 11, 30, tzinfo=TZ), [ev])
    assert selected is ev


def test_overlapping_priority_marker_album_shortest_created():
    a = event("a", "A", end=datetime(2026, 6, 9, tzinfo=TZ), desc="Album: Override")
    b = event("b", "B", desc="FLICKR: true")
    selected, _ = matcher().select_event(datetime(2026, 6, 4, tzinfo=TZ), [a, b])
    assert selected is b


def test_flickr_marker_requirement():
    selected, _ = matcher(require=True).select_event(datetime(2026, 6, 4, tzinfo=TZ), [event(desc="Album: X")])
    assert selected is None


def test_album_privacy_tags_parsing():
    settings = parse_event_description("FLICKR: true\nAlbum: X\nPrivacy: public\nTags: a, b")
    assert settings.flickr is True
    assert settings.album == "X"
    assert settings.privacy == "public"
    assert settings.tags == ("a", "b")


def test_album_name_normalization():
    assert normalize_album_name(" Camp   Meeting ") == normalize_album_name("camp meeting")


def test_duplicate_drive_file_detection(tmp_path: Path):
    db = Database(tmp_path / "state.sqlite")
    f = DriveFile("gid", "a.jpg", "image/jpeg", datetime.now(TZ), datetime.now(TZ))
    db.upsert_discovered(f)
    assert not db.is_processed("gid")
    db.mark_complete("gid", "ps")
    assert db.is_processed("gid")


def test_retry_behavior(tmp_path: Path):
    db = Database(tmp_path / "state.sqlite")
    f = DriveFile("gid", "a.jpg", "image/jpeg", datetime.now(TZ), datetime.now(TZ))
    db.upsert_discovered(f)
    assert db.increment_attempts("gid", "temporary") == 1


def test_no_calendar_event_plan():
    plan = matcher().build_plan(None)
    assert plan.album_name == "Unassigned Uploads"


def test_worker_heartbeat_health_states(tmp_path: Path):
    store = SettingsStore(Database(tmp_path / "heartbeat.sqlite"))
    current = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)
    assert worker_health("", 120, current)["status"] == "Not started"
    heartbeat = record_worker_heartbeat(store, current - timedelta(seconds=30))
    assert store.get("WORKER_HEARTBEAT") == heartbeat
    assert worker_health(heartbeat, 120, current)["status"] == "Running"
    stale = (current - timedelta(minutes=10)).isoformat()
    assert worker_health(stale, 120, current)["status"] == "Stale"
