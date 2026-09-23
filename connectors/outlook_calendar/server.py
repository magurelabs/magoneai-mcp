"""The outlook-calendar connector.

Ported from the v1 mixin form by a codemod.

7 tools, 2 of them proposed read-only. Every input schema
is carried across verbatim via `declare_schema` rather than derived from type
hints, so the wire contract is byte-identical to v1 and no live agent sees a
changed parameter schema.

Every `annotations=` below is a **proposal from the tool's name** and needs a
human to confirm it. `readOnlyHint` in particular is trusted by MCP_READ_ONLY,
so a wrong one is worse than a missing one.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.types import ToolAnnotations

from magone_mcp_core import ConnectorServer, declare_schema

from ._shared import GRAPH_API_URL, GraphNotFoundError, _get_access_token, _graph_request

# Restored after the port: an optional dependency whose try/except the port dropped.

try:  # stdlib on 3.9+, and the servers run 3.11
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]

SERVER_NAME = "outlook-calendar"
SERVER_VERSION = "2.0.0"

# Ported bodies reference `self.logger`, which was the v1 server's own logger.
logger = structlog.get_logger(f"mcp.{SERVER_NAME}")

server = ConnectorServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description="",
)


# -- carried across from the v1 modules ---------------------------------
# Imports, module-level helpers and the mixins' non-tool methods. A ported
# body refers to these, so moving the body without them leaves undefined
# names: 1,898 of them on the first pass, in code that parsed cleanly.

from .tools._when import WhenError
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from .tools._when import WhenError
import re
from typing import Optional
import sys
from pathlib import Path
from .tools._availability import DEFAULT_INTERVAL_MINUTES, DEFAULT_WINDOW_DAYS, MAX_PEOPLE, MAX_WINDOW_DAYS
from .tools._constants import CREDENTIAL_HEADERS
from .tools._when import WhenError
from .tools._constants import CREDENTIAL_HEADERS, DEFAULT_EVENT_MINUTES, MAX_RESULTS_CAP
from .tools._when import WhenError


MAX_PEOPLE = 20

MAX_WINDOW_DAYS = 14

DEFAULT_WINDOW_DAYS = 1

MIN_INTERVAL_MINUTES = 5

MAX_INTERVAL_MINUTES = 1440

DEFAULT_INTERVAL_MINUTES = 30

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

CREDENTIAL_HEADERS = [
    "X-Microsoft-Refresh-Token",
    "X-Microsoft-Client-Id",
    "X-Microsoft-Client-Secret",
    "X-Microsoft-Tenant-Id",
]

MAX_RESULTS_CAP = 100

DEFAULT_EVENT_MINUTES = 30

DEFAULT_TZ = "UTC"

_UNITS = {
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
}

_OFFSET_RE = re.compile(r"^([+-])\s*(\d+)\s*([a-z]+)$", re.IGNORECASE)

_IN_RE = re.compile(r"^in\s+(\d+)\s*([a-z]+)$", re.IGNORECASE)

def zone(tz: Optional[str]):
    """The tzinfo for an IANA name, falling back to UTC on anything unusable."""
    name = (tz or DEFAULT_TZ).strip() or DEFAULT_TZ
    if ZoneInfo is None:
        raise WhenError("zoneinfo unavailable; cannot resolve timezones")
    try:
        return ZoneInfo(name)
    except Exception:
        # An unknown zone must not book against a silently different clock.
        raise WhenError(
            f"Unknown timezone {name!r}. Pass an IANA name such as 'Asia/Dubai'."
        ) from None

def now_in(tz: Optional[str]) -> datetime:
    return datetime.now(zone(tz))

def _delta(amount: int, unit: str) -> timedelta:
    key = _UNITS.get(unit.lower())
    if not key:
        raise WhenError(f"Unknown time unit {unit!r}. Use minutes, hours, days or weeks.")
    return timedelta(**{key: amount})

def resolve(value: str, tz: Optional[str], *, base: Optional[datetime] = None) -> datetime:
    """One expression to an aware datetime in `tz`.

    `base` is the anchor for relative forms; it defaults to now in `tz` and is
    injectable so a caller resolving several fields anchors them all to the same
    instant. Without that, a start of "+30m" and an end of "+1h" computed a few
    milliseconds apart drift against each other.
    """
    if not isinstance(value, str) or not value.strip():
        raise WhenError("Empty datetime value")
    text = value.strip()
    tzinfo = zone(tz)
    anchor = base or datetime.now(tzinfo)

    if text.lower() == "now":
        return anchor

    match = _OFFSET_RE.match(text) or _IN_RE.match(text)
    if match:
        groups = match.groups()
        if len(groups) == 3:
            sign, amount, unit = groups
        else:
            sign, (amount, unit) = "+", groups
        delta = _delta(int(amount), unit)
        return anchor - delta if sign == "-" else anchor + delta

    iso = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise WhenError(
            f"Could not read {value!r} as a time. Use an ISO datetime "
            "(2026-08-21T09:00:00), 'now', an offset like '+30m', or 'in 2 hours'."
        ) from None
    # A naive literal means "wall clock in the target zone", which is what a
    # caller means by "09:00 tomorrow" and is NOT the same as 09:00 UTC.
    # An aware literal is converted INTO the target zone: callers strip tzinfo and
    # send the wall clock with that zone name, so returning it unconverted booked
    # 14:00+05:30 as 14:00 UTC.
    return parsed.replace(tzinfo=tzinfo) if parsed.tzinfo is None else parsed.astimezone(tzinfo)

def resolve_window(
    start: str,
    end: Optional[str],
    duration_minutes: Optional[int],
    tz: Optional[str],
    *,
    default_minutes: int = 30,
) -> tuple[datetime, datetime]:
    """Resolve a start/end pair, anchoring both to one instant.

    `end` and `duration_minutes` are alternatives; duration is the one a caller
    reaches for when it knows how long a thing runs but not when "now" is, which
    is the common case for a reminder. With neither, `default_minutes` applies
    so a caller is never forced to invent an end time.
    """
    tzinfo = zone(tz)
    anchor = datetime.now(tzinfo)
    start_dt = resolve(start, tz, base=anchor)

    if end and duration_minutes:
        raise WhenError("Pass either end_time or duration_minutes, not both")
    if duration_minutes is not None:
        if duration_minutes <= 0:
            raise WhenError("duration_minutes must be positive")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    elif end:
        # A relative end is measured from the START, not from now: "+1h" after a
        # start of "tomorrow 09:00" means 10:00, not an hour from this instant.
        end_dt = resolve(end, tz, base=start_dt)
    else:
        end_dt = start_dt + timedelta(minutes=default_minutes)

    if end_dt <= start_dt:
        raise WhenError("end_time must be after start_time")
    return start_dt, end_dt

_WHEN_DOC = (
    "ISO datetime (2026-08-21T09:00:00), or a relative expression resolved on the "
    "server: 'now', '+30m', '+2h', '+1d', or 'in 30 minutes'. Prefer the relative "
    "form for anything expressed relative to now - do NOT compute the clock time "
    "yourself. Read in `timezone`."
)

_TZ_DOC = (
    "IANA timezone for this call, e.g. 'Asia/Dubai'. Naive times are read in it, "
    "relative offsets are measured from now in it, and results come back in it. "
    "Pass the user's own zone; it defaults to UTC only when unknown."
)

def _event_view(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": e.get("id"),
        "title": e.get("subject", "(No title)"),
        "start": e.get("start", {}).get("dateTime"),
        "end": e.get("end", {}).get("dateTime"),
        "timezone": e.get("start", {}).get("timeZone"),
        "location": (e.get("location") or {}).get("displayName"),
        "attendees": [a.get("emailAddress", {}).get("address") for a in e.get("attendees", [])],
        "description": (e.get("bodyPreview") or "")[:200],
        "web_link": e.get("webLink"),
        "is_online_meeting": e.get("isOnlineMeeting"),
        # Graph has no top-level "onlineMeetingUrl" field - the join link
        # lives nested at onlineMeeting.joinUrl. Reading the (nonexistent)
        # flat name silently returned None for every event, even ones
        # genuinely created as a Teams meeting - confirmed live (Sep
        # 2026): create_event's own response (which reads the nested path
        # correctly, see below) returned a real teams.microsoft.com URL
        # for an event that list_events then reported as having none.
        "online_meeting_url": (e.get("onlineMeeting") or {}).get("joinUrl"),
    }



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=False, traced to the handler.
# Evidence: POST /me/calendar/events
@server.tool(
    name='create_event',
    description="Create a calendar event. `start_time` accepts a relative expression, so a reminder 30 minutes out is start_time='+30m' - never compute that time yourself. Give the length with `duration_minutes` OR `end_time`, not both; with neither it is 30 minutes. Leave `attendees` empty for a personal slot: an attendee turns it into an invitation sent to someone else's inbox.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'title': {'type': 'string', 'description': 'Event title'},
                    'start_time': {'type': 'string',
                                   'description': 'ISO datetime (2026-08-21T09:00:00), or '
                                                  'a relative expression resolved on the '
                                                  "server: 'now', '+30m', '+2h', '+1d', or "
                                                  "'in 30 minutes'. Prefer the relative "
                                                  'form for anything expressed relative to '
                                                  'now - do NOT compute the clock time '
                                                  'yourself. Read in `timezone`.'},
                    'end_time': {'type': 'string',
                                 'description': 'Optional end. ISO datetime '
                                                '(2026-08-21T09:00:00), or a relative '
                                                "expression resolved on the server: 'now', "
                                                "'+30m', '+2h', '+1d', or 'in 30 minutes'. "
                                                'Prefer the relative form for anything '
                                                'expressed relative to now - do NOT '
                                                'compute the clock time yourself. Read in '
                                                '`timezone`. A relative end is measured '
                                                'from the START.'},
                    'duration_minutes': {'type': 'integer',
                                         'description': 'How long it runs, as an '
                                                        'alternative to end_time'},
                    'description': {'type': 'string', 'description': 'Event body'},
                    'location': {'type': 'string', 'description': 'Event location'},
                    'attendees': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': 'Attendee emails. Omit for a personal '
                                                 'slot.'},
                    'is_online_meeting': {'type': 'boolean',
                                          'description': 'Create a Teams meeting',
                                          'default': False},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Naive times are read in it, "
                                                'relative offsets are measured from now in '
                                                'it, and results come back in it. Pass the '
                                                "user's own zone; it defaults to UTC only "
                                                'when unknown.'}},
     'required': ['title', 'start_time']})
async def create_event(credentials: Dict[str, str],
    title: str,
    start_time: str,
    end_time: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    description: str = "",
    location: str = "",
    attendees: Optional[List[str]] = None,
    is_online_meeting: bool = False,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an event, resolving whatever form the times arrived in."""
    try:
        start, end = resolve_window(
            start_time,
            end_time,
            duration_minutes,
            timezone,
            default_minutes=DEFAULT_EVENT_MINUTES,
        )
        tz_name = zone(timezone).key
        event: Dict[str, Any] = {
            "subject": title,
            "body": {
                "contentType": "HTML" if "<" in description else "Text",
                "content": description,
            },
            # Graph wants a naive local time plus the zone name; sending an
            # offset-aware string here makes it apply the offset twice.
            "start": {"dateTime": start.replace(tzinfo=None).isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end.replace(tzinfo=None).isoformat(), "timeZone": tz_name},
            "isOnlineMeeting": is_online_meeting,
        }
        if location:
            event["location"] = {"displayName": location}
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        if is_online_meeting:
            event["onlineMeetingProvider"] = "teamsForBusiness"

        created = await _graph_request(
            credentials, "POST", "/me/calendar/events", json_data=event
        )
        return {
            "success": True,
            "event_id": created.get("id"),
            "web_link": created.get("webLink"),
            "title": created.get("subject"),
            "description": (created.get("body") or {}).get("content"),
            "location": (created.get("location") or {}).get("displayName"),
            # Echo the resolved window so a caller reports what was booked
            # rather than what it intended.
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": tz_name,
            "online_meeting_url": (created.get("onlineMeeting") or {}).get("joinUrl")
            if is_online_meeting
            else None,
        }
    except WhenError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("create_event_error", error=str(e))
        return {"success": False, "error": f"Failed to create event: {str(e)}"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Evidence: DELETE /me/calendar/events/{event_id}
@server.tool(
    name='delete_event',
    description="Delete a calendar event by ID. Cancels it for any attendees.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'event_id': {'type': 'string', 'description': 'Event ID to delete'}},
     'required': ['event_id']})
