"""The ICS reader: times, time zones, recurrence, edited and cancelled occurrences, join links."""

# ruff: noqa: E501  (calendar fixtures are single lines of ICS)

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from streamstack.meetings import ics
from streamstack.meetings.model import find_join_link

BERLIN = ZoneInfo("Europe/Berlin")
UTC = UTC


def cal(*events: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + "".join(f"BEGIN:VEVENT\r\n{e}\r\nEND:VEVENT\r\n" for e in events)
        + "END:VCALENDAR\r\n"
    )


def ts(*args: int, tz=UTC) -> float:
    return datetime(*args, tzinfo=tz).timestamp()


def run(text: str, start: float, end: float, local=UTC) -> list[ics.Meeting]:
    return ics.meetings_in(text, "ics:t", start, end, local)


def test_a_utc_meeting_with_a_duration_and_a_join_link() -> None:
    text = cal(
        "UID:a1\nDTSTART:20261028T140000Z\nDURATION:PT45M\nSUMMARY:Planning\\, weekly\n"
        "DESCRIPTION:Join https://teams.microsoft.com/l/meetup-join/abc123 now"
    )
    [m] = run(text, ts(2026, 10, 28), ts(2026, 10, 29))
    assert (m.title, m.start, m.end) == ("Planning, weekly", ts(2026, 10, 28, 14), ts(2026, 10, 28, 14, 45))
    assert m.join == "https://teams.microsoft.com/l/meetup-join/abc123" and m.online


def test_tzid_times_follow_the_zone_including_daylight_saving() -> None:
    text = cal("UID:b\nDTSTART;TZID=Europe/Berlin:20261028T100000\nDTEND;TZID=Europe/Berlin:20261028T110000\nSUMMARY:x")
    [m] = run(text, ts(2026, 10, 28), ts(2026, 10, 29))
    assert m.start == ts(2026, 10, 28, 10, tz=BERLIN)  # CET, after the October change


def test_windows_zone_names_from_outlook_are_understood() -> None:
    text = cal(
        "UID:w\nDTSTART;TZID=W. Europe Standard Time:20261104T090000\nDTEND;TZID=W. Europe Standard Time:20261104T100000\nSUMMARY:x"
    )
    [m] = run(text, ts(2026, 11, 4), ts(2026, 11, 5))
    assert m.start == ts(2026, 11, 4, 9, tz=BERLIN)


def test_an_unknown_zone_and_floating_times_use_the_local_zone() -> None:
    text = cal(
        "UID:f\nDTSTART:20261104T090000\nDTEND:20261104T100000\nSUMMARY:floating",
        "UID:g\nDTSTART;TZID=Mars/Base:20261104T120000\nDTEND;TZID=Mars/Base:20261104T130000\nSUMMARY:odd",
    )
    found = run(text, ts(2026, 11, 4), ts(2026, 11, 5), local=BERLIN)
    assert [m.start for m in found] == [ts(2026, 11, 4, 9, tz=BERLIN), ts(2026, 11, 4, 12, tz=BERLIN)]


def test_all_day_cancelled_and_unreadable_events_are_left_out() -> None:
    text = cal(
        "UID:d\nDTSTART;VALUE=DATE:20261104\nDTEND;VALUE=DATE:20261105\nSUMMARY:holiday",
        "UID:c\nDTSTART:20261104T090000Z\nDTEND:20261104T100000Z\nSTATUS:CANCELLED\nSUMMARY:gone",
        "UID:e\nDTSTART:not-a-time\nSUMMARY:broken",
        "UID:ok\nDTSTART:20261104T110000Z\nDTEND:20261104T120000Z\nSUMMARY:kept",
    )
    assert [m.title for m in run(text, ts(2026, 11, 4), ts(2026, 11, 5))] == ["kept"]


def test_folded_lines_and_quoted_parameters() -> None:
    text = cal(
        'UID:z\nDTSTART:20261104T090000Z\nDTEND:20261104T100000Z\nSUMMARY:A very long\r\n  title\nATTENDEE;CN="Doe: John":mailto:j@example.com'
    )
    assert run(text, ts(2026, 11, 4), ts(2026, 11, 5))[0].title == "A very long title"


def test_a_daily_recurrence_with_count_and_exdate() -> None:
    text = cal(
        "UID:r\nDTSTART:20261102T090000Z\nDTEND:20261102T093000Z\nRRULE:FREQ=DAILY;COUNT=4\nEXDATE:20261103T090000Z\nSUMMARY:standup"
    )
    days = [datetime.fromtimestamp(m.start, UTC).day for m in run(text, ts(2026, 11, 1), ts(2026, 12, 1))]
    assert days == [2, 4, 5]


