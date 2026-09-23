"""Email dates are rendered in the caller's timezone, with a UTC fallback.

Graph returns `receivedDateTime` in UTC. Slicing that string hands the model a
time that is correct in UTC and wrong for the user, with nothing to indicate
which - the same class of defect as the calendar `update_event` finding, where an
aware datetime was relabelled instead of converted and booked a meeting 90
minutes late, silently.

v1 #167 shipped these helpers with no tests. They are covered here because every
failure mode is a plausible-looking wrong answer rather than an error.
"""

from connectors.outlook_email.server import _fmt_date, _tz_or_utc

UTC_NOON = "2026-09-22T12:00:00Z"


def test_a_utc_instant_is_converted_to_the_callers_zone():
    # Asia/Dubai is UTC+4 year round, so no DST ambiguity in the assertion.
    assert _fmt_date(UTC_NOON, "Asia/Dubai") == "2026-09-22 16:00"


def test_conversion_can_cross_the_date_boundary():
    assert _fmt_date("2026-09-22T22:00:00Z", "Asia/Dubai") == "2026-09-23 02:00"


def test_no_timezone_falls_back_to_utc():
    assert _fmt_date(UTC_NOON) == "2026-09-22 12:00"


def test_an_unrecognised_timezone_falls_back_to_utc_rather_than_raising():
    """A model can invent a zone name. That must not fail the call."""
    assert _fmt_date(UTC_NOON, "Mars/Olympus_Mons") == "2026-09-22 12:00"


def test_an_empty_or_whitespace_timezone_falls_back_to_utc():
    assert _fmt_date(UTC_NOON, "   ") == "2026-09-22 12:00"


def test_date_only_drops_the_time():
    assert _fmt_date(UTC_NOON, "Asia/Dubai", date_only=True) == "2026-09-22"


def test_a_naive_timestamp_is_treated_as_utc():
    """Graph sends Z, but a naive string must not be read as local time."""
    assert _fmt_date("2026-09-22T12:00:00", "Asia/Dubai") == "2026-09-22 16:00"


def test_an_offset_other_than_z_is_honoured():
    # 12:00+05:30 is 06:30 UTC, which is 10:30 in Dubai.
    assert _fmt_date("2026-09-22T12:00:00+05:30", "Asia/Dubai") == "2026-09-22 10:30"


def test_an_empty_value_is_an_empty_string():
    assert _fmt_date(None) == ""
    assert _fmt_date("") == ""


def test_an_unparseable_value_degrades_to_the_old_raw_slice():
    """Fails open to the previous behaviour rather than raising on a shape Graph
    has not returned before."""
    assert _fmt_date("not-a-date-at-all") == "not-a-date-at-al"
    assert _fmt_date("not-a-date-at-all", date_only=True) == "not-a-date"


def test_tz_or_utc_resolves_a_real_zone_and_rejects_nonsense():
    assert str(_tz_or_utc("Asia/Dubai")) == "Asia/Dubai"
    assert _tz_or_utc("nope/nope").tzname(None) == "UTC"
    assert _tz_or_utc(None).tzname(None) == "UTC"