async def delete_event(credentials: Dict[str, str], event_id: str) -> Dict[str, Any]:
    """Delete an event."""
    try:
        await _graph_request(credentials, "DELETE", f"/me/calendar/events/{event_id}")
        return {"success": True, "deleted_event_id": event_id}
    except Exception as e:
        logger.exception("delete_event_error", error=str(e))
        return {"success": False, "error": f"Failed to delete event: {str(e)}"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: POST /me/calendar/getSchedule. One of Graph's read-only action
# endpoints: it returns availability and changes no calendar.
@server.tool(
    name='get_free_busy',
    description="Free/busy for OTHER people's calendars, by email, over a window. Returns each person's busy blocks and an availability string, not their event details. Check `visible` per person first: someone outside the tenant or with a hidden calendar comes back not visible, with no blocks, which is NOT the same as free. Use list_events for the caller's own calendar. Up to 20 people and 14 days per call.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'emails': {'type': 'array',
                               'items': {'type': 'string'},
                               'description': 'Email addresses to check, at most 20'},
                    'start_time': {'type': 'string',
                                   'description': 'Window start. ISO datetime '
                                                  '(2026-08-21T09:00:00), or a relative '
                                                  'expression resolved on the server: '
                                                  "'now', '+30m', '+2h', '+1d', or 'in 30 "
                                                  "minutes'. Prefer the relative form for "
                                                  'anything expressed relative to now - do '
                                                  'NOT compute the clock time yourself. '
                                                  'Read in `timezone`. Defaults to now.'},
                    'end_time': {'type': 'string',
                                 'description': 'Window end. ISO datetime '
                                                '(2026-08-21T09:00:00), or a relative '
                                                "expression resolved on the server: 'now', "
                                                "'+30m', '+2h', '+1d', or 'in 30 minutes'. "
                                                'Prefer the relative form for anything '
                                                'expressed relative to now - do NOT '
                                                'compute the clock time yourself. Read in '
                                                '`timezone`. A relative end is measured '
                                                'from the START. Defaults to 1 day after '
                                                'the start.'},
                    'interval_minutes': {'type': 'integer',
                                         'description': 'Granularity of '
                                                        '`availability_view`, one '
                                                        'character per interval (default: '
                                                        '30)',
                                         'default': 30},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Naive times are read in it, "
                                                'relative offsets are measured from now in '
                                                'it, and results come back in it. Pass the '
                                                "user's own zone; it defaults to UTC only "
                                                'when unknown.'}},
     'required': ['emails']})
