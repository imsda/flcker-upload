"""Google OAuth and resource discovery helpers for the web UI."""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .secrets import SecretStore

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
]

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_CERT_URL = "https://www.googleapis.com/oauth2/v1/certs"


class GoogleOAuthConfigError(RuntimeError):
    """Raised when the Google OAuth application client is not configured."""


def configured(secrets: SecretStore, client_file: Path) -> bool:
    return bool(secrets.get("google_client_id") and secrets.get("google_client_secret")) or client_file.exists()


def client_config(client_file: Path, secrets: SecretStore) -> dict:
    client_id = secrets.get("google_client_id")
    client_secret = secrets.get("google_client_secret")
    if client_id and client_secret:
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "auth_provider_x509_cert_url": GOOGLE_CERT_URL,
            }
        }
    if client_file.exists():
        data = json.loads(client_file.read_text())
        web = data.get("web") or data.get("installed") or {}
        if not web.get("client_id"):
            raise GoogleOAuthConfigError("Google OAuth client configuration is missing a Client ID.")
        if not web.get("client_secret"):
            raise GoogleOAuthConfigError("Google OAuth client configuration is missing a Client Secret.")
        return {"web": web}
    if not client_id:
        raise GoogleOAuthConfigError("Google OAuth application credentials have not been configured. Enter your Client ID and Client Secret above before connecting an account.")
    raise GoogleOAuthConfigError("Google OAuth application credentials are incomplete. Enter both Client ID and Client Secret before connecting an account.")


def flow_for(client_file: Path, secrets: SecretStore, redirect_uri: str, state: str | None = None) -> Flow:
    flow = Flow.from_client_config(client_config(client_file, secrets), scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_uri
    return flow


def credentials(secrets: SecretStore) -> Credentials:
    token_json = secrets.get("google_token_json")
    if not token_json:
        raise GoogleOAuthConfigError("Google account is not connected. Connect or reauthorize the Google account first.")
    return Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)


def authed_service(secrets: SecretStore, api: str, version: str):
    creds = credentials(secrets)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        secrets.set("google_token_json", creds.to_json())
    return build(api, version, credentials=creds, cache_discovery=False)


def account_email(secrets: SecretStore) -> str:
    svc = authed_service(secrets, "oauth2", "v2")
    return str(svc.userinfo().get().execute().get("email", ""))


def list_folders(secrets: SecretStore, parent: str | None = None) -> list[dict]:
    svc = authed_service(secrets, "drive", "v3")
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        q += f" and '{parent}' in parents"
    fields = "nextPageToken, files(id,name,driveId,capabilities(canAddChildren,canEdit),ownedByMe,owners(emailAddress),permissions(role,type))"
    out: list[dict] = []
    token = None
    while True:
        resp = svc.files().list(q=q, spaces="drive", fields=fields, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives").execute()
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return out


def test_folder(secrets: SecretStore, folder_id: str) -> dict:
    svc = authed_service(secrets, "drive", "v3")
    return dict(svc.files().get(fileId=folder_id, fields="id,name,capabilities(canAddChildren,canEdit),ownedByMe", supportsAllDrives=True).execute())


def list_calendars(secrets: SecretStore) -> list[dict]:
    svc = authed_service(secrets, "calendar", "v3")
    items: list[dict] = []
    token = None
    while True:
        resp = svc.calendarList().list(pageToken=token).execute()
        items.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            return items


def test_calendar(secrets: SecretStore, calendar_id: str) -> dict:
    svc = authed_service(secrets, "calendar", "v3")
    return dict(svc.calendarList().get(calendarId=calendar_id).execute())
