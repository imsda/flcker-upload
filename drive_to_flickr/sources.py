"""Media sources the worker can pull from: a Google Drive folder or a local/NAS folder."""
from __future__ import annotations

import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .drive import GoogleDriveClient
from .models import DriveFile

LOGGER = logging.getLogger(__name__)

LOCAL_ID_PREFIX = "local:"


def safe_filename(name: str) -> str:
    """Strip directory components so a remote file name cannot escape the staging folder."""
    cleaned = Path(name.replace("\\", "/")).name.strip()
    return cleaned if cleaned not in {"", ".", ".."} else "media"


class MediaSource(Protocol):
    """Where media files come from.

    ``fetch`` returns a local path the pipeline can read. Sources that copy the
    file into the staging area set ``staged = True`` so the processor deletes
    the copy afterwards; sources that read files in place leave originals alone.
    """

    label: str
    staged: bool

    def list_media_files(self) -> list[DriveFile]: ...

    def fetch(self, media_file: DriveFile, staging_dir: Path) -> Path: ...

    def move_file(self, file_id: str, destination: str) -> None: ...


class GoogleDriveSource:
    label = "Google Drive"
    staged = True

    def __init__(self, client: GoogleDriveClient, folder_id: str) -> None:
        self.client = client
        self.folder_id = folder_id

    def list_media_files(self) -> list[DriveFile]:
        return self.client.list_media_files(self.folder_id)

    def fetch(self, media_file: DriveFile, staging_dir: Path) -> Path:
        destination = staging_dir / safe_filename(media_file.id) / safe_filename(media_file.name)
        self.client.download(media_file.id, destination)
        return destination

    def move_file(self, file_id: str, destination: str) -> None:
        self.client.move_file(file_id, destination)


class LocalFolderSource:
    """Reads media directly from a folder on this machine, such as a mounted NAS share.

    File IDs are ``local:<path relative to the root>`` so they stay stable across
    scans and do not collide with Google Drive IDs in the database.
    """

    label = "Local/NAS folder"
    staged = False

    def __init__(self, root: Path, *, recursive: bool = True, exclude: tuple[Path, ...] = ()) -> None:
        self.root = root
        self.recursive = recursive
        self.exclude = tuple(p.resolve() for p in exclude)

    def _relative(self, file_id: str) -> Path:
        if not file_id.startswith(LOCAL_ID_PREFIX):
            raise ValueError(f"Not a local file ID: {file_id}")
        relative = Path(file_id[len(LOCAL_ID_PREFIX):])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid local file ID: {file_id}")
        return relative

    def path_for(self, file_id: str) -> Path:
        return self.root / self._relative(file_id)

    def list_media_files(self) -> list[DriveFile]:
        if not self.root.is_dir():
            raise FileNotFoundError(f"Local media folder is not available: {self.root}. Check that the NAS share is mounted.")
        files: list[DriveFile] = []
        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                LOGGER.warning("Cannot read folder %s: %s", directory, exc)
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.name.startswith("@"):
                    # Hidden files plus NAS metadata folders such as Synology's @eaDir.
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if self.recursive and Path(entry.path).resolve() not in self.exclude:
                            pending.append(Path(entry.path))
                        continue
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError as exc:
                    LOGGER.warning("Cannot read %s: %s", entry.path, exc)
                    continue
                relative = Path(entry.path).relative_to(self.root).as_posix()
                modified = datetime.fromtimestamp(stat.st_mtime, UTC)
                birth = getattr(stat, "st_birthtime", None)
                created = datetime.fromtimestamp(birth, UTC) if birth else modified
                files.append(DriveFile(id=LOCAL_ID_PREFIX + relative, name=entry.name, mime_type="", created_at=created, modified_at=modified, path=relative, size=stat.st_size))
        return files

    def fetch(self, media_file: DriveFile, staging_dir: Path) -> Path:
        path = self.path_for(media_file.id)
        if not path.is_file():
            raise FileNotFoundError(f"Local media file no longer exists: {path}")
        return path

    def move_file(self, file_id: str, destination: str) -> None:
        source = self.path_for(file_id)
        target_dir = Path(destination)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        counter = 1
        while target.exists():
            target = target_dir / f"{source.stem} ({counter}){source.suffix}"
            counter += 1
        shutil.move(str(source), str(target))
