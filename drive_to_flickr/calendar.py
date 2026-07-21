"""Google Calendar integration and conversion to domain events."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .matcher import parse_event_description
from .models import CalendarEvent

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarClient:
    def __init__(self, credentials_file: Path, token_file: Path, timezone: ZoneInfo) -> None:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES + ["https://www.googleapis.com/auth/drive"])
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.timezone = timezone

    @staticmethod
    def authorize(credentials_file: Path, token_file: Path) -> None:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES + ["https://www.googleapis.com/auth/drive"])
        creds = flow.run_console()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)

    def events_around(self, calendar_id: str, when: datetime, window_days: int = 7) -> list[CalendarEvent]:
        start = (when.replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
        end = (when.replace(hour=23, minute=59, second=59, microsecond=0)).isoformat()
        response = self.service.events().list(calendarId=calendar_id, timeMin=start, timeMax=end, singleEvents=True, orderBy="startTime").execute()
        return [self._convert(e) for e in response.get("items", [])]

    def _convert(self, e: dict) -> CalendarEvent:
        all_day = "date" in e.get("start", {})
        if all_day:
            start_date = datetime.fromisoformat(e["start"]["date"]).date()
            end_date = datetime.fromisoformat(e["end"]["date"]).date()
            start = datetime.combine(start_date, time.min, self.timezone)
            end = datetime.combine(end_date, time.min, self.timezone)
        else:
            start = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(e["end"]["dateTime"].replace("Z", "+00:00"))
        created = datetime.fromisoformat(e.get("created", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
        return CalendarEvent(e["id"], e.get("summary", "Untitled"), start, end, created, all_day, parse_event_description(e.get("description")))
