import os
import subprocess
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from drive_to_flickr.calendar import GoogleCalendarClient
from drive_to_flickr.config import Settings
from drive_to_flickr.database import Database
from drive_to_flickr.flickr import PHOTO_ALREADY_IN_SET, PHOTOSET_NOT_FOUND, FlickrAPIError, FlickrClient
from drive_to_flickr.matcher import parse_event_description
from drive_to_flickr.metadata import read_exiftool
from drive_to_flickr.models import DriveFile, Status
from drive_to_flickr.processor import Processor, retry_delay
from drive_to_flickr.sources import GoogleDriveSource, LocalFolderSource, safe_filename

TZ = ZoneInfo("America/Chicago")
OLD = datetime(2026, 6, 4, 12, tzinfo=UTC)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(timezone=TZ, poll_interval_seconds=60, database_path=tmp_path / "state.sqlite", staging_dir=tmp_path / "staging", google_credentials_file=tmp_path / "c.json", google_token_file=tmp_path / "t.json", google_drive_folder_id="folder", google_calendar_id="cal", flickr_api_key="k", flickr_api_secret="s", flickr_oauth_token="t", flickr_oauth_token_secret="ts", minimum_file_age_seconds=0)
    return replace(base, **overrides)


class FakeDriveSource:
    """Google-Drive-like source that writes a small file into staging."""

    label = "fake drive"
    staged = True

    def __init__(self, files):
        self.files = files
        self.fetched = []
        self.moved = []

    def list_media_files(self):
        return list(self.files)

    def fetch(self, media_file, staging_dir):
        self.fetched.append(media_file.id)
        path = staging_dir / safe_filename(media_file.id) / safe_filename(media_file.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpeg")
        return path

    def move_file(self, file_id, destination):
        self.moved.append((file_id, destination))


def drive_file(file_id="f1", name="a.jpg"):
    return DriveFile(file_id, name, "image/jpeg", OLD, OLD)


def fake_calendar(events=()):
    calendar = Mock()
    calendar.events_for_day.return_value = list(events)
    return calendar


def fake_flickr():
    flickr = Mock()
    flickr.upload.return_value = "photo-1"
    flickr.find_uploaded_by_drive_id.return_value = None
    flickr.list_photosets.return_value = {}
    flickr.create_photoset.return_value = "set-1"
    return flickr


def build(tmp_path, files=None, source=None, calendar=None, flickr=None, **settings):
    db = Database(tmp_path / "state.sqlite")
    source = source or FakeDriveSource(files or [drive_file()])
    proc = Processor(make_settings(tmp_path, **settings), db, source, calendar or fake_calendar(), flickr or fake_flickr())
    return proc, db, source


@pytest.fixture(autouse=True)
def no_exiftool():
    with patch("drive_to_flickr.processor.extract_metadata") as extract:
        extract.side_effect = lambda path, kind, tz, f: Mock(captured_at=datetime(2026, 6, 4, 19, 42, tzinfo=TZ), tags=())
        yield extract


def test_completed_failed_and_manual_review_files_are_not_reprocessed(tmp_path):
    files = [drive_file("done"), drive_file("failed"), drive_file("review")]
    proc, db, source = build(tmp_path, files)
    for f in files:
        db.upsert_discovered(f)
    db.mark_complete("done", "set")
    db.update_status("failed", Status.FAILED, "boom")
    db.update_status("review", Status.MANUAL_REVIEW, "no event")
    assert proc.scan() == 0
    assert source.fetched == []


def test_retry_waits_for_backoff_then_runs(tmp_path):
    proc, db, source = build(tmp_path)
    db.upsert_discovered(drive_file())
    db.schedule_retry("f1", timedelta(hours=1), "later")
    assert proc.scan() == 0
    db.schedule_retry("f1", timedelta(seconds=-1), "now")
    assert proc.scan() == 1
    assert db.is_processed("f1")


def test_failure_schedules_retry_without_sleeping_and_cleans_staging(tmp_path):
    flickr = fake_flickr()
    flickr.upload.side_effect = ConnectionError("network down")
    proc, db, source = build(tmp_path, flickr=flickr)
    with patch("time.sleep") as sleep:
        proc.scan()
    sleep.assert_not_called()
    row = db.get_file("f1")
    assert row["status"] == Status.RETRY
    assert row["attempts"] == 1
    assert datetime.fromisoformat(row["next_retry_at"]) > datetime.now(UTC)
    assert list((tmp_path / "staging").iterdir()) == []


def test_permanent_failure_moves_to_failed_folder(tmp_path):
    flickr = fake_flickr()
    flickr.upload.side_effect = RuntimeError("bad file")
    proc, db, source = build(tmp_path, flickr=flickr, max_attempts=1, drive_failed_folder="failed-folder")
    proc.scan()
    assert db.get_file("f1")["status"] == Status.FAILED
    assert source.moved == [("f1", "failed-folder")]


def test_success_cleans_staging_and_completes(tmp_path):
    proc, db, source = build(tmp_path)
    proc.scan()
    assert db.is_processed("f1")
    assert list((tmp_path / "staging").iterdir()) == []


def test_new_album_does_not_add_primary_photo_twice(tmp_path):
    flickr = fake_flickr()
    proc, db, _ = build(tmp_path, flickr=flickr)
    proc.scan()
    flickr.create_photoset.assert_called_once_with("Unassigned Uploads", "photo-1")
    flickr.add_photo_to_photoset.assert_not_called()
    assert db.get_file("f1")["flickr_photoset_id"] == "set-1"


def test_existing_album_gets_photo_added(tmp_path):
    flickr = fake_flickr()
    flickr.list_photosets.return_value = {"unassigned uploads": ("set-9", "Unassigned Uploads")}
    proc, db, _ = build(tmp_path, flickr=flickr)
    proc.scan()
    flickr.add_photo_to_photoset.assert_called_once_with("set-9", "photo-1")
    flickr.create_photoset.assert_not_called()


def test_album_deleted_on_flickr_is_recreated(tmp_path):
    flickr = fake_flickr()
    flickr.add_photo_to_photoset.side_effect = FlickrAPIError(PHOTOSET_NOT_FOUND, "Photoset not found")
    proc, db, _ = build(tmp_path, flickr=flickr)
    db.save_photoset("unassigned uploads", "gone", "Unassigned Uploads")
    proc.scan()
    flickr.create_photoset.assert_called_once()
    assert db.get_photoset("unassigned uploads") == "set-1"


def test_add_photo_ignores_already_in_set():
    client = FlickrClient("k", "s", "t", "ts")
    client._call = Mock(side_effect=FlickrAPIError(PHOTO_ALREADY_IN_SET, "Photo already in set"))
    client.add_photo_to_photoset("set", "photo")
    client._call.side_effect = FlickrAPIError(2, "Photo not found")
    with pytest.raises(FlickrAPIError):
        client.add_photo_to_photoset("set", "photo")


def test_first_attempt_skips_duplicate_search_but_retry_searches(tmp_path):
    flickr = fake_flickr()
    proc, db, _ = build(tmp_path, flickr=flickr)
    proc.scan()
    flickr.find_uploaded_by_drive_id.assert_not_called()

    flickr2 = fake_flickr()
    flickr2.find_uploaded_by_drive_id.return_value = "existing"
    proc2, db2, _ = build(tmp_path / "second", files=[drive_file("f2")], flickr=flickr2)
    db2.upsert_discovered(drive_file("f2"))
    db2.update_status("f2", Status.UPLOADING)  # simulates a crash mid-upload
    proc2.scan()
    flickr2.upload.assert_not_called()
    assert db2.get_file("f2")["flickr_photo_id"] == "existing"


def test_known_flickr_photo_skips_download_and_upload(tmp_path):
    flickr = fake_flickr()
    proc, db, source = build(tmp_path, flickr=flickr)
    db.upsert_discovered(drive_file())
    db.mark_uploaded("f1", datetime(2026, 6, 4, 19, tzinfo=TZ), None, None, "photo-7")
    db.increment_attempts("f1", "album step failed")
    db.schedule_retry("f1", timedelta(seconds=-1))
    proc.scan()
    assert source.fetched == []
    flickr.upload.assert_not_called()
    assert db.is_processed("f1")


def test_calendar_is_fetched_once_per_day_per_scan(tmp_path):
    calendar = fake_calendar()
    files = [drive_file(f"f{i}", f"{i}.jpg") for i in range(4)]
    proc, db, _ = build(tmp_path, files, calendar=calendar)
    proc.scan()
    calendar.events_for_day.assert_called_once_with("cal", date(2026, 6, 4))


def test_no_event_skip_waits_before_checking_calendar_again(tmp_path):
    proc, db, source = build(tmp_path, no_event_action="skip")
    proc.scan()
    row = db.get_file("f1")
    assert row["status"] == Status.RETRY
    assert datetime.fromisoformat(row["next_retry_at"]) > datetime.now(UTC) + timedelta(minutes=30)
    assert proc.scan() == 0


def test_parallel_scan_processes_every_file_once(tmp_path):
    flickr = fake_flickr()
    counter = iter(range(1000))
    lock = threading.Lock()

    def upload(*a, **k):
        with lock:
            return f"photo-{next(counter)}"

    flickr.upload.side_effect = upload
    files = [drive_file(f"f{i}", f"{i}.jpg") for i in range(12)]
    proc, db, _ = build(tmp_path, files, flickr=flickr, worker_concurrency=4)
    assert proc.scan() == 12
    assert all(db.is_processed(f.id) for f in files)
    assert flickr.upload.call_count == 12
    flickr.create_photoset.assert_called_once()


def test_dry_run_does_not_write_state(tmp_path, capsys):
    flickr = fake_flickr()
    proc, db, _ = build(tmp_path, flickr=flickr, no_event_action="skip")
    proc.scan(dry_run=True)
    assert db.get_file("f1") is None
    flickr.upload.assert_not_called()
    assert "a.jpg" in capsys.readouterr().out


def test_should_stop_halts_scan(tmp_path):
    proc, db, source = build(tmp_path, [drive_file("a"), drive_file("b")])
    proc.scan(should_stop=lambda: True)
    assert source.fetched == []


def test_retry_delay_backoff():
    assert retry_delay(1) == timedelta(minutes=1)
    assert retry_delay(3) == timedelta(minutes=4)
    assert retry_delay(20) == timedelta(hours=1)


# --- local / NAS source -------------------------------------------------


def test_local_source_lists_recursively_and_skips_hidden_and_excluded(tmp_path):
    root = tmp_path / "nas"
    (root / "2026" / "june").mkdir(parents=True)
    (root / ".snapshots").mkdir()
    (root / "@eaDir").mkdir()
    (root / "uploaded").mkdir()
    (root / "top.jpg").write_bytes(b"x")
    (root / "2026" / "june" / "deep.mov").write_bytes(b"x")
    (root / ".hidden.jpg").write_bytes(b"x")
    (root / ".snapshots" / "old.jpg").write_bytes(b"x")
    (root / "@eaDir" / "thumb.jpg").write_bytes(b"x")
    (root / "uploaded" / "done.jpg").write_bytes(b"x")
    source = LocalFolderSource(root, exclude=(root / "uploaded",))
    ids = sorted(f.id for f in source.list_media_files())
    assert ids == ["local:2026/june/deep.mov", "local:top.jpg"]
    flat = LocalFolderSource(root, recursive=False, exclude=(root / "uploaded",))
    assert [f.id for f in flat.list_media_files()] == ["local:top.jpg"]


def test_local_source_missing_mount_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="mounted"):
        LocalFolderSource(tmp_path / "not-mounted").list_media_files()


