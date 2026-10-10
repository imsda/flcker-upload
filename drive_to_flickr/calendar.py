"""Google Calendar integration and conversion to domain events."""
from __future__ import annotations

import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .matcher import parse_event_description
from .models import CalendarEvent

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarClient:
    def __init__(self, credentials_file: Path, token_file: Path, timezone: ZoneInfo, credentials: Credentials | None = None) -> None:
        self._credentials = credentials or Credentials.from_authorized_user_file(str(token_file), SCOPES + ["https://www.googleapis.com/auth/drive"])
        # One service per thread: the underlying httplib2 connection is not thread-safe.
        self._local = threading.local()
        self._local.service = build("calendar", "v3", credentials=self._credentials, cache_discovery=False)
        self.timezone = timezone

    @property
    def service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            service = self._local.service = build("calendar", "v3", credentials=self._credentials, cache_discovery=False)
        return service

    @staticmethod
    def authorize(credentials_file: Path, token_file: Path) -> None:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES + ["https://www.googleapis.com/auth/drive"])
        creds = flow.run_console()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)

    def events_around(self, calendar_id: str, when: datetime, window_days: int = 1) -> list[CalendarEvent]:
        """Events overlapping the local day of ``when`` plus ``window_days`` either side.

        The extra days let event buffers reach across midnight (for example a
        ``Buffer Before: 360`` on an event that starts at 3am the next day).
        """
        return self.events_for_day(calendar_id, when.astimezone(self.timezone).date(), window_days)

    def events_for_day(self, calendar_id: str, day, window_days: int = 1) -> list[CalendarEvent]:
        start = datetime.combine(day - timedelta(days=window_days), time.min, self.timezone).isoformat()
        end = datetime.combine(day + timedelta(days=window_days + 1), time.min, self.timezone).isoformat()
        events: list[CalendarEvent] = []
        token = None
        while True:
            response = self.service.events().list(calendarId=calendar_id, timeMin=start, timeMax=end, singleEvents=True, orderBy="startTime", pageToken=token).execute()
            events.extend(self._convert(e) for e in response.get("items", []))
            token = response.get("nextPageToken")
            if not token:
                return events

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
