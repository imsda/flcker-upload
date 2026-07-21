"""Worker heartbeat recording and health evaluation."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .settings_store import SettingsStore


def record_worker_heartbeat(store: SettingsStore, at: datetime | None = None) -> str:
    timestamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
    store.set("WORKER_HEARTBEAT", timestamp)
    return timestamp


def worker_health(
    heartbeat: str,
    poll_interval_seconds: int,
    at: datetime | None = None,
    display_timezone: ZoneInfo = ZoneInfo("UTC"),
) -> dict[str, str]:
    if not heartbeat:
        return {
            "status": "Not started",
            "variant": "warning",
            "detail": "No worker heartbeat has been recorded yet.",
        }
    try:
        recorded = datetime.fromisoformat(heartbeat)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        recorded = recorded.astimezone(UTC)
    except ValueError:
        return {
            "status": "Unknown",
            "variant": "warning",
            "detail": "The recorded worker heartbeat is invalid.",
        }

    current = (at or datetime.now(UTC)).astimezone(UTC)
    age_seconds = max(0, int((current - recorded).total_seconds()))
    stale_after = max(300, poll_interval_seconds * 2 + 30)
    displayed = recorded.astimezone(display_timezone).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    if age_seconds <= stale_after:
        return {
            "status": "Running",
            "variant": "success",
            "detail": f"Last heartbeat {displayed}",
        }
    return {
        "status": "Stale",
        "variant": "danger",
        "detail": f"Last heartbeat {displayed}",
    }
