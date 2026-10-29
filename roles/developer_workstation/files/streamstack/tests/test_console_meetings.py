"""The meetings controller: sources, the next meeting, preparing the rig once, and keeping secrets out of files."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from streamstack.console import meetings as console_meetings
from streamstack.console.errors import ActionError
from streamstack.console.meetings import DemoMeetings, MeetingsController
from streamstack.console.rig import Rig
from streamstack.meetings import config
from streamstack.meetings.tokens import MemorySecrets

LINK = "https://calendar.example.com/feed/abc123secret.ics"
MIN = 60.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rig(clock: Clock) -> Iterator[Rig]:
    r = Rig.demo()
    # The demo studio applies start and stop after a timer (1 s). Preparation runs on the test's fake clock, so a
    # real-time timer landing mid-test (a starved runner) flips the studio under it: synchronous here.
    r.studio.delay = 0.0
    r.meetings = DemoMeetings(clock)
    r.meetings.rig = r
    r.meetings.notify = r.notify
    r.start()
    yield r
    r.stop()


def test_the_demo_studio_in_these_tests_applies_start_and_stop_at_once(rig: Rig) -> None:
    """The gate for the race: with a timer, `studio.stop` is still pending when preparation looks, which reads as
    "Already running" and the stop then lands after the prepare, so `studio.running` goes False after "done"."""
    rig.act("studio.stop", {})
    assert rig.snapshot()["studio"]["running"] is False
    rig.act("studio.start", {})
    assert rig.snapshot()["studio"]["running"] is True


def demo(rig: Rig) -> DemoMeetings:
    assert isinstance(rig.meetings, DemoMeetings)
    return rig.meetings


def state(rig: Rig) -> dict:
    return rig.snapshot()["meetings"]


def feed(rig: Rig, minutes: float, title: str = "Weekly sync", online: bool = True) -> None:
    rig.act("meetings.demo.feed", {"link": LINK, "minutes": minutes, "title": title, "online": online})


def add_calendar(rig: Rig, name: str = "Work") -> str:
    rig.act("meetings.ics.add", {"link": LINK, "name": name})
    return state(rig)["sources"][-1]["id"]


def tick(rig: Rig, clock: Clock, minutes: float = 0.0) -> None:
    clock.now += minutes * MIN
    rig.act("meetings.demo.tick", {})


def test_a_fresh_rig_has_meetings_signed_out_and_no_calendars(rig: Rig) -> None:
    s = state(rig)
    assert s["available"] and s["enabled"] and s["lead_minutes"] == 2 and s["next"] is None
    assert [(x["id"], x["status"]) for x in s["sources"]] == [("m365", "signed-out")]
    assert s["keyring"] == {"ok": True, "reason": None}
    assert [x["id"] for x in s["steps"] if x["on"]] == ["studio", "camera", "health"]


def test_a_calendar_link_is_added_and_its_next_meeting_shown(rig: Rig, clock: Clock) -> None:
    feed(rig, 30)
    ident = add_calendar(rig)
    s = state(rig)
    assert [(x["id"], x["status"], x["count"]) for x in s["sources"][1:]] == [(ident, "connected", 1)]
    assert s["next"]["title"] == "Weekly sync" and s["next"]["online"] and s["next"]["prep"] == "waiting"
    assert s["next"]["start"] == clock.now + 30 * MIN


def test_the_calendar_link_is_kept_in_the_keyring_and_never_in_the_settings(rig: Rig) -> None:
    feed(rig, 30)
    ident = add_calendar(rig)
    assert demo(rig).secrets.get(f"ics:{ident}") == LINK
    saved = json.dumps(demo(rig).store.load())
    assert "abc123secret" not in saved and "calendar.example.com" in saved  # the host is shown, the secret is not
    assert "abc123secret" not in json.dumps(state(rig))


def test_the_rig_is_prepared_once_when_the_meeting_is_two_minutes_away(rig: Rig, clock: Clock) -> None:
    rig.act("studio.stop", {})
    rig.act("video.privacy", {"on": True})
    feed(rig, 10)
    add_calendar(rig)
    tick(rig, clock, 7)  # 3 minutes before: too early
    assert state(rig)["prep"]["state"] == "idle" and rig.snapshot()["video"]["privacy"] is True
    tick(rig, clock, 1.5)  # 1.5 minutes before
    prep = state(rig)["prep"]
    assert prep["state"] == "done" and prep["meeting"] == "Weekly sync"
    assert [(x["id"], x["ok"]) for x in prep["steps"]] == [("studio", True), ("camera", True), ("health", True)]
    snap = rig.snapshot()
    assert snap["studio"]["running"] and snap["video"]["privacy"] is False
    assert state(rig)["next"]["prep"] == "done"
    rig.act("video.privacy", {"on": True})
    tick(rig, clock, 0.2)
    assert rig.snapshot()["video"]["privacy"] is True  # once per meeting: the person's own change stands


def test_a_meeting_that_has_begun_is_not_prepared_for_late(rig: Rig, clock: Clock) -> None:
    rig.act("video.privacy", {"on": True})
    feed(rig, 5)
    add_calendar(rig)
    tick(rig, clock, 6)
    assert state(rig)["prep"]["state"] == "idle" and rig.snapshot()["video"]["privacy"] is True


def call_on(rig: Rig) -> None:
    rig.act("call.demo", {"app": "Teams"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if rig.call.active():
            return
        time.sleep(0.02)
    raise AssertionError("the demo call did not become active")


def test_a_call_already_running_is_left_alone(rig: Rig, clock: Clock) -> None:
    rig.act("video.privacy", {"on": True})
    call_on(rig)
    feed(rig, 5)
    add_calendar(rig)
    tick(rig, clock, 4)
    prep = state(rig)["prep"]
    assert prep["state"] == "skipped" and "already running" in prep["text"]
    assert rig.snapshot()["video"]["privacy"] is True and state(rig)["in_call"] is True


def test_prepare_now_runs_even_in_a_call_because_a_person_asked(rig: Rig) -> None:
    rig.act("video.privacy", {"on": True})
    call_on(rig)
    message = rig.act("meetings.prepare", {})
    assert message and "Ready" in message and rig.snapshot()["video"]["privacy"] is False


def test_only_meetings_with_a_join_link_unless_asked_for_all(rig: Rig, clock: Clock) -> None:
    feed(rig, 30, "Lunch", online=False)
    add_calendar(rig)
    assert state(rig)["next"] is None
    rig.act("meetings.settings", {"only_online": False})
    assert state(rig)["next"]["title"] == "Lunch"


def test_switching_it_off_prepares_nothing(rig: Rig, clock: Clock) -> None:
    rig.act("video.privacy", {"on": True})
    rig.act("meetings.enable", {"on": False})
    feed(rig, 5)
    add_calendar(rig)
    tick(rig, clock, 4)
    assert state(rig)["prep"]["state"] == "idle" and rig.snapshot()["video"]["privacy"] is True


def test_a_failing_step_is_reported_and_the_others_still_run(
    rig: Rig, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.act("meetings.settings", {"steps": ["camera", "mic"]})
    feed(rig, 5)
    add_calendar(rig)

    real = rig.act

    def broken(action: str, params: dict | None = None) -> str | None:
        if action == "video.privacy":
            raise ActionError("The camera is busy.", 409)
        return real(action, params)

    monkeypatch.setattr(rig, "act", broken)
    tick(rig, clock, 4)
    monkeypatch.undo()
    prep = state(rig)["prep"]
    assert prep["state"] == "failed"
    assert [(x["id"], x["ok"], x["text"]) for x in prep["steps"]] == [
        ("camera", False, "The camera is busy."),
        ("mic", True, prep["steps"][1]["text"]),
    ]


def test_microsoft_365_signs_in_with_a_code_then_lists_meetings_and_signs_out(rig: Rig, clock: Clock) -> None:
    rig.act("meetings.demo.cloud", {"minutes": 20, "title": "Board call"})
    message = rig.act("meetings.m365.connect", {})
    assert message and "DEMO-1234" in message
    s = state(rig)["sources"][0]
    assert s["status"] == "pending" and s["code"]["user_code"] == "DEMO-1234"
    tick(rig, clock, 2)
    assert state(rig)["sources"][0]["status"] == "pending"  # not approved yet
    rig.act("meetings.demo.approve", {})
    tick(rig, clock, 2)
    s = state(rig)["sources"][0]
    assert (s["status"], s["account"], s["count"], s["code"]) == ("connected", "demo@example.com", 1, None)
    assert state(rig)["next"]["title"] == "Board call"
    rig.act("meetings.m365.disconnect", {})
    assert state(rig)["sources"][0]["status"] == "signed-out" and state(rig)["next"] is None


def test_a_sign_in_can_be_cancelled(rig: Rig) -> None:
    rig.act("meetings.m365.connect", {})
    rig.act("meetings.m365.cancel", {})
    assert state(rig)["sources"][0]["status"] == "signed-out"


def test_the_same_meeting_in_two_calendars_is_one_meeting(rig: Rig, clock: Clock) -> None:
    rig.act("meetings.demo.cloud", {"minutes": 30, "title": "Weekly sync"})
    rig.act("meetings.m365.connect", {})
    rig.act("meetings.demo.approve", {})
    tick(rig, clock, 2)
    feed(rig, 28, "Weekly sync")  # now is +2 min, so this starts at the same instant as the cloud one
    add_calendar(rig)
    assert [m["title"] for m in state(rig)["upcoming"]] == ["Weekly sync"]


def test_a_calendar_that_fails_keeps_the_last_meetings_and_says_why(rig: Rig, clock: Clock) -> None:
    feed(rig, 30)
    ident = add_calendar(rig)
    demo(rig).feed.clear()  # the link now answers 404
    rig.act("meetings.sync", {})
    s = state(rig)
    source = next(x for x in s["sources"] if x["id"] == ident)
    assert source["status"] == "error" and "404" in source["detail"]
    assert s["next"]["title"] == "Weekly sync"  # the last good answer still stands


@pytest.mark.parametrize(
    "link, message",
    [
        ("", "Paste the calendar link"),
        ("ftp://example.com/x.ics", "must start with https"),
        ("https://localhost/x.ics", "not at this network"),
        ("https://192.168.1.5/x.ics", "not at this network"),
        ("https://exa mple.com/x", "does not look like"),
    ],
)
def test_calendar_links_that_are_not_public_calendar_services_are_refused(rig: Rig, link: str, message: str) -> None:
    with pytest.raises(ActionError, match=message) as caught:
        rig.act("meetings.ics.add", {"link": link})
    assert caught.value.status == 400


def test_adding_a_link_that_is_not_a_calendar_or_does_not_answer_is_refused(rig: Rig) -> None:
    with pytest.raises(ActionError, match="HTTP 404"):
        rig.act("meetings.ics.add", {"link": LINK})
    demo(rig)._fetch = lambda url: "<html>please log in</html>"
    with pytest.raises(ActionError, match="not a calendar") as caught:
        rig.act("meetings.ics.add", {"link": LINK})
    assert caught.value.status == 400
    assert state(rig)["sources"][1:] == [] and demo(rig).secrets.names() == []


def test_without_a_keyring_nothing_is_stored_and_the_page_is_told(clock: Clock) -> None:
    controller = MeetingsController(
        config.MemoryStore(),
        MemorySecrets("There is no keyring."),
        fetch=lambda url: "BEGIN:VCALENDAR\nEND:VCALENDAR",
        clock=clock,
    )
    with pytest.raises(ActionError, match="no keyring") as caught:
        controller.act("meetings.ics.add", {"link": LINK})
    assert caught.value.status == 409
    with pytest.raises(ActionError, match="no keyring"):
        controller.act("meetings.m365.connect", {})
    controller.tick()
    assert controller.state()["keyring"] == {"ok": False, "reason": "There is no keyring."}
    assert controller.store.load()["ics"] == []


def test_a_calendar_is_removed_with_its_secret(rig: Rig) -> None:
    feed(rig, 30)
    ident = add_calendar(rig)
    rig.act("meetings.ics.remove", {"id": ident})
    assert state(rig)["sources"][1:] == [] and demo(rig).secrets.names() == []
    with pytest.raises(ActionError) as caught:
        rig.act("meetings.ics.remove", {"id": ident})
    assert caught.value.status == 404


@pytest.mark.parametrize(
    "params, message",
    [
        ({"lead_minutes": 99}, "lead_minutes"),
        ({"lead_minutes": True}, "lead_minutes"),
        ({"only_online": "yes"}, "only_online"),
        ({"steps": ["studio", "rm -rf"]}, "steps is a list"),
    ],
)
def test_settings_are_validated(rig: Rig, params: dict, message: str) -> None:
    with pytest.raises(ActionError, match=message):
        rig.act("meetings.settings", params)
    with pytest.raises(ActionError, match="on must be"):
        rig.act("meetings.enable", {"on": "yes"})


def test_settings_and_what_was_prepared_survive_a_restart_and_hold_no_secret(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "meetings.json"
    secrets = MemorySecrets()

    def make() -> MeetingsController:
        return MeetingsController(config.Store(path), secrets, fetch=lambda url: ICS, clock=clock)

    ICS = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\nSUMMARY:X\r\nDTSTART:20270115T080000Z\r\n"
        "DTEND:20270115T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    first = make()
    first.act("meetings.ics.add", {"link": LINK, "name": "Work"})
    first.act("meetings.settings", {"lead_minutes": 5, "steps": ["health"]})
    first._mark_prepped("m365:abc:1")
    second = make()
    s = second.state()
    assert (s["lead_minutes"], [x["id"] for x in s["steps"] if x["on"]]) == (5, ["health"])
    assert [x["name"] for x in s["sources"][1:]] == ["Work"]
    assert second.store.load()["prepped"] == ["m365:abc:1"]
    text = path.read_text()
    assert "abc123secret" not in text and (path.stat().st_mode & 0o777) == 0o600


def test_a_damaged_settings_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "meetings.json"
    path.write_text(
        '{"lead_minutes": 500, "steps": ["studio", "nonsense", "studio"], "ics": [{"id": "../x"}], "enabled": "no"}'
    )
    loaded = config.Store(path).load()
    assert (
        loaded["lead_minutes"] == 2
        and loaded["steps"] == ["studio"]
        and loaded["ics"] == []
        and loaded["enabled"] is True
    )


def test_the_worker_thread_starts_and_stops_and_prepares_on_its_own(clock: Clock) -> None:
    r = Rig.demo()
    r.meetings = DemoMeetings(clock)
    r.meetings.rig = r
    r.meetings._tick_seconds = 0.02
    r.start()
    try:
        r.act("video.privacy", {"on": True})
        r.act("meetings.demo.feed", {"link": LINK, "minutes": 1, "title": "Soon"})
        r.act("meetings.ics.add", {"link": LINK})
        deadline = threading.Event()
        for _ in range(200):
            if r.snapshot()["meetings"]["prep"]["state"] == "done":
                break
            deadline.wait(0.02)
        assert r.snapshot()["meetings"]["prep"]["state"] == "done" and r.snapshot()["video"]["privacy"] is False
    finally:
        r.stop()


class FeedHandler(BaseHTTPRequestHandler):
    body = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    def do_GET(self) -> None:
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        return None


def test_the_real_download_reads_text_and_reports_errors_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert console_meetings.default_fetch(f"{base}/ok.ics").startswith("BEGIN:VCALENDAR")
        with pytest.raises(ActionError, match="HTTP 404"):
            console_meetings.default_fetch(f"{base}/missing")
        monkeypatch.setattr(console_meetings, "ICS_BYTES", 10)
        with pytest.raises(ActionError, match="larger than"):
            console_meetings.default_fetch(f"{base}/big.ics")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    with pytest.raises(ActionError, match="could not be reached"):
        console_meetings.default_fetch(f"{base}/ok.ics")


def test_a_rig_without_meetings_reports_none_and_refuses_the_actions() -> None:
    bare = Rig.demo()
    bare.meetings = None
    assert bare.snapshot()["meetings"]["available"] is False
    with pytest.raises(ActionError) as caught:
        bare.act("meetings.sync", {})
    assert caught.value.status == 409
