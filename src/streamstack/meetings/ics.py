"""An ICS (iCalendar, RFC 5545) reader for the meetings a person has in a window of time. No dependencies.

Reads what calendar feeds really contain: folded lines, UTC, floating and TZID times (Windows zone names from Outlook
included), recurring meetings (RRULE with DAILY, WEEKLY, MONTHLY and YEARLY, INTERVAL, COUNT, UNTIL, BYDAY with
ordinals, BYMONTHDAY, BYMONTH), EXDATE, edited single occurrences (RECURRENCE-ID) and cancelled ones. All-day events
are left out: nobody joins a call at midnight. A broken event is skipped; one bad line never loses the calendar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from streamstack.meetings.model import Meeting, find_join_link

MAX_PERIODS = 60_000  # a recurrence is walked at most this many periods (a daily meeting for 160 years)
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WINDOWS_ZONES = {
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest",
    "Russian Standard Time": "Europe/Moscow",
    "Turkey Standard Time": "Europe/Istanbul",
    "Israel Standard Time": "Asia/Jerusalem",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "New Zealand Standard Time": "Pacific/Auckland",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabian Standard Time": "Asia/Dubai",
}


class IcsError(ValueError):
    """The text is not a calendar."""


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str


@dataclass
class Event:
    uid: str = ""
    title: str = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    duration: timedelta | None = None
    rrule: dict[str, str] = field(default_factory=dict)
    exdates: list[datetime | date] = field(default_factory=list)
    recurrence_id: datetime | date | None = None
    cancelled: bool = False
    texts: list[str] = field(default_factory=list)  # location, url, description: where a join link may be
    tz: tzinfo | None = None


def local_zone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_prop(line: str) -> Prop | None:
    """`NAME;P=V;Q="a:b":value` to a Prop. Colons inside quoted parameter values are not the separator."""
    in_quote = False
    split = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ":" and not in_quote:
            split = index
            break
    if split < 1:
        return None
    head, value = line[:split], line[split + 1 :]
    parts: list[str] = []
    current, in_quote = "", False
    for char in head:
        if char == '"':
            in_quote = not in_quote
        if char == ";" and not in_quote:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return Prop(parts[0].upper(), params, value)


def unescape(text: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def zone(name: str | None, fallback: tzinfo) -> tzinfo:
    if not name:
        return fallback
    clean = name.strip().strip('"')
    # "Europe/Berlin", "/mozilla.org/20050126_1/Europe/Berlin" and Outlook's "W. Europe Standard Time"
    candidates = [clean.lstrip("/"), "/".join(clean.split("/")[-2:]), WINDOWS_ZONES.get(clean, "")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return fallback


def parse_time(prop: Prop, fallback: tzinfo) -> tuple[datetime | date, tzinfo | None]:
    """The value of a DTSTART-like property: an aware datetime, or a date for an all-day value."""
    value = prop.value.strip()
    if prop.params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date(), None
    utc = value.endswith("Z")
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if utc:
        return naive.replace(tzinfo=UTC), UTC
    tz = zone(prop.params.get("TZID"), fallback)
    return naive.replace(tzinfo=tz), tz


def parse_duration(text: str) -> timedelta:
    found = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text.strip())
    if not found:
        raise IcsError(f"bad DURATION {text!r}")
    sign, weeks, days, hours, minutes, seconds = found.groups()
    span = timedelta(
        weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )  # fmt: skip
    return -span if sign == "-" else span


def parse_rrule(text: str) -> dict[str, str]:
    rule: dict[str, str] = {}
    for part in text.split(";"):
        key, _, value = part.partition("=")
        if key and value:
            rule[key.upper()] = value.upper()
    return rule


def parse_events(text: str, fallback: tzinfo) -> list[Event]:
    events: list[Event] = []
    current: Event | None = None
    depth = 0
    for line in unfold(text):
        prop = parse_prop(line)
        if prop is None:
            continue
        if prop.name == "BEGIN":
            if prop.value.upper() == "VEVENT" and depth == 0:
                current = Event()
            elif current is not None:
                depth += 1  # a VALARM inside the event
            continue
        if prop.name == "END":
            if prop.value.upper() == "VEVENT" and depth == 0 and current is not None:
                events.append(current)
                current = None
            elif depth:
                depth -= 1
            continue
        if current is None or depth:
            continue
        try:
            _apply(current, prop, fallback)
        except (ValueError, IcsError):
            current.start = None  # an event whose time cannot be read is not scheduled
            current.cancelled = True
    return events


def _apply(event: Event, prop: Prop, fallback: tzinfo) -> None:
    name = prop.name
    if name == "UID":
        event.uid = prop.value.strip()
    elif name == "SUMMARY":
        event.title = unescape(prop.value).strip()
    elif name == "DTSTART":
        event.start, event.tz = parse_time(prop, fallback)
    elif name == "DTEND":
        event.end, _ = parse_time(prop, fallback)
    elif name == "DURATION":
        event.duration = parse_duration(prop.value)
    elif name == "RRULE":
        event.rrule = parse_rrule(prop.value)
    elif name == "EXDATE":
        for item in prop.value.split(","):
            event.exdates.append(parse_time(Prop("EXDATE", prop.params, item), fallback)[0])
    elif name == "RECURRENCE-ID":
        event.recurrence_id, _ = parse_time(prop, fallback)
    elif name == "STATUS":
        event.cancelled = event.cancelled or prop.value.strip().upper() == "CANCELLED"
    elif name in ("LOCATION", "URL", "DESCRIPTION", "X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE"):
        event.texts.append(unescape(prop.value))
    elif name == "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK":
        event.texts.append(prop.value)


# -- recurrence ----------------------------------------------------------------------------------------------------


def _weekday_rules(text: str) -> list[tuple[int, int]]:
    """BYDAY "MO,2TU,-1FR" to [(ordinal or 0, weekday 0-6)]."""
    rules: list[tuple[int, int]] = []
    for item in text.split(","):
        found = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item.strip())
        if found:
            rules.append((int(found.group(1) or 0), WEEKDAYS.index(found.group(2))))
    return rules


def _ints(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if re.fullmatch(r"[+-]?\d+", x.strip())]


def _month_days(year: int, month: int) -> int:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - date(year, month, 1)).days


def _nth_weekdays(year: int, month: int, rules: list[tuple[int, int]]) -> list[int]:
    """The days of a month matching BYDAY rules (an ordinal picks the nth, negative counts from the end)."""
    days_in = _month_days(year, month)
    out: set[int] = set()
    for ordinal, weekday in rules:
        matches = [d for d in range(1, days_in + 1) if date(year, month, d).weekday() == weekday]
        if ordinal == 0:
            out.update(matches)
        elif 0 < ordinal <= len(matches):
            out.add(matches[ordinal - 1])
        elif ordinal < 0 and -ordinal <= len(matches):
            out.add(matches[ordinal])
    return sorted(out)


def _candidates(rule: dict[str, str], first: datetime):
    """Yield the wall-clock start of every occurrence in order (naive datetimes), beginning with `first`."""
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule["INTERVAL"])) if rule.get("INTERVAL", "").isdigit() else 1
    by_day = _weekday_rules(rule.get("BYDAY", ""))
    by_monthday = _ints(rule.get("BYMONTHDAY", ""))
    by_month = _ints(rule.get("BYMONTH", ""))
    clock = first.time()
    for index in range(MAX_PERIODS):
        if freq == "DAILY":
            day = first.date() + timedelta(days=index * interval)
            if by_day and day.weekday() not in {w for _, w in by_day}:
                continue
            if by_month and day.month not in by_month:
                continue
            yield datetime.combine(day, clock)
        elif freq == "WEEKLY":
            monday = first.date() - timedelta(days=first.weekday()) + timedelta(weeks=index * interval)
            weekdays = sorted({w for _, w in by_day}) or [first.weekday()]
            for weekday in weekdays:
                candidate = datetime.combine(monday + timedelta(days=weekday), clock)
                if candidate >= first:
                    yield candidate
        elif freq == "MONTHLY":
            total = first.year * 12 + first.month - 1 + index * interval
            year, month = divmod(total, 12)
            month += 1
            if by_day:
                days = _nth_weekdays(year, month, by_day)
            elif by_monthday:
                size = _month_days(year, month)
                days = sorted({d if d > 0 else size + 1 + d for d in by_monthday if 1 <= abs(d) <= size})
            else:
                days = [first.day] if first.day <= _month_days(year, month) else []
            for day in days:
                candidate = datetime.combine(date(year, month, day), clock)
                if candidate >= first:
                    yield candidate
        elif freq == "YEARLY":
            year = first.year + index * interval
            for month in by_month or [first.month]:
                if not 1 <= month <= 12:
                    continue
                if by_day:
                    days = _nth_weekdays(year, month, by_day)
                elif by_monthday:
                    size = _month_days(year, month)
                    days = sorted({d if d > 0 else size + 1 + d for d in by_monthday if 1 <= abs(d) <= size})
                else:
                    days = [first.day] if first.day <= _month_days(year, month) else []
                for day in days:
                    candidate = datetime.combine(date(year, month, day), clock)
                    if candidate >= first:
                        yield candidate
        else:
            return


def occurrences(event: Event, until: datetime) -> list[datetime]:
    """Start instants (aware) of an event up to `until`: the one start, or the expanded recurrence."""
    start = event.start
    if not isinstance(start, datetime):
        return []
    if not event.rrule:
        return [start]
    tz = event.tz or UTC
    first = start.astimezone(tz).replace(tzinfo=None)
    count_text = event.rrule.get("COUNT", "")
    limit = int(count_text) if count_text.isdigit() else None
    stop = None
    if "UNTIL" in event.rrule:
        try:
            stop, _ = parse_time(Prop("UNTIL", {}, event.rrule["UNTIL"]), tz)
        except ValueError:
            stop = None
        if isinstance(stop, date) and not isinstance(stop, datetime):
            stop = datetime.combine(stop, datetime.max.time(), tzinfo=tz)
    out: list[datetime] = []
    for seen, naive in enumerate(_candidates(event.rrule, first), 1):
        instant = naive.replace(tzinfo=tz)
        if stop is not None and instant > stop:
            break
        if limit is not None and seen > limit:
            break
        if instant > until:
            break
        out.append(instant)
    return out


def _same_instant(a: datetime | date, b: datetime | date) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.timestamp() == b.timestamp()
    return a == b


def _length(event: Event) -> timedelta:
    if event.duration is not None:
        return event.duration
    if isinstance(event.start, datetime) and isinstance(event.end, datetime):
        return max(event.end - event.start, timedelta(0))
    return timedelta(0)


def meetings_in(
    text: str, source: str, window_start: float, window_end: float, local: tzinfo | None = None
) -> list[Meeting]:
    """Every timed, not cancelled meeting overlapping [window_start, window_end] (Unix seconds), earliest first."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise IcsError("This is not a calendar (no VCALENDAR).")
    fallback = local or local_zone()
    events = parse_events(text, fallback)
    masters = [e for e in events if e.recurrence_id is None]
    overrides = {
        (e.uid, _key(e.recurrence_id)): e for e in events if e.recurrence_id is not None and e.start is not None
    }
    horizon = datetime.fromtimestamp(window_end, UTC)
    found: dict[str, Meeting] = {}
    used: set[tuple[str, float | date | None]] = set()
    for event in masters:
        if event.start is None or event.cancelled or not isinstance(event.start, datetime):
            continue
        length = _length(event)
        for instant in occurrences(event, horizon):
            if any(_same_instant(instant, x) for x in event.exdates):
                continue
            chosen = event
            begin, finish = instant, instant + length
            override = overrides.get((event.uid, _key(instant)))
            if override is not None:
                used.add((event.uid, _key(instant)))
                if override.cancelled or not isinstance(override.start, datetime):
                    continue
                chosen = override
                begin = override.start
                finish = begin + _length(override)
            _add(found, chosen, event, source, begin, finish, window_start, window_end)
    for (uid, key), override in overrides.items():
        if (uid, key) in used or override.cancelled or not isinstance(override.start, datetime):
            continue
        _add(
            found,
            override,
            override,
            source,
            override.start,
            override.start + _length(override),
            window_start,
            window_end,
        )
    return sorted(found.values(), key=lambda m: (m.start, m.id))


def _key(value: datetime | date | None) -> float | date | None:
    return value.timestamp() if isinstance(value, datetime) else value


def _add(
    found: dict[str, Meeting],
    shown: Event,
    master: Event,
    source: str,
    begin: datetime,
    finish: datetime,
    window_start: float,
    window_end: float,
) -> None:
    start, end = begin.timestamp(), finish.timestamp()
    if end < window_start or start > window_end:
        return
    ident = f"{source}:{master.uid or shown.title}:{int(start)}"
    join = find_join_link(*shown.texts, *master.texts)
    found[ident] = Meeting(ident, shown.title or "Meeting", start, max(end, start), join, source)