async def get_free_busy(credentials: Dict[str, str],
    emails: List[str],
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask Graph for each person's schedule inside one zone-aware window."""
    try:
        people = clean_emails(emails)
        tz_name = zone(timezone).key
        start = resolve(start_time or "now", timezone)
        end = resolve(end_time or f"+{DEFAULT_WINDOW_DAYS}d", timezone, base=start)
        check_window(start, end)
        interval = clamp_interval(interval_minutes)

        result = await _graph_request(
            credentials,
            "POST",
            "/me/calendar/getSchedule",
            json_data=build_payload(people, start, end, tz_name, interval),
            # Busy blocks come back rendered in the caller's zone, like events.
            prefer=f'outlook.timezone="{tz_name}"',
        )
        views = [person_view(entry) for entry in result.get("value", [])]
        return {
            "success": True,
            "timezone": tz_name,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "interval_minutes": interval,
            "people": views,
            "not_visible": [p["email"] for p in views if not p["visible"]],
        }
    except WhenError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("get_free_busy_error", error=str(e))
        return {"success": False, "error": f"Failed to get free/busy: {str(e)}"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=True, destructiveHint=False, traced to the handler.
# Evidence: GET /me/calendar/calendarView
@server.tool(
    name='list_events',
    description="List calendar events for a date range. Day boundaries are computed in `timezone`, and times are returned in it, so 'today' means the caller's today rather than UTC's.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'date': {'type': 'string',
                             'description': 'Start date YYYY-MM-DD (default: today in '
                                            '`timezone`)'},
                    'days': {'type': 'integer',
                             'description': 'Days to fetch (default: 1)',
                             'default': 1},
                    'max_results': {'type': 'integer',
                                    'description': 'Max events (default: 50)',
                                    'default': 50},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Naive times are read in it, "
                                                'relative offsets are measured from now in '
                                                'it, and results come back in it. Pass the '
                                                "user's own zone; it defaults to UTC only "
                                                'when unknown.'}}})
