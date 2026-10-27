"""Time of day for the AI director: where the sun is at this property, computed offline.

The sun position uses the NOAA low-precision solar equations (good to about a minute for sunrise and sunset at
latitudes below 65 degrees), so there is no network call and no geolocation service. The property location comes
from, in order: the Console's director settings, `STREAMSTACK_LATITUDE` / `STREAMSTACK_LONGITUDE`, and finally a
city for the machine's time zone (a built-in table, then the UTC offset as a longitude).

The day is cut into five slots by the sun's height and direction, which is what changes the light in a room:

    night      sun below -6 degrees (civil twilight over): artificial light only
    dawn       sun rising, below 8 degrees: dim, blue daylight mixing with room lights
    morning    sun rising, 8 degrees or higher, until solar noon
    afternoon  sun setting, 8 degrees or higher, after solar noon
    evening    sun setting, below 8 degrees, until it is 6 degrees under the horizon: golden hour and dusk

Near the poles the sun may never reach a threshold; the slots then follow whatever the sun does that day.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

SLOTS = ("night", "dawn", "morning", "afternoon", "evening")
SLOT_LABELS = {"night": "Night", "dawn": "Dawn", "morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}
HORIZON = -0.833  # sunrise and sunset: the sun's upper limb at the horizon, with refraction
CIVIL = -6.0
DAY_BRIGHT = 8.0

# What the light is usually like in each slot, for the prompt and the Auto page. These are generalisations the
# director checks against the picture, never rules.
SLOT_NOTES = {
    "night": "No daylight. Only artificial light: screens and lamps, often cool or mixed in colour, dim, with a "
    "risk of noise and a one-sided face. Warm practical lights flatter.",
    "dawn": "Daylight is rising but still dim and blue, mixing with room lights. Exposure and colour temperature "
    "drift quickly; do not chase them with big moves.",
    "morning": "Daylight is strong and rising, cool to neutral. Window light can be hard and from one side.",
    "afternoon": "The brightest, most neutral daylight. Windows can blow out behind the speaker; watch backlight "
    "and highlight clipping.",
    "evening": "Daylight is fading and turns warm (golden hour), then drops away as room lights take over. "
    "Exposure falls and colour goes orange.",
}
# Starting hints (picture look offsets) used only until the director has learned what you kept in a slot.
SLOT_PRIORS: dict[str, dict[str, float]] = {
    "night": {"gamma": 0.05},
    "dawn": {"gamma": 0.04},
    "morning": {},
    "afternoon": {"contrast": -0.03},
    "evening": {"warmth": -0.04},
}

# The same for the lights: (brightness fraction, kelvin) offsets from the persona's own. The dark slots raise the
# light and warm it (a dim room next to a lit face looks harsh), the bright afternoon needs less and can run cooler.
LIGHT_PRIORS: dict[str, tuple[float, float]] = {
    "night": (0.20, -400.0),
    "dawn": (0.15, -250.0),
    "morning": (0.0, 0.0),
    "afternoon": (-0.10, 200.0),
    "evening": (0.12, -350.0),
}

# City centre per IANA time zone: the default property location when none is configured.
ZONE_CITIES: dict[str, tuple[str, float, float]] = {
    "America/New_York": ("New York", 40.71, -74.01),
    "America/Detroit": ("Detroit", 42.33, -83.05),
    "America/Toronto": ("Toronto", 43.65, -79.38),
    "America/Chicago": ("Chicago", 41.88, -87.63),
    "America/Winnipeg": ("Winnipeg", 49.90, -97.14),
    "America/Denver": ("Denver", 39.74, -104.99),
    "America/Edmonton": ("Edmonton", 53.55, -113.49),
    "America/Phoenix": ("Phoenix", 33.45, -112.07),
    "America/Los_Angeles": ("Los Angeles", 34.05, -118.24),
    "America/Vancouver": ("Vancouver", 49.28, -123.12),
    "America/Anchorage": ("Anchorage", 61.22, -149.90),
    "America/Halifax": ("Halifax", 44.65, -63.58),
    "America/Mexico_City": ("Mexico City", 19.43, -99.13),
    "America/Bogota": ("Bogota", 4.71, -74.07),
    "America/Lima": ("Lima", -12.05, -77.04),
    "America/Sao_Paulo": ("Sao Paulo", -23.55, -46.63),
    "America/Argentina/Buenos_Aires": ("Buenos Aires", -34.60, -58.38),
    "America/Santiago": ("Santiago", -33.45, -70.67),
    "Pacific/Honolulu": ("Honolulu", 21.31, -157.86),
    "Atlantic/Reykjavik": ("Reykjavik", 64.15, -21.94),
    "Europe/London": ("London", 51.51, -0.13),
    "Europe/Dublin": ("Dublin", 53.35, -6.26),
    "Europe/Lisbon": ("Lisbon", 38.72, -9.14),
    "Europe/Paris": ("Paris", 48.86, 2.35),
    "Europe/Madrid": ("Madrid", 40.42, -3.70),
    "Europe/Berlin": ("Berlin", 52.52, 13.41),
    "Europe/Amsterdam": ("Amsterdam", 52.37, 4.90),
    "Europe/Brussels": ("Brussels", 50.85, 4.35),
    "Europe/Zurich": ("Zurich", 47.38, 8.54),
    "Europe/Rome": ("Rome", 41.90, 12.50),
    "Europe/Vienna": ("Vienna", 48.21, 16.37),
    "Europe/Prague": ("Prague", 50.08, 14.44),
    "Europe/Warsaw": ("Warsaw", 52.23, 21.01),
    "Europe/Stockholm": ("Stockholm", 59.33, 18.07),
    "Europe/Oslo": ("Oslo", 59.91, 10.75),
    "Europe/Copenhagen": ("Copenhagen", 55.68, 12.57),
    "Europe/Helsinki": ("Helsinki", 60.17, 24.94),
    "Europe/Athens": ("Athens", 37.98, 23.73),
    "Europe/Istanbul": ("Istanbul", 41.01, 28.98),
    "Europe/Kyiv": ("Kyiv", 50.45, 30.52),
    "Europe/Moscow": ("Moscow", 55.76, 37.62),
    "Africa/Cairo": ("Cairo", 30.04, 31.24),
    "Africa/Lagos": ("Lagos", 6.52, 3.38),
    "Africa/Nairobi": ("Nairobi", -1.29, 36.82),
    "Africa/Johannesburg": ("Johannesburg", -26.20, 28.05),
    "Asia/Jerusalem": ("Jerusalem", 31.77, 35.22),
    "Asia/Dubai": ("Dubai", 25.20, 55.27),
    "Asia/Tehran": ("Tehran", 35.69, 51.39),
    "Asia/Karachi": ("Karachi", 24.86, 67.01),
    "Asia/Kolkata": ("Mumbai", 19.08, 72.88),
    "Asia/Dhaka": ("Dhaka", 23.81, 90.41),
    "Asia/Bangkok": ("Bangkok", 13.76, 100.50),
    "Asia/Jakarta": ("Jakarta", -6.21, 106.85),
    "Asia/Singapore": ("Singapore", 1.35, 103.82),
    "Asia/Hong_Kong": ("Hong Kong", 22.32, 114.17),
    "Asia/Shanghai": ("Shanghai", 31.23, 121.47),
    "Asia/Taipei": ("Taipei", 25.03, 121.57),
    "Asia/Manila": ("Manila", 14.60, 120.98),
    "Asia/Seoul": ("Seoul", 37.57, 126.98),
    "Asia/Tokyo": ("Tokyo", 35.68, 139.69),
    "Australia/Perth": ("Perth", -31.95, 115.86),
    "Australia/Adelaide": ("Adelaide", -34.93, 138.60),
    "Australia/Sydney": ("Sydney", -33.87, 151.21),
    "Australia/Melbourne": ("Melbourne", -37.81, 144.96),
    "Australia/Brisbane": ("Brisbane", -27.47, 153.03),
    "Pacific/Auckland": ("Auckland", -36.85, 174.76),
    "UTC": ("Greenwich", 51.48, 0.0),
    "Etc/UTC": ("Greenwich", 51.48, 0.0),
}
DEFAULT_LATITUDE = 40.0  # when only the UTC offset is known


class LocationError(ValueError):
    """A latitude or longitude that is not a real place on Earth."""


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float
    source: str  # "settings", "env", "timezone" or "utc-offset"

    def public(self) -> dict:
        return {"name": self.name, "latitude": self.latitude, "longitude": self.longitude, "source": self.source}


def check_coordinates(latitude: str | float | None, longitude: str | float | None) -> tuple[float, float]:
    """Validated (latitude, longitude); raises LocationError for anything that is not a coordinate pair."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise LocationError("latitude and longitude must be numbers") from exc
    if not (math.isfinite(lat) and math.isfinite(lon)) or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise LocationError("latitude must be within -90..90 and longitude within -180..180")
    return round(lat, 4), round(lon, 4)


