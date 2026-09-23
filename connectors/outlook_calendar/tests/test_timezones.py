from connectors.outlook_calendar.server import WhenError, zone

"""Timezone resolution, which depends on the tz database being in the image."""
import pytest
REAL_ZONES = ['UTC', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Calcutta', 'Europe/London', 'America/New_York', 'Asia/Katmandu']

@pytest.mark.parametrize('name', REAL_ZONES)
def test_real_timezones_resolve(name: str) -> None:
    assert zone(name) is not None

def test_the_tz_database_is_present() -> None:
    """Fails with a pointed message if the image ships no tz data at all."""
    from zoneinfo import available_timezones
    zones = available_timezones()
    assert len(zones) > 100, f'only {len(zones)} timezones available. Add `tzdata` to requirements.txt.'

def test_an_unknown_zone_is_still_refused() -> None:
    """Never book against a guessed clock."""
    with pytest.raises(WhenError, match='Unknown timezone'):
        zone('Mars/Olympus_Mons')

def test_blank_falls_back_rather_than_raising() -> None:
    assert zone(None) is not None
    assert zone('   ') is not None