async def list_events(credentials: Dict[str, str],
    date: Optional[str] = None,
    days: int = 1,
    max_results: int = 50,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """List events between two zone-aware day boundaries."""
    try:
        days = max(1, days)
        max_results = min(max(max_results, 1), MAX_RESULTS_CAP)
        if date:
            start = resolve(f"{date}T00:00:00", timezone)
        else:
            start = now_in(timezone).replace(hour=0, minute=0, second=0, microsecond=0)
        end = resolve(f"+{days}d", timezone, base=start)

        result = await _graph_request(
            credentials,
            "GET",
            "/me/calendar/calendarView",
            params={
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$top": max_results,
                "$orderby": "start/dateTime",
                "$select": "id,subject,start,end,location,attendees,bodyPreview,webLink,isOnlineMeeting,onlineMeeting",
            },
            # Ask Graph to render times in the caller's zone rather than UTC,
            # so nothing downstream has to convert them back by hand.
            prefer=f'outlook.timezone="{zone(timezone).key}"',
        )
        events = result.get("value", [])
        return {
            "success": True,
            "count": len(events),
            "timezone": zone(timezone).key,
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "events": [_event_view(e) for e in events],
        }
    except WhenError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("list_events_error", error=str(e))
        return {"success": False, "error": f"Failed to list events: {str(e)}"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /me/calendar/events/{id}. The handler's own
# comment records that Graph REPLACES the attendee list rather than
# appending, and that attendees=[] removes everyone.
# Evidence: PATCH /me/calendar/events/{event_id}
@server.tool(
    name='update_event_details',
    description="Change an existing event's title, description, location, who is invited, or online-meeting status (`is_online_meeting`, e.g. to add or remove a Teams link). Needs the `event_id` from list_events. Send only the fields that change. `attendees` REPLACES the whole guest list, so to add someone send everyone who should be on the event, not just the new person - sending only the new person removes everybody else. This tool CANNOT move or resize an event - it has no time parameters at all, on purpose. To change when an event happens, call `update_event_time` instead, in a separate call. Renaming a meeting is never a reason to touch its time.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'event_id': {'type': 'string',
                                 'description': 'Event ID from list_events'},
                    'title': {'type': 'string', 'description': 'New title'},
                    'description': {'type': 'string', 'description': 'New body'},
                    'location': {'type': 'string', 'description': 'New location'},
                    'attendees': {'type': 'array',
                                  'items': {'type': 'string'},
                                  'description': 'The COMPLETE guest list after the '
                                                 'change, as email addresses. Replaces the '
                                                 'existing list; omit to leave it '
                                                 'untouched.'},
                    'is_online_meeting': {'type': 'boolean',
                                          'description': 'Turn this event into (true) or '
                                                         'out of (false) a Teams meeting'}},
     'required': ['event_id']})