def local_zone_name() -> str | None:
    """The IANA name of this machine's time zone from TZ, /etc/timezone or the /etc/localtime link."""
    name = os.environ.get("TZ", "").lstrip(":").strip()
    if name:
        return name
    try:
        text = Path("/etc/timezone").read_text().strip()
        if text:
            return text
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else None


def default_location(zone: str | None, utc_offset: timedelta) -> Location:
    """A city for the time zone, or a point on the offset's meridian when the zone is not in the table."""
    if zone and zone in ZONE_CITIES:
        city, lat, lon = ZONE_CITIES[zone]
        return Location(city, lat, lon, "timezone")
    hours = utc_offset.total_seconds() / 3600
    return Location(f"UTC{hours:+g}", DEFAULT_LATITUDE, round(max(-180.0, min(180.0, hours * 15.0)), 1), "utc-offset")


def resolve_location(
    settings: dict | None, cfg: dict[str, str] | None = None, now: datetime | None = None, zone: str | None = None
) -> Location:
    """The property location: director settings, then the environment file, then the time zone's city."""
    prop = (settings or {}).get("property") or {}
    cfg = cfg or {}
    for source, name, lat, lon in (
        ("settings", prop.get("name"), prop.get("latitude"), prop.get("longitude")),
        ("env", cfg.get("STREAMSTACK_PROPERTY"), cfg.get("STREAMSTACK_LATITUDE"), cfg.get("STREAMSTACK_LONGITUDE")),
    ):
        if lat in (None, "") or lon in (None, ""):
            continue
        try:
            la, lo = check_coordinates(lat, lon)
        except LocationError:
            continue  # a typo in one source falls through to the next rather than stopping the director
        return Location(str(name or "").strip()[:60] or "This property", la, lo, source)
    now = now or datetime.now().astimezone()
    return default_location(zone if zone is not None else local_zone_name(), now.utcoffset() or timedelta(0))


