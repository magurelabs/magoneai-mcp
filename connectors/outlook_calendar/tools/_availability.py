"""Pure helpers for the free/busy tool: request shape in, people out.

Kept free of the framework so the parsing is unit-testable, and because the
Graph response is the part most likely to surprise: a person who cannot be
seen comes back as an entry with an `error` rather than as an HTTP failure.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ._when import WhenError

# Graph accepts at most 20 schedules per getSchedule call.
MAX_PEOPLE = 20
# Graph rejects spans of 62 days or more; two weeks is what "next week or the
# one after" needs, and keeps the availability string short enough to read.
MAX_WINDOW_DAYS = 14
DEFAULT_WINDOW_DAYS = 1
# availabilityViewInterval bounds, per Graph.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 30

# One character per interval in availabilityView.
AVAILABILITY_CODES = {
    "0": "free",
    "1": "tentative",
    "2": "busy",
    "3": "oof",
    "4": "working_elsewhere",
}


def clean_emails(emails: Optional[List[str]]) -> List[str]:
    """Trim, lowercase, dedupe, and refuse an empty or oversized list."""
    seen: List[str] = []
    for raw in emails or []:
        address = str(raw or "").strip().lower()
        if "@" not in address:
            raise WhenError(f"{raw!r} is not an email address")
        if address not in seen:
            seen.append(address)
    if not seen:
        raise WhenError("Pass at least one email address in `emails`")
    if len(seen) > MAX_PEOPLE:
        raise WhenError(f"At most {MAX_PEOPLE} people per call; got {len(seen)}")
    return seen


def clamp_interval(minutes: Optional[int]) -> int:
    if minutes is None:
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(int(minutes), MAX_INTERVAL_MINUTES))


def check_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise WhenError("end_time must be after start_time")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise WhenError(f"The window must be {MAX_WINDOW_DAYS} days or less")


def build_payload(
    emails: List[str], start: datetime, end: datetime, tz_name: str, interval: int
) -> Dict[str, Any]:
    """The getSchedule body. Naive local times plus the zone name, as with events."""
    return {
        "schedules": emails,
        "startTime": {"dateTime": start.replace(tzinfo=None).isoformat(), "timeZone": tz_name},
        "endTime": {"dateTime": end.replace(tzinfo=None).isoformat(), "timeZone": tz_name},
        "availabilityViewInterval": interval,
    }


def _working_hours(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    return {
        "days": raw.get("daysOfWeek") or [],
        "start": raw.get("startTime"),
        "end": raw.get("endTime"),
        "timezone": (raw.get("timeZone") or {}).get("name"),
    }


def _busy_block(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "start": (item.get("start") or {}).get("dateTime"),
        "end": (item.get("end") or {}).get("dateTime"),
        "status": item.get("status"),
        # Present only when the caller may see the other person's details.
        "title": item.get("subject"),
    }


def person_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One getSchedule entry to a compact, honest view.

    `visible` is the field a caller must check first: an entry carrying an
    `error` (external address, hidden calendar, unknown user) has an empty view
    and no busy blocks, which read as "completely free" if taken at face value.
    """
    error = entry.get("error")
    view = entry.get("availabilityView") or ""
    busy = [_busy_block(item) for item in entry.get("scheduleItems") or []]
    return {
        "email": entry.get("scheduleId"),
        "visible": not error,
        "availability_view": view,
        "busy": busy,
        "busy_count": len(busy),
        "working_hours": _working_hours(entry.get("workingHours")),
        "error": (error or {}).get("message") if error else None,
    }
