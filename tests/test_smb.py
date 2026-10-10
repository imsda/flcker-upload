import ntpath
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from drive_to_flickr.models import Status
from drive_to_flickr.sources import (
    FILE_ATTRIBUTE_DIRECTORY,
    STATUS_LOGON_FAILURE,
    SMBSource,
    friendly_smb_error,
    parse_smb_location,
)
from test_processor import build, fake_flickr


class FakeSMBClient:
    """Stands in for the smbclient module, serving \\\\nas\\<share>\\... from a local folder."""

    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.path = SimpleNamespace(exists=lambda p, **kw: self._local(p).exists())

    def _local(self, unc: str) -> Path:
        parts = [p for p in unc.split("\\") if p]
        assert parts[0] == "nas", unc
        return self.root.joinpath(*parts[1:])

    def scandir(self, unc, **kwargs):
        self.calls.append(("scandir", unc, kwargs))
        local = self._local(unc)
        entries = []
        for e in os.scandir(local):
            st = e.stat()
            entries.append(SimpleNamespace(name=e.name, smb_info=SimpleNamespace(
                file_attributes=FILE_ATTRIBUTE_DIRECTORY if e.is_dir() else 0x80,
                last_write_time=datetime.fromtimestamp(st.st_mtime, UTC),
                creation_time=datetime.fromtimestamp(st.st_mtime, UTC),
                end_of_file=st.st_size)))

        class Iterator(list):
            def __enter__(self):
                return iter(self)

            def __exit__(self, *exc):
                return False

        return Iterator(entries)

    def open_file(self, unc, mode="rb", **kwargs):
        self.calls.append(("open_file", unc, kwargs))
        return self._local(unc).open(mode)

    def makedirs(self, unc, exist_ok=False, **kwargs):
        self._local(unc).mkdir(parents=True, exist_ok=exist_ok)

    def rename(self, src, dst, **kwargs):
        self.calls.append(("rename", src, dst))
        shutil.move(self._local(src), self._local(dst))


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "nas"
    photos = root / "Photos"
    (photos / "Events" / "Camp Meeting 2026").mkdir(parents=True)
    (photos / "Events" / "#recycle").mkdir()
    (photos / "Events" / "@eaDir").mkdir()
    (photos / "Events" / ".ugreen").mkdir()
    (photos / "Events" / "Uploaded").mkdir()
    (photos / "Events" / "Camp Meeting 2026" / "IMG_1.jpg").write_bytes(b"jpeg-1")
    (photos / "Events" / "top.mov").write_bytes(b"mov")
    (photos / "Events" / "#recycle" / "old.jpg").write_bytes(b"x")
    (photos / "Events" / "@eaDir" / "thumb.jpg").write_bytes(b"x")
    (photos / "Events" / "Uploaded" / "done.jpg").write_bytes(b"x")
    old = (datetime.now() - timedelta(hours=1)).timestamp()
    for path in photos.rglob("*"):
        os.utime(path, (old, old))
    return root


def source(share, **kwargs):
    client = FakeSMBClient(share)
    options = {"username": "photos", "password": "pw", "exclude": ("Events/Uploaded",), "client": client} | kwargs
    return SMBSource("nas", "Photos", "Events", **options), client


def test_smb_lists_media_and_skips_nas_system_folders(share):
    src, client = source(share)
    files = {f.id: f for f in src.list_media_files()}
    assert sorted(files) == ["smb:Camp Meeting 2026/IMG_1.jpg", "smb:top.mov"]
    assert files["smb:Camp Meeting 2026/IMG_1.jpg"].size == 6
    assert all(call[2] == {"username": "photos", "password": "pw", "port": 445, "connection_timeout": 15} for call in client.calls)


def test_smb_non_recursive(share):
    src, _ = source(share, recursive=False)
    assert [f.id for f in src.list_media_files()] == ["smb:top.mov"]


def test_smb_folder_browser(share):
    src, _ = source(share)
    assert src.list_folders("") == [{"name": "Events", "path": "Events"}]
    names = [d["name"] for d in src.list_folders("Events")]
    assert names == ["Camp Meeting 2026", "Uploaded"]