async def update_event_details(credentials: Dict[str, str],
    event_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    is_online_meeting: Optional[bool] = None,
) -> Dict[str, Any]:
    """Patch an event's non-time fields. No start/end/duration/timezone params
    exist on this tool at all - a title-only rename has no channel through which
    it could ever also move the event, unlike the old combined update_event,
    where the model sending start_time+timezone="UTC" alongside an unrelated
    title change silently moved a 9pm IST meeting to 2:30am the next day."""
    try:
        event: Dict[str, Any] = {}
        if title is not None:
            event["subject"] = title
        if description is not None:
            event["body"] = {
                "contentType": "HTML" if "<" in description else "Text",
                "content": description,
            }
        if location is not None:
            event["location"] = {"displayName": location}
        if attendees is not None:
            # There was no `attendees` parameter here at all, so a caller
            # asking to invite somebody had the argument dropped as unknown:
            # the PATCH succeeded on the fields that did exist, returned a
            # real event id, and the person was never invited. Reported as
            # success, silently wrong - the worst shape a write can take.
            #
            # Graph REPLACES the list on PATCH rather than appending, which
            # is why the description insists on the complete roster. An
            # empty list is therefore "remove everyone" and is a legitimate,
            # if rare, thing to ask for; `None` means "leave it alone".
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        if is_online_meeting is not None:
            event["isOnlineMeeting"] = is_online_meeting
            if is_online_meeting:
                event["onlineMeetingProvider"] = "teamsForBusiness"

        if not event:
            return {"success": False, "error": "No fields provided to update"}

        updated = await _graph_request(
            credentials, "PATCH", f"/me/calendar/events/{event_id}", json_data=event
        )
        return {
            "success": True,
            "event_id": updated.get("id"),
            "title": updated.get("subject"),
            # Echo back what Graph actually stored, not what we asked for - a
            # caller (or a user disputing "it didn't change") needs a real
            # readback to quote, not blind trust in a 200 that carries no body.
            # Confirmed live (Sep 2026): a body-change dispute couldn't be
            # settled from this response before, because there was nothing here
            # to compare against the request.
            "description": (updated.get("body") or {}).get("content"),
            "location": (updated.get("location") or {}).get("displayName"),
            "web_link": updated.get("webLink"),
            "start": (updated.get("start") or {}).get("dateTime"),
            "end": (updated.get("end") or {}).get("dateTime"),
            "timezone": (updated.get("start") or {}).get("timeZone"),
            # Read back off the provider's own response, not echoed from the
            # request. The caller asked who is on this event; the only
            # answer worth giving is the one Graph now holds.
            "attendees": [
                (a.get("emailAddress") or {}).get("address")
                for a in (updated.get("attendees") or [])
            ],
            "online_meeting_url": (updated.get("onlineMeeting") or {}).get("joinUrl")
            if is_online_meeting
            else None,
        }
    except Exception as e:
        logger.exception("update_event_details_error", error=str(e))
        return {"success": False, "error": f"Failed to update event: {str(e)}"}