# ---------------------------------------------------------------------------
# Sun position (NOAA)
# ---------------------------------------------------------------------------


def julian_day(when: datetime) -> float:
    utc = when.astimezone(UTC)
    seconds = utc.hour * 3600 + utc.minute * 60 + utc.second + utc.microsecond / 1e6
    year, month = utc.year, utc.month
    if month <= 2:
        year, month = year - 1, month + 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + utc.day + b - 1524.5 + seconds / 86400
    )


def solar_position(when: datetime, latitude: float, longitude: float) -> tuple[float, float, float]:
    """(elevation, azimuth, hour angle) in degrees for an aware datetime; the hour angle is negative before noon."""
    if when.tzinfo is None:
        raise ValueError("solar_position needs an aware datetime")
    t = (julian_day(when) - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * t)
    apparent = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    declination = math.asin(math.sin(eps) * math.sin(apparent))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(m)
        + 4 * e * y * math.sin(m) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * m)
    )
    utc = when.astimezone(UTC)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60 + utc.microsecond / 6e7
    solar_minutes = (minutes + eq_time + 4 * longitude) % 1440
    hour_angle = solar_minutes / 4 - 180
    lat = math.radians(latitude)
    ha = math.radians(hour_angle)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(ha)
    zenith = math.acos(max(-1.0, min(1.0, cos_zenith)))
    elevation = 90.0 - math.degrees(zenith)
    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-9:
        azimuth = 0.0
    else:
        cos_az = (math.sin(lat) * cos_zenith - math.sin(declination)) / (math.cos(lat) * sin_zenith)
        azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (azimuth + 180) % 360 if hour_angle > 0 else (540 - azimuth) % 360
    return elevation, azimuth, hour_angle


def slot_for(elevation: float, hour_angle: float) -> str:
    if elevation < CIVIL:
        return "night"
    if hour_angle < 0:
        return "dawn" if elevation < DAY_BRIGHT else "morning"
    return "afternoon" if elevation >= DAY_BRIGHT else "evening"


def daylight_fraction(elevation: float) -> float:
    """0 in the dark, 1 in full daylight: a smooth ramp from -6 to +20 degrees of sun height."""
    x = max(0.0, min(1.0, (elevation - CIVIL) / (20.0 - CIVIL)))
    return x * x * (3 - 2 * x)


