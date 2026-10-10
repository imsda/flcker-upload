"""Media sources the worker can pull from: a Google Drive folder or a local/NAS folder."""
from __future__ import annotations

import logging
import ntpath
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from .drive import GoogleDriveClient
from .models import DriveFile

LOGGER = logging.getLogger(__name__)

LOCAL_ID_PREFIX = "local:"
SMB_ID_PREFIX = "smb:"
SMB_COPY_CHUNK = 4 * 1024 * 1024
SMB_CONNECT_TIMEOUT_SECONDS = 15
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def skip_entry(name: str) -> bool:
    """Hidden files and NAS housekeeping folders such as @eaDir, #recycle and .ugreen*."""
    return name.startswith((".", "@", "#"))


def clean_relative(value: str) -> str:
    """Normalise a folder inside a share to ``a/b`` form, rejecting attempts to climb out."""
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("Folder paths cannot contain '..'")
    return "/".join(parts)


def parse_smb_location(value: str) -> tuple[str, str, str]:
    """Split ``smb://nas/share/folder`` or ``\\\\nas\\share\\folder`` into (server, share, folder).

    A bare host name returns empty share and folder.
    """
    value = value.strip()
    if value.lower().startswith("smb://"):
        parsed = urlparse(value)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        server = parsed.hostname or ""
    else:
        parts = [p for p in value.replace("\\", "/").split("/") if p]
        server = parts.pop(0) if parts else ""
    share = parts[0] if parts else ""
    return server, share, "/".join(parts[1:])


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
                if skip_entry(entry.name):
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