# credential_headers: ['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id']
# permission_group: 'default'  required_scopes: []
# REVIEWED: readOnlyHint=False, destructiveHint=True, traced to the handler.
# Destructive: PATCH /me/calendar/events/{id}: the prior start and
# end are gone.
# Evidence: PATCH /me/calendar/events/{event_id}
@server.tool(
    name='update_event_time',
    description="Move or resize an existing event: start time, end time, or duration. Needs the `event_id` from list_events. `start_time` accepts the same relative expressions as create_event. To shift an event, pass the new absolute start rather than an offset - a relative offset here is measured from NOW, not from the event's current time. This tool has no title/description/location/is_online_meeting parameters at all - for those, call `update_event_details` instead, in a separate call. Never call this tool just because a title, description or location changed.",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {'event_id': {'type': 'string',
                                 'description': 'Event ID from list_events'},
                    'start_time': {'type': 'string',
                                   'description': 'ISO datetime (2026-08-21T09:00:00), or '
                                                  'a relative expression resolved on the '
                                                  "server: 'now', '+30m', '+2h', '+1d', or "
                                                  "'in 30 minutes'. Prefer the relative "
                                                  'form for anything expressed relative to '
                                                  'now - do NOT compute the clock time '
                                                  'yourself. Read in `timezone`.'},
                    'end_time': {'type': 'string',
                                 'description': 'New end. ISO datetime '
                                                '(2026-08-21T09:00:00), or a relative '
                                                "expression resolved on the server: 'now', "
                                                "'+30m', '+2h', '+1d', or 'in 30 minutes'. "
                                                'Prefer the relative form for anything '
                                                'expressed relative to now - do NOT '
                                                'compute the clock time yourself. Read in '
                                                '`timezone`.'},
                    'duration_minutes': {'type': 'integer',
                                         'description': 'New length, as an alternative to '
                                                        'end_time (requires start_time)'},
                    'timezone': {'type': 'string',
                                 'description': 'IANA timezone for this call, e.g. '
                                                "'Asia/Dubai'. Naive times are read in it, "
                                                'relative offsets are measured from now in '
                                                'it, and results come back in it. Pass the '
                                                "user's own zone; it defaults to UTC only "
                                                'when unknown.'}},
     'required': ['event_id']})