def _crossing(
    start: datetime, end: datetime, latitude: float, longitude: float, level: float, rising: bool
) -> datetime | None:
    """First time in [start, end] the sun's elevation passes `level` going up (rising) or down."""
    step = timedelta(minutes=5)
    previous_time = start
    previous = solar_position(start, latitude, longitude)[0]
    moment = start
    while moment < end:
        moment = min(end, moment + step)
        current = solar_position(moment, latitude, longitude)[0]
        if (rising and previous < level <= current) or (not rising and previous >= level > current):
            low, high = previous_time, moment
            for _ in range(14):  # 5 min / 2^14 is under 20 ms
                mid = low + (high - low) / 2
                above = solar_position(mid, latitude, longitude)[0] >= level
                if above == rising:
                    high = mid
                else:
                    low = mid
            return high
        previous_time, previous = moment, current
    return None


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def sun_times(day: date, loc: Location, tz: tzinfo) -> dict[str, datetime | None]:
    """Civil dawn, sunrise, solar noon, sunset and civil dusk on a local calendar day (None when the sun does not)."""
    start, end = day_bounds(day, tz)
    lat, lon = loc.latitude, loc.longitude
    noon, best = None, -91.0
    moment = start
    while moment <= end:
        elevation = solar_position(moment, lat, lon)[0]
        if elevation > best:
            best, noon = elevation, moment
        moment += timedelta(minutes=10)
    if noon is not None:  # refine to the minute around the coarse maximum
        for offset in range(-9, 10):
            candidate = noon + timedelta(minutes=offset)
            elevation = solar_position(candidate, lat, lon)[0]
            if elevation > best:
                best, noon = elevation, candidate
    return {
        "civil_dawn": _crossing(start, end, lat, lon, CIVIL, True),
        "sunrise": _crossing(start, end, lat, lon, HORIZON, True),
        "solar_noon": noon,
        "sunset": _crossing(start, end, lat, lon, HORIZON, False),
        "civil_dusk": _crossing(start, end, lat, lon, CIVIL, False),
    }


@dataclass(frozen=True)
class SunState:
    slot: str
    label: str  # "Dawn, daylight rising"
    elevation: float
    azimuth: float
    daylight: float
    trend: str  # rising, falling or steady
    note: str
    times: dict[str, datetime | None]

    def public(self) -> dict:
        return {
            "slot": self.slot,
            "slot_label": SLOT_LABELS[self.slot],
            "label": self.label,
            "elevation": round(self.elevation, 1),
            "daylight": round(self.daylight, 2),
            "trend": self.trend,
            "note": self.note,
            "times": {k: v.strftime("%H:%M") if v else None for k, v in self.times.items()},
        }


def describe(slot: str, elevation: float, trend: str) -> str:
    """ "Dawn, daylight rising" and its siblings."""
    label = SLOT_LABELS[slot]
    if slot == "night":
        return f"{label}, artificial light only"
    if slot in ("morning", "afternoon") and elevation > 40:
        return f"{label}, strong daylight"
    if slot == "afternoon":
        return f"{label}, daylight easing"
    if slot == "evening":
        return f"{label}, daylight fading"
    return f"{label}, daylight rising" if trend != "falling" else f"{label}, daylight fading"


def sun_state(when: datetime, loc: Location) -> SunState:
    """Where the sun is now at the property, which slot that is, and today's sun times."""
    if when.tzinfo is None:
        raise ValueError("sun_state needs an aware datetime")
    elevation, azimuth, hour_angle = solar_position(when, loc.latitude, loc.longitude)
    later = solar_position(when + timedelta(minutes=20), loc.latitude, loc.longitude)[0]
    trend = "rising" if later - elevation > 0.4 else "falling" if later - elevation < -0.4 else "steady"
    slot = slot_for(elevation, hour_angle)
    return SunState(
        slot=slot,
        label=describe(slot, elevation, trend),
        elevation=elevation,
        azimuth=azimuth,
        daylight=daylight_fraction(elevation),
        trend=trend,
        note=SLOT_NOTES[slot],
        times=sun_times(when.date(), loc, when.tzinfo),
    )


def day_strip(when: datetime, loc: Location, step_minutes: int = 10) -> list[dict]:
    """The local day as consecutive slot segments (minutes from midnight), for the Console's time-of-day strip."""
    start, end = day_bounds(when.date(), when.tzinfo or UTC)
    segments: list[dict] = []
    moment = start
    while moment < end:
        elevation, _, hour_angle = solar_position(moment, loc.latitude, loc.longitude)
        slot = slot_for(elevation, hour_angle)
        minute = int((moment - start).total_seconds() // 60)
        if segments and segments[-1]["slot"] == slot:
            segments[-1]["end"] = minute + step_minutes
        else:
            segments.append({"slot": slot, "start": minute, "end": minute + step_minutes})
        moment += timedelta(minutes=step_minutes)
    if segments:
        segments[-1]["end"] = min(segments[-1]["end"], 1440)
    return segments