def test_smb_fetch_copies_into_staging(share, tmp_path):
    src, _ = source(share)
    media = next(f for f in src.list_media_files() if f.name == "IMG_1.jpg")
    path = src.fetch(media, tmp_path / "staging")
    assert path.read_bytes() == b"jpeg-1"
    assert path.is_relative_to(tmp_path / "staging")


def test_smb_move_into_share_folder_without_overwriting(share):
    src, _ = source(share)
    (share / "Photos" / "Events" / "Uploaded" / "top.mov").write_bytes(b"older")
    src.move_file("smb:top.mov", "Events/Uploaded")
    assert (share / "Photos" / "Events" / "Uploaded" / "top (1).mov").read_bytes() == b"mov"
    assert (share / "Photos" / "Events" / "Uploaded" / "top.mov").read_bytes() == b"older"


def test_smb_rejects_paths_that_escape_the_share(share):
    src, _ = source(share)
    with pytest.raises(ValueError):
        src.fetch(SimpleNamespace(id="smb:../../secret.jpg", name="secret.jpg"), share)
    with pytest.raises(ValueError):
        SMBSource("nas", "Photos", "../other", client=FakeSMBClient(share))


def test_smb_unc_paths():
    src = SMBSource("nas", "Photos", "Events/2026", client=object())
    assert src.unc() == "\\\\nas\\Photos"
    assert src.unc("Events/2026/a.jpg") == ntpath.join("\\\\nas\\Photos", "Events", "2026", "a.jpg")


def test_processor_with_smb_source_uploads_cleans_up_and_moves(share, tmp_path):
    src, _ = source(share)
    flickr = fake_flickr()
    uploaded = []
    flickr.upload.side_effect = lambda path, **kw: uploaded.append(path.read_bytes()) or f"photo-{len(uploaded)}"
    proc, db, _ = build(tmp_path, source=src, flickr=flickr, source_type="smb", drive_success_action="move", smb_success_folder="Events/Uploaded")
    assert proc.scan() == 2
    assert sorted(uploaded) == [b"jpeg-1", b"mov"]
    assert db.is_processed("smb:top.mov")
    assert (share / "Photos" / "Events" / "Uploaded" / "IMG_1.jpg").exists()
    assert list((tmp_path / "staging").iterdir()) == []
    assert proc.scan() == 0  # moved files are excluded, completed ones skipped


def test_unreachable_share_raises_and_records_nothing(tmp_path):
    client = FakeSMBClient(tmp_path / "missing")
    src = SMBSource("nas", "Photos", "Events", client=client)
    proc, db, _ = build(tmp_path, source=src, source_type="smb")
    with pytest.raises(FileNotFoundError):
        proc.scan()
    assert db.status_counts() == []


@pytest.mark.parametrize("value, expected", [
    ("smb://192.168.1.20/Photos/Camp%20Meeting", ("192.168.1.20", "Photos", "Camp Meeting")),
    ("\\\\nas\\Photos\\Events\\2026", ("nas", "Photos", "Events/2026")),
    ("//nas/Photos", ("nas", "Photos", "")),
    ("nas.local", ("nas.local", "", "")),
])
def test_parse_smb_location(value, expected):
    assert parse_smb_location(value) == expected


def test_friendly_smb_errors():
    logon = type("SMBOSError", (OSError,), {})()
    logon.ntstatus = STATUS_LOGON_FAILURE
    assert "username or password" in friendly_smb_error(logon)
    assert "Could not reach the NAS" in friendly_smb_error(ValueError("Failed to connect to 'nas:445'"))
    assert "shared folder" in friendly_smb_error(type("BadNetworkName", (Exception,), {})())


def test_status_retry_kept_when_smb_upload_fails(share, tmp_path):
    src, _ = source(share)
    flickr = fake_flickr()
    flickr.upload.side_effect = ConnectionError("down")
    proc, db, _ = build(tmp_path, source=src, flickr=flickr, source_type="smb")
    proc.scan()
    assert db.get_file("smb:top.mov")["status"] == Status.RETRY
    assert (share / "Photos" / "Events" / "top.mov").exists()