async def update_event_time(credentials: Dict[str, str],
    event_id: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    """Patch only an event's start/end, resolving any supplied times. This tool
    exists so a title/description/location-only edit has no time parameters to
    misuse - see update_event_details."""
    try:
        tz_name = zone(timezone).key
        event: Dict[str, Any] = {}

        if start_time is not None:
            start, end = resolve_window(
                start_time,
                end_time,
                duration_minutes,
                timezone,
                default_minutes=DEFAULT_EVENT_MINUTES,
            )
            event["start"] = {
                "dateTime": start.replace(tzinfo=None).isoformat(),
                "timeZone": tz_name,
            }
            event["end"] = {
                "dateTime": end.replace(tzinfo=None).isoformat(),
                "timeZone": tz_name,
            }
        elif end_time is not None:
            end = resolve(end_time, timezone)
            event["end"] = {
                "dateTime": end.replace(tzinfo=None).isoformat(),
                "timeZone": tz_name,
            }
        elif duration_minutes is not None:
            # Length alone is ambiguous without a start: Graph has no "keep the
            # start, change the length" patch, and guessing the start is how an
            # event silently moves.
            return {
                "success": False,
                "error": "duration_minutes needs start_time; pass both, or pass end_time",
            }

        if not event:
            return {"success": False, "error": "No time fields provided to update"}

        updated = await _graph_request(
            credentials, "PATCH", f"/me/calendar/events/{event_id}", json_data=event
        )
        return {
            "success": True,
            "event_id": updated.get("id"),
            "title": updated.get("subject"),
            "start": (updated.get("start") or {}).get("dateTime"),
            "end": (updated.get("end") or {}).get("dateTime"),
            "timezone": (updated.get("start") or {}).get("timeZone"),
        }
    except WhenError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("update_event_time_error", error=str(e))
        return {"success": False, "error": f"Failed to update event time: {str(e)}"}


# -- compatibility: the tool name saved configurations still point at --------
#
# v1 split `update_event` into `update_event_details` and `update_event_time`.
# Measured before the split shipped: stored workflow and agent configurations
# still bound the exact quoted name `"update_event"`, and none referenced
# either replacement. A TOOL node bound
# to a tool that no longer exists hard-fails; an agent simply loses the
# capability and is never told, which is the worse of the two because nothing
# reports it.
#
# So the name survives one release as a dispatcher. It is deliberately NOT a
# new capability: it accepts the v1 schema exactly and forwards to whichever
# of the two replacements the caller's fields belong to.
#
# **Retire this once those 10 bindings are migrated**, and not before. The
# check is a query, not a memory: look for the quoted name in
# `use_cases.workflow_json` and `agents.config_json` on every cluster.
@server.tool(
    name='update_event',
    description=(
        "Deprecated alias for update_event_details and update_event_time; "
        "prefer those. Change an existing event: time, title, description, "
        "location or who is invited. Needs the `event_id` from list_events. "
        "Send only the fields that change. To shift an event, pass the new "
        "absolute start rather than an offset - a relative offset here is "
        "measured from NOW, not from the event's current time. `attendees` "
        "REPLACES the whole guest list, so to add someone send everyone who "
        "should be on the event, not just the new person."
    ),
    # Destructive: it can REPLACE the attendee list and move the event, and
    # both tools it dispatches to are marked destructive.
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
    ),
    # Without these the alias received no credentials and failed on every
    # call with "Missing X-Microsoft-Refresh-Token header".
    credential_headers=['X-Microsoft-Refresh-Token', 'X-Microsoft-Client-Id', 'X-Microsoft-Client-Secret', 'X-Microsoft-Tenant-Id'],
    permission_group='default',
)
@declare_schema({'type': 'object',
     'properties': {
        'event_id': {'type': 'string', 'description': 'Event ID from list_events'},
        'title': {'type': 'string', 'description': 'New title'},
        'start_time': {'type': 'string', 'description': 'New start time.'},
        'end_time': {'type': 'string', 'description': 'New end time.'},
        'duration_minutes': {'type': 'integer',
                             'description': 'New length, as an alternative to '
                                            'end_time (requires start_time)'},
        'description': {'type': 'string', 'description': 'New body'},
        'location': {'type': 'string', 'description': 'New location'},
        'attendees': {'type': 'array', 'items': {'type': 'string'},
                      'description': 'The COMPLETE guest list after the change, as '
                                     'email addresses. Replaces the existing list; '
                                     'omit to leave it untouched.'},
        'timezone': {'type': 'string', 'description': 'IANA timezone name.'}},
     'required': ['event_id']})