class SMBSource:
    """Reads media from a Windows/SMB network share (Ugreen, Synology, QNAP, TrueNAS, Windows).

    Connects with the app's own credentials, so nothing has to be mounted on the
    server. Files are copied into staging for processing, like Google Drive files.
    File IDs are ``smb:<path relative to the watched folder>``.
    """

    label = "Network share (SMB)"
    staged = True

    def __init__(self, server: str, share: str, folder: str = "", *, username: str = "", password: str = "", port: int = 445, recursive: bool = True, exclude: tuple[str, ...] = (), client: Any = None, connection_timeout: int = SMB_CONNECT_TIMEOUT_SECONDS) -> None:
        if client is None:
            import smbclient  # imported lazily so the dependency is only needed for SMB

            client = smbclient
        self.client = client
        self.server = server
        self.share = share
        self.folder = clean_relative(folder)
        self.recursive = recursive
        self.exclude = {clean_relative(e).casefold() for e in exclude if e.strip()}
        self.auth = {"username": username or None, "password": password or None, "port": port, "connection_timeout": connection_timeout}

    def unc(self, relative_to_share: str = "") -> str:
        path = "\\\\" + self.server + "\\" + self.share
        parts = [p for p in clean_relative(relative_to_share).split("/") if p]
        return ntpath.join(path, *parts) if parts else path

    def _share_relative(self, file_id: str) -> str:
        if not file_id.startswith(SMB_ID_PREFIX):
            raise ValueError(f"Not an SMB file ID: {file_id}")
        relative = clean_relative(file_id[len(SMB_ID_PREFIX):])
        return f"{self.folder}/{relative}" if self.folder else relative

    @staticmethod
    def _kind(entry: Any) -> str | None:
        # Use the attributes returned by the directory listing itself. DirEntry.is_dir()/stat()
        # can open a second connection that ignores the configured port.
        attributes = entry.smb_info.file_attributes
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return None  # symlinks/junctions: skip rather than risk loops
        return "dir" if attributes & FILE_ATTRIBUTE_DIRECTORY else "file"

    def list_folders(self, folder: str = "") -> list[dict[str, str]]:
        """Subfolders of ``folder`` (relative to the share root) for the folder picker."""
        folder = clean_relative(folder)
        with self.client.scandir(self.unc(folder), **self.auth) as entries:
            names = sorted((e.name for e in entries if not skip_entry(e.name) and self._kind(e) == "dir"), key=str.lower)
        return [{"name": name, "path": f"{folder}/{name}" if folder else name} for name in names]

    def list_media_files(self) -> list[DriveFile]:
        files: list[DriveFile] = []
        pending = [self.folder]
        while pending:
            current = pending.pop()
            try:
                with self.client.scandir(self.unc(current), **self.auth) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                if current == self.folder:
                    raise
                LOGGER.warning("Cannot read SMB folder %s: %s", current, exc)
                continue
            for entry in entries:
                if skip_entry(entry.name):
                    continue
                share_relative = f"{current}/{entry.name}" if current else entry.name
                kind = self._kind(entry)
                if kind == "dir":
                    if self.recursive and share_relative.casefold() not in self.exclude:
                        pending.append(share_relative)
                    continue
                if kind != "file":
                    continue
                info = entry.smb_info
                relative = str(PurePosixPath(share_relative).relative_to(self.folder)) if self.folder else share_relative
                modified = info.last_write_time.astimezone(UTC)
                created = info.creation_time.astimezone(UTC) if info.creation_time else modified
                files.append(DriveFile(id=SMB_ID_PREFIX + relative, name=entry.name, mime_type="", created_at=created, modified_at=modified, path=share_relative, size=info.end_of_file))
        return files

    def fetch(self, media_file: DriveFile, staging_dir: Path) -> Path:
        destination = staging_dir / safe_filename(media_file.id.replace("/", "_")) / safe_filename(media_file.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.client.open_file(self.unc(self._share_relative(media_file.id)), mode="rb", **self.auth) as remote, destination.open("wb") as local:
            shutil.copyfileobj(remote, local, SMB_COPY_CHUNK)
        return destination

    def move_file(self, file_id: str, destination: str) -> None:
        """Move a file to ``destination``, a folder path relative to the share root."""
        source_relative = self._share_relative(file_id)
        target_folder = clean_relative(destination)
        self.client.makedirs(self.unc(target_folder), exist_ok=True, **self.auth)
        name = PurePosixPath(source_relative)
        target = f"{target_folder}/{name.name}" if target_folder else name.name
        counter = 1
        while self.client.path.exists(self.unc(target), **self.auth):
            candidate = f"{name.stem} ({counter}){name.suffix}"
            target = f"{target_folder}/{candidate}" if target_folder else candidate
            counter += 1
        self.client.rename(self.unc(source_relative), self.unc(target), **self.auth)


# NTSTATUS codes smbprotocol reports for common setup mistakes.
STATUS_LOGON_FAILURE = 0xC000006D
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_BAD_NETWORK_NAME = 0xC00000CC
STATUS_NOT_FOUND = 0xC0000225
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A


def friendly_smb_error(exc: Exception) -> str:
    """Turn smbprotocol exceptions into advice an admin can act on."""
    name = type(exc).__name__
    status = getattr(exc, "ntstatus", None) or getattr(exc, "status", None)
    text = str(exc)
    if name == "LogonFailure" or status == STATUS_LOGON_FAILURE or "STATUS_LOGON_FAILURE" in text:
        return "The NAS rejected the username or password. Check the account on the NAS and re-enter the password."
    if name == "BadNetworkName" or status in (STATUS_BAD_NETWORK_NAME, STATUS_NOT_FOUND):
        return "The NAS has no shared folder with that name. Check the shared folder name on the NAS."
    if name == "AccessDenied" or status == STATUS_ACCESS_DENIED or "STATUS_ACCESS_DENIED" in text:
        return "The NAS account does not have permission to read this folder. Grant it read access (or read/write to move files) on the NAS."
    if name in {"ObjectNameNotFound", "ObjectPathNotFound"} or status in (STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND):
        return "That folder does not exist on the share. Pick it with the folder browser below."
    if "Failed to connect" in text or isinstance(exc, (ConnectionError, TimeoutError)):
        return "Could not reach the NAS. Check the server name or IP address, that SMB is enabled on the NAS, and that this server can reach it on the SMB port (normally 445)."
    return text or "The SMB connection failed."