def test_local_source_rejects_path_traversal(tmp_path):
    source = LocalFolderSource(tmp_path)
    with pytest.raises(ValueError):
        source.path_for("local:../etc/passwd")
    with pytest.raises(ValueError):
        source.path_for("drive-id")


def test_local_source_move_avoids_overwriting(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"new")
    done = tmp_path / "done"
    done.mkdir()
    (done / "a.jpg").write_bytes(b"old")
    LocalFolderSource(root).move_file("local:a.jpg", str(done))
    assert (done / "a.jpg").read_bytes() == b"old"
    assert (done / "a (1).jpg").read_bytes() == b"new"
    assert not (root / "a.jpg").exists()


def test_processor_with_local_source_reads_in_place_and_moves_on_success(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    photo = root / "event.jpg"
    photo.write_bytes(b"jpeg")
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(photo, (old, old))
    flickr = fake_flickr()
    uploaded_paths = []
    flickr.upload.side_effect = lambda path, **kw: uploaded_paths.append(path) or "photo-1"
    source = LocalFolderSource(root)
    proc, db, _ = build(tmp_path, source=source, flickr=flickr, source_type="local", local_source_path=root, drive_success_action="move", local_success_folder=tmp_path / "uploaded")
    proc.scan()
    assert uploaded_paths == [photo]
    assert db.is_processed("local:event.jpg")
    assert (tmp_path / "uploaded" / "event.jpg").exists()
    assert not (tmp_path / "staging").exists() or list((tmp_path / "staging").iterdir()) == []


def test_local_source_original_kept_after_failure(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    photo = root / "event.jpg"
    photo.write_bytes(b"jpeg")
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(photo, (old, old))
    flickr = fake_flickr()
    flickr.upload.side_effect = ConnectionError("down")
    proc, db, _ = build(tmp_path, source=LocalFolderSource(root), flickr=flickr, source_type="local", local_source_path=root)
    proc.scan()
    assert photo.exists()
    assert db.get_file("local:event.jpg")["status"] == Status.RETRY


def test_google_drive_source_sanitises_staging_name(tmp_path):
    client = Mock()
    source = GoogleDriveSource(client, "folder")
    path = source.fetch(DriveFile("id1", "../../evil.jpg", "image/jpeg", OLD, OLD), tmp_path)
    assert path == tmp_path / "id1" / "evil.jpg"
    assert safe_filename("..") == "media"


# --- smaller fixes ------------------------------------------------------


def test_missing_exiftool_falls_back(tmp_path):
    with patch("drive_to_flickr.metadata.subprocess.run", side_effect=FileNotFoundError("exiftool")):
        assert read_exiftool(tmp_path / "a.jpg") == {}
    with patch("drive_to_flickr.metadata.subprocess.run", side_effect=subprocess.TimeoutExpired("exiftool", 1)):
        assert read_exiftool(tmp_path / "a.jpg") == {}


def test_bad_buffer_value_is_ignored():
    settings = parse_event_description("FLICKR: true\nBuffer Before: soon\nBuffer After: 30")
    assert settings.buffer_before is None
    assert settings.buffer_after == timedelta(minutes=30)


def test_calendar_query_covers_adjacent_days_for_buffers(tmp_path):
    with patch("drive_to_flickr.calendar.build") as build_service:
        client = GoogleCalendarClient(tmp_path / "c.json", tmp_path / "t.json", TZ, Mock())
    service = build_service.return_value
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    client.events_around("cal", datetime(2026, 6, 4, 23, 30, tzinfo=TZ))
    kwargs = service.events.return_value.list.call_args.kwargs
    assert kwargs["timeMin"].startswith("2026-06-03T00:00:00")
    assert kwargs["timeMax"].startswith("2026-06-06T00:00:00")


def test_retry_cli_queues_failed_and_scans():
    from drive_to_flickr import main as cli

    proc, db = Mock(), Mock()
    db.queue_all_failed.return_value = 2
    with patch.object(cli, "build_processor", return_value=(proc, db)):
        assert cli.main(["retry"]) == 0
    db.queue_all_failed.assert_called_once()
    proc.scan.assert_called_once()
