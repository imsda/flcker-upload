"""Google Drive API integration."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from .models import DriveFile

SCOPES = ["https://www.googleapis.com/auth/drive"]
PAGE_SIZE = 1000


def parse_google_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quote_query_value(value: str) -> str:
    """Escape a value for use inside single quotes in a Drive ``q`` expression."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveClient:
    """Direct Drive API client; avoids cloud filesystem polling issues and deprecated wrappers."""

    def __init__(self, credentials_file: Path, token_file: Path, credentials: Credentials | None = None) -> None:
        self._credentials = credentials or Credentials.from_authorized_user_file(str(token_file), SCOPES)
        # googleapiclient service objects share an httplib2 connection that is not
        # thread-safe, so each worker thread gets its own service.
        self._local = threading.local()
        self._local.service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)

    @property
    def service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            service = self._local.service = build("drive", "v3", credentials=self._credentials, cache_discovery=False)
        return service

    @staticmethod
    def authorize(credentials_file: Path, token_file: Path) -> None:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_console()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)

    def list_media_files(self, folder_id: str) -> list[DriveFile]:
        query = f"'{quote_query_value(folder_id)}' in parents and trashed=false"
        fields = "nextPageToken, files(id,name,mimeType,createdTime,modifiedTime,md5Checksum,size)"
        files: list[DriveFile] = []
        token = None
        while True:
            response = self.service.files().list(q=query, fields=fields, pageSize=PAGE_SIZE, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for item in response.get("files", []):
                files.append(DriveFile(id=item["id"], name=item["name"], mime_type=item.get("mimeType", ""), created_at=parse_google_time(item["createdTime"]), modified_at=parse_google_time(item["modifiedTime"]), checksum=item.get("md5Checksum"), size=int(item["size"]) if item.get("size") else None))
            token = response.get("nextPageToken")
            if not token:
                return files

    def download(self, file_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=file_id)
        with destination.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    def move_file(self, file_id: str, destination_folder_id: str) -> None:
        file = self.service.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
        previous = ",".join(file.get("parents", []))
        self.service.files().update(fileId=file_id, addParents=destination_folder_id, removeParents=previous, fields="id, parents", supportsAllDrives=True).execute()
