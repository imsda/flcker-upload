"""Calendar metadata parsing and event matching."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import VALID_PRIVACY
from .models import AlbumPlan, CalendarEvent, EventSettings


def normalize_album_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def parse_event_description(description: str | None) -> EventSettings:
    values: dict[str, str] = {}
    for line in (description or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        if key in {"flickr", "album", "privacy", "tags", "description", "buffer_before", "buffer_after"}:
            values[key] = value.strip()
    flickr = None
    if "flickr" in values:
        flickr = values["flickr"].lower() in {"true", "yes", "1", "on"}
    privacy = values.get("privacy")
    if privacy and privacy not in VALID_PRIVACY:
        privacy = None
    def minutes(name: str) -> timedelta | None:
        if name not in values:
            return None
        return timedelta(minutes=int(values[name]))
    tags = tuple(t.strip() for t in values.get("tags", "").split(",") if t.strip())
    return EventSettings(flickr=flickr, album=values.get("album"), privacy=privacy, tags=tags, description=values.get("description"), buffer_before=minutes("buffer_before"), buffer_after=minutes("buffer_after"))


class EventMatcher:
    def __init__(self, *, buffer_before: timedelta, buffer_after: timedelta, require_flickr_marker: bool, default_privacy: str, global_tags: tuple[str, ...], unassigned_album: str) -> None:
        self.buffer_before = buffer_before
        self.buffer_after = buffer_after
        self.require_flickr_marker = require_flickr_marker
        self.default_privacy = default_privacy
        self.global_tags = global_tags
        self.unassigned_album = unassigned_album

    def matching_events(self, captured_at: datetime, events: list[CalendarEvent]) -> list[CalendarEvent]:
        matches = []
        for event in events:
            if event.settings.flickr is False:
                continue
            if self.require_flickr_marker and event.settings.flickr is not True:
                continue
            before = event.settings.buffer_before if event.settings.buffer_before is not None else self.buffer_before
            after = event.settings.buffer_after if event.settings.buffer_after is not None else self.buffer_after
            if event.start - before <= captured_at <= event.end + after:
                matches.append(event)
        return matches

    def select_event(self, captured_at: datetime, events: list[CalendarEvent]) -> tuple[CalendarEvent | None, str]:
        matches = self.matching_events(captured_at, events)
        if not matches:
            return None, "no matching event"
        selected = sorted(matches, key=lambda e: (e.settings.flickr is not True, e.settings.album is None, e.duration, e.created_at))[0]
        return selected, "selected by FLICKR marker, album override, shortest duration, earliest created"

    def build_plan(self, event: CalendarEvent | None) -> AlbumPlan:
        if event is None:
            return AlbumPlan(self.unassigned_album, self.default_privacy, self.global_tags, None)
        privacy = event.settings.privacy or self.default_privacy
        tags = tuple(dict.fromkeys((*self.global_tags, *event.settings.tags)))
        return AlbumPlan(event.album_name, privacy, tags, event, event.settings.description)