def test_a_weekly_recurrence_on_chosen_days_keeps_local_time_across_dst() -> None:
    text = cal(
        "UID:w\nDTSTART;TZID=Europe/Berlin:20261023T100000\nDTEND;TZID=Europe/Berlin:20261023T103000\nRRULE:FREQ=WEEKLY;BYDAY=MO,FR;UNTIL=20261106T235959Z\nSUMMARY:sync"
    )
    found = run(text, ts(2026, 10, 20), ts(2026, 12, 1))
    assert [m.start for m in found] == [
        ts(2026, 10, 23, 10, tz=BERLIN), ts(2026, 10, 26, 10, tz=BERLIN),
        ts(2026, 10, 30, 10, tz=BERLIN), ts(2026, 11, 2, 10, tz=BERLIN), ts(2026, 11, 6, 10, tz=BERLIN),
    ]  # fmt: skip
    # 10:00 in Berlin is 08:00 UTC before the clocks change on the 25th and 09:00 UTC after it
    assert (found[0].start, found[1].start) == (ts(2026, 10, 23, 8), ts(2026, 10, 26, 9))


def test_an_every_other_week_interval() -> None:
    text = cal(
        "UID:i\nDTSTART:20261102T090000Z\nDTEND:20261102T100000Z\nRRULE:FREQ=WEEKLY;INTERVAL=2\nSUMMARY:biweekly"
    )
    days = [datetime.fromtimestamp(m.start, UTC).day for m in run(text, ts(2026, 11, 1), ts(2026, 12, 1))]
    assert days == [2, 16, 30]


def test_monthly_by_ordinal_weekday_and_by_month_day() -> None:
    second_tue = cal(
        "UID:m\nDTSTART:20261110T150000Z\nDTEND:20261110T160000Z\nRRULE:FREQ=MONTHLY;BYDAY=2TU\nSUMMARY:board"
    )
    assert [
        datetime.fromtimestamp(m.start, UTC).strftime("%m-%d") for m in run(second_tue, ts(2026, 11, 1), ts(2027, 2, 1))
    ] == ["11-10", "12-08", "01-12"]
    last_fri = cal(
        "UID:l\nDTSTART:20261030T150000Z\nDTEND:20261030T160000Z\nRRULE:FREQ=MONTHLY;BYDAY=-1FR\nSUMMARY:review"
    )
    assert [
        datetime.fromtimestamp(m.start, UTC).strftime("%m-%d") for m in run(last_fri, ts(2026, 10, 1), ts(2027, 1, 1))
    ] == ["10-30", "11-27", "12-25"]
    fifteenth = cal(
        "UID:d\nDTSTART:20261031T150000Z\nDTEND:20261031T160000Z\nRRULE:FREQ=MONTHLY;BYMONTHDAY=31\nSUMMARY:eom"
    )
    assert [
        datetime.fromtimestamp(m.start, UTC).strftime("%m-%d") for m in run(fifteenth, ts(2026, 10, 1), ts(2027, 1, 20))
    ] == ["10-31", "12-31"]  # no 31st in November


def test_a_yearly_recurrence() -> None:
    text = cal("UID:y\nDTSTART:20261224T120000Z\nDTEND:20261224T130000Z\nRRULE:FREQ=YEARLY\nSUMMARY:party")
    assert len(run(text, ts(2026, 1, 1), ts(2029, 1, 1))) == 3


def test_an_edited_occurrence_replaces_its_original_and_a_cancelled_one_disappears() -> None:
    text = cal(
        "UID:s\nDTSTART:20261102T090000Z\nDTEND:20261102T100000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:series",
        "UID:s\nRECURRENCE-ID:20261103T090000Z\nDTSTART:20261103T130000Z\nDTEND:20261103T140000Z\nSUMMARY:series (moved)",
        "UID:s\nRECURRENCE-ID:20261104T090000Z\nDTSTART:20261104T090000Z\nDTEND:20261104T100000Z\nSTATUS:CANCELLED\nSUMMARY:series",
    )
    found = run(text, ts(2026, 11, 1), ts(2026, 11, 10))
    assert [(m.title, datetime.fromtimestamp(m.start, UTC).hour) for m in found] == [
        ("series", 9),
        ("series (moved)", 13),
    ]


def test_the_window_clips_long_recurrences_and_keeps_a_meeting_in_progress() -> None:
    text = cal("UID:long\nDTSTART:20200101T090000Z\nDTEND:20200101T100000Z\nRRULE:FREQ=DAILY\nSUMMARY:old series")
    found = run(text, ts(2026, 10, 27, 9, 30), ts(2026, 10, 28, 8))
    assert [datetime.fromtimestamp(m.start, UTC).strftime("%d %H") for m in found] == ["27 09"]  # in progress at 09:30


def test_text_that_is_not_a_calendar_is_refused() -> None:
    with pytest.raises(ics.IcsError):
        run("<html>login</html>", 0, 1)


@pytest.mark.parametrize(
    "text, link",
    [
        ("Zoom: https://us02web.zoom.us/j/123456?pwd=abc.", "https://us02web.zoom.us/j/123456?pwd=abc"),
        ("meet https://meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij"),
        ("see https://example.com/page", ""),
    ],
)
def test_join_links(text: str, link: str) -> None:
    assert find_join_link(text) == link