async def update_event(credentials: Dict[str, str],
    event_id: str,
    title: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    timezone: Optional[str] = None,
) -> Dict[str, Any]:
    time_fields = (start_time, end_time, duration_minutes)
    detail_fields = (title, description, location, attendees)

    results: List[Dict[str, Any]] = []
    if any(field is not None for field in time_fields):
        results.append(
            await update_event_time(
                credentials,
                event_id=event_id,
                start_time=start_time,
                end_time=end_time,
                duration_minutes=duration_minutes,
                timezone=timezone,
            )
        )
    if any(field is not None for field in detail_fields):
        results.append(
            await update_event_details(
                credentials,
                event_id=event_id,
                title=title,
                description=description,
                location=location,
                attendees=attendees,
            )
        )

    if not results:
        return {
            "success": False,
            "error": "Nothing to update: pass at least one field besides event_id.",
        }

    # A partial success is a failure worth naming. Updating the time and then
    # failing to update the guest list leaves the event half-changed, and
    # reporting only the last call's result would hide that.
    failed = [r for r in results if not r.get("success", False)]
    if failed:
        return {
            "success": False,
            "error": "; ".join(str(r.get("error", "unknown error")) for r in failed),
            "partial": len(failed) < len(results),
        }
    merged: Dict[str, Any] = {"success": True}
    for r in results:
        merged.update({k: v for k, v in r.items() if k != "success"})
    return merged
