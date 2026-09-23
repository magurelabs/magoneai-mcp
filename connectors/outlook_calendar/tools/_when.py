"""Resolve a "when" expression to a concrete zoned datetime.

Every datetime field on every calendar tool goes through this, so a caller can
say WHEN IT MEANS rather than computing a wall-clock time itself.

The reason this exists is not convenience. A model asked for a reminder "in 30
minutes" has to read its own clock, add 30 minutes, convert into the target
zone, and format the result - and it gets that wrong often enough to matter.
Observed: asked at 23:34 local for a reminder in 30 minutes, it booked
23:34-23:39, treating the offset as a duration. The reply looked right and the
calendar entry was useless. Arithmetic is not a thing to prompt harder about;
it is a thing to move into code.

Accepted forms, for any datetime field:

    "2026-08-21T09:00:00"     naive ISO, read in `tz`
    "2026-08-21T09:00:00Z"    offset-aware ISO, honoured as given
    "2026-08-21 09:00"        space separator, same as naive ISO
    "now"
    "+30m" "+2h" "-1d" "+1w"  signed offset from now
    "in 30 minutes"           the same offset in words
    "in 2 hours" "in 3 days"

Units: m/min/minute(s), h/hr/hour(s), d/day(s), w/week(s).

Anything else raises WhenError, which the tools surface as a clean error rather
than guessing. A silently misread time books a real meeting at the wrong hour,
so refusing is always better than approximating.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

try:  # stdlib on 3.9+, and the servers run 3.11
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]

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

# "+30m", "-2h", "+1 day"
_OFFSET_RE = re.compile(r"^([+-])\s*(\d+)\s*([a-z]+)$", re.IGNORECASE)
# "in 30 minutes", "in 2 hours"
_IN_RE = re.compile(r"^in\s+(\d+)\s*([a-z]+)$", re.IGNORECASE)


class WhenError(ValueError):
    """The expression could not be resolved. Never guess past this."""


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
