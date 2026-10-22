"""Sunrise and sunset math (offline), time-of-day slots, the default location and the property settings."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest

from streamstack.director import daylight, settings

NYC = daylight.Location("New York", 40.7128, -74.006, "settings")


def minutes(moment: datetime | None) -> int:
    assert moment is not None
    return moment.hour * 60 + moment.minute


@pytest.mark.parametrize(
    ("loc", "offset", "day", "sunrise", "sunset"),
    [
        # Reference times from published almanac tables, within three minutes.
        (NYC, -4, date(2026, 6, 21), (5, 25), (20, 31)),
        (daylight.Location("London", 51.5074, -0.1278, "settings"), 0, date(2026, 12, 21), (8, 4), (15, 53)),
        (daylight.Location("Sydney", -33.8688, 151.2093, "settings"), 11, date(2026, 12, 21), (5, 41), (20, 5)),
    ],
)
def test_sunrise_and_sunset_match_the_almanac(loc, offset, day, sunrise, sunset) -> None:
    times = daylight.sun_times(day, loc, timezone(timedelta(hours=offset)))
    assert abs(minutes(times["sunrise"]) - (sunrise[0] * 60 + sunrise[1])) <= 3
    assert abs(minutes(times["sunset"]) - (sunset[0] * 60 + sunset[1])) <= 3
    assert times["civil_dawn"] < times["sunrise"] < times["solar_noon"] < times["sunset"] < times["civil_dusk"]


def test_polar_day_and_night_have_no_sunrise_and_do_not_crash() -> None:
    tromso = daylight.Location("Tromso", 69.65, 18.96, "settings")
    midnight_sun = daylight.sun_times(date(2026, 6, 21), tromso, timezone(timedelta(hours=2)))
    polar_night = daylight.sun_times(date(2026, 12, 21), tromso, timezone(timedelta(hours=1)))
    assert midnight_sun["sunrise"] is None and midnight_sun["sunset"] is None
    assert polar_night["sunrise"] is None
    state = daylight.sun_state(datetime(2026, 12, 21, 12, tzinfo=timezone(timedelta(hours=1))), tromso)
    assert state.slot in daylight.SLOTS


def test_slots_follow_the_sun_through_the_day() -> None:
    tz = timezone(timedelta(hours=-4))
    slots = {h: daylight.sun_state(datetime(2026, 10, 22, h, 0, tzinfo=tz), NYC).slot for h in (1, 7, 10, 15, 18, 22)}
    assert slots[1] == "night" and slots[22] == "night"
    assert slots[7] == "dawn"
    assert slots[10] == "morning" and slots[15] == "afternoon" and slots[18] == "evening"


def test_the_label_says_what_the_light_is_doing() -> None:
    tz = timezone(timedelta(hours=-4))
    dawn = daylight.sun_state(datetime(2026, 10, 22, 7, 30, tzinfo=tz), NYC)
    assert dawn.label == "Dawn, daylight rising" and dawn.trend == "rising"
    night = daylight.sun_state(datetime(2026, 10, 22, 2, 0, tzinfo=tz), NYC)
    assert night.label == "Night, artificial light only"
    assert daylight.sun_state(datetime(2026, 10, 22, 17, 30, tzinfo=tz), NYC).trend == "falling"
    public = dawn.public()
    assert public["slot_label"] == "Dawn" and set(public["times"]) >= {"sunrise", "sunset"}


def test_day_strip_covers_the_whole_day_in_order() -> None:
    tz = timezone(timedelta(hours=-4))
    strip = daylight.day_strip(datetime(2026, 10, 22, 12, tzinfo=tz), NYC)
    assert strip[0]["start"] == 0 and strip[-1]["end"] == 1440
    assert all(a["end"] == b["start"] for a, b in itertools.pairwise(strip))
    assert strip[0]["slot"] == "night" and "afternoon" in {s["slot"] for s in strip}


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        daylight.sun_state(datetime(2026, 1, 1, 12), NYC)


def test_default_location_comes_from_the_time_zone_or_the_utc_offset() -> None:
    assert daylight.default_location("America/New_York", timedelta(hours=-4)).name == "New York"
    unknown = daylight.default_location("Mars/Olympus", timedelta(hours=5.5))
    assert unknown.source == "utc-offset" and unknown.longitude == pytest.approx(82.5)
    assert daylight.default_location(None, timedelta(0)).source == "utc-offset"


def test_location_priority_is_settings_then_env_then_time_zone() -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=-4)))
    cfg = {"STREAMSTACK_PROPERTY": "Cabin", "STREAMSTACK_LATITUDE": "46.5", "STREAMSTACK_LONGITUDE": "-84.3"}
    prefs = {"property": {"name": "Studio", "latitude": 34.05, "longitude": -118.24}}
    assert daylight.resolve_location(prefs, cfg, now, "America/New_York").name == "Studio"
    assert daylight.resolve_location({}, cfg, now, "America/New_York").name == "Cabin"
    assert daylight.resolve_location({}, {}, now, "America/New_York").source == "timezone"
    broken = {"STREAMSTACK_LATITUDE": "ninety", "STREAMSTACK_LONGITUDE": "1"}
    assert daylight.resolve_location({}, broken, now, "America/New_York").source == "timezone"


@pytest.mark.parametrize("pair", [("91", "0"), ("0", "181"), ("x", "1"), (None, None), ("nan", "0"), ("inf", "0")])
def test_impossible_coordinates_are_rejected(pair) -> None:
    with pytest.raises(daylight.LocationError):
        daylight.check_coordinates(*pair)


def test_property_settings_round_trip_with_private_permissions() -> None:
    settings.set_property("  Home  ", "40.7", "-74.0")
    assert settings.load()["property"] == {"name": "Home", "latitude": 40.7, "longitude": -74.0}
    assert settings.settings_path().stat().st_mode & 0o077 == 0
    with pytest.raises(settings.SettingsError):
        settings.set_property("Nowhere", "100", "0")
    assert settings.load()["property"]["latitude"] == 40.7
    settings.clear_property()
    assert "property" not in settings.load()
    settings.set_persona("executive")
    assert settings.load()["persona"] == "executive"


def test_a_damaged_settings_file_reads_as_empty(tmp_path) -> None:
    settings.settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings.settings_path().write_text("{not json")
    assert settings.load() == {}
